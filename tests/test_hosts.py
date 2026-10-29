"""hosts: what tkctl learns about each host of a multi-host cluster and how it reaches them."""

from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

import pytest

from tk8s import model
from tk8s.host import DryRunHost, FakeHost, Result, SshHost
from tk8s.hosts import (
    HostsError,
    LanFacts,
    build,
    dry_run_lan,
    in_use,
    kernel,
    lan_facts,
    local_hostname,
)

ROUTE = '[{"dst":"default","gateway":"192.168.1.1","dev":"ens18","protocol":"static"}]'
ADDR = (
    '[{"ifname":"ens18","addr_info":[{"family":"inet","local":"192.168.1.230","prefixlen":24,'
    '"scope":"global"}]}]'
)
ROUTE_Q = ("ip", "-j", "route", "show", "default")
ADDR_Q = ("ip", "-j", "-4", "addr", "show", "dev", "ens18")


def test_lan_facts_from_the_default_route_and_the_interface_address() -> None:
    h = FakeHost(responses={ROUTE_Q: Result(0, ROUTE, ""), ADDR_Q: Result(0, ADDR, "")})
    assert lan_facts(h, "a") == LanFacts(
        "ens18", IPv4Network("192.168.1.0/24"), IPv4Address("192.168.1.1")
    )


@pytest.mark.parametrize(
    ("route", "addr", "message"),
    [
        ("[]", ADDR, "host a: expected one default route, found 0"),
        (ROUTE[:-1] + "," + ROUTE[1:], ADDR, "host a: expected one default route, found 2"),
        ("not json", ADDR, "host a: cannot read the default route"),
        (ROUTE, '[{"ifname":"ens18","addr_info":[]}]', "host a: no IPv4 address on ens18"),
    ],
)
def test_odd_routes_or_addresses_are_a_named_error(route: str, addr: str, message: str) -> None:
    h = FakeHost(responses={ROUTE_Q: Result(0, route, ""), ADDR_Q: Result(0, addr, "")})
    with pytest.raises(HostsError, match=message):
        lan_facts(h, "a")


def test_dry_run_lan_is_the_slash_24_around_the_range_start() -> None:
    assert dry_run_lan(IPv4Address("192.168.1.64")) == LanFacts(
        "eth0", IPv4Network("192.168.1.0/24"), IPv4Address("192.168.1.1")
    )


def test_kernel_is_major_minor_of_uname() -> None:
    h = FakeHost(responses={("uname", "-r"): Result(0, "6.12.43+deb13-amd64\n", "")})
    assert kernel(h) == (6, 12)


def test_in_use_lists_ip_and_mac_of_answering_addresses() -> None:
    h = FakeHost(
        responses={
            ("arping", "-c", "2", "-w", "1", "-I", "ens18", "192.168.1.64"): Result(
                0, "Unicast reply from 192.168.1.64 [AA:BB:CC:DD:EE:FF]  0.5ms\n", ""
            ),
            ("arping", "-c", "2", "-w", "1", "-I", "ens18", "192.168.1.65"): Result(1, "", ""),
        }
    )
    addrs = [IPv4Address("192.168.1.64"), IPv4Address("192.168.1.65")]
    assert in_use(h, "ens18", addrs) == [("192.168.1.64", "AA:BB:CC:DD:EE:FF")]
    h.responses = {("arping",): Result(127, "", "arping: command not found")}
    with pytest.raises(HostsError, match="arping not found"):
        in_use(h, "ens18", addrs)


def test_build_maps_names_to_the_operator_or_an_ssh_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    op = FakeHost()
    spec = [model.UserHost(name="a"), model.UserHost(name="b", ssh="tux@h:2222")]
    hosts = build(op, spec, say=op.lines.append, quiet=False, dry_run=True)
    assert hosts["a"] is op
    assert isinstance(hosts["b"], DryRunHost) and hosts["b"].label == "tux@h"
    real = build(op, spec, say=op.lines.append, quiet=True, dry_run=False)
    assert isinstance(real["b"], SshHost) and real["b"].target.port == 2222
    with pytest.raises(HostsError, match="host b: bad ssh target 'a b'"):
        build(
            op,
            [model.UserHost(name="b", ssh="a b")],
            say=op.lines.append,
            quiet=False,
            dry_run=True,
        )


def test_local_hostname_falls_back_under_dry_run() -> None:
    assert local_hostname(FakeHost(responses={("hostname",): Result(0, "pve-a\n", "")})) == "pve-a"
    assert local_hostname(DryRunHost(lambda _: None)) == "dry-run"
