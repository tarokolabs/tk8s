"""What connect refuses, in order, before it touches any cluster: a cluster that is not
running, one created before mesh support, mixed single- and multi-host clusters, two clusters
with one cluster id, Cilium versions more than a minor apart."""

from .. import lifecycle, model
from ..phases import Context
from . import MeshError


def minor(version: str) -> tuple[int, int]:
    """(major, minor) of a Cilium version such as 1.20.4."""
    parts = version.split(".")
    return int(parts[0]), int(parts[1])


def can_federate(c: model.Cluster) -> bool:
    """Created with mesh support: cilium installed with a cluster id (recorded with its
    version) and an address reserved for the clustermesh-apiserver. The one definition
    preflight, describe and enable go by."""
    return (
        c.status is not None
        and c.status.cluster_id is not None
        and c.status.cilium is not None
        and c.spec.network.mesh is not None
    )


def preflight(ctxs: list[Context]) -> None:
    first = ctxs[0].require_cluster()
    for ctx in ctxs:
        c = ctx.require_cluster()
        state = lifecycle.state_of(ctx, c)
        if state == "Creating":
            raise MeshError(
                f"cluster {c.name} is not fully created (tkctl create cluster {c.name})"
            )
        if state == "Degraded":
            raise MeshError(
                f"cluster {c.name} is degraded, a host is down (tkctl verify cluster {c.name})"
            )
        if state != "Running":
            raise MeshError(f"cluster {c.name} is not running (tkctl start cluster {c.name})")
        assert c.status is not None
        if not can_federate(c):
            raise MeshError(
                f"cluster {c.name} was created before mesh support; recreate it to connect"
            )
        if c.multi_host != first.multi_host:
            single, multi = (first, c) if c.multi_host else (c, first)
            raise MeshError(
                f"cluster {single.name} is single-host and {multi.name} is multi-host; "
                "only clusters of one kind can be connected"
            )
    for i, ctx in enumerate(ctxs):
        a = ctx.require_cluster()
        assert a.status is not None
        for other in ctxs[i + 1 :]:
            b = other.require_cluster()
            assert b.status is not None
            if a.status.cluster_id == b.status.cluster_id:
                raise MeshError(
                    f"clusters {a.name} and {b.name} share cluster id {a.status.cluster_id} "
                    f"(subnet index {a.spec.network.index}); recreate one of them"
                )
            x, y = str(a.status.cilium), str(b.status.cilium)
            if minor(x)[0] != minor(y)[0] or abs(minor(x)[1] - minor(y)[1]) > 1:
                raise MeshError(
                    f"cluster {a.name} runs Cilium {x} and {b.name} {y}; "
                    "Cluster Mesh allows one minor version apart"
                )
