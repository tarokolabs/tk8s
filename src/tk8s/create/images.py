"""images: the node image on the host and every image the cluster needs in the shared store."""

from .. import images, podman
from ..phases import Context, Phase
from . import router


def done(ctx: Context) -> bool:
    """Everything ensure() provides: the node and router images and the cilium CLI on the host,
    then every wanted image in the store. Only looks; a missing piece is run()'s to fetch."""
    k8s = ctx.require_cluster().spec.kubernetes
    on_host = (
        images.node_image_present(ctx, k8s)
        and podman.exists(ctx, "image", router.image(ctx.versions))
        and images.cilium_cli_present(ctx)
    )
    return on_host and all(s == "present" for s, _ in images.status(ctx, k8s))


def run(ctx: Context) -> None:
    images.ensure(ctx, ctx.require_cluster().spec.kubernetes)


PHASE = Phase("images", "pull every image the cluster needs into the shared store", done, run)
