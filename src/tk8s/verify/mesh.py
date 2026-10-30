"""[mesh]: every recorded peer still exists and lists this cluster back, cilium clustermesh
status is connected, and a pod here reaches a pod of every peer. Nothing to say when the
cluster is not connected."""

from .. import lifecycle
from ..mesh import ops, probe
from .report import Report


def check_mesh(r: Report) -> None:
    c = r.ctx.require_cluster()
    peers = c.status.mesh if c.status is not None else []
    if not peers:
        r.skip("mesh", "not connected to any cluster")
        return
    reachable = []
    for p in peers:
        pc = ops.peer_context(r.ctx, p)
        if pc is None:
            r.ng(
                f"mesh peer {p}",
                f"cluster.yaml not found (deleted? tkctl disconnect cluster {c.name} {p})",
            )
            continue
        peer = pc.require_cluster()
        if peer.status is None or c.name not in peer.status.mesh:
            r.ng(
                f"mesh peer {p}", f"{p} does not list {c.name} (tkctl connect cluster {c.name} {p})"
            )
            continue
        if lifecycle.state_of(pc, peer) == "Running":
            reachable.append(pc)
    st = r.ctx.host.run(
        ["cilium", "clustermesh", "status", "--wait", "--wait-duration", f"{r.timeout}s"],
        env=ops.kubeconfig_env([r.ctx]),
        capture=True,
        check=False,
    )
    out = r.ctx.log_path("verify-clustermesh.out")
    r.ctx.host.write(out, st.stdout + st.stderr)
    if st.returncode == 0:
        r.ok(f"clustermesh status: {len(peers)} peer(s) connected")
    else:
        r.ng("clustermesh status", f"see {out}")
    for pc in reachable:
        name = pc.require_cluster().name
        failure = probe.pod_ping(r.ctx, pc)
        if failure is None:
            r.ok(f"pods of {name} reachable")
        else:
            r.ng(f"pods of {name}", failure)
