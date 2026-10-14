"""The user-owned data tree under TK_DATA_DIR: clusters, cni, images, cache.

Shared by preflight and the image store, because `tkctl pull images` may be the first command on a
fresh host. Only creating the tree under /opt needs root; a root-owned directory (an earlier
root-run tkctl, or podman creating a bind-mount source) is taken over, another user's is refused.
"""

from .phases import Context


class PreflightError(Exception):
    pass


def data_dirs(ctx: Context) -> None:
    p = ctx.paths
    uid = ctx.host.run(["id", "-u"], capture=True).stdout.strip()
    gid = ctx.host.run(["id", "-g"], capture=True).stdout.strip()
    for d in (p.data_dir, p.clusters_dir, p.cni_dir, p.images_dir, p.cache_dir):
        if not ctx.host.exists(d):
            ctx.host.mkdir(d, sudo=True, owner=f"{uid}:{gid}")
        elif not ctx.host.writable(d):
            owner = ctx.host.run(["stat", "-c", "%u", str(d)], capture=True).stdout.strip()
            if owner == "0":
                ctx.host.run(["chown", f"{uid}:{gid}", str(d)], sudo=True)
            else:
                name = ctx.host.run(["stat", "-c", "%U", str(d)], capture=True).stdout.strip()
                me = ctx.host.run(["id", "-un"], capture=True).stdout.strip()
                raise PreflightError(
                    f"{d} is owned by {name}, not {me}; set TK_DATA_DIR to a directory of "
                    "your own or fix its ownership"
                )
