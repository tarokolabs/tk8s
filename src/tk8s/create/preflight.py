"""preflight: the host can run tk8s (podman >= 5.4, systemd, cgroup v2, swap off, passwordless
sudo, curl), bridged frames stay out of the host firewall, forwarding is on, the data tree is the
user's, and the host has the cores and memory the planned nodes ask for. Read-only apart from two
sysctl files and the data tree; runs every time.

On several hosts the same checks run on every host, plus: the operator and every host sit on
the LAN cluster.yaml was planned for, no other cluster of the name lives on a host, the kernels
carry the datapath, the network managers leave the shim interfaces alone, and nothing on the
LAN already answers for an address of the range."""

from ipaddress import IPv4Address
from pathlib import Path

from .. import hosts, model
from ..datatree import PreflightError, data_dirs
from ..host import Host
from ..phases import Context, Phase, PhaseFailed
from . import nodefs
from .plan import majority_warning, parse_range

__all__ = ["PHASE", "PreflightError", "data_dirs"]

NM_CONF = Path("/etc/NetworkManager/conf.d/90-tk8s.conf")
NM_TEXT = (
    "# tk8s: the cluster shim interfaces (tk8s-*) are managed by tkctl's routes units\n"
    "[keyfile]\n"
    "unmanaged-devices+=interface-name:tk8s-*\n"
)
NETWORKD_CONF = Path("/etc/systemd/network/90-tk8s-unmanaged.network")
NETWORKD_TEXT = (
    "# tk8s: the cluster shim interfaces (tk8s-*) are managed by tkctl's routes units\n"
    "[Match]\n"
    "Name=tk8s-*\n"
    "\n"
    "[Link]\n"
    "Unmanaged=yes\n"
)

SYSCTL = (
    "# tk8s: bridged frames between cluster nodes bypass the host firewall "
    "(Docker sets FORWARD DROP)\n"
    "net.bridge.bridge-nf-call-iptables = 0\n"
    "net.bridge.bridge-nf-call-ip6tables = 0\n"
    "# tk8s: node network namespaces inherit this at creation\n"
    "net.ipv4.ip_forward = 1\n"
)


def podman_version(ctx: Context) -> str:
    r = ctx.host.run(["podman", "--version"], capture=True, check=False)
    if r.returncode != 0:
        raise PreflightError("podman not found (podman >= 5.4 is required)")
    v = r.stdout.split()[2]
    major, minor = (int(x) for x in v.split(".")[:2])
    if (major, minor) < (5, 4):
        raise PreflightError(f"podman {v} is too old (>= 5.4 required)")
    return v


def check_tools(ctx: Context) -> str:
    """Every prerequisite, each with the message the README promises."""
    v = podman_version(ctx)
    systemd = ctx.host.exists(Path("/run/systemd/system")) or (
        ctx.host.run(["systemctl", "is-system-running"], check=False, capture=True).returncode == 0
    )
    if not systemd:
        raise PreflightError("systemd is not running (tk8s v2 supports systemd hosts only)")
    fs = ctx.host.run(["stat", "-fc", "%T", "/sys/fs/cgroup"], capture=True, check=False).stdout
    if fs.strip() != "cgroup2fs":
        raise PreflightError("cgroup v2 is required")
    swap = ctx.host.run(["swapon", "--show", "--noheadings"], capture=True, check=False).stdout
    if swap.strip():
        raise PreflightError(
            "swap is enabled; run: sudo swapoff -a and comment the swap line in /etc/fstab"
        )
    if ctx.host.run(["true"], sudo=True, check=False).returncode != 0:
        raise PreflightError(
            "sudo without a password prompt is required for podman and systemd operations"
        )
    if ctx.host.run(["curl", "--version"], capture=True, check=False).returncode != 0:
        raise PreflightError("curl not found")
    return v


def bridge_and_forwarding(ctx: Context) -> None:
    """kubeadm needs br_netfilter, but with it the host's FORWARD chain sees bridged frames between
    nodes; Docker's FORWARD DROP would kill pod-to-node traffic. Keep host-bridge frames out of the
    host firewall; forwarding on before any node namespace copies the flag."""
    if "br_netfilter" not in ctx.host.run(["lsmod"], capture=True, check=False).stdout:
        ctx.host.run(["modprobe", "br_netfilter"], sudo=True)
    ctx.host.run(
        [
            "sysctl",
            "-qw",
            "net.bridge.bridge-nf-call-iptables=0",
            "net.bridge.bridge-nf-call-ip6tables=0",
        ],
        sudo=True,
    )
    ctx.host.run(["sysctl", "-qw", "net.ipv4.ip_forward=1"], sudo=True)
    ctx.host.write(Path("/etc/modules-load.d/tk8s.conf"), "br_netfilter\n", sudo=True)
    ctx.host.write(Path("/etc/sysctl.d/90-tk8s.conf"), SYSCTL, sudo=True)


def check_host_fits(ctx: Context, nodes: list[model.Node] | None = None) -> str:
    """Every planned node (or the given ones: those placed on ctx.host) fits the host: a node
    sized above the host's cores or MemTotal would report a capacity the host cannot back (and
    podman refuses --cpus above the core count only after the units are written). Returns the
    host size for the ok line."""
    said = ctx.host.run(["nproc"], capture=True).stdout.strip()
    if not said.isdigit():
        raise PreflightError(f"cannot read the host's cpu count: nproc said {said!r}")
    cpus = int(said)
    meminfo = ctx.host.read(Path("/proc/meminfo")) or ""
    totals = [ln.split() for ln in meminfo.splitlines() if ln.startswith("MemTotal:")]
    if not totals or len(totals[0]) < 2 or not totals[0][1].isdigit():
        raise PreflightError("no MemTotal in /proc/meminfo")
    host_mib = int(totals[0][1]) // 1024
    for n in ctx.require_cluster().spec.nodes if nodes is None else nodes:
        if n.cpu > cpus:
            raise PreflightError(f"node {n.name} asks for {n.cpu} cpu, the host has {cpus}")
        node_mib = nodefs.memory_bytes(n.memory) // 2**20
        if node_mib > host_mib:
            raise PreflightError(
                f"node {n.name} asks for {node_mib} MiB memory, the host has {host_mib} MiB"
            )
    return f"host {cpus} cpu, {host_mib} MiB"


# ---- several hosts


def check_lan(ctx: Context, name: str, host: Host, want: model.Host) -> None:
    """The host is on the LAN cluster.yaml was planned for, by the same interface."""
    net = ctx.require_cluster().spec.network
    try:
        lan = hosts.lan_facts(host, name)
    except hosts.HostsError as err:
        raise PreflightError(str(err)) from err
    if (lan.iface, lan.subnet, lan.gateway) != (want.iface, net.nodes, net.gateway):
        raise PreflightError(
            f"host {name}: on {lan.subnet} via {lan.iface} (gateway {lan.gateway}), "
            f"cluster.yaml says {want.iface} on {net.nodes} (gateway {net.gateway})"
        )


def check_operator(ctx: Context) -> None:
    """An operator that runs no node still needs the tools (kubectl, podman for the image
    list, sudo for its routes unit), the data tree, and a place on the cluster's LAN."""
    net = ctx.require_cluster().spec.network
    if any(h is ctx.host for _, h in ctx.node_hosts()):
        return  # a node host: the per-host checks cover it
    check_tools(ctx)
    data_dirs(ctx)
    try:
        lan = hosts.lan_facts(ctx.host, "operator")
    except hosts.HostsError as err:
        raise PreflightError(str(err)) from err
    if lan.subnet != net.nodes:
        raise PreflightError(
            f"the operator host is not on the cluster's LAN ({net.nodes}; it is on "
            f"{lan.subnet}); run tkctl on a host of that network"
        )
    ctx.say("  operator: tools, data tree and LAN ok (runs no node)")


def check_hostnames(ctx: Context) -> None:
    """status.operator is a hostname: two hosts with the same one (clones of a template) would
    make a copy look like the original."""
    seen: dict[str, str] = {}
    for name, h in [*ctx.node_hosts(), ("operator", ctx.host)]:
        hn = hosts.local_hostname(h)
        if hn in seen and seen[hn] != name and not (h is ctx.host and seen[hn] in ctx.hosts):
            raise PreflightError(
                f"hosts {seen[hn]} and {name} both answer to hostname {hn}; tkctl tells the "
                "operator apart by hostname, so give each host its own"
            )
        seen.setdefault(hn, name)


def check_name_free(ctx: Context, name: str, host: Host) -> None:
    """No other cluster of this name on the host: its units directory or state file exist
    only when the cluster.yaml there is our own copy (a rerun)."""
    c = ctx.require_cluster()
    text = host.read(ctx.state_dir / "cluster.yaml")
    if text is None and not host.exists(ctx.paths.unit_dir / c.name):
        return
    mine = c.status.operator if c.status is not None else None
    try:
        there = model.load_text(text).status if text is not None else None
    except model.ClusterFileError:
        there = None
    if there is None or there.operator != mine:
        raise PreflightError(
            f"host {name}: a cluster named {c.name} already exists there "
            "(not managed from this operator); pick another name or delete it there"
        )


def unmanaged_drop_ins(host: Host) -> None:
    """NetworkManager and systemd-networkd are told, their own way, to leave tk8s-* alone."""
    if host.exists(NM_CONF.parent.parent):
        if host.read(NM_CONF, sudo=True) != NM_TEXT:
            host.write(NM_CONF, NM_TEXT, sudo=True)
        host.run(["nmcli", "general", "reload", "conf"], sudo=True, check=False)
    if host.is_active("systemd-networkd"):
        if host.read(NETWORKD_CONF, sudo=True) != NETWORKD_TEXT:
            host.write(NETWORKD_CONF, NETWORKD_TEXT, sudo=True)
        host.run(["networkctl", "reload"], sudo=True, check=False)


def check_kernels(ctx: Context) -> None:
    if ctx.require_cluster().spec.datapath != "netkit":
        return
    for name, h in ctx.node_hosts():
        rel = h.run(["uname", "-r"], capture=True).stdout.strip()
        if hosts.kernel(h) < (6, 8):
            raise PreflightError(
                f"host {name}: kernel {rel} is too old for --datapath netkit (6.8 needed)"
            )


def cluster_addresses(c: model.Cluster) -> list[IPv4Address]:
    """Everything the cluster will answer for on the LAN: the whole reserved pool."""
    return parse_range(c.spec.network.range or "")


def check_addresses_free(ctx: Context) -> None:
    """Nothing on the LAN answers for an address of the range. Only on a fresh create: once the
    cluster's target runs anywhere, its own containers answer."""
    c = ctx.require_cluster()
    if any(h.is_active(f"{c.name}.target") for _, h in ctx.node_hosts()):
        return
    _, first = ctx.node_hosts()[0]
    iface = (c.spec.hosts or [])[0].iface
    try:
        taken = hosts.in_use(first, iface, cluster_addresses(c))
    except hosts.HostsError as err:
        raise PreflightError(str(err)) from err
    if taken:
        listed = ", ".join(f"{ip} ({mac})" for ip, mac in taken)
        raise PreflightError(
            f"addresses in use on the LAN: {listed}; pick another --ip-range or free them"
        )


def run_one(ctx: Context, nodes: list[model.Node] | None) -> str:
    """The single-host checks on ctx.host; the ok line's tail."""
    v = check_tools(ctx)
    bridge_and_forwarding(ctx)
    data_dirs(ctx)
    size = check_host_fits(ctx, nodes) if ctx.cluster is not None else ""
    me = ctx.host.run(["id", "-un"], capture=True).stdout.strip()
    return (
        f"preflight ok: podman {v}, systemd, cgroup v2, swap off, host bridge-nf off, "
        f"ip_forward on, {ctx.paths.data_dir} owned by {me}{', ' + size if size else ''}"
    )


def run_multi(ctx: Context) -> None:
    """Every host is checked before any host is written to: a refusal on the last host must
    not leave sysctl files and drop-ins on the first."""
    c = ctx.require_cluster()
    check_operator(ctx)
    found: dict[str, tuple[str, str]] = {}
    for name, h in ctx.node_hosts():
        want = next(x for x in c.spec.hosts or [] if x.name == name)
        try:
            hosts.check_reachable(h, name)
        except hosts.HostsError as err:
            raise PreflightError(str(err)) from err
        sub = ctx.on(h)
        try:
            v = check_tools(sub)
            size = check_host_fits(sub, [n for n in c.spec.nodes if n.host == name])
        except PreflightError as err:
            raise PreflightError(f"host {name}: {err}") from err
        check_lan(ctx, name, h, want)
        check_name_free(ctx, name, h)
        found[name] = (v, size)
    check_hostnames(ctx)
    check_kernels(ctx)
    check_addresses_free(ctx)
    for name, h in ctx.node_hosts():
        sub = ctx.on(h)
        try:
            bridge_and_forwarding(sub)
            data_dirs(sub)
        except PreflightError as err:
            raise PreflightError(f"host {name}: {err}") from err
        unmanaged_drop_ins(h)
        me = h.run(["id", "-un"], capture=True).stdout.strip()
        v, size = found[name]
        ctx.say(
            f"  host {name}: preflight ok: podman {v}, systemd, cgroup v2, swap off, host "
            f"bridge-nf off, ip_forward on, {ctx.paths.data_dir} owned by {me}, {size}"
        )
    warning = majority_warning(c)
    if warning:
        ctx.say(f"  {warning}")


def run(ctx: Context) -> None:
    try:
        if ctx.cluster is not None and ctx.cluster.multi_host:
            run_multi(ctx)
        else:
            ctx.say(f"  {run_one(ctx, None)}")
    except PreflightError as err:
        raise PhaseFailed(f"preflight: {err}") from err


PHASE = Phase(
    "preflight",
    "check the host: podman, systemd, cgroup v2, swap, sudo, sysctls, data tree, node sizes",
    None,  # no completion state: checked every time
    run,
)
