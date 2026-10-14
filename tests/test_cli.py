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
    capfd: pytest.CaptureFixture[str], data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plug = data_dir / "plugins"
    plug.mkdir()
    for name, body in (
        ("tkctl-hello", 'echo "hello: $*"'),
        ("tkctl-hello-world", 'echo "hello-world: $*"'),
        ("tkctl-create", "echo never"),
    ):
        f = plug / name
        f.write_text(f"#!/bin/sh\n{body}\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{plug}{os.pathsep}{os.environ['PATH']}")
    rc = main(["hello", "create", "class", "k8s-101", "--students", "3"])
    out = capfd.readouterr().out
    assert rc == 0 and out.strip().endswith("hello: create class k8s-101 --students 3")
    main(["hello", "world", "a", "--x"])
    assert capfd.readouterr().out.strip().endswith("hello-world: a --x")
    main(["create", "cluster", "--dry-run"])
    assert "name: tk8s" in capfd.readouterr().out  # a plugin cannot shadow a built-in verb
    main(["--help"])
    assert "Plugins found on PATH: create hello hello-world" in capfd.readouterr().out
    assert main(["frobnicate", "cluster", "demo"]) != 0
