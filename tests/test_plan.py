from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

import pytest

from tk8s import model
from tk8s.create.plan import (
    PlanError,
    expand_nodes,
    load_existing,
    pick_index,
    resolve,
    used_indices,
    validate,
)
from tk8s.paths import Paths

K8S = "1.37.1"
HOME = Path(__file__).resolve().parents[1]


def user(text: str) -> model.UserCluster:
    return model.load_user_text(text, "in.yaml")


def res(
    u: model.UserCluster,
    *,
    existing: model.Cluster | None = None,
    used: set[int] | None = None,
    allow_overlap: bool = False,
) -> model.Cluster:
    return resolve(
        u,
        k8s_default=K8S,
        existing=existing,
        used_indices=used or set(),
        allow_overlap=allow_overlap,
        tk8s_version="test",
    )


DEMO = (
    "metadata: {name: demo}\nspec:\n  nodes:\n    - {role: control-plane, cpu: 2, memory: 4G}\n"
    "    - {role: worker, count: 2, cpu: 2, memory: 4G}\n"
)


def test_resolve_fills_defaults_and_skips_a_used_index() -> None:
    c = res(user(DEMO), used={0})
    n = c.spec.network
    assert n.index == 1 and n.nodes == IPv4Network("172.22.1.0/24")
    assert n.pods == IPv4Network("10.244.8.0/21") and n.services == IPv4Network("10.98.1.0/24")
    assert n.gateway == IPv4Address("172.22.1.254") and n.router == IPv4Address("172.22.1.253")
    assert n.ha == "proxy"
    assert n.lb_range == "172.22.1.200-172.22.1.219"
    assert [x.name for x in c.spec.nodes] == [
        "demo-control-plane-1",
        "demo-worker-1",
        "demo-worker-2",
    ]
    assert str(c.spec.nodes[2].ip) == "172.22.1.3" and c.spec.nodes[2].memory == "4g"
    assert c.spec.kubernetes == K8S and all(x.join for x in c.spec.nodes)
    assert c.status is not None and c.status.phase == "Creating" and c.status.tk8s == "test"
    assert "runtime" not in model.dumps(c) and "vip" not in model.dumps(c)


def test_ha_and_deferred_join() -> None:
    c = res(
        user(
            "metadata: {name: defer}\nspec:\n  nodes: [{role: control-plane}, "
            "{role: control-plane, count: 2, join: false}]\n"
        )
    )
    assert [x.name for x in c.control_planes] == [
        "defer-control-plane-1",
        "defer-control-plane-2",
        "defer-control-plane-3",
    ]
    assert [x.join for x in c.spec.nodes] == [True, False, False]
    assert c.spec.network.router == IPv4Address("172.22.0.253")


def test_resume_keeps_index_and_resolved_datapath() -> None:
    first = res(user(DEMO), used={0, 1, 2, 3, 4, 5, 6})
    assert first.spec.network.index == 7
    recorded = first.model_copy(
        update={"spec": first.spec.model_copy(update={"datapath_resolved": "netkit"})}
    )
    again = res(user(DEMO), existing=recorded, used={0, 7})
    assert again.spec.network.index == 7 and again.spec.datapath_resolved == "netkit"
    assert again.status == recorded.status


def test_explicit_overlapping_subnet_is_refused_unless_allowed() -> None:
    u = user(
        "metadata: {name: clash}\nspec:\n  network: {nodes: 172.22.0.0/24}\n"
        "  nodes: [{role: control-plane}]\n"
    )
    with pytest.raises(
        PlanError,
        match=r"subnet 172\.22\.0\.0/24 is already used by another cluster; pass --allow-overlap",
    ):
        res(u, used={0})
    assert res(u, used={0}, allow_overlap=True).spec.network.nodes == IPv4Network("172.22.0.0/24")


def test_explicit_subnet_must_be_172_22_n() -> None:
    u = user(
        "metadata: {name: x}\nspec:\n  network: {nodes: 10.0.0.0/24}\n"
        "  nodes: [{role: control-plane}]\n"
    )
    with pytest.raises(PlanError, match=r"spec\.network\.nodes must be 172\.22\.N\.0/24"):
        res(u)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("  nodes: [{role: control-plane}, {role: worker, memory: 4Gi}]", "4096M"),
        ("  nodes: [{role: control-plane}, {role: worker, name: bad-big, count: 2}]", "count"),
        (
            "  nodes: [{role: control-plane}, {role: worker, name: bad-x}, "
            "{role: worker, name: bad-x}]",
            "bad-x",
        ),
        ("  nodes: [{role: worker, count: 2}]", "control-plane"),
        ("  nodes: [{role: worker}, {role: control-plane}]", "first"),
        ("  nodes: [{role: control-plane, count: 2}]", "odd"),
        ("  nodes: [{role: control-plane, count: 7}]", "at most 5"),
        ("  gvisor: true\n  datapath: netkit\n  nodes: [{role: control-plane}]", "netkit"),
        ("  nodes: []", "at least one control-plane"),
    ],
)
def test_invalid_input_is_refused_with_the_old_message(body: str, message: str) -> None:
    with pytest.raises(PlanError, match=message):
        validate(user(f"metadata: {{name: bad}}\nspec:\n{body}\n"))


def test_gvisor_without_datapath_resolves_to_veth() -> None:
    c = res(user("metadata: {name: gv}\nspec:\n  gvisor: true\n  nodes: [{role: control-plane}]\n"))
    assert c.spec.datapath == "veth" and c.spec.gvisor


def test_explicit_memory_and_names_are_kept_lower_cased() -> None:
    c = res(
        user(
            "metadata: {name: good}\nspec:\n  nodes: [{role: control-plane, memory: 4096M}, "
            "{role: worker, name: good-big, cpu: 8}]\n"
        )
    )
    assert c.spec.nodes[0].memory == "4096m"
    assert c.spec.nodes[1].name == "good-big" and c.spec.nodes[1].cpu == 8


def test_router_pin_kept_and_range_enforced() -> None:
    ok = user(
        "metadata: {name: pin}\nspec:\n  network: {nodes: 172.22.9.0/24, router: 172.22.9.240}\n"
        "  nodes: [{role: control-plane}]\n"
    )
    assert res(ok).spec.network.router == IPv4Address("172.22.9.240")
    bad = user(
        "metadata: {name: clash}\nspec:\n  network: {nodes: 172.22.9.0/24, router: 172.22.9.99}\n"
        "  nodes: [{role: control-plane}]\n"
    )
    with pytest.raises(PlanError, match=r"172\.22\.9\.220-253"):
        res(bad)


def test_node_addresses_run_from_1_to_199_without_a_hole() -> None:
    """The router lives at .253, outside the node range; .100 is a node address like any other."""
    groups = [model.NodeGroup(role="control-plane"), model.NodeGroup(role="worker", count=100)]
    ips = [str(n.ip) for n in expand_nodes("big", groups, 1)]
    assert ips[99] == "172.22.1.100" and ips[100] == "172.22.1.101"


def test_index_and_existing_come_from_the_clusters_dir(tmp_path: Path) -> None:
    paths = Paths(tmp_path, tmp_path / "wulin", HOME)
    (tmp_path / "clusters" / "a").mkdir(parents=True)
    a = res(user("metadata: {name: a}\nspec:\n  nodes: [{role: control-plane}]\n"), used={1})
    model.save(a, tmp_path / "clusters" / "a" / "cluster.yaml")
    assert used_indices(paths) == {0}
    assert load_existing(paths, "a") is not None and load_existing(paths, "b") is None
    assert pick_index(user(DEMO), None, used_indices(paths)) == 1


def test_plan_done_means_the_file_holds_this_spec(tmp_path: Path) -> None:
    from tk8s import versions
    from tk8s.create import plan as plan_phase
    from tk8s.host import FakeHost
    from tk8s.phases import Context, Options

    host = FakeHost()
    paths = Paths(tmp_path, tmp_path / "wulin", HOME)
    ctx = Context(
        host=host,
        paths=paths,
        versions=versions.load(HOME / "versions.yaml"),
        options=Options(),
        say=host.lines.append,
    )
    ctx.cluster = res(user(DEMO))
    assert not plan_phase.done(ctx)
    plan_phase.run(ctx)
    assert plan_phase.done(ctx)
    ctx.cluster = res(user(DEMO.replace("count: 2", "count: 3")), existing=ctx.cluster)
    assert not plan_phase.done(ctx)  # the file holds the old topology


def test_used_indices_tolerates_a_file_from_an_older_tk8s(tmp_path: Path) -> None:
    paths = Paths(tmp_path, tmp_path / "wulin", HOME)
    (tmp_path / "clusters" / "old").mkdir(parents=True)
    (tmp_path / "clusters" / "old" / "cluster.yaml").write_text(
        "metadata: {name: old}\nspec:\n  runtime: crio\n  network: {index: 3}\n  nodes: []\n"
    )
    (tmp_path / "clusters" / "junk").mkdir()
    (tmp_path / "clusters" / "junk" / "cluster.yaml").write_text("not: [valid")
    assert used_indices(paths) == {3}
