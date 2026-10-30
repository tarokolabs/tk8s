"""connect and disconnect: what cilium-cli is asked, in which kubeconfig environment, what is
recorded in every cluster.yaml, what is printed."""

from pathlib import Path

import pytest

from tests.fixtures import argvs, mesh_ctx
from tk8s import lifecycle, model
from tk8s.host import FakeHost, Result
from tk8s.mesh import MeshError, ops
from tk8s.phases import Context

DNS_POD = ("kubectl", "-n", "kube-system", "get", "pod", "-l", "k8s-app=kube-dns")
APISERVER_SVC = ("kubectl", "-n", "kube-system", "get", "svc", "clustermesh-apiserver")


def ready(ctx: Context, host: FakeHost, pod_ip: str) -> None:
    """The answers a healthy, already-enabled cluster gives."""
    host.responses[("cilium", "version", "--client")] = Result(0, "cilium-cli: v0.20.2", "")
    host.responses[APISERVER_SVC] = Result(0, str(ctx.require_cluster().spec.network.mesh), "")
    host.responses[DNS_POD] = Result(0, pod_ip, "")


def pair(tmp_path: Path) -> tuple[Context, FakeHost, Context, FakeHost]:
    a, ha = mesh_ctx(tmp_path, "demo", 0)
    b, hb = mesh_ctx(tmp_path, "other", 1)
    ready(a, ha, "10.244.0.5")
    ready(b, hb, "10.244.8.5")
    return a, ha, b, hb


def peers(host: FakeHost, ctx: Context) -> list[str]:
    c = model.load_text(host.files[ctx.state_dir / "cluster.yaml"])
    assert c.status is not None
    return c.status.mesh


def test_connect_enables_connects_waits_probes_and_records(tmp_path: Path) -> None:
    a, ha, b, hb = pair(tmp_path)
    ha.responses[APISERVER_SVC] = [Result(0, "", ""), Result(0, "172.22.0.200", "")]  # not yet
    ops.connect([a, b])
    argv = argvs(ha)
    assert [
        "cilium",
        "clustermesh",
        "enable",
        "--context",
        "demo",
        "--service-type",
        "LoadBalancer",
    ] in argv
    enable = next(c for c in ha.calls if c.argv[:3] == ["cilium", "clustermesh", "enable"])
    assert enable.env == {"KUBECONFIG": f"{a.state_dir}/kubeconfig"}
    con = next(c for c in ha.calls if c.argv[:3] == ["cilium", "clustermesh", "connect"])
    assert con.argv[3:] == [
        "--context",
        "demo",
        "--destination-context",
        "other",
        "--connection-mode",
        "mesh",
        "--allow-mismatching-ca",
    ]
    assert con.env == {"KUBECONFIG": f"{a.state_dir}/kubeconfig:{b.state_dir}/kubeconfig"}
    status = [c for c in ha.calls if c.argv[:3] == ["cilium", "clustermesh", "status"]]
    assert [c.argv[3:] for c in status] == [["--wait", "--wait-duration", "5m"]] * 2
    assert [c.env["KUBECONFIG"] for c in status] == [
        f"{a.state_dir}/kubeconfig",
        f"{b.state_dir}/kubeconfig",
    ]
    assert peers(ha, a) == ["other"] and peers(hb, b) == ["demo"]
    assert "  demo: peers other" in ha.lines and "  other: peers demo" in ha.lines
    assert ha.lines[-1] == "clusters demo, other connected"


def test_connect_skips_enable_when_the_apiserver_already_has_the_mesh_address(
    tmp_path: Path,
) -> None:
    a, ha, b, _ = pair(tmp_path)
    ops.connect([a, b])
    assert not any(c.argv[:3] == ["cilium", "clustermesh", "enable"] for c in ha.calls)
    assert "  demo: already enabled" in ha.lines and "  other: already enabled" in ha.lines


def test_connect_waits_for_the_mesh_address_and_names_the_service_when_it_never_comes(
    tmp_path: Path,
) -> None:
    a, ha, b, _ = pair(tmp_path)
    ha.responses[APISERVER_SVC] = Result(0, "", "")
    with pytest.raises(
        MeshError,
        match=r"cluster demo: clustermesh-apiserver did not get 172.22.0.200 within 2 minutes "
        r"\(kubectl -n kube-system get svc clustermesh-apiserver\)",
    ):
        ops.connect([a, b])
    assert not any(c.argv[:3] == ["cilium", "clustermesh", "connect"] for c in ha.calls)


def test_connect_fails_by_name_when_clustermesh_status_never_settles(tmp_path: Path) -> None:
    a, ha, b, _ = pair(tmp_path)
    ha.responses[("cilium", "clustermesh", "status")] = Result(1, "", "waiting")
    with pytest.raises(
        MeshError,
        match=r"cluster demo: clustermesh not ready within 5 minutes "
        r"\(see /tmp/demo-clustermesh.out\)",
    ):
        ops.connect([a, b])
    assert ha.files[Path("/tmp/demo-clustermesh.out")] == "waiting"


def test_connect_records_the_mesh_even_when_a_probe_fails_and_exits_nonzero(
    tmp_path: Path,
) -> None:
    a, ha, b, hb = pair(tmp_path)
    hb.responses[("podman", "exec", "other-control-plane-1", "ping")] = Result(1, "", "")
    with pytest.raises(MeshError, match=r"1 probe\(s\) failed; the mesh is up but traffic is not"):
        ops.connect([a, b])
    assert (
        "  FAIL  other -> demo: node 172.22.0.1 unreachable from other-control-plane-1" in ha.lines
    )
    assert peers(ha, a) == ["other"] and peers(hb, b) == ["demo"]


def test_connect_three_clusters_sends_every_destination_at_once(tmp_path: Path) -> None:
    a, ha, b, hb = pair(tmp_path)
    c, hc = mesh_ctx(tmp_path, "third", 2)
    ready(c, hc, "10.244.16.5")
    ops.connect([a, b, c])
    con = next(x for x in ha.calls if x.argv[:3] == ["cilium", "clustermesh", "connect"])
    assert con.argv[3:7] == ["--context", "demo", "--destination-context", "other,third"]
    assert peers(ha, a) == ["other", "third"]
    assert peers(hb, b) == ["demo", "third"]
    assert peers(hc, c) == ["demo", "other"]


def test_members_closes_over_recorded_peers_and_refuses_a_missing_one(tmp_path: Path) -> None:
    a, _, b, _ = pair(tmp_path)
    c, _ = mesh_ctx(tmp_path, "third", 2)
    for ctx, peer in ((b, "third"), (c, "other")):
        cl = ctx.require_cluster()
        assert cl.status is not None
        status = cl.status.model_copy(update={"mesh": [peer]})
        ctx.cluster = cl.model_copy(update={"status": status})
    table = {"demo": a, "other": b, "third": c}

    def ctx_of(name: str) -> Context:
        if name not in table:
            raise lifecycle.ClusterNotFound(f"cluster {name} not found")
        return table[name]

    assert [x.require_cluster().name for x in ops.members(ctx_of, ["demo", "other"])] == [
        "demo",
        "other",
        "third",
    ]
    del table["third"]
    with pytest.raises(
        MeshError,
        match=r"cluster third \(a peer of other\) not found; disconnect it first: "
        r"tkctl disconnect cluster other third",
    ):
        ops.members(ctx_of, ["demo", "other"])
