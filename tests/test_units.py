from pathlib import Path

from tk8s.create import units
from tk8s.host import FakeHost
from tk8s.phases import Context, Runner


def test_render_lists_the_files_in_write_order(ctx: Context) -> None:
    paths = list(units.render(ctx))
    assert [p.name for p in paths] == [
        "demo.network",
        "config.boot",
        "vyos-postconfig-bootup.script",
        "demo-router.container",
        "demo-control-plane-1.container",
        "demo-worker-1.container",
        "demo-worker-2.container",
        "demo.target",
        "demo-routes.service",
    ]
    assert paths[1] == ctx.state_dir / "router" / "config.boot"
    assert paths[2] == ctx.state_dir / "router" / "scripts" / "vyos-postconfig-bootup.script"


def test_control_plane_has_no_after_and_workers_wait_for_it(ctx: Context) -> None:
    files = units.render(ctx)
    cp = files[Path("/etc/containers/systemd/demo/demo-control-plane-1.container")]
    wk = files[Path("/etc/containers/systemd/demo/demo-worker-1.container")]
    assert "PartOf=demo.target\n\n[Container]" in cp and "After=" not in cp
    assert "PartOf=demo.target\nAfter=demo-control-plane-1.service\n" in wk


def test_config_boot_lists_one_server_per_control_plane_with_a_tcp_check(ctx: Context) -> None:
    cfg = units.render(ctx)[ctx.state_dir / "router" / "config.boot"]
    assert cfg.count("            server demo-control-plane-") == 1
    assert (
        "            server demo-control-plane-1 {\n"
        '                address "172.22.0.1"\n'
        "                check {\n"
        "                }\n"
        '                port "6443"\n'
        "            }"
    ) in cfg
    assert "http-check" not in cfg and "ssl" not in cfg  # TCP check only (spike 2)
    assert 'address "172.22.0.253/24"' in cfg and "next-hop 172.22.0.254" in cfg
    assert 'host-name "demo-router"' in cfg


def test_router_container_hostname_matches_the_vyos_host_name(ctx: Context) -> None:
    """VyOS runs sudo at every commit; a hostname it cannot resolve stalls the boot for minutes."""
    files = units.render(ctx)
    unit = files[ctx.paths.unit_dir / "demo" / "demo-router.container"]
    cfg = files[ctx.state_dir / "router" / "config.boot"]
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


def test_bootup_script_drops_the_route_to_itself(ctx: Context) -> None:
    script = units.render(ctx)[
        ctx.state_dir / "router" / "scripts" / "vyos-postconfig-bootup.script"
    ]
    assert script.startswith("#!/bin/sh\n")
    assert "ip route del default via 172.22.0.253" in script


def test_nodes_resolve_the_api_name_to_the_router(ctx: Context) -> None:
    unit = units.render(ctx)[ctx.paths.unit_dir / "demo" / "demo-worker-1.container"]
    assert "AddHost=api.demo.k8s:172.22.0.253" in unit


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
    assert writes[str(ctx.state_dir / "router" / "config.boot")].sudo  # VyOS owns that directory
    script = writes[str(ctx.state_dir / "router" / "scripts" / "vyos-postconfig-bootup.script")]
    assert script.sudo and "mode=755" in script.argv[2]
    assert writes[str(ctx.paths.systemd_dir / "demo.target")].sudo
    assert units.done(ctx)


def test_units_phase_runs_then_is_done(ctx: Context, host: FakeHost) -> None:
    assert not units.done(ctx)
    Runner([units.PHASE]).run(ctx)
    assert units.done(ctx) and "[units] write the Quadlet units" in "".join(host.lines)
