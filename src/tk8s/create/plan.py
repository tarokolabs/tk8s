"""plan: turn what the user asked for into the cluster.yaml every other phase reads.

Fills defaults, allocates the subnet index, expands node groups into named, numbered nodes.
"""

import re
from ipaddress import IPv4Address, IPv4Network
from typing import Literal

from .. import model
from ..paths import Paths
from ..phases import Context, Phase


class PlanError(Exception):
    pass


def validate(u: model.UserCluster) -> None:
    """The rules a -f file and the flags share; messages name the field (same words as before)."""
    s = u.spec
    if s.gvisor and s.datapath == "netkit":
        raise PlanError(
            "gvisor requires the veth datapath; gVisor sandboxes have no network under netkit"
        )
    if not s.nodes:
        raise PlanError("spec.nodes must list at least one control-plane node")
    names: set[str] = set()
    for g in s.nodes:
        if not re.fullmatch(r"[0-9]+[GgMm]", g.memory):
            raise PlanError(
                f'node memory "{g.memory}" must be a number followed by G or M, e.g. 4G or '
                "4096M (podman does not accept 4Gi)"
            )
        if g.name is not None:
            if g.count != 1:
                raise PlanError(
                    f'node name "{g.name}" cannot be combined with count {g.count}; '
                    "a named entry is one node"
                )
            if g.name in names:
                raise PlanError(f'node name "{g.name}" is used twice')
            names.add(g.name)
    if s.nodes[0].role != "control-plane":
        raise PlanError(
            "the first entry in spec.nodes must be a control-plane (it runs kubeadm init)"
        )
    cps = sum(g.count for g in s.nodes if g.role == "control-plane")
    if cps % 2 == 0:
        raise PlanError(f"control-plane count {cps} must be odd (1, 3, 5) for etcd quorum")
    if cps > 5:
        raise PlanError(
            f"control-plane count {cps} exceeds the limit: at most 5 "
            "(the apiserver LB has five backend slots)"
        )


def pick_index(u: model.UserCluster, existing: model.Cluster | None, used: set[int]) -> int:
    """The index already recorded for this cluster (resume), else the one an explicit subnet
    implies, else the smallest one no other cluster uses."""
    if existing is not None:
        return existing.spec.network.index
    want = u.spec.network.nodes if u.spec.network else None
    if want is not None:
        m = re.fullmatch(r"172\.22\.([0-9]+)\.0/24", str(want))
        if m is None:
            raise PlanError(
                "spec.network.nodes must be 172.22.N.0/24 (N = 0..255) "
                "or omitted for automatic allocation"
            )
        return int(m.group(1))
    n = 0
    while n in used:
        n += 1
    return n


def network(
    index: int,
    want_router: IPv4Address | None,
    ha: Literal["proxy", "bgp"] | None,
    want_vip: IPv4Address | None = None,
    routers: Literal[1, 2] | None = None,
) -> model.Network:
    """Subnet by index: nodes 172.22.N.0/24 (the router at .253, LB-IPAM .200-.219), pods a /21,
    services a /24; the gateway .254 is the host bridge, the router's upstream. bgp mode adds the
    anycast VIP 172.23.N.1, deliberately outside the node subnet (same subnet would mean ARP,
    not a route through the router)."""
    mode: Literal["proxy", "bgp"] = "proxy" if ha is None else ha
    if want_router is not None and not re.fullmatch(
        rf"172\.22\.{index}\.(2[2-4][0-9]|25[0-3])", str(want_router)
    ):
        raise PlanError(
            f"spec.network.router {want_router} must be 172.22.{index}.220-253 "
            "(the default is .253)"
        )
    if routers == 2 and want_router is not None and int(want_router) & 0xFF in (251, 252):
        raise PlanError(
            f"spec.network.router {want_router} is a router itself with routers: 2 "
            f"(.251 and .252); pick another 172.22.{index}.220-253"
        )
    vip_net = IPv4Network(f"172.23.{index}.0/24")
    if want_vip is not None and mode != "bgp":
        raise PlanError(f"spec.network.vip {want_vip} is only used with ha: bgp")
    if want_vip is not None and (
        want_vip not in vip_net or want_vip in (vip_net.network_address, vip_net.broadcast_address)
    ):
        raise PlanError(
            f"spec.network.vip {want_vip} must be a host address in {vip_net} "
            f"(the default is 172.23.{index}.1)"
        )
    vip = (want_vip or IPv4Address(f"172.23.{index}.1")) if mode == "bgp" else None
    return model.Network(
        index=index,
        nodes=IPv4Network(f"172.22.{index}.0/24"),
        gateway=IPv4Address(f"172.22.{index}.254"),
        router=want_router or IPv4Address(f"172.22.{index}.253"),
        ha=mode,
        routers=routers or 1,
        vip=vip,
        lb_range=f"172.22.{index}.200-172.22.{index}.219",
        pods=IPv4Network(f"10.244.{index * 8}.0/21"),
        services=IPv4Network(f"10.98.{index}.0/24"),
    )


def expand_nodes(name: str, groups: list[model.NodeGroup], index: int) -> list[model.Node]:
    """Each group becomes count nodes named <cluster>-control-plane-N / <cluster>-worker-N, with
    addresses .1 upwards (the router at .253 and the LB-IPAM pool are outside that range)."""
    nodes: list[model.Node] = []
    ip, cp, wk = 0, 0, 0
    for g in groups:
        for _ in range(g.count):
            ip += 1
            if g.role == "control-plane":
                cp += 1
                auto = f"{name}-control-plane-{cp}"
            else:
                wk += 1
                auto = f"{name}-worker-{wk}"
            nodes.append(
                model.Node(
                    role=g.role,
                    name=g.name or auto,
                    ip=IPv4Address(f"172.22.{index}.{ip}"),
                    cpu=g.cpu,
                    memory=g.memory.lower(),
                    join=g.join,
                )
            )
    return nodes


def resolve(
    u: model.UserCluster,
    *,
    k8s_default: str,
    existing: model.Cluster | None,
    used_indices: set[int],
    allow_overlap: bool,
    tk8s_version: str,
) -> model.Cluster:
    validate(u)
    index = pick_index(u, existing, used_indices)
    if existing is None and index in used_indices and not allow_overlap:
        raise PlanError(
            f"subnet 172.22.{index}.0/24 is already used by another cluster; "
            "pass --allow-overlap to force"
        )
    s = u.spec
    spec = model.Spec(
        kubernetes=s.kubernetes or k8s_default,
        cni=s.cni,
        datapath=s.datapath or ("veth" if s.gvisor else "auto"),
        datapath_resolved=existing.spec.datapath_resolved if existing else None,
        gvisor=s.gvisor,
        network=network(
            index,
            s.network.router if s.network else None,
            s.network.ha if s.network else None,
            s.network.vip if s.network else None,
            routers=s.network.routers if s.network else None,
        ),
        nodes=expand_nodes(u.metadata.name, s.nodes, index),
    )
    status = existing.status if existing else model.Status(phase="Creating", tk8s=tk8s_version)
    return model.Cluster(metadata=u.metadata, spec=spec, status=status)


def load_existing(paths: Paths, name: str) -> model.Cluster | None:
    """The cluster.yaml a previous, interrupted create left behind (resume), if any."""
    p = paths.state_dir(name) / "cluster.yaml"
    return model.load(p) if p.exists() else None


def used_indices(paths: Paths) -> set[int]:
    """Subnet indices of every cluster on this host (dry-run reads the real directory too)."""
    found = (model.peek(p)[0] for p in paths.clusters_dir.glob("*/cluster.yaml"))
    return {i for i in found if i is not None}


# ---- the phase


def done(ctx: Context) -> bool:
    """The file on the host holds this very spec (a resume with other flags re-plans)."""
    if ctx.cluster is None:
        return False
    text = ctx.host.read(ctx.state_dir / "cluster.yaml")
    if text is None:
        return False
    try:
        return model.load_text(text).spec == ctx.cluster.spec
    except model.ClusterFileError:
        return False


def run(ctx: Context) -> None:
    c = ctx.require_cluster()
    if ctx.options.preset:
        ctx.say(f"  {ctx.options.preset}")
    n = len(c.control_planes)
    ctx.say(
        f"  cluster {c.name}: {n} control plane{'s' if n > 1 else ''} + "
        f"{len(c.workers)} workers, K8s {c.spec.kubernetes}, subnet {c.spec.network.nodes}"
    )
    ctx.host.write(ctx.state_dir / "cluster.yaml", model.dumps(c))


PHASE = Phase("plan", "resolve cluster.yaml: defaults, subnet, node names", done, run)
