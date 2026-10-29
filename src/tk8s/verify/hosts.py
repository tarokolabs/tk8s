"""[hosts]: every host of a multi-host cluster answers over ssh, runs the cluster target and has
its macvlan shim up. Nothing to say on a single host."""

from ..create.units import shim_name
from ..host import HostError, Result
from .report import Report


def check_hosts(r: Report) -> None:
    c = r.ctx.require_cluster()
    if not c.multi_host:
        return
    shim = shim_name(c.name)
    trouble: list[str] = []
    for name, h in r.ctx.node_hosts():
        try:
            reach = h.run(["true"], capture=True, check=False)
        except HostError as err:
            reach = Result(err.returncode, "", str(err).rsplit(": ", 1)[-1])
        if reach.returncode != 0:
            why = reach.stderr.strip().splitlines()[-1] if reach.stderr.strip() else "no answer"
            trouble.append(f"{name}: unreachable ({why})")
            continue
        if not h.is_active(f"{c.name}.target"):
            trouble.append(f"{name}: target inactive")
        if h is r.ctx.host and c.spec.network.shim is not None:
            up = h.run(["ip", "-o", "link", "show", shim], capture=True, check=False)
            if up.returncode != 0:
                trouble.append(f"{name}: shim {shim} missing")
    n = len(r.ctx.node_hosts())
    if trouble:
        r.ng("hosts", "; ".join(trouble))
    elif c.spec.network.shim is not None:
        r.ok(f"hosts: {n}/{n} reachable, target active, shim {shim} up")
    else:
        r.ok(f"hosts: {n}/{n} reachable, target active")
