"""What every node verb shares: the cluster.yaml edits (plain functions, no host needed), the
node a phase is working on, and the two host-side helpers add and delete both need, reloading the
LB and asking the apiserver whether a node is Ready."""

from ipaddress import IPv4Address
from pathlib import Path

from .. import model
from ..create import nodes
from ..create.kubeadm import kubectl_env
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
    """One past the highest node address. .100 is the LB and .200-.219 the LB pool, so .199 is
    the last node."""
    net = c.spec.network.nodes
    last = max(int(n.ip) - int(net.network_address) for n in c.spec.nodes)
    host = last + 1
    if host > 199:
        raise ClusterError(f"no free node address left in {net} (.200-.219 is the LB pool)")
    return net.network_address + host


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


OLD_LB_MOUNT = "/usr/local/etc/haproxy/haproxy.cfg"


def reload_lb(ctx: Context) -> None:
    """The LB's backend list came from cluster.yaml; SIGUSR2 makes HAProxy reload it without
    dropping connections. An LB started by v2026.10.x bind-mounted the single haproxy.cfg file
    and still reads the inode the config was replaced under: restart it once, the unit on disk
    already mounts the directory."""
    c = ctx.require_cluster()
    mounts = ctx.host.run(
        [
            "podman",
            "inspect",
            f"{c.name}-lb",
            "--format",
            "{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}",
        ],
        sudo=True,
        capture=True,
        check=False,
    ).stdout
    if OLD_LB_MOUNT in mounts:
        ctx.host.run(["systemctl", "restart", f"{c.name}-lb.service"], sudo=True)
        nodes.wait_lb(ctx)
        return
    ctx.host.run(["podman", "kill", "--signal", "USR2", f"{c.name}-lb"], sudo=True, capture=True)


def node_ready(ctx: Context, name: str) -> bool:
    ready = 'jsonpath={.status.conditions[?(@.type=="Ready")].status}'
    r = ctx.host.run(
        ["kubectl", "get", "node", name, "-o", ready],
        env=kubectl_env(ctx),
        capture=True,
        check=False,
    )
    return r.returncode == 0 and r.stdout.strip() == "True"
