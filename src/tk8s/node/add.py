"""add node and join node: the phases reuse what create already does for the whole cluster (the
image store, the units, the per-node addons), so a node added later looks exactly like one created
with the cluster. join node runs the same tail to finish a node that an add left unjoined."""

import time

from ..create import addons, frr, kubeadm, nodes, router, units
from ..create import images as create_images
from ..lifecycle import save_cluster
from ..phases import Context, Phase, PhaseFailed
from .common import (
    find,
    marked_joined,
    node_of,
    node_ready,
    plan_add,
    require_node,
    with_node,
)


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
    if not ctx.host.is_active(f"{n}.service") or not router.synced(ctx, ctx.require_cluster()):
        return False
    return nodes.systemd_state(ctx, n) in ("running", "degraded")


def start_run(ctx: Context) -> None:
    """daemon-reload picks the new unit up, the router learns the new backend list, the node
    starts."""
    n = require_node(ctx)
    ctx.host.run(["systemctl", "daemon-reload"], sudo=True)
    router.reload(ctx, ctx.require_cluster())
    ctx.host.run(["systemctl", "start", f"{n}.service"], sudo=True)
    nodes.wait_node(ctx, n)


START = Phase(
    "start",
    "reload the router, start the node, wait for its systemd and CRI-O",
    start_done,
    start_run,
)


# ---- join: the same three phases finish an add and tkctl join node


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
    frr.NODE_PHASE,
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
