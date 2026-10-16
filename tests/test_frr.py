from tk8s import model
from tk8s.create import frr
from tk8s.host import FakeHost, Result
from tk8s.phases import Context, Runner


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
        # inactive answers once, then active: what configure() leaves behind
        host.responses[key] = (
            Result(0, "", "") if active else [Result(3, "", ""), Result(0, "", "")]
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
    assert frr.PHASE.done is not None and not frr.PHASE.done(bgp_ctx)
    # fresh answers: the Runner asks done() itself before it runs the phase
    node_answers(host, cp(bgp_ctx, 2).name, conf=frr.config(bgp_ctx, cp(bgp_ctx, 2)), active=False)
    Runner([frr.PHASE], record=False).run(bgp_ctx)
    restarted = [c.argv[2] for c in host.calls if c.argv[3:5] == ["systemctl", "restart"]]
    assert restarted == ["demo-control-plane-3"]
    assert "  demo-control-plane-1 already configured" in host.lines


def test_frr_phase_is_done_in_proxy_mode_without_touching_a_node(
    ctx: Context, host: FakeHost
) -> None:
    assert frr.PHASE.done is not None and frr.PHASE.done(ctx)
    assert not any(c.argv[:2] == ["podman", "exec"] for c in host.calls)
