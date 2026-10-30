"""connect and disconnect, with the pinned cilium-cli: enable the clustermesh-apiserver in every
cluster, connect them all at once (mesh mode, each cluster keeps its own CA), wait, probe, and
record the peers in every cluster.yaml. Every cilium command runs on the operator with a
KUBECONFIG that names the clusters' own files; the context names are the cluster names."""

from collections.abc import Callable
from pathlib import Path

from .. import images, lifecycle
from ..create.kubeadm import kubectl
from ..phases import Context, poll_until
from . import MeshError, checks, probe

STATUS_WAIT = "5m"


def _name(ctx: Context) -> str:
    return ctx.require_cluster().name


def kubeconfig_env(ctxs: list[Context]) -> dict[str, str]:
    """KUBECONFIG naming every cluster's file (client-go merges a colon-separated list)."""
    return {"KUBECONFIG": ":".join(str(c.state_dir / "kubeconfig") for c in ctxs)}


def members(ctx_of: Callable[[str], Context], names: list[str]) -> list[Context]:
    """The clusters a connect works on: the ones named and every peer they already have, so the
    mesh stays a full graph; sorted by name. A recorded peer that cannot be loaded is refused."""
    found: dict[str, Context] = {}
    todo = list(names)
    while todo:
        name = todo.pop(0)
        if name in found:
            continue
        found[name] = ctx_of(name)
        c = found[name].require_cluster()
        for peer in c.status.mesh if c.status is not None else []:
            if peer in found:
                continue
            try:
                found[peer] = ctx_of(peer)
            except lifecycle.ClusterNotFound as err:
                raise MeshError(
                    f"cluster {peer} (a peer of {name}) not found; disconnect it first: "
                    f"tkctl disconnect cluster {name} {peer}"
                ) from err
            todo.append(peer)
    return [found[n] for n in sorted(found)]


def apiserver_ip(ctx: Context) -> str:
    r = kubectl(
        ctx,
        "-n",
        "kube-system",
        "get",
        "svc",
        "clustermesh-apiserver",
        "-o",
        "jsonpath={.status.loadBalancer.ingress[0].ip}",
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def enabled(ctx: Context) -> bool:
    """The clustermesh-apiserver Service holds the address reserved for it."""
    return apiserver_ip(ctx) == str(ctx.require_cluster().spec.network.mesh)


def enable(ctx: Context, op: Context | None = None) -> None:
    """cilium clustermesh enable, run on the operator (op, ctx itself by default), then wait for
    the apiserver Service to take the reserved address."""
    c = ctx.require_cluster()
    r = (op or ctx).host.run(
        ["cilium", "clustermesh", "enable", "--context", c.name, "--service-type", "LoadBalancer"],
        env=kubeconfig_env([ctx]),
        capture=True,
        check=False,
    )
    if r.returncode != 0:
        tail = (r.stderr.strip() or r.stdout.strip()).splitlines()[-1:]
        raise MeshError(f"cluster {c.name}: cilium clustermesh enable failed: {' '.join(tail)}")
    if not poll_until(lambda: enabled(ctx), rounds=60, delay=ctx.options.poll):
        raise MeshError(
            f"cluster {c.name}: clustermesh-apiserver did not get {c.spec.network.mesh} within "
            "2 minutes (kubectl -n kube-system get svc clustermesh-apiserver)"
        )


def wait_status(ctx: Context, op: Context | None = None) -> None:
    """cilium clustermesh status --wait, run on the operator; its output lands in
    /tmp/<cluster>-clustermesh.out."""
    c = ctx.require_cluster()
    r = (op or ctx).host.run(
        ["cilium", "clustermesh", "status", "--wait", "--wait-duration", STATUS_WAIT],
        env=kubeconfig_env([ctx]),
        capture=True,
        check=False,
    )
    out = Path(f"/tmp/{c.name}-clustermesh.out")
    (op or ctx).host.write(out, r.stdout + r.stderr)
    if r.returncode != 0:
        raise MeshError(f"cluster {c.name}: clustermesh not ready within 5 minutes (see {out})")


def record_peers(ctx: Context, peers: list[str]) -> None:
    c = ctx.require_cluster()
    assert c.status is not None
    status = c.status.model_copy(update={"mesh": sorted(peers)})
    ctx.save_cluster(c.model_copy(update={"status": status}))


def connect(ctxs: list[Context]) -> None:
    """Every cluster enabled, one connect over all of them, status waited for, every pair
    probed, peers recorded; probe failures are printed and fail the verb after the recording
    (the mesh is up, the fix is in the network, disconnect undoes it)."""
    checks.preflight(ctxs)
    op = ctxs[0]  # the operator: every cilium command runs here, whatever the cluster
    images.ensure_cilium_cli(op)
    for ctx in ctxs:
        if enabled(ctx):
            op.say(f"  {_name(ctx)}: already enabled")
        else:
            enable(ctx, op)
            at = ctx.require_cluster().spec.network.mesh
            op.say(f"  {_name(ctx)}: clustermesh-apiserver at {at}")
    first, rest = ctxs[0], ctxs[1:]
    r = op.host.run(
        [
            "cilium",
            "clustermesh",
            "connect",
            "--context",
            _name(first),
            "--destination-context",
            ",".join(_name(c) for c in rest),
            "--connection-mode",
            "mesh",
            "--allow-mismatching-ca",
        ],
        env=kubeconfig_env(ctxs),
        capture=True,
        check=False,
    )
    if r.returncode != 0:
        tail = (r.stderr.strip() or r.stdout.strip()).splitlines()[-1:]
        raise MeshError(f"cilium clustermesh connect failed: {' '.join(tail)}")
    for ctx in ctxs:
        wait_status(ctx, op)
    failures: list[str] = []
    for i, a in enumerate(ctxs):
        for b in ctxs[i + 1 :]:
            failures += probe.probe_pair(a, b, rounds=12, delay=op.options.poll_slow)
    for f in failures:
        op.say(f"  FAIL  {f}")
    names = [_name(c) for c in ctxs]
    for ctx in ctxs:
        record_peers(ctx, [n for n in names if n != _name(ctx)])
        op.say(f"  {_name(ctx)}: peers {', '.join(n for n in names if n != _name(ctx))}")
    if failures:
        undo = f"tkctl disconnect cluster {names[0]} {names[1]}"
        raise MeshError(
            f"{len(failures)} probe(s) failed; the mesh is up but traffic is not, see above "
            f"(undo with {undo})"
        )
    op.say(f"clusters {', '.join(names)} connected")
