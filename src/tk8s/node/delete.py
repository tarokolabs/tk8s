"""delete node: drain it, stop its unit, remove its container and volumes, re-render the units
without it, take it out of cluster.yaml. Like delete cluster, every phase's done() is "that
part is already gone", so a delete that stopped halfway is finished by running it again."""

from .. import model
from ..create import kubeadm, units
from ..create.kubeadm import kubectl_env
from ..delete import (
    container_exists,
    node_volumes,
    remove_containers,
    remove_volumes,
    volume_exists,
)
from ..lifecycle import ClusterError, save_cluster
from ..phases import Context, Phase
from .common import find, reload_lb, require_node, unit_file, without_node


def confirm_delete(ctx: Context) -> None:
    if ctx.options.assume_yes:
        return
    try:
        answer = input(f"Delete node {require_node(ctx)} from cluster {ctx.cluster_name}? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() != "y":
        raise ClusterError("aborted")


def node_object_exists(ctx: Context, name: str) -> bool:
    """Only a NotFound answer means the node is gone; a failed kubectl (the cluster is stopped,
    the apiserver is away) must not pass for "already drained"."""
    r = ctx.host.run(
        ["kubectl", "get", "node", name], env=kubectl_env(ctx), capture=True, check=False
    )
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
    r = ctx.host.run(
        ["kubectl", "drain", n, "--ignore-daemonsets", "--delete-emptydir-data", "--timeout=120s"],
        env=kubectl_env(ctx),
        capture=True,
        check=False,
    )
    if r.returncode != 0:
        why = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "unknown"
        ctx.say(f"  drain of {n} did not finish: {why}; removing it anyway")
    ctx.host.run(
        ["podman", "exec", n, "kubeadm", "reset", "-f"], sudo=True, capture=True, check=False
    )
    # A control plane's apiserver just went away; the LB keeps routing to it for a few seconds.
    kubeadm.wait_router_ready(ctx)
    ctx.host.run(["kubectl", "delete", "node", n], env=kubectl_env(ctx), capture=True)


DRAIN = Phase(
    "drain", "drain the node, kubeadm reset, delete the Node object", drain_done, drain_run
)


def stop_done(ctx: Context) -> bool:
    return not ctx.host.is_active(f"{require_node(ctx)}.service")


def stop_run(ctx: Context) -> None:
    ctx.host.run(["systemctl", "stop", f"{require_node(ctx)}.service"], sudo=True, check=False)


STOP = Phase("stop", "stop the node's unit", stop_done, stop_run)


def container_done(ctx: Context) -> bool:
    return not container_exists(ctx, require_node(ctx))


def container_run(ctx: Context) -> None:
    remove_containers(ctx, [require_node(ctx)])


CONTAINER = Phase("container", "remove the node container", container_done, container_run)


def volumes_done(ctx: Context) -> bool:
    return not any(volume_exists(ctx, v) for v in node_volumes(require_node(ctx)))


def volumes_run(ctx: Context) -> None:
    remove_volumes(ctx, node_volumes(require_node(ctx)))


VOLUMES = Phase("volumes", "remove the node's volumes", volumes_done, volumes_run)


def trimmed(ctx: Context) -> model.Cluster:
    """The cluster as it will be once the node is gone; entry writes it last, so an interrupted
    delete still finds the node in cluster.yaml and can be rerun."""
    c, n = ctx.require_cluster(), require_node(ctx)
    return without_node(c, n) if find(c, n) is not None else c


def units_done(ctx: Context) -> bool:
    return not ctx.host.exists(unit_file(ctx)) and units.done(ctx, trimmed(ctx))


def units_run(ctx: Context) -> None:
    """The target and haproxy.cfg are rendered without the node first, then its unit goes, then
    systemd and the LB learn about it."""
    n = require_node(ctx)
    units.run(ctx, trimmed(ctx))
    ctx.host.run(["rm", "-f", str(unit_file(ctx))], sudo=True)
    ctx.host.run(["systemctl", "daemon-reload"], sudo=True)
    ctx.host.run(
        ["systemctl", "reset-failed", f"{n}.service"], sudo=True, capture=True, check=False
    )
    reload_lb(ctx)


UNITS = Phase(
    "units",
    "drop the node's unit, re-render the target and the LB config, reload the LB",
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
