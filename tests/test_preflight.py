from collections.abc import Callable
from pathlib import Path

import pytest

from tests.fixtures import argvs
from tk8s.create import preflight
from tk8s.create.preflight import PreflightError
from tk8s.host import FakeHost, Result
from tk8s.phases import Context, PhaseFailed, Runner


def good_host(host: FakeHost, uid: str = "1000") -> None:
    host.responses[("podman", "--version")] = Result(0, "podman version 5.4.2\n", "")
    host.responses[("stat", "-fc", "%T", "/sys/fs/cgroup")] = Result(0, "cgroup2fs\n", "")
    host.responses[("swapon",)] = Result(0, "", "")
    host.responses[("lsmod",)] = Result(0, "br_netfilter 32768 0\n", "")
    host.responses[("id", "-u")] = Result(0, f"{uid}\n", "")
    host.responses[("id", "-g")] = Result(0, "1000\n", "")
    host.responses[("id", "-un")] = Result(0, "student\n", "")
    host.dirs.add(Path("/run/systemd/system"))
    host_size(host, 8, 32873260)


def host_size(host: FakeHost, cpus: int, kb: int) -> None:
    host.responses[("nproc",)] = Result(0, f"{cpus}\n", "")
    host.files[Path("/proc/meminfo")] = f"MemTotal:       {kb} kB\nMemFree:        1 kB\n"


def test_good_host_passes_and_persists_sysctls(ctx: Context, host: FakeHost) -> None:
    good_host(host)
    Runner([preflight.PHASE]).run(ctx)
    a = [" ".join(x) for x in argvs(host)]
    assert (
        "sysctl -qw net.bridge.bridge-nf-call-iptables=0 net.bridge.bridge-nf-call-ip6tables=0"
    ) in a
    assert "sysctl -qw net.ipv4.ip_forward=1" in a
    conf = host.files[Path("/etc/sysctl.d/90-tk8s.conf")]
    assert "net.bridge.bridge-nf-call-iptables = 0" in conf and "net.ipv4.ip_forward = 1" in conf
    assert host.files[Path("/etc/modules-load.d/tk8s.conf")] == "br_netfilter\n"
    ok = [line for line in host.lines if line.startswith("  preflight ok: podman 5.4.2")]
    assert ok and ok[0].endswith(", host 8 cpu, 32102 MiB")


def test_data_tree_is_created_for_the_user(ctx: Context, host: FakeHost) -> None:
    good_host(host)
    preflight.data_dirs(ctx)
    made = [c for c in host.calls if c.argv[0] == "mkdir"]
    assert [c.argv[1] for c in made] == [
        str(ctx.paths.data_dir),
        str(ctx.paths.clusters_dir),
        str(ctx.paths.cni_dir),
        str(ctx.paths.images_dir),
        str(ctx.paths.cache_dir),
    ]
    assert all(c.sudo and c.argv[2] == "owner=1000:1000" for c in made)


def test_root_owned_dir_is_taken_over_another_users_is_refused(
    ctx: Context, host: FakeHost
) -> None:
    good_host(host)
    d = ctx.paths.clusters_dir
    host.dirs.add(d)
    host.unwritable.add(d)
    host.responses[("stat", "-c", "%u", str(d))] = Result(0, "0\n", "")
    preflight.data_dirs(ctx)
    assert ["chown", "1000:1000", str(d)] in argvs(host)
    host.calls.clear()
    host.responses[("stat", "-c", "%u", str(d))] = Result(0, "999\n", "")
    host.responses[("stat", "-c", "%U", str(d))] = Result(0, "someone\n", "")
    with pytest.raises(preflight.PreflightError, match=f"{d} is owned by someone, not student"):
        preflight.data_dirs(ctx)
    assert not any(c.argv[0] == "chown" for c in host.calls)


@pytest.mark.parametrize(
    ("break_it", "message"),
    [
        (
            lambda h: h.responses.update(
                {("podman", "--version"): Result(0, "podman version 4.9.3\n", "")}
            ),
            "podman 4.9.3 is too old",
        ),
        (
            lambda h: h.responses.update({("podman", "--version"): Result(127, "", "not found")}),
            "podman not found",
        ),
        (
            lambda h: h.responses.update(
                {("stat", "-fc", "%T", "/sys/fs/cgroup"): Result(0, "tmpfs\n", "")}
            ),
            "cgroup v2 is required",
        ),
        (
            lambda h: h.responses.update(
                {("swapon",): Result(0, "/dev/sda2 partition 2G 0B -1\n", "")}
            ),
            "swap is enabled",
        ),
        (lambda h: h.responses.update({("true",): Result(1, "", "")}), "sudo without a password"),
        (
            lambda h: h.responses.update({("curl", "--version"): Result(127, "", "")}),
            "curl not found",
        ),
    ],
)
def test_each_prerequisite_fails_by_name(
    ctx: Context, host: FakeHost, break_it: Callable[[FakeHost], None], message: str
) -> None:
    good_host(host)
    break_it(host)
    with pytest.raises(PhaseFailed, match=message):
        Runner([preflight.PHASE]).run(ctx)


def test_missing_br_netfilter_is_loaded(ctx: Context, host: FakeHost) -> None:
    good_host(host)
    host.responses[("lsmod",)] = Result(0, "overlay 1 0\n", "")
    Runner([preflight.PHASE]).run(ctx)
    assert ["modprobe", "br_netfilter"] in argvs(host)


def test_a_host_with_exactly_the_node_size_passes(ctx: Context, host: FakeHost) -> None:
    host_size(host, 2, 4 * 2**20)  # demo nodes are 2 cpu / 4g
    assert preflight.check_host_fits(ctx) == "host 2 cpu, 4096 MiB"


def test_a_host_one_core_short_names_the_node_and_both_numbers(
    ctx: Context, host: FakeHost
) -> None:
    host_size(host, 1, 4 * 2**20)
    with pytest.raises(
        PreflightError, match="node demo-control-plane-1 asks for 2 cpu, the host has 1"
    ):
        preflight.check_host_fits(ctx)


def test_a_host_one_kb_short_of_memory_is_refused(ctx: Context, host: FakeHost) -> None:
    host_size(host, 8, 4 * 2**20 - 1)
    with pytest.raises(PreflightError, match="asks for 4096 MiB memory, the host has 4095 MiB"):
        preflight.check_host_fits(ctx)


def test_an_unreadable_host_size_is_a_preflight_error_not_a_traceback(
    ctx: Context, host: FakeHost
) -> None:
    host.responses[("nproc",)] = Result(0, "\n", "")
    host.files[Path("/proc/meminfo")] = "MemTotal:       1 kB\n"
    with pytest.raises(PreflightError, match="cannot read the host's cpu count"):
        preflight.check_host_fits(ctx)
    host.responses[("nproc",)] = Result(0, "8\n", "")
    host.files[Path("/proc/meminfo")] = "MemFree:        1 kB\n"
    with pytest.raises(PreflightError, match="no MemTotal in /proc/meminfo"):
        preflight.check_host_fits(ctx)


# ---- several hosts

ROUTE_Q = ("ip", "-j", "route", "show", "default")
ADDR_Q = ("ip", "-j", "-4", "addr", "show", "dev", "ens18")
ROUTE = '[{"dst":"default","gateway":"192.168.1.1","dev":"ens18"}]'
ADDR = (
    '[{"ifname":"ens18","addr_info":[{"family":"inet","local":"192.168.1.230","prefixlen":24,'
    '"scope":"global"}]}]'
)


def good_lan(host: FakeHost, kernel: str = "6.12.0", iface: str = "ens18") -> None:
    good_host(host)
    host.responses[ROUTE_Q] = Result(0, ROUTE.replace("ens18", iface), "")
    host.responses[("ip", "-j", "-4", "addr", "show", "dev", iface)] = Result(
        0, ADDR.replace("ens18", iface), ""
    )
    host.responses[("hostname",)] = Result(0, f"host-{id(host)}\n", "")
    host.responses[("uname", "-r")] = Result(0, f"{kernel}\n", "")
    host.responses[("arping",)] = Result(1, "", "")  # nobody answers


def test_multi_host_preflight_checks_every_host_and_prints_one_line_each(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    good_lans(hosts)
    Runner([preflight.PHASE]).run(ctx)
    out = "\n".join(hosts["a"].lines)
    for name in ("a", "b", "c"):
        assert f"  host {name}: preflight ok: podman 5.4.2" in out
    for h in hosts.values():
        assert ["sysctl", "-qw", "net.ipv4.ip_forward=1"] in argvs(h)
        assert Path("/etc/sysctl.d/90-tk8s.conf") in h.files
    # the pool was probed from the first host, every address but none twice
    probed = [
        c.argv[-1] for c in hosts["a"].calls if c.argv[:1] == ["arping"] and c.argv[1] != "-V"
    ]
    assert len(probed) == 17 and probed[0] == "192.168.1.64" and probed[-1] == "192.168.1.89"
    assert not any(c.argv[:1] == ["arping"] for c in hosts["b"].calls)
    assert "warning" not in out


def good_lans(hosts: dict[str, FakeHost]) -> None:
    """The three hosts of MULTI_YAML as preflight expects them (c reaches the LAN by eth0)."""
    for name, h in hosts.items():
        good_lan(h, iface="eth0" if name == "c" else "ens18")


def test_unreachable_host_is_named_with_the_ssh_advice(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    good_lans(hosts)
    hosts["b"].responses[("true",)] = Result(255, "", "Host key verification failed.\n")
    with pytest.raises(
        PhaseFailed,
        match=r"host b: ssh failed \(Host key verification failed\.\); ssh to it once by hand",
    ):
        Runner([preflight.PHASE]).run(ctx)


def test_host_on_another_subnet_or_interface_is_refused(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    good_lans(hosts)
    good_lan(hosts["c"], iface="eth9")
    with pytest.raises(
        PhaseFailed, match=r"host c: on 192.168.1.0/24 via eth9 .*, cluster.yaml says eth0"
    ):
        Runner([preflight.PHASE]).run(ctx)


def test_operator_off_the_lan_is_refused(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    op = FakeHost()
    good_lan(op)
    op.responses[ADDR_Q] = Result(0, ADDR.replace("192.168.1.230", "10.0.0.5"), "")
    ctx.host = op
    good_lans(hosts)
    with pytest.raises(PhaseFailed, match="the operator host is not on the cluster's LAN"):
        Runner([preflight.PHASE]).run(ctx)


def test_same_named_foreign_cluster_on_a_host_is_refused(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    good_lans(hosts)
    hosts["c"].dirs.add(Path("/etc/containers/systemd/demo"))  # units of another demo
    with pytest.raises(
        PhaseFailed, match=r"host c: a cluster named demo already exists there \(not managed"
    ):
        Runner([preflight.PHASE]).run(ctx)
    # our own copy there (a rerun) is fine
    ctx.save_cluster(ctx.require_cluster())
    Runner([preflight.PHASE]).run(ctx)


def test_addresses_in_use_are_listed_and_not_probed_on_a_rerun(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    good_lans(hosts)
    taken = "Unicast reply from 192.168.1.80 [AA:BB:CC:DD:EE:FF]  0.6ms\n"
    hosts["a"].responses = {
        ("arping", "-c", "2", "-w", "1", "-I", "ens18", "192.168.1.80"): Result(0, taken, ""),
        **hosts["a"].responses,
    }
    with pytest.raises(
        PhaseFailed, match=r"addresses in use on the LAN: 192.168.1.80 \(AA:BB:CC:DD:EE:FF\)"
    ):
        Runner([preflight.PHASE]).run(ctx)
    hosts["b"].active.add("demo.target")  # a rerun: our own containers would answer
    Runner([preflight.PHASE]).run(ctx)


def test_network_manager_and_networkd_get_unmanaged_drop_ins(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    good_lans(hosts)
    hosts["b"].dirs.add(Path("/etc/NetworkManager"))
    hosts["c"].active.add("systemd-networkd")
    Runner([preflight.PHASE]).run(ctx)
    nm = hosts["b"].files[Path("/etc/NetworkManager/conf.d/90-tk8s.conf")]
    assert "[keyfile]\nunmanaged-devices+=interface-name:tk8s-*\n" in nm
    assert ["nmcli", "general", "reload", "conf"] in argvs(hosts["b"])
    nd = hosts["c"].files[Path("/etc/systemd/network/90-tk8s-unmanaged.network")]
    assert "[Match]\nName=tk8s-*\n\n[Link]\nUnmanaged=yes\n" in nd
    assert ["networkctl", "reload"] in argvs(hosts["c"])
    assert Path("/etc/NetworkManager/conf.d/90-tk8s.conf") not in hosts["a"].files


def test_explicit_netkit_with_an_old_host_is_refused(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    good_lans(hosts)
    good_lan(hosts["c"], kernel="6.1.0-30-amd64", iface="eth0")
    c = ctx.require_cluster()
    ctx.cluster = c.model_copy(update={"spec": c.spec.model_copy(update={"datapath": "netkit"})})
    with pytest.raises(
        PhaseFailed, match=r"host c: kernel 6\.1\.0-30-amd64 is too old for --datapath netkit"
    ):
        Runner([preflight.PHASE]).run(ctx)


def test_majority_warning_is_printed_by_preflight(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    good_lans(hosts)
    c = ctx.require_cluster()
    moved = [
        n.model_copy(update={"host": "a"}) if n.name.endswith("plane-3") else n
        for n in c.spec.nodes
    ]
    ctx.cluster = c.model_copy(update={"spec": c.spec.model_copy(update={"nodes": moved})})
    Runner([preflight.PHASE]).run(ctx)
    assert (
        "  warning: host a holds 2 of 3 control planes; losing it stops the apiserver"
        in "\n".join(hosts["a"].lines)
    )


def test_single_host_preflight_output_is_unchanged(ctx: Context, host: FakeHost) -> None:
    good_host(host)
    Runner([preflight.PHASE]).run(ctx)
    assert host.lines[-1] == (
        "  preflight ok: podman 5.4.2, systemd, cgroup v2, swap off, host bridge-nf off, "
        "ip_forward on, " + str(ctx.paths.data_dir) + " owned by student, host 8 cpu, 32102 MiB"
    )
    assert not any(c.argv[:1] in (["arping"], ["uname"]) for c in host.calls)
