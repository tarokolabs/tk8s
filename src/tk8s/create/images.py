"""images: the node image on the host and every image the cluster needs in the shared store."""

from .. import images
from ..phases import Context, Phase


def done(ctx: Context) -> bool:
    k8s = ctx.require_cluster().spec.kubernetes
    return images.node_image_present(ctx, k8s) and all(
        s == "present" for s, _ in images.status(ctx, k8s)
    )


def run(ctx: Context) -> None:
    images.ensure(ctx, ctx.require_cluster().spec.kubernetes)


PHASE = Phase("images", "pull every image the cluster needs into the shared store", done, run)
