"""router: the VyOS container(s) of a cluster: the image, how tkctl waits for a router, reads
its backend or peer list and reloads its configuration. Every function that talks to a router
names it (model.Router); the cluster-wide ones at the end loop over Cluster.routers. The
tkctl.boot blocks are router_config's, the running/VRRP state router_state's."""

import json

from .. import model, podman, versions
from ..phases import Context, PhaseFailed, wait_for
from .router_state import OP, running  # OP lives there: router_state cannot import this module


def image(v: versions.Versions) -> str:
    return f"ghcr.io/tarokolabs/tk8s/router:{v.vyos}"


# ---- one router


def booted(ctx: Context, r: model.Router) -> bool:
    """VyOS writes /tmp/vyos-config-status (0 = applied) when the boot-time config load ends;
    vyos-router.service is active long before that."""
    res = podman.exec_(ctx, r.name, "cat", "/tmp/vyos-config-status")
    return res.returncode == 0 and res.stdout.strip() == "0"


def backends(ctx: Context, r: model.Router) -> list[tuple[str, str]]:
    """(server, status) for every control plane in the api backend, from `show haproxy`."""
    res = podman.exec_(ctx, r.name, OP, "show", "haproxy")
    if res.returncode != 0:
        return []
    rows = (line.split() for line in res.stdout.splitlines())
    return [
        (f[1], f[2])
        for f in rows
        if len(f) > 2 and f[0] == "api" and f[1] not in ("FRONTEND", "BACKEND")
    ]


def _vtysh(ctx: Context, r: model.Router, command: str) -> object | None:
    """One op-mode query as JSON, or None when the router or FRR does not answer."""
    res = podman.exec_(ctx, r.name, "vtysh", "-c", f"{command} json")
    if res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        data: object = json.loads(res.stdout)
    except ValueError:
        return None
    return data


def peers(ctx: Context, r: model.Router, c: model.Cluster) -> list[tuple[str, str]]:
    """(control plane, BGP state) per neighbor in `show bgp ipv4 unicast summary`; a neighbor
    cluster.yaml does not have is listed by address."""
    data = _vtysh(ctx, r, "show bgp ipv4 unicast summary")
    if not isinstance(data, dict):
        return []
    by_ip = {str(n.ip): n.name for n in c.control_planes}
    return [
        (by_ip.get(ip, ip), str(p.get("state", "?")))
        for ip, p in data.get("peers", {}).items()
        if isinstance(p, dict)
    ]


def _nexthop_ips(ctx: Context, r: model.Router, c: model.Cluster) -> list[str]:
    """The active next hops of the router's selected route to the VIP: where apiserver
    connections go, empty without a route."""
    vip = c.spec.network.vip
    data = _vtysh(ctx, r, f"show ip route {vip}/32")
    entries = data.get(f"{vip}/32", []) if isinstance(data, dict) else []
    dicts = [e for e in entries if isinstance(e, dict)]
    chosen = next((e for e in dicts if e.get("selected")), dicts[0] if dicts else None)
    if not isinstance(chosen, dict):
        return []
    hops = chosen.get("nexthops", [])
    if not isinstance(hops, list):
        return []
    return [str(h.get("ip", "")) for h in hops if isinstance(h, dict) and h.get("active")]


def nexthops(ctx: Context, r: model.Router, c: model.Cluster) -> int:
    """How many active next hops the route to the VIP has: the ECMP width that carries traffic."""
    return len(_nexthop_ips(ctx, r, c))


def routes_to(ctx: Context, r: model.Router, c: model.Cluster, n: model.Node) -> bool:
    """Whether this router still sends apiserver connections to this control plane: an UP
    backend in proxy mode, an active next hop of the VIP route in bgp mode."""
    if c.spec.network.ha == "proxy":
        return any(name == n.name and s.startswith("UP") for name, s in backends(ctx, r))
    return str(n.ip) in _nexthop_ips(ctx, r, c)


def bfd_down(ctx: Context, r: model.Router, c: model.Cluster) -> list[str]:
    """The control planes whose BFD session with this router is not up."""
    data = _vtysh(ctx, r, "show bfd peers")
    status = {s.get("peer"): s.get("status") for s in data} if isinstance(data, list) else {}
    return [n.name for n in c.control_planes if status.get(str(n.ip)) != "up"]


def members(ctx: Context, r: model.Router, c: model.Cluster) -> list[tuple[str, str]]:
    """What this router knows per control plane in this mode: backends or BGP peers."""
    return backends(ctx, r) if c.spec.network.ha == "proxy" else peers(ctx, r, c)


def kind(c: model.Cluster) -> str:
    return "backend" if c.spec.network.ha == "proxy" else "peer"


def show(c: model.Cluster) -> str:
    """The op-mode command a person runs to see the same list."""
    return f"{OP} show haproxy" if c.spec.network.ha == "proxy" else "vtysh -c 'show bgp summary'"


def show_path(c: model.Cluster) -> str:
    """The op-mode command that shows where apiserver connections go right now."""
    if c.spec.network.ha == "proxy":
        return f"{OP} show haproxy"
    return f"vtysh -c 'show ip route {c.spec.network.vip}'"


def synced(ctx: Context, r: model.Router, c: model.Cluster) -> bool:
    """This router's backend (proxy) or peer (bgp) list is exactly the cluster's control planes."""
    return {n for n, _ in members(ctx, r, c)} == {n.name for n in c.control_planes}


def wait_booted(ctx: Context, r: model.Router) -> None:
    """The boot-time config load holds VyOS for a few seconds after start; a load alongside it
    fails ("Failed to generate committed config") with exit status 0."""
    wait_for(
        ctx,
        lambda: booted(ctx, r),
        rounds=30,
        delay=ctx.options.poll,
        failure=lambda: (
            f"{r.name} did not finish booting within 60 s (podman logs {r.name}; "
            f"podman exec {r.name} cat /tmp/vyos-config-status)"
        ),
    )


def wait_synced(ctx: Context, r: model.Router, c: model.Cluster) -> None:
    """Every control plane listed as a backend (proxy) or neighbor (bgp), no other: the config
    took. The backends stay DOWN, the sessions short of Established, until kubeadm brings an
    apiserver up (and, in bgp mode, kubeadm/frr starts FRR in the control plane)."""

    def mismatch() -> str:
        listed = ", ".join(n for n, _ in members(ctx, r, c)) or "nothing"
        want = ", ".join(n.name for n in c.control_planes)
        return (
            f"{r.name} lists {listed} as {kind(c)}s, cluster.yaml has {want} "
            f"(podman exec {r.name} {show(c)})"
        )

    wait_for(ctx, lambda: synced(ctx, r, c), rounds=30, delay=ctx.options.poll, failure=mismatch)
    got = len(c.control_planes)
    ctx.say(f"  {r.name}: VyOS up, {got} control plane {kind(c)}(s) at {r.ip}")


RELOAD = (
    "cp /opt/vyatta/etc/config/tkctl.boot /opt/vyatta/etc/config/config.boot\n"
    "grep '^// ' /opt/vyatta/etc/config.boot.default >> /opt/vyatta/etc/config/config.boot\n"
    "source /opt/vyatta/etc/functions/script-template\n"
    "configure\n"
    "load /opt/vyatta/etc/config/config.boot\n"
    "loaded=$?\n"
    "commit\n"
    "exit\n"
    "builtin exit $loaded\n"
)


def reload(ctx: Context, r: model.Router, c: model.Cluster) -> None:
    """tkctl.boot was re-rendered (a control plane came or went): it becomes config.boot with
    VyOS's version footer (so nothing is migrated), then VyOS loads it and commits the difference
    without dropping connections. `exit` in script-template only tears the session down, so the
    script carries load's status out itself; commit's is useless both ways (1 when nothing
    changed, 0 when it failed), so the backend list is the proof the change took. The session
    runs in the vyattacfg group, as VyOS requires of configuration scripts: what it leaves under
    /opt/vyatta/config must stay writable to that group, not become root's."""
    wait_booted(ctx, r)
    res = podman.exec_(ctx, r.name, "sg", "vyattacfg", "-c", "/bin/vbash -s", input=RELOAD)
    out = res.stdout + res.stderr
    if res.returncode != 0 or "Commit failed" in out:
        raise PhaseFailed(f"{r.name} did not accept config.boot: {out.strip()[-300:]}")
    wait_synced(ctx, r, c)


# ---- every router of the cluster


def wait(ctx: Context) -> None:
    """create: every router boots and lists the control planes."""
    c = ctx.require_cluster()
    for r in c.routers:
        wait_booted(ctx, r)
        wait_synced(ctx, r, c)


def all_booted_and_synced(ctx: Context, c: model.Cluster) -> bool:
    return all(booted(ctx, r) and synced(ctx, r, c) for r in c.routers)


def running_routers(ctx: Context, c: model.Cluster) -> list[model.Router]:
    return [r for r in c.routers if running(ctx, r)]


def all_synced(ctx: Context, c: model.Cluster) -> bool:
    """Every running router lists exactly the control planes; false when none runs."""
    up = running_routers(ctx, c)
    return bool(up) and all(synced(ctx, r, c) for r in up)


def reload_all(ctx: Context, c: model.Cluster) -> None:
    """node add/delete: the running routers load the new tkctl.boot now; a stopped one gets it
    at its next boot (the preconfig script copies tkctl.boot over config.boot every time), so it
    is skipped with a word, not waited for. No router running at all is a failure: nothing would
    carry the change."""
    up = running_routers(ctx, c)
    if not up:
        raise PhaseFailed(f"no router of cluster {c.name} is running")
    for r in c.routers:
        if r in up:
            reload(ctx, r, c)
        else:
            ctx.say(f"  {r.name} is not running; it loads the new tkctl.boot when it starts")


def any_routes_to(ctx: Context, c: model.Cluster, n: model.Node) -> list[str]:
    """The running routers that still send apiserver connections to this control plane."""
    return [r.name for r in running_routers(ctx, c) if routes_to(ctx, r, c, n)]
