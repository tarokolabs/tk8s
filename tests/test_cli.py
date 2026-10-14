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
    from tk8s import model, verbs, versions
    from tk8s.host import FakeHost
    from tk8s.paths import Paths
    from tk8s.phases import Context, Options

    host = FakeHost()

    def fake_context(**kw: object) -> Context:
        paths = Paths.from_env(verbs.HOME)
        return Context(
            host=host,
            paths=paths,
            versions=versions.load(verbs.HOME / "versions.yaml"),
            options=Options(poll=0, poll_slow=0, retry_delay=0),
            say=print,
        )

    monkeypatch.setattr(verbs, "make_context", fake_context)
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
    from tk8s import model, verbs, versions
    from tk8s.host import FakeHost
    from tk8s.paths import Paths
    from tk8s.phases import Context, Options

    host = FakeHost()

    def fake_context(**kw: object) -> Context:
        return Context(
            host=host,
            paths=Paths.from_env(verbs.HOME),
            versions=versions.load(verbs.HOME / "versions.yaml"),
            options=Options(poll=0, poll_slow=0, retry_delay=0),
            say=print,
        )

    monkeypatch.setattr(verbs, "make_context", fake_context)
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
