import pytest

from tk8s import model
from tk8s.create import cni
from tk8s.host import FakeHost, Result
from tk8s.phases import Context, PhaseFailed, Runner


def healthy(host: FakeHost, ctx: Context) -> None:
    host.files[ctx.state_dir / "kubeconfig"] = "kind: Config\n"
    host.files[ctx.state_dir / "cluster.yaml"] = model.dumps(ctx.require_cluster())
    host.responses[("uname", "-r")] = Result(0, "6.12.0-1-amd64\n", "")
    host.responses[("kubectl", "-n", "kube-system", "get", "ds", "cilium")] = Result(1, "", "")
    host.responses[("kubectl", "get", "ciliumloadbalancerippool")] = Result(1, "", "")
    host.responses[("kubectl", "-n", "kube-system", "get", "secret")] = Result(0, "", "")
    host.responses[("kubectl", "get", "nodes", "--no-headers")] = Result(
        0,
        "demo-control-plane-1 Ready control-plane 1m v1.37.1\n"
        "demo-worker-1 Ready <none> 1m v1.37.1\n",
        "",
    )


def installs(host: FakeHost) -> list[list[str]]:
    return [
        c.argv
        for c in host.calls
        if c.argv[:1] == ["cilium"] and c.argv[1] in ("install", "upgrade")
    ]


def test_auto_datapath_is_netkit_on_a_recent_kernel(ctx: Context, host: FakeHost) -> None:
    host.responses[("uname", "-r")] = Result(0, "6.12.0-1-amd64\n", "")
    assert cni.resolve_datapath(ctx) == "netkit"
    host.responses[("uname", "-r")] = Result(0, "6.1.0-25-amd64\n", "")
    assert cni.resolve_datapath(ctx) == "veth"
    c = ctx.require_cluster()
    ctx.cluster = c.model_copy(update={"spec": c.spec.model_copy(update={"datapath": "veth"})})
    host.responses[("uname", "-r")] = Result(0, "6.12.0\n", "")
    assert cni.resolve_datapath(ctx) == "veth"


def test_cilium_install_argv_carries_every_setting(ctx: Context) -> None:
    argv = cni.cilium_install_argv(ctx, "install", "netkit")
    v = ctx.versions.cilium
    assert argv[:3] == ["cilium", "install", "--version"] and argv[3] == v
    chart = ctx.paths.home / "manifests" / f"cilium-{v}"
    assert argv[argv.index("--chart-directory") + 1] == str(chart)
    joined = " ".join(argv)
    for s in (
        "--set bpf.datapathMode=netkit",
        "--set cgroup.autoMount.enabled=false --set cgroup.hostRoot=/sys/fs/cgroup",
        "--set securityContext.privileged=true",
        "--set ipam.mode=kubernetes --set k8s.requireIPv4PodCIDR=true "
        "--set ipam.operator.clusterPoolIPv4MaskSize=24",
        "--set kubeProxyReplacement=true --set k8sServiceHost=api.demo.k8s "
        "--set k8sServicePort=6443",
        "--set l2announcements.enabled=true",
        "--set routingMode=native --set autoDirectNodeRoutes=true "
        "--set ipv4NativeRoutingCIDR=10.244.0.0/21",
        "--set bpf.masquerade=true",
        "--set bpf.lbExternalClusterIP=true",
        "--set gatewayAPI.enabled=true --set gatewayAPI.externalTrafficPolicy=Local",
    ):
        assert s in joined, s


def test_lb_pool_manifest_uses_the_cluster_range(ctx: Context) -> None:
    text = cni.lb_pool_manifest(ctx)
    assert "start: 172.22.0.200\n    stop: 172.22.0.219" in text and "${LBNET}" not in text


def test_healthy_cilium_path(ctx: Context, host: FakeHost) -> None:
    healthy(host, ctx)
    cni.install_cilium(ctx)
    a = [c.argv for c in host.calls]
    assert ["kubectl", "-n", "kube-system", "delete", "ds", "kube-proxy", "--ignore-not-found"] in a
    gw = ctx.versions.gateway_api.lstrip("v")
    crds = str(ctx.paths.home / "manifests" / f"gateway-api.{gw}.yaml")
    assert ["kubectl", "apply", "--server-side", "-f", crds] in a
    assert installs(host)[0][1] == "install"
    assert ["cilium", "status", "--wait", "--wait-duration", "5m"] in a
    pool = next(c for c in host.calls if c.argv == ["kubectl", "apply", "-f", "-"])
    assert pool.input is not None and "start: 172.22.0.200" in pool.input
    assert [
        "kubectl",
        "wait",
        "gatewayclass",
        "cilium",
        "--for=condition=Accepted",
        "--timeout=120s",
    ] in a
    assert f"  cilium {ctx.versions.cilium} ok (install)" in host.lines
    assert "  LB-IPAM pool 172.22.0.200-172.22.0.219" in host.lines
    saved = model.load_text(host.files[ctx.state_dir / "cluster.yaml"])
    assert saved.spec.datapath_resolved == "netkit"
    kube = [
        c
        for c in host.calls
        if c.argv[0] == "kubectl"
        or c.argv[:2] in (["cilium", "install"], ["cilium", "upgrade"], ["cilium", "status"])
    ]
    assert all(c.env.get("KUBECONFIG") == str(ctx.state_dir / "kubeconfig") for c in kube)
    assert not any("raw.githubusercontent.com" in " ".join(c.argv) for c in host.calls)


def test_cilium_never_ready_fails_by_name(ctx: Context, host: FakeHost) -> None:
    healthy(host, ctx)
    host.responses[("cilium", "status")] = Result(1, "", "waiting for cilium")
    with pytest.raises(PhaseFailed, match="cilium did not become ready within 5 minutes"):
        Runner([cni.PHASE], only="cni/install").run(ctx)


def test_gatewayclass_not_accepted_fails_by_name(ctx: Context, host: FakeHost) -> None:
    healthy(host, ctx)
    host.responses[("kubectl", "wait", "gatewayclass")] = Result(1, "", "timed out")
    with pytest.raises(PhaseFailed, match="GatewayClass cilium was not accepted within 120 s"):
        Runner([cni.PHASE], only="cni/install").run(ctx)


def test_nodes_never_ready_fails_by_name(ctx: Context, host: FakeHost) -> None:
    healthy(host, ctx)
    host.responses[("kubectl", "get", "nodes", "--no-headers")] = Result(
        0,
        "demo-control-plane-1 Ready control-plane 1m v1.37.1\n"
        "demo-worker-1 NotReady <none> 1m v1.37.1\n",
        "",
    )
    with pytest.raises(PhaseFailed, match="nodes are not Ready after 5 minutes"):
        Runner([cni.PHASE], only="cni/nodes-ready").run(ctx)


def test_rerun_after_timeout_upgrades_and_applies_the_pool(ctx: Context, host: FakeHost) -> None:
    healthy(host, ctx)
    host.responses[("kubectl", "-n", "kube-system", "get", "ds", "cilium")] = Result(
        0, "cilium", ""
    )
    host.responses[("kubectl", "-n", "kube-system", "get", "secret")] = Result(
        0, "sh.helm.release.v1.cilium.v1 helm.sh/release.v1 1 1m\n", ""
    )
    assert not cni.install_done(ctx)  # DaemonSet present, pool missing: not done
    cni.install_cilium(ctx)
    assert installs(host)[0][1] == "upgrade"
    assert f"  cilium {ctx.versions.cilium} ok (upgrade)" in host.lines
    host.responses[("kubectl", "get", "ciliumloadbalancerippool")] = Result(0, "default-pool", "")
    assert cni.install_done(ctx)


def test_datapath_resolved_is_written_once(ctx: Context, host: FakeHost) -> None:
    healthy(host, ctx)
    cni.install_cilium(ctx)
    cni.install_cilium(ctx)
    assert host.files[ctx.state_dir / "cluster.yaml"].count("datapath_resolved") == 1


def test_nodes_ready_is_not_done_when_kubectl_fails(ctx: Context, host: FakeHost) -> None:
    healthy(host, ctx)
    host.responses[("kubectl", "get", "nodes", "--no-headers")] = Result(1, "", "refused")
    assert not cni.nodes_ready_done(ctx)
    with pytest.raises(PhaseFailed, match="nodes are not Ready after 5 minutes"):
        Runner([cni.PHASE], only="cni/nodes-ready").run(ctx)


def test_auto_datapath_takes_the_lowest_kernel(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    for h, rel in zip(hosts.values(), ("6.12.0", "6.1.0-30-amd64", "6.8.0"), strict=True):
        h.responses[("uname", "-r")] = Result(0, f"{rel}\n", "")
    assert cni.resolve_datapath(ctx) == "veth"
    hosts["b"].responses[("uname", "-r")] = Result(0, "6.8.0\n", "")
    assert cni.resolve_datapath(ctx) == "netkit"
