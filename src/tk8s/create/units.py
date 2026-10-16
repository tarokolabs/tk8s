"""units: the Quadlet units, the cluster target, the host routes and the router config, rendered
from cluster.yaml; units go under /etc (root), the router's tkctl.boot into the state dir (root
too: VyOS owns that directory), the rest as the user."""

from pathlib import Path

from .. import model
from ..phases import Context, Phase
from ..render import template
from . import router


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
    files[state / "router" / "tkctl.boot"] = template(
        p.home,
        "router/tkctl.boot",
        cluster=c.name,
        router_ip=net.router,
        gateway=net.gateway,
        servers=router.servers(c),
    )
    files[state / "router" / "scripts" / "vyos-preconfig-bootup.script"] = template(
        p.home, "router/vyos-preconfig-bootup.script", cluster=c.name
    )
    files[state / "router" / "scripts" / "vyos-postconfig-bootup.script"] = template(
        p.home, "router/vyos-postconfig-bootup.script", cluster=c.name, router_ip=net.router
    )
    files[unit_dir / f"{c.name}-router.container"] = template(
        p.home,
        "router.container",
        cluster=c.name,
        router_ip=net.router,
        router_image=router.image(v),
        state_dir=state,
    )
    for n in c.spec.nodes:
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
            router_ip=net.router,
            cni_dir=p.cni_dir,
            images_dir=p.images_dir,
            k8s=c.spec.kubernetes,
            state_dir=state,
            wulin_dir=p.wulin_dir,
            cpu=n.cpu,
            memory=n.memory,
        )
    wants = [
        f"{c.name}-router.service",
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
    )
    return files


def needs_sudo(ctx: Context, path: Path) -> bool:
    """Units live under /etc; the router directory is written by VyOS (root in the container)."""
    router_dir = ctx.paths.state_dir(ctx.cluster_name) / "router"
    return not path.is_relative_to(ctx.paths.data_dir) or path.is_relative_to(router_dir)


def done(ctx: Context, cluster: model.Cluster | None = None) -> bool:
    """Every file is on the host with exactly this content (a changed cluster.yaml re-renders)."""
    return all(
        ctx.host.read(path, sudo=needs_sudo(ctx, path)) == content
        for path, content in render(ctx, cluster).items()
    )


def run(ctx: Context, cluster: model.Cluster | None = None) -> None:
    for path, content in render(ctx, cluster).items():
        mode = 0o755 if path.suffix == ".script" else 0o644
        ctx.host.write(path, content, sudo=needs_sudo(ctx, path), mode=mode)


PHASE = Phase(
    "units", "write the Quadlet units, the target, the routes and the router config", done, run
)
