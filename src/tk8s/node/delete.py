"""delete node: drain it, stop its unit, remove its container and volumes, re-render the units
without it, take it out of cluster.yaml. Like delete cluster, every phase's done() is "that
part is already gone", so a delete that stopped halfway is finished by running it again."""

from pathlib import Path

from .. import model, podman
from ..create import kubeadm, nodefs, router, units
from ..delete import node_volumes_gone, remove_containers, remove_node_volumes
from ..host import Host
from ..lifecycle import ClusterError, save_cluster
from ..phases import Context, Phase, PhaseFailed, confirm, wait_for
from .common import find, require_node, unit_file, without_node


def confirm_delete(ctx: Context) -> None:
    confirm(
        ctx,
        f"Delete node {require_node(ctx)} from cluster {ctx.cluster_name}? [y/N] ",
        ClusterError,
    )


def node_object_exists(ctx: Context, name: str) -> bool:
    """Only a NotFound answer means the node is gone; a failed kubectl (the cluster is stopped,
    the apiserver is away) must not pass for "already drained"."""
    r = kubeadm.kubectl(ctx, "get", "node", name)
    if r.returncode == 0:
        return True
    if "NotFound" in r.stderr or "not found" in r.stderr:
        return False
    why = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "no answer"
    raise ClusterError(
        f"cannot reach the apiserver of cluster {ctx.cluster_name}: {why} (tkctl get clusters)"
    )


def drain_done(ctx: Context) -> bool:
    return not node_object_exists(ctx, require_node(ctx))


def drain_run(ctx: Context) -> None:
    """Workloads move off first; kubeadm reset clears the node's own state; then the Node object
    goes. A drain that cannot finish (a PodDisruptionBudget) is reported, not fatal: the node is
    going away either way."""
    n = require_node(ctx)
    r = kubeadm.kubectl(
        ctx, "drain", n, "--ignore-daemonsets", "--delete-emptydir-data", "--timeout=120s"
    )
    if r.returncode != 0:
        why = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "unknown"
        ctx.say(f"  drain of {n} did not finish: {why}; removing it anyway")
    podman.exec_(ctx, n, "kubeadm", "reset", "-f")
    node = find(ctx.require_cluster(), n)
    if node is not None and node.role == "control-plane":
        _wait_router_forgot(ctx, node)
    kubeadm.wait_router_ready(ctx)
    r = kubeadm.kubectl(ctx, "delete", "node", n)
    if r.returncode != 0:
        raise PhaseFailed(f"kubectl delete node {n} failed: {r.stderr.strip()}")


def _wait_router_forgot(ctx: Context, n: model.Node) -> None:
    """kubeadm reset took this control plane's apiserver down, but the router keeps sending it
    connections for a few seconds (haproxy's check in proxy mode, the health timer's withdrawal
    in bgp mode); a kubectl delete node in that window can land on the dead one. Every running
    router has to let go (a stopped one has no say and gets the new config at its next boot)."""
    c = ctx.require_cluster()
    last: list[str] = []

    def forgotten() -> bool:
        last[:] = router.any_routes_to(ctx, c, n)
        return not last

    def still() -> str:
        verb = "sends" if len(last) == 1 else "send"
        return (
            f"{', '.join(last)} still {verb} apiserver connections to {n.name} after 30 s "
            f"(podman exec {last[0]} {router.show_path(c)})"
        )

    wait_for(ctx, forgotten, rounds=16, delay=ctx.options.poll, failure=still)


DRAIN = Phase(
    "drain", "drain the node, kubeadm reset, delete the Node object", drain_done, drain_run
)


def _host(ctx: Context) -> Host:
    """The host the node runs on (the operator on a single-host cluster)."""
    return ctx.host_for(require_node(ctx))


def stop_done(ctx: Context) -> bool:
    return not _host(ctx).is_active(f"{require_node(ctx)}.service")


def stop_run(ctx: Context) -> None:
    _host(ctx).run(["systemctl", "stop", f"{require_node(ctx)}.service"], sudo=True, check=False)


STOP = Phase("stop", "stop the node's unit", stop_done, stop_run)


def container_done(ctx: Context) -> bool:
    return not podman.exists(ctx, "container", require_node(ctx))


def container_run(ctx: Context) -> None:
    remove_containers(ctx, [require_node(ctx)])


CONTAINER = Phase("container", "remove the node container", container_done, container_run)


def volumes_done(ctx: Context) -> bool:
    return node_volumes_gone(ctx, require_node(ctx))


def volumes_run(ctx: Context) -> None:
    remove_node_volumes(ctx, require_node(ctx))


VOLUMES = Phase("volumes", "remove the node's volumes", volumes_done, volumes_run)


def trimmed(ctx: Context) -> model.Cluster:
    """The cluster as it will be once the node is gone; entry writes it last, so an interrupted
    delete still finds the node in cluster.yaml and can be rerun."""
    c, n = ctx.require_cluster(), require_node(ctx)
    return without_node(c, n) if find(c, n) is not None else c


def node_dir(ctx: Context) -> Path:
    """The node's meminfo, NUMA node and kubelet drop-in under <state>/nodes/."""
    return nodefs.node_dir(ctx.paths.state_dir(ctx.cluster_name), require_node(ctx))


def units_done(ctx: Context) -> bool:
    c = trimmed(ctx)
    host = _host(ctx)
    return (
        not host.exists(unit_file(ctx))
        and not host.exists(node_dir(ctx))
        and units.done(ctx, c)
        and router.all_synced(ctx, c)
    )


def units_run(ctx: Context) -> None:
    """The target and config.boot are rendered without the node first, then its unit goes (on
    its host), then systemd there and the router learn about it."""
    n = require_node(ctx)
    host = _host(ctx)
    units.run(ctx, trimmed(ctx))
    host.run(["rm", "-f", str(unit_file(ctx))], sudo=True)
    host.run(["rm", "-rf", str(node_dir(ctx))])
    host.run(["systemctl", "daemon-reload"], sudo=True)
    host.run(["systemctl", "reset-failed", f"{n}.service"], sudo=True, capture=True, check=False)
    router.reload_all(ctx, trimmed(ctx))


UNITS = Phase(
    "units",
    "drop the node's unit, re-render the target and the router config, reload the router",
    units_done,
    units_run,
)


def entry_done(ctx: Context) -> bool:
    return find(ctx.require_cluster(), require_node(ctx)) is None


def entry_run(ctx: Context) -> None:
    save_cluster(ctx, without_node(ctx.require_cluster(), require_node(ctx)))


ENTRY = Phase("entry", "remove the node from cluster.yaml", entry_done, entry_run)

PHASE = Phase(
    "delete",
    "remove a node from the cluster",
    None,
    lambda ctx: None,
    children=(DRAIN, STOP, CONTAINER, VOLUMES, UNITS, ENTRY),
)
