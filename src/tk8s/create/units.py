"""units: the Quadlet units, the cluster target, the host routes and the router config, rendered
from cluster.yaml; units go under /etc (root), the router's tkctl.boot into the state dir (root
too: VyOS owns that directory), the rest as the user."""

from pathlib import Path

from .. import model
from ..phases import Context, Phase
from ..render import template
from . import nodefs, router, router_config


def render(ctx: Context, cluster: model.Cluster | None = None) -> dict[Path, str]:
    """Every file this phase owns, path -> content, in the order they are written. delete node
    renders for the cluster without the node before cluster.yaml says so."""
    c = cluster or ctx.require_cluster()
    p, v = ctx.paths, ctx.versions
    state, unit_dir = p.state_dir(c.name), p.unit_dir / c.name
    net, cp = c.spec.network, c.first_control_plane
    files: dict[Path, str] = {}
    files[unit_dir / f"{c.name}.network"] = template(
        p.home,
        "cluster.network",
        cluster=c.name,
        nodes=net.nodes,
        gateway=net.gateway,
        router_ip=net.router,
    )
    blocks: dict[str, object]
    if net.ha == "proxy":
        boot, blocks = "router/tkctl-proxy.boot", {"servers": router_config.servers(c)}
    else:
        # the reject route at distance 254 loses to any BGP-learned VIP (distance 20); with no
        # control plane announcing, the router refuses at once instead of looping the packet
        # back to the host, whose VIP/32 route points here
        boot = "router/tkctl-bgp.boot"
        blocks = {
            "neighbors": router_config.neighbors(c),
            "bfd_peers": router_config.bfd_peers(c),
            "vip": net.vip,
        }
    for r in c.routers:
        # the same tkctl.boot per router but for its own address, host name and VRRP priority;
        # the VRRP block (two routers only) goes first, config.boot keeps its sections sorted
        vrrp = router_config.vrrp_block(c, r)
        files[state / r.dir / "tkctl.boot"] = template(
            p.home,
            boot,
            router_ip=r.ip,
            router_name=r.name,
            gateway=net.gateway,
            vrrp=vrrp + "\n" if vrrp else "",
            **blocks,
        )
        files[state / r.dir / "scripts" / "vyos-preconfig-bootup.script"] = template(
            p.home, "router/vyos-preconfig-bootup.script", cluster=c.name
        )
        files[state / r.dir / "scripts" / "vyos-postconfig-bootup.script"] = template(
            p.home,
            "router/vyos-postconfig-bootup.script",
            cluster=c.name,
            floating_ip=c.floating_ip,
        )
        files[unit_dir / f"{r.name}.container"] = template(
            p.home,
            "router.container",
            cluster=c.name,
            router_name=r.name,
            router_dir=r.dir,
            router_ip=r.ip,
            router_image=router.image(v),
            state_dir=state,
        )
    for n in c.spec.nodes:
        # the files the node mounts over /proc/meminfo, /sys/devices/system/node and the kubelet
        # drop-in directory (nodefs); a cluster from before node sizing gets none and today's unit
        node_mounts = ""
        if sized(c):
            d = nodefs.node_dir(state, n.name)
            files.update(nodefs.files(d, n))
            node_mounts = nodefs.mounts(d)
        files[unit_dir / f"{n.name}.container"] = template(
            p.home,
            "node.container",
            cluster=c.name,
            role=n.role,
            name=n.name,
            ip=n.ip,
            after="" if n is cp else f"\nAfter={cp.name}.service",
            image=c.node_image,
            domain=c.domain,
            api_ip=c.endpoint_ip,
            cni_dir=p.cni_dir,
            images_dir=p.images_dir,
            k8s=c.spec.kubernetes,
            state_dir=state,
            wulin_dir=p.wulin_dir,
            cpu=n.cpu,
            memory=n.memory,
            node_mounts=node_mounts,
        )
    wants = [
        *(f"{r.name}.service" for r in c.routers),
        *(f"{n.name}.service" for n in c.spec.nodes),
        f"{c.name}-routes.service",
    ]
    files[p.systemd_dir / f"{c.name}.target"] = template(
        p.home, "cluster.target", cluster=c.name, wants=" ".join(wants)
    )
    files[p.systemd_dir / f"{c.name}-routes.service"] = template(
        p.home,
        "routes.service",
        cluster=c.name,
        cp_name=cp.name,
        cp_ip=cp.ip,
        pods=net.pods,
        services=net.services,
        vip_start=""
        if net.vip is None
        else f"\nExecStart=ip route replace {net.vip}/32 via {net.router}",
        vip_stop=""
        if net.vip is None
        else f"\nExecStop=-ip route del {net.vip}/32 via {net.router}",
    )
    return files


def sized(c: model.Cluster) -> bool:
    """Created by a tkctl that sizes nodes: status.node_capacity. Older clusters keep the units
    they were created with."""
    return c.status is not None and c.status.node_capacity


def links(ctx: Context, cluster: model.Cluster | None = None) -> dict[Path, Path]:
    """The cpu symlinks of every node's fake NUMA node, path -> target (none for an old cluster)."""
    c = cluster or ctx.require_cluster()
    if not sized(c):
        return {}
    state = ctx.paths.state_dir(c.name)
    out: dict[Path, Path] = {}
    for n in c.spec.nodes:
        out.update(nodefs.links(nodefs.node_dir(state, n.name), n))
    return out


def needs_sudo(ctx: Context, path: Path) -> bool:
    """Units live under /etc; the router directories are written by VyOS (root in the container)."""
    state = ctx.paths.state_dir(ctx.cluster_name)
    router_dirs = [state / r.dir for r in ctx.require_cluster().routers]
    return not path.is_relative_to(ctx.paths.data_dir) or any(
        path.is_relative_to(d) for d in router_dirs
    )


def done(ctx: Context, cluster: model.Cluster | None = None) -> bool:
    """Every file is on the host with exactly this content (a changed cluster.yaml re-renders)."""
    return all(
        ctx.host.read(path, sudo=needs_sudo(ctx, path)) == content
        for path, content in render(ctx, cluster).items()
    ) and all(ctx.host.readlink(path) == target for path, target in links(ctx, cluster).items())


def run(ctx: Context, cluster: model.Cluster | None = None) -> None:
    for path, content in render(ctx, cluster).items():
        mode = 0o755 if path.suffix == ".script" else 0o644
        ctx.host.write(path, content, sudo=needs_sudo(ctx, path), mode=mode)
    for path, target in links(ctx, cluster).items():
        ctx.host.symlink(path, target)


PHASE = Phase(
    "units", "write the Quadlet units, the target, the routes and the router config", done, run
)
