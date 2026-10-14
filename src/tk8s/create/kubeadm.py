"""kubeadm: the init configuration, then init on the first control plane and join for the rest.

Plan 1 ships the render sub-phase; init, join and kubeconfig follow with the same shape.
"""

from ..phases import Context, Phase
from ..render import template


def render_init(ctx: Context) -> str:
    c = ctx.require_cluster()
    cp, net = c.first_control_plane, c.spec.network
    sans = [f"api.{c.domain}", str(net.lb), *(str(n.ip) for n in c.control_planes)]
    return template(
        ctx.paths.home,
        "init-config.yaml",
        cluster=c.name,
        cp_ip=cp.ip,
        cp_name=cp.name,
        k8s=c.spec.kubernetes,
        domain=c.domain,
        pods=net.pods,
        services=net.services,
        cert_sans="\n".join(f"    - {s}" for s in sans),
    )


def render_done(ctx: Context) -> bool:
    return ctx.host.read(ctx.state_dir / "init-config.yaml") == render_init(ctx)


def render_run(ctx: Context) -> None:
    ctx.host.write(ctx.state_dir / "init-config.yaml", render_init(ctx), mode=0o600)


RENDER = Phase(
    "render", "write the kubeadm init configuration into the state dir", render_done, render_run
)
PHASE = Phase(
    "kubeadm",
    "initialise the first control plane and join the others",
    lambda ctx: False,
    lambda ctx: None,
    children=(RENDER,),
)
