import time
from pathlib import Path

import pytest

from tests.fixtures import make_ctx as fixture_ctx
from tk8s.host import FakeHost, Result
from tk8s.phases import (
    Context,
    Phase,
    PhaseFailed,
    Runner,
    confirm,
    poll_until,
    tree,
    wait_for,
)

HOME = Path(__file__).resolve().parents[1]


def make_ctx(tmp_path: Path, **opts: bool) -> tuple[Context, FakeHost]:
    host = FakeHost()
    ctx = fixture_ctx(host, tmp_path, user=None)
    for k, v in opts.items():
        setattr(ctx.options, k, v)
    return ctx, host


def counter_phase(name: str, needed_runs: int) -> tuple[Phase, dict[str, int]]:
    """done() after needed_runs calls of run()."""
    state = {"runs": 0}

    def done(ctx: Context) -> bool:
        return state["runs"] >= needed_runs

    def run(ctx: Context) -> None:
        state["runs"] += 1

    return Phase(name, f"do {name}", done, run), state


def test_done_phases_are_skipped_and_the_rest_run_in_order(tmp_path: Path) -> None:
    ctx, host = make_ctx(tmp_path)
    a, sa = counter_phase("a", 0)  # already done
    b, sb = counter_phase("b", 1)
    Runner([a, b]).run(ctx)
    assert (sa["runs"], sb["runs"]) == (0, 1)
    assert host.lines == ["[a] already done", "[b] do b"]


def test_ran_but_not_done_is_a_bug_not_a_retry(tmp_path: Path) -> None:
    ctx, _ = make_ctx(tmp_path)
    p, s = counter_phase("p", 2)
    with pytest.raises(PhaseFailed, match=r"\[p\] ran but is not done"):
        Runner([p]).run(ctx)
    assert s["runs"] == 1


def test_only_and_skip_select_leaves_and_subtrees(tmp_path: Path) -> None:
    ctx, host = make_ctx(tmp_path)
    r1, _ = counter_phase("render", 1)
    i1, _ = counter_phase("init", 1)
    kube = Phase("kubeadm", "kubeadm", lambda c: False, lambda c: None, children=(r1, i1))
    other, _ = counter_phase("other", 1)
    Runner([kube, other], only="kubeadm/init").run(ctx)
    assert host.lines == ["[kubeadm/init] do init"]
    host.lines.clear()
    Runner([kube, other], skip={"kubeadm"}).run(ctx)
    assert host.lines == ["[kubeadm/render] skipped", "[kubeadm/init] skipped", "[other] do other"]
    with pytest.raises(
        PhaseFailed, match="unknown phase 'nope'; phases: kubeadm/render, kubeadm/init, other"
    ):
        Runner([kube, other], only="nope").run(ctx)


def test_host_failure_becomes_a_phase_failure_with_rerun_hint(tmp_path: Path) -> None:
    ctx, host = make_ctx(tmp_path)
    host.responses[("podman", "pull")] = Result(125, "", "manifest unknown")

    def pull(c: Context) -> None:
        c.host.run(["podman", "pull", "i"])

    boom = Phase("x", "x", lambda c: False, pull)
    with pytest.raises(PhaseFailed) as e:
        Runner([boom]).run(ctx)
    assert "[x] failed: podman pull i exited 125: manifest unknown" in str(e.value)


def test_tree_lists_children_indented(tmp_path: Path) -> None:
    r1, _ = counter_phase("render", 1)
    kube = Phase("kubeadm", "init and join", lambda c: False, lambda c: None, children=(r1,))
    assert tree([kube]) == ["kubeadm            init and join", "  render             do render"]


def test_dry_run_never_checks_done_after_run(tmp_path: Path) -> None:
    ctx, host = make_ctx(tmp_path, dry_run=True)
    p, s = counter_phase("p", 99)  # would never be done
    Runner([p]).run(ctx)
    assert s["runs"] == 1 and host.lines == ["[p] do p", ""]


def test_completed_phases_are_recorded_in_cluster_yaml_through_the_host(ctx: Context) -> None:
    from tk8s import model

    host = ctx.host
    assert isinstance(host, FakeHost)
    host.files[ctx.state_dir / "cluster.yaml"] = model.dumps(ctx.require_cluster())  # plan ran
    p, _ = counter_phase("p", 1)
    Runner([p]).run(ctx)
    saved = model.load_text(host.files[ctx.state_dir / "cluster.yaml"])
    assert saved.status is not None and "p" in saved.status.completed


def test_a_phase_without_completion_state_runs_every_time(tmp_path: Path) -> None:
    ctx, host = make_ctx(tmp_path)
    runs = {"n": 0}

    def run(c: Context) -> None:
        runs["n"] += 1

    always = Phase("preflight", "check the host", None, run)
    Runner([always]).run(ctx)
    Runner([always]).run(ctx)
    assert runs["n"] == 2 and host.lines == ["[preflight] check the host"] * 2


def test_a_done_check_that_hits_the_host_fails_the_phase_not_the_program(tmp_path: Path) -> None:
    ctx, host = make_ctx(tmp_path)
    host.responses[("podman", "build")] = Result(1, "", "build broke")

    def done(c: Context) -> bool:
        c.host.run(["podman", "build", "x"])
        return True

    p = Phase("images", "x", done, lambda c: None)
    with pytest.raises(PhaseFailed, match=r"\[images\] failed: podman build x exited 1"):
        Runner([p]).run(ctx)


def test_nothing_is_recorded_before_plan_wrote_cluster_yaml(ctx: Context) -> None:
    """preflight completes before plan; recording it would create cluster.yaml early and make
    plan look already done."""
    from tk8s import model

    host = ctx.host
    assert isinstance(host, FakeHost)
    first = Phase("preflight", "check", None, lambda c: None)
    ran: list[str] = []

    def plan_run(c: Context) -> None:
        ran.append("plan")
        c.host.write(c.state_dir / "cluster.yaml", model.dumps(c.require_cluster()))

    plan = Phase("plan", "plan", lambda c: c.host.exists(c.state_dir / "cluster.yaml"), plan_run)
    Runner([first, plan]).run(ctx)
    assert ran == ["plan"] and host.lines == ["[preflight] check", "[plan] plan"]
    saved = model.load_text(host.files[ctx.state_dir / "cluster.yaml"])
    assert saved.status is not None and list(saved.status.completed) == ["plan"]


def test_record_false_leaves_status_completed_alone(ctx: Context, host: FakeHost) -> None:
    from tk8s import model

    c = ctx.require_cluster()
    host.files[ctx.state_dir / "cluster.yaml"] = model.dumps(c)
    phase, _ = counter_phase("x", 1)
    Runner([phase], record=False).run(ctx)
    saved = model.load_text(host.files[ctx.state_dir / "cluster.yaml"])
    assert saved.status is not None and "x" not in saved.status.completed
    again, _ = counter_phase("x", 1)  # a fresh phase: the first one is done now
    Runner([again]).run(ctx)  # the default still records
    saved = model.load_text(host.files[ctx.state_dir / "cluster.yaml"])
    assert saved.status is not None and "x" in saved.status.completed


def test_context_name_stands_in_for_a_missing_cluster(tmp_path: Path) -> None:
    ctx, _ = make_ctx(tmp_path)
    with pytest.raises(PhaseFailed):
        _ = ctx.state_dir
    ctx.name = "gone"
    assert ctx.cluster_name == "gone" and ctx.state_dir == tmp_path / "clusters" / "gone"


def test_only_accepts_a_leaf_name_as_well_as_the_full_path(tmp_path: Path) -> None:
    ctx, _ = make_ctx(tmp_path)
    a, sa = counter_phase("a", 1)
    b, sb = counter_phase("b", 1)
    parent = Phase("p", "parent", None, lambda c: None, children=(a, b))
    Runner([parent], only="b").run(ctx)
    assert (sa["runs"], sb["runs"]) == (0, 1)
    Runner([parent], only="p/a").run(ctx)
    assert (sa["runs"], sb["runs"]) == (1, 1)


def test_confirm_takes_y_refuses_the_rest_and_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = make_ctx(tmp_path)
    answers = iter(["y", "n", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    confirm(ctx, "Delete it? [y/N] ", PhaseFailed)
    with pytest.raises(PhaseFailed, match="aborted"):
        confirm(ctx, "Delete it? [y/N] ", PhaseFailed)
    with pytest.raises(PhaseFailed, match="aborted"):
        confirm(ctx, "Delete it? [y/N] ", PhaseFailed)

    def eof(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    with pytest.raises(PhaseFailed, match="aborted"):
        confirm(ctx, "Delete it? [y/N] ", PhaseFailed)
    ctx.options.assume_yes = True
    confirm(ctx, "never asked", PhaseFailed)


def test_wait_for_polls_rounds_times_then_fails_with_the_callers_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = make_ctx(tmp_path)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    seen = iter([False, False, True])
    wait_for(ctx, lambda: next(seen), rounds=5, delay=2.0, failure=lambda: "never")
    assert slept == [2.0, 2.0]  # no sleep after the probe that held
    with pytest.raises(PhaseFailed, match="gave up after 3"):
        wait_for(ctx, lambda: False, rounds=3, delay=0.5, failure=lambda: "gave up after 3")
    assert slept == [2.0, 2.0, 0.5, 0.5]  # rounds - 1 sleeps, none after the last probe
    assert poll_until(lambda: False, rounds=0, delay=0) is False
