"""What each tkctl verb does with its parsed flags: build the context, hand the work to the
module that owns it, print the closing lines."""

import argparse
import os
from pathlib import Path

from . import delete, images, lifecycle, model, verify, versions
from .args import (
    UsageError,
    normalize_memory,
    user_cluster_from_file,
    user_cluster_from_flags,
    validate_k8s,
    validate_name,
    validate_node_name,
)
from .create import DRY_RUN_PHASES, PHASES
from .create.plan import load_existing, resolve, used_indices
from .host import DryRunHost, Host
from .node import add as node_add
from .node import common as node_common
from .node import delete as node_delete
from .paths import Paths
from .phases import Context, Options, Runner, tk8s_version, tree

HOME = Path(__file__).resolve().parents[2]


# ---- create cluster


def cmd_create(a: argparse.Namespace, raw: list[str]) -> int:
    if a.list_phases:
        print("\n".join(tree(PHASES)))
        return 0
    if a.k8s:
        validate_k8s(a.k8s)
    if a.gvisor and a.datapath is None:
        a.datapath = "veth"
    flags = ("--control-planes", "--workers", "--cpu", "--memory", "--ha")
    topo = any(a == f or a.startswith(f + "=") for a in raw for f in flags)  # --ha bgp, --ha=bgp
    user = user_cluster_from_file(a, topo) if a.file else user_cluster_from_flags(a)
    ctx = make_context(dry_run=a.dry_run, quiet=a.quiet)
    name = user.metadata.name
    existing = load_existing(ctx.paths, name)
    if existing is not None and existing.status is not None and existing.status.phase == "Ready":
        raise UsageError(f"cluster {name} already exists; delete it first or pick another name")
    ctx.cluster = resolve(
        user,
        k8s_default=ctx.versions.kubernetes.default,
        existing=existing,
        used_indices=used_indices(ctx.paths),
        allow_overlap=a.allow_overlap,
        tk8s_version=tk8s_version(HOME),
    )
    phases = DRY_RUN_PHASES if a.dry_run else PHASES
    skip = {s for s in a.skip_phases.split(",") if s}
    Runner(phases, only=a.phase, skip=skip).run(ctx)
    if a.dry_run or a.phase:
        return 0
    c = ctx.require_cluster()
    if skip:
        print()
        print(
            f"cluster {c.name} is not marked Ready: skipped {','.join(sorted(skip))}; "
            f"rerun tkctl create cluster {c.name} to finish"
        )
        return 0
    if c.status is not None and c.status.phase != "Ready":
        status = c.status.model_copy(update={"phase": "Ready"})
        ctx.cluster = c.model_copy(update={"status": status})
        ctx.host.write(ctx.state_dir / "cluster.yaml", model.dumps(ctx.cluster))
    print()
    print(f"cluster {c.name} is ready")
    print(f"  kubeconfig: {ctx.state_dir}/kubeconfig   (tkctl use cluster {c.name})")
    print(f"  units:      /etc/containers/systemd/{c.name}/   target: {c.name}.target")
    print(f"  describe:   tkctl describe cluster {c.name}")
    return 0


# ---- verify cluster


def cmd_verify(a: argparse.Namespace) -> int:
    if a.list_phases:
        print("\n".join(tree(verify.PHASES)))
        return 0
    ctx = cluster_context(a.name, "verify", usage="usage: tkctl verify cluster NAME [--phase P]")
    return 1 if verify.run(ctx, only=a.phase) else 0


# ---- get clusters, describe, stop, start, use


def cluster_context(
    name: str | None, verb: str, *, quiet: bool = False, usage: str | None = None
) -> Context:
    """The context of an existing cluster, for the verbs that take `cluster NAME`."""
    if not name:
        raise UsageError(usage or f"usage: tkctl {verb} cluster NAME")
    ctx = make_context(quiet=quiet)
    lifecycle.load_cluster(ctx, validate_name(name))
    return ctx


def cmd_get_clusters() -> int:
    lifecycle.list_clusters(make_context(quiet=True))
    return 0


def cmd_cluster(verb: str, a: argparse.Namespace) -> int:
    looking = verb == "describe"  # read-only verbs print no `$` lines: nothing to redo
    ctx = cluster_context(a.name, verb, quiet=looking)
    c = ctx.require_cluster()
    if verb == "describe":
        lifecycle.describe(ctx, c, a.output)
    elif verb == "stop":
        lifecycle.stop(ctx, c)
    elif verb == "start":
        lifecycle.start(ctx, c)
    else:
        lifecycle.use(ctx, c)
    return 0


# ---- delete cluster


def cmd_delete_cluster(a: argparse.Namespace) -> int:
    if a.list_phases:
        print("\n".join(tree([delete.PHASE])))
        return 0
    if not a.name:
        raise UsageError("usage: tkctl delete cluster NAME [--yes]")
    name = validate_name(a.name)
    ctx = make_context(assume_yes=a.yes)
    ctx.name = name
    try:
        lifecycle.load_cluster(ctx, name)
    except lifecycle.ClusterError:
        pass  # no cluster.yaml: delete what the units and podman still know
    except model.ClusterFileError as err:
        print(f"  {err}; deleting what the units and podman still know")
    if not delete.found(ctx):
        raise lifecycle.ClusterError(
            f"cluster {name} not found: no state under {ctx.paths.clusters_dir}, no units, "
            "no network (tkctl get clusters)"
        )
    delete.confirm_delete(ctx)
    Runner([delete.PHASE], only=a.phase, record=False).run(ctx)
    if a.phase:
        return 0
    print(f"cluster {name} deleted")
    return 0


# ---- add node, join node, delete node


def running_cluster(ctx: Context, name: str) -> model.Cluster:
    """Node verbs talk to the apiserver and the LB: the cluster has to be Ready and up."""
    c = lifecycle.load_cluster(ctx, validate_name(name))
    if c.status is None or c.status.phase != "Ready":
        raise lifecycle.ClusterError(f"cluster {c.name} is not fully created (tkctl get clusters)")
    if lifecycle.state_of(ctx, c) != "Running":
        raise lifecycle.ClusterError(
            f"cluster {c.name} is not running (tkctl start cluster {c.name})"
        )
    return c


def cmd_add_node(a: argparse.Namespace) -> int:
    if not a.cluster:
        raise UsageError(
            "usage: tkctl add node CLUSTER --role worker|control-plane [--cpu N] [--memory SIZE] "
            "[--no-join]"
        )
    if a.role not in ("worker", "control-plane"):
        raise UsageError("--role must be worker or control-plane")
    if a.cpu is not None and a.cpu < 1:
        raise UsageError("--cpu must be a positive integer")
    memory = normalize_memory(a.memory) if a.memory else None
    ctx = make_context()
    c = running_cluster(ctx, a.cluster)
    ctx.request = model.NodeRequest(role=a.role, cpu=a.cpu, memory=memory)
    Runner([node_add.add_phase(join=not a.no_join)], record=False).run(ctx)
    print()
    if a.no_join:
        print(f"{ctx.node} left unjoined; join with: tkctl join node {c.name} {ctx.node}")
    else:
        print(f"node {ctx.node} added to cluster {c.name}")
    return 0


def cmd_join_node(a: argparse.Namespace) -> int:
    if not a.cluster or not a.node:
        raise UsageError("usage: tkctl join node CLUSTER NODE")
    validate_node_name(a.node)
    ctx = make_context()
    c = running_cluster(ctx, a.cluster)
    if node_common.find(c, a.node) is None:
        raise lifecycle.ClusterError(f"node {a.node} not found in cluster {c.name}")
    ctx.node = a.node
    Runner([node_add.JOIN_PHASE], record=False).run(ctx)
    print()
    print(f"node {a.node} joined cluster {c.name}")
    return 0


def cmd_delete_node(a: argparse.Namespace) -> int:
    if a.list_phases:
        print("\n".join(tree([node_delete.PHASE])))
        return 0
    if not a.cluster or not a.node:
        raise UsageError("usage: tkctl delete node CLUSTER NODE [--yes]")
    validate_node_name(a.node)
    ctx = make_context(assume_yes=a.yes)
    c = running_cluster(ctx, a.cluster)
    ctx.node = a.node
    # entry is the last phase: an interrupted delete still finds the node
    node_common.deletable(c, a.node)
    node_delete.confirm_delete(ctx)
    Runner([node_delete.PHASE], only=a.phase, record=False).run(ctx)
    if a.phase:
        return 0
    print(f"node {a.node} removed")
    return 0


# ---- images verbs


def cmd_images(verb: str, a: argparse.Namespace) -> int:
    if a.k8s:
        validate_k8s(a.k8s)
    if verb == "delete" and not a.k8s:
        raise UsageError("usage: tkctl delete images --k8s VERSION [--yes]")
    ctx = make_context(assume_yes=getattr(a, "yes", False), refresh=getattr(a, "refresh", False))
    k8s = a.k8s or ctx.versions.kubernetes.default
    if verb == "pull":
        images.ensure(ctx, k8s)
    elif verb == "get":
        print(f"{'STATUS':<8} IMAGE")
        for s, img in images.listing(ctx, k8s):
            print(f"{s:<8} {img}")
    else:
        images.prune(ctx, k8s)
    return 0


# ---- wiring


def make_context(
    *, dry_run: bool = False, quiet: bool = False, assume_yes: bool = False, refresh: bool = False
) -> Context:
    paths = Paths.from_env(HOME)
    opts = Options(
        dry_run=dry_run,
        offline=os.environ.get("TK_OFFLINE") == "1",
        assume_yes=assume_yes or os.environ.get("TK_ASSUME_YES") == "1",
        refresh=refresh,
        verify_timeout=int(os.environ.get("TK_VERIFY_TIMEOUT") or 180),
    )
    host: Host = DryRunHost(print, quiet=quiet) if dry_run else Host(print, quiet=quiet)
    return Context(
        host=host,
        paths=paths,
        versions=versions.load(paths.home / "versions.yaml"),
        options=opts,
        say=print,
    )
