"""The hosts of a multi-host cluster: how tkctl reaches each one (the operator itself, or an
SshHost) and what it learns about them before planning: the LAN interface, subnet and gateway
the macvlan network hangs off, the kernel (datapath), and which addresses of the reserved range
are already taken."""

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network

from . import model
from .host import DryRunHost, Host, HostError, Result, SshHost, parse_ssh


class HostsError(Exception):
    pass


@dataclass(frozen=True)
class LanFacts:
    iface: str
    subnet: IPv4Network
    gateway: IPv4Address
    address: IPv4Address  # the host's own address on that interface


def _json(host: Host, argv: list[str], name: str, what: str) -> object:
    r = host.run(argv, capture=True, check=False)
    try:
        data: object = json.loads(r.stdout) if r.returncode == 0 else None
    except ValueError:
        data = None
    if data is None:
        raise HostsError(f"host {name}: cannot read {what} ({' '.join(argv)})")
    return data


def lan_facts(host: Host, name: str) -> LanFacts:
    """The interface the default route leaves by, its IPv4 subnet, and the gateway."""
    routes = _json(host, ["ip", "-j", "route", "show", "default"], name, "the default route")
    if not isinstance(routes, list) or len(routes) != 1 or not isinstance(routes[0], dict):
        n = len(routes) if isinstance(routes, list) else 0
        raise HostsError(f"host {name}: expected one default route, found {n}")
    iface, gateway = routes[0].get("dev"), routes[0].get("gateway")
    if not isinstance(iface, str) or not isinstance(gateway, str):
        raise HostsError(f"host {name}: the default route has no dev or gateway")
    links = _json(host, ["ip", "-j", "-4", "addr", "show", "dev", iface], name, f"{iface}")
    addrs = [
        a
        for link in (links if isinstance(links, list) else [])
        if isinstance(link, dict)
        for a in link.get("addr_info", [])
        if isinstance(a, dict) and a.get("family") == "inet" and a.get("scope") == "global"
    ]
    if not addrs:
        raise HostsError(f"host {name}: no IPv4 address on {iface}")
    subnet = IPv4Network(f"{addrs[0]['local']}/{addrs[0]['prefixlen']}", strict=False)
    return LanFacts(iface, subnet, IPv4Address(gateway), IPv4Address(addrs[0]["local"]))


def dry_run_lan(start: IPv4Address) -> LanFacts:
    """What --dry-run assumes without a host to ask: eth0 on the /24 around the range, the
    host itself at the subnet's last address."""
    subnet = IPv4Network(f"{start}/24", strict=False)
    return LanFacts("eth0", subnet, subnet.network_address + 1, subnet.broadcast_address - 1)


def kernel(host: Host) -> tuple[int, int]:
    rel = host.run(["uname", "-r"], capture=True).stdout.strip()
    major, minor = (int(x.split("-")[0]) for x in rel.split(".")[:2])
    return major, minor


def in_use(host: Host, iface: str, addrs: Sequence[IPv4Address]) -> list[tuple[str, str]]:
    """(address, MAC) for every address something on the LAN answers ARP for: arping when the
    host has it (exit 0 answered, 1 silent, anything else is an error), else a ping that makes
    the kernel resolve the neighbour and `ip neigh` to read what came back."""
    taken: list[tuple[str, str]] = []
    use_arping = host.run(["arping", "-V"], capture=True, check=False).returncode != 127
    for ip in addrs:
        if use_arping:
            r = host.run(
                ["arping", "-c", "2", "-w", "1", "-I", iface, str(ip)], capture=True, check=False
            )
            if r.returncode not in (0, 1):
                raise HostsError(
                    f"arping {ip} on {iface} failed ({r.stderr.strip() or r.returncode}); "
                    "the address probe needs CAP_NET_RAW (run tkctl as a user with sudo)"
                )
            if r.returncode == 0:
                m = re.search(r"\[([0-9A-Fa-f:]+)\]", r.stdout)
                taken.append((str(ip), m.group(1) if m else "?"))
            continue
        host.run(["ping", "-c", "1", "-W", "1", "-I", iface, str(ip)], capture=True, check=False)
        n = host.run(
            ["ip", "-j", "neigh", "show", str(ip), "dev", iface], capture=True, check=False
        )
        try:
            entries = json.loads(n.stdout) if n.returncode == 0 and n.stdout.strip() else []
        except ValueError:
            entries = []
        for e in entries if isinstance(entries, list) else []:
            if (
                isinstance(e, dict)
                and e.get("lladdr")
                and e.get("state") not in (["FAILED"], ["INCOMPLETE"])
            ):
                taken.append((str(ip), str(e["lladdr"])))
                break
    return taken


def build(
    operator: Host,
    hosts: Sequence[model.UserHost] | Sequence[model.Host],
    *,
    say: Callable[[str], None],
    quiet: bool,
    dry_run: bool,
) -> dict[str, Host]:
    """One Host per cluster host: the operator itself for the entry without ssh, an SshHost
    (or, under --dry-run, a printing host with the same label) for the others."""
    out: dict[str, Host] = {}
    for h in hosts:
        if h.ssh is None:
            out[h.name] = operator
            continue
        try:
            target = parse_ssh(h.ssh)
        except ValueError as err:
            raise HostsError(f"host {h.name}: bad ssh target {h.ssh!r}") from err
        if dry_run:
            out[h.name] = DryRunHost(say, quiet=quiet, label=str(target))
        else:
            out[h.name] = SshHost(say, target, quiet=quiet)
    return out


def check_reachable(host: Host, name: str) -> None:
    """The host answers over ssh (keys, host key and sudo-less login all fine)."""
    try:
        r = host.run(["true"], capture=True, check=False)
    except HostError as err:
        r = Result(err.returncode, "", str(err))
    if r.returncode != 0:
        why = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else f"exit {r.returncode}"
        raise HostsError(
            f"host {name}: ssh failed ({why}); ssh to it once by hand so the host key is known "
            "and the key and sudo work"
        )


def local_hostname(host: Host) -> str:
    """What status.operator records; a dry run has no host to ask."""
    name = host.run(["hostname"], capture=True, check=False).stdout.strip()
    return name or "dry-run"
