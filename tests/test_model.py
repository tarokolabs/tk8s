from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

import pytest

from tests.fixtures import DEMO_USER, MULTI_YAML, resolved
from tk8s import model

GOOD = """\
apiVersion: taroko.io/v1alpha1
kind: Cluster
metadata:
  name: demo
spec:
  kubernetes: "1.37.2"
  cni: cilium
  datapath: auto
  gvisor: false
  network:
    index: 3
    nodes: 172.22.3.0/24
    gateway: 172.22.3.254
    router: 172.22.3.253
    ha: proxy
    routers: 1
    lb_range: 172.22.3.200-172.22.3.219
    pods: 10.244.24.0/21
    services: 10.98.3.0/24
  nodes:
    - role: control-plane
      name: demo-control-plane-1
      ip: 172.22.3.1
      cpu: 2
      memory: 4g
      join: true
    - role: worker
      name: demo-worker-1
      ip: 172.22.3.2
      cpu: 2
      memory: 4g
      join: true
"""


def test_round_trip_keeps_the_exact_text(tmp_path: Path) -> None:
    p = tmp_path / "cluster.yaml"
    p.write_text(GOOD)
    c = model.load(p)
    assert c.name == "demo" and c.domain == "demo.k8s"
    assert c.first_control_plane.name == "demo-control-plane-1" and len(c.workers) == 1
    assert c.spec.network.pods == IPv4Network("10.244.24.0/21")
    assert c.node_image == "ghcr.io/tarokolabs/tk8s/node:v1.37.2"
    assert model.dumps(c) == GOOD  # quoted version, indented lists, key order


def test_status_is_dumped_after_spec() -> None:
    c = model.load_text(GOOD).model_copy(
        update={"status": model.Status(phase="Creating", tk8s="v1")}
    )
    text = model.dumps(c)
    assert text.endswith(
        "status:\n  phase: Creating\n  completed: {}\n  tk8s: v1\n  node_capacity: false\n"
    )
    assert text.index("spec:") < text.index("status:")


def test_duplicate_keys_are_refused(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("metadata: {name: a}\nmetadata: {name: b}\nspec: {nodes: []}\n")
    with pytest.raises(model.ClusterFileError, match="duplicate key 'metadata'"):
        model.load_user_file(p)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("metadata: {name: Demo}\nspec: {nodes: [{role: control-plane}]}\n", "metadata.name"),
        ("spec: {nodes: [{role: control-plane}]}\n", "metadata"),
        ("metadata: {name: x}\nspec: {nodes: [{role: master}]}\n", "spec.nodes.0.role"),
        ("metadata: {name: x}\nspec: {nodes: [{role: worker, cpu: two}]}\n", "spec.nodes.0.cpu"),
        ("metadata: {name: x}\nspec: {nodes: [{role: worker, name: Big_Node}]}\n", "Big_Node"),
        (
            "metadata: {name: x}\nspec: {nodes: [{role: control-plane}]}\nstatus: {phase: Ready}\n",
            "status is written by tkctl",
        ),
    ],
)
def test_bad_user_files_name_the_field(tmp_path: Path, text: str, message: str) -> None:
    p = tmp_path / "in.yaml"
    p.write_text(text)
    with pytest.raises(model.ClusterFileError, match=message):
        model.load_user_file(p)


def test_user_file_with_spec_before_metadata_is_fine(tmp_path: Path) -> None:
    p = tmp_path / "later.yaml"
    p.write_text(
        "spec:\n  nodes:\n    - role: control-plane\n    - role: worker\n      name: later-big\n"
        "metadata:\n  name: later\n"
    )
    u = model.load_user_file(p)
    assert u.metadata.name == "later" and u.spec.nodes[1].name == "later-big"


def test_user_network_accepts_router_pin() -> None:
    u = model.UserCluster.model_validate(
        {
            "metadata": {"name": "pin"},
            "spec": {
                "network": {"nodes": "172.22.9.0/24", "router": "172.22.9.240"},
                "nodes": [{"role": "control-plane"}],
            },
        }
    )
    assert u.spec.network is not None and u.spec.network.router == IPv4Address("172.22.9.240")


def test_a_cluster_file_with_the_old_lb_key_is_refused_as_a_plain_extra_field() -> None:
    """No compatibility and no special message: clusters made before the router existed are
    deleted with that version's tkctl and recreated."""
    with pytest.raises(model.ClusterFileError, match=r"spec\.network"):
        model.load_text(GOOD.replace("    router: 172.22.3.253\n", "    lb: 172.22.3.100\n"))


def test_network_ha_defaults_to_proxy_and_accepts_only_proxy_for_now() -> None:
    c = model.load_text(GOOD.replace("    ha: proxy\n", ""))
    assert c.spec.network.ha == "proxy"
    with pytest.raises(model.ClusterFileError, match="ha"):
        model.load_text(GOOD.replace("ha: proxy", "ha: anycast"))


def test_ha_and_vip_go_together() -> None:
    """bgp without a VIP, or proxy with one, is a malformed file: later phases must never guess."""
    bgp = GOOD.replace("    ha: proxy\n", "    ha: bgp\n    vip: 172.23.3.1\n")
    c = model.load_text(bgp)
    assert c.spec.network.ha == "bgp" and str(c.spec.network.vip) == "172.23.3.1"
    assert str(c.endpoint_ip) == "172.23.3.1"
    assert str(model.load_text(GOOD).endpoint_ip) == "172.22.3.253"
    with pytest.raises(model.ClusterFileError, match="vip"):
        model.load_text(GOOD.replace("    ha: proxy\n", "    ha: bgp\n"))
    with pytest.raises(model.ClusterFileError, match="vip"):
        model.load_text(GOOD.replace("    ha: proxy\n", "    ha: proxy\n    vip: 172.23.3.1\n"))


def test_node_request_is_frozen_and_sizes_default_to_none() -> None:
    from pydantic import ValidationError

    r = model.NodeRequest(role="worker")
    assert r.cpu is None and r.memory is None
    with pytest.raises(ValidationError):
        model.NodeRequest(role="worker", cpu=0)


def test_cni_is_cilium_only() -> None:
    with pytest.raises(model.ClusterFileError):
        model.load_text(GOOD.replace("cni: cilium", "cni: canal"))


def test_a_cluster_yaml_without_node_capacity_loads_as_false() -> None:
    text = model.dumps(resolved(DEMO_USER)).replace("  node_capacity: true\n", "")
    assert "node_capacity" not in text
    status = model.load_text(text).status
    assert status is not None and status.node_capacity is False


def test_node_capacity_is_dumped_right_after_tk8s() -> None:
    text = model.dumps(resolved(DEMO_USER))
    assert "  tk8s: test\n  node_capacity: true\n" in text


# ---- multi-host fields


def test_multi_host_cluster_round_trips_and_derives_routers() -> None:
    c = model.load_text(MULTI_YAML)
    assert c.multi_host and c.host_names == ["a", "b", "c"]
    assert c.range_start == IPv4Address("192.168.1.64")
    assert [r.ip for r in c.routers] == [IPv4Address("192.168.1.65"), IPv4Address("192.168.1.66")]
    assert [r.host for r in c.routers] == ["a", "b"]
    assert [r.name for r in c.routers] == ["demo-router-1", "demo-router-2"]
    assert c.host_of("demo-control-plane-2") == "b" and c.host_of("demo-router-1") == "a"
    assert c.host_of("nothing") is None
    assert model.dumps(c) == MULTI_YAML


def test_one_router_on_multi_host_sits_at_the_floating_address() -> None:
    text = MULTI_YAML.replace("routers: 2", "routers: 1").replace(
        "    router_hosts:\n      - a\n      - b\n", "    router_hosts:\n      - c\n"
    )
    c = model.load_text(text)
    assert [(r.name, str(r.ip), r.host) for r in c.routers] == [
        ("demo-router", "192.168.1.64", "c")
    ]


def test_single_host_cluster_has_no_host_fields_in_its_yaml() -> None:
    c = resolved(DEMO_USER)
    text = model.dumps(c)
    for word in ("hosts:", "    range:", "operator:", "host:"):
        assert word not in text, word
    assert not c.multi_host and c.host_names == [] and c.host_of("demo-worker-1") is None
    assert all(r.host is None for r in c.routers)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            "    range: 192.168.1.64-192.168.1.70,192.168.1.80-192.168.1.89\n",
            "",
            "spec.hosts and spec.network.range go together",
        ),
        ("      host: c\n", "      host: x\n", "node host x is not in spec.hosts"),
        ("      ssh: tux@192.168.1.231\n", "", "at most one host without ssh"),
        (
            "    router_hosts:\n      - a\n      - b\n",
            "    router_hosts:\n      - a\n",
            "router_hosts must name 2 hosts",
        ),
        ("    - name: b\n", "    - name: a\n", "host name a is used twice"),
    ],
)
def test_inconsistent_host_fields_are_refused(old: str, new: str, message: str) -> None:
    assert old in MULTI_YAML
    with pytest.raises(model.ClusterFileError, match=message):
        model.load_text(MULTI_YAML.replace(old, new))


def test_a_node_host_on_a_single_host_cluster_is_refused() -> None:
    c = resolved(DEMO_USER)
    text = model.dumps(c).replace("      join: true\n", "      join: true\n      host: a\n", 1)
    with pytest.raises(model.ClusterFileError, match="host on a single-host cluster"):
        model.load_text(text)


def test_peek_text_reads_index_and_version_from_any_text() -> None:
    assert model.peek_text("spec: {network: {index: 3}, kubernetes: '1.37.2'}") == (3, "1.37.2")
    assert model.peek_text("garbage: [") == (None, None)
