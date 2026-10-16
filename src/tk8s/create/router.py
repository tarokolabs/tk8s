"""router: the VyOS container of a cluster: its image, the server blocks of its tkctl.boot,
and how tkctl waits for it, reads its load balancer status and reloads its configuration."""

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


def synced(ctx: Context, c: model.Cluster) -> bool:
    """The router's backend list is exactly the cluster's control planes, by name."""
    return {n for n, _ in backends(ctx)} == {n.name for n in c.control_planes}


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
    """Every control plane listed as a backend, no other: the config took. The backends stay
    DOWN until kubeadm brings an apiserver up."""
    for i in range(1, 31):
        if synced(ctx, c):
            got = len(c.control_planes)
            ctx.say(
                f"  {name(ctx)}: VyOS up, {got} control plane backend(s) at {c.spec.network.router}"
            )
            return
        if i == 30:
            listed = ", ".join(n for n, _ in backends(ctx)) or "nothing"
            want = ", ".join(n.name for n in c.control_planes)
            raise PhaseFailed(
                f"{name(ctx)} lists {listed} as backends, cluster.yaml has {want} "
                f"(podman exec {name(ctx)} {OP} show haproxy)"
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
