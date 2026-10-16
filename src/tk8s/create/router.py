"""router: the VyOS container of a cluster: its image, the server blocks of its tkctl.boot,
and how tkctl waits for it, reads its load balancer status and reloads its configuration."""

import json
import time

from .. import model, versions
from ..phases import Context, PhaseFailed

OP = "/opt/vyatta/bin/vyatta-op-cmd-wrapper"


def image(v: versions.Versions) -> str:
    return f"ghcr.io/tarokolabs/tk8s/router:{v.vyos}"


def servers(c: model.Cluster) -> str:
    """The backend's server blocks, one per control plane, in config.boot's own layout."""
    return "\n".join(
        f"            server {n.name} {{\n"
        f'                address "{n.ip}"\n'
        f"                check {{\n"
        f"                }}\n"
        f'                port "6443"\n'
        f"            }}"
        for n in c.control_planes
    )


def neighbors(c: model.Cluster) -> str:
    """One eBGP neighbor per control plane (AS 65001, BFD on, timers 3/9), in config.boot's own
    layout, for the bgp template."""
    return "\n".join(
        f"        neighbor {n.ip} {{\n"
        f"            address-family {{\n"
        f"                ipv4-unicast {{\n"
        f"                }}\n"
        f"            }}\n"
        f"            bfd {{\n"
        f"            }}\n"
        f'            remote-as "65001"\n'
        f"            timers {{\n"
        f'                holdtime "9"\n'
        f'                keepalive "3"\n'
        f"            }}\n"
        f"        }}"
        for n in c.control_planes
    )


def bfd_peers(c: model.Cluster) -> str:
    """One BFD peer per control plane, 300 ms x 3: a silent control plane is dead in about a
    second (the spike measured 1.1 s against 8.8 s on BGP timers alone)."""
    return "\n".join(
        f"        peer {n.ip} {{\n"
        f"            interval {{\n"
        f'                multiplier "3"\n'
        f'                receive "300"\n'
        f'                transmit "300"\n'
        f"            }}\n"
        f"        }}"
        for n in c.control_planes
    )


def name(ctx: Context) -> str:
    return f"{ctx.cluster_name}-router"


def booted(ctx: Context) -> bool:
    """VyOS writes /tmp/vyos-config-status (0 = applied) when the boot-time config load ends;
    vyos-router.service is active long before that."""
    r = ctx.host.run(
        ["podman", "exec", name(ctx), "cat", "/tmp/vyos-config-status"],
        sudo=True,
        capture=True,
        check=False,
    )
    return r.returncode == 0 and r.stdout.strip() == "0"


def backends(ctx: Context) -> list[tuple[str, str]]:
    """(server, status) for every control plane in the api backend, from `show haproxy`."""
    r = ctx.host.run(
        ["podman", "exec", name(ctx), OP, "show", "haproxy"], sudo=True, capture=True, check=False
    )
    if r.returncode != 0:
        return []
    rows = (line.split() for line in r.stdout.splitlines())
    return [
        (f[1], f[2])
        for f in rows
        if len(f) > 2 and f[0] == "api" and f[1] not in ("FRONTEND", "BACKEND")
    ]


def _vtysh(ctx: Context, command: str) -> object | None:
    """One op-mode query as JSON, or None when the router or FRR does not answer."""
    r = ctx.host.run(
        ["podman", "exec", name(ctx), "vtysh", "-c", f"{command} json"],
        sudo=True,
        capture=True,
        check=False,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        data: object = json.loads(r.stdout)
    except ValueError:
        return None
    return data


def peers(ctx: Context, c: model.Cluster) -> list[tuple[str, str]]:
    """(control plane, BGP state) per neighbor in `show bgp ipv4 unicast summary`; a neighbor
    cluster.yaml does not have is listed by address."""
    data = _vtysh(ctx, "show bgp ipv4 unicast summary")
    if not isinstance(data, dict):
        return []
    by_ip = {str(n.ip): n.name for n in c.control_planes}
    return [
        (by_ip.get(ip, ip), str(p.get("state", "?")))
        for ip, p in data.get("peers", {}).items()
        if isinstance(p, dict)
    ]


def nexthops(ctx: Context, c: model.Cluster) -> int:
    """How many active next hops the router's selected route to the VIP has: the ECMP width
    that carries traffic, 0 without a route."""
    vip = c.spec.network.vip
    data = _vtysh(ctx, f"show ip route {vip}/32")
    entries = data.get(f"{vip}/32", []) if isinstance(data, dict) else []
    dicts = [e for e in entries if isinstance(e, dict)]
    chosen = next((e for e in dicts if e.get("selected")), dicts[0] if dicts else None)
    if not isinstance(chosen, dict):
        return 0
    hops = chosen.get("nexthops", [])
    return sum(1 for h in hops if isinstance(h, dict) and h.get("active")) if hops else 0


def bfd_down(ctx: Context, c: model.Cluster) -> list[str]:
    """The control planes whose BFD session with the router is not up."""
    data = _vtysh(ctx, "show bfd peers")
    status = {s.get("peer"): s.get("status") for s in data} if isinstance(data, list) else {}
    return [n.name for n in c.control_planes if status.get(str(n.ip)) != "up"]


def members(ctx: Context, c: model.Cluster) -> list[tuple[str, str]]:
    """What the router knows per control plane in this mode: backends or BGP peers."""
    return backends(ctx) if c.spec.network.ha == "proxy" else peers(ctx, c)


def kind(c: model.Cluster) -> str:
    return "backend" if c.spec.network.ha == "proxy" else "peer"


def show(c: model.Cluster) -> str:
    """The op-mode command a person runs to see the same list."""
    return f"{OP} show haproxy" if c.spec.network.ha == "proxy" else "vtysh -c 'show bgp summary'"


def synced(ctx: Context, c: model.Cluster) -> bool:
    """The router's backend (proxy) or peer (bgp) list is exactly the cluster's control planes."""
    return {n for n, _ in members(ctx, c)} == {n.name for n in c.control_planes}


def wait_booted(ctx: Context) -> None:
    """The boot-time config load holds VyOS for a few seconds after start; a load alongside it
    fails ("Failed to generate committed config") with exit status 0."""
    for i in range(1, 31):
        if booted(ctx):
            return
        if i == 30:
            raise PhaseFailed(
                f"{name(ctx)} did not finish booting within 60 s (podman logs {name(ctx)}; "
                f"podman exec {name(ctx)} cat /tmp/vyos-config-status)"
            )
        time.sleep(ctx.options.poll)


def wait_synced(ctx: Context, c: model.Cluster) -> None:
    """Every control plane listed as a backend (proxy) or neighbor (bgp), no other: the config
    took. The backends stay DOWN, the sessions short of Established, until kubeadm brings an
    apiserver up (and, in bgp mode, kubeadm/frr starts FRR in the control plane)."""
    for i in range(1, 31):
        if synced(ctx, c):
            got = len(c.control_planes)
            ctx.say(
                f"  {name(ctx)}: VyOS up, {got} control plane {kind(c)}(s) at "
                f"{c.spec.network.router}"
            )
            return
        if i == 30:
            listed = ", ".join(n for n, _ in members(ctx, c)) or "nothing"
            want = ", ".join(n.name for n in c.control_planes)
            raise PhaseFailed(
                f"{name(ctx)} lists {listed} as {kind(c)}s, cluster.yaml has {want} "
                f"(podman exec {name(ctx)} {show(c)})"
            )
        time.sleep(ctx.options.poll)


def wait(ctx: Context) -> None:
    wait_booted(ctx)
    wait_synced(ctx, ctx.require_cluster())


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


def reload(ctx: Context, c: model.Cluster) -> None:
    """tkctl.boot was re-rendered (a control plane came or went): it becomes config.boot with
    VyOS's version footer (so nothing is migrated), then VyOS loads it and commits the difference
    without dropping connections. `exit` in script-template only tears the session down, so the
    script carries load's status out itself; commit's is useless both ways (1 when nothing
    changed, 0 when it failed), so the backend list is the proof the change took. The session
    runs in the vyattacfg group, as VyOS requires of configuration scripts: what it leaves under
    /opt/vyatta/config must stay writable to that group, not become root's."""
    wait_booted(ctx)
    r = ctx.host.run(
        ["podman", "exec", "-i", name(ctx), "sg", "vyattacfg", "-c", "/bin/vbash -s"],
        sudo=True,
        input=RELOAD,
        capture=True,
        check=False,
    )
    out = r.stdout + r.stderr
    if r.returncode != 0 or "Commit failed" in out:
        raise PhaseFailed(f"{name(ctx)} did not accept config.boot: {out.strip()[-300:]}")
    wait_synced(ctx, c)
