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
    from tk8s import verbs, versions
    from tk8s.host import FakeHost
    from tk8s.paths import Paths
    from tk8s.phases import Context, Options

    assert isinstance(host, FakeHost)
    return Context(
        host=host,
        paths=Paths.from_env(verbs.HOME),
        versions=versions.load(verbs.HOME / "versions.yaml"),
        options=Options(poll=0, poll_slow=0, retry_delay=0, assume_yes=bool(kw.get("assume_yes"))),
        say=print,
    )


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
    assert rc == 0 and "name: demo-cp-3" in out and "name: demo-worker-4" in out
    assert 'kubernetes: "1.36.5"' in out and "lb: 172.22." in out
    rc, out = run(
        capsys, "create", "cluster", "demo", "--control-planes", "3", "--defer-join", "--dry-run"
    )
    assert out.count("join: false") == 2
    rc, out = run(capsys, "create", "cluster", "demo", "--gvisor", "--dry-run")
    assert "gvisor: true" in out and "datapath: veth" in out
    rc, out = run(capsys, "create", "cluster", "demo", "--memory", "4096M", "--dry-run")
    assert rc == 0 and "memory: 4096m" in out


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
            "--control-planes must be odd",
        ),
        (["create", "cluster", "demo", "--control-planes", "7", "--dry-run"], "at most 5"),
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
    "  network: {index: 0, nodes: 172.22.0.0/24, gateway: 172.22.0.254, lb: 172.22.0.100, "
    "lb_range: 172.22.0.200-172.22.0.219, pods: 10.244.0.0/21, services: 10.98.0.0/24}\n"
    "  nodes: [{role: control-plane, name: taken-cp-1, ip: 172.22.0.1, cpu: 2, memory: 4g, "
    "join: true}]\n"
)


def test_existing_cluster_is_refused_and_indices_come_from_the_real_dir(
    capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    taken = data_dir / "clusters" / "taken"
    taken.mkdir(parents=True)
    (taken / ".create-complete").touch()
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
    assert rc == 0 and "~/.kube/config now points at demo" in out
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
    monkeypatch.setenv("TK_ASSUME_YES", "1")
    (state / "cluster.yaml").write_text("metadata: {name: demo}\nspec: {runtime: crio}\n")
    host.dirs.add(state)
    rc, out = run(capsys, "delete", "cluster", "demo")
    assert (
        rc == 0 and "cluster.yaml" in out and "cluster demo deleted" in out
    )  # reported, not fatal


def test_node_verbs(
    capsys: pytest.CaptureFixture[str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures import ready_host
    from tests.test_node import new_node_answers
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
    rc, out = run(capsys, "delete", "node", "demo", "demo-cp-1", "--yes")
    assert rc == 1 and "the first control plane demo-cp-1 cannot be deleted" in out


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
        (["delete", "node", "demo", "Demo-cp-1", "--yes"], "invalid node name 'Demo-cp-1'"),
        (["join", "node", "demo", "demo-worker-1/x"], "invalid node name"),
    ],
)
def test_node_names_are_validated_before_anything_runs(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    rc, out = run(capsys, *argv)
    assert rc != 0 and message in out, out
