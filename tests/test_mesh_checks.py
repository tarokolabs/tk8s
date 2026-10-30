"""connect's preflight: what it refuses, by name, before touching any cluster."""

from pathlib import Path

import pytest

from tests.fixtures import make_multi_ctx, mesh_ctx
from tk8s import model
from tk8s.host import FakeHost
from tk8s.mesh import MeshError, checks
from tk8s.phases import Context


def pair(tmp_path: Path) -> tuple[Context, FakeHost, Context, FakeHost]:
    a, ha = mesh_ctx(tmp_path, "demo", 0)
    b, hb = mesh_ctx(tmp_path, "other", 1)
    return a, ha, b, hb


def with_status(ctx: Context, **update: object) -> None:
    c = ctx.require_cluster()
    assert c.status is not None
    ctx.cluster = c.model_copy(update={"status": c.status.model_copy(update=update)})


def test_preflight_passes_two_ready_running_clusters(tmp_path: Path) -> None:
    a, _, b, _ = pair(tmp_path)
    checks.preflight([a, b])


def test_preflight_refuses_a_stopped_cluster_by_name(tmp_path: Path) -> None:
    a, _, b, hb = pair(tmp_path)
    hb.active.clear()
    with pytest.raises(
        MeshError, match=r"cluster other is not running \(tkctl start cluster other\)"
    ):
        checks.preflight([a, b])


def test_preflight_refuses_a_cluster_from_before_mesh_support(tmp_path: Path) -> None:
    a, _, b, _ = pair(tmp_path)
    with_status(b, cluster_id=None)
    with pytest.raises(
        MeshError,
        match=r"cluster other was created before mesh support; recreate it to connect",
    ):
        checks.preflight([a, b])


def test_preflight_refuses_mixed_kinds(tmp_path: Path, host: FakeHost) -> None:
    a, _ = mesh_ctx(tmp_path, "demo", 0)
    b, hosts = make_multi_ctx(host, tmp_path)
    c = b.require_cluster()
    b.cluster = c.model_copy(
        update={
            "status": model.Status(
                phase="Ready", tk8s="test", cluster_id=1, cilium=b.versions.cilium, operator="op"
            )
        }
    )
    b.cluster = b.cluster.model_copy(update={"metadata": model.Metadata(name="other")})
    for h in hosts.values():
        h.active.add("other.target")
    with pytest.raises(MeshError, match=r"demo is single-host and other is multi-host"):
        checks.preflight([a, b])


def test_preflight_refuses_a_shared_cluster_id(tmp_path: Path) -> None:
    a, _, _, _ = pair(tmp_path)
    b, _ = mesh_ctx(tmp_path, "other", 0)  # --allow-overlap gave it index 0 too
    with pytest.raises(MeshError, match=r"share cluster id 1 \(subnet index 0\)"):
        checks.preflight([a, b])


def test_preflight_allows_one_minor_of_cilium_skew_and_refuses_two(tmp_path: Path) -> None:
    a, _, b, _ = pair(tmp_path)
    with_status(a, cilium="1.20.4")
    with_status(b, cilium="1.19.9")
    checks.preflight([a, b])
    with_status(b, cilium="1.18.3")
    with pytest.raises(MeshError, match=r"runs Cilium 1.20.4 and other 1.18.3"):
        checks.preflight([a, b])


def test_minor_parses_a_version() -> None:
    assert checks.minor("1.20.4") == (1, 20)


def test_preflight_refuses_a_cluster_without_a_mesh_address(tmp_path: Path) -> None:
    """A pre-feature cluster.yaml resumed through cni after the upgrade has a cluster id but no
    reserved address and no mesh pool: one predicate decides, everywhere."""
    a, _, b, _ = pair(tmp_path)
    c = b.require_cluster()
    net = c.spec.network.model_copy(update={"mesh": None})
    b.cluster = c.model_copy(update={"spec": c.spec.model_copy(update={"network": net})})
    with pytest.raises(
        MeshError, match=r"cluster other was created before mesh support; recreate it to connect"
    ):
        checks.preflight([a, b])
    assert not checks.can_federate(b.require_cluster())
    assert checks.can_federate(a.require_cluster())
