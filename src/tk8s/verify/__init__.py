"""Platform health checks, one PASS/FAIL/SKIP line each, exit 1 on any FAIL. Test resources live
in namespace tk-verify and are removed at the end, including on interruption. Shared by people
and the e2e CI. TK_VERIFY_TIMEOUT is the per-check wait in seconds (default 180).

Each check is a phase (tkctl verify cluster NAME --phase <check>); verify walks them itself so
the output keeps the [section] lines people and tklab's logs expect."""

from .. import lifecycle
from ..phases import Context, Phase, PhaseFailed
from .gateway import check_gateway, check_gatewayclass, check_httproute, check_tcproute, check_xff
from .ha import check_router
from .hosts import check_hosts
from .platform import (
    check_cilium,
    check_kube_system,
    check_kubelet,
    check_metrics,
    check_node_sizes,
    check_nodes,
    check_operator,
    check_storage,
)
from .report import NS, Report, VerifyError
from .runtime import check_gvisor_network, check_runtimeclass_crun, check_runtimeclass_gvisor

__all__ = ["NS", "PHASES", "Report", "VerifyError", "run"]

CHECKS = [
    ("hosts", "every host reachable, its target active, its shim up (several hosts)", check_hosts),
    ("nodes", "every node Ready", check_nodes),
    ("node-sizes", "capacity and allocatable follow cluster.yaml", check_node_sizes),
    ("kubelet", "no runtime cgroup errors in the kubelet log", check_kubelet),
    ("kube-system", "every kube-system pod Running", check_kube_system),
    ("cilium", "cilium status", check_cilium),
    ("cilium-operator", "cilium-operator without restarts and with Gateway API on", check_operator),
    ("metrics", "kubectl top nodes", check_metrics),
    ("storage", "a PVC bound and written, data under the state dir", check_storage),
    ("runtimeclass-crun", "a pod under RuntimeClass crun", check_runtimeclass_crun),
    ("runtimeclass-gvisor", "a pod under RuntimeClass gvisor", check_runtimeclass_gvisor),
    ("gvisor-network", "the kubernetes API reachable from a gVisor sandbox", check_gvisor_network),
    ("router", "the router healthy with every joined control plane behind it", check_router),
    ("gatewayclass", "GatewayClass cilium Accepted", check_gatewayclass),
    ("gateway", "a Gateway programmed by LB-IPAM", check_gateway),
    ("httproute", "an HTTPRoute reachable from the host", check_httproute),
    ("xff", "X-Forwarded-For carries the client IP", check_xff),
    ("tcproute", "a TCPRoute reachable from the host", check_tcproute),
]
PHASES: list[Phase] = [Phase(name, summary, None, lambda ctx: None) for name, summary, _ in CHECKS]
# A check that reads what an earlier one set up (the Gateway's address, the gVisor pod's output)
# runs that one first when asked for on its own.
PREREQUISITES = {
    "httproute": ["gateway"],
    "xff": ["gateway"],
    "tcproute": ["gateway"],
    "gvisor-network": ["runtimeclass-gvisor"],
}
SECTIONS = {
    "hosts": "hosts",
    "nodes": "nodes",
    "kubelet": "kubelet",
    "kube-system": "system",
    "metrics": "metrics-server",
    "storage": "local-path",
    "runtimeclass-crun": "runtimeclass",
    "router": "router",
    "gatewayclass": "gateway-api",
}


def _section_of(check: str) -> str:
    """The [section] a check prints under: the nearest section heading at or before it."""
    current = ""
    for name, _, _ in CHECKS:
        current = SECTIONS.get(name, current)
        if name == check:
            return current
    return current


def run(ctx: Context, only: str | None = None) -> int:
    """Every check (or one), then the summary line; the number of FAILs."""
    c = ctx.require_cluster()
    if c.status is None or c.status.phase != "Ready":
        raise VerifyError(
            f"cluster {c.name} is not fully created; rerun tkctl create cluster {c.name}"
        )
    if lifecycle.state_of(ctx, c) != "Running":
        raise VerifyError(f"cluster {c.name} is not running (tkctl start cluster {c.name})")
    if only is not None and only not in {n for n, _, _ in CHECKS}:
        raise PhaseFailed(f"unknown phase {only!r}; phases: {', '.join(n for n, _, _ in CHECKS)}")
    r = Report(ctx)
    ctx.say(f"verify {c.name} ({len(c.spec.nodes)} nodes, timeout {r.timeout}s)")
    r.kubectl("delete", "ns", NS, "--ignore-not-found", "--wait=true", "--timeout=60s")
    try:
        if r.kubectl("create", "ns", NS)[0] != 0:
            r.ng(f"create namespace {NS}")
            return r.failed
        wanted = None if only is None else {*PREREQUISITES.get(only, []), only}
        printed = ""
        for name, _, check in CHECKS:
            if wanted is not None and name not in wanted:
                continue
            if name == "hosts" and not c.multi_host:
                continue  # nothing to print on a single host, not even the section
            section = _section_of(name)
            if section != printed:
                ctx.say("")
                ctx.say(f"[{section}]")
                printed = section
            check(r)
    finally:
        # The Gateway and its routes go before the namespace: cilium-operator reconciling a
        # Gateway inside a terminating namespace fails in a loop until it loses its lease.
        r.kubectl(
            "delete",
            "-n",
            NS,
            "gateway,httproute,tcproute",
            "--all",
            "--ignore-not-found",
            "--wait=true",
            "--timeout=60s",
        )
        r.kubectl("delete", "ns", NS, "--ignore-not-found", "--wait=false")
        ctx.say("")
        ctx.say(f"PASS {r.passed}  FAIL {r.failed}  SKIP {r.skipped}")
    return r.failed
