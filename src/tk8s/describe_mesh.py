"""describe's Mesh: section: the peers, the clustermesh-apiserver's address, every global
service with its local and remote backend counts (what a blue/green move is watched by), and
the entrypoints an external load balancer or DNS points at."""

from . import model
from .mesh import checks, services
from .phases import Context


def describe_mesh(ctx: Context, c: model.Cluster, state: str) -> None:
    ctx.say("Mesh:")
    net = c.spec.network
    if not checks.can_federate(c):
        ctx.say("  unavailable (created before mesh support; recreate to connect)")
        return
    peers = c.status.mesh if c.status is not None else []
    ctx.say(f"  peers:      {', '.join(peers) if peers else 'none'}")
    ctx.say(f"  endpoint:   {net.mesh} (clustermesh-apiserver)")
    up = state in ("Running", "Degraded")  # degraded: the apiserver usually still answers
    ctx.say("  global services:")
    if not up:
        ctx.say("    unavailable (cluster is not running)")
    elif not peers:
        ctx.say("    none (not connected)")
    else:
        table = services.backends(ctx)
        found = services.global_services(ctx)
        for svc in found:
            local, remote = services.counts(c, svc, table)
            ctx.say(f"    {svc.ns + '/' + svc.name:<24} local {local}  remote {remote}")
        if not found:
            ctx.say("    none (no Service annotated service.cilium.io/global)")
    ctx.say("  entrypoints:")
    if not up:
        ctx.say("    unavailable (cluster is not running)")
        return
    points = services.entrypoints(ctx)
    for kind, name, addr in points:
        ctx.say(f"    {kind:<8} {name:<16}{addr}")
    if not points:
        ctx.say("    none")
