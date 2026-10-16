"""[router]: the apiserver endpoint answers and the VyOS router has every joined control plane
behind it, as haproxy backends (proxy) or as BGP peers with ECMP next hops to the VIP (bgp)."""

from .. import model
from ..create import router
from .report import Report


def _readyz(r: Report, endpoint: str) -> str:
    return r.ctx.host.run(
        ["curl", "-sk", "--max-time", "5", f"https://{endpoint}:6443/readyz"],
        capture=True,
        check=False,
    ).stdout[:40]


def _check_router_proxy(r: Report, c: model.Cluster, ready: str) -> None:
    endpoint = c.spec.network.router
    joined = sum(1 for n in c.control_planes if n.join)
    listed = router.backends(r.ctx)
    up = sum(1 for _, status in listed if status.startswith("UP"))  # "UP 1/2" too
    stale = {n for n, _ in listed} != {n.name for n in c.control_planes}
    if ready == "ok" and up == joined and not stale:
        r.ok(f"apiserver router {endpoint} healthy ({up}/{joined} control planes UP)")
    else:
        names = ", ".join(n for n, _ in listed) or "nothing"
        r.ng(
            f"apiserver router {endpoint}",
            f"readyz='{ready}', {up} of {joined} control planes UP"
            + (f", backends {names}" if stale else "")
            + f" (podman exec {c.name}-router {router.OP} show haproxy)",
        )


def _check_router_bgp(r: Report, c: model.Cluster, ready: str) -> None:
    net = c.spec.network
    joined_names = {n.name for n in c.control_planes if n.join}
    joined = len(joined_names)
    peers = router.peers(r.ctx, c)
    # a --defer-join / --no-join control plane runs FRR (session up) but announces nothing
    up = sum(1 for n, s in peers if s == "Established" and n in joined_names)
    stale = {n for n, _ in peers} != {n.name for n in c.control_planes}
    hops = router.nexthops(r.ctx, c)
    down = router.bfd_down(r.ctx, c)
    if ready == "ok" and up == joined and not stale and hops == joined and not down:
        r.ok(
            f"apiserver router {net.router} healthy ({up}/{joined} control planes Established, "
            f"{hops} ECMP next hops to {net.vip}, BFD up)"
        )
    else:
        names = ", ".join(n for n, _ in peers) or "nothing"
        r.ng(
            f"apiserver router {net.router}",
            f"readyz='{ready}', {up} of {joined} control planes Established, "
            f"{hops} next hops to {net.vip}"
            + (f", BFD down on {', '.join(down)}" if down else "")
            + (f", peers {names}" if stale else "")
            + f" (podman exec {c.name}-router {router.show(c)})",
        )


def check_router(r: Report) -> None:
    """The endpoint answers /readyz and the router knows every joined control plane: backends UP
    in proxy mode; peers Established, as many ECMP next hops to the VIP, BFD up in bgp mode."""
    c = r.ctx.require_cluster()
    ready = _readyz(r, str(c.endpoint_ip))
    if c.spec.network.ha == "proxy":
        _check_router_proxy(r, c, ready)
    else:
        _check_router_bgp(r, c, ready)
