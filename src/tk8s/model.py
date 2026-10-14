"""cluster.yaml, typed: what the user asked for (spec) and where the platform got to (status).

This is the only module that reads or writes YAML. Everything else works on these objects.
"""

from datetime import datetime
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

Role = Literal["control-plane", "worker"]
NODE_NAME = r"^[a-z][a-z0-9-]*$"
CLUSTER_NAME = r"^[a-z][a-z0-9-]{0,15}$"
MEMORY = r"^[0-9]+[gm]$"


class Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---- what the user writes: a -f file, or the flags turned into the same shape


class NodeGroup(Strict):
    role: Role
    count: int = Field(default=1, ge=1)
    name: str | None = Field(default=None, pattern=NODE_NAME)
    cpu: int = Field(default=2, ge=1)
    memory: str = "4G"  # podman size (4G, 4096M); plan lower-cases it
    join: bool = True


class UserNetwork(Strict):
    nodes: IPv4Network | None = None
    lb: IPv4Address | None = None


class UserSpec(Strict):
    kubernetes: str | None = None
    cni: Literal["cilium", "canal"] = "cilium"
    datapath: Literal["auto", "netkit", "veth"] | None = None
    gvisor: bool = False
    network: UserNetwork | None = None
    nodes: list[NodeGroup]


class Metadata(Strict):
    name: str = Field(pattern=CLUSTER_NAME)


class UserCluster(Strict):
    apiVersion: Literal["taroko.io/v1alpha1"] = "taroko.io/v1alpha1"
    kind: Literal["Cluster"] = "Cluster"
    metadata: Metadata
    spec: UserSpec


# ---- what the platform keeps in <state dir>/cluster.yaml


class Node(Strict):
    role: Role
    name: str = Field(pattern=NODE_NAME)
    ip: IPv4Address
    cpu: int = Field(ge=1)
    memory: str = Field(pattern=MEMORY)
    join: bool = True


class Network(Strict):
    index: int = Field(ge=0, le=255)
    nodes: IPv4Network
    gateway: IPv4Address
    lb: IPv4Address
    lb_range: str
    pods: IPv4Network
    services: IPv4Network


class Spec(Strict):
    kubernetes: str
    cni: Literal["cilium", "canal"]
    datapath: Literal["auto", "netkit", "veth"]
    datapath_resolved: Literal["netkit", "veth"] | None = None
    gvisor: bool
    network: Network
    nodes: list[Node] = Field(min_length=1)


class Status(Strict):
    phase: Literal["Creating", "Ready"]
    completed: dict[str, datetime] = {}
    tk8s: str  # the tk8s version that created the cluster


class Cluster(Strict):
    apiVersion: Literal["taroko.io/v1alpha1"] = "taroko.io/v1alpha1"
    kind: Literal["Cluster"] = "Cluster"
    metadata: Metadata
    spec: Spec
    status: Status | None = None

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def domain(self) -> str:
        return f"{self.name}.k8s"

    @property
    def control_planes(self) -> list[Node]:
        return [n for n in self.spec.nodes if n.role == "control-plane"]

    @property
    def workers(self) -> list[Node]:
        return [n for n in self.spec.nodes if n.role == "worker"]

    @property
    def first_control_plane(self) -> Node:
        return self.spec.nodes[0]  # plan guarantees the first entry is a control plane

    @property
    def node_image(self) -> str:
        return f"ghcr.io/tarokolabs/tk8s/node:v{self.spec.kubernetes}"


# ---- YAML in


class ClusterFileError(Exception):
    """A cluster file that does not parse or validate; the message names the field."""


class _Loader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys (PyYAML keeps the last one silently)."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.YAMLError(
                    f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


def _parse(text: str, where: str) -> Any:
    try:
        return yaml.load(text, Loader=_Loader)  # _Loader is a SafeLoader: no arbitrary objects
    except yaml.YAMLError as err:
        raise ClusterFileError(f"{where}: {err}") from err


def _first_error(where: str, err: ValidationError) -> str:
    """One line, naming the field and, for a scalar, the value that was given."""
    e = err.errors()[0]
    loc = ".".join(str(p) for p in e["loc"]) or "document"
    given = e.get("input")
    got = f" (got {given!r})" if isinstance(given, str | int | float | bool) else ""
    return f"{where}: {loc}: {e['msg']}{got}"


def _refuse_old_keys(raw: Any) -> None:
    spec = raw.get("spec") if isinstance(raw, dict) else None
    if not isinstance(spec, dict):
        return
    if "runtime" in spec:
        raise ClusterFileError(
            "spec.runtime is not a field any more: nodes run CRI-O from the single node image; "
            "remove the key"
        )
    if isinstance(spec.get("network"), dict) and "vip" in spec["network"]:
        raise ClusterFileError(
            "spec.network.vip is no longer supported: every cluster gets an apiserver LB at "
            "spec.network.lb; recreate the cluster"
        )


def load_user_text(text: str, where: str = "cluster file") -> UserCluster:
    raw = _parse(text, where)
    _refuse_old_keys(raw)
    if isinstance(raw, dict) and "status" in raw:
        raise ClusterFileError(f"{where}: status is written by tkctl, not given in a cluster file")
    try:
        return UserCluster.model_validate(raw)
    except ValidationError as err:
        raise ClusterFileError(_first_error(where, err)) from err


def load_user_file(path: Path) -> UserCluster:
    return load_user_text(path.read_text(), str(path))


def load_text(
    text: str, where: str = "cluster.yaml", *, create_complete_marker: bool = False
) -> Cluster:
    try:
        c = Cluster.model_validate(_parse(text, where))
    except ValidationError as err:
        raise ClusterFileError(_first_error(where, err)) from err
    if c.status is None and create_complete_marker:
        # A cluster created before status existed: the marker file said "complete".
        c = c.model_copy(update={"status": Status(phase="Ready", tk8s="pre-status")})
    return c


def load(path: Path, *, create_complete_marker: bool = False) -> Cluster:
    return load_text(path.read_text(), str(path), create_complete_marker=create_complete_marker)


# ---- YAML out


class _Quoted(str):
    """Dumped with double quotes: the Kubernetes version must not read like a float."""


class _Dumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)  # lists indented under their key, kubectl style


_Dumper.add_representer(
    _Quoted, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", str(v), style='"')
)


def dumps(cluster: Cluster) -> str:
    data = cluster.model_dump(mode="json", exclude_none=True)
    data["spec"]["kubernetes"] = _Quoted(data["spec"]["kubernetes"])
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, default_flow_style=False)


def save(cluster: Cluster, path: Path) -> None:
    """Atomic: a crash mid-write never leaves half a cluster.yaml behind."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dumps(cluster))
    tmp.replace(path)


def peek(path: Path) -> tuple[int | None, str | None]:
    """(spec.network.index, spec.kubernetes) of any cluster.yaml on the host, however old or
    broken: subnet allocation and store pruning must not be blocked by another cluster's file."""
    try:
        raw = yaml.safe_load(path.read_text())
    except OSError, yaml.YAMLError:
        return None, None
    spec = raw.get("spec") if isinstance(raw, dict) else None
    if not isinstance(spec, dict):
        return None, None
    net = spec.get("network")
    index = net.get("index") if isinstance(net, dict) else None
    k8s = spec.get("kubernetes")
    return (index if isinstance(index, int) else None, str(k8s) if k8s is not None else None)
