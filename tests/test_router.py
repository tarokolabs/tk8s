import json

import pytest

from tests.fixtures import (
    OP,
    SHOW_HAPROXY,
    VBASH,
    VTYSH_ROUTE,
    VTYSH_SUMMARY,
    bgp_router_answers,
)
from tk8s import model
from tk8s.create import router
from tk8s.host import FakeHost, Result
from tk8s.phases import Context, PhaseFailed


def booted(host: FakeHost, haproxy: str = SHOW_HAPROXY) -> None:
    """VyOS finished its boot-time load and lists the demo control plane."""
    host.responses[("podman", "exec", "demo-router", "cat", "/tmp/vyos-config-status")] = Result(
        0, "0\n", ""
    )
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(
        0, haproxy, ""
    )


def test_backends_reads_the_server_rows_of_the_api_backend(ctx: Context, host: FakeHost) -> None:
    two = SHOW_HAPROXY + "api                   demo-control-plane-2  DOWN       0 ms         4s\n"
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(0, two, "")
    assert router.backends(ctx) == [
        ("demo-control-plane-1", "UP"),
        ("demo-control-plane-2", "DOWN"),
    ]


def test_backends_is_empty_before_vyos_answers(ctx: Context, host: FakeHost) -> None:
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(1, "", "")
    assert router.backends(ctx) == []


def test_wait_needs_the_boot_marker_and_every_control_plane_listed(
    ctx: Context, host: FakeHost
) -> None:
    host.responses[("podman", "exec", "demo-router", "cat", "/tmp/vyos-config-status")] = [
        Result(1, "", "No such file"),
        Result(0, "0\n", ""),
    ]
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(
        0, SHOW_HAPROXY, ""
    )
    ctx.options.poll = 0
    router.wait(ctx)
    assert "  demo-router: VyOS up, 1 control plane backend(s) at 172.22.0.253" in host.lines


def test_wait_names_the_router_logs_when_vyos_never_finishes(ctx: Context, host: FakeHost) -> None:
    ctx.options.poll = 0
    host.responses[("podman", "exec", "demo-router", "cat", "/tmp/vyos-config-status")] = Result(
        1, "", ""
    )
    with pytest.raises(PhaseFailed, match="demo-router did not finish booting within 60 s"):
        router.wait(ctx)


def test_reload_feeds_vyos_a_load_and_commit_script(ctx: Context, host: FakeHost) -> None:
    booted(host)
    host.responses[VBASH] = Result(0, "", "")
    router.reload(ctx, ctx.require_cluster())
    call = next(c for c in host.calls if c.argv[:3] == ["podman", "exec", "-i"])
    # the configuration session must run in the vyattacfg group (VyOS's own rule for scripts),
    # or the files it leaves under /opt/vyatta/config belong to root and later sessions fail
    assert call.argv[4:] == ["sg", "vyattacfg", "-c", "/bin/vbash -s"]
    assert call.sudo and call.input == (
        "cp /opt/vyatta/etc/config/tkctl.boot /opt/vyatta/etc/config/config.boot\n"
        "grep '^// ' /opt/vyatta/etc/config.boot.default >> /opt/vyatta/etc/config/config.boot\n"
        "source /opt/vyatta/etc/functions/script-template\n"
        "configure\n"
        "load /opt/vyatta/etc/config/config.boot\n"
        "loaded=$?\n"
        "commit\n"
        "exit\n"
        "builtin exit $loaded\n"
    )


def test_reload_waits_for_the_boot_commit_to_finish_first(ctx: Context, host: FakeHost) -> None:
    """A load alongside the boot-time commit fails with exit status 0 ("Failed to generate
    committed config"): the status file says when that commit is over."""
    ctx.options.poll = 0
    booted(host)
    host.responses[("podman", "exec", "demo-router", "cat", "/tmp/vyos-config-status")] = [
        Result(1, "", "No such file"),
        Result(0, "0\n", ""),
    ]
    host.responses[VBASH] = Result(0, "", "")
    router.reload(ctx, ctx.require_cluster())
    a = [c.argv for c in host.calls]
    status = [i for i, x in enumerate(a) if x[-1:] == ["/tmp/vyos-config-status"]]
    assert len(status) == 2 and status[-1] < a.index(list(VBASH))


def test_reload_fails_when_vyos_says_commit_failed_with_exit_0(
    ctx: Context, host: FakeHost
) -> None:
    booted(host)
    host.responses[VBASH] = Result(0, "[[system conntrack]] failed\nCommit failed\n", "")
    with pytest.raises(
        PhaseFailed, match=r"demo-router did not accept config\.boot: (?s:.*)Commit failed"
    ):
        router.reload(ctx, ctx.require_cluster())


def test_reload_fails_when_the_backends_do_not_follow(ctx: Context, host: FakeHost) -> None:
    """Neither load nor commit has a usable exit status in every case: the backend list is the
    proof the change took."""
    ctx.options.poll = 0
    stale = "api                   demo-control-plane-9  DOWN       0 ms      4s\n"
    booted(host, SHOW_HAPROXY + stale)
    host.responses[VBASH] = Result(0, "", "")
    with pytest.raises(
        PhaseFailed,
        match=r"demo-router lists demo-control-plane-1, demo-control-plane-9 as backends, "
        r"cluster\.yaml has demo-control-plane-1 \(podman exec demo-router .* show haproxy\)",
    ):
        router.reload(ctx, ctx.require_cluster())


def test_synced_compares_backend_names_with_the_control_planes(
    ctx: Context, host: FakeHost
) -> None:
    c = ctx.require_cluster()
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(
        0, SHOW_HAPROXY, ""
    )
    assert router.synced(ctx, c)
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(1, "", "")
    assert not router.synced(ctx, c)


def test_reload_fails_by_name_when_commit_fails(ctx: Context, host: FakeHost) -> None:
    booted(host)
    host.responses[VBASH] = Result(1, "", "Commit failed")
    with pytest.raises(PhaseFailed, match=r"demo-router did not accept config\.boot"):
        router.reload(ctx, ctx.require_cluster())


def test_neighbors_and_bfd_peers_render_one_block_per_control_plane(bgp_ctx: Context) -> None:
    c = bgp_ctx.require_cluster()
    n = router.neighbors(c)
    assert n.count("        neighbor 172.22.0.") == 3 and "neighbor 172.22.0.4 " not in n
    assert n.startswith(
        "        neighbor 172.22.0.1 {\n"
        "            address-family {\n"
        "                ipv4-unicast {\n"
        "                }\n"
        "            }\n"
        "            bfd {\n"
        "            }\n"
        '            remote-as "65001"\n'
        "            timers {\n"
        '                holdtime "9"\n'
        '                keepalive "3"\n'
        "            }\n"
        "        }\n"
        "        neighbor 172.22.0.2 {"
    )
    b = router.bfd_peers(c)
    assert b.count("        peer 172.22.0.") == 3
    assert b.startswith(
        "        peer 172.22.0.1 {\n"
        "            interval {\n"
        '                multiplier "3"\n'
        '                receive "300"\n'
        '                transmit "300"\n'
        "            }\n"
        "        }\n"
    )


def test_peers_maps_vtysh_summary_to_control_plane_names(bgp_ctx: Context, host: FakeHost) -> None:
    bgp_router_answers(host, established=2)
    assert router.peers(bgp_ctx, bgp_ctx.require_cluster()) == [
        ("demo-control-plane-1", "Established"),
        ("demo-control-plane-2", "Established"),
        ("demo-control-plane-3", "Active"),
    ]


def test_peers_lists_a_neighbor_cluster_yaml_does_not_know_by_address(
    bgp_ctx: Context, host: FakeHost
) -> None:
    bgp_router_answers(host)
    answer = host.responses[VTYSH_SUMMARY]
    assert isinstance(answer, Result)
    extra = json.loads(answer.stdout)
    extra["peers"]["172.22.0.9"] = {"state": "Active"}
    host.responses[VTYSH_SUMMARY] = Result(0, json.dumps(extra), "")
    assert ("172.22.0.9", "Active") in router.peers(bgp_ctx, bgp_ctx.require_cluster())


def test_peers_is_empty_when_vtysh_fails_or_answers_garbage(
    bgp_ctx: Context, host: FakeHost
) -> None:
    host.responses[VTYSH_SUMMARY] = Result(1, "", "bgpd is not running")
    assert router.peers(bgp_ctx, bgp_ctx.require_cluster()) == []
    host.responses[VTYSH_SUMMARY] = Result(0, "not json", "")
    assert router.peers(bgp_ctx, bgp_ctx.require_cluster()) == []


def test_nexthops_counts_the_ecmp_width_and_zero_without_a_route(
    bgp_ctx: Context, host: FakeHost
) -> None:
    c = bgp_ctx.require_cluster()
    bgp_router_answers(host, hops=2)
    assert router.nexthops(bgp_ctx, c) == 2
    bgp_router_answers(host, hops=0)
    assert router.nexthops(bgp_ctx, c) == 0


def test_routes_to_is_an_up_backend_in_proxy_mode(ctx: Context, host: FakeHost) -> None:
    c = ctx.require_cluster()
    key = ("podman", "exec", "demo-router", OP, "show", "haproxy")
    host.responses[key] = Result(0, SHOW_HAPROXY, "")
    assert router.routes_to(ctx, c, c.control_planes[0])
    gone = model.Node(
        role="control-plane", name="demo-control-plane-2", ip="172.22.0.4", cpu=2, memory="4g"
    )
    host.responses[key] = Result(
        0, SHOW_HAPROXY + "api                   demo-control-plane-2  DOWN       0 ms   4s\n", ""
    )
    assert not router.routes_to(ctx, c, gone)
    host.responses[key] = Result(0, SHOW_HAPROXY, "")
    assert not router.routes_to(ctx, c, gone)  # not listed at all


def test_routes_to_is_an_active_next_hop_in_bgp_mode(bgp_ctx: Context, host: FakeHost) -> None:
    c = bgp_ctx.require_cluster()
    bgp_router_answers(host, hops=2)
    assert router.routes_to(bgp_ctx, c, c.control_planes[0])
    assert not router.routes_to(bgp_ctx, c, c.control_planes[2])
    bgp_router_answers(host, hops=0)
    assert not router.routes_to(bgp_ctx, c, c.control_planes[0])


def test_show_path_names_the_haproxy_or_the_vip_route(ctx: Context, bgp_ctx: Context) -> None:
    assert router.show_path(ctx.require_cluster()) == f"{OP} show haproxy"
    assert router.show_path(bgp_ctx.require_cluster()) == "vtysh -c 'show ip route 172.23.0.1'"


def test_bfd_down_names_the_control_planes_whose_session_is_not_up(
    bgp_ctx: Context, host: FakeHost
) -> None:
    bgp_router_answers(host, bfd_up=1)
    assert router.bfd_down(bgp_ctx, bgp_ctx.require_cluster()) == [
        "demo-control-plane-2",
        "demo-control-plane-3",
    ]


def test_synced_in_bgp_mode_compares_peers_with_the_control_planes(
    bgp_ctx: Context, host: FakeHost
) -> None:
    c = bgp_ctx.require_cluster()
    bgp_router_answers(host, established=0)  # configured is enough; sessions come up later
    assert router.synced(bgp_ctx, c)
    assert router.members(bgp_ctx, c)[0] == ("demo-control-plane-1", "Active")
    one = {"peers": {"172.22.0.1": {"state": "Active"}}}
    host.responses[VTYSH_SUMMARY] = Result(0, json.dumps(one), "")
    assert not router.synced(bgp_ctx, c)


def test_wait_synced_in_bgp_mode_names_peers(bgp_ctx: Context, host: FakeHost) -> None:
    bgp_router_answers(host)
    router.wait(bgp_ctx)
    assert "  demo-router: VyOS up, 3 control plane peer(s) at 172.22.0.253" in host.lines
    host.responses[VTYSH_SUMMARY] = Result(0, "{}", "")
    with pytest.raises(
        PhaseFailed,
        match=r"demo-router lists nothing as peers, cluster\.yaml has demo-control-plane-1, "
        r"demo-control-plane-2, demo-control-plane-3 "
        r"\(podman exec demo-router vtysh -c 'show bgp summary'\)",
    ):
        router.wait(bgp_ctx)


def test_nexthops_counts_only_active_next_hops_and_survives_odd_json(
    bgp_ctx: Context, host: FakeHost
) -> None:
    c = bgp_ctx.require_cluster()
    bgp_router_answers(host)
    answer = host.responses[VTYSH_ROUTE]
    assert isinstance(answer, Result)
    data = json.loads(answer.stdout)
    data["172.23.0.1/32"][0]["nexthops"][2]["active"] = False
    host.responses[VTYSH_ROUTE] = Result(0, json.dumps(data), "")
    assert router.nexthops(bgp_ctx, c) == 2
    host.responses[VTYSH_ROUTE] = Result(0, json.dumps({"172.23.0.1/32": ["garbage"]}), "")
    assert router.nexthops(bgp_ctx, c) == 0
