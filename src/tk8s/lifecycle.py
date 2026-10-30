"""Day-2 verbs on an existing cluster: get clusters, describe, stop, start, use. Plain functions in
the order a person meets them; nothing here has steps to resume, so there are no phases."""

from datetime import UTC, datetime
from pathlib import Path

from . import hosts, kubeconfig, model
from .create import PHASES, units
from .create.kubeadm import kubectl
from .describe_mesh import describe_mesh
from .describe_router import describe_router
from .host import Host, HostError
from .phases import Context, poll_until


class ClusterError(Exception):
    """A verb that cannot proceed; the message is the whole story."""


class ClusterNotFound(ClusterError):
    """No cluster.yaml of that name here (delete falls back to what the host still has)."""


# ---- the cluster a verb works on


def load_cluster(ctx: Context, name: str) -> model.Cluster:
    """cluster.yaml of an existing cluster. A multi-host cluster brings its hosts along, and is
    only the operator's to touch: a copy on another host names where to run tkctl."""
    path = ctx.paths.state_dir(name) / "cluster.yaml"
    if not path.exists():
        raise ClusterNotFound(
            f"cluster {name} not found under {ctx.paths.clusters_dir} (tkctl get clusters)"
        )
    ctx.name = name
    c = model.load(path)
    if c.multi_host:
        op = managed_from(ctx, c)
        if op is not None:
            raise ClusterError(f"cluster {name} is managed from {op}; run tkctl there")
        if not ctx.hosts:
            ctx.hosts = hosts.build(
                ctx.host, c.spec.hosts or [], say=ctx.say, quiet=ctx.host.quiet, dry_run=False
            )
    ctx.cluster = c
    return c


def managed_from(ctx: Context, c: model.Cluster) -> str | None:
    """The operator's hostname when this is a copy on another host, else None."""
    op = c.status.operator if c.status is not None else None
    if op is None or op == hosts.local_hostname(ctx.host):
        return None
    return op


def save_cluster(ctx: Context, cluster: model.Cluster) -> None:
    ctx.save_cluster(cluster)


def state_of(ctx: Context, c: model.Cluster) -> str:
    """Running, Stopped, or (several hosts) Degraded when only some hosts run their target."""
    if c.status is None or c.status.phase != "Ready":
        return "Creating"
    if not c.multi_host:
        return "Running" if ctx.host.is_active(f"{c.name}.target") else "Stopped"
    up = [target_up(h, c.name) for _, h in ctx.node_hosts()]
    if all(up):
        return "Running"
    return "Degraded" if any(up) else "Stopped"


def target_up(host: Host, cluster: str) -> bool | None:
    """Whether the cluster's target runs on the host; None when the host does not answer (an
    outage is something to describe, not to die in)."""
    try:
        return host.is_active(f"{cluster}.target")
    except HostError:
        return None


# ---- get clusters


def list_clusters(ctx: Context) -> None:
    dirs = sorted(d for d in ctx.paths.clusters_dir.glob("*") if d.is_dir())
    if not dirs:
        ctx.say("no clusters")
        return
    rows: list[tuple[str, ...] | str] = []  # a tuple is a row; a str is a line of its own
    for d in dirs:
        name = d.name
        path = d / "cluster.yaml"
        if not path.exists():
            # a create interrupted before plan: the directory is there, nothing else is, and a
            # second create of the name would fail on a clash the student cannot see
            why = f"no cluster.yaml (tkctl delete cluster {name})"
            rows.append((name, "-", "-", "-", "Incomplete", "-", "-", why))
            continue
        try:
            c = model.load(path)
        except model.ClusterFileError as err:
            rows.append(f"{name:<12} {err}")  # one bad file must not hide the other clusters
            continue
        s = c.spec
        nodes = f"{len(c.control_planes)}+{len(c.workers)}"
        datapath = s.datapath_resolved or s.datapath
        count = str(len(c.host_names)) if c.multi_host else "-"
        peers = ", ".join(c.status.mesh) if c.status is not None and c.status.mesh else "-"
        op = managed_from(ctx, c) if c.multi_host else None
        if op is not None:  # a copy: the operator's hosts are not ours to ask
            state, tail = "managed", f" from {op}"
        else:
            row = ctx.on(ctx.host)  # each row judged with its own cluster and hosts
            row.cluster = c
            row.hosts = (
                hosts.build(ctx.host, c.spec.hosts or [], say=ctx.say, quiet=True, dry_run=False)
                if c.multi_host
                else {}
            )
            state, tail = state_of(row, c), ""
        rows.append(
            (name, nodes, s.kubernetes, datapath, state, count, peers, f"{s.network.nodes}{tail}")
        )
    # the MESH column is as wide as its longest entry (several peers, long names)
    width = max([4, *(len(r[6]) for r in rows if isinstance(r, tuple))])
    ctx.say(
        f"{'NAME':<12} {'NODES':<7} {'K8S':<8} {'DATAPATH':<9} {'STATE':<9} {'HOSTS':<6} "
        f"{'MESH':<{width}} SUBNET"
    )
    for r in rows:
        if isinstance(r, str):
            ctx.say(r)
            continue
        name, nodes, k8s, dp, state, count, peers, last = r
        ctx.say(
            f"{name:<12} {nodes:<7} {k8s:<8} {dp:<9} {state:<9} {count:<6} {peers:<{width}} {last}"
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
    if c.multi_host:
        shim = f"  shim {net.shim}" if net.shim is not None else ""
        ctx.say(f"             range {net.range}{shim}")
    if c.multi_host:
        describe_hosts(ctx, c)
    ctx.say("Nodes:")
    host_col = f" {'JOIN':<11} HOST" if c.multi_host else " JOIN"
    ctx.say(f"  {'NAME':<24} {'ROLE':<14} {'IP':<14} {'RESOURCES':<14}{host_col}")
    for n in s.nodes:
        size = f"{n.cpu} cpu, {n.memory}"
        join = "joined" if n.join else "not joined"
        tail = f" {join:<11} {n.host}" if c.multi_host else f" {join}"
        ctx.say(f"  {n.name:<24} {n.role:<14} {n.ip!s:<14} {size:<14}{tail}")
    describe_status(ctx, c)
    describe_addons(ctx, c, state)
    describe_router(ctx, c, state)
    describe_mesh(ctx, c, state)
    ctx.say("Access:")
    routers = " and ".join(r.name for r in c.routers)
    spread = "spreads" if len(c.routers) == 1 else "spread"
    if net.ha == "proxy":
        ctx.say(
            f"  apiserver:  https://{net.router}:6443 (VyOS haproxy in {routers}; every "
            "node and this host go through it)"
        )
    else:
        ctx.say(
            f"  apiserver:  https://{net.vip}:6443 (anycast VIP; {routers} {spread} it over "
            "the control planes announcing it)"
        )
    if c.multi_host:
        ctx.say(
            f"  nodes and LoadBalancer IPs ({net.lb_range}) are on the LAN; ClusterIPs "
            f"({net.services}) are routed on every host (through the tk8s-* shim)"
        )
    else:
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


def describe_hosts(ctx: Context, c: model.Cluster) -> None:
    ctx.say("Hosts:")
    ctx.say(f"  {'NAME':<8} {'SSH':<21} {'IFACE':<8} TARGET")
    for h in c.spec.hosts or []:
        up = target_up(ctx.hosts[h.name], c.name) if h.name in ctx.hosts else False
        ssh = h.ssh or "(this host)"
        target = "unreachable" if up is None else ("active" if up else "inactive")
        ctx.say(f"  {h.name:<8} {ssh:<21} {h.iface:<8} {target}")


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
    all (on every host, each its own)."""
    done, away = [], []
    for name, h in ctx.target_hosts():
        units_ = [
            f"{c.name}.target",
            *(f"{r.name}.service" for r in c.routers if not name or r.host == name),
            *(f"{n.name}.service" for n in c.spec.nodes if not name or n.host == name),
        ]
        # A node recorded in cluster.yaml but never given a unit (an add that died early) makes
        # systemctl exit non-zero after stopping everything else; that is not a failed stop.
        try:
            r = h.run(["systemctl", "stop", *units_], sudo=True, capture=True, check=False)
        except HostError:  # the host is away: stop the others, then say so
            away.append(name)
            continue
        done.append(name)
        if r.returncode != 0 and r.stderr.strip():
            ctx.say(f"  {r.stderr.strip().splitlines()[-1]}")
    if away:
        raise ClusterError(
            f"host {', '.join(away)} unreachable; stopped {', '.join(done)}; stop the rest "
            "when it is back (tkctl stop cluster again)"
        )
    ctx.say(f"cluster {c.name} stopped")


READY_HEARTBEATS = (
    '{range .items[*]}{.metadata.name} {range .status.conditions[?(@.type=="Ready")]}'
    '{.status} {.lastHeartbeatTime}{end}{"\\n"}{end}'
)


def start(ctx: Context, c: model.Cluster) -> None:
    """Node objects keep their last status for a while after a stop: a node is back only with a
    Ready heartbeat newer than this start."""
    missing = units.missing_node_files(ctx, c)
    if missing:
        raise ClusterError(
            f"cluster {c.name}: files its nodes mount are missing from the host "
            f"({', '.join(str(p) for p in missing[:3])}"
            f"{', ...' if len(missing) > 3 else ''}); "
            f"tkctl create cluster {c.name} writes them again"
        )
    since = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    done, away = [], []
    for name, h in ctx.target_hosts():
        try:
            h.run(["systemctl", "start", f"{c.name}.target"], sudo=True)
        except HostError as err:
            if err.returncode != 255:
                raise
            away.append(name)
            continue
        done.append(name)
    if away:
        raise ClusterError(
            f"host {', '.join(away)} unreachable; started {', '.join(done)}; run tkctl start "
            "cluster again when it is back"
        )

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
    """The cluster joins ~/.kube/config (other clusters stay) and its context becomes current;
    the previous file is kept as ~/.kube/config.bak."""
    text = ctx.host.read(ctx.state_dir / "kubeconfig")
    if text is None:
        raise ClusterError(
            f"cluster {c.name} has no kubeconfig yet (tkctl describe cluster {c.name})"
        )
    kube = Path.home() / ".kube"
    previous = ctx.host.read(kube / "config")
    try:
        # a state file from before the renaming still carries kubeadm's names; rename is idempotent
        text = kubeconfig.rename(text, c.name, f"https://{c.endpoint_ip}:6443")
        merged = kubeconfig.merge(previous, text, c.name)
    except ValueError as err:
        raise ClusterError(f"{kube / 'config'}: {err}") from err
    if previous is not None:
        ctx.host.write(kube / "config.bak", previous, mode=0o600)
    ctx.host.write(kube / "config", merged, mode=0o600)
    contexts = ", ".join(x["name"] for x in kubeconfig.load(merged)["contexts"])
    ctx.say(f"~/.kube/config: current context {c.name} (contexts: {contexts})")
