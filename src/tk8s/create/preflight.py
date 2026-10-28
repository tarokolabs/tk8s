"""preflight: the host can run tk8s (podman >= 5.4, systemd, cgroup v2, swap off, passwordless
sudo, curl), bridged frames stay out of the host firewall, forwarding is on, the data tree is the
user's, and the host has the cores and memory the planned nodes ask for. Read-only apart from two
sysctl files and the data tree; runs every time."""

from pathlib import Path

from ..datatree import PreflightError, data_dirs
from ..phases import Context, Phase, PhaseFailed
from . import nodefs

__all__ = ["PHASE", "PreflightError", "data_dirs"]

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


def check_host_fits(ctx: Context) -> str:
    """Every planned node fits the host: a node sized above the host's cores or MemTotal would
    report a capacity the host cannot back (and podman refuses --cpus above the core count only
    after the units are written). Returns the host size for the ok line."""
    cpus = int(ctx.host.run(["nproc"], capture=True).stdout.strip())
    meminfo = ctx.host.read(Path("/proc/meminfo")) or ""
    kb = int(next(ln.split()[1] for ln in meminfo.splitlines() if ln.startswith("MemTotal:")))
    for n in ctx.require_cluster().spec.nodes:
        if n.cpu > cpus:
            raise PreflightError(f"node {n.name} asks for {n.cpu} cpu, the host has {cpus}")
        if nodefs.memory_bytes(n.memory) > kb * 1024:
            raise PreflightError(f"node {n.name} asks for {n.memory} memory, the host has {kb} kB")
    return f"host {cpus} cpu, {kb} kB"


def run(ctx: Context) -> None:
    try:
        v = check_tools(ctx)
        bridge_and_forwarding(ctx)
        data_dirs(ctx)
        size = check_host_fits(ctx) if ctx.cluster is not None else ""
    except PreflightError as err:
        raise PhaseFailed(f"preflight: {err}") from err
    me = ctx.host.run(["id", "-un"], capture=True).stdout.strip()
    ctx.say(
        f"  preflight ok: podman {v}, systemd, cgroup v2, swap off, host bridge-nf off, "
        f"ip_forward on, {ctx.paths.data_dir} owned by {me}{', ' + size if size else ''}"
    )


PHASE = Phase(
    "preflight",
    "check the host: podman, systemd, cgroup v2, swap, sudo, sysctls, data tree, node sizes",
    None,  # no completion state: checked every time
    run,
)
