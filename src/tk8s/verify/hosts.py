"""[hosts]: every host of a multi-host cluster answers over ssh, runs the cluster target and has
its macvlan shim up. Nothing to say on a single host."""

from ..create.units import shim_name
from .report import Report


def check_hosts(r: Report) -> None:
    c = r.ctx.require_cluster()
    if not c.multi_host:
        return
    shim = shim_name(c.name)
    trouble: list[str] = []
    for name, h in r.ctx.node_hosts():
        reach = h.run(["true"], capture=True, check=False)
        if reach.returncode != 0:
            why = reach.stderr.strip().splitlines()[-1] if reach.stderr.strip() else "no answer"
            trouble.append(f"{name}: unreachable ({why})")
            continue
        if not h.is_active(f"{c.name}.target"):
            trouble.append(f"{name}: target inactive")
        if h.run(["ip", "-o", "link", "show", shim], capture=True, check=False).returncode != 0:
            trouble.append(f"{name}: shim {shim} missing")
    n = len(r.ctx.node_hosts())
    if trouble:
        r.ng("hosts", "; ".join(trouble))
    else:
        r.ok(f"hosts: {n}/{n} reachable, target active, shim {shim} up")
