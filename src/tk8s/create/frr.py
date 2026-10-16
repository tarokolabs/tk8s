"""frr: in bgp mode every control plane runs FRR (bgpd + bfdd) and announces the apiserver VIP to
the router exactly while its own apiserver answers /readyz (tk8s-apiserver-health keeps the VIP
on lo). tkctl writes the FRR configuration and the VIP into the node and turns on the units the
node image ships disabled. proxy mode has nothing to do here."""

from .. import model
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


def _read(ctx: Context, node: str, path: str) -> str | None:
    r = ctx.host.run(["podman", "exec", node, "cat", path], sudo=True, capture=True, check=False)
    return r.stdout if r.returncode == 0 else None


def configured(ctx: Context, n: model.Node) -> bool:
    """The VIP file, the very configuration this tkctl renders, and both units active: anything
    less (an interrupted run, a changed router address) is configured again."""
    vip = _read(ctx, n.name, "/etc/tk8s/vip")
    if vip is None or vip.strip() != str(ctx.require_cluster().spec.network.vip):
        return False
    if _read(ctx, n.name, "/etc/frr/frr.conf") != config(ctx, n):
        return False
    return all(
        ctx.host.run(
            ["podman", "exec", n.name, "systemctl", "is-active", "--quiet", u],
            sudo=True,
            check=False,
        ).returncode
        == 0
        for u in UNITS
    )


def configure(ctx: Context, n: model.Node) -> None:
    c = ctx.require_cluster()
    net = c.spec.network
    has_frr = ctx.host.run(
        ["podman", "exec", n.name, "test", "-f", "/etc/frr/daemons"], sudo=True, check=False
    )
    if has_frr.returncode != 0:
        # a host that cached the node tag from before FRR was in the recipe
        raise PhaseFailed(
            f"{n.name} has no FRR: its node image predates bgp mode; run tkctl pull images "
            f"--refresh --k8s {c.spec.kubernetes} and create the cluster again"
        )
    previous = (_read(ctx, n.name, "/etc/tk8s/vip") or "").strip()
    ctx.host.run(["podman", "exec", n.name, "sed", "-i", DAEMONS, "/etc/frr/daemons"], sudo=True)
    ctx.host.run(
        [
            "podman",
            "exec",
            "-i",
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
        ],
        sudo=True,
        input=config(ctx, n),
    )
    ctx.host.run(
        [
            "podman",
            "exec",
            "-i",
            n.name,
            "install",
            "-D",
            "-m",
            "644",
            "/dev/stdin",
            "/etc/tk8s/vip",
        ],
        sudo=True,
        input=f"{net.vip}\n",
    )
    if previous and previous != str(net.vip):
        # the health script only moves the current VIP; a changed one would leave the old /32 on lo
        ctx.host.run(
            ["podman", "exec", n.name, "ip", "addr", "del", f"{previous}/32", "dev", "lo"],
            sudo=True,
            check=False,
        )
    ctx.host.run(["podman", "exec", n.name, "systemctl", "enable", "--quiet", *UNITS], sudo=True)
    # restart, not start: a rerun with a changed frr.conf must load it
    ctx.host.run(["podman", "exec", n.name, "systemctl", "restart", *UNITS], sudo=True)
    ctx.say(f"  {n.name}: FRR announces {net.vip} to {net.router} while its apiserver is ready")


def done(ctx: Context) -> bool:
    c = ctx.require_cluster()
    return c.spec.network.ha != "bgp" or all(configured(ctx, n) for n in c.control_planes)


def run(ctx: Context) -> None:
    for n in ctx.require_cluster().control_planes:
        if configured(ctx, n):
            ctx.say(f"  {n.name} already configured")
        else:
            configure(ctx, n)


PHASE = Phase("frr", "bgp mode: FRR and the VIP health timer on every control plane", done, run)


# ---- one node: what add node / join node run, so a stopped sibling control plane is no concern


def _this_node(ctx: Context) -> model.Node | None:
    """The control plane the node verb works on, or None (a worker, proxy mode, no node)."""
    c = ctx.require_cluster()
    if c.spec.network.ha != "bgp" or ctx.node is None:
        return None
    n = next((x for x in c.spec.nodes if x.name == ctx.node), None)
    return n if n is not None and n.role == "control-plane" else None


def node_done(ctx: Context) -> bool:
    n = _this_node(ctx)
    return n is None or configured(ctx, n)


def node_run(ctx: Context) -> None:
    n = _this_node(ctx)
    if n is None:
        return
    if configured(ctx, n):
        ctx.say(f"  {n.name} already configured")
    else:
        configure(ctx, n)


NODE_PHASE = Phase(
    "frr", "bgp mode: FRR and the VIP health timer on this control plane", node_done, node_run
)
