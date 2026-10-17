"""Day-2 verbs on an existing cluster: get clusters, describe, stop, start, use. Plain functions in
the order a person meets them; nothing here has steps to resume, so there are no phases."""

from datetime import UTC, datetime
from pathlib import Path

from . import model
from .create import PHASES, router
from .create.kubeadm import kubectl
from .phases import Context, poll_until


class ClusterError(Exception):
    """A verb that cannot proceed; the message is the whole story."""


# ---- the cluster a verb works on


def load_cluster(ctx: Context, name: str) -> model.Cluster:
    """cluster.yaml of an existing cluster."""
    path = ctx.paths.state_dir(name) / "cluster.yaml"
    if not path.exists():
        raise ClusterError(
            f"cluster {name} not found under {ctx.paths.clusters_dir} (tkctl get clusters)"
        )
    ctx.name = name
    ctx.cluster = model.load(path)
    return ctx.cluster


def save_cluster(ctx: Context, cluster: model.Cluster) -> None:
    ctx.cluster = cluster
    ctx.host.write(ctx.state_dir / "cluster.yaml", model.dumps(cluster))


def state_of(ctx: Context, c: model.Cluster) -> str:
    if c.status is None or c.status.phase != "Ready":
        return "Creating"
    return "Running" if ctx.host.is_active(f"{c.name}.target") else "Stopped"


# ---- get clusters


def list_clusters(ctx: Context) -> None:
    dirs = sorted(d for d in ctx.paths.clusters_dir.glob("*") if d.is_dir())
    if not dirs:
        ctx.say("no clusters")
        return
    ctx.say(f"{'NAME':<12} {'NODES':<7} {'K8S':<8} {'DATAPATH':<9} {'STATE':<9} SUBNET")
    for d in dirs:
        name = d.name
        path = d / "cluster.yaml"
        if not path.exists():
            # a create interrupted before plan: the directory is there, nothing else is, and a
            # second create of the name would fail on a clash the student cannot see
            ctx.say(
                f"{name:<12} {'-':<7} {'-':<8} {'-':<9} {'Incomplete':<9} "
                f"no cluster.yaml (tkctl delete cluster {name})"
            )
            continue
        try:
            c = model.load(path)
        except model.ClusterFileError as err:
            ctx.say(f"{name:<12} {err}")  # one bad file must not hide the other clusters
            continue
        s = c.spec
        nodes = f"{len(c.control_planes)}+{len(c.workers)}"
        datapath = s.datapath_resolved or s.datapath
        ctx.say(
            f"{name:<12} {nodes:<7} {s.kubernetes:<8} {datapath:<9} {state_of(ctx, c):<9} "
            f"{s.network.nodes}"
        )


# ---- describe cluster


def describe(ctx: Context, c: model.Cluster, output: str | None) -> None:
    if output == "yaml":
        ctx.say(model.dumps(c).rstrip("\n"))
        return
    s, net = c.spec, c.spec.network
    state = state_of(ctx, c)
    ctx.say(f"Name:        {c.name}")
    ctx.say(f"State:       {state}")
    ctx.say(
        f"Kubernetes:  {s.kubernetes}   cni: {s.cni}   "
        f"datapath: {s.datapath_resolved or s.datapath}   gvisor: {str(s.gvisor).lower()}"
    )
    vip = f"  vip {net.vip}" if net.vip is not None else ""
    ctx.say(
        f"Network:     nodes {net.nodes} (gateway {net.gateway})  pods {net.pods}  "
        f"services {net.services}  lb-ipam {net.lb_range}  router {net.router}{vip}"
    )
    ctx.say("Nodes:")
    ctx.say(f"  {'NAME':<24} {'ROLE':<14} {'IP':<14} {'RESOURCES':<14} JOIN")
    for n in s.nodes:
        size = f"{n.cpu} cpu, {n.memory}"
        join = "joined" if n.join else "not joined"
        ctx.say(f"  {n.name:<24} {n.role:<14} {n.ip!s:<14} {size:<14} {join}")
    describe_status(ctx, c)
    describe_addons(ctx, c, state)
    describe_router(ctx, c, state)
    ctx.say("Access:")
    if net.ha == "proxy":
        ctx.say(
            f"  apiserver:  https://{net.router}:6443 (VyOS haproxy in {c.name}-router; every "
            "node and this host go through it)"
        )
    else:
        ctx.say(
            f"  apiserver:  https://{net.vip}:6443 (anycast VIP; {c.name}-router spreads it over "
            "the control planes announcing it)"
        )
    ctx.say(
        f"  from this host:       LoadBalancer IPs ({net.lb_range}) and ClusterIPs "
        f"({net.services}) are routed"
    )
    via = host_ip(ctx)
    routes = f"sudo ip route add {net.nodes} via {via}"
    if net.vip is not None:  # the kubeconfig points at the VIP: that route too
        routes += f" && sudo ip route add {net.vip}/32 via {via}"
    ctx.say(f"  from another machine: {routes}   (and {net.services} for ClusterIPs)")
    ctx.say("Paths:")
    ctx.say(f"  state:      {ctx.state_dir}/")
    ctx.say(f"  kubeconfig: {ctx.state_dir}/kubeconfig")
    ctx.say(
        f"  units:      {ctx.paths.unit_dir}/{c.name}/   "
        f"target: {ctx.paths.systemd_dir}/{c.name}.target"
    )


def describe_router(ctx: Context, c: model.Cluster, state: str) -> None:
    net = c.spec.network
    ctx.say("Router:")
    if net.ha == "proxy":
        ctx.say("  mode:     proxy (VyOS load-balancing haproxy, TCP check on every control plane)")
        ctx.say(f"  address:  {net.router} (api.{c.domain} resolves to it on every node)")
    else:
        ctx.say(
            "  mode:     bgp (VyOS BGP AS 65000, ECMP over every control plane announcing the VIP)"
        )
        ctx.say(f"  address:  {net.router} (api.{c.domain} -> {net.vip}, the VIP)")
    ctx.say(f"  shell:    sudo podman exec -it {c.name}-router vbash")
    label = f"{router.kind(c)}s:"
    if state != "Running":
        ctx.say(f"  {label:<10}unavailable (cluster is not running)")
    else:
        rows = router.members(ctx, c)
        ctx.say(f"  {label:<10}" + (", ".join(f"{n} {s}" for n, s in rows) or "none listed"))
        if net.ha == "bgp":
            ctx.say(f"  nexthops: {router.nexthops(ctx, c)} to {net.vip} (show ip route {net.vip})")
    if net.ha == "bgp" and len(c.control_planes) == 1:
        ctx.say("  note:     one control plane: nothing to spread; --control-planes 3 shows ECMP")


def describe_status(ctx: Context, c: model.Cluster) -> None:
    """status.phase and the phases create recorded: an interrupted create shows where it stopped
    and what runs next."""
    ctx.say("Status:")
    st = c.status
    if st is None:
        ctx.say("  phase:      Creating   (nothing recorded yet)")
        return
    ctx.say(f"  phase:      {st.phase}   (created by tk8s {st.tk8s})")
    for name, when in st.completed.items():
        ctx.say(f"  {name:<22} {when.strftime('%Y-%m-%d %H:%M:%S')}Z")
    if st.phase != "Ready":
        # The phase after the last recorded one: preflight (before cluster.yaml exists) and
        # phases skipped as already done are never recorded.
        order = [full for p in PHASES for full, _ in p.leaves()]
        done = [i for i, full in enumerate(order) if full in st.completed]
        following = order[max(done) + 1 :] if done else order
        if following:
            ctx.say(f"  next:       {following[0]}   (rerun tkctl create cluster {c.name})")


def describe_addons(ctx: Context, c: model.Cluster, state: str) -> None:
    ctx.say("Addons:")
    if state != "Running":
        ctx.say("  unavailable (cluster is not running)")
        return

    def ask(*args: str) -> str:
        r = kubectl(ctx, "--request-timeout=5s", *args)
        return r.stdout.strip() if r.returncode == 0 else ""

    image = "jsonpath={.spec.template.spec.containers[0].image}"
    ms = ask("-n", "kube-system", "get", "deploy", "metrics-server", "-o", image)
    ctx.say(f"  metrics-server  {ms.rsplit(':', 1)[-1] or 'not installed'}")
    lp = ask("-n", "local-path-storage", "get", "deploy", "local-path-provisioner", "-o", image)
    ctx.say(f"  local-path      {lp.rsplit(':', 1)[-1] or 'not installed'}")
    rc = ask("get", "runtimeclass", "-o", 'jsonpath={range .items[*]}{.metadata.name}{" "}{end}')
    ctx.say(f"  runtimeclass    {rc or 'none'}")


def host_ip(ctx: Context) -> str:
    """The address another machine routes through, from the default route's source."""
    r = ctx.host.run(["ip", "-4", "route", "get", "1.1.1.1"], capture=True, check=False)
    words = r.stdout.split()
    return words[words.index("src") + 1] if "src" in words else "<host-ip>"


# ---- stop / start cluster


def stop(ctx: Context, c: model.Cluster) -> None:
    """systemctl waits only for the units it was given; the nodes and the LB are PartOf= the
    target and would still be stopping when a bare `stop <cluster>.target` returns: name them
    all."""
    units = [
        f"{c.name}.target",
        f"{c.name}-router.service",
        *(f"{n.name}.service" for n in c.spec.nodes),
    ]
    # A node recorded in cluster.yaml but never given a unit (an add that died early) makes
    # systemctl exit non-zero after stopping everything else; that is not a failed stop.
    r = ctx.host.run(["systemctl", "stop", *units], sudo=True, capture=True, check=False)
    if r.returncode != 0 and r.stderr.strip():
        ctx.say(f"  {r.stderr.strip().splitlines()[-1]}")
    ctx.say(f"cluster {c.name} stopped")


READY_HEARTBEATS = (
    '{range .items[*]}{.metadata.name} {range .status.conditions[?(@.type=="Ready")]}'
    '{.status} {.lastHeartbeatTime}{end}{"\\n"}{end}'
)


def start(ctx: Context, c: model.Cluster) -> None:
    """Node objects keep their last status for a while after a stop: a node is back only with a
    Ready heartbeat newer than this start."""
    since = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    ctx.host.run(["systemctl", "start", f"{c.name}.target"], sudo=True)

    def all_fresh() -> bool:
        r = kubectl(ctx, "get", "nodes", "-o", f"jsonpath={READY_HEARTBEATS}")
        rows = [line.split() for line in r.stdout.splitlines() if line.strip()]
        fresh = [row for row in rows if len(row) == 3 and row[1] == "True" and row[2] > since]
        return bool(rows) and len(fresh) == len(rows)

    poll_until(all_fresh, rounds=90, delay=ctx.options.poll_slow)
    r = kubectl(ctx, "get", "nodes", "--no-headers")
    for line in r.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            ctx.say(f"  {fields[0]} {fields[1]}")


# ---- use cluster


def use(ctx: Context, c: model.Cluster) -> None:
    """~/.kube/config points at the cluster; the previous file is kept as ~/.kube/config.bak."""
    text = ctx.host.read(ctx.state_dir / "kubeconfig")
    if text is None:
        raise ClusterError(
            f"cluster {c.name} has no kubeconfig yet (tkctl describe cluster {c.name})"
        )
    kube = Path.home() / ".kube"
    previous = ctx.host.read(kube / "config")
    if previous is not None:
        ctx.host.write(kube / "config.bak", previous, mode=0o600)
    ctx.host.write(kube / "config", text, mode=0o600)
    ctx.say(f"~/.kube/config now points at {c.name}")
