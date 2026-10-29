import os
import stat
from pathlib import Path

import pytest

from tk8s.cli import main


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TK_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TK_OFFLINE", raising=False)
    return tmp_path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    rc = main(list(argv))
    out = capsys.readouterr()
    return rc, out.out + out.err


def fake_context(host: object, **kw: object) -> object:
    """verbs.make_context with a FakeHost and no waiting; assume_yes follows --yes."""
    from tests.fixtures import make_ctx
    from tk8s import verbs
    from tk8s.host import FakeHost
    from tk8s.paths import Paths

    assert isinstance(host, FakeHost)
    ctx = make_ctx(
        host,
        Path(os.environ["TK_DATA_DIR"]),
        user=None,
        say=print,
        assume_yes=bool(kw.get("assume_yes")),
    )
    ctx.paths = Paths.from_env(verbs.HOME)  # what the real make_context reads
    return ctx


def test_default_dry_run(capsys: pytest.CaptureFixture[str], data_dir: Path) -> None:
    rc, out = run(capsys, "create", "cluster", "--dry-run")
    assert rc == 0
    assert "name: tk8s\n" in out and "name: tk8s-worker-2" in out
    assert "cpu: 2\n" in out and "memory: 4g" in out
    assert "# ---- /etc/containers/systemd/tk8s/tk8s.network" in out
    assert "# ---- /etc/containers/systemd/tk8s/tk8s-worker-2.container" in out
    assert "kubernetesVersion: 1.37.1" in out
    assert "[plan] " in out and "[units] " in out and "[kubeadm/render] " in out
    assert "[images]" not in out and "[preflight]" not in out  # dry-run never touches the host
    assert not (data_dir / "clusters" / "tk8s").exists()


def test_flags(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = run(
        capsys,
        "create",
        "cluster",
        "demo",
        "--control-planes",
        "3",
        "--workers",
        "4",
        "--cpu",
        "4",
        "--memory",
        "8G",
        "--k8s",
        "1.36.5",
        "--dry-run",
    )
    assert rc == 0 and "name: demo-control-plane-3" in out and "name: demo-worker-4" in out
    assert 'kubernetes: "1.36.5"' in out and "router: 172.22." in out
    rc, out = run(
        capsys, "create", "cluster", "demo", "--control-planes", "3", "--defer-join", "--dry-run"
    )
    assert out.count("join: false") == 2
    rc, out = run(capsys, "create", "cluster", "demo", "--gvisor", "--dry-run")
    assert "gvisor: true" in out and "datapath: veth" in out
    rc, out = run(capsys, "create", "cluster", "demo", "--memory", "4096M", "--dry-run")
    assert rc == 0 and "memory: 4096m" in out
    rc, out = run(capsys, "create", "cluster", "demo", "--ha", "bgp", "--dry-run")
    assert rc == 0 and "ha: bgp" in out and "vip: 172.23." in out
    rc, out = run(capsys, "create", "cluster", "demo", "--dry-run")
    assert "ha: proxy" in out and "vip:" not in out


def test_workers_zero_builds_a_control_plane_only_cluster(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, out = run(capsys, "create", "cluster", "demo", "--workers", "0", "--dry-run")
    assert rc == 0
    assert "name: demo-control-plane-1" in out and "demo-worker" not in out
    assert "cluster demo: 1 control plane + 0 workers" in out
    assert "Wants=demo-router.service demo-control-plane-1.service demo-routes.service" in out


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (
            ["create", "cluster", "demo", "--defer-join", "--dry-run"],
            "--defer-join needs --control-planes 3 or more",
        ),
        (
            ["create", "cluster", "demo", "--gvisor", "--datapath", "netkit", "--dry-run"],
            "gvisor requires the veth datapath",
        ),
        (["create", "cluster", "Demo", "--dry-run"], "invalid cluster name 'Demo'"),
        (["create", "cluster", "tk8s.k8s", "--dry-run"], "invalid cluster name"),
        (["create", "cluster", "abcdefghijklmnopq", "--dry-run"], "max 16 characters"),
        (["create", "cluster", "demo", "--memory", "4Gi", "--dry-run"], "invalid --memory '4Gi'"),
        (["create", "cluster", "demo", "--memory", "4096", "--dry-run"], "invalid --memory"),
        (
            ["create", "cluster", "demo", "--control-planes", "0", "--dry-run"],
            "--control-planes must be a positive integer",
        ),
        (
            ["create", "cluster", "demo", "--control-planes", "2", "--dry-run"],
            "control-plane count 2 must be odd (1, 3, 5) for etcd quorum",
        ),
        (
            ["create", "cluster", "demo", "--workers", "-1", "--dry-run"],
            "--workers must be an integer",
        ),
        (["create", "cluster", "demo", "--control-planes", "7", "--dry-run"], "at most 5"),
        (
            ["create", "cluster", "-f", "x.yaml", "--ha", "bgp", "--dry-run"],
            "-f cannot be combined with --control-planes/--workers/--cpu/--memory/--ha/--routers",
        ),
        (
            ["create", "cluster", "-f", "x.yaml", "--ha=bgp", "--dry-run"],
            "-f cannot be combined with --control-planes/--workers/--cpu/--memory/--ha/--routers",
        ),
        (
            ["create", "cluster", "demo", "--k8s", "1.36", "--dry-run"],
            "--k8s must look like 1.37.0",
        ),
        (
            ["create", "cluster", "demo", "--colour", "red", "--dry-run"],
            "unrecognized arguments: --colour",
        ),
        (["frobnicate", "cluster", "demo"], "unknown command 'frobnicate'"),
        (["pull", "cluster"], "usage: tkctl pull images"),
        (["pull", "images", "--now"], "unrecognized arguments"),
        (["get", "images", "--k8s", "1.36"], "--k8s must look like 1.37.0"),
        (["delete", "images", "--yes"], "usage: tkctl delete images --k8s VERSION"),
    ],
)
def test_bad_invocations_fail_with_the_old_messages(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    rc, out = run(capsys, *argv)
    assert rc != 0 and message in out


TAKEN = (
    "apiVersion: taroko.io/v1alpha1\nkind: Cluster\nmetadata: {name: taken}\n"
    'spec:\n  kubernetes: "1.37.1"\n  cni: cilium\n  datapath: auto\n  gvisor: false\n'
    "  network: {index: 0, nodes: 172.22.0.0/24, gateway: 172.22.0.254, router: 172.22.0.253, "
    "lb_range: 172.22.0.200-172.22.0.219, pods: 10.244.0.0/21, services: 10.98.0.0/24}\n"
    "  nodes: [{role: control-plane, name: taken-control-plane-1, ip: 172.22.0.1, cpu: 2, "
    "memory: 4g, join: true}]\n"
    "status: {phase: Ready, tk8s: test}\n"
)


def test_existing_cluster_is_refused_and_indices_come_from_the_real_dir(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    taken = data_dir / "clusters" / "taken"
    taken.mkdir(parents=True)
    (taken / "cluster.yaml").write_text(TAKEN)
    rc, out = run(capsys, "create", "cluster", "taken", "--dry-run")
    assert rc == 1 and "cluster taken already exists; delete it first or pick another name" in out
    rc, out = run(capsys, "create", "cluster", "--dry-run")
    assert rc == 0 and "index: 1" in out
    (data_dir / "clash.yaml").write_text(
        "metadata: {name: clash}\nspec:\n  network: {nodes: 172.22.0.0/24}\n"
        "  nodes: [{role: control-plane}]\n"
    )
    rc, out = run(capsys, "create", "cluster", "-f", str(data_dir / "clash.yaml"), "--dry-run")
    assert rc == 1 and "already used" in out


def test_create_refuses_a_cluster_yaml_without_status(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    """A cluster.yaml this tkctl did not write (no status: a tk8s before v2026.10.7) is neither
    Ready nor a create to resume; resuming would re-resolve the topology from the flags and
    overwrite the file. create refuses and names the way out."""
    taken = data_dir / "clusters" / "taken"
    taken.mkdir(parents=True)
    legacy = TAKEN.replace("status: {phase: Ready, tk8s: test}\n", "")
    assert "status:" not in legacy
    (taken / "cluster.yaml").write_text(legacy)
    rc, out = run(capsys, "create", "cluster", "taken", "--control-planes", "3", "--dry-run")
    assert rc == 1
    assert "cluster taken has a cluster.yaml without status" in out
    assert "tkctl delete cluster taken" in out
    assert (taken / "cluster.yaml").read_text() == legacy


def test_file_input(capsys: pytest.CaptureFixture[str], data_dir: Path) -> None:
    f = data_dir / "c.yaml"
    f.write_text(
        "metadata: {name: fromfile}\nspec:\n  nodes: [{role: control-plane, cpu: 4, memory: 4G}, "
        "{role: worker, name: fromfile-big, cpu: 8, memory: 16G}]\n"
    )
    rc, out = run(capsys, "create", "cluster", "-f", str(f), "--dry-run")
    assert rc == 0 and "name: fromfile-big" in out and "memory: 16g" in out
    rc, out = run(capsys, "create", "cluster", "-f", str(f), "--workers", "3", "--dry-run")
    assert rc == 1 and "-f cannot be combined with --control-planes/--workers/--cpu/--memory" in out
    rc, out = run(capsys, "create", "cluster", "named", "-f", str(f), "--dry-run")
    assert rc == 1 and "the cluster name comes from metadata.name" in out
    rc, out = run(capsys, "create", "cluster", "-f", str(data_dir / "missing.yaml"), "--dry-run")
    assert rc == 1 and "file not found" in out
    rc, out = run(
        capsys, "create", "cluster", "-f", str(f), "--k8s", "1.36.5", "--gvisor", "--dry-run"
    )
    assert 'kubernetes: "1.36.5"' in out and "gvisor: true" in out and "datapath: veth" in out


def test_phase_flags(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = run(capsys, "create", "cluster", "--list-phases")
    assert rc == 0 and out.splitlines()[0].startswith("preflight") and "  render" in out
    rc, out = run(capsys, "create", "cluster", "--dry-run", "--phase", "units")
    assert rc == 0 and "[units]" in out and "[plan]" not in out and "[kubeadm/render]" not in out
    rc, out = run(capsys, "create", "cluster", "--dry-run", "--skip-phases", "kubeadm")
    assert "[kubeadm/render] skipped" in out and "kubernetesVersion" not in out
    rc, out = run(capsys, "create", "cluster", "--dry-run", "--phase", "nope")
    assert rc == 1 and "unknown phase 'nope'" in out


def test_help_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = run(capsys, "--help")
    assert rc == 0
    for s in (
        "create cluster",
        "add node",
        "pull images",
        "at most 5",
        "TK_OFFLINE",
        "--refresh",
        "--phase",
    ):
        assert s in out, s
    assert "kube-vip" not in out
    rc, out = run(capsys, "version")
    assert rc == 0 and "kubernetes default 1.37.1" in out


def test_plugins_follow_kubectl_rules(
    capsys: pytest.CaptureFixture[str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plug = data_dir / "plugins"
    plug.mkdir()
    for name in ("tkctl-hello", "tkctl-hello-world", "tkctl-create"):
        f = plug / name
        f.write_text("#!/bin/sh\necho never\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{plug}{os.pathsep}{os.environ['PATH']}")
    execs: list[list[str]] = []

    def fake_execvp(file: str, args: list[str]) -> None:
        execs.append(args)
        raise SystemExit(0)

    monkeypatch.setattr("tk8s.cli.os.execvp", fake_execvp)
    with pytest.raises(SystemExit):
        main(["hello", "create", "class", "k8s-101", "--students", "3"])
    assert execs[-1] == [str(plug / "tkctl-hello"), "create", "class", "k8s-101", "--students", "3"]
    with pytest.raises(SystemExit):
        main(["hello", "world", "a", "--x"])
    assert execs[-1] == [str(plug / "tkctl-hello-world"), "a", "--x"]  # longest match wins
    _rc, out = run(capsys, "create", "cluster", "--dry-run")
    assert "name: tk8s" in out and len(execs) == 2  # a plugin cannot shadow a built-in verb
    _, out = run(capsys, "--help")
    assert "Plugins found on PATH: create hello hello-world" in out
    assert main(["frobnicate", "cluster", "demo"]) != 0 and len(execs) == 2


def test_plugins_exec_with_the_callers_environment(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plug = data_dir / "plugins"
    plug.mkdir()
    f = plug / "tkctl-lab"
    f.write_text("#!/bin/sh\necho lab\n")
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{plug}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("TK_LAB_PVE_TOKEN", "secret")
    seen: dict[str, object] = {}

    def fake_execvp(file: str, args: list[str]) -> None:
        seen["file"], seen["args"], seen["env"] = file, args, dict(os.environ)
        raise SystemExit(0)

    monkeypatch.setattr("tk8s.cli.os.execvp", fake_execvp)
    with pytest.raises(SystemExit):
        main(["lab", "create", "class", "k8s-101"])
    assert seen["file"] == str(f) and seen["args"] == [str(f), "create", "class", "k8s-101"]
    assert seen["env"]["TK_LAB_PVE_TOKEN"] == "secret"  # type: ignore[index]


def test_host_and_preflight_errors_are_one_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tk8s.datatree import PreflightError
    from tk8s.host import HostError

    def boom(ctx: object, k8s: str) -> None:
        raise PreflightError("/opt/taroko is owned by someone, not student")

    monkeypatch.setattr("tk8s.verbs.images.ensure", boom)
    rc, out = run(capsys, "pull", "images")
    assert rc == 1 and out.strip() == "tkctl: /opt/taroko is owned by someone, not student"

    def boom2(ctx: object, k8s: str) -> None:
        raise HostError(["curl", "x"], 22, "The requested URL returned error: 404")

    monkeypatch.setattr("tk8s.verbs.images.ensure", boom2)
    rc, out = run(capsys, "pull", "images")
    assert rc == 1 and out.startswith("tkctl: curl x exited 22")


def test_a_broken_cluster_yaml_of_another_cluster_does_not_block_create(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    old = data_dir / "clusters" / "old"
    old.mkdir(parents=True)
    (old / "cluster.yaml").write_text(
        "metadata: {name: old}\nspec:\n  runtime: crio\n  network: {index: 0}\n  nodes: []\n"
    )
    rc, out = run(capsys, "create", "cluster", "--dry-run")
    assert rc == 0 and "index: 1" in out  # the old file's index still counts


def test_create_runs_every_phase_and_marks_the_cluster_ready(
    capsys: pytest.CaptureFixture[str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures import ready_host
    from tk8s import model, verbs
    from tk8s.host import FakeHost

    host = FakeHost()
    monkeypatch.setattr(verbs, "make_context", lambda **kw: fake_context(host, **kw))
    ready_host(host, data_dir)
    rc, out = run(capsys, "create", "cluster", "demo")
    assert rc == 0
    phases = [line.split("]")[0][1:] for line in out.splitlines() if line.startswith("[")]
    assert phases == [
        "preflight",
        "plan",
        "images",
        "units",
        "nodes/cni-plugins",
        "nodes/up",
        "kubeadm/render",
        "kubeadm/frr",
        "kubeadm/init",
        "kubeadm/kubeconfig",
        "kubeadm/join",
        "cni/install",
        "cni/nodes-ready",
        "addons/runtimeclass",
        "addons/gvisor",
        "addons/metrics-server",
        "addons/local-path",
    ]
    assert "cluster demo is ready" in out and "tkctl use cluster demo" in out
    saved = model.load_text(host.files[data_dir / "clusters" / "demo" / "cluster.yaml"])
    assert saved.status is not None and saved.status.phase == "Ready"


def test_verify_cluster_verb(
    capsys: pytest.CaptureFixture[str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tk8s import verify

    rc, out = run(capsys, "verify", "cluster")
    assert rc == 1 and "usage: tkctl verify cluster NAME [--phase P]" in out
    rc, out = run(capsys, "verify", "cluster", "nope")
    assert rc == 1
    assert f"cluster nope not found under {data_dir}/clusters (tkctl get clusters)" in out
    calls: list[tuple[object, str | None]] = []

    def fake_run(ctx: object, only: str | None = None) -> int:
        calls.append((ctx, only))
        return 1

    monkeypatch.setattr(verify, "run", fake_run)
    (data_dir / "clusters" / "demo").mkdir(parents=True)
    (data_dir / "clusters" / "demo" / "cluster.yaml").write_text(TAKEN.replace("taken", "demo"))
    monkeypatch.setenv("TK_VERIFY_TIMEOUT", "30")
    rc, out = run(capsys, "verify", "cluster", "demo", "--phase", "nodes")
    assert rc == 1 and calls[0][1] == "nodes"
    assert calls[0][0].options.verify_timeout == 30  # type: ignore[attr-defined]
    rc, out = run(capsys, "verify", "cluster", "--list-phases")
    assert rc == 0 and out.splitlines()[0].startswith("nodes") and "tcproute" in out


def test_stdout_is_line_buffered_so_redirected_logs_show_each_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import sys

    from tk8s import cli

    fake = io.TextIOWrapper(io.BytesIO(), line_buffering=False)
    monkeypatch.setattr(sys, "stdout", fake)
    cli.main(["version"])
    assert fake.line_buffering  # a `tkctl create … > log` shows every phase as it happens


def test_skipped_phases_leave_the_cluster_creating(
    capsys: pytest.CaptureFixture[str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures import ready_host
    from tk8s import model, verbs
    from tk8s.host import FakeHost

    host = FakeHost()
    monkeypatch.setattr(verbs, "make_context", lambda **kw: fake_context(host, **kw))
    ready_host(host, data_dir)
    c = model.load_text(host.files[data_dir / "clusters" / "demo" / "cluster.yaml"])
    assert c.status is not None
    host.files[data_dir / "clusters" / "demo" / "cluster.yaml"] = model.dumps(
        c.model_copy(update={"status": c.status.model_copy(update={"phase": "Creating"})})
    )
    rc, out = run(capsys, "create", "cluster", "demo", "--skip-phases", "cni")
    assert rc == 0 and "cluster demo is ready" not in out
    assert "not marked Ready: skipped cni; rerun tkctl create cluster demo to finish" in out
    saved = model.load_text(host.files[data_dir / "clusters" / "demo" / "cluster.yaml"])
    assert saved.status is not None and saved.status.phase == "Creating"


def test_cluster_verbs_reach_lifecycle(
    capsys: pytest.CaptureFixture[str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures import ready_host
    from tk8s import verbs
    from tk8s.host import FakeHost

    host = FakeHost()
    asked: list[dict[str, object]] = []

    def context(**kw: object) -> object:
        asked.append(kw)
        return fake_context(host, **kw)

    monkeypatch.setattr(verbs, "make_context", context)
    c = ready_host(host, data_dir)
    state = data_dir / "clusters" / "demo"
    state.mkdir(parents=True)
    (state / "cluster.yaml").write_text(host.files[state / "cluster.yaml"])
    rc, out = run(capsys, "get", "clusters")
    assert rc == 0 and out.startswith("NAME") and "demo " in out
    assert asked[-1].get("quiet") is True  # read-only verbs print no `$` lines
    rc, out = run(capsys, "describe", "cluster", "demo")
    assert rc == 0 and "Name:        demo" in out and "Status:" in out
    assert asked[-1].get("quiet") is True
    rc, out = run(capsys, "describe", "cluster", "demo", "-o", "yaml")
    assert rc == 0 and out.startswith("apiVersion")
    rc, out = run(capsys, "stop", "cluster", "demo")
    assert rc == 0 and out.rstrip().endswith("cluster demo stopped")
    assert not asked[-1].get("quiet")
    rc, out = run(capsys, "start", "cluster", "demo")
    assert rc == 0 and ["systemctl", "start", "demo.target"] in [k.argv for k in host.calls]
    monkeypatch.setenv("HOME", str(data_dir / "home"))
    rc, out = run(capsys, "use", "cluster", "demo")
    assert rc == 0 and "~/.kube/config: current context demo" in out
    assert c.name == "demo"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["stop", "cluster"], "usage: tkctl stop cluster NAME"),
        (["start"], "usage: tkctl start cluster NAME"),
        (["use", "cluster", "Nope"], "invalid cluster name 'Nope'"),
        (["describe", "cluster", "nope"], "cluster nope not found under"),
        (["stop", "cluster", "nope"], "cluster nope not found under"),
        (["get", "nothing"], "usage: tkctl get clusters | tkctl get images [--k8s VERSION]"),
    ],
)
def test_cluster_verb_usage_errors(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    rc, out = run(capsys, *argv)
    assert rc != 0 and message in out, out


def test_delete_cluster_verb(
    capsys: pytest.CaptureFixture[str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures import ready_host
    from tk8s import verbs
    from tk8s.host import FakeHost

    host = FakeHost()
    monkeypatch.setattr(verbs, "make_context", lambda **kw: fake_context(host, **kw))
    ready_host(host, data_dir)
    state = data_dir / "clusters" / "demo"
    state.mkdir(parents=True)
    (state / "cluster.yaml").write_text(host.files[state / "cluster.yaml"])
    host.dirs.add(state)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    rc, out = run(capsys, "delete", "cluster", "demo")
    assert rc == 1 and out.rstrip().endswith("tkctl: aborted")
    assert (state / "cluster.yaml").exists()
    rc, out = run(capsys, "delete", "cluster", "demo", "--list-phases")
    assert rc == 0 and out.splitlines()[0].startswith("delete") and "  state" in out
    rc, out = run(capsys, "delete", "cluster", "demo", "--yes", "--phase", "delete/stop")
    assert rc == 0 and "[delete/stop]" in out and "deleted" not in out
    rc, out = run(capsys, "delete", "cluster", "demo", "--yes")
    assert rc == 0 and "[delete/stop]" in out and out.rstrip().endswith("cluster demo deleted")
    assert ["rm", "-rf", str(state)] in [k.argv for k in host.calls]
    rc, out = run(capsys, "delete", "cluster", "nothing", "--yes")
    assert rc == 1 and "cluster nothing not found: no state under" in out and "no network" in out
    rc, out = run(capsys, "delete", "cluster")
    assert rc == 1 and "usage: tkctl delete cluster NAME [--yes]" in out
    (state / "cluster.yaml").write_text("metadata: {name: demo}\nspec: {runtime: crio}\n")
    host.dirs.add(state)
    rc, out = run(capsys, "delete", "cluster", "demo", "--yes")
    assert (
        rc == 0 and "cluster.yaml" in out and "cluster demo deleted" in out
    )  # reported, not fatal


def test_tk_assume_yes_is_read_once_in_make_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from tk8s import verbs

    monkeypatch.delenv("TK_ASSUME_YES", raising=False)
    assert not verbs.make_context().options.assume_yes
    assert verbs.make_context(assume_yes=True).options.assume_yes
    monkeypatch.setenv("TK_ASSUME_YES", "1")
    assert verbs.make_context().options.assume_yes


def test_node_verbs(
    capsys: pytest.CaptureFixture[str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures import new_node_answers, ready_host
    from tk8s import model, verbs
    from tk8s.host import FakeHost, Result

    host = FakeHost()
    monkeypatch.setattr(verbs, "make_context", lambda **kw: fake_context(host, **kw))
    c = ready_host(host, data_dir)
    state = data_dir / "clusters" / "demo"
    state.mkdir(parents=True)
    new_node_answers(host, "demo-worker-3", ready_after=0)
    assert c.status is not None
    ready = c.model_copy(update={"status": c.status.model_copy(update={"phase": "Ready"})})
    (state / "cluster.yaml").write_text(model.dumps(ready))
    host.files[state / "cluster.yaml"] = model.dumps(ready)
    rc, out = run(capsys, "add", "node", "demo", "--role", "worker", "--no-join")
    assert rc == 0 and "[add/plan]" in out
    assert out.rstrip().endswith(
        "demo-worker-3 left unjoined; join with: tkctl join node demo demo-worker-3"
    )
    (state / "cluster.yaml").write_text(
        host.files[state / "cluster.yaml"]
    )  # the next verb reads disk
    host.active.add("demo-worker-3.service")
    rc, out = run(capsys, "join", "node", "demo", "demo-worker-3")
    assert rc == 0 and "[join/kubeadm]" in out
    assert out.rstrip().endswith("node demo-worker-3 joined cluster demo")
    (state / "cluster.yaml").write_text(host.files[state / "cluster.yaml"])
    host.podman.add("container:demo-worker-3")
    host.responses[("kubectl", "get", "node", "demo-worker-3")] = [
        Result(0, "", ""),
        Result(1, "", 'Error from server (NotFound): nodes "demo-worker-3" not found'),
    ]  # joined, then gone
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    rc, out = run(capsys, "delete", "node", "demo", "demo-worker-3")
    assert rc == 1 and out.rstrip().endswith("tkctl: aborted")
    rc, out = run(capsys, "delete", "node", "demo", "demo-worker-3", "--yes")
    assert rc == 0 and "[delete/drain]" in out
    assert out.rstrip().endswith("node demo-worker-3 removed")
    rc, out = run(capsys, "delete", "node", "demo", "demo-control-plane-1", "--yes")
    assert rc == 1 and "the first control plane demo-control-plane-1 cannot be deleted" in out


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["add", "node", "demo"], "--role must be worker or control-plane"),
        (["add", "node", "demo", "--role", "lb"], "--role must be worker or control-plane"),
        (
            ["add", "node", "demo", "--role", "worker", "--memory", "4Gi"],
            "invalid --memory '4Gi'",
        ),
        (
            ["add", "node"],
            "usage: tkctl add node CLUSTER --role worker|control-plane [--cpu N] [--memory SIZE] "
            "[--no-join]",
        ),
        (["join", "node", "demo"], "usage: tkctl join node CLUSTER NODE"),
        (["delete", "node", "demo"], "usage: tkctl delete node CLUSTER NODE [--yes]"),
        (["add", "node", "nope", "--role", "worker"], "cluster nope not found under"),
    ],
)
def test_node_verb_usage_errors(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    rc, out = run(capsys, *argv)
    assert rc != 0 and message in out, out


def test_add_node_refuses_a_cluster_still_creating(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    from tests.fixtures import ready_host
    from tk8s import model
    from tk8s.host import FakeHost

    c = ready_host(FakeHost(), data_dir)  # resolve() leaves status.phase at Creating
    state = data_dir / "clusters" / "demo"
    state.mkdir(parents=True)
    (state / "cluster.yaml").write_text(model.dumps(c))
    rc, out = run(capsys, "add", "node", "demo", "--role", "worker")
    assert rc == 1 and "cluster demo is not fully created (tkctl get clusters)" in out


def test_node_verbs_need_a_running_cluster(
    capsys: pytest.CaptureFixture[str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures import ready_host
    from tk8s import model, verbs
    from tk8s.host import FakeHost

    host = FakeHost()
    monkeypatch.setattr(verbs, "make_context", lambda **kw: fake_context(host, **kw))
    c = ready_host(host, data_dir)
    assert c.status is not None
    ready = c.model_copy(update={"status": c.status.model_copy(update={"phase": "Ready"})})
    state = data_dir / "clusters" / "demo"
    state.mkdir(parents=True)
    (state / "cluster.yaml").write_text(model.dumps(ready))
    host.active.discard("demo.target")  # Stopped
    for argv in (
        ["add", "node", "demo", "--role", "worker"],
        ["join", "node", "demo", "demo-worker-2"],
        ["delete", "node", "demo", "demo-worker-2", "--yes"],
    ):
        rc, out = run(capsys, *argv)
        assert rc == 1 and "cluster demo is not running (tkctl start cluster demo)" in out, argv


def test_delete_node_lists_its_phases_and_accepts_short_names(
    capsys: pytest.CaptureFixture[str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, out = run(capsys, "delete", "node", "--list-phases")
    assert (
        rc == 0
        and out.splitlines()[0].startswith("delete")
        and "  drain" in out
        and "  entry" in out
    )


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["join", "node", "demo", "../x"], "invalid node name '../x'"),
        (
            ["delete", "node", "demo", "Demo-control-plane-1", "--yes"],
            "invalid node name 'Demo-control-plane-1'",
        ),
        (["join", "node", "demo", "demo-worker-1/x"], "invalid node name"),
    ],
)
def test_node_names_are_validated_before_anything_runs(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    rc, out = run(capsys, *argv)
    assert rc != 0 and message in out, out


def test_a_closed_pipe_ends_quietly(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tkctl version | head -1`: the reader goes away, tkctl must not print a traceback."""
    from tk8s import cli

    def boom(args: list[str]) -> int:
        raise BrokenPipeError

    monkeypatch.setattr(cli, "_main", boom)
    assert main(["version"]) == 0
    assert capsys.readouterr().err == ""


def test_ctrl_c_is_one_line_and_130(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tk8s import cli

    def boom(args: list[str]) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_main", boom)
    assert main(["create", "cluster"]) == 130
    assert capsys.readouterr().err == "tkctl: interrupted\n"


# ---- --routers and --preset


def test_preset_ha_expands_and_prints_the_flags(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = run(capsys, "create", "cluster", "demo", "--preset", "ha", "--dry-run")
    assert rc == 0
    assert "  preset ha: --control-planes 3 --workers 3 --ha bgp --routers 2\n" in out
    assert "  cluster demo: 3 control planes + 3 workers," in out
    assert "    ha: bgp\n    routers: 2\n" in out


def test_preset_lab_is_todays_default(capsys: pytest.CaptureFixture[str]) -> None:
    _, plain = run(capsys, "create", "cluster", "--dry-run")
    _, lab = run(capsys, "create", "cluster", "--preset", "lab", "--dry-run")
    line = "  preset lab: --control-planes 1 --workers 2 --ha proxy --routers 1\n"
    assert line in lab and lab.replace(line, "") == plain


@pytest.mark.parametrize(
    ("extra", "flag"),
    [
        (["--workers", "1"], "--workers"),
        (["--control-planes", "3"], "--control-planes"),
        (["--ha=bgp"], "--ha"),
        (["--routers", "2"], "--routers"),
        (["--defer-join"], "--defer-join"),
        (["-f", "x.yaml"], "-f"),
    ],
)
def test_preset_refuses_a_topology_flag_even_with_the_same_value(
    capsys: pytest.CaptureFixture[str], extra: list[str], flag: str
) -> None:
    rc, out = run(capsys, "create", "cluster", "demo", "--preset", "ha", *extra, "--dry-run")
    assert rc == 1
    assert f"--preset ha cannot be combined with {flag}; use the expanded flags instead" in out


def test_routers_flag_reaches_cluster_yaml(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = run(capsys, "create", "cluster", "demo", "--routers", "2", "--dry-run")
    assert rc == 0 and "    ha: proxy\n    routers: 2\n" in out


def test_preset_cannot_be_bypassed_by_an_abbreviated_or_glued_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """argparse would accept --work for --workers and -fx.yaml for -f x.yaml; neither may slip
    past the no-mixing rule (the first is refused outright, the second is seen as -f)."""
    rc, out = run(capsys, "create", "cluster", "demo", "--preset", "ha", "--work", "5", "--dry-run")
    assert rc == 2 and "unrecognized arguments: --work 5" in out
    rc, out = run(capsys, "create", "cluster", "demo", "--preset", "ha", "-fx.yaml", "--dry-run")
    assert rc == 1 and "--preset ha cannot be combined with -f" in out


# ---- --hosts and --ip-range


def test_hosts_flag_expands_to_spec_hosts_and_range() -> None:
    from tk8s.args import parse_hosts_flag

    hosts = parse_hosts_flag("a,b=tux@h:2222,pve-c")
    assert [(h.name, h.ssh) for h in hosts] == [
        ("a", "a"),
        ("b", "tux@h:2222"),
        ("pve-c", "pve-c"),
    ]


def test_dry_run_multi_host_prints_hosts_placement_and_remote_files(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, out = run(
        capsys,
        "create",
        "cluster",
        "demo",
        "--control-planes",
        "3",
        "--hosts",
        "a,b=tux@192.168.1.231,c=192.168.1.232",
        "--ip-range",
        "192.168.1.64-192.168.1.95",
        "--dry-run",
    )
    assert rc == 0
    assert "  host a: eth0, shim 192.168.1.65\n" in out  # one router: no router-1/-2 addresses
    assert (
        "  placement: demo-control-plane-1 a, demo-control-plane-2 b, demo-control-plane-3 c" in out
    )
    assert "  routers: demo-router a\n" in out
    assert "warning" not in out
    assert "    range: 192.168.1.64-192.168.1.95\n" in out and "  operator: dry-run\n" in out
    assert (
        "# ---- tux@192.168.1.231:/etc/containers/systemd/demo/demo-control-plane-2.container"
        in out
    )


def test_dry_run_warns_when_one_host_holds_most_control_planes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, out = run(
        capsys,
        "create",
        "cluster",
        "--control-planes",
        "3",
        "--hosts",
        "a,b=h",
        "--ip-range",
        "192.168.1.64-192.168.1.95",
        "--dry-run",
    )
    assert rc == 0
    assert "  warning: host a holds 2 of 3 control planes; losing it stops the apiserver" in out


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--ip-range", "192.168.1.64-192.168.1.95"], "--ip-range needs --hosts"),
        (["--hosts", "a,b"], "--hosts needs --ip-range A-B"),
        (["--hosts", "a,b", "--ip-range", "x", "-f", "c.yaml"], "-f cannot be combined with"),
        (["--hosts", "A=h", "--ip-range", "x"], "invalid host name 'A'"),
        (["--hosts", "a,192.168.1.2", "--ip-range", "x"], "invalid host name '192.168.1.2'"),
    ],
)
def test_hosts_flag_mistakes_are_usage_errors(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    rc, out = run(capsys, "create", "cluster", *argv, "--dry-run")
    assert rc == 1 and message in out
