"""images: the node image on the host and every image the cluster needs in the shared store.
On several hosts every node host gets its own node and router images and store; the list of
images is computed once, on the operator (it takes the node image and the cilium CLI, which
only the operator has)."""

from .. import images, podman
from ..phases import Context, Phase
from . import router


def _on_operator(ctx: Context, k8s: str) -> bool:
    return (
        images.node_image_present(ctx, k8s)
        and podman.exists(ctx, "image", router.image(ctx.versions))
        and images.cilium_cli_present(ctx)
    )


def done(ctx: Context) -> bool:
    """Everything ensure() provides: the node and router images and the cilium CLI on the host,
    then every wanted image in the store (on every node host). Only looks; a missing piece is
    run()'s to fetch."""
    c = ctx.require_cluster()
    k8s = c.spec.kubernetes
    if not _on_operator(ctx, k8s):
        return False
    if not c.multi_host:
        return all(s == "present" for s, _ in images.status(ctx, k8s))
    want = images.wanted(ctx, k8s)
    for _, h in ctx.node_hosts():
        sub = ctx.on(h)
        if h is not ctx.host and not (
            images.node_image_present(sub, k8s)
            and podman.exists(sub, "image", router.image(ctx.versions))
        ):
            return False
        if not all(s == "present" for s, _ in images.status(sub, k8s, images=want)):
            return False
    return True


def run(ctx: Context) -> None:
    c = ctx.require_cluster()
    k8s = c.spec.kubernetes
    if not c.multi_host:
        images.ensure(ctx, k8s)
        return
    images.ensure_node_image(ctx, k8s)
    images.ensure_router_image(ctx)
    images.ensure_cilium_cli(ctx)
    want = images.wanted(ctx, k8s)
    for name, h in ctx.node_hosts():
        ctx.say(f"  host {name}:")
        images.ensure(ctx.on(h), k8s, images=want, cli=False)


PHASE = Phase("images", "pull every image the cluster needs into the shared store", done, run)
