"""frr: in bgp mode every control plane runs FRR (bgpd + bfdd) and announces the apiserver VIP to
the router exactly while its own apiserver answers /readyz (tk8s-apiserver-health keeps the VIP
on lo). tkctl writes the FRR configuration and the VIP into the node and turns on the units the
node image ships disabled. proxy mode has nothing to do here."""

from .. import model, podman
from ..phases import Context, Phase, PhaseFailed
from ..render import template

UNITS = ["frr.service", "tk8s-apiserver-health.timer"]
DAEMONS = "s/^bgpd=no/bgpd=yes/; s/^bfdd=no/bfdd=yes/"


def config(ctx: Context, n: model.Node) -> str:
    c = ctx.require_cluster()
    net = c.spec.network
    return template(
        ctx.paths.home,
        "node/frr.conf",
        cluster=c.name,
        node=n.name,
        node_ip=n.ip,
        router_ip=net.router,
        vip=net.vip,
    )


def configured(ctx: Context, n: model.Node) -> bool:
    """The VIP file, the very configuration this tkctl renders, and both units active: anything
    less (an interrupted run, a changed router address) is configured again."""
    vip = podman.node_read(ctx, n.name, "/etc/tk8s/vip")
    if vip is None or vip.strip() != str(ctx.require_cluster().spec.network.vip):
        return False
    if podman.node_read(ctx, n.name, "/etc/frr/frr.conf") != config(ctx, n):
        return False
    return all(podman.node_active(ctx, n.name, u) for u in UNITS)


def configure(ctx: Context, n: model.Node) -> None:
    c = ctx.require_cluster()
    net = c.spec.network
    if not podman.node_has(ctx, n.name, "/etc/frr/daemons", "-f"):
        # a host that cached the node tag from before FRR was in the recipe
        raise PhaseFailed(
            f"{n.name} has no FRR: its node image predates bgp mode; run tkctl pull images "
            f"--refresh --k8s {c.spec.kubernetes} and create the cluster again"
        )
    previous = (podman.node_read(ctx, n.name, "/etc/tk8s/vip") or "").strip()
    podman.must(ctx, n.name, "sed", "-i", DAEMONS, "/etc/frr/daemons")
    podman.must(
        ctx,
        n.name,
        "install",
        "-m",
        "640",
        "-o",
        "frr",
        "-g",
        "frr",
        "/dev/stdin",
        "/etc/frr/frr.conf",
        input=config(ctx, n),
    )
    podman.must(
        ctx,
        n.name,
        "install",
        "-D",
        "-m",
        "644",
        "/dev/stdin",
        "/etc/tk8s/vip",
        input=f"{net.vip}\n",
    )
    if previous and previous != str(net.vip):
        # the health script only moves the current VIP; a changed one would leave the old /32 on lo
        podman.exec_(ctx, n.name, "ip", "addr", "del", f"{previous}/32", "dev", "lo")
    podman.must(ctx, n.name, "systemctl", "enable", "--quiet", *UNITS)
    # restart, not start: a rerun with a changed frr.conf must load it
    podman.must(ctx, n.name, "systemctl", "restart", *UNITS)
    ctx.say(f"  {n.name}: FRR announces {net.vip} to {net.router} while its apiserver is ready")


def _targets(ctx: Context, *, only_this_node: bool) -> list[model.Node]:
    """bgp mode's control planes; with only_this_node, just the one a node verb works on (an
    empty list for a worker, for proxy mode, for no node), so a stopped sibling is never touched."""
    c = ctx.require_cluster()
    if c.spec.network.ha != "bgp":
        return []
    if not only_this_node:
        return c.control_planes
    n = next((x for x in c.spec.nodes if x.name == ctx.node), None)
    return [n] if n is not None and n.role == "control-plane" else []


def _done(ctx: Context, nodes: list[model.Node]) -> bool:
    return all(configured(ctx, n) for n in nodes)


def _run(ctx: Context, nodes: list[model.Node]) -> None:
    for n in nodes:
        if configured(ctx, n):
            ctx.say(f"  {n.name} already configured")
        else:
            configure(ctx, n)


PHASE = Phase(
    "frr",
    "bgp mode: FRR and the VIP health timer on every control plane",
    lambda ctx: _done(ctx, _targets(ctx, only_this_node=False)),
    lambda ctx: _run(ctx, _targets(ctx, only_this_node=False)),
)
# what add node / join node run, so a stopped sibling control plane is no concern
NODE_PHASE = Phase(
    "frr",
    "bgp mode: FRR and the VIP health timer on this control plane",
    lambda ctx: _done(ctx, _targets(ctx, only_this_node=True)),
    lambda ctx: _run(ctx, _targets(ctx, only_this_node=True)),
)
