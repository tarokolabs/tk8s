"""nodes: the CNI plugins the nodes mount, then start the cluster target and wait for every
node's systemd and CRI-O and for the router to boot and list the control planes."""

from .. import model, podman
from ..datatree import owner
from ..phases import Context, Phase, PhaseFailed, wait_for
from . import router

# ---- cni-plugins


def cni_plugins_done(ctx: Context) -> bool:
    return all(h.exists(ctx.paths.cni_dir / "bridge") for _, h in ctx.node_hosts())


def cni_plugins_run(ctx: Context) -> None:
    """Download the plugins into CNI_DIR on every node host that lacks them."""
    for name, h in ctx.node_hosts():
        if h.exists(ctx.paths.cni_dir / "bridge"):
            continue
        if name:
            ctx.say(f"  host {name}:")
        _cni_plugins_into(ctx.on(h))


def _cni_plugins_into(ctx: Context) -> None:
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
    if not all(h.is_active(f"{c.name}.target") for _, h in ctx.node_hosts()):
        return False
    if any(systemd_state(ctx, n.name) not in ("running", "degraded") for n in c.spec.nodes):
        return False
    return router.all_booted_and_synced(ctx, c)


def remove_stale_volumes(ctx: Context, nodes: list[model.Node] | None = None) -> None:
    """A leftover <node>-var volume from an earlier cluster carries old kubelet/etcd state and
    makes kubeadm preflight fail. Drop it when the node container is new and no cluster exists.
    `nodes`: the ones on ctx.host (a host of a multi-host cluster); the kubeconfig that says
    "a cluster exists" is the operator's, so the caller passes `has_kubeconfig`."""
    c = ctx.require_cluster()
    if nodes is None and ctx.host.exists(ctx.state_dir / "kubeconfig"):
        return
    for n in c.spec.nodes if nodes is None else nodes:
        if podman.exists(ctx, "container", n.name):
            continue
        for v in podman.node_volumes(n.name):
            if podman.exists(ctx, "volume", v):
                ctx.host.run(["podman", "volume", "rm", "-f", v], sudo=True, capture=True)
                ctx.say(f"  removed stale volume {v}")


def wait_node(ctx: Context, node: str) -> None:
    """systemd inside the node, then CRI-O: kubeadm preflight fails if the CRI socket is not up
    yet (the first start also copies /etc into its volume)."""
    last: list[str] = []

    def systemd_up() -> bool:
        last.append(systemd_state(ctx, node))
        return last[-1] in ("running", "degraded")

    wait_for(
        ctx,
        systemd_up,
        rounds=90,
        delay=ctx.options.poll,
        failure=lambda: f"{node} did not reach a running systemd state (journalctl -u {node})",
    )
    wait_for(
        ctx,
        lambda: podman.node_active(ctx, node, "crio"),
        rounds=60,
        delay=ctx.options.poll,
        failure=lambda: (
            f"{node}: crio did not become active (podman exec {node} journalctl -u crio)"
        ),
    )
    ctx.say(f"  {node}: systemd {last[-1]}, crio active")


def _start_on(ctx: Context, nodes: list[model.Node] | None) -> None:
    """The host-side start: the directories the nodes mount, stale volumes, the target."""
    c = ctx.require_cluster()
    ctx.host.mkdir(ctx.state_dir / "storage")
    ctx.host.mkdir(ctx.state_dir / "logs")
    if not ctx.host.exists(ctx.paths.wulin_dir):
        ctx.host.mkdir(ctx.paths.wulin_dir, sudo=True, owner=owner(ctx))
    remove_stale_volumes(ctx, nodes)
    ctx.host.run(["systemctl", "daemon-reload"], sudo=True)
    ctx.host.run(["systemctl", "reset-failed", f"{c.name}*"], sudo=True, check=False)
    ctx.host.run(["systemctl", "enable", "--now", "--quiet", f"{c.name}.target"], sudo=True)


def up_run(ctx: Context) -> None:
    c = ctx.require_cluster()
    if not c.multi_host:
        _start_on(ctx, None)
    else:
        fresh = not ctx.host.exists(ctx.state_dir / "kubeconfig")
        for name, h in ctx.node_hosts():
            ctx.say(f"  host {name}:")
            mine = [n for n in c.spec.nodes if n.host == name] if fresh else []
            _start_on(ctx.on(h), mine)
    for n in c.spec.nodes:
        wait_node(ctx, n.name)
    router.wait(ctx)


UP = Phase("up", "start the cluster target and wait for every node and the router", up_done, up_run)

PHASE = Phase("nodes", "start the nodes", None, lambda ctx: None, children=(CNI_PLUGINS, UP))
