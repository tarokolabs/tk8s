import pytest

from tk8s import model, verify
from tk8s.host import FakeHost, Result
from tk8s.phases import Context

NODES = (
    "demo-cp-1 Ready control-plane 1m v1.37.1\ndemo-worker-1 Ready <none> 1m v1.37.1\n"
    "demo-worker-2 Ready <none> 1m v1.37.1\n"
)
STATS = (
    "control-planes,demo-cp-1,0,0,0,0,,0,0,0,,0,,0,0,0,0,UP,1\n"
    "control-planes,BACKEND,0,0,0,0,200,0,0,0,0,0,,0,0,0,0,UP,1\n"
)


def healthy(ctx: Context, host: FakeHost) -> None:
    c = ctx.require_cluster()
    ctx.cluster = c.model_copy(update={"status": model.Status(phase="Ready", tk8s="test")})
    ctx.options.verify_timeout = 15
    host.files[ctx.state_dir / "kubeconfig"] = "kind: Config\n"
    host.dirs.add(ctx.state_dir / "storage" / "pvc-1234_tk-verify_pvc")
    host.active.add("demo.target")
    host.responses[("kubectl", "get", "nodes", "--no-headers")] = Result(0, NODES, "")
    host.responses[("kubectl", "get", "nodes", "-o", "custom-columns")] = Result(
        0, "demo-cp-1 v1.37.1 cri-o://1.37.2 6.12\n", ""
    )
    host.responses[("podman", "exec", "demo-cp-1", "journalctl")] = Result(0, "", "")
    host.responses[("kubectl", "get", "pods", "-n", "kube-system", "--no-headers")] = [
        Result(0, "coredns-1 0/1 ContainerCreating 0 1m\n", ""),
        Result(0, "coredns-1 1/1 Running 0 1m\ncilium-1 1/1 Running 0 1m\n", ""),
    ]
    host.responses[("cilium", "version", "--client")] = Result(0, "", "")
    host.responses[("cilium", "status")] = Result(0, "", "")
    host.responses[("kubectl", "-n", "kube-system", "get", "cm", "cilium-config")] = Result(
        0, "netkit", ""
    )
    host.responses[("kubectl", "top", "nodes")] = [
        Result(1, "", ""),
        Result(1, "", ""),
        Result(0, "ok", ""),
    ]
    host.responses[("kubectl", "logs", "-n", "tk-verify", "writer")] = Result(0, "verify-ok\n", "")
    host.responses[("kubectl", "get", "pvc", "-n", "tk-verify", "pvc", "-o")] = Result(
        0, "pvc-1234", ""
    )
    host.responses[("kubectl", "get", "runtimeclass", "crun")] = Result(0, "crun", "")
    host.responses[("kubectl", "get", "runtimeclass", "gvisor")] = Result(1, "", "")
    host.responses[("kubectl", "logs", "-n", "tk-verify", "rc-crun")] = Result(0, "6.12.0\n", "")
    host.responses[("podman", "exec", "demo-lb")] = Result(0, STATS, "")
    host.responses[("curl", "-sk", "--max-time", "5")] = Result(0, "ok", "")
    host.responses[("kubectl", "get", "gateway", "-n", "tk-verify", "gw", "-o")] = Result(
        0, "172.22.0.200", ""
    )
    host.responses[("curl", "-s", "-m", "8", "-H")] = [
        Result(0, "echo-abc", ""),
        Result(0, "172.22.0.254", ""),
    ]
    host.responses[("curl", "-s", "-m", "8", "http://172.22.0.200:9000/hostname")] = Result(
        0, "echo-xyz", ""
    )


def lines(host: FakeHost, prefix: str) -> list[str]:
    return [line for line in host.lines if line.startswith(prefix)]


def test_all_healthy_is_14_pass_1_skip(ctx: Context, host: FakeHost) -> None:
    healthy(ctx, host)
    assert verify.run(ctx) == 0
    assert host.lines[-1] == "PASS 14  FAIL 0  SKIP 1"
    for s in (
        "PASS  nodes: 3/3 Ready",
        "PASS  kubelet: no runtime cgroup errors",
        "PASS  kube-system pods all Running",
        "PASS  cilium status",
        "PASS  kubectl top nodes",
        "PASS  PVC bound and pod wrote data",
        f"PASS  PV data on host ({ctx.state_dir}/storage)",
        "PASS  RuntimeClass crun (kernel 6.12.0)",
        "SKIP  RuntimeClass gvisor — not enabled (create the cluster with --gvisor)",
        "PASS  apiserver LB 172.22.0.100 healthy (1/1 control planes UP)",
        "PASS  GatewayClass cilium Accepted",
        "PASS  Gateway programmed (172.22.0.200)",
        "PASS  HTTPRoute reachable from host (backend echo-abc)",
        "PASS  X-Forwarded-For carries the client IP (172.22.0.254)",
        "PASS  TCPRoute reachable from host (backend echo-xyz)",
    ):
        assert s in host.lines, s
    pin = ctx.versions.verify_alpine
    assert any(f"--image={pin}" in " ".join(c.argv) for c in host.calls)
    assert host.calls[-1].argv == [
        "kubectl",
        "delete",
        "ns",
        "tk-verify",
        "--ignore-not-found",
        "--wait=false",
    ]
    kube = [c for c in host.calls if c.argv[0] == "kubectl"]
    assert all(c.env.get("KUBECONFIG") == str(ctx.state_dir / "kubeconfig") for c in kube)


def test_one_broken_component_is_one_fail_and_exit_1(ctx: Context, host: FakeHost) -> None:
    healthy(ctx, host)
    host.responses[("kubectl", "top", "nodes")] = Result(1, "", "")
    assert verify.run(ctx) == 1
    assert "FAIL  kubectl top nodes — metrics-server not ready" in host.lines
    assert host.lines[-1] == "PASS 13  FAIL 1  SKIP 1"


def test_kubelet_noise_is_reported_with_the_cgroup(ctx: Context, host: FakeHost) -> None:
    healthy(ctx, host)
    host.responses[("podman", "exec", "demo-cp-1", "journalctl")] = Result(
        0,
        'E0929 summary_sys_containers.go:91] "Failed to get system container stats" '
        'err="failed to get cgroup stats for /system.slice/containerd.service"\n',
        "",
    )
    verify.run(ctx)
    assert any(
        line.startswith(
            "FAIL  kubelet runtime cgroup — 1 errors in 2 min "
            "(cgroup stats for /system.slice/containerd.service"
        )
        for line in host.lines
    )


def test_lb_rising_counts_as_up_and_down_fails(ctx: Context, host: FakeHost) -> None:
    healthy(ctx, host)
    rising = STATS.replace(",UP,1\ncontrol-planes,BACKEND", ",UP 1/2,1\ncontrol-planes,BACKEND")
    host.responses[("podman", "exec", "demo-lb")] = Result(0, rising, "")
    verify.run(ctx)
    assert "PASS  apiserver LB 172.22.0.100 healthy (1/1 control planes UP)" in host.lines
    host.lines.clear()
    down = STATS.replace(",UP,1\ncontrol-planes,BACKEND", ",DOWN,1\ncontrol-planes,BACKEND")
    host.responses[("podman", "exec", "demo-lb")] = Result(0, down, "")
    host.responses[("curl", "-sk", "--max-time", "5")] = Result(0, "", "")
    verify.run(ctx)
    assert (
        "FAIL  apiserver LB 172.22.0.100 — readyz='', 0 of 1 control planes UP "
        "(podman logs demo-lb)"
    ) in host.lines


def test_only_runs_one_check(ctx: Context, host: FakeHost) -> None:
    healthy(ctx, host)
    assert verify.run(ctx, only="apiserver-lb") == 0
    assert lines(host, "PASS  ") == [
        "PASS  apiserver LB 172.22.0.100 healthy (1/1 control planes UP)"
    ]


def test_namespace_is_deleted_even_when_a_check_raises(ctx: Context, host: FakeHost) -> None:
    healthy(ctx, host)
    host.responses[("kubectl", "create", "ns")] = Result(1, "", "forbidden")
    assert verify.run(ctx) == 1
    assert "FAIL  create namespace tk-verify" in host.lines
    assert host.calls[-1].argv[:4] == ["kubectl", "delete", "ns", "tk-verify"]


def test_stopped_or_unfinished_cluster_is_refused(ctx: Context, host: FakeHost) -> None:
    healthy(ctx, host)
    host.active.discard("demo.target")
    with pytest.raises(
        verify.VerifyError, match=r"cluster demo is not running \(tkctl start cluster demo\)"
    ):
        verify.run(ctx)
    host.active.add("demo.target")
    c = ctx.require_cluster()
    assert c.status is not None
    ctx.cluster = c.model_copy(update={"status": c.status.model_copy(update={"phase": "Creating"})})
    with pytest.raises(
        verify.VerifyError,
        match="cluster demo is not fully created; rerun tkctl create cluster demo",
    ):
        verify.run(ctx)
