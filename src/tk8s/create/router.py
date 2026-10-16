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


def wait(ctx: Context) -> None:
    """Booted, then every control plane listed as a backend: the config was good. The backends
    stay DOWN until kubeadm brings an apiserver up."""
    c = ctx.require_cluster()
    for i in range(1, 31):
        if booted(ctx):
            break
        if i == 30:
            raise PhaseFailed(
                f"{name(ctx)} did not finish booting within 60 s (podman logs {name(ctx)}; "
                f"podman exec {name(ctx)} cat /tmp/vyos-config-status)"
            )
        time.sleep(ctx.options.poll)
    want = len(c.control_planes)
    for i in range(1, 31):
        got = len(backends(ctx))
        if got >= want:
            ctx.say(
                f"  {name(ctx)}: VyOS up, {got} control plane backend(s) at {c.spec.network.router}"
            )
            return
        if i == 30:
            raise PhaseFailed(
                f"{name(ctx)} lists {got} of {want} control planes within 60 s "
                f"(podman exec {name(ctx)} {OP} show haproxy)"
            )
        time.sleep(ctx.options.poll)


RELOAD = (
    "cp /opt/vyatta/etc/config/tkctl.boot /opt/vyatta/etc/config/config.boot\n"
    "grep '^// ' /opt/vyatta/etc/config.boot.default >> /opt/vyatta/etc/config/config.boot\n"
    "source /opt/vyatta/etc/functions/script-template\n"
    "configure\n"
    "load /opt/vyatta/etc/config/config.boot\n"
    "commit\n"
    "exit\n"
)


def reload(ctx: Context) -> None:
    """tkctl.boot was re-rendered (a control plane came or went); it becomes config.boot with
    VyOS's version footer (so nothing is migrated), then VyOS loads it and commits the difference
    without dropping connections. The boot-time commit holds a lock for a few seconds after
    start: retry through it."""
    for attempt in range(1, 4):
        r = ctx.host.run(
            ["podman", "exec", "-i", name(ctx), "/bin/vbash", "-s"],
            sudo=True,
            input=RELOAD,
            capture=True,
            check=False,
        )
        out = r.stdout + r.stderr
        if r.returncode != 0:
            raise PhaseFailed(f"{name(ctx)} did not accept config.boot: {out.strip()[-300:]}")
        if "temporarily locked" not in out:
            return
        if attempt == 3:
            raise PhaseFailed(f"{name(ctx)} kept the configuration locked through three attempts")
        time.sleep(ctx.options.poll)
