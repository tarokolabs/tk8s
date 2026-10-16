"""router: the VyOS container of a cluster. Plan 1: the image name and the config.boot text;
the wait, the backend status and the reload come with the nodes phase and the node verbs."""

from .. import model, versions


def image(v: versions.Versions) -> str:
    return f"ghcr.io/tarokolabs/tk8s/router:{v.vyos}"


def servers(c: model.Cluster) -> str:
    """The backend's server blocks, one per control plane, in config.boot's own layout."""
    return "\n".join(
        f"            server {n.name} {{\n"
        f'                address "{n.ip}"\n'
        f"                check {{\n"
        f"                }}\n"
        f'                port "6443"\n'
        f"            }}"
        for n in c.control_planes
    )
