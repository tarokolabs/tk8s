"""What describe reads off a running cluster: the global services (annotated
service.cilium.io/global) with their backends split into local and remote by pod CIDR, and the
entrypoints an external load balancer or DNS would point at (Gateways, LoadBalancer Services).
Imports nothing from lifecycle: lifecycle imports describe_mesh, which imports this."""

import json
from dataclasses import dataclass
from ipaddress import IPv4Address

from .. import model
from ..create.kubeadm import kubectl
from ..phases import Context

TIMEOUT = "--request-timeout=5s"  # describe must not hang on an apiserver that is away

MESH_APISERVER = "kube-system/clustermesh-apiserver"


@dataclass(frozen=True)
class GlobalService:
    ns: str
    name: str
    cluster_ip: str


def global_services(ctx: Context) -> list[GlobalService]:
    r = kubectl(
        ctx,
        "get",
        "svc",
        "-A",
        "-o",
        "jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name} {.spec.clusterIP} "
        '{.metadata.annotations.service\\.cilium\\.io/global}{"\\n"}{end}',
        TIMEOUT,
    )
    if r.returncode != 0:
        return []
    out: list[GlobalService] = []
    for line in r.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2] == "true":
            ns, _, name = fields[0].partition("/")
            out.append(GlobalService(ns, name, fields[1]))
    return out


def backends(ctx: Context) -> dict[str, list[str]]:
    """frontend IP -> backend IPs from the agent's service map (cilium-dbg service list), every
    port of a frontend merged; {} when the agent cannot be asked."""
    r = kubectl(
        ctx,
        "-n",
        "kube-system",
        "exec",
        "ds/cilium",
        TIMEOUT,
        "--",
        "cilium-dbg",
        "service",
        "list",
        "-o",
        "json",
    )
    if r.returncode != 0:
        return {}
    try:
        entries = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    table: dict[str, list[str]] = {}
    for e in entries:
        spec = e.get("spec", {})
        front = spec.get("frontend-address", {}).get("ip")
        if not front:
            continue
        ips = table.setdefault(front, [])
        for b in spec.get("backend-addresses") or []:
            if b.get("ip") and b["ip"] not in ips:
                ips.append(b["ip"])
    return table


def counts(c: model.Cluster, svc: GlobalService, table: dict[str, list[str]]) -> tuple[int, int]:
    """(local, remote) backends of a global service: local is inside this cluster's pod CIDR."""
    local = remote = 0
    for ip in table.get(svc.cluster_ip, []):
        if IPv4Address(ip) in c.spec.network.pods:
            local += 1
        else:
            remote += 1
    return local, remote


def entrypoints(ctx: Context) -> list[tuple[str, str, str]]:
    """(kind, ns/name, address) of every Gateway and LoadBalancer Service but the mesh
    apiserver's; an address not assigned yet reads `pending`."""
    out: list[tuple[str, str, str]] = []
    gws = kubectl(
        ctx,
        "get",
        "gateway",
        "-A",
        "-o",
        "jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name} "
        '{.status.addresses[0].value}{"\\n"}{end}',
        TIMEOUT,
    )
    svcs = kubectl(
        ctx,
        "get",
        "svc",
        "-A",
        "--field-selector",
        "spec.type=LoadBalancer",
        "-o",
        "jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name} "
        '{.status.loadBalancer.ingress[0].ip}{"\\n"}{end}',
        TIMEOUT,
    )
    for kind, r in (("gateway", gws), ("service", svcs)):
        if r.returncode != 0:
            continue
        for line in r.stdout.splitlines():
            fields = line.split()
            if not fields or fields[0] == MESH_APISERVER:
                continue
            out.append((kind, fields[0], fields[1] if len(fields) > 1 else "pending"))
    return out
