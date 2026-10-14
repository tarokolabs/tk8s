"""What each tkctl verb does with its parsed flags: build the context, hand the work to the
module that owns it, print the closing lines."""

import argparse
import os
from pathlib import Path

from . import images, model, verify, versions
from .args import (
    UsageError,
    user_cluster_from_file,
    user_cluster_from_flags,
    validate_k8s,
    validate_name,
)
from .create import DRY_RUN_PHASES, PHASES
from .create.plan import load_existing, resolve, used_indices
from .host import DryRunHost, Host
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
    if a.gvisor and a.datapath == "netkit":
        raise UsageError(
            "--gvisor requires the veth datapath; gVisor sandboxes have no network under netkit "
            "(drop --datapath netkit)"
        )
    if a.gvisor and a.datapath is None:
        a.datapath = "veth"
    topo = any(f in raw for f in ("--control-planes", "--workers", "--cpu", "--memory"))
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
    if not a.name:
        raise UsageError("usage: tkctl verify cluster NAME [--phase P]")
    name = validate_name(a.name)
    ctx = make_context()
    state = ctx.paths.state_dir(name)
    if not (state / "cluster.yaml").exists():
        raise UsageError(
            f"cluster {name} not found under {ctx.paths.clusters_dir} (tkctl get clusters)"
        )
    marker = (state / ".create-complete").exists()
    ctx.cluster = model.load(state / "cluster.yaml", create_complete_marker=marker)
    return 1 if verify.run(ctx, only=a.phase) else 0


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
        for s, img in images.status(ctx, k8s):
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
        quiet=quiet,
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
