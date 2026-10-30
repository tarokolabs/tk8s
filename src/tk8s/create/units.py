"""units: the Quadlet units, the cluster target, the host routes and the router config, rendered
from cluster.yaml; units go under /etc (root), the router's tkctl.boot into the state dir (root
too: VyOS owns that directory), the rest as the user.

On several hosts every host gets its own set: its macvlan network (the parent interface is its
own), the units of the nodes and routers placed on it, a target that wants just those, and a
routes unit that also builds the host's macvlan shim. An operator that runs no node gets the
target and the plain routes unit."""

import hashlib
import re
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, summarize_address_range
from pathlib import Path

from .. import model
from ..host import Host
from ..phases import Context, Phase
from ..render import template
from . import nodefs, router, router_config


def mac(ip: IPv4Address) -> str:
    """A fixed, locally administered MAC for a container address: netavark picks a random one
    at every start (Quadlet recreates the container), and a LAN that knew the old one would
    keep sending to it until its ARP entry aged out."""
    return "02:54:4b:" + ":".join(f"{b:02x}" for b in ip.packed[1:])


def shim_name(cluster: str) -> str:
    """The host's macvlan shim interface, within the kernel's 15 characters."""
    if len(cluster) <= 10:
        return f"tk8s-{cluster}"
    return f"tk8s-{cluster[:7]}-{hashlib.sha1(cluster.encode()).hexdigest()[:2]}"


def range_prefixes(rng: str) -> list[IPv4Network]:
    """The pool's segments as prefixes, for the shim's routes."""
    out: list[IPv4Network] = []
    for seg in rng.split(","):
        a_text, _, b_text = seg.partition("-")
        a, b = IPv4Address(a_text), IPv4Address(b_text or a_text)
        out += list(summarize_address_range(a, b))
    return out


# the resolvers of a macvlan container (the bridge network hands them out itself)
MACVLAN_DNS = "\nDNS=1.1.1.1\nDNS=1.0.0.1\nDNS=8.8.8.8\nDNS=8.8.4.4"


@dataclass(frozen=True)
class HostFiles:
    """What one host gets: the files in write order and the fake NUMA cpu symlinks."""

    name: str  # "" on a single host, "operator" for an operator that runs no node
    host: Host
    files: dict[Path, str]
    links: dict[Path, Path]


def render(
    ctx: Context, cluster: model.Cluster | None = None, host_name: str | None = None
) -> dict[Path, str]:
    """Every file this phase owns, path -> content, in the order they are written. delete node
    renders for the cluster without the node before cluster.yaml says so. On several hosts the
    files of one host (`host_name`; "operator" for an operator that runs no node)."""
    c = cluster or ctx.require_cluster()
    if c.multi_host and host_name is None:
        raise ValueError("a multi-host cluster renders per host (per_host)")
    p, v = ctx.paths, ctx.versions
    state, unit_dir = p.state_dir(c.name), p.unit_dir / c.name
    net, cp = c.spec.network, c.first_control_plane
    me = next((h for h in c.spec.hosts or [] if h.name == host_name), None)
    mine_nodes = [n for n in c.spec.nodes if not c.multi_host or n.host == host_name]
    mine_routers = [r for r in c.routers if not c.multi_host or r.host == host_name]
    files: dict[Path, str] = {}
    if me is not None:
        files[unit_dir / f"{c.name}.network"] = template(
            p.home,
            "cluster-macvlan.network",
            cluster=c.name,
            parent=me.iface,
            nodes=net.nodes,
            gateway=net.gateway,
            router_ip=net.router,
        )
    elif not c.multi_host:
        files[unit_dir / f"{c.name}.network"] = template(
            p.home,
            "cluster.network",
            cluster=c.name,
            nodes=net.nodes,
            gateway=net.gateway,
            router_ip=net.router,
        )
    blocks: dict[str, object]
    if net.ha == "proxy":
        boot, blocks = "router/tkctl-proxy.boot", {"servers": router_config.servers(c)}
    else:
        # the reject route at distance 254 loses to any BGP-learned VIP (distance 20); with no
        # control plane announcing, the router refuses at once instead of looping the packet
        # back to the host, whose VIP/32 route points here
        boot = "router/tkctl-bgp.boot"
        blocks = {
            "neighbors": router_config.neighbors(c),
            "bfd_peers": router_config.bfd_peers(c),
            "vip": net.vip,
        }
    for r in mine_routers:
        # the same tkctl.boot per router but for its own address, host name and VRRP priority;
        # the VRRP block (two routers only) goes first, config.boot keeps its sections sorted
        vrrp = router_config.vrrp_block(c, r)
        files[state / r.dir / "tkctl.boot"] = template(
            p.home,
            boot,
            router_ip=r.ip,
            router_name=r.name,
            prefix=net.nodes.prefixlen,
            gateway=net.gateway,
            vrrp=vrrp + "\n" if vrrp else "",
            **blocks,
        )
        files[state / r.dir / "scripts" / "vyos-preconfig-bootup.script"] = template(
            p.home, "router/vyos-preconfig-bootup.script", cluster=c.name
        )
        files[state / r.dir / "scripts" / "vyos-postconfig-bootup.script"] = template(
            p.home,
            "router/vyos-postconfig-bootup.script",
            cluster=c.name,
            floating_ip=c.floating_ip,
        )
        files[unit_dir / f"{r.name}.container"] = template(
            p.home,
            "router.container",
            cluster=c.name,
            router_name=r.name,
            router_dir=r.dir,
            router_ip=r.ip,
            router_image=router.image(v),
            state_dir=state,
            mac_arg=f" --mac-address {mac(r.ip)}" if c.multi_host else "",
            dns=MACVLAN_DNS if c.multi_host else "",
        )
    if me is not None:
        # kubeadm join --control-plane copies init's ClusterConfiguration, audit-policy-file
        # included: every host a control plane may run on needs the policy in its state dir
        policy = state / "logs" / "apiserver" / "audit-policy.yaml"
        files[policy] = (p.home / "manifests" / "audit-policy.yaml").read_text()
    for n in mine_nodes:
        # the files the node mounts over /proc/meminfo, /sys/devices/system/node and the kubelet
        # drop-in directory (nodefs); a cluster from before node sizing gets none and today's unit
        node_mounts = ""
        if sized(c):
            d = nodefs.node_dir(state, n.name)
            files.update(nodefs.files(d, n))
            node_mounts = nodefs.mounts(d)
        files[unit_dir / f"{n.name}.container"] = template(
            p.home,
            "node.container",
            cluster=c.name,
            role=n.role,
            name=n.name,
            ip=n.ip,
            after="" if n is cp or cp not in mine_nodes else f"\nAfter={cp.name}.service",
            image=c.node_image,
            domain=c.domain,
            api_ip=c.endpoint_ip,
            cni_dir=p.cni_dir,
            images_dir=p.images_dir,
            k8s=c.spec.kubernetes,
            state_dir=state,
            wulin_dir=p.wulin_dir,
            cpu=n.cpu,
            memory=n.memory,
            node_mounts=node_mounts,
            mac_arg=f" --mac-address {mac(n.ip)}" if c.multi_host else "",
            dns=MACVLAN_DNS if c.multi_host else "",
        )
    # routes (and the shim) belong to the operator: the one machine that talks to the cluster
    # from its host network; a node host that is not the operator needs none
    operator_here = me is None or me.ssh is None
    wants = [
        *(f"{r.name}.service" for r in mine_routers),
        *(f"{n.name}.service" for n in mine_nodes),
        *([f"{c.name}-routes.service"] if operator_here else []),
    ]
    files[p.systemd_dir / f"{c.name}.target"] = template(
        p.home, "cluster.target", cluster=c.name, wants=" ".join(wants)
    )
    if me is not None and me.ssh is None:
        files[p.systemd_dir / f"{c.name}-routes.service"] = render_routes_macvlan(ctx, c, me)
    elif me is None:
        files[p.systemd_dir / f"{c.name}-routes.service"] = template(
            p.home,
            "routes.service",
            cluster=c.name,
            cp_name=cp.name,
            cp_ip=cp.ip,
            pods=net.pods,
            services=net.services,
            node_routes=node_routes(c, "ExecStart=ip route replace", ""),
            node_routes_stop=node_routes(c, "ExecStop=-ip route del", ""),
            vip_start=""
            if net.vip is None
            else f"\nExecStart=ip route replace {net.vip}/32 via {net.router}",
            vip_stop=""
            if net.vip is None
            else f"\nExecStop=-ip route del {net.vip}/32 via {net.router}",
        )
    return files


def node_routes(c: model.Cluster, verb: str, dev: str) -> str:
    """One route per node that has its pod /24 and is not the first control plane (the /21
    through it covers its own pods): the host reaches a worker's pods straight, not through
    control-plane-1, whose BPF host routing does not forward TCP to another node."""
    first = c.first_control_plane.name
    return "".join(
        f"\n{verb} {n.pod_cidr} via {n.ip}{dev}"
        for n in c.spec.nodes
        if n.pod_cidr is not None and n.name != first
    )


def routes_current(ctx: Context, cluster: model.Cluster | None = None) -> bool:
    """The routes unit on the host that has one holds exactly what cluster.yaml renders."""
    c = cluster or ctx.require_cluster()
    path = ctx.paths.systemd_dir / f"{c.name}-routes.service"
    for hf in per_host(ctx, c):
        if path in hf.files and hf.host.read(path, sudo=needs_sudo(ctx, path)) != hf.files[path]:
            return False
    return True


def write_routes(ctx: Context, cluster: model.Cluster | None = None) -> None:
    """The routes unit rendered again (the pod CIDRs changed) on the host that has one, then
    reloaded and restarted so the routes follow."""
    c = cluster or ctx.require_cluster()
    path = ctx.paths.systemd_dir / f"{c.name}-routes.service"
    for hf in per_host(ctx, c):
        if path not in hf.files:
            continue
        hf.host.write(path, hf.files[path], sudo=needs_sudo(ctx, path))
        hf.host.run(["systemctl", "daemon-reload"], sudo=True)
        hf.host.run(["systemctl", "restart", path.name], sudo=True)


def render_routes_macvlan(ctx: Context, c: model.Cluster, me: model.Host) -> str:
    """The routes unit of a node host: the shim, the routes to the cluster's range through it,
    then the pod, service and VIP routes as on a single host but out of the shim."""
    net, cp, shim = c.spec.network, c.first_control_plane, shim_name(c.name)
    assert net.range is not None and net.shim is not None
    prefixes = range_prefixes(net.range)
    return template(
        ctx.paths.home,
        "routes-macvlan.service",
        cluster=c.name,
        shim=shim,
        iface=me.iface,
        shim_ip=net.shim,
        range_routes="".join(
            f"\nExecStart=ip route replace {px} dev {shim} src {net.shim}" for px in prefixes
        ),
        range_routes_stop="".join(f"\nExecStop=-ip route del {px} dev {shim}" for px in prefixes),
        cp_ip=cp.ip,
        pods=net.pods,
        services=net.services,
        node_routes=node_routes(c, "ExecStart=ip route replace", f" dev {shim}"),
        node_routes_stop=node_routes(c, "ExecStop=-ip route del", f" dev {shim}"),
        vip_start=""
        if net.vip is None
        else f"\nExecStart=ip route replace {net.vip}/32 via {net.router} dev {shim}",
        vip_stop=""
        if net.vip is None
        else f"\nExecStop=-ip route del {net.vip}/32 via {net.router} dev {shim}",
    )


def per_host(ctx: Context, cluster: model.Cluster | None = None) -> list[HostFiles]:
    """Every host's files and links: one entry on a single host; on several, one per node host
    in cluster.yaml order plus the operator when it runs no node."""
    c = cluster or ctx.require_cluster()
    if not c.multi_host:
        return [HostFiles("", ctx.host, render(ctx, c), links(ctx, c))]
    out = [
        HostFiles(name, h, render(ctx, c, name), links(ctx, c, name))
        for name, h in ctx.node_hosts()
    ]
    if all(hf.host is not ctx.host for hf in out):
        out.append(HostFiles("operator", ctx.host, render(ctx, c, "operator"), {}))
    return out


def sized(c: model.Cluster) -> bool:
    """Created by a tkctl that sizes nodes: status.node_capacity. Older clusters keep the units
    they were created with."""
    return c.status is not None and c.status.node_capacity


def _nodes_on(c: model.Cluster, host_name: str | None) -> list[model.Node]:
    return [n for n in c.spec.nodes if host_name is None or n.host == host_name]


def links(
    ctx: Context, cluster: model.Cluster | None = None, host_name: str | None = None
) -> dict[Path, Path]:
    """The cpu symlinks of every node's fake NUMA node, path -> target (none for an old
    cluster); on several hosts those of one host's nodes."""
    c = cluster or ctx.require_cluster()
    if not sized(c):
        return {}
    state = ctx.paths.state_dir(c.name)
    out: dict[Path, Path] = {}
    for n in _nodes_on(c, host_name):
        out.update(nodefs.links(nodefs.node_dir(state, n.name), n))
    return out


def _stale_links_on(
    ctx: Context, c: model.Cluster, host: Host, host_name: str | None
) -> list[Path]:
    wanted = set(links(ctx, c, host_name))
    state = ctx.paths.state_dir(c.name)
    out: list[Path] = []
    for n in _nodes_on(c, host_name):
        n0 = nodefs.node_dir(state, n.name) / "sys-node" / "node0"
        out += [
            n0 / e
            for e in host.listdir(n0)
            if re.fullmatch(r"cpu[0-9]+", e) and n0 / e not in wanted
        ]
    return out


def stale_links(ctx: Context, cluster: model.Cluster | None = None) -> list[Path]:
    """cpuN entries in a node's fake NUMA node that the cluster no longer wants (a resume with a
    smaller cpu): cAdvisor would count them. Checked on the host each node runs on."""
    c = cluster or ctx.require_cluster()
    if not sized(c):
        return []
    if not c.multi_host:
        return _stale_links_on(ctx, c, ctx.host, None)
    return [p for name, h in ctx.node_hosts() for p in _stale_links_on(ctx, c, h, name)]


def missing_node_files(ctx: Context, cluster: model.Cluster | None = None) -> list[Path]:
    """Node files podman will bind-mount that are not on the host (someone removed nodes/):
    the node container would refuse to start. Checked on the host each node runs on."""
    c = cluster or ctx.require_cluster()
    if not sized(c):
        return []
    state = ctx.paths.state_dir(c.name)
    missing: list[Path] = []
    for n in c.spec.nodes:
        host = ctx.host_for(n.name)
        d = nodefs.node_dir(state, n.name)
        missing += [p for p in nodefs.files(d, n) if host.read(p) is None]
        missing += [p for p in nodefs.links(d, n) if host.readlink(p) is None]
    return missing


def needs_sudo(ctx: Context, path: Path) -> bool:
    """Units live under /etc; the router directories are written by VyOS (root in the container)."""
    state = ctx.paths.state_dir(ctx.cluster_name)
    router_dirs = [state / r.dir for r in ctx.require_cluster().routers]
    return not path.is_relative_to(ctx.paths.data_dir) or any(
        path.is_relative_to(d) for d in router_dirs
    )


def done(ctx: Context, cluster: model.Cluster | None = None) -> bool:
    """Every file is on its host with exactly this content (a changed cluster.yaml re-renders)."""
    for hf in per_host(ctx, cluster):
        if any(
            hf.host.read(path, sudo=needs_sudo(ctx, path)) != content
            for path, content in hf.files.items()
        ):
            return False
        if any(hf.host.readlink(path) != target for path, target in hf.links.items()):
            return False
    return not stale_links(ctx, cluster)


def run(ctx: Context, cluster: model.Cluster | None = None) -> None:
    for hf in per_host(ctx, cluster):
        for path, content in hf.files.items():
            mode = 0o755 if path.suffix == ".script" else 0o644
            hf.host.write(path, content, sudo=needs_sudo(ctx, path), mode=mode)
        for path, target in hf.links.items():
            hf.host.symlink(path, target)
    for name, h in ctx.node_hosts():
        c = cluster or ctx.require_cluster()
        for path in _stale_links_on(ctx, c, h, name if c.multi_host else None):
            h.remove(path)


PHASE = Phase(
    "units", "write the Quadlet units, the target, the routes and the router config", done, run
)
