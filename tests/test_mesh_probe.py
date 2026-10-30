"""The node-level and pod-level reachability probes connect and verify run between peers."""

from pathlib import Path

from tests.fixtures import argvs, mesh_ctx
from tk8s.host import FakeHost, Result
from tk8s.mesh import probe
from tk8s.phases import Context

DNS_POD = ("kubectl", "-n", "kube-system", "get", "pod", "-l", "k8s-app=kube-dns")


def pair(tmp_path: Path) -> tuple[Context, FakeHost, Context, FakeHost]:
    a, ha = mesh_ctx(tmp_path, "demo", 0)
    b, hb = mesh_ctx(tmp_path, "other", 1)
    ha.responses[DNS_POD] = Result(0, "10.244.0.5", "")
    hb.responses[DNS_POD] = Result(0, "10.244.8.5", "")
    return a, ha, b, hb


def test_node_ping_runs_inside_the_first_control_plane_against_the_peers(tmp_path: Path) -> None:
    a, ha, b, _ = pair(tmp_path)
    assert probe.node_ping(a, b) is None
    ping = next(c for c in ha.calls if c.argv[:2] == ["podman", "exec"])
    assert ping.argv == [
        "podman",
        "exec",
        "demo-control-plane-1",
        "ping",
        "-c",
        "1",
        "-W",
        "3",
        "172.22.1.1",
    ]
    assert ping.sudo


def test_node_ping_failure_names_both_ends(tmp_path: Path) -> None:
    a, ha, b, _ = pair(tmp_path)
    ha.responses[("podman", "exec", "demo-control-plane-1", "ping")] = Result(1, "", "")
    assert probe.node_ping(a, b) == (
        "demo -> other: node 172.22.1.1 unreachable from demo-control-plane-1"
    )


def test_pod_ping_runs_a_short_lived_busybox_pod_with_the_cluster_kubeconfig(
    tmp_path: Path,
) -> None:
    a, ha, b, _ = pair(tmp_path)
    assert probe.pod_ping(a, b) is None
    run = next(c for c in ha.calls if c.argv[:2] == ["kubectl", "run"])
    assert run.env == {"KUBECONFIG": str(a.state_dir / "kubeconfig")}
    assert run.argv[2] == "tk-mesh-probe-other"
    assert f"--image={a.versions.verify_busybox}" in run.argv
    assert run.argv[-6:] == ["ping", "-c", "1", "-W", "3", "10.244.8.5"][-6:] or run.argv[-5:] == [
        "ping",
        "-c",
        "1",
        "-W",
        "3",
        "10.244.8.5",
    ]
    assert [
        "kubectl",
        "delete",
        "pod",
        "tk-mesh-probe-other",
        "--ignore-not-found",
        "--wait=false",
    ] in argvs(ha)


def test_pod_ping_failure_names_the_pod(tmp_path: Path) -> None:
    a, ha, b, _ = pair(tmp_path)
    ha.responses[("kubectl", "run", "tk-mesh-probe-other")] = Result(1, "", "")
    assert probe.pod_ping(a, b) == "demo -> other: pod 10.244.8.5 unreachable from a pod in demo"


def test_pod_ping_without_a_peer_pod_says_so(tmp_path: Path) -> None:
    a, _, b, hb = pair(tmp_path)
    hb.responses[DNS_POD] = Result(0, "", "")
    assert probe.pod_ping(a, b) == "other: no coredns pod with an IP to probe"


def test_probe_pair_retries_pod_pings_and_collects_failures(tmp_path: Path) -> None:
    a, ha, b, hb = pair(tmp_path)
    ha.responses[("kubectl", "run", "tk-mesh-probe-other")] = [Result(1, "", ""), Result(0, "", "")]
    hb.responses[("podman", "exec", "other-control-plane-1", "ping")] = Result(1, "", "")
    out = probe.probe_pair(a, b, rounds=3, delay=0)
    assert out == ["other -> demo: node 172.22.0.1 unreachable from other-control-plane-1"]
    assert sum(1 for c in ha.calls if c.argv[:2] == ["kubectl", "run"]) == 2
