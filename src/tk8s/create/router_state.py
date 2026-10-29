"""What a router container is doing right now, as podman and VyOS's op mode report it: whether
the container runs at all, and which VRRP state it holds. Kept apart from router.py, which
owns the configuration side (config.boot, reload, the backend and peer lists)."""

from .. import model, podman
from ..phases import Context

OP = "/opt/vyatta/bin/vyatta-op-cmd-wrapper"


def running(ctx: Context, r: model.Router) -> bool:
    """podman's own word: a stopped router (systemctl stop) is not reloaded and not waited for;
    `podman container exists` is not enough, a stopped container exists too."""
    res = ctx.host_for(r.name).run(
        ["podman", "container", "inspect", "--format", "{{.State.Running}}", r.name],
        sudo=True,
        capture=True,
        check=False,
    )
    return res.returncode == 0 and res.stdout.strip() == "true"


def vrrp(ctx: Context, r: model.Router) -> str | None:
    """The State column (MASTER, BACKUP, FAULT) of VRRP group tk8s in `show vrrp`, or None when
    the router does not answer or runs no VRRP ("VRRP data is not available ...")."""
    res = podman.exec_(ctx, r.name, OP, "show", "vrrp")
    if res.returncode != 0:
        return None
    for line in res.stdout.splitlines():
        f = line.split()
        if len(f) >= 4 and f[0] == "tk8s":
            return f[3]
    return None
