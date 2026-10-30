import pytest

from tk8s import model
from tk8s.create import frr
from tk8s.host import FakeHost, Result
from tk8s.phases import Context, PhaseFailed, Runner


def cp(ctx: Context, i: int) -> model.Node:
    return ctx.require_cluster().control_planes[i]


def node_answers(
    host: FakeHost,
    name: str,
    *,
    vip: str = "172.23.0.1",
    conf: str | None = None,
    active: bool = True,
) -> None:
    host.responses[("podman", "exec", name, "cat", "/etc/tk8s/vip")] = Result(0, vip + "\n", "")
    host.responses[("podman", "exec", name, "cat", "/etc/frr/frr.conf")] = Result(0, conf or "", "")
    for u in ("frr.service", "tk8s-apiserver-health.timer"):
        key = ("podman", "exec", name, "systemctl", "is-active", "--quiet", u)
        # with active=False frr.service answers inactive for the Runner's done() and for run()'s
        # own configured(), then active: what configure() leaves behind; the timer stays active
        # so the inactive answers are the ones seen
        inactive = not active and u == "frr.service"
        host.responses[key] = (
            [Result(3, "", "")] * 2 + [Result(0, "", "")] if inactive else Result(0, "", "")
        )


def test_config_announces_only_the_vip_to_the_router_with_bfd(bgp_ctx: Context) -> None:
    text = frr.config(bgp_ctx, cp(bgp_ctx, 1))
    assert "hostname demo-control-plane-2\n" in text and " bgp router-id 172.22.0.2\n" in text
    assert " neighbor 172.22.0.253 remote-as 65000\n" in text
    assert " neighbor 172.22.0.253 bfd\n" in text and " neighbor 172.22.0.253 timers 3 9\n" in text
    assert "  redistribute connected route-map vip-only\n" in text
    assert "  neighbor 172.22.0.253 route-map vip-only out\n" in text
    assert "ip prefix-list vip seq 10 permit 172.23.0.1/32\n" in text
    assert "  receive-interval 300\n  transmit-interval 300\n" in text
    assert "  detect-multiplier 3\n" in text
    assert "network " not in text  # the announcement is the address on lo, nothing static


def test_configure_writes_daemons_conf_vip_and_enables_the_units(
    bgp_ctx: Context, host: FakeHost
) -> None:
    n = cp(bgp_ctx, 0)
    frr.configure(bgp_ctx, n)
    a = [c.argv for c in host.calls]
    sed = "s/^bgpd=no/bgpd=yes/; s/^bfdd=no/bfdd=yes/"
    assert ["podman", "exec", n.name, "sed", "-i", sed, "/etc/frr/daemons"] in a
    conf = next(
        c for c in host.calls if c.argv[:6] == ["podman", "exec", "-i", n.name, "install", "-m"]
    )
    assert conf.argv[6:] == ["640", "-o", "frr", "-g", "frr", "/dev/stdin", "/etc/frr/frr.conf"]
    assert conf.sudo and conf.input == frr.config(bgp_ctx, n)
    vip = next(c for c in host.calls if c.argv[-1] == "/etc/tk8s/vip" and c.argv[2] == "-i")
    assert vip.argv[4:] == ["install", "-D", "-m", "644", "/dev/stdin", "/etc/tk8s/vip"]
    assert vip.input == "172.23.0.1\n"
    units = ["frr.service", "tk8s-apiserver-health.timer"]
    assert ["podman", "exec", n.name, "systemctl", "enable", "--quiet", *units] in a
    assert ["podman", "exec", n.name, "systemctl", "restart", *units] in a
    assert (
        "  demo-control-plane-1: FRR announces 172.23.0.1 to 172.22.0.253 while its apiserver "
        "is ready"
    ) in host.lines


def test_configure_takes_a_previous_vip_off_lo(bgp_ctx: Context, host: FakeHost) -> None:
    """A changed spec.network.vip: the old /32 would otherwise stay on lo (unannounced, the
    prefix-list only passes the new one, but there) until the node restarts."""
    n = cp(bgp_ctx, 0)
    node_answers(host, n.name, vip="172.23.0.9")
    frr.configure(bgp_ctx, n)
    a = [c.argv for c in host.calls]
    gone = ["podman", "exec", n.name, "ip", "addr", "del", "172.23.0.9/32", "dev", "lo"]
    assert gone in a and a.index(gone) < a.index(
        ["podman", "exec", n.name, "systemctl", "restart", *frr.UNITS]
    )
    host.calls.clear()
    node_answers(host, n.name)  # the same VIP, and a node that never had one
    frr.configure(bgp_ctx, n)
    host.responses.pop(("podman", "exec", n.name, "cat", "/etc/tk8s/vip"))
    frr.configure(bgp_ctx, n)
    assert not any(c.argv[3:6] == ["ip", "addr", "del"] for c in host.calls)


def test_configured_needs_the_vip_the_same_conf_and_both_units_active(
    bgp_ctx: Context, host: FakeHost
) -> None:
    n = cp(bgp_ctx, 0)
    node_answers(host, n.name, conf=frr.config(bgp_ctx, n))
    assert frr.configured(bgp_ctx, n)
    node_answers(host, n.name, conf=frr.config(bgp_ctx, n), vip="172.23.0.9")
    assert not frr.configured(bgp_ctx, n)
    node_answers(host, n.name, conf="router bgp 65001\n")
    assert not frr.configured(bgp_ctx, n)


def test_frr_half_configured_control_plane_is_not_done(bgp_ctx: Context, host: FakeHost) -> None:
    """vip written, units never enabled (an interrupted run): the phase must run again."""
    for i in range(3):
        node_answers(host, cp(bgp_ctx, i).name, conf=frr.config(bgp_ctx, cp(bgp_ctx, i)))
    node_answers(host, cp(bgp_ctx, 2).name, conf=frr.config(bgp_ctx, cp(bgp_ctx, 2)), active=False)
    Runner([frr.PHASE], record=False).run(bgp_ctx)  # done() is false, so the phase runs
    restarted = [c.argv[2] for c in host.calls if c.argv[3:5] == ["systemctl", "restart"]]
    assert restarted == ["demo-control-plane-3"]
    assert "  demo-control-plane-1 already configured" in host.lines


def test_frr_phase_is_done_in_proxy_mode_without_touching_a_node(
    ctx: Context, host: FakeHost
) -> None:
    assert frr.PHASE.done is not None and frr.PHASE.done(ctx)
    assert not any(c.argv[:2] == ["podman", "exec"] for c in host.calls)


def test_configure_names_the_refresh_when_the_node_image_has_no_frr(
    bgp_ctx: Context, host: FakeHost
) -> None:
    """A host that cached the node tag from before FRR was in the image."""
    n = cp(bgp_ctx, 0)
    host.responses[("podman", "exec", n.name, "test", "-f", "/etc/frr/daemons")] = Result(1, "", "")
    with pytest.raises(
        PhaseFailed,
        match=r"demo-control-plane-1 has no FRR: its node image predates bgp mode; "
        r"run tkctl pull images --refresh --k8s 1\.37\.2 and create the cluster again",
    ):
        frr.configure(bgp_ctx, n)
    assert not any(c.argv[3:4] == ["sed"] for c in host.calls)


def test_node_phase_configures_only_the_node_being_added(bgp_ctx: Context, host: FakeHost) -> None:
    """add node must not touch the other control planes: one of them may be stopped."""
    bgp_ctx.node = "demo-control-plane-3"
    for i in range(2):  # the others answer nothing sensible, as a stopped container would
        host.responses[("podman", "exec", cp(bgp_ctx, i).name, "cat", "/etc/tk8s/vip")] = Result(
            125, "", "not running"
        )
    n3 = cp(bgp_ctx, 2)
    node_answers(host, n3.name, conf=frr.config(bgp_ctx, n3), active=False)  # not yet enabled
    Runner([frr.NODE_PHASE], record=False).run(bgp_ctx)
    touched = {
        c.argv[3] if c.argv[2] == "-i" else c.argv[2]
        for c in host.calls
        if c.argv[:2] == ["podman", "exec"]
    }
    assert touched == {"demo-control-plane-3"}
    assert any(c.argv[3:5] == ["systemctl", "restart"] for c in host.calls)


def test_node_phase_is_done_for_a_worker_and_in_proxy_mode(
    bgp_ctx: Context, ctx: Context, host: FakeHost
) -> None:
    bgp_ctx.node = "demo-worker-1"
    assert frr.NODE_PHASE.done is not None and frr.NODE_PHASE.done(bgp_ctx)
    ctx.node = "demo-control-plane-1"
    assert frr.NODE_PHASE.done(ctx)
    assert not any(c.argv[:2] == ["podman", "exec"] for c in host.calls)


# ---- two routers


def test_config_for_one_router_is_unchanged(bgp_ctx: Context) -> None:
    conf = frr.config(bgp_ctx, cp(bgp_ctx, 0))
    assert conf.startswith(
        "! Generated by tkctl for demo-control-plane-1 of cluster demo: announce the apiserver "
        "VIP 172.23.0.1 to the router\n"
    )
    assert conf.count("neighbor 172.22.0.253 remote-as 65000") == 1
    assert conf.count(" peer 172.22.0.") == 1
    assert "neighbor 172.22.0.253 route-map vip-only out" in conf
    assert "bgp router-id 172.22.0.1" in conf


def test_config_has_one_neighbor_and_bfd_peer_per_router(two_router_ctx: Context) -> None:
    conf = frr.config(two_router_ctx, cp(two_router_ctx, 0))
    for ip in ("172.22.0.251", "172.22.0.252"):
        assert (
            f" neighbor {ip} remote-as 65000\n neighbor {ip} bfd\n neighbor {ip} timers 3 9\n"
            in conf
        )
        assert f"  neighbor {ip} route-map vip-only out\n" in conf
        peer = f" peer {ip}\n  detect-multiplier 3\n  receive-interval 300\n"
        assert peer + "  transmit-interval 300\n exit\n" in conf
    assert "172.22.0.253" not in conf  # the floating address is nobody's BGP speaker
