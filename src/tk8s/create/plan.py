"""plan: turn what the user asked for into the cluster.yaml every other phase reads.

Fills defaults, allocates the subnet index, expands node groups into named, numbered nodes. On
several hosts: places every node and router on a host (round-robin by role, so control planes
and routers land on different hosts first) and cuts the reserved LAN range in a fixed order.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from typing import Literal

from .. import model
from ..host import Host
from ..hosts import LanFacts
from ..paths import Paths
from ..phases import Context, Phase
from . import nodefs

LB_POOL = 5  # LoadBalancer addresses: the tail of the pool (the single-host pool is 20)


class PlanError(Exception):
    pass


def check_memory_minimum(memory: str) -> None:
    """Below 2G the kubelet's 512Mi reservation and 500Mi eviction threshold leave nothing."""
    if nodefs.memory_bytes(memory) < 2 * 2**30:
        raise PlanError(
            f'node memory "{memory}" is below the minimum 2G '
            "(kubelet reserves 512Mi and evicts below 500Mi)"
        )


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
        check_memory_minimum(g.memory)
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
    validate_hosts(u)


def validate_hosts(u: model.UserCluster) -> None:
    """What a multi-host definition may and may not say."""
    s = u.spec
    net = s.network
    rng = net.range if net else None
    if (s.hosts is None) != (rng is None):
        raise PlanError("spec.hosts needs spec.network.range (and the other way round)")
    if s.hosts is None:
        if any(g.host not in (None, "auto") for g in s.nodes) or (net and net.router_hosts):
            raise PlanError("host placement (host:, router_hosts) needs spec.hosts")
        return
    if s.gvisor:
        raise PlanError("gvisor is not supported on a multi-host cluster yet")
    assert net is not None
    if net.nodes is not None or net.router is not None:
        raise PlanError(
            "spec.network.nodes and router are allocated from spec.network.range "
            "on a multi-host cluster"
        )
    names: list[str] = []
    for h in s.hosts:
        if h.name in names:
            raise PlanError(f"host name {h.name} is used twice")
        names.append(h.name)
    if sum(1 for h in s.hosts if h.ssh is None) > 1:
        raise PlanError("at most one host without ssh (the one tkctl runs on)")
    for g in s.nodes:
        if g.host not in (None, "auto") and g.host not in names:
            raise PlanError(f"node host {g.host} is not in spec.hosts")
    for r in net.router_hosts or []:
        if r not in names:
            raise PlanError(f"router host {r} is not in spec.hosts")
    if net.router_hosts is not None and len(net.router_hosts) != (net.routers or 1):
        raise PlanError(f"router_hosts must name {net.routers or 1} hosts")
    parse_range(rng or "")


# ---- several hosts: placement and the address range


def parse_range(text: str) -> list[IPv4Address]:
    """The address pool: comma-separated A-B segments in order, B spelled in full or as the
    last octet only (192.168.1.64-70). No address twice."""
    pool: list[IPv4Address] = []
    for seg in text.split(","):
        seg = seg.strip()
        a_text, _, b_text = seg.partition("-")
        try:
            a = IPv4Address(a_text)
            if b_text and "." not in b_text:
                b_text = ".".join([*a_text.split(".")[:3], b_text])
            b = IPv4Address(b_text)
        except ValueError as err:
            raise PlanError(
                f"spec.network.range must be A-B[,C-D] (IPv4 addresses, the end may be a last "
                f"octet): {seg!r}"
            ) from err
        if b < a:
            raise PlanError(f"spec.network.range {seg}: the end is before the start")
        for i in range(int(a), int(b) + 1):
            ip = IPv4Address(i)
            if ip in pool:
                raise PlanError(f"spec.network.range: {ip} is listed twice")
            pool.append(ip)
    return pool


def ranges_text(addrs: Sequence[IPv4Address]) -> str:
    """Addresses as A-B segments (a lone address as itself), in the given order."""
    out: list[str] = []
    run: list[IPv4Address] = []
    for ip in [*addrs, None]:
        if run and (ip is None or int(ip) != int(run[-1]) + 1):
            out.append(str(run[0]) if len(run) == 1 else f"{run[0]}-{run[-1]}")
            run = []
        if ip is not None:
            run.append(ip)
    return ",".join(out)


def normalize_range(text: str) -> str:
    """The pool written out in full, the form cluster.yaml keeps."""
    return ranges_text(parse_range(text))


@dataclass(frozen=True)
class Allocation:
    router: IPv4Address  # the floating address: the nodes' gateway, proxy mode's entry
    routers: list[IPv4Address]  # router-1 and -2 (two routers), empty with one
    shim: IPv4Address | None  # the operator's, when it is one of the hosts
    nodes: list[IPv4Address]
    free: list[IPv4Address]  # left for add node
    lb_range: str  # the pool's last LB_POOL addresses


def allocate(
    pool: list[IPv4Address], *, shim: bool, routers: Literal[1, 2], nodes: int
) -> Allocation:
    """The pool cut in a fixed order: the router's floating address, the router containers'
    own (two routers), the operator's shim (when it runs nodes), the nodes, free addresses for
    add node, and the LoadBalancer pool at the tail."""
    physical = 2 if routers == 2 else 0
    shims = 1 if shim else 0
    need = 1 + physical + shims + nodes + LB_POOL
    if len(pool) < need:
        raise PlanError(
            f"spec.network.range {ranges_text(pool)} has {len(pool)} addresses; this cluster "
            f"needs {need} (1 router address, {physical} router container addresses, "
            f"{shims} operator shim, {nodes} nodes, {LB_POOL} LoadBalancer addresses)"
        )
    cursor = 1
    routers_ = pool[cursor : cursor + physical]
    cursor += physical
    shim_ip = pool[cursor] if shim else None
    cursor += shims
    node_ips = pool[cursor : cursor + nodes]
    cursor += nodes
    return Allocation(
        router=pool[0],
        routers=routers_,
        shim=shim_ip,
        nodes=node_ips,
        free=pool[cursor : len(pool) - LB_POOL],
        lb_range=ranges_text(pool[len(pool) - LB_POOL :]),
    )


def node_region(c: model.Cluster) -> list[IPv4Address]:
    """The pool addresses nodes may take (after the fixed head, before the LoadBalancer tail)."""
    net = c.spec.network
    pool = parse_range(net.range or "")
    physical = 2 if net.routers == 2 else 0
    head = 1 + physical + (1 if net.shim is not None else 0)
    return pool[head : len(pool) - LB_POOL]


def place(groups: list[model.NodeGroup], hosts: list[str]) -> list[str]:
    """A host per expanded node: control planes round-robin from the first host, workers carry
    on from where they stopped; a group with a host stays there and takes no turn."""
    out: list[str] = []
    turn = 0
    for role in ("control-plane", "worker"):
        for g in groups:
            if g.role != role:
                continue
            for _ in range(g.count):
                if g.host not in (None, "auto"):
                    out.append(str(g.host))
                else:
                    out.append(hosts[turn % len(hosts)])
                    turn += 1
    # back into cluster.yaml order (control planes were taken first above)
    order = [i for i, g in enumerate(groups) for _ in range(g.count)]
    by_role: dict[str, list[str]] = {"control-plane": [], "worker": []}
    it = iter(out)
    for role in ("control-plane", "worker"):
        for g in groups:
            if g.role == role:
                by_role[role] += [next(it) for _ in range(g.count)]
    result: list[str] = []
    taken = {"control-plane": 0, "worker": 0}
    for i in order:
        role = groups[i].role
        result.append(by_role[role][taken[role]])
        taken[role] += 1
    return result


def place_routers(want: list[str] | None, hosts: list[str], routers: int) -> list[str]:
    """router-1 on the first host, router-2 on the second (one host: both on it)."""
    if want is not None:
        return list(want)
    return [hosts[i % len(hosts)] for i in range(routers)]


def majority_host(cps: list[str]) -> str | None:
    """The host holding more than half of the control planes (etcd quorum goes with it)."""
    for h in set(cps):
        if cps.count(h) * 2 > len(cps):
            return h
    return None


def majority_warning(c: model.Cluster) -> str | None:
    cps = [n.host or "" for n in c.control_planes]
    h = majority_host(cps)
    if h is None or not c.multi_host:
        return None
    return (
        f"warning: host {h} holds {cps.count(h)} of {len(cps)} control planes; "
        "losing it stops the apiserver (etcd quorum)"
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


def expand_nodes(
    name: str,
    groups: list[model.NodeGroup],
    index: int,
    *,
    ips: list[IPv4Address] | None = None,
    hosts: list[str] | None = None,
) -> list[model.Node]:
    """Each group becomes count nodes named <cluster>-control-plane-N / <cluster>-worker-N, with
    addresses .1 upwards (the router at .253 and the LB-IPAM pool are outside that range), or,
    on several hosts, the given addresses and hosts in the same order."""
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
                    ip=ips[ip - 1] if ips is not None else IPv4Address(f"172.22.{index}.{ip}"),
                    cpu=g.cpu,
                    memory=g.memory.lower(),
                    join=g.join,
                    host=hosts[ip - 1] if hosts is not None else None,
                )
            )
    return nodes


def network_multi(
    index: int,
    lan: LanFacts,
    rng: str,
    ha: Literal["proxy", "bgp"] | None,
    want_vip: IPv4Address | None,
    routers: Literal[1, 2] | None,
    alloc: Allocation,
    router_hosts: list[str],
) -> model.Network:
    """The LAN is the node subnet and its gateway the router's upstream; the index still picks
    the pod and service subnets and the VIP."""
    by_index = network(index, None, ha, want_vip, routers=routers)
    return by_index.model_copy(
        update={
            "nodes": lan.subnet,
            "gateway": lan.gateway,
            "router": alloc.router,
            "lb_range": alloc.lb_range,
            "range": rng,
            "router_hosts": router_hosts,
            "shim": alloc.shim,
        }
    )


def resolve(
    u: model.UserCluster,
    *,
    k8s_default: str,
    existing: model.Cluster | None,
    used_indices: set[int],
    allow_overlap: bool,
    tk8s_version: str,
    lans: dict[str, LanFacts] | None = None,
    operator: str | None = None,
) -> model.Cluster:
    validate(u)
    index = pick_index(u, existing, used_indices)
    if existing is None and index in used_indices and not allow_overlap:
        raise PlanError(
            f"subnet 172.22.{index}.0/24 is already used by another cluster; "
            "pass --allow-overlap to force"
        )
    s = u.spec
    want_routers = (s.network.routers if s.network else None) or 1
    if existing is not None and want_routers != existing.spec.network.routers:
        # a resume re-renders the units but removes none: the old router's unit would stay next
        # to the new ones, two claimants of the floating address
        raise PlanError(
            f"cluster {u.metadata.name} is being created with routers: "
            f"{existing.spec.network.routers}; rerun with the same --routers (or --preset), "
            f"or delete it first: tkctl delete cluster {u.metadata.name}"
        )
    if s.hosts is None:
        net = network(
            index,
            s.network.router if s.network else None,
            s.network.ha if s.network else None,
            s.network.vip if s.network else None,
            routers=s.network.routers if s.network else None,
        )
        nodes = expand_nodes(u.metadata.name, s.nodes, index)
        hosts = None
    else:
        assert s.network is not None and s.network.range is not None
        if lans is None:
            raise PlanError("a multi-host cluster needs the hosts' LAN facts to be planned")
        names = [h.name for h in s.hosts]
        first = lans[names[0]]
        for n in names[1:]:
            if (lans[n].subnet, lans[n].gateway) != (first.subnet, first.gateway):
                raise PlanError(
                    f"host {n} is on {lans[n].subnet} via {lans[n].gateway}, host {names[0]} on "
                    f"{first.subnet} via {first.gateway}; every host must share one LAN"
                )
        pool = parse_range(s.network.range)
        rng = ranges_text(pool)
        if any(ip not in first.subnet for ip in pool):
            raise PlanError(
                f"spec.network.range {rng} is outside the hosts' network {first.subnet}"
            )
        for ip, what in (
            (first.subnet.network_address, "network"),
            (first.subnet.broadcast_address, "broadcast"),
        ):
            if ip in pool:
                raise PlanError(
                    f"spec.network.range {rng}: {ip} is the {what} address of {first.subnet}"
                )
        for n in names:
            if lans[n].address in pool:
                raise PlanError(
                    f"host {n}'s own address {lans[n].address} is inside spec.network.range {rng}"
                )
        placed = place(s.nodes, names)
        router_hosts = place_routers(s.network.router_hosts, names, want_routers)
        shim = any(h.ssh is None for h in s.hosts)
        alloc = allocate(pool, shim=shim, routers=want_routers, nodes=sum(g.count for g in s.nodes))
        net = network_multi(
            index, first, rng, s.network.ha, s.network.vip, want_routers, alloc, router_hosts
        )
        nodes = expand_nodes(u.metadata.name, s.nodes, index, ips=alloc.nodes, hosts=placed)
        hosts = [model.Host(name=h.name, ssh=h.ssh, iface=lans[h.name].iface) for h in s.hosts]
        if existing is not None:
            check_resume(u.metadata.name, existing, hosts, nodes, net)
    spec = model.Spec(
        kubernetes=s.kubernetes or k8s_default,
        cni=s.cni,
        datapath=s.datapath or ("veth" if s.gvisor else "auto"),
        datapath_resolved=existing.spec.datapath_resolved if existing else None,
        gvisor=s.gvisor,
        network=net,
        nodes=nodes,
        hosts=hosts,
    )
    status = (
        existing.status
        if existing
        else model.Status(
            phase="Creating", tk8s=tk8s_version, node_capacity=True, operator=operator
        )
    )
    return model.Cluster(metadata=u.metadata, spec=spec, status=status)


def check_resume(
    name: str,
    existing: model.Cluster,
    hosts: list[model.Host],
    nodes: list[model.Node],
    net: model.Network,
) -> None:
    """A resume must describe the same hosts and the same placement: units already written on
    hosts that drop out, or nodes that move, would be left running with their addresses."""
    was = existing.spec
    if was.hosts is None:
        raise PlanError(
            f"cluster {name} is being created on a single host; rerun without --hosts, "
            f"or delete it first: tkctl delete cluster {name}"
        )
    for h in hosts:
        old = next((x for x in was.hosts if x.name == h.name), None)
        if old is not None and old.iface != h.iface:
            raise PlanError(
                f"cluster.yaml says host {h.name} on {old.iface}, the host now says {h.iface}; "
                f"delete the cluster and create it again"
            )
    if [(h.name, h.ssh) for h in was.hosts] != [(h.name, h.ssh) for h in hosts]:
        raise PlanError(
            f"cluster {name} is being created on hosts {', '.join(h.name for h in was.hosts)}; "
            f"rerun with the same --hosts, or delete it first: tkctl delete cluster {name}"
        )
    same = [(n.name, n.ip, n.host) for n in was.nodes] == [(n.name, n.ip, n.host) for n in nodes]
    if not same or was.network.router_hosts != net.router_hosts or was.network.range != net.range:
        raise PlanError(
            f"cluster {name} is being created with another placement or range; rerun with the "
            f"same flags, or delete it first: tkctl delete cluster {name}"
        )


def load_existing(paths: Paths, name: str) -> model.Cluster | None:
    """The cluster.yaml a previous, interrupted create left behind (resume), if any."""
    p = paths.state_dir(name) / "cluster.yaml"
    return model.load(p) if p.exists() else None


def used_indices(paths: Paths) -> set[int]:
    """Subnet indices of every cluster on this host (dry-run reads the real directory too)."""
    found = (model.peek(p)[0] for p in paths.clusters_dir.glob("*/cluster.yaml"))
    return {i for i in found if i is not None}


def used_indices_on(host: Host, paths: Paths) -> set[int]:
    """The same on another host of the cluster, through its Host: its own clusters and the
    copies it holds both count (their pod, service and VIP prefixes are routed there)."""
    out: set[int] = set()
    for name in host.listdir(paths.clusters_dir):
        text = host.read(paths.clusters_dir / name / "cluster.yaml")
        index = model.peek_text(text)[0] if text is not None else None
        if index is not None:
            out.add(index)
    return out


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
    for line in placement_lines(c):
        ctx.say(f"  {line}")
    ctx.save_cluster(c)


def placement_lines(c: model.Cluster) -> list[str]:
    """What plan (and preflight's warning) say about a multi-host cluster; nothing otherwise."""
    if not c.multi_host:
        return []
    shim = c.spec.network.shim
    out = [
        f"host {h.name}: {h.iface}" + (f" (this host, shim {shim})" if h.ssh is None else "")
        for h in c.spec.hosts or []
    ]
    out.append("placement: " + ", ".join(f"{n.name} {n.host}" for n in c.spec.nodes))
    out.append("routers: " + ", ".join(f"{r.name} {r.host}" for r in c.routers))
    warning = majority_warning(c)
    if warning:
        out.append(warning)
    return out


PHASE = Phase("plan", "resolve cluster.yaml: defaults, subnet, node names", done, run)
