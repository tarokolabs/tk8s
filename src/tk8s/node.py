"""Nodes of an existing cluster: add one, join one that was created unjoined, delete one. The
cluster.yaml edits come first as plain functions (no host needed); the phases follow and reuse
what create already does for the whole cluster (the image store, the units, the per-node
addons), so a node added later looks exactly like one created with the cluster."""

import time
from ipaddress import IPv4Address
from pathlib import Path

from . import model
from .create import addons, kubeadm, nodes, units
from .create import images as create_images
from .create.kubeadm import kubectl_env
from .lifecycle import ClusterError, save_cluster
from .phases import Context, Phase, PhaseFailed

# ---- cluster.yaml edits


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
    if host == 100:
        host = 101
    if host > 199:
        raise ClusterError(f"no free node address left in {net} (.200-.219 is the LB pool)")
    return net.network_address + host


def plan_add(c: model.Cluster, req: model.NodeRequest) -> model.Node:
    """The size defaults to the first node of the same role (or the first node at all)."""
    like = next((n for n in c.spec.nodes if n.role == req.role), c.first_control_plane)
    return model.Node(
        role=req.role,
        name=next_name(c, req.role),
        ip=next_ip(c),
        cpu=req.cpu or like.cpu,
        memory=req.memory or like.memory,
        join=req.join,
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


# ---- add node


def plan_done(ctx: Context) -> bool:
    return ctx.node is not None and find(ctx.require_cluster(), ctx.node) is not None


def plan_run(ctx: Context) -> None:
    c = ctx.require_cluster()
    if ctx.request is None:
        raise PhaseFailed("add node needs a request (role, size, join)")
    n = plan_add(c, ctx.request)
    save_cluster(ctx, with_node(c, n))
    ctx.node = n.name
    ctx.say(f"  {n.name}: {n.role} at {n.ip}, {n.cpu} cpu, {n.memory}")


PLAN = Phase(
    "plan", "name, address and size of the new node, recorded in cluster.yaml", plan_done, plan_run
)


def start_done(ctx: Context) -> bool:
    n = require_node(ctx)
    if not ctx.host.is_active(f"{n}.service"):
        return False
    return nodes.systemd_state(ctx, n) in ("running", "degraded")


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


def start_run(ctx: Context) -> None:
    """daemon-reload picks the new unit up, the LB learns the new backend list, the node starts."""
    n = require_node(ctx)
    ctx.host.run(["systemctl", "daemon-reload"], sudo=True)
    reload_lb(ctx)
    ctx.host.run(["systemctl", "start", f"{n}.service"], sudo=True)
    nodes.wait_node(ctx, n)


START = Phase(
    "start", "reload the LB, start the node, wait for its systemd and CRI-O", start_done, start_run
)


# ---- join: the same three phases finish an add and tkctl join node


def node_ready(ctx: Context, name: str) -> bool:
    ready = 'jsonpath={.status.conditions[?(@.type=="Ready")].status}'
    r = ctx.host.run(
        ["kubectl", "get", "node", name, "-o", ready],
        env=kubectl_env(ctx),
        capture=True,
        check=False,
    )
    return r.returncode == 0 and r.stdout.strip() == "True"


def join_done(ctx: Context) -> bool:
    return kubeadm.joined(ctx, require_node(ctx))


def join_run(ctx: Context) -> None:
    n = node_of(ctx)
    join = kubeadm.join_command(ctx)
    key = kubeadm.certificate_key(ctx, fresh=True) if n.role == "control-plane" else ""
    kubeadm.join_node(ctx, n, join, key)


JOIN = Phase("kubeadm", "kubeadm join through the first control plane", join_done, join_run)


def ready_done(ctx: Context) -> bool:
    return node_ready(ctx, require_node(ctx))


def ready_run(ctx: Context) -> None:
    n = node_of(ctx)
    for i in range(1, 61):
        if node_ready(ctx, n.name):
            break
        if i == 60:
            raise PhaseFailed(
                f"{n.name} is not Ready after 5 minutes (kubectl describe node {n.name})"
            )
        time.sleep(ctx.options.poll_slow)
    if n.role == "control-plane":
        kubeadm.untaint_control_planes(ctx)
    ctx.say(f"  {n.name} Ready ({n.role})")


READY = Phase(
    "ready",
    "wait for the node to be Ready (control planes schedule workloads too)",
    ready_done,
    ready_run,
)


def record_done(ctx: Context) -> bool:
    return node_of(ctx).join


def record_run(ctx: Context) -> None:
    save_cluster(ctx, marked_joined(ctx.require_cluster(), require_node(ctx)))


RECORD = Phase("record", "record join: true in cluster.yaml", record_done, record_run)


# images, units, runtimeclass and gvisor are create's own phases: each is idempotent over the
# whole cluster, so the new node gets what the others got.
PROVISION: tuple[Phase, ...] = (
    create_images.PHASE,
    units.PHASE,
    START,
    addons.RUNTIMECLASS,
    addons.GVISOR,
)


def add_phase(join: bool) -> Phase:
    children: tuple[Phase, ...] = (PLAN, *PROVISION)
    if join:
        children += (JOIN, READY, RECORD)
    return Phase("add", "add a node to the cluster", None, lambda ctx: None, children=children)


# join node finishes whatever an add left undone (a node recorded in cluster.yaml but never
# started, or started unjoined): every phase before kubeadm is already done on a normal node.
JOIN_PHASE = Phase(
    "join",
    "finish a node recorded in cluster.yaml and join it",
    None,
    lambda ctx: None,
    children=(*PROVISION, JOIN, READY, RECORD),
)
