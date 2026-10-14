from pathlib import Path

import pytest

from tk8s import versions
from tk8s.host import FakeHost, Result
from tk8s.paths import Paths
from tk8s.phases import Context, Options, Phase, PhaseFailed, Runner, tree

HOME = Path(__file__).resolve().parents[1]


def make_ctx(tmp_path: Path, **opts: bool) -> tuple[Context, FakeHost]:
    host = FakeHost()
    c = Context(
        host=host,
        paths=Paths(tmp_path, tmp_path / "wulin", HOME),
        versions=versions.load(HOME / "versions.yaml"),
        options=Options(**opts),
        say=host.lines.append,
    )
    return c, host


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
    p, _ = counter_phase("p", 1)
    Runner([p]).run(ctx)
    saved = model.load_text(host.files[ctx.state_dir / "cluster.yaml"])
    assert saved.status is not None and "p" in saved.status.completed
