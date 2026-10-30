"""What describe reads from a running cluster: the global services with their local and
remote backends, and the entrypoints (Gateways, LoadBalancer Services)."""

import json

from tk8s.host import FakeHost, Result
from tk8s.mesh import services
from tk8s.phases import Context

SVC_LIST = ("kubectl", "get", "svc", "-A", "-o")
CILIUM_DBG = ("kubectl", "-n", "kube-system", "exec", "ds/cilium", "--", "cilium-dbg")
DBG_JSON = json.dumps(
    [
        {
            "spec": {
                "frontend-address": {"ip": "10.98.0.20", "port": 80},
                "backend-addresses": [{"ip": "10.244.0.7"}, {"ip": "10.244.8.3"}],
            }
        },
        {
            "spec": {
                "frontend-address": {"ip": "10.98.0.20", "port": 443},
                "backend-addresses": [{"ip": "10.244.0.7"}],
            }
        },
        {"spec": {"frontend-address": {"ip": "10.98.0.10", "port": 53}, "backend-addresses": []}},
    ]
)


def test_global_services_are_the_ones_annotated_true(ctx: Context, host: FakeHost) -> None:
    host.responses[SVC_LIST] = Result(
        0,
        "default/echo 10.98.0.20 true\nkube-system/kube-dns 10.98.0.10 \n"
        "default/x 10.98.0.3 false\n",
        "",
    )
    assert services.global_services(ctx) == [
        services.GlobalService("default", "echo", "10.98.0.20")
    ]


def test_backends_map_frontends_to_backend_ips(ctx: Context, host: FakeHost) -> None:
    host.responses[CILIUM_DBG] = Result(0, DBG_JSON, "")
    assert services.backends(ctx) == {
        "10.98.0.20": ["10.244.0.7", "10.244.8.3"],
        "10.98.0.10": [],
    }
    host.responses[CILIUM_DBG] = Result(1, "", "no agent")
    assert services.backends(ctx) == {}


def test_counts_split_backends_by_the_clusters_own_pod_cidr(ctx: Context) -> None:
    svc = services.GlobalService("default", "echo", "10.98.0.20")
    table = {"10.98.0.20": ["10.244.0.7", "10.244.8.3"]}
    assert services.counts(ctx.require_cluster(), svc, table) == (1, 1)
    assert services.counts(ctx.require_cluster(), svc, {}) == (0, 0)


def test_entrypoints_list_gateways_and_loadbalancers_but_not_the_mesh_apiserver(
    ctx: Context, host: FakeHost
) -> None:
    host.responses[("kubectl", "get", "gateway", "-A", "-o")] = Result(
        0, "default/gw 172.22.0.205\n", ""
    )
    host.responses[("kubectl", "get", "svc", "-A", "--field-selector")] = Result(
        0,
        "default/db 172.22.0.206\nkube-system/clustermesh-apiserver 172.22.0.200\ndefault/new \n",
        "",
    )
    assert services.entrypoints(ctx) == [
        ("gateway", "default/gw", "172.22.0.205"),
        ("service", "default/db", "172.22.0.206"),
        ("service", "default/new", "pending"),
    ]
