from collections.abc import Callable
from pathlib import Path

import pytest

from tests.fixtures import argvs
from tk8s.create import preflight
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
    assert any(
        line.startswith("  preflight ok: podman 5.4.2, systemd, cgroup v2, swap off")
        for line in host.lines
    )


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
