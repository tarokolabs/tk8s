"""describe's Router: section: mode, the address api.<cluster>.k8s resolves to, the router(s)
with their VRRP state when there are two, a shell and a stop command, and what each router
knows about the control planes (haproxy backends or BGP peers and ECMP next hops)."""

from . import model
from .create import router, router_state
from .phases import Context


def describe_router(ctx: Context, c: model.Cluster, state: str) -> None:
    net = c.spec.network
    two = len(c.routers) == 2
    first = c.routers[0]
    ctx.say("Router:")
    if net.ha == "proxy":
        ctx.say("  mode:     proxy (VyOS load-balancing haproxy, TCP check on every control plane)")
        floats = "; VRRP floats it between the two routers" if two else ""
        ctx.say(f"  address:  {net.router} (api.{c.domain} resolves to it on every node{floats})")
    else:
        ctx.say(
            "  mode:     bgp (VyOS BGP AS 65000, ECMP over every control plane announcing the VIP)"
        )
        floats = "; VRRP floats it between the two routers" if two else ""
        ctx.say(f"  address:  {net.router} (api.{c.domain} -> {net.vip}, the VIP{floats})")
    if two:
        _describe_router_pair(ctx, c, state)
    else:
        ctx.say(f"  shell:    sudo podman exec -it {first.name} vbash")
    label = f"{router.kind(c)}s:"
    if state != "Running":
        ctx.say(f"  {label:<10}unavailable (cluster is not running)")
    else:
        per_router = []
        for rt in c.routers:
            rows = router.members(ctx, rt, c)
            listed = ", ".join(f"{n} {s}" for n, s in rows) or "none listed"
            per_router.append(f"{listed} ({rt.name})" if two else listed)
        ctx.say(f"  {label:<10}" + " / ".join(per_router))
        if net.ha == "bgp":
            hops = [router.nexthops(ctx, rt, c) for rt in c.routers]
            if two:
                where = f"{hops[0]} to {net.vip} on {first.name}, {hops[1]} on {c.routers[1].name}"
            else:
                where = f"{hops[0]} to {net.vip}"
            ctx.say(f"  nexthops: {where} (show ip route {net.vip})")
    if net.ha == "bgp" and len(c.control_planes) == 1:
        ctx.say("  note:     one control plane: nothing to spread; --control-planes 3 shows ECMP")


def _describe_router_pair(ctx: Context, c: model.Cluster, state: str) -> None:
    """The two routers with their VRRP state, how to get a shell on each, and how to stop one
    (podman stop is undone by the unit's Restart=always within seconds)."""
    r1, r2 = c.routers
    if state == "Running":
        pair = ", ".join(
            f"{rt.name} {rt.ip} {router_state.vrrp(ctx, rt) or 'unavailable'}" for rt in c.routers
        )
    else:
        pair = f"{r1.name} {r1.ip}, {r2.name} {r2.ip} (unavailable: cluster is not running)"
    ctx.say(f"  routers:  {pair}")
    ctx.say(f"  shell:    sudo podman exec -it {r1.name} vbash   ({r2.name} likewise)")
    ctx.say(
        f"  stop:     sudo systemctl stop {r1.name}.service   "
        "(podman stop would be restarted by systemd)"
    )
