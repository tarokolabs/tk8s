"""What the command line says, checked: cluster names, memory sizes, Kubernetes versions, and
the create flags or -f file turned into a UserCluster."""

import argparse
import re
from pathlib import Path

from . import model

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
K8S_RE = re.compile(r"^1\.[0-9]+\.[0-9]+$")


class UsageError(Exception):
    pass


# --preset: a named topology, expanded into the flags below before anything else sees them
PRESETS: dict[str, dict[str, object]] = {
    "lab": {"control_planes": 1, "workers": 2, "ha": "proxy", "routers": 1},
    "ha": {"control_planes": 3, "workers": 3, "ha": "bgp", "routers": 2},
}
PRESET_FLAGS = ("--control-planes", "--workers", "--ha", "--routers", "--defer-join", "-f")


def preset_line(name: str) -> str:
    """The expansion [plan] prints, so a preset is visibly a shorthand for flags."""
    p = PRESETS[name]
    return (
        f"preset {name}: --control-planes {p['control_planes']} --workers {p['workers']} "
        f"--ha {p['ha']} --routers {p['routers']}"
    )


def apply_preset(a: argparse.Namespace, raw: list[str]) -> str | None:
    """Without --preset: None. With it: the topology flags take the preset's values and the
    expansion line comes back; a topology flag on the command line, whatever its value, is
    refused (the rule is no mixing, not no contradiction)."""
    if a.preset is None:
        return None
    for flag in PRESET_FLAGS:
        glued = flag == "-f"  # -fFILE is how argparse also spells -f FILE
        if any(
            x == flag or x.startswith(flag + "=") or (glued and x.startswith("-f")) for x in raw
        ):
            raise UsageError(
                f"--preset {a.preset} cannot be combined with {flag}; "
                "use the expanded flags instead"
            )
    for k, v in PRESETS[a.preset].items():
        setattr(a, k, v)
    return preset_line(a.preset)


def validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise UsageError(
            f"invalid cluster name '{name}': use lowercase letters, digits and '-', "
            "start with a letter, max 16 characters"
        )
    return name


def validate_node_name(name: str) -> str:
    """Node names land in unit paths and podman names: the same shape plan gives them."""
    if not re.fullmatch(model.NODE_NAME, name):
        raise UsageError(
            f"invalid node name '{name}': use lowercase letters, digits and '-', "
            "start with a letter"
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


def add_create_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("name", nargs="?")
    p.add_argument("--control-planes", type=int, default=1)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--cpu", type=int, default=2)
    p.add_argument("--memory", default="4G")
    p.add_argument("--k8s")
    p.add_argument("--gvisor", action="store_true")
    p.add_argument("--datapath", choices=["auto", "netkit", "veth"])
    p.add_argument("--ha", choices=["proxy", "bgp"])
    p.add_argument("--routers", type=int, choices=[1, 2], default=1)
    p.add_argument("--preset", choices=sorted(PRESETS))
    p.add_argument("--defer-join", action="store_true")
    p.add_argument("-f", dest="file")
    p.add_argument("--allow-overlap", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--phase")
    p.add_argument("--skip-phases", default="")
    p.add_argument("--list-phases", action="store_true")
    p.add_argument("--quiet", action="store_true")


def user_cluster_from_flags(a: argparse.Namespace) -> model.UserCluster:
    """argparse-level checks only; what a -f file and the flags share is plan.validate's."""
    if a.control_planes < 1:
        raise UsageError("--control-planes must be a positive integer")
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
    if a.workers:
        groups.append(model.NodeGroup(role="worker", count=a.workers, cpu=a.cpu, memory=memory))
    network = model.UserNetwork(ha=a.ha, routers=a.routers) if a.ha or a.routers == 2 else None
    spec = model.UserSpec(
        kubernetes=a.k8s, gvisor=a.gvisor, datapath=a.datapath, network=network, nodes=groups
    )
    name = validate_name(a.name or "tk8s")
    return model.UserCluster(metadata=model.Metadata(name=name), spec=spec)


def user_cluster_from_file(a: argparse.Namespace, topo_flags: bool) -> model.UserCluster:
    if topo_flags:
        raise UsageError(
            "-f cannot be combined with --control-planes/--workers/--cpu/--memory/--ha/"
            "--routers; put them in the file"
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
