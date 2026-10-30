"""delete cluster: stop the units, remove the containers, the volumes, the units, the network, the
host routes and the state directory. Every phase's done() is "that thing is already gone", so a
delete that stopped halfway is finished by running it again, and a cluster that lost its
cluster.yaml (an interrupted create, a cleanup by hand) is deleted from what is left.

On several hosts the host-side phases (stop, units, network, routes, state) run on every host of
the cluster, each its own; containers and volumes go on the host that runs them. Without a
cluster.yaml only this host is known, and only this host is cleaned."""

from collections.abc import Callable
from pathlib import Path

from . import kubeconfig, lifecycle, podman
from .create.units import shim_name
from .host import Host
from .lifecycle import ClusterError
from .mesh import ops as mesh
from .phases import Context, Phase, confirm
from .podman import node_volumes


def targets(ctx: Context) -> list[tuple[str, Host]]:
    """The hosts the host-side phases visit: the node hosts and, when it runs no node, the
    operator; the operator last, so cluster.yaml (the list of hosts) goes last of all."""
    return ctx.target_hosts()


def per_host(
    done: Callable[[Context], bool], run: Callable[[Context], None]
) -> tuple[Callable[[Context], bool], Callable[[Context], None]]:
    """A host-side phase on every target: done when done everywhere, run where it is not."""

    def done_all(ctx: Context) -> bool:
        return all(done(ctx.on(h)) for _, h in targets(ctx))

    def run_each(ctx: Context) -> None:
        hosts = targets(ctx)
        for name, h in hosts:
            sub = ctx.on(h)
            if len(hosts) > 1 and done(sub):  # one host: the runner just asked
                continue
            if name:
                ctx.say(f"  host {name}:")
            run(sub)

    return done_all, run_each


def found(ctx: Context) -> bool:
    name = ctx.cluster_name
    return any(
        h.exists(ctx.state_dir)
        or h.exists(ctx.paths.unit_dir / name)
        or h.exists(ctx.paths.systemd_dir / f"{name}.target")
        or podman.exists(ctx.on(h), "network", name)
        for _, h in targets(ctx)
    )


# ---- mesh: leave the Cluster Mesh before anything is stopped


def mesh_done(ctx: Context) -> bool:
    c = ctx.cluster
    return c is None or c.status is None or c.status.mesh == []


def mesh_run(ctx: Context) -> None:
    """Disconnect from every peer that can be reached; a peer that is gone or stopped is
    forgotten on this side only, with a warning (its record goes with its own disconnect)."""
    c = ctx.require_cluster()
    assert c.status is not None
    for peer in list(c.status.mesh):
        pc = mesh.peer_context(ctx, peer)
        if pc is not None and lifecycle.state_of(pc, pc.require_cluster()) == "Running":
            mesh.disconnect(ctx, pc, peer)
            continue
        mesh.forget_peer(ctx, peer)
        ctx.say(
            f"  warning: peer {peer} is not reachable; {peer} still lists {c.name} "
            f"(tkctl disconnect cluster {peer} {c.name} there)"
        )


MESH = Phase("mesh", "disconnect from every peer of the mesh", mesh_done, mesh_run)


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
    """Each container on the host that runs it."""
    for n in names:
        if podman.exists(ctx, "container", n):
            ctx.host_for(n).run(["podman", "rm", "-f", n], sudo=True, capture=True)
            ctx.say(f"  removed container {n}")


def remove_node_volumes(ctx: Context, node: str) -> None:
    """The node's volumes, on the host that runs it."""
    sub = ctx.on(ctx.host_for(node))
    for v in node_volumes(node):
        if podman.exists(sub, "volume", v):
            sub.host.run(["podman", "volume", "rm", "-f", v], sudo=True, capture=True)
            ctx.say(f"  removed volume {v}")


def node_volumes_gone(ctx: Context, node: str) -> bool:
    sub = ctx.on(ctx.host_for(node))
    return not any(podman.exists(sub, "volume", v) for v in node_volumes(node))


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


def _note_this_host_only(ctx: Context) -> None:
    """A multi-host cluster without its cluster.yaml: the other hosts cannot be found."""
    if ctx.cluster is not None:
        return
    net = ctx.host.read(ctx.paths.unit_dir / ctx.cluster_name / f"{ctx.cluster_name}.network")
    if net and "Driver=macvlan" in net:
        ctx.say(
            "  no cluster.yaml: cleaning this host only; other hosts keep their units until "
            "tkctl delete cluster runs there"
        )


def _stop_run(ctx: Context) -> None:
    _note_this_host_only(ctx)
    stop_run(ctx)


STOP = Phase(
    "stop", "stop the cluster target and its network unit", *per_host(stop_done, _stop_run)
)


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
    "units",
    "remove the Quadlet units, the target and the routes unit",
    *per_host(units_done, units_run),
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


def volumes_done(ctx: Context) -> bool:
    return all(node_volumes_gone(ctx, n) for n in node_names(ctx))


def volumes_run(ctx: Context) -> None:
    for n in node_names(ctx):
        remove_node_volumes(ctx, n)


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


NETWORK = Phase("network", "remove the podman network", *per_host(network_done, network_run))


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
    if ctx.cluster is not None and ctx.cluster.multi_host:
        # the shim goes with the routes unit; a host that never had one answers "not found"
        ctx.host.run(["ip", "link", "del", shim_name(ctx.cluster.name)], sudo=True, check=False)


ROUTES = Phase(
    "routes",
    "remove the host routes to the pod and service subnets (and the shim)",
    *per_host(routes_done, routes_run),
)


# ---- state


def state_done(ctx: Context) -> bool:
    return not ctx.host.exists(ctx.state_dir)


def state_run(ctx: Context) -> None:
    """Root wrote here too (PVC data, the apiserver's audit log), so this is sudo."""
    ctx.host.run(["rm", "-rf", str(ctx.state_dir)], sudo=True)


STATE = Phase(
    "state", "remove the state directory, PVC data included", *per_host(state_done, state_run)
)


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
    children=(MESH, STOP, CONTAINERS, VOLUMES, UNITS, NETWORK, ROUTES, STATE, KUBECONFIG),
)
