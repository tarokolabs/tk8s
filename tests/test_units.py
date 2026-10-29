from pathlib import Path

from tk8s import model
from tk8s.create import units
from tk8s.host import FakeHost
from tk8s.phases import Context, Runner


def test_render_lists_the_files_in_write_order(ctx: Context) -> None:
    paths = list(units.render(ctx))
    node_files = [
        "meminfo",
        "kubelet-reserved.conf",
        "kubelet-config-dir.conf",
        "cpulist",
        "cpumap",
        "distance",
        "meminfo",
        "nr_hugepages",
        "nr_hugepages",
    ]
    assert [p.name for p in paths] == [
        "demo.network",
        "tkctl.boot",
        "vyos-preconfig-bootup.script",
        "vyos-postconfig-bootup.script",
        "demo-router.container",
        *node_files,
        "demo-control-plane-1.container",
        *node_files,
        "demo-worker-1.container",
        *node_files,
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


# ---- two routers


def test_two_routers_render_two_directories_and_units(two_router_ctx: Context) -> None:
    files = units.render(two_router_ctx)
    names = [p.name for p in files]
    assert names[:9] == [
        "demo.network",
        "tkctl.boot",
        "vyos-preconfig-bootup.script",
        "vyos-postconfig-bootup.script",
        "demo-router-1.container",
        "tkctl.boot",
        "vyos-preconfig-bootup.script",
        "vyos-postconfig-bootup.script",
        "demo-router-2.container",
    ]
    state = two_router_ctx.state_dir
    assert list(files)[1] == state / "router-1" / "tkctl.boot"
    assert list(files)[5] == state / "router-2" / "tkctl.boot"
    target = files[two_router_ctx.paths.systemd_dir / "demo.target"]
    wants = "Wants=demo-router-1.service demo-router-2.service demo-control-plane-1.service"
    assert wants in target


def test_each_router_config_has_its_own_address_hostname_and_vrrp_priority(
    two_router_ctx: Context,
) -> None:
    files = units.render(two_router_ctx)
    state, unit_dir = two_router_ctx.state_dir, two_router_ctx.paths.unit_dir / "demo"
    c1 = files[state / "router-1" / "tkctl.boot"]
    c2 = files[state / "router-2" / "tkctl.boot"]
    assert 'address "172.22.0.251/24"' in c1 and 'host-name "demo-router-1"' in c1
    assert 'address "172.22.0.252/24"' in c2 and 'host-name "demo-router-2"' in c2
    assert 'priority "200"' in c1 and 'priority "100"' in c2
    assert c1.startswith("high-availability {") and c2.startswith("high-availability {")
    assert c1.count("neighbor 172.22.0.") == 3 == c2.count("neighbor 172.22.0.")
    u1 = files[unit_dir / "demo-router-1.container"]
    assert "ContainerName=demo-router-1" in u1 and "HostName=demo-router-1" in u1
    assert "IP=172.22.0.251" in u1 and f"Volume={state}/router-1:/opt/vyatta/etc/config" in u1


def test_postconfig_removes_the_route_via_the_floating_address_on_both_routers(
    two_router_ctx: Context,
) -> None:
    files = units.render(two_router_ctx)
    for d in ("router-1", "router-2"):
        s = files[two_router_ctx.state_dir / d / "scripts" / "vyos-postconfig-bootup.script"]
        assert "ip route del default via 172.22.0.253 " in s
        assert "via 172.22.0.253 metric 100 proto static" in s


def test_one_router_config_has_no_vrrp_block(ctx: Context) -> None:
    cfg = units.render(ctx)[ctx.state_dir / "router" / "tkctl.boot"]
    assert cfg.startswith("interfaces {") and "high-availability" not in cfg


def test_router_directories_are_written_with_sudo(two_router_ctx: Context) -> None:
    state = two_router_ctx.state_dir
    assert units.needs_sudo(two_router_ctx, state / "router-2" / "tkctl.boot")
    assert not units.needs_sudo(two_router_ctx, state / "cluster.yaml")


def old_cluster(ctx: Context) -> model.Cluster:
    c = ctx.require_cluster()
    return c.model_copy(update={"status": model.Status(phase="Ready", tk8s="old")})


def test_node_files_come_right_before_each_node_unit(ctx: Context) -> None:
    names = [p.name for p in units.render(ctx)]
    i = names.index("demo-worker-1.container")
    assert names[i - 9 : i] == [
        "meminfo",
        "kubelet-reserved.conf",
        "kubelet-config-dir.conf",
        "cpulist",
        "cpumap",
        "distance",
        "meminfo",
        "nr_hugepages",
        "nr_hugepages",
    ]
    d = ctx.state_dir / "nodes" / "demo-worker-1"
    assert units.render(ctx)[d / "meminfo"] == "MemTotal: 4194304 kB\nSwapTotal: 0 kB\n"


def test_node_unit_mounts_the_three_files(ctx: Context) -> None:
    unit = units.render(ctx)[ctx.paths.unit_dir / "demo" / "demo-worker-1.container"]
    d = ctx.state_dir / "nodes" / "demo-worker-1"
    assert (
        f"Volume={ctx.paths.wulin_dir}:/opt/taroko/wulin\n"
        f"Volume={d}/meminfo:/proc/meminfo:ro\n"
        f"Volume={d}/sys-node:/sys/devices/system/node:ro\n"
        f"Volume={d}/kubelet-reserved.conf:/etc/kubernetes/kubelet.conf.d/10-tk8s-reserved.conf:ro\n"
        f"Volume={d}/kubelet-config-dir.conf"
        ":/etc/systemd/system/kubelet.service.d/20-tk8s-config-dir.conf:ro\n"
        "StopTimeout=60\n"
    ) in unit


def test_links_are_one_per_cpu_and_run_creates_them(ctx: Context, host: FakeHost) -> None:
    d = ctx.state_dir / "nodes" / "demo-control-plane-1" / "sys-node" / "node0"
    assert units.links(ctx)[d / "cpu1"] == Path("/sys/devices/system/cpu/cpu1")
    assert len(units.links(ctx)) == 6  # three nodes x 2 cpus
    assert not units.done(ctx)
    units.run(ctx)
    assert host.readlink(d / "cpu0") == Path("/sys/devices/system/cpu/cpu0")
    assert units.done(ctx)
    host.links.pop(d / "cpu0")
    assert not units.done(ctx)


def test_an_old_cluster_renders_todays_units_and_no_node_files(ctx: Context) -> None:
    old = old_cluster(ctx)
    files = units.render(ctx, old)
    assert not any("/nodes/" in str(p) for p in files)
    assert units.links(ctx, old) == {}
    unit = files[ctx.paths.unit_dir / "demo" / "demo-worker-1.container"]
    assert "kubelet.conf.d" not in unit and "/proc/meminfo" not in unit
    assert f"Volume={ctx.paths.wulin_dir}:/opt/taroko/wulin\nStopTimeout=60\n" in unit


def test_run_removes_cpu_links_a_resume_no_longer_wants(ctx: Context, host: FakeHost) -> None:
    """A resume with a smaller cpu must not leave the old cpuN symlinks: cAdvisor counts them."""
    n0 = ctx.state_dir / "nodes" / "demo-worker-1" / "sys-node" / "node0"
    host.symlink(n0 / "cpu2", Path("/sys/devices/system/cpu/cpu2"))
    host.symlink(n0 / "cpu3", Path("/sys/devices/system/cpu/cpu3"))
    units.run(ctx)
    assert host.readlink(n0 / "cpu2") is None and host.readlink(n0 / "cpu3") is None
    assert host.readlink(n0 / "cpu1") == Path("/sys/devices/system/cpu/cpu1")
    assert units.done(ctx)
    host.symlink(n0 / "cpu2", Path("/sys/devices/system/cpu/cpu2"))
    assert not units.done(ctx)


def test_a_cluster_without_status_renders_todays_units_too(ctx: Context) -> None:
    none = ctx.require_cluster().model_copy(update={"status": None})
    files = units.render(ctx, none)
    assert not any("/nodes/" in str(p) for p in files) and units.links(ctx, none) == {}
    assert files == units.render(ctx, old_cluster(ctx))


# ---- several hosts: each gets its own files

UNIT_DIR = Path("/etc/containers/systemd/demo")
SYSTEMD = Path("/etc/systemd/system")


def test_mac_and_shim_name_are_derived_from_the_address_and_the_cluster() -> None:
    from ipaddress import IPv4Address

    assert units.mac(IPv4Address("192.168.1.70")) == "02:54:4b:a8:01:46"
    assert units.shim_name("demo") == "tk8s-demo"
    long = units.shim_name("a-very-long-name")
    assert (
        len(long) <= 15
        and long.startswith("tk8s-a-very-")
        and long != units.shim_name("a-very-long-namf")
    )
    assert [str(p) for p in units.range_prefixes("192.168.1.64-192.168.1.95")] == [
        "192.168.1.64/27"
    ]
    assert [str(p) for p in units.range_prefixes("192.168.1.65-192.168.1.68,192.168.1.90")] == [
        "192.168.1.65/32",
        "192.168.1.66/31",
        "192.168.1.68/32",
        "192.168.1.90/32",
    ]


def test_each_host_gets_its_own_network_units_target_and_routes(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    state = ctx.state_dir
    by = {hf.name: hf for hf in units.per_host(ctx)}
    assert list(by) == ["a", "b", "c"] and by["b"].host is hosts["b"]
    a, b = by["a"].files, by["b"].files
    net = a[UNIT_DIR / "demo.network"]
    assert "Driver=macvlan\nOptions=parent=ens18\n" in net
    assert "Subnet=192.168.1.0/24\nGateway=192.168.1.1\n" in net
    assert "Options=no_default_route=1\nPodmanArgs=--route 0.0.0.0/0,192.168.1.64\n" in net
    cp1 = a[UNIT_DIR / "demo-control-plane-1.container"]
    assert "IP=192.168.1.68\n" in cp1
    assert "--cpus 2 --memory 4g --mac-address 02:54:4b:a8:01:44\n\n[Service]" in cp1
    assert UNIT_DIR / "demo-control-plane-2.container" not in a
    assert "IP=192.168.1.69\n" in b[UNIT_DIR / "demo-control-plane-2.container"]
    assert (
        "After=demo-control-plane-1.service" not in b[UNIT_DIR / "demo-control-plane-2.container"]
    )
    assert (
        "Wants=demo-router-1.service demo-control-plane-1.service demo-worker-1.service "
        "demo-routes.service\n"
    ) in a[SYSTEMD / "demo.target"]
    # only the operator talks to the cluster from its host network: b and c get no routes unit
    assert (
        "Wants=demo-router-2.service demo-control-plane-2.service demo-worker-2.service\n"
    ) in b[SYSTEMD / "demo.target"]
    assert SYSTEMD / "demo-routes.service" not in b
    assert SYSTEMD / "demo-routes.service" not in by["c"].files
    routes = a[SYSTEMD / "demo-routes.service"]
    assert "ExecStart=-ip link add tk8s-demo link ens18 type macvlan mode bridge\n" in routes
    assert "ExecStart=ip addr replace 192.168.1.67/32 dev tk8s-demo\n" in routes
    assert "ExecStart=ip route replace 192.168.1.64/30 dev tk8s-demo src 192.168.1.67\n" in routes
    assert "ExecStart=ip route replace 192.168.1.80/29 dev tk8s-demo src 192.168.1.67\n" in routes
    assert "ExecStart=ip route replace 192.168.1.88/31 dev tk8s-demo src 192.168.1.67\n" in routes
    assert "ExecStart=ip route replace 10.244.0.0/21 via 192.168.1.68 dev tk8s-demo\n" in routes
    assert "ExecStart=ip route replace 172.23.0.1/32 via 192.168.1.64 dev tk8s-demo\n" in routes
    assert "ExecStop=-ip link del tk8s-demo\n" in routes and "Wants=" not in routes
    boot = a[state / "router-1" / "tkctl.boot"]
    assert 'address "192.168.1.65/24"' in boot and "next-hop 192.168.1.1" in boot
    assert "address 192.168.1.64/24 {" in boot  # the VRRP group
    assert "--mac-address 02:54:4b:a8:01:41" in a[UNIT_DIR / "demo-router-1.container"]
    assert state / "router-2" / "tkctl.boot" not in a and state / "router-2" / "tkctl.boot" in b
    assert state / "nodes" / "demo-control-plane-3" / "meminfo" in by["c"].files
    assert state / "nodes" / "demo-control-plane-3" / "meminfo" not in a
    assert set(by["c"].links) == {
        state / "nodes" / "demo-control-plane-3" / "sys-node" / "node0" / f"cpu{i}"
        for i in range(2)
    }


def test_an_operator_outside_the_node_hosts_gets_routes_only(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, _ = multi_ctx
    op = FakeHost()
    ctx.host = op  # a is no longer the operator's own machine
    by = {hf.name: hf for hf in units.per_host(ctx)}
    assert list(by) == ["a", "b", "c", "operator"] and by["operator"].host is op
    files = by["operator"].files
    assert set(files) == {SYSTEMD / "demo.target", SYSTEMD / "demo-routes.service"}
    assert "Wants=demo-routes.service\n" in files[SYSTEMD / "demo.target"]
    routes = files[SYSTEMD / "demo-routes.service"]
    assert "ExecStart=ip route replace 10.244.0.0/21 via 192.168.1.68\n" in routes
    assert "ExecStart=ip route replace 172.23.0.1/32 via 192.168.1.64\n" in routes
    assert "tk8s-demo" not in routes


def test_run_writes_each_hosts_files_on_that_host(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    units.run(ctx)
    assert UNIT_DIR / "demo-control-plane-3.container" in hosts["c"].files
    assert UNIT_DIR / "demo-control-plane-3.container" not in hosts["a"].files
    assert (
        UNIT_DIR / "demo.network" in hosts["a"].files
        and UNIT_DIR / "demo.network" in hosts["b"].files
    )
    assert units.done(ctx)
    assert units.missing_node_files(ctx) == []
    del hosts["b"].files[ctx.state_dir / "nodes" / "demo-worker-2" / "meminfo"]
    assert units.missing_node_files(ctx) == [ctx.state_dir / "nodes" / "demo-worker-2" / "meminfo"]
    assert not units.done(ctx)


def test_single_host_files_carry_no_mac_or_shim(ctx: Context) -> None:
    files = units.render(ctx)
    text = "\n".join(files.values())
    assert "mac-address" not in text and "tk8s-demo" not in text and "macvlan" not in text
    assert 'address "172.22.0.253/24"' in files[ctx.state_dir / "router" / "tkctl.boot"]
    assert [hf.name for hf in units.per_host(ctx)] == [""]


def test_every_node_host_gets_the_audit_policy_the_apiservers_mount(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    """kubeadm join --control-plane copies init's ClusterConfiguration, audit-policy-file
    included: the file has to be on every host a control plane may run on."""
    ctx, _ = multi_ctx
    by = {hf.name: hf for hf in units.per_host(ctx)}
    policy = ctx.state_dir / "logs" / "apiserver" / "audit-policy.yaml"
    text = (ctx.paths.home / "manifests" / "audit-policy.yaml").read_text()
    for name in ("a", "b", "c"):
        assert by[name].files[policy] == text, name


def test_single_host_units_do_not_carry_the_audit_policy(ctx: Context) -> None:
    assert ctx.state_dir / "logs" / "apiserver" / "audit-policy.yaml" not in units.render(ctx)


def test_vrrp_id_follows_the_floating_address_on_several_hosts(
    multi_ctx: tuple[Context, dict[str, FakeHost]], two_router_ctx: Context
) -> None:
    ctx, _ = multi_ctx
    by = {hf.name: hf for hf in units.per_host(ctx)}
    boot = by["a"].files[ctx.state_dir / "router-1" / "tkctl.boot"]
    assert 'vrid "64"' in boot  # 192.168.1.64: two clusters on one LAN never share a VRID
    single = units.render(two_router_ctx)[two_router_ctx.state_dir / "router-1" / "tkctl.boot"]
    assert 'vrid "10"' in single


def test_macvlan_containers_carry_their_own_dns_the_network_none(
    multi_ctx: tuple[Context, dict[str, FakeHost]], ctx: Context
) -> None:
    """netavark refuses DNS= on a macvlan network (no aardvark there): the resolvers go into
    each container instead; a bridge network keeps them as before."""
    multi, _ = multi_ctx
    by = {hf.name: hf for hf in units.per_host(multi)}
    assert "DNS=" not in by["a"].files[UNIT_DIR / "demo.network"]
    cp1 = by["a"].files[UNIT_DIR / "demo-control-plane-1.container"]
    assert "IP=192.168.1.68\nDNS=1.1.1.1\nDNS=1.0.0.1\nDNS=8.8.8.8\nDNS=8.8.4.4\n" in cp1
    assert "DNS=1.1.1.1\n" in by["a"].files[UNIT_DIR / "demo-router-1.container"]
    single = units.render(ctx)
    assert "DNS=1.1.1.1\n" in single[UNIT_DIR / "demo.network"]
    assert "DNS=" not in single[UNIT_DIR / "demo-control-plane-1.container"]
    assert "DNS=" not in single[UNIT_DIR / "demo-router.container"]
