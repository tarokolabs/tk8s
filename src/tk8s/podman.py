"""The questions tkctl asks rootful podman about its objects, and the commands it runs inside
a node container. Every caller used to spell these argv out; here they are spelled once, so
the `$` lines a student sees stay the same everywhere."""

from typing import Literal

from .host import Result
from .phases import Context, PhaseFailed

Kind = Literal["container", "volume", "network", "image"]


def exists(ctx: Context, kind: Kind, name: str) -> bool:
    """A container is asked for on the host it runs on; images, volumes and networks on
    ctx.host (per-host callers use ctx.on(host))."""
    host = ctx.host_for(name) if kind == "container" else ctx.host
    r = host.run(["podman", kind, "exists", name], sudo=True, check=False)
    return r.returncode == 0


def node_volumes(node: str) -> list[str]:
    return [f"{node}-var", f"{node}-etc", f"{node}-usr-local-bin"]


def exec_(ctx: Context, node: str, *argv: str, input: str | None = None) -> Result:
    """A command inside the node, as root, output captured, never raising; stdin needs -i."""
    dash_i = ["-i"] if input is not None else []
    return ctx.host_for(node).run(
        ["podman", "exec", *dash_i, node, *argv], sudo=True, input=input, capture=True, check=False
    )


def node_has(ctx: Context, node: str, path: str, flag: str = "-e") -> bool:
    return exec_(ctx, node, "test", flag, path).returncode == 0


def node_read(ctx: Context, node: str, path: str) -> str | None:
    r = exec_(ctx, node, "cat", path)
    return r.stdout if r.returncode == 0 else None


def node_active(ctx: Context, node: str, unit: str) -> bool:
    return exec_(ctx, node, "systemctl", "is-active", "--quiet", unit).returncode == 0


def must(ctx: Context, node: str, *argv: str, input: str | None = None) -> Result:
    """exec_ for a step that has to succeed: a non-zero exit is PhaseFailed, named after the
    node and the first two words of the command."""
    r = exec_(ctx, node, *argv, input=input)
    if r.returncode != 0:
        raise PhaseFailed(f"{node}: {' '.join(argv[:2])} failed: {r.stderr.strip()}")
    return r
