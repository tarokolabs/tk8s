from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

import pytest

from tests.fixtures import (
    DEMO_USER,
    MULTI_LANS,
    MULTI_YAML,
    bgp_user,
    multi_user,
    resolved,
    two_router_user,
)
from tk8s import model
from tk8s.create.plan import (
    PlanError,
    allocate,
    expand_nodes,
    load_existing,
    majority_host,
    normalize_range,
    parse_range,
    pick_index,
    place,
    place_routers,
    ranges_text,
    used_indices,
    validate,
)
from tk8s.hosts import LanFacts
from tk8s.paths import Paths

K8S = "1.37.2"
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
    return resolved(u, existing=existing, used_indices=used, allow_overlap=allow_overlap)


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
    assert n.lb_range == "172.22.1.201-172.22.1.219"
    assert n.mesh == IPv4Address("172.22.1.200")
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


def test_bgp_mode_allocates_the_vip_in_the_other_slash_16() -> None:
    """The VIP must not be in the node subnet: there the nodes would ARP for it instead of routing
    to the router, and the BGP demonstration would be void."""
    c = res(
        user(
            "metadata: {name: bgp}\nspec:\n  network: {nodes: 172.22.9.0/24, ha: bgp}\n"
            "  nodes: [{role: control-plane}]\n"
        )
    )
    assert c.spec.network.ha == "bgp" and str(c.spec.network.vip) == "172.23.9.1"
    proxy = res(user("metadata: {name: p}\nspec:\n  nodes: [{role: control-plane}]\n"))
    assert proxy.spec.network.ha == "proxy" and proxy.spec.network.vip is None


def test_vip_pin_must_be_in_the_other_slash_16() -> None:
    ok = user(
        "metadata: {name: pin}\nspec:\n"
        "  network: {nodes: 172.22.9.0/24, ha: bgp, vip: 172.23.9.77}\n"
        "  nodes: [{role: control-plane}]\n"
    )
    assert str(res(ok).spec.network.vip) == "172.23.9.77"
    bad = user(
        "metadata: {name: pin}\nspec:\n"
        "  network: {nodes: 172.22.9.0/24, ha: bgp, vip: 172.22.9.100}\n"
        "  nodes: [{role: control-plane}]\n"
    )
    with pytest.raises(PlanError, match=r"172\.23\.9\.0/24"):
        res(bad)


def test_vip_is_refused_in_proxy_mode() -> None:
    bad = user(
        "metadata: {name: p}\nspec:\n  network: {nodes: 172.22.9.0/24, vip: 172.23.9.1}\n"
        "  nodes: [{role: control-plane}]\n"
    )
    with pytest.raises(PlanError, match=r"spec\.network\.vip .* ha: bgp"):
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
    (tmp_path / "clusters" / "a" / "cluster.yaml").write_text(model.dumps(a))
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


def test_a_creating_cluster_is_resumed(tmp_path: Path) -> None:
    paths = Paths(tmp_path, tmp_path / "wulin", HOME)
    c = res(user(DEMO))  # status Creating
    state = paths.state_dir("demo")
    state.mkdir(parents=True)
    (state / "cluster.yaml").write_text(model.dumps(c))
    existing = load_existing(paths, "demo")
    assert existing is not None and existing.status is not None
    assert existing.status.phase == "Creating" and existing.spec.network.index == 0


# ---- routers


def test_two_routers_are_named_and_addressed_below_the_floating_address() -> None:
    c = resolved(two_router_user())
    assert c.spec.network.routers == 2 and c.floating_ip == IPv4Address("172.22.0.253")
    assert [(r.name, r.dir, str(r.ip), r.priority) for r in c.routers] == [
        ("demo-router-1", "router-1", "172.22.0.251", 200),
        ("demo-router-2", "router-2", "172.22.0.252", 100),
    ]


def test_one_router_keeps_todays_name_and_address() -> None:
    c = resolved(DEMO_USER)
    assert c.spec.network.routers == 1
    assert [(r.name, r.dir, str(r.ip), r.priority) for r in c.routers] == [
        ("demo-router", "router", "172.22.0.253", None)
    ]


def test_the_floating_address_cannot_be_a_router_body_with_two_routers() -> None:
    user = model.UserCluster(
        metadata=model.Metadata(name="demo"),
        spec=model.UserSpec(
            network=model.UserNetwork(ha="bgp", routers=2, router=IPv4Address("172.22.0.252")),
            nodes=[model.NodeGroup(role="control-plane")],
        ),
    )
    with pytest.raises(
        PlanError,
        match=r"spec\.network\.router 172\.22\.0\.252 is a router itself with routers: 2 "
        r"\(\.251 and \.252\); pick another 172\.22\.0\.220-253",
    ):
        resolved(user)


def test_a_cluster_yaml_without_routers_loads_as_one_router() -> None:
    text = model.dumps(resolved(DEMO_USER)).replace("    routers: 1\n", "")
    assert "routers" not in text
    assert model.load_text(text).spec.network.routers == 1


def test_cluster_yaml_writes_routers_right_after_ha() -> None:
    assert "    ha: proxy\n    routers: 1\n" in model.dumps(resolved(DEMO_USER))
    assert "    ha: bgp\n    routers: 2\n    vip: 172.23.0.1\n" in model.dumps(
        resolved(two_router_user())
    )


def test_resume_refuses_a_different_router_count() -> None:
    """An interrupted create resumed with another --routers would render the new routers next to
    the old one's unit, two claimants of the floating address: refuse, point at delete."""
    recorded = resolved(bgp_user())
    with pytest.raises(
        PlanError,
        match=r"cluster demo is being created with routers: 1; rerun with the same --routers "
        r"\(or --preset\), or delete it first: tkctl delete cluster demo",
    ):
        resolved(two_router_user(), existing=recorded)


def test_a_new_cluster_is_marked_node_capacity_and_a_resume_keeps_its_status() -> None:
    fresh = res(user(DEMO))
    assert fresh.status is not None and fresh.status.node_capacity is True
    old = fresh.model_copy(update={"status": model.Status(phase="Creating", tk8s="old")})
    resumed = res(user(DEMO), existing=old)
    assert resumed.status is not None and resumed.status.node_capacity is False


def test_memory_below_2g_is_refused() -> None:
    body = "metadata: {name: demo}\nspec:\n  nodes:\n    - {role: control-plane, memory: 1G}\n"
    with pytest.raises(PlanError, match=r'node memory "1G" is below the minimum 2G'):
        validate(user(body))


# ---- multi-host: placement and the address range


def ip(last: int) -> IPv4Address:
    return IPv4Address(f"192.168.1.{last}")


def test_parse_range_takes_several_segments_with_a_short_end() -> None:
    assert parse_range("192.168.1.64-192.168.1.66") == [ip(64), ip(65), ip(66)]
    assert parse_range("192.168.1.64-66,192.168.1.80-81") == [
        ip(64),
        ip(65),
        ip(66),
        ip(80),
        ip(81),
    ]
    assert normalize_range("192.168.1.64-66, 192.168.1.80-81") == (
        "192.168.1.64-192.168.1.66,192.168.1.80-192.168.1.81"
    )
    assert ranges_text([ip(85), ip(86), ip(87), ip(89)]) == "192.168.1.85-192.168.1.87,192.168.1.89"
    with pytest.raises(PlanError, match="the end is before the start"):
        parse_range("192.168.1.95-192.168.1.64")
    with pytest.raises(PlanError, match=r"spec\.network\.range must be A-B"):
        parse_range("192.168.1.64-nope")
    with pytest.raises(PlanError, match=r"192\.168\.1\.66 is listed twice"):
        parse_range("192.168.1.64-66,192.168.1.66-70")


def test_allocate_cuts_the_pool_in_the_fixed_order() -> None:
    pool = parse_range("192.168.1.64-70,192.168.1.80-89")  # 17 addresses
    a = allocate(pool, shim=True, routers=2, nodes=5)
    assert a.router == ip(64) and a.routers == [ip(65), ip(66)] and a.shim == ip(67)
    assert a.nodes == [ip(68), ip(69), ip(70), ip(80), ip(81)]
    assert a.lb_range == "192.168.1.85-192.168.1.89"
    assert a.mesh == ip(84)  # just before the LoadBalancer tail
    assert a.free == [ip(82), ip(83)]
    one = allocate(pool, shim=False, routers=1, nodes=1)
    assert one.routers == [] and one.shim is None and one.nodes == [ip(65)]
    tail = allocate(parse_range("192.168.1.64-70,192.168.1.80-82"), shim=False, routers=1, nodes=3)
    assert tail.mesh == ip(68) and tail.free == []
    assert tail.lb_range == "192.168.1.69-192.168.1.70,192.168.1.80-192.168.1.82"  # the last 5


def test_allocate_refuses_a_pool_too_small_with_the_count_it_needs() -> None:
    with pytest.raises(
        PlanError,
        match=r"range 192.168.1.64-192.168.1.71 has 8 addresses; this cluster needs 15 "
        r"\(1 router address, 2 router container addresses, 1 operator shim, 5 nodes, "
        r"1 Cluster Mesh address, 5 LoadBalancer addresses\)",
    ):
        allocate(parse_range("192.168.1.64-71"), shim=True, routers=2, nodes=5)


def cp(count: int = 1, host: str | None = None) -> model.NodeGroup:
    return model.NodeGroup(role="control-plane", count=count, host=host)


def worker(count: int = 1, host: str | None = None) -> model.NodeGroup:
    return model.NodeGroup(role="worker", count=count, host=host)


def test_place_round_robins_by_role_and_keeps_fixed_hosts_out_of_the_rotation() -> None:
    assert place([cp(3), worker(2, host="c"), worker(2)], ["a", "b", "c"]) == [
        "a",
        "b",
        "c",
        "c",
        "c",
        "a",
        "b",
    ]
    assert place([cp(), worker(2, host="auto")], ["x", "y"]) == ["x", "y", "x"]
    assert place_routers(None, ["a", "b", "c"], 2) == ["a", "b"]
    assert place_routers(None, ["a"], 2) == ["a", "a"]
    assert place_routers(["c", "a"], ["a", "b", "c"], 2) == ["c", "a"]


def test_majority_host_names_the_host_holding_more_than_half() -> None:
    assert majority_host(["a", "a", "b"]) == "a"
    assert majority_host(["a", "b", "c"]) is None
    assert majority_host(["a"]) == "a"


def test_resolve_multi_host_fills_hosts_placement_and_addresses() -> None:
    c = resolved(multi_user(), lans=MULTI_LANS, operator="op")
    assert c == model.load_text(MULTI_YAML)
    assert [n.host for n in c.spec.nodes] == ["a", "b", "c", "a", "b"]
    assert c.spec.hosts is not None
    assert [h.iface for h in c.spec.hosts] == ["ens18", "ens18", "eth0"]
    assert str(c.spec.network.shim) == "192.168.1.67"
    assert c.spec.network.range == "192.168.1.64-192.168.1.70,192.168.1.80-192.168.1.89"


def test_resolve_without_the_operator_among_the_hosts_reserves_no_shim() -> None:
    u = multi_user()
    hosts = [model.UserHost(name="a", ssh="tux@192.168.1.230"), *(u.spec.hosts or [])[1:]]
    u = u.model_copy(update={"spec": u.spec.model_copy(update={"hosts": hosts})})
    c = resolved(u, lans=MULTI_LANS, operator="op")
    assert c.spec.network.shim is None and str(c.spec.nodes[0].ip) == "192.168.1.67"


def test_resolve_multi_host_needs_the_lan_facts() -> None:
    with pytest.raises(PlanError, match="needs the hosts' LAN facts"):
        resolved(multi_user())


def test_hosts_on_different_subnets_or_gateways_are_refused() -> None:
    lans = dict(MULTI_LANS)
    lans["c"] = LanFacts("eth0", IPv4Network("10.0.0.0/24"), IPv4Address("10.0.0.1"), ip(1))
    with pytest.raises(PlanError, match=r"host c is on 10\.0\.0\.0/24 via 10\.0\.0\.1, host a on"):
        resolved(multi_user(), lans=lans, operator="op")


def test_resume_with_different_lan_facts_or_hosts_is_refused() -> None:
    existing = model.load_text(MULTI_YAML)
    other = dict(MULTI_LANS)
    other["a"] = LanFacts("eth9", MULTI_LANS["a"].subnet, MULTI_LANS["a"].gateway, ip(230))
    with pytest.raises(PlanError, match=r"says host a on ens18, the host now says eth9"):
        resolved(multi_user(), existing=existing, lans=other, operator="op")
    again = resolved(multi_user(), existing=existing, lans=MULTI_LANS, operator="op")
    assert again.spec == existing.spec
    u = multi_user()
    hosts = [*(u.spec.hosts or [])[:2], model.UserHost(name="d", ssh="192.168.1.233")]
    moved = u.model_copy(update={"spec": u.spec.model_copy(update={"hosts": hosts})})
    lans = {**MULTI_LANS, "d": MULTI_LANS["c"]}
    with pytest.raises(PlanError, match="is being created on hosts a, b, c; rerun with the same"):
        resolved(moved, existing=existing, lans=lans, operator="op")
    fewer = u.model_copy(update={"spec": u.spec.model_copy(update={"nodes": u.spec.nodes[:1]})})
    with pytest.raises(PlanError, match="is being created with another placement"):
        resolved(fewer, existing=existing, lans=MULTI_LANS, operator="op")


def test_range_outside_the_lan_is_refused() -> None:
    far = {
        n: LanFacts("ens18", IPv4Network("10.0.0.0/24"), IPv4Address("10.0.0.1"), ip(1))
        for n in "abc"
    }
    with pytest.raises(PlanError, match=r"is outside the hosts' network 10\.0\.0\.0/24"):
        resolved(multi_user(), lans=far, operator="op")


def test_a_hosts_own_address_inside_the_range_is_refused() -> None:
    lans = dict(MULTI_LANS)
    lans["b"] = LanFacts("ens18", MULTI_LANS["b"].subnet, MULTI_LANS["b"].gateway, ip(69))
    with pytest.raises(PlanError, match=r"host b's own address 192\.168\.1\.69 is inside"):
        resolved(multi_user(), lans=lans, operator="op")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "  hosts: [{name: a}]\n  nodes: [{role: control-plane}]\n",
            "spec.hosts needs spec.network.range",
        ),
        (
            "  network: {range: 192.168.1.64-192.168.1.95}\n  nodes: [{role: control-plane}]\n",
            "spec.hosts needs spec.network.range",
        ),
        (
            "  hosts: [{name: a}]\n  gvisor: true\n"
            "  network: {range: 192.168.1.64-192.168.1.95}\n  nodes: [{role: control-plane}]\n",
            "gvisor is not supported on a multi-host cluster yet",
        ),
        (
            "  hosts: [{name: a}]\n"
            "  network: {range: 192.168.1.64-192.168.1.95, nodes: 172.22.0.0/24}\n"
            "  nodes: [{role: control-plane}]\n",
            "spec.network.nodes and router are allocated from spec.network.range",
        ),
        (
            "  hosts: [{name: a}]\n  network: {range: 192.168.1.64-192.168.1.95}\n"
            "  nodes: [{role: control-plane, host: zz}]\n",
            "node host zz is not in spec.hosts",
        ),
        (
            "  hosts: [{name: a}]\n"
            "  network: {range: 192.168.1.64-192.168.1.95, router_hosts: [zz]}\n"
            "  nodes: [{role: control-plane}]\n",
            "router host zz is not in spec.hosts",
        ),
    ],
)
def test_multi_host_user_mistakes_are_refused(body: str, message: str) -> None:
    with pytest.raises(PlanError, match=message):
        validate(user(f"metadata: {{name: m}}\nspec:\n{body}"))


def test_used_indices_on_a_remote_host_read_its_copies(tmp_path: Path) -> None:
    from tests.fixtures import MULTI_YAML
    from tk8s.create.plan import used_indices_on
    from tk8s.host import FakeHost

    paths = Paths(data_dir=tmp_path, wulin_dir=tmp_path / "wulin", home=HOME)
    h = FakeHost()
    h.dirs.update({paths.clusters_dir / "demo", paths.clusters_dir / "other"})
    h.files[paths.clusters_dir / "demo" / "cluster.yaml"] = MULTI_YAML
    h.files[paths.clusters_dir / "other" / "cluster.yaml"] = (
        "spec:\n  network:\n    index: 4\n  kubernetes: '1.37.2'\n"
    )
    assert used_indices_on(h, paths) == {0, 4}
    assert used_indices_on(FakeHost(), paths) == set()


def test_a_pool_touching_the_networks_own_addresses_is_refused() -> None:
    u = multi_user()
    net = model.UserNetwork(ha="bgp", routers=2, range="192.168.1.0-20")
    u = u.model_copy(update={"spec": u.spec.model_copy(update={"network": net})})
    with pytest.raises(PlanError, match=r"192\.168\.1\.0 is the network address of 192"):
        resolved(u, lans=MULTI_LANS, operator="op")
    net = model.UserNetwork(ha="bgp", routers=2, range="192.168.1.240-255")
    u = u.model_copy(update={"spec": u.spec.model_copy(update={"network": net})})
    with pytest.raises(PlanError, match=r"192\.168\.1\.255 is the broadcast address of 192"):
        resolved(u, lans=MULTI_LANS, operator="op")


def test_a_segment_may_be_a_single_address() -> None:
    assert parse_range("192.168.1.70-72,192.168.1.216") == [ip(70), ip(71), ip(72), ip(216)]
    assert normalize_range("192.168.1.216") == "192.168.1.216"
    u = multi_user()
    pool = "192.168.1.64-70,192.168.1.80-88,192.168.1.90"
    net = model.UserNetwork(ha="bgp", routers=2, range=pool)
    u = u.model_copy(update={"spec": u.spec.model_copy(update={"network": net})})
    c = resolved(u, lans=MULTI_LANS, operator="op")
    full = "192.168.1.64-192.168.1.70,192.168.1.80-192.168.1.88,192.168.1.90"
    assert c.spec.network.range == full
    assert c.spec.network.lb_range == "192.168.1.85-192.168.1.88,192.168.1.90"
    assert c.spec.network.mesh == ip(84)
