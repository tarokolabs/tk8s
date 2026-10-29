"""cluster.yaml, typed: what the user asked for (spec) and where the platform got to (status).

This is the only module that reads or writes YAML. Everything else works on these objects.
"""

from datetime import datetime
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

Role = Literal["control-plane", "worker"]
NODE_NAME = r"^[a-z][a-z0-9-]*$"
CLUSTER_NAME = r"^[a-z][a-z0-9-]{0,15}$"
MEMORY = r"^[0-9]+[gm]$"
HOST_NAME = r"^[a-z][a-z0-9-]{0,31}$"
RANGE = r"^[0-9.]+(-[0-9.]+)?(, *[0-9.]+(-[0-9.]+)?)*$"  # A-B or A [,C-D...]; B may be a last octet


class Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---- what the user writes: a -f file, or the flags turned into the same shape


class UserHost(Strict):
    """A host of a multi-host cluster: the name cluster.yaml and the output call it, and how
    to reach it ([user@]host[:port]); no ssh (or `local`) means the host tkctl runs on."""

    name: str = Field(pattern=HOST_NAME)
    ssh: str | None = None

    @field_validator("ssh")
    @classmethod
    def _local_is_none(cls, v: str | None) -> str | None:
        return None if v == "local" else v


class NodeGroup(Strict):
    role: Role
    count: int = Field(default=1, ge=1)
    name: str | None = Field(default=None, pattern=NODE_NAME)
    cpu: int = Field(default=2, ge=1)
    memory: str = "4G"  # podman size (4G, 4096M); plan lower-cases it
    join: bool = True
    host: str | None = None  # multi-host: the host name, or "auto"/omitted for round-robin


class UserNetwork(Strict):
    nodes: IPv4Network | None = None
    router: IPv4Address | None = None
    ha: Literal["proxy", "bgp"] | None = None
    routers: Literal[1, 2] | None = None
    vip: IPv4Address | None = None
    range: str | None = Field(default=None, pattern=RANGE)  # multi-host: A-B on the hosts' LAN
    router_hosts: list[str] | None = None  # multi-host: where the router container(s) run


class UserSpec(Strict):
    kubernetes: str | None = None
    cni: Literal["cilium"] = "cilium"
    datapath: Literal["auto", "netkit", "veth"] | None = None
    gvisor: bool = False
    network: UserNetwork | None = None
    nodes: list[NodeGroup]
    hosts: list[UserHost] | None = None  # present: a multi-host cluster


class Metadata(Strict):
    name: str = Field(pattern=CLUSTER_NAME)


class UserCluster(Strict):
    apiVersion: Literal["taroko.io/v1alpha1"] = "taroko.io/v1alpha1"
    kind: Literal["Cluster"] = "Cluster"
    metadata: Metadata
    spec: UserSpec


class NodeRequest(Strict):
    """tkctl add node: what was asked for; plan_add turns it into a Node."""

    role: Role
    cpu: int | None = Field(default=None, ge=1)
    memory: str | None = Field(default=None, pattern=MEMORY)
    host: str | None = None  # multi-host: --host, else the least loaded host


def node_image(k8s: str) -> str:
    return f"ghcr.io/tarokolabs/tk8s/node:v{k8s}"


# ---- what the platform keeps in <state dir>/cluster.yaml


class Host(Strict):
    """A host of a multi-host cluster as plan recorded it, with the LAN interface the macvlan
    network hangs off (the host's own default route interface)."""

    name: str = Field(pattern=HOST_NAME)
    ssh: str | None = None
    iface: str


class Node(Strict):
    role: Role
    name: str = Field(pattern=NODE_NAME)
    ip: IPv4Address
    cpu: int = Field(ge=1)
    memory: str = Field(pattern=MEMORY)
    join: bool = True
    host: str | None = None  # multi-host: the host this node's container runs on


class Network(Strict):
    index: int = Field(ge=0, le=255)
    nodes: IPv4Network
    gateway: IPv4Address
    router: IPv4Address
    ha: Literal["proxy", "bgp"] = "proxy"
    routers: Literal[1, 2] = 1  # two: VRRP floats `router` between them (.251 and .252)
    vip: IPv4Address | None = None  # bgp mode only: the anycast apiserver address, 172.23.N.1
    lb_range: str
    pods: IPv4Network
    services: IPv4Network
    range: str | None = Field(default=None, pattern=RANGE)  # multi-host: the reserved pool
    router_hosts: list[str] | None = None  # multi-host: one host per router container
    shim: IPv4Address | None = None  # multi-host: the operator's shim, when it runs nodes too

    @model_validator(mode="after")
    def _vip_follows_ha(self) -> Network:
        if (self.ha == "bgp") != (self.vip is not None):
            raise ValueError(
                "spec.network.vip is required with ha: bgp and not allowed with ha: proxy"
            )
        return self


class Router(Strict):
    """One router container of the cluster, derived from Network and never stored: with one
    router it is <cluster>-router at the floating address; with two, <cluster>-router-1 (.251,
    VRRP priority 200) and -2 (.252, 100) behind it."""

    name: str
    dir: str  # the state subdirectory (router, router-1, router-2)
    ip: IPv4Address
    priority: int | None
    host: str | None = None  # multi-host: the host it runs on


class Spec(Strict):
    kubernetes: str
    cni: Literal["cilium"]
    datapath: Literal["auto", "netkit", "veth"]
    datapath_resolved: Literal["netkit", "veth"] | None = None
    gvisor: bool
    network: Network
    nodes: list[Node] = Field(min_length=1)
    hosts: list[Host] | None = None  # present: a multi-host cluster

    @model_validator(mode="after")
    def _hosts_consistent(self) -> Spec:
        """hosts, range and the placement fields come and go together, and every placement
        names a listed host."""
        net = self.network
        if (self.hosts is None) != (net.range is None):
            raise ValueError("spec.hosts and spec.network.range go together")
        if self.hosts is None:
            if any(n.host is not None for n in self.nodes):
                raise ValueError("a node has a host on a single-host cluster (no spec.hosts)")
            if net.router_hosts is not None:
                raise ValueError("spec.network.router_hosts needs spec.hosts")
            return self
        names: list[str] = []
        for h in self.hosts:
            if h.name in names:
                raise ValueError(f"host name {h.name} is used twice")
            names.append(h.name)
        if sum(1 for h in self.hosts if h.ssh is None) > 1:
            raise ValueError("at most one host without ssh (the one tkctl runs on)")
        for n in self.nodes:
            if n.host not in names:
                raise ValueError(f"node host {n.host} is not in spec.hosts")
        if net.router_hosts is None or len(net.router_hosts) != net.routers:
            raise ValueError(f"router_hosts must name {net.routers} hosts")
        for r in net.router_hosts:
            if r not in names:
                raise ValueError(f"router host {r} is not in spec.hosts")
        return self


class Status(Strict):
    phase: Literal["Creating", "Ready"]
    completed: dict[str, datetime] = {}
    tk8s: str  # the tk8s version that created the cluster
    # True from the version that sizes nodes (capacity/allocatable from cpu/memory); a cluster
    # created before it keeps today's units and reports the host, and verify skips the check.
    node_capacity: bool = False
    operator: str | None = None  # multi-host: the hostname of the machine that manages it


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
    def endpoint_ip(self) -> IPv4Address:
        """What api.<cluster>.k8s resolves to: the router in proxy mode, the VIP in bgp mode."""
        net = self.spec.network
        return net.vip if net.vip is not None else net.router

    @property
    def floating_ip(self) -> IPv4Address:
        """The nodes' gateway and, in proxy mode, the apiserver entry: the one router's address,
        or the VRRP address the two routers share."""
        return self.spec.network.router

    @property
    def multi_host(self) -> bool:
        return self.spec.hosts is not None

    @property
    def host_names(self) -> list[str]:
        return [h.name for h in self.spec.hosts or []]

    @property
    def range_start(self) -> IPv4Address:
        """The first address of the reserved LAN range (multi-host): the router's floating one."""
        if self.spec.network.range is None:
            raise ValueError(f"cluster {self.name} has no spec.network.range (single host)")
        return IPv4Address(self.spec.network.range.split("-")[0])

    def host_of(self, container: str) -> str | None:
        """The host a node or router container runs on; None on a single-host cluster or for a
        name the cluster does not have."""
        for n in self.spec.nodes:
            if n.name == container:
                return n.host
        for r in self.routers:
            if r.name == container:
                return r.host
        return None

    @property
    def routers(self) -> list[Router]:
        """On a single host router-1 and -2 sit at .251/.252 of the node subnet; on several hosts
        at the two addresses after the floating one in the range."""
        net = self.spec.network
        hosts: list[str | None] = [None, None]
        if net.router_hosts is not None:
            hosts = list(net.router_hosts)
        if net.routers == 1:
            return [
                Router(
                    name=f"{self.name}-router",
                    dir="router",
                    ip=net.router,
                    priority=None,
                    host=hosts[0],
                )
            ]
        if net.range is None:
            base = int(net.nodes.network_address)
            first, second = IPv4Address(base + 251), IPv4Address(base + 252)
        else:
            first, second = self.range_start + 1, self.range_start + 2
        n = self.name
        return [
            Router(name=f"{n}-router-1", dir="router-1", ip=first, priority=200, host=hosts[0]),
            Router(name=f"{n}-router-2", dir="router-2", ip=second, priority=100, host=hosts[1]),
        ]

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
        return node_image(self.spec.kubernetes)


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


def load_user_text(text: str, where: str = "cluster file") -> UserCluster:
    raw = _parse(text, where)
    if isinstance(raw, dict) and "status" in raw:
        raise ClusterFileError(f"{where}: status is written by tkctl, not given in a cluster file")
    try:
        return UserCluster.model_validate(raw)
    except ValidationError as err:
        raise ClusterFileError(_first_error(where, err)) from err


def load_user_file(path: Path) -> UserCluster:
    return load_user_text(path.read_text(), str(path))


def load_text(text: str, where: str = "cluster.yaml") -> Cluster:
    try:
        return Cluster.model_validate(_parse(text, where))
    except ValidationError as err:
        raise ClusterFileError(_first_error(where, err)) from err


def load(path: Path) -> Cluster:
    return load_text(path.read_text(), str(path))


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


def peek(path: Path) -> tuple[int | None, str | None]:
    """(spec.network.index, spec.kubernetes) of any cluster.yaml on the host, however old or
    broken: subnet allocation and store pruning must not be blocked by another cluster's file."""
    try:
        return peek_text(path.read_text())
    except OSError:
        return None, None


def peek_text(text: str) -> tuple[int | None, str | None]:
    """peek() for a file already read (a copy on another host)."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        return None, None
    spec = raw.get("spec") if isinstance(raw, dict) else None
    if not isinstance(spec, dict):
        return None, None
    net = spec.get("network")
    index = net.get("index") if isinstance(net, dict) else None
    k8s = spec.get("kubernetes")
    return (index if isinstance(index, int) else None, str(k8s) if k8s is not None else None)
