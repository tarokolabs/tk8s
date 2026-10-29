"""What every node verb shares: the cluster.yaml edits (plain functions, no host needed), the
node a phase is working on, and the two host-side helpers add and delete both need, reloading the
LB and asking the apiserver whether a node is Ready."""

from ipaddress import IPv4Address
from pathlib import Path

from .. import model
from ..create.kubeadm import kubectl
from ..create.plan import LB_POOL, parse_range
from ..lifecycle import ClusterError
from ..phases import Context, PhaseFailed


def find(c: model.Cluster, name: str) -> model.Node | None:
    return next((n for n in c.spec.nodes if n.name == name), None)


def next_name(c: model.Cluster, role: model.Role) -> str:
    """Past the highest existing suffix, never by count: a deleted node's name is not reused while
    a higher-numbered node exists."""
    base = f"{c.name}-{role}-"  # <cluster>-control-plane-N / <cluster>-worker-N
    suffixes = [n.name[len(base) :] for n in c.spec.nodes if n.name.startswith(base)]
    used = [int(s) for s in suffixes if s.isdigit()]
    return f"{base}{max(used, default=0) + 1}"


def next_ip(c: model.Cluster) -> IPv4Address:
    """One past the highest node address. .200-.219 is the LB-IPAM pool and .253 the router,
    so .199 is the last node. On several hosts: the lowest free address of the range's node
    region (after the router and shim addresses, before the LoadBalancer pool)."""
    net = c.spec.network.nodes
    if c.spec.network.range is not None:
        return _next_ip_in_range(c)
    last = max(int(n.ip) - int(net.network_address) for n in c.spec.nodes)
    host = last + 1
    if host > 199:
        raise ClusterError(f"no free node address left in {net} (.200-.219 is the LB pool)")
    return net.network_address + host


def _next_ip_in_range(c: model.Cluster) -> IPv4Address:
    rng = c.spec.network.range or ""
    start, end = parse_range(rng)
    physical = 2 if c.spec.network.routers == 2 else 0
    first = int(start) + 1 + physical + len(c.host_names)
    last = int(end) - LB_POOL
    used = {int(n.ip) for n in c.spec.nodes}
    free = next((i for i in range(first, last + 1) if i not in used), None)
    if free is None:
        raise ClusterError(
            f"no free node address left in {rng} (the last {LB_POOL} are the LoadBalancer pool)"
        )
    return IPv4Address(free)


def least_loaded(c: model.Cluster) -> str:
    """The host with the fewest nodes, the first in cluster.yaml order on a tie."""
    counts = {h: sum(1 for n in c.spec.nodes if n.host == h) for h in c.host_names}
    return min(c.host_names, key=lambda h: counts[h])


def host_for_request(c: model.Cluster, req: model.NodeRequest) -> str | None:
    if not c.multi_host:
        if req.host is not None:
            raise ClusterError("--host only applies to a multi-host cluster")
        return None
    if req.host is None:
        return least_loaded(c)
    if req.host not in c.host_names:
        raise ClusterError(
            f"host {req.host} is not in cluster {c.name} (hosts: {', '.join(c.host_names)})"
        )
    return req.host


def plan_add(c: model.Cluster, req: model.NodeRequest) -> model.Node:
    """The size defaults to the first node of the same role (or the first node at all)."""
    if req.role == "control-plane" and sum(n.role == "control-plane" for n in c.spec.nodes) >= 5:
        raise ClusterError(
            f"cluster {c.name} already has 5 control planes; at most 5 "
            "(the apiserver LB has five backend slots, the same limit create enforces)"
        )
    like = next((n for n in c.spec.nodes if n.role == req.role), c.first_control_plane)
    return model.Node(
        role=req.role,
        name=next_name(c, req.role),
        ip=next_ip(c),
        cpu=req.cpu or like.cpu,
        memory=req.memory or like.memory,
        # cluster.yaml says what is true: the record phase writes join: true after kubeadm join
        # and Ready, so an add interrupted in between describes the node as not joined.
        join=False,
        host=host_for_request(c, req),
    )


def with_node(c: model.Cluster, n: model.Node) -> model.Cluster:
    spec = c.spec.model_copy(update={"nodes": [*c.spec.nodes, n]})
    return c.model_copy(update={"spec": spec})


def deletable(c: model.Cluster, name: str) -> model.Node:
    if name == c.first_control_plane.name:
        raise ClusterError(
            f"the first control plane {name} cannot be deleted; delete the cluster instead"
        )
    n = find(c, name)
    if n is None:
        raise ClusterError(f"node {name} not found in cluster {c.name}")
    return n


def without_node(c: model.Cluster, name: str) -> model.Cluster:
    deletable(c, name)
    kept = [n for n in c.spec.nodes if n.name != name]
    return c.model_copy(update={"spec": c.spec.model_copy(update={"nodes": kept})})


def marked_joined(c: model.Cluster, name: str) -> model.Cluster:
    changed = [n.model_copy(update={"join": True}) if n.name == name else n for n in c.spec.nodes]
    return c.model_copy(update={"spec": c.spec.model_copy(update={"nodes": changed})})


# ---- the node a phase works on


def require_node(ctx: Context) -> str:
    if ctx.node is None:
        raise PhaseFailed("no node in this context; plan runs first")
    return ctx.node


def node_of(ctx: Context) -> model.Node:
    n = find(ctx.require_cluster(), require_node(ctx))
    if n is None:
        raise PhaseFailed(f"node {ctx.node} is not in cluster.yaml")
    return n


def unit_file(ctx: Context) -> Path:
    return ctx.paths.unit_dir / ctx.cluster_name / f"{require_node(ctx)}.container"


# ---- host-side helpers


def node_ready(ctx: Context, name: str) -> bool:
    ready = 'jsonpath={.status.conditions[?(@.type=="Ready")].status}'
    r = kubectl(ctx, "get", "node", name, "-o", ready)
    return r.returncode == 0 and r.stdout.strip() == "True"
