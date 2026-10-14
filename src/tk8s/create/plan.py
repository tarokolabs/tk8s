"""plan: turn what the user asked for into the cluster.yaml every other phase reads.

Fills defaults, allocates the subnet index, expands node groups into named, numbered nodes.
"""

import re
from ipaddress import IPv4Address, IPv4Network

from .. import model
from ..paths import Paths
from ..phases import Context, Phase


class PlanError(Exception):
    pass


def validate(u: model.UserCluster) -> None:
    """The rules a -f file and the flags share; messages name the field (same words as before)."""
    s = u.spec
    if s.gvisor and s.datapath == "netkit":
        raise PlanError(
            "gvisor requires the veth datapath; gVisor sandboxes have no network under netkit"
        )
    if not s.nodes:
        raise PlanError("spec.nodes must list at least one control-plane node")
    names: set[str] = set()
    for g in s.nodes:
        if not re.fullmatch(r"[0-9]+[GgMm]", g.memory):
            raise PlanError(
                f'node memory "{g.memory}" must be a number followed by G or M, e.g. 4G or '
                "4096M (podman does not accept 4Gi)"
            )
        if g.name is not None:
            if g.count != 1:
                raise PlanError(
                    f'node name "{g.name}" cannot be combined with count {g.count}; '
                    "a named entry is one node"
                )
            if g.name in names:
                raise PlanError(f'node name "{g.name}" is used twice')
            names.add(g.name)
    if s.nodes[0].role != "control-plane":
        raise PlanError(
            "the first entry in spec.nodes must be a control-plane (it runs kubeadm init)"
        )
    cps = sum(g.count for g in s.nodes if g.role == "control-plane")
    if cps % 2 == 0:
        raise PlanError(f"control-plane count {cps} must be odd (1, 3, 5) for etcd quorum")
    if cps > 5:
        raise PlanError(
            f"control-plane count {cps} exceeds the limit: at most 5 "
            "(the apiserver LB has five backend slots)"
        )


def pick_index(u: model.UserCluster, existing: model.Cluster | None, used: set[int]) -> int:
    """The index already recorded for this cluster (resume), else the one an explicit subnet
    implies, else the smallest one no other cluster uses."""
    if existing is not None:
        return existing.spec.network.index
    want = u.spec.network.nodes if u.spec.network else None
    if want is not None:
        m = re.fullmatch(r"172\.22\.([0-9]+)\.0/24", str(want))
        if m is None:
            raise PlanError(
                "spec.network.nodes must be 172.22.N.0/24 (N = 0..255) "
                "or omitted for automatic allocation"
            )
        return int(m.group(1))
    n = 0
    while n in used:
        n += 1
    return n


def network(index: int, want_lb: IPv4Address | None) -> model.Network:
    """172.22.N.0/24: nodes .1-.199, the LB .100 (custom: .220-.253), LB-IPAM .200-.219,
    the gateway .254."""
    if want_lb is not None and not re.fullmatch(
        rf"172\.22\.{index}\.(2[2-4][0-9]|25[0-3])", str(want_lb)
    ):
        raise PlanError(
            f"spec.network.lb {want_lb} must be 172.22.{index}.220-253 (the default is .100)"
        )
    return model.Network(
        index=index,
        nodes=IPv4Network(f"172.22.{index}.0/24"),
        gateway=IPv4Address(f"172.22.{index}.254"),
        lb=want_lb or IPv4Address(f"172.22.{index}.100"),
        lb_range=f"172.22.{index}.200-172.22.{index}.219",
        pods=IPv4Network(f"10.244.{index * 8}.0/21"),
        services=IPv4Network(f"10.98.{index}.0/24"),
    )


def expand_nodes(name: str, groups: list[model.NodeGroup], index: int) -> list[model.Node]:
    """Each group becomes count nodes named <cluster>-cp-N / <cluster>-worker-N; IPs skip .100."""
    nodes: list[model.Node] = []
    ip, cp, wk = 0, 0, 0
    for g in groups:
        for _ in range(g.count):
            ip += 1
            if ip == 100:
                ip = 101
            if g.role == "control-plane":
                cp += 1
                auto = f"{name}-cp-{cp}"
            else:
                wk += 1
                auto = f"{name}-worker-{wk}"
            nodes.append(
                model.Node(
                    role=g.role,
                    name=g.name or auto,
                    ip=IPv4Address(f"172.22.{index}.{ip}"),
                    cpu=g.cpu,
                    memory=g.memory.lower(),
                    join=g.join,
                )
            )
    return nodes


def resolve(
    u: model.UserCluster,
    *,
    k8s_default: str,
    existing: model.Cluster | None,
    used_indices: set[int],
    allow_overlap: bool,
    tk8s_version: str,
) -> model.Cluster:
    validate(u)
    index = pick_index(u, existing, used_indices)
    if existing is None and index in used_indices and not allow_overlap:
        raise PlanError(
            f"subnet 172.22.{index}.0/24 is already used by another cluster; "
            "pass --allow-overlap to force"
        )
    s = u.spec
    spec = model.Spec(
        kubernetes=s.kubernetes or k8s_default,
        cni=s.cni,
        datapath=s.datapath or ("veth" if s.gvisor else "auto"),
        datapath_resolved=existing.spec.datapath_resolved if existing else None,
        gvisor=s.gvisor,
        network=network(index, s.network.lb if s.network else None),
        nodes=expand_nodes(u.metadata.name, s.nodes, index),
    )
    status = existing.status if existing else model.Status(phase="Creating", tk8s=tk8s_version)
    return model.Cluster(metadata=u.metadata, spec=spec, status=status)


def load_existing(paths: Paths, name: str) -> model.Cluster | None:
    """The cluster.yaml a previous, interrupted create left behind (resume), if any."""
    p = paths.state_dir(name) / "cluster.yaml"
    if not p.exists():
        return None
    marker = (paths.state_dir(name) / ".create-complete").exists()
    return model.load(p, create_complete_marker=marker)


def used_indices(paths: Paths) -> set[int]:
    """Subnet indices of every cluster on this host (dry-run reads the real directory too)."""
    return {model.load(p).spec.network.index for p in paths.clusters_dir.glob("*/cluster.yaml")}


# ---- the phase


def done(ctx: Context) -> bool:
    return ctx.cluster is not None and ctx.host.exists(ctx.state_dir / "cluster.yaml")


def run(ctx: Context) -> None:
    c = ctx.require_cluster()
    n = len(c.control_planes)
    ctx.say(
        f"  cluster {c.name}: {n} control plane{'s' if n > 1 else ''} + "
        f"{len(c.workers)} workers, K8s {c.spec.kubernetes}, subnet {c.spec.network.nodes}"
    )
    ctx.host.write(ctx.state_dir / "cluster.yaml", model.dumps(c))


PHASE = Phase("plan", "resolve cluster.yaml: defaults, subnet, node names", done, run)
