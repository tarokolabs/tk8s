"""tkctl: kubectl-style verbs. Flags become a cluster definition, phases do the work."""

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

from . import images, model, verify, versions
from .create import DRY_RUN_PHASES, PHASES
from .create.plan import PlanError, load_existing, resolve, used_indices
from .datatree import PreflightError
from .host import DryRunHost, Host, HostError
from .paths import Paths
from .phases import Context, Options, PhaseFailed, Runner, tk8s_version, tree

HOME = Path(__file__).resolve().parents[2]

USAGE = """\
tkctl: create and manage tk8s Kubernetes clusters on a single podman host.

Usage:
  tkctl create cluster [NAME] [flags]
  tkctl create cluster --list-phases
  tkctl delete cluster NAME [--yes]
  tkctl get clusters
  tkctl describe cluster NAME [-o yaml]
  tkctl stop cluster NAME
  tkctl start cluster NAME
  tkctl use cluster NAME
  tkctl verify cluster NAME [--phase P] [--list-phases]
  tkctl add node CLUSTER --role worker|control-plane [--cpu N] [--memory SIZE] [--no-join]
  tkctl delete node CLUSTER NODE [--yes]
  tkctl join node CLUSTER NODE
  tkctl pull images [--k8s VERSION] [--refresh]
                         (--refresh: pull the node image again even when its tag is present)
  tkctl get images [--k8s VERSION]
  tkctl delete images --k8s VERSION [--yes]
  tkctl version

Create flags:
  --control-planes N     control-plane nodes (default 1; odd, at most 5; every cluster has an
                         apiserver LB)
  --workers N            worker nodes (default 2)
  --cpu N                CPUs per node (default 2)
  --memory SIZE          memory per node, e.g. 4G or 4096M (default 4G)
  --k8s VERSION          Kubernetes version (default from versions.yaml)
  --gvisor               install the gVisor RuntimeClass (forces --datapath veth)
  --datapath MODE        cilium datapath: auto | netkit | veth (default auto)
  --defer-join           create the extra control planes but do not join them (HA exercise);
                         join later with: tkctl join node CLUSTER NODE
  -f FILE                cluster definition file; cannot be combined with topology flags
  --allow-overlap        allow subnets that overlap an existing cluster
  --dry-run              print the resolved cluster.yaml and generated files without changing
                         the host
  --phase P              run one phase (or one subtree, e.g. kubeadm) instead of all of them
  --skip-phases P,...    phases to skip
  --quiet                hide the `$ command` lines (the [phase] lines always show)

NAME must match ^[a-z][a-z0-9-]{0,15}$ (it becomes the DNS domain NAME.k8s).

Environment:
  TK_DATA_DIR            where clusters, CNI plugins and materials live (default /opt/taroko)
  TK_WULIN_DIR           teaching materials mounted into every node (default TK_DATA_DIR/wulin)
  TK_ASSUME_YES=1        skip confirmation prompts (same as --yes)
  TK_OFFLINE=1           never download: create and pull images fail on a missing image instead
                         of pulling
  TK_VERIFY_TIMEOUT      seconds to wait for each verify check (default 180)
"""

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
K8S_RE = re.compile(r"^1\.[0-9]+\.[0-9]+$")
KNOWN_VERBS = {
    "create",
    "delete",
    "get",
    "describe",
    "stop",
    "start",
    "use",
    "verify",
    "add",
    "join",
    "pull",
    "version",
}
NOT_YET = {
    "delete cluster",
    "get clusters",
    "describe",
    "stop",
    "start",
    "use",
    "add",
    "join",
}


class UsageError(Exception):
    pass


def validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise UsageError(
            f"invalid cluster name '{name}': use lowercase letters, digits and '-', "
            "start with a letter, max 16 characters"
        )
    return name


def normalize_memory(m: str) -> str:
    if not re.fullmatch(r"[0-9]+[GgMm]", m):
        raise UsageError(
            f"invalid --memory '{m}': use a number followed by G or M, e.g. 4G or 4096M"
        )
    return m.lower()


def validate_k8s(v: str) -> str:
    if not K8S_RE.match(v):
        raise UsageError("--k8s must look like 1.37.0")
    return v


# ---- plugins (kubectl's rule: tkctl-<a>-<b> before tkctl-<a>, longest match; built-ins win)


def plugins_on_path() -> list[str]:
    found: set[str] = set()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        try:
            entries = list(Path(d).iterdir())
        except OSError:
            continue
        for f in entries:
            if f.name.startswith("tkctl-") and f.is_file() and os.access(f, os.X_OK):
                found.add(f.name[len("tkctl-") :])
    return sorted(found)


def plugin_argv(argv: list[str]) -> list[str] | None:
    """The plugin to hand over to, with its arguments, or None when no plugin matches."""
    words: list[str] = []
    for a in argv:
        if a.startswith("-"):
            break
        words.append(a)
    for n in range(len(words), 0, -1):
        path = shutil.which("tkctl-" + "-".join(words[:n]))
        if path:
            return [path, *argv[n:]]
    return None


# ---- create cluster


def add_create_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("name", nargs="?")
    p.add_argument("--control-planes", type=int, default=1)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--cpu", type=int, default=2)
    p.add_argument("--memory", default="4G")
    p.add_argument("--k8s")
    p.add_argument("--gvisor", action="store_true")
    p.add_argument("--datapath", choices=["auto", "netkit", "veth"])
    p.add_argument("--defer-join", action="store_true")
    p.add_argument("-f", dest="file")
    p.add_argument("--allow-overlap", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--phase")
    p.add_argument("--skip-phases", default="")
    p.add_argument("--list-phases", action="store_true")
    p.add_argument("--quiet", action="store_true")


def user_cluster_from_flags(a: argparse.Namespace) -> model.UserCluster:
    if a.control_planes < 1:
        raise UsageError("--control-planes must be a positive integer")
    if a.control_planes % 2 == 0:
        raise UsageError("--control-planes must be odd (1, 3, 5) for etcd quorum")
    if a.control_planes > 5:
        raise UsageError(
            "--control-planes must be at most 5 (the apiserver LB has five backend slots)"
        )
    if a.defer_join and a.control_planes == 1:
        raise UsageError("--defer-join needs --control-planes 3 or more")
    if a.workers < 0:
        raise UsageError("--workers must be an integer")
    if a.cpu < 1:
        raise UsageError("--cpu must be a positive integer")
    memory = normalize_memory(a.memory)
    groups = [model.NodeGroup(role="control-plane", cpu=a.cpu, memory=memory)]
    if a.control_planes > 1:
        groups.append(
            model.NodeGroup(
                role="control-plane",
                count=a.control_planes - 1,
                cpu=a.cpu,
                memory=memory,
                join=not a.defer_join,
            )
        )
    groups.append(model.NodeGroup(role="worker", count=a.workers, cpu=a.cpu, memory=memory))
    spec = model.UserSpec(kubernetes=a.k8s, gvisor=a.gvisor, datapath=a.datapath, nodes=groups)
    name = validate_name(a.name or "tk8s")
    return model.UserCluster(metadata=model.Metadata(name=name), spec=spec)


def user_cluster_from_file(a: argparse.Namespace, topo_flags: bool) -> model.UserCluster:
    if topo_flags:
        raise UsageError(
            "-f cannot be combined with --control-planes/--workers/--cpu/--memory; "
            "put them in the file"
        )
    path = Path(a.file)
    if not path.is_file():
        raise UsageError(f"file not found: {path}")
    if a.name:
        raise UsageError(f"the cluster name comes from metadata.name in {path}")
    u = model.load_user_file(path)
    validate_name(u.metadata.name)
    spec = u.spec
    if a.k8s:
        spec = spec.model_copy(update={"kubernetes": a.k8s})
    if a.gvisor:
        spec = spec.model_copy(update={"gvisor": True, "datapath": "veth"})
    return u.model_copy(update={"spec": spec})


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


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tkctl", add_help=False)
    sub = ap.add_subparsers(dest="verb")
    create = sub.add_parser("create", add_help=False).add_subparsers(dest="noun")
    add_create_flags(create.add_parser("cluster", add_help=False))
    v = sub.add_parser("verify", add_help=False).add_subparsers(dest="noun")
    vc = v.add_parser("cluster", add_help=False)
    vc.add_argument("name", nargs="?")
    vc.add_argument("--phase")
    vc.add_argument("--list-phases", action="store_true")
    for verb in ("pull", "get", "delete"):
        nouns = sub.add_parser(verb, add_help=False).add_subparsers(dest="noun")
        p = nouns.add_parser("images", add_help=False)
        p.add_argument("--k8s")
        if verb == "pull":
            p.add_argument("--refresh", action="store_true")
        if verb == "delete":
            p.add_argument("--yes", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    # Every [phase] and `$` line lands in a redirected log as it happens, not when the buffer fills.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    try:
        return _main(args)
    except UsageError as err:
        print(f"tkctl: {err}", file=sys.stderr)
        return 1
    except (
        model.ClusterFileError,
        PlanError,
        PhaseFailed,
        images.ImagesError,
        HostError,
        PreflightError,
        verify.VerifyError,
    ) as err:
        print(f"tkctl: {err}", file=sys.stderr)
        return 1


def _usage_of(verb: str) -> str:
    return {
        "pull": "usage: tkctl pull images [--k8s VERSION] [--refresh]",
        "get": "usage: tkctl get clusters | tkctl get images [--k8s VERSION]",
        "delete": "usage: tkctl delete cluster NAME [--yes] | "
        "tkctl delete images --k8s VERSION [--yes]",
    }[verb]


def _main(args: list[str]) -> int:
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE, end="")
        plugins = plugins_on_path()
        if plugins:
            print(f"Plugins found on PATH: {' '.join(plugins)}   (tkctl <plugin> ...)")
        return 0 if args else 1
    verb = args[0]
    if verb == "version":
        print(f"tkctl {tk8s_version(HOME)}")
        print(f"kubernetes default {versions.load(HOME / 'versions.yaml').kubernetes.default}")
        return 0
    if verb not in KNOWN_VERBS:
        plugin = plugin_argv(args)
        if plugin is not None:  # the plugin replaces tkctl, with the caller's whole environment
            os.execvp(plugin[0], plugin)
        raise UsageError(f"unknown command '{verb}' (see tkctl --help)")
    noun = args[1] if len(args) > 1 else ""
    if verb in ("pull", "get", "delete") and noun != "images":
        if f"{verb} {noun}" in NOT_YET or verb in NOT_YET:
            raise UsageError(f"{verb} {noun} is not implemented yet on this branch")
        raise UsageError(_usage_of(verb))
    if verb in NOT_YET:
        raise UsageError(f"{verb} is not implemented yet on this branch")
    try:
        a = build_parser().parse_args(args)
    except SystemExit as err:  # argparse already printed "unrecognized arguments: ..."
        return int(err.code or 1)
    if a.verb == "create" and a.noun == "cluster":
        return cmd_create(a, args)
    if a.verb == "verify" and a.noun == "cluster":
        return cmd_verify(a)
    if a.noun == "images":
        return cmd_images(a.verb, a)
    raise UsageError(f"unknown command '{' '.join(args[:2])}' (see tkctl --help)")
