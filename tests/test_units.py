from pathlib import Path

from tk8s.create import units
from tk8s.host import FakeHost
from tk8s.phases import Context, Runner


def test_render_lists_the_files_in_write_order(ctx: Context) -> None:
    paths = list(units.render(ctx))
    assert [p.name for p in paths] == [
        "demo.network",
        "tkctl.boot",
        "vyos-preconfig-bootup.script",
        "vyos-postconfig-bootup.script",
        "demo-router.container",
        "demo-control-plane-1.container",
        "demo-worker-1.container",
        "demo-worker-2.container",
        "demo.target",
        "demo-routes.service",
    ]
    assert paths[1] == ctx.state_dir / "router" / "tkctl.boot"
    assert paths[2] == ctx.state_dir / "router" / "scripts" / "vyos-preconfig-bootup.script"
    assert paths[3] == ctx.state_dir / "router" / "scripts" / "vyos-postconfig-bootup.script"


def test_control_plane_has_no_after_and_workers_wait_for_it(ctx: Context) -> None:
    files = units.render(ctx)
    cp = files[Path("/etc/containers/systemd/demo/demo-control-plane-1.container")]
    wk = files[Path("/etc/containers/systemd/demo/demo-worker-1.container")]
    assert "PartOf=demo.target\n\n[Container]" in cp and "After=" not in cp
    assert "PartOf=demo.target\nAfter=demo-control-plane-1.service\n" in wk


def test_config_boot_lists_one_server_per_control_plane_with_a_tcp_check(ctx: Context) -> None:
    cfg = units.render(ctx)[ctx.state_dir / "router" / "tkctl.boot"]
    assert cfg.count("            server demo-control-plane-") == 1
    assert "http-check" not in cfg and "ssl" not in cfg  # TCP check only (spike 2)
    assert 'address "172.22.0.253/24"' in cfg and "next-hop 172.22.0.254" in cfg
    assert 'host-name "demo-router"' in cfg


def test_router_container_hostname_matches_the_vyos_host_name(ctx: Context) -> None:
    """VyOS runs sudo at every commit; a hostname it cannot resolve stalls the boot for minutes."""
    files = units.render(ctx)
    unit = files[ctx.paths.unit_dir / "demo" / "demo-router.container"]
    cfg = files[ctx.state_dir / "router" / "tkctl.boot"]
    assert "HostName=demo-router" in unit and 'host-name "demo-router"' in cfg
    assert "ContainerName=demo-router" in unit and "IP=172.22.0.253" in unit
    assert f"Image=ghcr.io/tarokolabs/tk8s/router:{ctx.versions.vyos}" in unit
    assert f"Volume={ctx.state_dir}/router:/opt/vyatta/etc/config" in unit
    assert "PodmanArgs=--privileged --systemd=always" in unit
    assert "StopSignal=SIGRTMIN+3" in unit


def test_network_sends_the_nodes_to_the_router_not_the_host(ctx: Context) -> None:
    net = units.render(ctx)[ctx.paths.unit_dir / "demo" / "demo.network"]
    assert "Gateway=172.22.0.254" in net
    assert "Options=no_default_route=1" in net
    assert "PodmanArgs=--route 0.0.0.0/0,172.22.0.253" in net


def test_preconfig_script_installs_tkctl_boot_with_vyos_own_version_footer(ctx: Context) -> None:
    """A config.boot without the version footer is migrated by VyOS at boot: it adds blocks (a
    conntrack helper list whose modprobe fails in a container) and rewrites the file. The
    footer is build-specific, so VyOS writes it itself, right before loading."""
    script = units.render(ctx)[
        ctx.state_dir / "router" / "scripts" / "vyos-preconfig-bootup.script"
    ]
    assert script.startswith("#!/bin/sh\n")
    assert "cp /opt/vyatta/etc/config/tkctl.boot /opt/vyatta/etc/config/config.boot" in script
    assert (
        "grep '^// ' /opt/vyatta/etc/config.boot.default >> /opt/vyatta/etc/config/config.boot"
        in script
    )


def test_bootup_script_drops_the_route_to_itself(ctx: Context) -> None:
    script = units.render(ctx)[
        ctx.state_dir / "router" / "scripts" / "vyos-postconfig-bootup.script"
    ]
    assert script.startswith("#!/bin/sh\n")
    assert "ip route del default via 172.22.0.253" in script
    # netavark deletes exactly the route it added when the container stops, and a missing route
    # fails the unit (ESRCH): a transient unit puts it back at shutdown
    assert (
        "systemd-run --quiet --unit=tk8s-podman-route -p RemainAfterExit=yes "
        '-p "ExecStop=/usr/sbin/ip route replace default via 172.22.0.253 metric 100 proto static" '
        "/bin/true"
    ) in script
    # the host bridge is on the nodes' link: without this the router answers every forwarded
    # packet with an ICMP redirect to the host, which ping counts as errors
    assert (
        "sysctl -qw net.ipv4.conf.all.send_redirects=0 net.ipv4.conf.eth0.send_redirects=0"
        in script
    )


def test_target_wants_router_every_node_and_routes(ctx: Context) -> None:
    target = units.render(ctx)[ctx.paths.systemd_dir / "demo.target"]
    assert (
        "Wants=demo-router.service demo-control-plane-1.service demo-worker-1.service "
        "demo-worker-2.service demo-routes.service"
    ) in target


def test_run_writes_etc_and_the_router_dir_with_sudo_the_rest_as_the_user(
    ctx: Context, host: FakeHost
) -> None:
    units.run(ctx)
    writes = {c.argv[1]: c for c in host.calls if c.argv[0] == "write"}
    assert writes[str(ctx.paths.unit_dir / "demo" / "demo.network")].sudo
    assert writes[str(ctx.state_dir / "router" / "tkctl.boot")].sudo  # VyOS owns that directory
    for name in ("vyos-preconfig-bootup.script", "vyos-postconfig-bootup.script"):
        script = writes[str(ctx.state_dir / "router" / "scripts" / name)]
        assert script.sudo and "mode=755" in script.argv[2]
    assert writes[str(ctx.paths.systemd_dir / "demo.target")].sudo
    assert units.done(ctx)


def test_units_phase_runs_then_is_done(ctx: Context, host: FakeHost) -> None:
    assert not units.done(ctx)
    Runner([units.PHASE]).run(ctx)
    assert units.done(ctx) and "[units] write the Quadlet units" in "".join(host.lines)


def test_router_unit_counts_systemd_halt_as_success(ctx: Context) -> None:
    """VyOS's systemd exits 130 on SIGRTMIN+3; without this the unit shows failed after stop."""
    files = units.render(ctx, ctx.require_cluster())
    unit = files[ctx.paths.unit_dir / "demo" / "demo-router.container"]
    assert "SuccessExitStatus=130" in unit


def test_bgp_router_config_has_bgp_and_bfd_and_no_haproxy(bgp_ctx: Context) -> None:
    boot = units.render(bgp_ctx)[bgp_ctx.state_dir / "router" / "tkctl.boot"]
    assert "load-balancing" not in boot
    assert "protocols {\n    bfd {\n        peer 172.22.0.1 {\n" in boot
    assert (
        "    bgp {\n"
        "        address-family {\n"
        "            ipv4-unicast {\n"
        "                maximum-paths {\n"
        '                    ebgp "8"\n'
        "                }\n"
        "            }\n"
        "        }\n"
        "        neighbor 172.22.0.1 {\n"
    ) in boot
    assert '        system-as "65000"\n    }\n    static {\n' in boot
    assert 'host-name "demo-router"' in boot and 'address "172.22.0.253/24"' in boot


def test_proxy_router_config_is_unchanged_by_the_bgp_template(ctx: Context) -> None:
    boot = units.render(ctx)[ctx.state_dir / "router" / "tkctl.boot"]
    assert "load-balancing {" in boot and "protocols {\n    static {" in boot
    assert "bgp" not in boot and "bfd" not in boot


def test_bgp_host_routes_reach_the_vip_through_the_router(bgp_ctx: Context) -> None:
    unit = units.render(bgp_ctx)[bgp_ctx.paths.systemd_dir / "demo-routes.service"]
    assert "ExecStart=ip route replace 172.23.0.1/32 via 172.22.0.253\n" in unit
    assert "ExecStop=-ip route del 172.23.0.1/32 via 172.22.0.253\n" in unit


def test_proxy_host_routes_have_no_vip_line(ctx: Context) -> None:
    unit = units.render(ctx)[ctx.paths.systemd_dir / "demo-routes.service"]
    assert "172.23." not in unit and unit.count("ExecStart=") == 2 and unit.count("ExecStop=") == 2


def test_bgp_router_rejects_the_vip_when_no_control_plane_announces_it(bgp_ctx: Context) -> None:
    """Without this the router's default route sends a VIP packet back to the host, whose VIP/32
    route sends it back to the router: a TTL loop and a hanging client instead of a refusal."""
    boot = units.render(bgp_ctx)[bgp_ctx.state_dir / "router" / "tkctl.boot"]
    assert (
        "        route 172.23.0.1/32 {\n"
        "            reject {\n"
        '                distance "254"\n'
        "            }\n"
        "        }\n"
    ) in boot
