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
    lb: 172.22.3.100
    lb_range: 172.22.3.200-172.22.3.219
    pods: 10.244.24.0/21
    services: 10.98.3.0/24
  nodes:
    - role: control-plane
      name: demo-cp-1
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
    assert c.first_control_plane.name == "demo-cp-1" and len(c.workers) == 1
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


def test_save_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "cluster.yaml"
    p.write_text("old: file\n")
    c = model.load_text(GOOD)

    def boom(self: Path, target: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError):
        model.save(c, p)
    assert p.read_text() == "old: file\n"  # the old file is intact; only the .tmp was touched


def test_missing_status_with_marker_is_ready(tmp_path: Path) -> None:
    p = tmp_path / "cluster.yaml"
    p.write_text(GOOD)
    assert model.load(p).status is None
    c = model.load(p, create_complete_marker=True)
    assert c.status is not None and c.status.phase == "Ready" and c.status.tk8s == "pre-status"


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
            "metadata: {name: x}\nspec: {runtime: crio, nodes: [{role: control-plane}]}\n",
            "spec.runtime is not a field any more",
        ),
        (
            "metadata: {name: x}\nspec: {network: {nodes: 172.22.7.0/24, vip: 172.22.7.100}, "
            "nodes: [{role: control-plane}]}\n",
            "spec.network.vip is no longer supported",
        ),
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


def test_user_network_accepts_lb_pin() -> None:
    u = model.UserCluster.model_validate(
        {
            "metadata": {"name": "pin"},
            "spec": {
                "network": {"nodes": "172.22.9.0/24", "lb": "172.22.9.240"},
                "nodes": [{"role": "control-plane"}],
            },
        }
    )
    assert u.spec.network is not None and u.spec.network.lb == IPv4Address("172.22.9.240")


def test_node_request_is_frozen_and_defaults_to_join() -> None:
    from pydantic import ValidationError

    r = model.NodeRequest(role="worker")
    assert r.cpu is None and r.memory is None and r.join is True
    with pytest.raises(ValidationError):
        model.NodeRequest(role="worker", cpu=0)
