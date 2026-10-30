"""add node and join node: the phases reuse what create already does for the whole cluster (the
image store, the units, the per-node addons), so a node added later looks exactly like one created
with the cluster. join node runs the same tail to finish a node that an add left unjoined."""

from ..create import addons, cni, frr, kubeadm, nodes, preflight, router, units
from ..create import images as create_images
from ..create.plan import PlanError, check_memory_minimum
from ..datatree import PreflightError
from ..lifecycle import ClusterError, save_cluster
from ..phases import Context, Phase, PhaseFailed, wait_for
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
    try:
        n = plan_add(c, ctx.request)
    except ClusterError as err:
        raise PhaseFailed(str(err)) from err
    grown = with_node(c, n)
    if units.sized(c):
        # the same refusals create makes, before the node is written into cluster.yaml: a node
        # under 2G has no allocatable left, one above the host cannot be backed
        try:
            check_memory_minimum(n.memory)
            ctx.cluster = grown
            if c.multi_host:
                preflight.check_host_fits(ctx.on(ctx.host_for(n.name)), [n])
            else:
                preflight.check_host_fits(ctx)
        except (PlanError, PreflightError) as err:
            ctx.cluster = c
            raise PhaseFailed(str(err)) from err
    save_cluster(ctx, grown)
    ctx.node = n.name
    where = f" on {n.host}" if n.host else ""
    ctx.say(f"  {n.name}: {n.role} at {n.ip}{where}, {n.cpu} cpu, {n.memory}")


PLAN = Phase(
    "plan", "name, address and size of the new node, recorded in cluster.yaml", plan_done, plan_run
)


def start_done(ctx: Context) -> bool:
    n = require_node(ctx)
    active = ctx.host_for(n).is_active(f"{n}.service")
    if not active or not router.all_synced(ctx, ctx.require_cluster()):
        return False
    return nodes.systemd_state(ctx, n) in ("running", "degraded")


def start_run(ctx: Context) -> None:
    """daemon-reload picks the new unit up (on the node's host), the router learns the new
    backend list, the node starts."""
    n = require_node(ctx)
    host = ctx.host_for(n)
    host.run(["systemctl", "daemon-reload"], sudo=True)
    router.reload_all(ctx, ctx.require_cluster())
    host.run(["systemctl", "start", f"{n}.service"], sudo=True)
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
    wait_for(
        ctx,
        lambda: node_ready(ctx, n.name),
        rounds=60,
        delay=ctx.options.poll_slow,
        failure=lambda: f"{n.name} is not Ready after 5 minutes (kubectl describe node {n.name})",
    )
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
        children += (JOIN, READY, cni.HOST_ROUTES, RECORD)
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
