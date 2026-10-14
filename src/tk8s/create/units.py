"""units: the Quadlet units, the cluster target, the host routes and the LB config, rendered from
cluster.yaml; units go under /etc (root), haproxy.cfg into the state dir (the user)."""

from pathlib import Path

from .. import model
from ..phases import Context, Phase
from ..render import template


def render(ctx: Context, cluster: model.Cluster | None = None) -> dict[Path, str]:
    """Every file this phase owns, path -> content, in the order they are written. delete node
    renders for the cluster without the node before cluster.yaml says so."""
    c = cluster or ctx.require_cluster()
    p, v = ctx.paths, ctx.versions
    state, unit_dir = p.state_dir(c.name), p.unit_dir / c.name
    net, cp = c.spec.network, c.first_control_plane
    files: dict[Path, str] = {}
    files[unit_dir / f"{c.name}.network"] = template(
        p.home, "cluster.network", cluster=c.name, nodes=net.nodes, gateway=net.gateway
    )
    files[state / "lb" / "haproxy.cfg"] = template(
        p.home,
        "haproxy.cfg",
        cluster=c.name,
        lb_ip=net.lb,
        servers="\n".join(f"    server {n.name} {n.ip}:6443" for n in c.control_planes),
    )
    files[unit_dir / f"{c.name}-lb.container"] = template(
        p.home,
        "lb.container",
        cluster=c.name,
        lb_ip=net.lb,
        haproxy_image=v.haproxy,
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
            lb_ip=net.lb,
            cni_dir=p.cni_dir,
            images_dir=p.images_dir,
            k8s=c.spec.kubernetes,
            state_dir=state,
            wulin_dir=p.wulin_dir,
            cpu=n.cpu,
            memory=n.memory,
        )
    wants = [
        f"{c.name}-lb.service",
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


def _under_etc(ctx: Context, path: Path) -> bool:
    return not path.is_relative_to(ctx.paths.data_dir)


def done(ctx: Context, cluster: model.Cluster | None = None) -> bool:
    """Every file is on the host with exactly this content (a changed cluster.yaml re-renders)."""
    return all(
        ctx.host.read(path, sudo=_under_etc(ctx, path)) == content
        for path, content in render(ctx, cluster).items()
    )


def run(ctx: Context, cluster: model.Cluster | None = None) -> None:
    for path, content in render(ctx, cluster).items():
        ctx.host.write(path, content, sudo=_under_etc(ctx, path))


PHASE = Phase(
    "units", "write the Quadlet units, the target, the routes and the LB config", done, run
)
