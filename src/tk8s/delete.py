"""delete cluster: stop the units, remove the containers, the volumes, the units, the network, the
host routes and the state directory. Every phase's done() is "that thing is already gone", so a
delete that stopped halfway is finished by running it again, and a cluster that lost its
cluster.yaml (an interrupted create, a cleanup by hand) is deleted from what is left."""

from pathlib import Path

from . import kubeconfig, podman
from .lifecycle import ClusterError
from .phases import Context, Phase, confirm
from .podman import node_volumes


def found(ctx: Context) -> bool:
    name = ctx.cluster_name
    return (
        ctx.host.exists(ctx.state_dir)
        or ctx.host.exists(ctx.paths.unit_dir / name)
        or ctx.host.exists(ctx.paths.systemd_dir / f"{name}.target")
        or podman.exists(ctx, "network", ctx.cluster_name)
    )


def confirm_delete(ctx: Context) -> None:
    confirm(
        ctx,
        f"Delete cluster {ctx.cluster_name} including its PVC data under "
        f"{ctx.state_dir}/storage? [y/N] ",
        ClusterError,
    )


def _router_units(ctx: Context) -> set[str]:
    """The router unit names a cluster can have: one router, or router-1 and router-2."""
    n = ctx.cluster_name
    return {f"{n}-router.container", f"{n}-router-1.container", f"{n}-router-2.container"}


def _container_units(ctx: Context) -> list[str]:
    entries = ctx.host.listdir(ctx.paths.unit_dir / ctx.cluster_name)
    return [e for e in entries if e.endswith(".container")]


def node_names(ctx: Context) -> list[str]:
    """From cluster.yaml, or from the unit directory when the file is gone (so containers and
    volumes are removed before units takes that directory away). Every container unit but the
    routers' counts as a node there, a pre-router cluster's `<name>-lb.container` included: its
    cluster.yaml does not parse any more, and this is how its container still goes."""
    if ctx.cluster is not None:
        return [n.name for n in ctx.cluster.spec.nodes]
    routers = _router_units(ctx)
    return [e.removesuffix(".container") for e in _container_units(ctx) if e not in routers]


def router_names(ctx: Context) -> list[str]:
    """The router containers: cluster.yaml's, or whichever router units the directory has."""
    if ctx.cluster is not None:
        return [r.name for r in ctx.cluster.routers]
    routers = _router_units(ctx)
    return [e.removesuffix(".container") for e in _container_units(ctx) if e in routers]


# ---- removals, shared with delete node


def remove_containers(ctx: Context, names: list[str]) -> None:
    for n in names:
        if podman.exists(ctx, "container", n):
            ctx.host.run(["podman", "rm", "-f", n], sudo=True, capture=True)
            ctx.say(f"  removed container {n}")


def remove_volumes(ctx: Context, names: list[str]) -> None:
    for v in names:
        if podman.exists(ctx, "volume", v):
            ctx.host.run(["podman", "volume", "rm", "-f", v], sudo=True, capture=True)
            ctx.say(f"  removed volume {v}")


# ---- stop


def stop_done(ctx: Context) -> bool:
    name = ctx.cluster_name
    target_down = not ctx.host.is_active(f"{name}.target")
    return target_down and not ctx.host.is_active(f"{name}-network.service")


def stop_run(ctx: Context) -> None:
    """The Quadlet network unit is a oneshot that stays active after the target is down; left
    running, a later cluster of the same name never gets its network created."""
    name = ctx.cluster_name
    ctx.host.run(["systemctl", "disable", "--now", f"{name}.target"], sudo=True, check=False)
    ctx.host.run(["systemctl", "stop", f"{name}-network.service"], sudo=True, check=False)


STOP = Phase("stop", "stop the cluster target and its network unit", stop_done, stop_run)


# ---- units


def unit_paths(ctx: Context) -> list[Path]:
    name, p = ctx.cluster_name, ctx.paths
    return [
        p.unit_dir / name,
        p.systemd_dir / f"{name}.target",
        p.systemd_dir / f"{name}-routes.service",
    ]


def units_done(ctx: Context) -> bool:
    return not any(ctx.host.exists(path) for path in unit_paths(ctx))


def units_run(ctx: Context) -> None:
    ctx.host.run(["rm", "-rf", *(str(path) for path in unit_paths(ctx))], sudo=True)
    ctx.host.run(["systemctl", "daemon-reload"], sudo=True)
    ctx.host.run(
        ["systemctl", "reset-failed", f"{ctx.cluster_name}*"], sudo=True, capture=True, check=False
    )


UNITS = Phase(
    "units", "remove the Quadlet units, the target and the routes unit", units_done, units_run
)


# ---- containers


def containers(ctx: Context) -> list[str]:
    return [*node_names(ctx), *router_names(ctx)]


def containers_done(ctx: Context) -> bool:
    return not any(podman.exists(ctx, "container", n) for n in containers(ctx))


def containers_run(ctx: Context) -> None:
    remove_containers(ctx, containers(ctx))


CONTAINERS = Phase(
    "containers", "remove the node and router containers", containers_done, containers_run
)


# ---- volumes


def volumes(ctx: Context) -> list[str]:
    return [v for n in node_names(ctx) for v in node_volumes(n)]


def volumes_done(ctx: Context) -> bool:
    return not any(podman.exists(ctx, "volume", v) for v in volumes(ctx))


def volumes_run(ctx: Context) -> None:
    remove_volumes(ctx, volumes(ctx))


VOLUMES = Phase(
    "volumes",
    "remove the node volumes (/var, /etc, /usr/local/bin of every node)",
    volumes_done,
    volumes_run,
)


# ---- network


def network_done(ctx: Context) -> bool:
    return not podman.exists(ctx, "network", ctx.cluster_name)


def network_run(ctx: Context) -> None:
    ctx.host.run(["podman", "network", "rm", "-f", ctx.cluster_name], sudo=True, capture=True)


NETWORK = Phase("network", "remove the podman network", network_done, network_run)


# ---- routes


def subnets(ctx: Context) -> list[str]:
    if ctx.cluster is None:
        return []
    net = ctx.cluster.spec.network
    return [str(net.pods), str(net.services)]


def route_present(ctx: Context, subnet: str) -> bool:
    r = ctx.host.run(["ip", "route", "show", subnet], capture=True, check=False)
    return bool(r.stdout.strip())


def routes_done(ctx: Context) -> bool:
    return not any(route_present(ctx, s) for s in subnets(ctx))


def routes_run(ctx: Context) -> None:
    for s in subnets(ctx):
        if route_present(ctx, s):
            ctx.host.run(["ip", "route", "del", s], sudo=True)


ROUTES = Phase(
    "routes", "remove the host routes to the pod and service subnets", routes_done, routes_run
)


# ---- state


def state_done(ctx: Context) -> bool:
    return not ctx.host.exists(ctx.state_dir)


def state_run(ctx: Context) -> None:
    """Root wrote here too (PVC data, the apiserver's audit log), so this is sudo."""
    ctx.host.run(["rm", "-rf", str(ctx.state_dir)], sudo=True)


STATE = Phase("state", "remove the state directory, PVC data included", state_done, state_run)


def _kube_config(ctx: Context) -> tuple[Path, str | None]:
    path = Path.home() / ".kube" / "config"
    return path, ctx.host.read(path)


def _server(ctx: Context) -> str | None:
    """What this cluster's entries point at; None without a cluster.yaml (nothing to match)."""
    return f"https://{ctx.cluster.endpoint_ip}:6443" if ctx.cluster is not None else None


def kubeconfig_done(ctx: Context) -> bool:
    _, text = _kube_config(ctx)
    server = _server(ctx)
    if text is None or server is None:
        return True
    try:
        return not kubeconfig.has(text, ctx.cluster_name, server)
    except ValueError:
        return True  # not ours to touch


def kubeconfig_run(ctx: Context) -> None:
    """The cluster's entries leave ~/.kube/config (the previous file kept as config.bak); other
    clusters, and a same-named cluster that points elsewhere, stay. A file that is not a
    kubeconfig is left alone."""
    path, text = _kube_config(ctx)
    server = _server(ctx)
    if text is None or server is None:
        return
    try:
        rest = kubeconfig.remove(text, ctx.cluster_name, server)
    except ValueError:
        return
    if rest == text:
        return
    ctx.host.write(path.with_name("config.bak"), text, mode=0o600)
    if rest is None:
        ctx.host.remove(path)
    else:
        ctx.host.write(path, rest, mode=0o600)


KUBECONFIG = Phase(
    "kubeconfig", "drop the cluster from ~/.kube/config", kubeconfig_done, kubeconfig_run
)

PHASE = Phase(
    "delete",
    "delete the cluster",
    None,
    lambda ctx: None,
    children=(STOP, CONTAINERS, VOLUMES, UNITS, NETWORK, ROUTES, STATE, KUBECONFIG),
)
