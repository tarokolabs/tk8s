"""[router]: the apiserver endpoint answers and every VyOS router has every joined control plane
behind it, as haproxy backends (proxy) or as BGP peers with ECMP next hops to the VIP (bgp); a
two-router cluster also has exactly one VRRP MASTER and one BACKUP."""

from .. import model
from ..create import router, router_state
from .report import Report


def _readyz(r: Report, endpoint: str) -> str:
    return r.ctx.host.run(
        ["curl", "-sk", "--max-time", "5", f"https://{endpoint}:6443/readyz"],
        capture=True,
        check=False,
    ).stdout[:40]


def _label(c: model.Cluster, rt: model.Router) -> str:
    """How the line names the router: by address alone with one router (the wording students
    know), by name and address with two."""
    return str(c.spec.network.router) if len(c.routers) == 1 else f"{rt.name} ({rt.ip})"


def _check_proxy(r: Report, c: model.Cluster, rt: model.Router, ready: str) -> None:
    joined = sum(1 for n in c.control_planes if n.join)
    listed = router.backends(r.ctx, rt)
    up = sum(1 for _, status in listed if status.startswith("UP"))  # "UP 1/2" too
    stale = {n for n, _ in listed} != {n.name for n in c.control_planes}
    if ready == "ok" and up == joined and not stale:
        r.ok(f"apiserver router {_label(c, rt)} healthy ({up}/{joined} control planes UP)")
    else:
        names = ", ".join(n for n, _ in listed) or "nothing"
        r.ng(
            f"apiserver router {_label(c, rt)}",
            f"readyz='{ready}', {up} of {joined} control planes UP"
            + (f", backends {names}" if stale else "")
            + f" (podman exec {rt.name} {router.OP} show haproxy)",
        )


def _check_bgp(r: Report, c: model.Cluster, rt: model.Router, ready: str) -> None:
    net = c.spec.network
    joined_names = {n.name for n in c.control_planes if n.join}
    joined = len(joined_names)
    peers = router.peers(r.ctx, rt, c)
    # a --defer-join / --no-join control plane runs FRR (session up) but announces nothing
    up = sum(1 for n, s in peers if s == "Established" and n in joined_names)
    stale = {n for n, _ in peers} != {n.name for n in c.control_planes}
    hops = router.nexthops(r.ctx, rt, c)
    down = router.bfd_down(r.ctx, rt, c)
    if ready == "ok" and up == joined and not stale and hops == joined and not down:
        r.ok(
            f"apiserver router {_label(c, rt)} healthy ({up}/{joined} control planes "
            f"Established, {hops} ECMP next hops to {net.vip}, BFD up)"
        )
    else:
        names = ", ".join(n for n, _ in peers) or "nothing"
        r.ng(
            f"apiserver router {_label(c, rt)}",
            f"readyz='{ready}', {up} of {joined} control planes Established, "
            f"{hops} next hops to {net.vip}"
            + (f", BFD down on {', '.join(down)}" if down else "")
            + (f", peers {names}" if stale else "")
            + f" (podman exec {rt.name} {router.show(c)})",
        )


def _check_vrrp(r: Report, c: model.Cluster) -> None:
    """Exactly one MASTER and one BACKUP, whichever router holds which (router-1 takes the
    address back ten seconds after it boots, so the pair may be swapped for a moment)."""
    states = {rt.name: router_state.vrrp(r.ctx, rt) or "unavailable" for rt in c.routers}
    listed = ", ".join(f"{n} {s}" for n, s in states.items())
    if sorted(states.values()) == ["BACKUP", "MASTER"]:
        r.ok(f"router VRRP: {listed}")
    else:
        r.ng(
            "router VRRP",
            f"{listed}; expected one MASTER and one BACKUP "
            f"(podman exec {c.routers[0].name} {router_state.OP} show vrrp)",
        )


def check_router(r: Report) -> None:
    """The endpoint answers /readyz and every router knows every joined control plane: backends
    UP in proxy mode; peers Established, as many ECMP next hops to the VIP, BFD up in bgp mode.
    Two routers must also agree on who holds the floating address."""
    c = r.ctx.require_cluster()
    ready = _readyz(r, str(c.endpoint_ip))
    for rt in c.routers:
        if c.spec.network.ha == "proxy":
            _check_proxy(r, c, rt, ready)
        else:
            _check_bgp(r, c, rt, ready)
    if len(c.routers) == 2:
        _check_vrrp(r, c)
