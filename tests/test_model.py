from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

import pytest

from tk8s import model

GOOD = """\
apiVersion: taroko.io/v1alpha1
kind: Cluster
metadata:
  name: demo
spec:
  kubernetes: "1.37.1"
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
    assert c.node_image == "ghcr.io/tarokolabs/tk8s/node:v1.37.1"
    assert model.dumps(c) == GOOD  # quoted version, indented lists, key order


def test_status_is_dumped_after_spec() -> None:
    c = model.load_text(GOOD).model_copy(
        update={"status": model.Status(phase="Creating", tk8s="v1")}
    )
    text = model.dumps(c)
    assert text.endswith("status:\n  phase: Creating\n  completed: {}\n  tk8s: v1\n")
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
