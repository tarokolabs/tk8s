"""nodes: the CNI plugins the nodes mount, the LB image in the host store, then start the cluster
target and wait for every node's systemd and CRI-O and for the LB to list the control planes."""

import time

from .. import images
from ..phases import Context, Phase, PhaseFailed

STATS_CMD = (
    'exec 3<>/dev/tcp/127.0.0.1/8404; printf "GET /stats;csv HTTP/1.0\\r\\n\\r\\n" >&3; cat <&3'
)


# ---- cni-plugins


def cni_plugins_done(ctx: Context) -> bool:
    return ctx.host.exists(ctx.paths.cni_dir / "bridge")


def cni_plugins_run(ctx: Context) -> None:
    """Install or refresh the plugins in CNI_DIR (copied in place, never rm -r)."""
    cni = ctx.paths.cni_dir
    if ctx.options.offline:
        raise PhaseFailed(
            f"CNI plugins are not installed in {cni} and TK_OFFLINE=1 forbids downloading them"
        )
    ctx.host.mkdir(cni)
    bridge = cni / "bridge"
    if ctx.host.exists(bridge) and not ctx.host.writable(bridge):
        owner = ctx.host.run(["stat", "-c", "%u", str(bridge)], capture=True).stdout.strip()
        if owner == "0":  # plugins installed by an earlier root-run tkctl
            uid = ctx.host.run(["id", "-u"], capture=True).stdout.strip()
            gid = ctx.host.run(["id", "-g"], capture=True).stdout.strip()
            ctx.host.run(["chown", "-R", f"{uid}:{gid}", str(cni)], sudo=True)
    v = ctx.versions.cni_plugins
    url = (
        f"https://github.com/containernetworking/plugins/releases/download/{v}/"
        f"cni-plugins-linux-amd64-{v}.tgz"
    )
    tgz = ctx.host.run_bytes(["curl", "-fsSL", "--retry", "3", "--retry-all-errors", url])
    ctx.host.run_bytes(["tar", "-xz", "-C", str(cni)], input=tgz)
    ver = ctx.host.run([str(bridge), "--version"], capture=True, check=False).stdout.splitlines()
    ctx.say(f"  CNI plugins {ver[0] if ver else v} in {cni}")


CNI_PLUGINS = Phase(
    "cni-plugins", "install the CNI plugins the nodes mount", cni_plugins_done, cni_plugins_run
)


# ---- up


def systemd_state(ctx: Context, node: str) -> str:
    r = ctx.host.run(
        ["podman", "exec", node, "systemctl", "is-system-running"],
        sudo=True,
        capture=True,
        check=False,
    )
    return r.stdout.strip()


def stats_csv(ctx: Context) -> str:
    c = ctx.require_cluster()
    r = ctx.host.run(
        ["podman", "exec", f"{c.name}-lb", "bash", "-c", STATS_CMD],
        sudo=True,
        capture=True,
        check=False,
    )
    return r.stdout


def lb_backends(ctx: Context) -> int:
    """Rows of the control-planes backend in the LB's stats CSV, the BACKEND summary excluded."""
    rows = (line.split(",") for line in stats_csv(ctx).splitlines())
    return sum(1 for f in rows if len(f) > 1 and f[0] == "control-planes" and f[1] != "BACKEND")


def up_done(ctx: Context) -> bool:
    c = ctx.require_cluster()
    if not ctx.host.is_active(f"{c.name}.target"):
        return False
    if any(systemd_state(ctx, n.name) not in ("running", "degraded") for n in c.spec.nodes):
        return False
    return lb_backends(ctx) >= len(c.control_planes)


def remove_stale_volumes(ctx: Context) -> None:
    """A leftover <node>-var volume from an earlier cluster carries old kubelet/etcd state and
    makes kubeadm preflight fail. Drop it when the node container is new and no cluster exists."""
    c = ctx.require_cluster()
    if ctx.host.exists(ctx.state_dir / "kubeconfig"):
        return
    for n in c.spec.nodes:
        r = ctx.host.run(["podman", "container", "exists", n.name], sudo=True, check=False)
        if r.returncode == 0:
            continue
        for v in (f"{n.name}-var", f"{n.name}-etc", f"{n.name}-usr-local-bin"):
            have = ctx.host.run(["podman", "volume", "exists", v], sudo=True, check=False)
            if have.returncode == 0:
                ctx.host.run(["podman", "volume", "rm", "-f", v], sudo=True, capture=True)
                ctx.say(f"  removed stale volume {v}")


def wait_node(ctx: Context, node: str) -> None:
    """systemd inside the node, then CRI-O: kubeadm preflight fails if the CRI socket is not up
    yet (the first start also copies /etc into its volume)."""
    state = ""
    for i in range(1, 91):
        state = systemd_state(ctx, node)
        if state in ("running", "degraded"):
            break
        if i == 90:
            raise PhaseFailed(
                f"{node} did not reach a running systemd state (journalctl -u {node})"
            )
        time.sleep(ctx.options.poll)
    for i in range(1, 61):
        r = ctx.host.run(
            ["podman", "exec", node, "systemctl", "is-active", "--quiet", "crio"],
            sudo=True,
            check=False,
        )
        if r.returncode == 0:
            ctx.say(f"  {node}: systemd {state}, crio active")
            return
        if i == 60:
            raise PhaseFailed(
                f"{node}: crio did not become active (podman exec {node} journalctl -u crio)"
            )
        time.sleep(ctx.options.poll)


def wait_lb(ctx: Context) -> None:
    """The LB listing every control plane as a backend proves it is up with a good config; the
    backends stay DOWN until kubeadm brings an apiserver up."""
    c = ctx.require_cluster()
    want = len(c.control_planes)
    got = 0
    for i in range(1, 31):
        got = lb_backends(ctx)
        if got >= want:
            ctx.say(f"  {c.name}-lb: {got} control plane backend(s) at {c.spec.network.lb}")
            return
        if i == 30:
            raise PhaseFailed(
                f"{c.name}-lb lists {got} of {want} control planes within 60 s "
                f"(podman logs {c.name}-lb)"
            )
        time.sleep(ctx.options.poll)


def up_run(ctx: Context) -> None:
    c = ctx.require_cluster()
    images.copy_to_host(ctx, c.spec.kubernetes, ctx.versions.haproxy)
    ctx.host.mkdir(ctx.state_dir / "storage")
    ctx.host.mkdir(ctx.state_dir / "logs")
    if not ctx.host.exists(ctx.paths.wulin_dir):
        uid = ctx.host.run(["id", "-u"], capture=True).stdout.strip()
        gid = ctx.host.run(["id", "-g"], capture=True).stdout.strip()
        ctx.host.mkdir(ctx.paths.wulin_dir, sudo=True, owner=f"{uid}:{gid}")
    remove_stale_volumes(ctx)
    ctx.host.run(["systemctl", "daemon-reload"], sudo=True)
    ctx.host.run(["systemctl", "reset-failed", f"{c.name}*"], sudo=True, check=False)
    ctx.host.run(["systemctl", "enable", "--now", "--quiet", f"{c.name}.target"], sudo=True)
    for n in c.spec.nodes:
        wait_node(ctx, n.name)
    wait_lb(ctx)


UP = Phase("up", "start the cluster target and wait for every node and the LB", up_done, up_run)

PHASE = Phase("nodes", "start the nodes", None, lambda ctx: None, children=(CNI_PLUGINS, UP))
