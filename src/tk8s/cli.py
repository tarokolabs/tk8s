"""tkctl: kubectl-style verbs. Flags become a cluster definition, phases do the work."""

import argparse
import os
import shutil
import sys
from pathlib import Path

from . import images, lifecycle, model, verify, versions
from .args import UsageError, add_create_flags
from .create.plan import PlanError
from .datatree import PreflightError
from .host import HostError
from .phases import PhaseFailed, tk8s_version
from .verbs import (
    HOME,
    cmd_add_node,
    cmd_cluster,
    cmd_create,
    cmd_delete_cluster,
    cmd_delete_node,
    cmd_get_clusters,
    cmd_images,
    cmd_join_node,
    cmd_verify,
)

USAGE = """\
tkctl: create and manage tk8s Kubernetes clusters on a single podman host.

Usage:
  tkctl create cluster [NAME] [flags]
  tkctl create cluster --list-phases
  tkctl delete cluster NAME [--yes] [--phase P] [--list-phases]
  tkctl get clusters
  tkctl describe cluster NAME [-o yaml]
  tkctl stop cluster NAME
  tkctl start cluster NAME
  tkctl use cluster NAME
  tkctl verify cluster NAME [--phase P] [--list-phases]
  tkctl add node CLUSTER --role worker|control-plane [--cpu N] [--memory SIZE] [--no-join]
  tkctl delete node CLUSTER NODE [--yes] [--phase P]
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
NOUNS = {
    "create": {"cluster"},
    "verify": {"cluster"},
    "pull": {"images"},
    "get": {"clusters", "images"},
    "delete": {"cluster", "node", "images"},
    "describe": {"cluster"},
    "stop": {"cluster"},
    "start": {"cluster"},
    "use": {"cluster"},
    "add": {"node"},
    "join": {"node"},
}


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
        if verb == "get":
            nouns.add_parser("clusters", add_help=False)
        if verb == "delete":
            dc = nouns.add_parser("cluster", add_help=False)
            dc.add_argument("name", nargs="?")
            dc.add_argument("--yes", action="store_true")
            dc.add_argument("--phase")
            dc.add_argument("--list-phases", action="store_true")
            dn = nouns.add_parser("node", add_help=False)
            dn.add_argument("cluster", nargs="?")
            dn.add_argument("node", nargs="?")
            dn.add_argument("--yes", action="store_true")
            dn.add_argument("--phase")
    an = (
        sub.add_parser("add", add_help=False)
        .add_subparsers(dest="noun")
        .add_parser("node", add_help=False)
    )
    an.add_argument("cluster", nargs="?")
    an.add_argument("--role")
    an.add_argument("--cpu", type=int)
    an.add_argument("--memory")
    an.add_argument("--no-join", action="store_true")
    jn = (
        sub.add_parser("join", add_help=False)
        .add_subparsers(dest="noun")
        .add_parser("node", add_help=False)
    )
    jn.add_argument("cluster", nargs="?")
    jn.add_argument("node", nargs="?")
    for verb in ("describe", "stop", "start", "use"):
        nouns = sub.add_parser(verb, add_help=False).add_subparsers(dest="noun")
        p = nouns.add_parser("cluster", add_help=False)
        p.add_argument("name", nargs="?")
        if verb == "describe":
            p.add_argument("-o", dest="output", choices=["yaml"])
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
        lifecycle.ClusterError,
    ) as err:
        print(f"tkctl: {err}", file=sys.stderr)
        return 1


def _usage_of(verb: str) -> str:
    return {
        "pull": "usage: tkctl pull images [--k8s VERSION] [--refresh]",
        "get": "usage: tkctl get clusters | tkctl get images [--k8s VERSION]",
        "delete": "usage: tkctl delete cluster NAME [--yes] | tkctl delete node CLUSTER NODE "
        "[--yes] | tkctl delete images --k8s VERSION [--yes]",
        "create": "usage: tkctl create cluster [NAME] [flags]",
        "verify": "usage: tkctl verify cluster NAME [--phase P]",
        "describe": "usage: tkctl describe cluster NAME [-o yaml]",
        "stop": "usage: tkctl stop cluster NAME",
        "start": "usage: tkctl start cluster NAME",
        "use": "usage: tkctl use cluster NAME",
        "add": "usage: tkctl add node CLUSTER --role worker|control-plane "
        "[--cpu N] [--memory SIZE] [--no-join]",
        "join": "usage: tkctl join node CLUSTER NODE",
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
    if noun not in NOUNS[verb]:
        raise UsageError(_usage_of(verb))
    try:
        a = build_parser().parse_args(args)
    except SystemExit as err:  # argparse already printed "unrecognized arguments: ..."
        return int(err.code or 1)
    if a.verb == "create" and a.noun == "cluster":
        return cmd_create(a, args)
    if a.verb == "verify" and a.noun == "cluster":
        return cmd_verify(a)
    if a.verb == "get" and a.noun == "clusters":
        return cmd_get_clusters()
    if a.verb == "delete" and a.noun == "cluster":
        return cmd_delete_cluster(a)
    if a.verb == "add" and a.noun == "node":
        return cmd_add_node(a)
    if a.verb == "join" and a.noun == "node":
        return cmd_join_node(a)
    if a.verb == "delete" and a.noun == "node":
        return cmd_delete_node(a)
    if a.verb in ("describe", "stop", "start", "use") and a.noun == "cluster":
        return cmd_cluster(a.verb, a)
    if a.noun == "images":
        return cmd_images(a.verb, a)
    raise UsageError(f"unknown command '{' '.join(args[:2])}' (see tkctl --help)")
