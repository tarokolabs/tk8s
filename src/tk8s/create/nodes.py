"""nodes: the CNI plugins the nodes mount, then start the cluster target and wait for every
node's systemd and CRI-O and for the router to boot and list the control planes."""

import time

from .. import podman
from ..datatree import owner
from ..phases import Context, Phase, PhaseFailed
from . import router

# ---- cni-plugins


def cni_plugins_done(ctx: Context) -> bool:
    return ctx.host.exists(ctx.paths.cni_dir / "bridge")


def cni_plugins_run(ctx: Context) -> None:
    """Download the plugins into CNI_DIR (copied in place, never rm -r)."""
    cni = ctx.paths.cni_dir
    if ctx.options.offline:
        raise PhaseFailed(
            f"CNI plugins are not installed in {cni} and TK_OFFLINE=1 forbids downloading them"
        )
    ctx.host.mkdir(cni)
    bridge = cni / "bridge"
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
    return podman.exec_(ctx, node, "systemctl", "is-system-running").stdout.strip()


def up_done(ctx: Context) -> bool:
    c = ctx.require_cluster()
    if not ctx.host.is_active(f"{c.name}.target"):
        return False
    if any(systemd_state(ctx, n.name) not in ("running", "degraded") for n in c.spec.nodes):
        return False
    return router.booted(ctx) and router.synced(ctx, c)


def remove_stale_volumes(ctx: Context) -> None:
    """A leftover <node>-var volume from an earlier cluster carries old kubelet/etcd state and
    makes kubeadm preflight fail. Drop it when the node container is new and no cluster exists."""
    c = ctx.require_cluster()
    if ctx.host.exists(ctx.state_dir / "kubeconfig"):
        return
    for n in c.spec.nodes:
        if podman.exists(ctx, "container", n.name):
            continue
        for v in podman.node_volumes(n.name):
            if podman.exists(ctx, "volume", v):
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
        if podman.node_active(ctx, node, "crio"):
            ctx.say(f"  {node}: systemd {state}, crio active")
            return
        if i == 60:
            raise PhaseFailed(
                f"{node}: crio did not become active (podman exec {node} journalctl -u crio)"
            )
        time.sleep(ctx.options.poll)


def up_run(ctx: Context) -> None:
    c = ctx.require_cluster()
    ctx.host.mkdir(ctx.state_dir / "storage")
    ctx.host.mkdir(ctx.state_dir / "logs")
    if not ctx.host.exists(ctx.paths.wulin_dir):
        ctx.host.mkdir(ctx.paths.wulin_dir, sudo=True, owner=owner(ctx))
    remove_stale_volumes(ctx)
    ctx.host.run(["systemctl", "daemon-reload"], sudo=True)
    ctx.host.run(["systemctl", "reset-failed", f"{c.name}*"], sudo=True, check=False)
    ctx.host.run(["systemctl", "enable", "--now", "--quiet", f"{c.name}.target"], sudo=True)
    for n in c.spec.nodes:
        wait_node(ctx, n.name)
    router.wait(ctx)


UP = Phase("up", "start the cluster target and wait for every node and the router", up_done, up_run)

PHASE = Phase("nodes", "start the nodes", None, lambda ctx: None, children=(CNI_PLUGINS, UP))
