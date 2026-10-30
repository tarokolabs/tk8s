"""cni: cilium (kube-proxy replacement, LB-IPAM with L2 announcements, the Gateway API
controller); then wait for every node to be Ready.

The Gateway API CRDs, the cilium chart and the cilium CLI are pinned in versions.yaml; the CRDs
and the chart are vendored under manifests/, so nothing here fetches from helm.cilium.io."""

from ipaddress import IPv4Network
from typing import Any, Literal

import yaml

from .. import hosts, images, model
from ..phases import Context, Phase, PhaseFailed, wait_for
from . import units
from .kubeadm import kubectl, kubectl_env

Datapath = Literal["netkit", "veth"]


def resolve_datapath(ctx: Context) -> Datapath:
    """auto: netkit needs kernel 6.8; older kernels get veth. On several hosts the oldest
    kernel decides: one node without netkit would be one node without a network."""
    d = ctx.require_cluster().spec.datapath
    if d != "auto":
        return d
    oldest = min(hosts.kernel(h) for _, h in ctx.node_hosts())
    return "netkit" if oldest >= (6, 8) else "veth"


def cilium_install_argv(ctx: Context, action: str, datapath: Datapath) -> list[str]:
    c, v = ctx.require_cluster(), ctx.versions
    chart = ctx.paths.home / "manifests" / f"cilium-{v.cilium}"
    settings = [
        f"bpf.datapathMode={datapath}",
        "cgroup.autoMount.enabled=false",
        "cgroup.hostRoot=/sys/fs/cgroup",
        "securityContext.privileged=true",
        "ipam.mode=kubernetes",
        "k8s.requireIPv4PodCIDR=true",
        "ipam.operator.clusterPoolIPv4MaskSize=24",
        "kubeProxyReplacement=true",
        f"k8sServiceHost=api.{c.domain}",
        "k8sServicePort=6443",
        "l2announcements.enabled=true",
        "routingMode=native",
        "autoDirectNodeRoutes=true",
        # every index's pod /21: Cluster Mesh with native routing wants one CIDR over all clusters
        "ipv4NativeRoutingCIDR=10.244.0.0/16",
        # Cluster Mesh identity, fixed at install (changing it later restarts every workload)
        f"cluster.name={c.name}",
        f"cluster.id={c.spec.network.index + 1}",
        "bpf.masquerade=true",
        "bpf.lbExternalClusterIP=true",
        "gatewayAPI.enabled=true",
        "gatewayAPI.externalTrafficPolicy=Local",
    ]
    argv = ["cilium", action, "--version", v.cilium, "--chart-directory", str(chart)]
    for s in settings:
        argv += ["--set", s]
    return argv


MESH_SERVICE = {
    "io.kubernetes.service.namespace": "kube-system",
    "io.kubernetes.service.name": "clustermesh-apiserver",
}


def _pool(
    name: str, blocks: list[dict[str, str]], selector: dict[str, Any] | None
) -> dict[str, Any]:
    spec: dict[str, Any] = {"blocks": blocks}
    if selector is not None:
        spec["serviceSelector"] = selector
    return {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumLoadBalancerIPPool",
        "metadata": {"name": name},
        "spec": spec,
    }


def lb_pool_manifest(ctx: Context) -> str:
    """The LB-IPAM pools, then the L2 policy from manifests/cilium-lb.yaml: default-pool holds
    lb_range (one block per segment, a lone address its own block) for every Service but the
    clustermesh-apiserver, mesh-pool holds that one address for it alone. A cluster from before
    mesh support (no mesh address) keeps its single, unselective pool."""
    net = ctx.require_cluster().spec.network
    blocks = []
    for seg in net.lb_range.split(","):
        start, _, stop = seg.partition("-")
        blocks.append({"start": start, "stop": stop or start})
    pools: list[dict[str, Any]]
    if net.mesh is None:
        pools = [_pool("default-pool", blocks, None)]
    else:
        name = MESH_SERVICE["io.kubernetes.service.name"]
        keep_out = {
            "matchExpressions": [
                {"key": "io.kubernetes.service.name", "operator": "NotIn", "values": [name]}
            ]
        }
        only_mesh = {"matchLabels": dict(MESH_SERVICE)}
        pools = [
            _pool("default-pool", blocks, keep_out),
            _pool("mesh-pool", [{"start": str(net.mesh), "stop": str(net.mesh)}], only_mesh),
        ]
    l2 = (ctx.paths.home / "manifests" / "cilium-lb.yaml").read_text()
    return yaml.safe_dump_all(pools, sort_keys=False, explicit_start=True) + "---\n" + l2


def record_cni(ctx: Context, datapath: Datapath) -> None:
    """cni records the datapath it resolved and, for Cluster Mesh, the cluster id and the Cilium
    version it installed; a resume must not lose them when rewriting the file."""
    c = ctx.require_cluster()
    spec = c.spec.model_copy(update={"datapath_resolved": datapath})
    status = c.status
    if status is not None:
        status = status.model_copy(
            update={"cluster_id": c.spec.network.index + 1, "cilium": ctx.versions.cilium}
        )
    ctx.save_cluster(c.model_copy(update={"spec": spec, "status": status}))


def _kubectl(ctx: Context, *args: str, input: str | None = None, check: bool = True) -> int:
    r = kubectl(ctx, *args, input=input)
    if check and r.returncode != 0:
        raise PhaseFailed(f"kubectl {' '.join(args)} failed: {r.stderr.strip()}")
    return r.returncode


# ---- install


def install_done(ctx: Context) -> bool:
    """cilium counts as installed only once its LB-IPAM pool exists: a first attempt that timed out
    waiting for the DaemonSet leaves the DaemonSet behind but never reached the pool."""
    ds = _kubectl(ctx, "-n", "kube-system", "get", "ds", "cilium", check=False) == 0
    pool = _kubectl(ctx, "get", "ciliumloadbalancerippool", "default-pool", check=False) == 0
    return ds and pool


def install_cilium(ctx: Context) -> None:
    c, v = ctx.require_cluster(), ctx.versions
    images.ensure_cilium_cli(ctx)
    _kubectl(ctx, "-n", "kube-system", "delete", "ds", "kube-proxy", "--ignore-not-found")
    gw = ctx.paths.home / "manifests" / f"gateway-api.{v.gateway_api.lstrip('v')}.yaml"
    _kubectl(ctx, "apply", "--server-side", "-f", str(gw))
    ctx.say(f"  Gateway API {v.gateway_api} CRDs installed (experimental channel)")
    datapath = resolve_datapath(ctx)
    note = (
        " (gVisor sandboxes have no network under netkit; use --datapath veth if you need gVisor)"
    )
    ctx.say(f"  cilium datapath: {datapath}{note if datapath == 'netkit' else ''}")
    # A partially installed release (interrupted create) is upgraded in place instead of failing.
    secrets = kubectl(
        ctx, "-n", "kube-system", "get", "secret", "-l", "owner=helm,name=cilium", "--no-headers"
    )
    action = "upgrade" if secrets.stdout.strip() else "install"
    r = ctx.host.run(
        cilium_install_argv(ctx, action, datapath),
        env=kubectl_env(ctx),
        capture=True,
        check=False,
    )
    out = ctx.log_path("cilium.out")
    ctx.host.write(out, r.stdout + r.stderr)
    if r.returncode != 0:
        raise PhaseFailed(f"cilium {action} failed (see {out})")
    st = ctx.host.run(
        ["cilium", "status", "--wait", "--wait-duration", "5m"],
        env=kubectl_env(ctx),
        capture=True,
        check=False,
    )
    ctx.host.write(ctx.log_path("cilium-status.out"), st.stdout + st.stderr)
    if st.returncode != 0:
        tail = "\n".join((st.stdout + st.stderr).splitlines()[-20:])
        raise PhaseFailed(
            f"cilium did not become ready within 5 minutes (see {out} and cilium status)\n{tail}"
        )
    ctx.say(f"  cilium {v.cilium} ok ({action})")
    _kubectl(ctx, "apply", "-f", "-", input=lb_pool_manifest(ctx))
    ctx.say(f"  LB-IPAM pool {c.spec.network.lb_range}")
    accepted = _kubectl(
        ctx,
        "wait",
        "gatewayclass",
        "cilium",
        "--for=condition=Accepted",
        "--timeout=120s",
        check=False,
    )
    if accepted != 0:
        raise PhaseFailed(
            "GatewayClass cilium was not accepted within 120 s "
            "(kubectl describe gatewayclass cilium)"
        )
    ctx.say("  GatewayClass cilium accepted")
    record_cni(ctx, datapath)


INSTALL = Phase(
    "install",
    "install the CNI (cilium: kube-proxy replacement, LB-IPAM, Gateway API)",
    install_done,
    install_cilium,
)


# ---- nodes-ready


def not_ready(ctx: Context) -> list[str] | None:
    """Names of the nodes that are not Ready; None when kubectl itself failed."""
    r = kubectl(ctx, "get", "nodes", "--no-headers")
    if r.returncode != 0:
        return None
    return [line.split()[0] for line in r.stdout.splitlines() if line.split()[1:2] != ["Ready"]]


def nodes_ready_done(ctx: Context) -> bool:
    return not_ready(ctx) == []


def nodes_ready_run(ctx: Context) -> None:
    """Host routes to the pod and service subnets are the <cluster>-routes.service unit, started
    with the cluster target by nodes/up; with the CNI in place the nodes turn Ready."""
    pending: list[str] | None = []

    def all_ready() -> bool:
        nonlocal pending
        pending = not_ready(ctx)
        return pending == []

    wait_for(
        ctx,
        all_ready,
        rounds=60,
        delay=ctx.options.poll_slow,
        failure=lambda: (
            "nodes are not Ready after 5 minutes: "
            f"{' '.join(pending) if pending else 'kubectl get nodes failed'}"
        ),
    )
    r = kubectl(ctx, "get", "nodes", "--no-headers")
    if r.returncode != 0:
        raise PhaseFailed(f"kubectl get nodes failed: {r.stderr.strip()}")
    for line in r.stdout.splitlines():
        f = line.split()
        ctx.say(f"  {f[0]} {f[1]}")


NODES_READY = Phase(
    "nodes-ready", "wait for every node to be Ready", nodes_ready_done, nodes_ready_run
)

# ---- host-routes

POD_CIDRS = '{range .items[*]}{.metadata.name} {.spec.podCIDR}{"\\n"}{end}'


def node_pod_cidrs(ctx: Context) -> dict[str, str]:
    """node name -> the pod CIDR Kubernetes gave it ("" while it has none)."""
    r = kubectl(ctx, "get", "nodes", "-o", f"jsonpath={POD_CIDRS}")
    if r.returncode != 0:
        raise PhaseFailed(f"kubectl get nodes failed: {r.stderr.strip()}")
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        fields = line.split()
        if fields:
            out[fields[0]] = fields[1] if len(fields) > 1 else ""
    return out


def with_pod_cidrs(c: model.Cluster, cidrs: dict[str, str]) -> model.Cluster:
    """cluster.yaml's nodes with the pod CIDRs Kubernetes reports; a node Kubernetes has not
    registered keeps what it had. What is reported becomes a host route, so only a prefix inside
    the cluster's own pod subnet is accepted."""
    pods = c.spec.network.pods
    nodes: list[model.Node] = []
    for n in c.spec.nodes:
        if not cidrs.get(n.name):
            nodes.append(n)
            continue
        try:
            cidr = IPv4Network(cidrs[n.name])
        except ValueError as err:
            raise PhaseFailed(f"{n.name} reports pod CIDR {cidrs[n.name]!r}: {err}") from err
        if not cidr.subnet_of(pods) or cidr == pods:
            raise PhaseFailed(f"{n.name} reports pod CIDR {cidr} outside {pods}")
        nodes.append(n.model_copy(update={"pod_cidr": cidr}))
    return c.model_copy(update={"spec": c.spec.model_copy(update={"nodes": nodes})})


def host_routes_done(ctx: Context) -> bool:
    """Every registered node's CIDR is recorded and the routes unit on disk says so."""
    c = ctx.require_cluster()
    cidrs = node_pod_cidrs(ctx)
    for n in c.spec.nodes:
        if n.name in cidrs and str(n.pod_cidr or "") != cidrs[n.name]:
            return False
    return units.routes_current(ctx, c)


def host_routes_run(ctx: Context) -> None:
    c = ctx.require_cluster()
    cidrs = node_pod_cidrs(ctx)
    for n in c.spec.nodes:
        if n.join and n.name in cidrs and not cidrs[n.name]:
            raise PhaseFailed(f"{n.name} has no pod CIDR yet (kubectl get node {n.name} -o yaml)")
    c = with_pod_cidrs(c, cidrs)
    ctx.save_cluster(c)
    units.write_routes(ctx, c)
    first = c.first_control_plane.name
    routed = [f"{n.name} {n.pod_cidr}" for n in c.spec.nodes if n.pod_cidr and n.name != first]
    ctx.say(f"  host routes: {', '.join(routed) if routed else 'none (one node)'}")


HOST_ROUTES = Phase(
    "host-routes",
    "route each node's pod CIDR from the host straight to that node",
    host_routes_done,
    host_routes_run,
)

PHASE = Phase(
    "cni",
    "install the CNI and wait for the nodes",
    None,
    lambda ctx: None,
    children=(INSTALL, NODES_READY, HOST_ROUTES),
)
