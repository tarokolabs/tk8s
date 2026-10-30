import pytest

from tests.fixtures import (
    OP,
    SHOW_HAPROXY,
    bgp_router_answers,
    vrrp_answers,
    vtysh_bfd,
    vtysh_route,
    vtysh_summary,
)
from tk8s import model, verify
from tk8s.host import FakeHost, Result
from tk8s.phases import Context
from tk8s.verify import platform

NODES = (
    "demo-control-plane-1 Ready control-plane 1m v1.37.2\ndemo-worker-1 Ready <none> 1m v1.37.2\n"
    "demo-worker-2 Ready <none> 1m v1.37.2\n"
)


def healthy(ctx: Context, host: FakeHost) -> None:
    c = ctx.require_cluster()
    ctx.cluster = c.model_copy(
        update={"status": model.Status(phase="Ready", tk8s="test", node_capacity=True)}
    )
    ctx.options.verify_timeout = 15
    host.responses[("kubectl", "get", "nodes", "-o", f"jsonpath={platform.SIZES}")] = Result(
        0,
        "demo-control-plane-1 2 4Gi 1500m 3084Mi\ndemo-worker-1 2 4Gi 1500m 3084Mi\n"
        "demo-worker-2 2 4Gi 1500m 3084Mi\n",
        "",
    )
    host.files[ctx.state_dir / "kubeconfig"] = "kind: Config\n"
    host.dirs.add(ctx.state_dir / "storage" / "pvc-1234_tk-verify_pvc")
    host.active.add("demo.target")
    host.responses[("kubectl", "get", "nodes", "--no-headers")] = Result(0, NODES, "")
    host.responses[("kubectl", "get", "nodes", "-o", "custom-columns")] = Result(
        0, "demo-control-plane-1 v1.37.2 cri-o://1.37.2 6.12\n", ""
    )
    host.responses[("podman", "exec", "demo-control-plane-1", "journalctl")] = Result(0, "", "")
    host.responses[("kubectl", "get", "pods", "-n", "kube-system", "--no-headers")] = [
        Result(0, "coredns-1 0/1 ContainerCreating 0 1m\n", ""),
        Result(0, "coredns-1 1/1 Running 0 1m\ncilium-1 1/1 Running 0 1m\n", ""),
    ]
    host.responses[
        ("kubectl", "-n", "kube-system", "get", "pods", "-l", "name=cilium-operator")
    ] = Result(0, "0\n", "")
    host.responses[("kubectl", "-n", "kube-system", "logs", "deploy/cilium-operator")] = Result(
        0, 'level=info msg="LB-IPAM done initializing"\n', ""
    )
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
    host.responses[("podman", "exec", "demo-router", "cat", "/tmp/vyos-config-status")] = Result(
        0, "0\n", ""
    )
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(
        0, SHOW_HAPROXY, ""
    )
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


def test_all_healthy_is_16_pass_1_skip(ctx: Context, host: FakeHost) -> None:
    healthy(ctx, host)
    assert verify.run(ctx) == 0
    assert host.lines[-1] == "PASS 16  FAIL 0  SKIP 1"
    for s in (
        "PASS  nodes: 3/3 Ready",
        "PASS  node sizes: capacity and allocatable follow cluster.yaml (kubeReserved 500m/512Mi)",
        "PASS  kubelet: no runtime cgroup errors",
        "PASS  kube-system pods all Running",
        "PASS  cilium-operator: 0 restarts, Gateway API on",
        "PASS  cilium status",
        "PASS  kubectl top nodes",
        "PASS  PVC bound and pod wrote data",
        f"PASS  PV data on host ({ctx.state_dir}/storage)",
        "PASS  RuntimeClass crun (kernel 6.12.0)",
        "SKIP  RuntimeClass gvisor — not enabled (create the cluster with --gvisor)",
        "PASS  apiserver router 172.22.0.253 healthy (1/1 control planes UP)",
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
    assert host.lines[-1] == "PASS 15  FAIL 1  SKIP 1"


def test_kubelet_noise_is_reported_with_the_cgroup(ctx: Context, host: FakeHost) -> None:
    healthy(ctx, host)
    host.responses[("podman", "exec", "demo-control-plane-1", "journalctl")] = Result(
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


def test_router_rising_counts_as_up_and_down_fails(ctx: Context, host: FakeHost) -> None:
    healthy(ctx, host)
    rising = SHOW_HAPROXY.replace("  UP         0 ms", "  UP 1/2     0 ms")
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(0, rising, "")
    verify.run(ctx)
    assert "PASS  apiserver router 172.22.0.253 healthy (1/1 control planes UP)" in host.lines
    host.lines.clear()
    down = SHOW_HAPROXY.replace("  UP         0 ms", "  DOWN       0 ms")
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(0, down, "")
    host.responses[("curl", "-sk", "--max-time", "5")] = Result(0, "", "")
    verify.run(ctx)
    assert (
        "FAIL  apiserver router 172.22.0.253 — readyz='', 0 of 1 control planes UP "
        f"(podman exec demo-router {OP} show haproxy)"
    ) in host.lines


def test_router_check_fails_on_a_backend_cluster_yaml_does_not_have(
    ctx: Context, host: FakeHost
) -> None:
    """A deleted control plane the router still lists is DOWN, so counting UP alone passes it."""
    healthy(ctx, host)
    stale = SHOW_HAPROXY + "api                   demo-control-plane-9  DOWN       0 ms      4s\n"
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(0, stale, "")
    verify.run(ctx)
    assert (
        "FAIL  apiserver router 172.22.0.253 — readyz='ok', 1 of 1 control planes UP, "
        "backends demo-control-plane-1, demo-control-plane-9 "
        f"(podman exec demo-router {OP} show haproxy)"
    ) in host.lines


def test_only_runs_one_check(ctx: Context, host: FakeHost) -> None:
    healthy(ctx, host)
    assert verify.run(ctx, only="router") == 0
    assert lines(host, "PASS  ") == [
        "PASS  apiserver router 172.22.0.253 healthy (1/1 control planes UP)"
    ]


def test_namespace_is_deleted_even_when_a_check_raises(ctx: Context, host: FakeHost) -> None:
    healthy(ctx, host)
    host.responses[("kubectl", "create", "ns")] = Result(1, "", "forbidden")
    assert verify.run(ctx) == 1
    assert "FAIL  create namespace tk-verify" in host.lines
    assert host.calls[-1].argv[:4] == ["kubectl", "delete", "ns", "tk-verify"]


def test_cleanup_removes_the_gateway_objects_before_the_namespace(
    ctx: Context, host: FakeHost
) -> None:
    """Deleting the namespace with a live Gateway in it makes cilium-operator reconcile against a
    terminating namespace until it loses its lease; the routes and the Gateway go first."""
    healthy(ctx, host)
    verify.run(ctx)
    argvs = [c.argv for c in host.calls]
    objects = argvs.index(
        [
            "kubectl",
            "delete",
            "-n",
            "tk-verify",
            "gateway,httproute,tcproute",
            "--all",
            "--ignore-not-found",
            "--wait=true",
            "--timeout=60s",
        ]
    )
    assert objects < len(argvs) - 1 and argvs[-1][:4] == ["kubectl", "delete", "ns", "tk-verify"]


def test_operator_that_came_back_without_gateway_api_fails_with_the_recovery(
    ctx: Context, host: FakeHost
) -> None:
    """cilium-operator restarted into a flaky apiserver takes a CRD discovery error for "CRDs not
    installed" and runs Ready with Gateway API off (cilium/cilium#48083)."""
    healthy(ctx, host)
    host.responses[
        ("kubectl", "-n", "kube-system", "get", "pods", "-l", "name=cilium-operator")
    ] = Result(0, "1\n", "")
    host.responses[("kubectl", "-n", "kube-system", "logs", "deploy/cilium-operator")] = Result(
        0,
        'level=error msg="Required GatewayAPI resources are not found, please refer to docs"\n',
        "",
    )
    assert verify.run(ctx) == 1
    line = next(ln for ln in host.lines if ln.startswith("FAIL  cilium-operator"))
    assert "1 restart" in line and "Gateway API off" in line
    assert "kubectl -n kube-system rollout restart deploy/cilium-operator" in line


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


def test_a_dependent_check_runs_its_prerequisite_first(ctx: Context, host: FakeHost) -> None:
    healthy(ctx, host)
    host.responses[("curl", "-s", "-m", "8", "-H")] = Result(0, "172.22.0.254", "")  # only xff asks
    assert verify.run(ctx, only="xff") == 0
    assert lines(host, "PASS  ") == [
        "PASS  Gateway programmed (172.22.0.200)",
        "PASS  X-Forwarded-For carries the client IP (172.22.0.254)",
    ]
    assert "[gateway-api]" in host.lines
    host.lines.clear()
    assert verify.run(ctx, only="gvisor-network") == 0
    assert lines(host, "SKIP  ") == [
        "SKIP  RuntimeClass gvisor — not enabled (create the cluster with --gvisor)"
    ]
    assert host.lines[-1] == "PASS 0  FAIL 0  SKIP 1"


def bgp_healthy(bgp_ctx: Context, host: FakeHost) -> None:
    healthy(bgp_ctx, host)  # the kubectl answers; the ctx is the bgp one
    bgp_router_answers(host)
    host.responses[("curl", "-sk", "--max-time", "5")] = Result(0, "ok", "")


def test_router_check_in_bgp_mode_passes_with_every_peer_established_and_ecmp_full(
    bgp_ctx: Context, host: FakeHost
) -> None:
    bgp_healthy(bgp_ctx, host)
    assert verify.run(bgp_ctx, only="router") == 0
    assert (
        "PASS  apiserver router 172.22.0.253 healthy (3/3 control planes Established, "
        "3 ECMP next hops to 172.23.0.1, BFD up)"
    ) in host.lines
    assert ["curl", "-sk", "--max-time", "5", "https://172.23.0.1:6443/readyz"] in [
        c.argv for c in host.calls
    ]


def test_router_check_in_bgp_mode_fails_on_a_missing_next_hop_or_bfd(
    bgp_ctx: Context, host: FakeHost
) -> None:
    bgp_healthy(bgp_ctx, host)
    bgp_router_answers(host, established=3, hops=2, bfd_up=2)
    assert verify.run(bgp_ctx, only="router") == 1
    assert (
        "FAIL  apiserver router 172.22.0.253 — readyz='ok', 3 of 3 control planes Established, "
        "2 next hops to 172.23.0.1, BFD down on demo-control-plane-3 "
        "(podman exec demo-router vtysh -c 'show bgp summary')"
    ) in host.lines


def test_router_check_in_bgp_mode_counts_established_only_among_joined_control_planes(
    bgp_ctx: Context, host: FakeHost
) -> None:
    """A --defer-join control plane runs FRR (session up, BFD up) but has no apiserver and no
    VIP: it must not be counted against the joined ones."""
    c = bgp_ctx.require_cluster()
    nodes = [
        n.model_copy(update={"join": False}) if n.name.endswith("-3") else n for n in c.spec.nodes
    ]
    bgp_ctx.cluster = c.model_copy(update={"spec": c.spec.model_copy(update={"nodes": nodes})})
    bgp_healthy(bgp_ctx, host)
    bgp_router_answers(host, established=3, hops=2, bfd_up=3)
    assert verify.run(bgp_ctx, only="router") == 0
    assert (
        "PASS  apiserver router 172.22.0.253 healthy (2/2 control planes Established, "
        "2 ECMP next hops to 172.23.0.1, BFD up)"
    ) in host.lines


# ---- two routers


def two_routers_healthy(two_router_ctx: Context, host: FakeHost) -> None:
    bgp_healthy(two_router_ctx, host)
    for r in ("demo-router-1", "demo-router-2"):
        bgp_router_answers(host, router=r)
    vrrp_answers(host, {"demo-router-1": "MASTER", "demo-router-2": "BACKUP"})


def test_router_check_with_two_routers_reports_each_router_and_the_vrrp_pair(
    two_router_ctx: Context, host: FakeHost
) -> None:
    two_routers_healthy(two_router_ctx, host)
    assert verify.run(two_router_ctx, only="router") == 0
    assert lines(host, "PASS  ") == [
        "PASS  apiserver router demo-router-1 (172.22.0.251) healthy (3/3 control planes "
        "Established, 3 ECMP next hops to 172.23.0.1, BFD up)",
        "PASS  apiserver router demo-router-2 (172.22.0.252) healthy (3/3 control planes "
        "Established, 3 ECMP next hops to 172.23.0.1, BFD up)",
        "PASS  router VRRP: demo-router-1 MASTER, demo-router-2 BACKUP",
    ]


def test_router_vrrp_check_fails_when_both_are_master(
    two_router_ctx: Context, host: FakeHost
) -> None:
    two_routers_healthy(two_router_ctx, host)
    vrrp_answers(host, {"demo-router-1": "MASTER", "demo-router-2": "MASTER"})
    assert verify.run(two_router_ctx, only="router") == 1
    assert (
        "FAIL  router VRRP — demo-router-1 MASTER, demo-router-2 MASTER; expected one MASTER "
        f"and one BACKUP (podman exec demo-router-1 {OP} show vrrp)"
    ) in host.lines


def test_router_vrrp_check_fails_when_a_router_has_no_vrrp_data(
    two_router_ctx: Context, host: FakeHost
) -> None:
    two_routers_healthy(two_router_ctx, host)
    vrrp_answers(host, {"demo-router-1": None, "demo-router-2": "MASTER"})
    assert verify.run(two_router_ctx, only="router") == 1
    assert any(
        line.startswith("FAIL  router VRRP — demo-router-1 unavailable, demo-router-2 MASTER;")
        for line in host.lines
    )


def test_router_check_with_two_routers_fails_the_one_that_is_down(
    two_router_ctx: Context, host: FakeHost
) -> None:
    two_routers_healthy(two_router_ctx, host)
    vrrp_answers(host, {"demo-router-1": "MASTER", "demo-router-2": None})
    for key in (
        vtysh_summary("demo-router-2"),
        vtysh_route("demo-router-2"),
        vtysh_bfd("demo-router-2"),
    ):
        host.responses[key] = Result(1, "", "Error: container is not running")
    assert verify.run(two_router_ctx, only="router") == 2  # its own line and the VRRP pair
    assert (
        "PASS  apiserver router demo-router-1 (172.22.0.251) healthy (3/3 control planes "
        "Established, 3 ECMP next hops to 172.23.0.1, BFD up)"
    ) in host.lines
    assert any(
        line.startswith("FAIL  apiserver router demo-router-2 (172.22.0.252) — ")
        for line in host.lines
    )


def test_router_vrrp_check_fails_on_a_fault_state(two_router_ctx: Context, host: FakeHost) -> None:
    two_routers_healthy(two_router_ctx, host)
    vrrp_answers(host, {"demo-router-1": "FAULT", "demo-router-2": "MASTER"})
    assert verify.run(two_router_ctx, only="router") == 1
    assert any(
        line.startswith("FAIL  router VRRP — demo-router-1 FAULT, demo-router-2 MASTER;")
        for line in host.lines
    )


def test_node_sizes_fail_names_the_node_whose_allocatable_lost_the_reservation(
    ctx: Context, host: FakeHost
) -> None:
    healthy(ctx, host)
    host.responses[("kubectl", "get", "nodes", "-o", f"jsonpath={platform.SIZES}")] = Result(
        0,
        "demo-control-plane-1 2 4Gi 1500m 3084Mi\ndemo-worker-1 2 4Gi 2 3596Mi\n"
        "demo-worker-2 2 4Gi 1500m 3084Mi\n",
        "",
    )
    assert verify.run(ctx, only="node-sizes") == 1
    assert lines(host, "FAIL") == [
        "FAIL  node sizes — demo-worker-1: capacity 2/4Gi allocatable 2/3596Mi, want 1500m/3084Mi"
    ]


def test_node_sizes_is_skipped_on_a_cluster_from_before(ctx: Context, host: FakeHost) -> None:
    healthy(ctx, host)
    c = ctx.require_cluster()
    ctx.cluster = c.model_copy(update={"status": model.Status(phase="Ready", tk8s="old")})
    assert verify.run(ctx, only="node-sizes") == 0
    assert lines(host, "SKIP") == [
        "SKIP  node sizes — cluster created before node capacity sizing; "
        "recreate it to get node-sized capacity"
    ]


def test_node_sizes_names_a_line_it_cannot_parse(ctx: Context, host: FakeHost) -> None:
    healthy(ctx, host)
    host.responses[("kubectl", "get", "nodes", "-o", f"jsonpath={platform.SIZES}")] = Result(
        0, "demo-control-plane-1 2 4Gi 1500m 3084Mi\ndemo-worker-1 2 4Gi\n", ""
    )
    assert verify.run(ctx, only="node-sizes") == 1
    assert lines(host, "FAIL") == [
        "FAIL  node sizes — demo-worker-1: unexpected line 'demo-worker-1 2 4Gi'; "
        "demo-worker-2: not in kubectl get nodes"
    ]


# ---- several hosts

from tests.fixtures import running_multi_answers  # noqa: E402
from tk8s.verify.hosts import check_hosts  # noqa: E402
from tk8s.verify.report import Report  # noqa: E402


def test_hosts_check_passes_when_every_host_is_reachable_with_its_target_and_shim(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    running_multi_answers(hosts, ctx)
    r = Report(ctx)
    check_hosts(r)
    assert hosts["a"].lines[-1] == "PASS  hosts: 3/3 reachable, target active, shim tk8s-demo up"
    assert ["ip", "-o", "link", "show", "tk8s-demo"] in [c.argv for c in hosts["a"].calls]
    assert ["ip", "-o", "link", "show", "tk8s-demo"] not in [c.argv for c in hosts["c"].calls]
    assert (r.passed, r.failed) == (1, 0)


def test_hosts_check_names_each_hosts_trouble(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    running_multi_answers(hosts, ctx)
    hosts["b"].responses[("true",)] = Result(255, "", "Connection refused\n")
    hosts["c"].active.discard("demo.target")
    hosts["a"].responses[("ip", "-o", "link", "show", "tk8s-demo")] = Result(1, "", "")
    r = Report(ctx)
    check_hosts(r)
    assert hosts["a"].lines[-1] == (
        "FAIL  hosts — a: shim tk8s-demo missing; b: unreachable (Connection refused); "
        "c: target inactive"
    )


def test_single_host_verify_has_no_hosts_line(ctx: Context, host: FakeHost) -> None:
    healthy(ctx, host)
    verify.run(ctx)
    assert not any("hosts" in ln for ln in host.lines if ln.startswith(("PASS", "FAIL", "SKIP")))
    assert "[hosts]" not in host.lines


def test_verify_refuses_a_stopped_cluster_but_runs_on_a_degraded_one(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    c = ctx.require_cluster()
    assert c.status is not None
    ctx.cluster = c.model_copy(update={"status": c.status.model_copy(update={"phase": "Ready"})})
    running_multi_answers(hosts, ctx)
    for h in hosts.values():
        h.active.discard("demo.target")
    with pytest.raises(verify.VerifyError, match="cluster demo is not running"):
        verify.run(ctx)
    hosts["a"].active.add("demo.target")
    hosts["b"].active.add("demo.target")  # c is down: Degraded, and the checks say so
    hosts["a"].responses[("kubectl", "create", "ns")] = Result(0, "", "")
    assert verify.run(ctx, only="hosts") == 1
    assert "FAIL  hosts — c: target inactive" in hosts["a"].lines


def test_storage_check_finds_the_pv_directory_on_any_node_host(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    running_multi_answers(hosts, ctx)
    ctx.options.verify_timeout = 15
    hosts["a"].responses[("kubectl", "logs", "-n", "tk-verify", "writer")] = Result(
        0, "verify-ok\n", ""
    )
    hosts["a"].responses[("kubectl", "get", "pvc", "-n", "tk-verify", "pvc", "-o")] = Result(
        0, "pvc-1234", ""
    )
    hosts["c"].dirs.add(ctx.state_dir / "storage" / "pvc-1234_tk-verify_pvc")
    r = Report(ctx)
    platform.check_storage(r)
    assert f"PASS  PV data on host ({ctx.state_dir}/storage)" in hosts["a"].lines


def test_xff_expects_the_operators_source_address_on_several_hosts(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    r = Report(ctx)
    r.gateway_ip = "192.168.1.85"
    hosts["a"].responses[("ip", "-4", "route", "get", "192.168.1.85")] = Result(
        0, "192.168.1.85 dev tk8s-demo src 192.168.1.67 uid 1000\n", ""
    )
    hosts["a"].responses[("curl", "-s", "-m", "8", "-H")] = Result(0, "192.168.1.67", "")
    from tk8s.verify.gateway import check_xff

    check_xff(r)
    assert "PASS  X-Forwarded-For carries the client IP (192.168.1.67)" in hosts["a"].lines


def test_verify_runs_on_a_degraded_cluster_and_names_the_unreachable_host(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    from tk8s.host import HostError

    ctx, hosts = multi_ctx
    c = ctx.require_cluster()
    assert c.status is not None
    ctx.cluster = c.model_copy(update={"status": c.status.model_copy(update={"phase": "Ready"})})
    running_multi_answers(hosts, ctx)
    hosts["c"].responses[("true",)] = HostError(["ssh"], 255, "No route to host")
    hosts["c"].responses[("systemctl", "is-active")] = HostError(["ssh"], 255, "No route to host")
    hosts["a"].responses[("kubectl", "create", "ns")] = Result(0, "", "")
    assert verify.run(ctx, only="hosts") == 1
    assert "FAIL  hosts — c: unreachable (No route to host)" in hosts["a"].lines


def test_router_check_names_a_router_whose_host_is_unreachable(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    from tk8s.host import HostError

    ctx, hosts = multi_ctx
    running_multi_answers(hosts, ctx)
    ctx.options.verify_timeout = 15
    hosts["a"].responses[("curl", "-sk", "--max-time", "5")] = Result(0, "ok", "")
    down = HostError(["ssh"], 255, "No route to host")
    hosts["b"].responses = {("podman",): down, **hosts["b"].responses}  # before the specifics
    r = Report(ctx)
    from tk8s.verify.ha import check_router

    check_router(r)
    lines = [ln for ln in hosts["a"].lines if ln.startswith(("PASS", "FAIL"))]
    assert lines[0].startswith("PASS  apiserver router mh-router-1") or lines[0].startswith(
        "PASS  apiserver router demo-router-1"
    )
    assert "FAIL  apiserver router demo-router-2 (192.168.1.66) — host b unreachable" in lines[1]
    assert any(ln.startswith("FAIL  router VRRP") for ln in lines)


def test_hosts_check_wants_the_shim_to_carry_its_address(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    running_multi_answers(hosts, ctx)
    hosts["a"].responses[("ip", "-o", "-4", "addr", "show", "dev", "tk8s-demo")] = Result(0, "", "")
    r = Report(ctx)
    check_hosts(r)
    assert "FAIL  hosts — a: shim tk8s-demo has no address (want 192.168.1.67)" in hosts["a"].lines
    hosts["a"].responses[("ip", "-o", "-4", "addr", "show", "dev", "tk8s-demo")] = Result(
        0, "5: tk8s-demo    inet 192.168.1.67/32 scope global tk8s-demo\n", ""
    )
    hosts["a"].lines.clear()
    check_hosts(r)
    assert hosts["a"].lines[-1] == "PASS  hosts: 3/3 reachable, target active, shim tk8s-demo up"
