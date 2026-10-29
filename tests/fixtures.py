"""A fake host on which every create phase is already done, for whole-command tests."""

import json
from collections.abc import Callable
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

from tk8s import kubeconfig, model, versions
from tk8s.create import kubeadm, units
from tk8s.create.plan import resolve
from tk8s.host import FakeHost, Result
from tk8s.hosts import LanFacts
from tk8s.paths import Paths
from tk8s.phases import Context, Options

HOME = Path(__file__).resolve().parents[1]

# kubeadm's admin.conf, the shape tkctl renames into the host's kubeconfig
ADMIN_CONF = (
    "apiVersion: v1\nkind: Config\nclusters:\n- cluster:\n    server: https://api.demo.k8s:6443\n"
    "  name: kubernetes\ncontexts:\n- context:\n    cluster: kubernetes\n"
    "    user: kubernetes-admin\n"
    "  name: kubernetes-admin@kubernetes\ncurrent-context: kubernetes-admin@kubernetes\n"
    "users:\n- name: kubernetes-admin\n  user:\n    client-key-data: S0VZ\n"
)

DEMO_USER = model.UserCluster(
    metadata=model.Metadata(name="demo"),
    spec=model.UserSpec(
        nodes=[model.NodeGroup(role="control-plane"), model.NodeGroup(role="worker", count=2)]
    ),
)


def bgp_user() -> model.UserCluster:
    """demo in bgp mode: 3 control planes (.1 .2 .3) + 1 worker (.4), VIP 172.23.0.1."""
    return model.UserCluster(
        metadata=model.Metadata(name="demo"),
        spec=model.UserSpec(
            network=model.UserNetwork(ha="bgp"),
            nodes=[model.NodeGroup(role="control-plane", count=3), model.NodeGroup(role="worker")],
        ),
    )


def two_router_user() -> model.UserCluster:
    """demo in bgp mode with two routers: 3 control planes (.1 .2 .3) + 1 worker (.4), routers
    .251/.252 behind the floating .253, VIP 172.23.0.1."""
    return model.UserCluster(
        metadata=model.Metadata(name="demo"),
        spec=model.UserSpec(
            network=model.UserNetwork(ha="bgp", routers=2),
            nodes=[model.NodeGroup(role="control-plane", count=3), model.NodeGroup(role="worker")],
        ),
    )


def ha_user() -> model.UserCluster:
    """demo with 3 control planes (the third deferred) and one worker."""
    return model.UserCluster(
        metadata=model.Metadata(name="demo"),
        spec=model.UserSpec(
            nodes=[
                model.NodeGroup(role="control-plane"),
                model.NodeGroup(role="control-plane"),
                model.NodeGroup(role="control-plane", join=False),
                model.NodeGroup(role="worker"),
            ]
        ),
    )


# demo spread over three hosts (a is the operator, b and c over ssh), bgp mode with two routers,
# resolved from the range 192.168.1.64-192.168.1.95 on the hosts' LAN 192.168.1.0/24 via ens18:
# router .64 floating, router-1 .65 (a), router-2 .66 (b), shims .67/.68/.69, control planes
# .70/.71/.72 on a/b/c, workers .73/.74 on a/b, LoadBalancer pool .76-.95, VIP 172.23.0.1.
MULTI_YAML = """\
apiVersion: taroko.io/v1alpha1
kind: Cluster
metadata:
  name: demo
spec:
  kubernetes: "1.37.1"
  cni: cilium
  datapath: auto
  gvisor: false
  network:
    index: 0
    nodes: 192.168.1.0/24
    gateway: 192.168.1.1
    router: 192.168.1.64
    ha: bgp
    routers: 2
    vip: 172.23.0.1
    lb_range: 192.168.1.76-192.168.1.95
    pods: 10.244.0.0/21
    services: 10.98.0.0/24
    range: 192.168.1.64-192.168.1.95
    router_hosts:
      - a
      - b
  nodes:
    - role: control-plane
      name: demo-control-plane-1
      ip: 192.168.1.70
      cpu: 2
      memory: 4g
      join: true
      host: a
    - role: control-plane
      name: demo-control-plane-2
      ip: 192.168.1.71
      cpu: 2
      memory: 4g
      join: true
      host: b
    - role: control-plane
      name: demo-control-plane-3
      ip: 192.168.1.72
      cpu: 2
      memory: 4g
      join: true
      host: c
    - role: worker
      name: demo-worker-1
      ip: 192.168.1.73
      cpu: 2
      memory: 4g
      join: true
      host: a
    - role: worker
      name: demo-worker-2
      ip: 192.168.1.74
      cpu: 2
      memory: 4g
      join: true
      host: b
  hosts:
    - name: a
      iface: ens18
      shim: 192.168.1.67
    - name: b
      ssh: tux@192.168.1.231
      iface: ens18
      shim: 192.168.1.68
    - name: c
      ssh: 192.168.1.232
      iface: ens18
      shim: 192.168.1.69
status:
  phase: Creating
  completed: {}
  tk8s: test
  node_capacity: true
  operator: op
"""


MULTI_LAN = LanFacts("ens18", IPv4Network("192.168.1.0/24"), IPv4Address("192.168.1.1"))


def multi_user() -> model.UserCluster:
    """demo over three hosts: what MULTI_YAML is resolved from."""
    return model.UserCluster(
        metadata=model.Metadata(name="demo"),
        spec=model.UserSpec(
            hosts=[
                model.UserHost(name="a"),
                model.UserHost(name="b", ssh="tux@192.168.1.231"),
                model.UserHost(name="c", ssh="192.168.1.232"),
            ],
            network=model.UserNetwork(ha="bgp", routers=2, range="192.168.1.64-192.168.1.95"),
            nodes=[
                model.NodeGroup(role="control-plane", count=3),
                model.NodeGroup(role="worker", count=2),
            ],
        ),
    )


def make_multi_ctx(host: FakeHost, data_dir: Path) -> tuple[Context, dict[str, FakeHost]]:
    """The MULTI_YAML cluster on three fake hosts: a is the operator (`host`), b and c are
    labelled like ssh hosts."""
    ctx = make_ctx(host, data_dir, user=None)
    ctx.cluster = model.load_text(MULTI_YAML)
    hosts = {
        "a": host,
        "b": FakeHost(label="tux@192.168.1.231"),
        "c": FakeHost(label="192.168.1.232"),
    }
    ctx.hosts = dict(hosts)
    return ctx, hosts


def resolved(
    user: model.UserCluster,
    *,
    existing: model.Cluster | None = None,
    used_indices: set[int] | None = None,
    allow_overlap: bool = False,
    lan: LanFacts | None = None,
    operator: str | None = None,
) -> model.Cluster:
    return resolve(
        user,
        k8s_default=versions.load(HOME / "versions.yaml").kubernetes.default,
        existing=existing,
        used_indices=used_indices or set(),
        allow_overlap=allow_overlap,
        tk8s_version="test",
        lan=lan,
        operator=operator,
    )


def make_ctx(
    host: FakeHost,
    data_dir: Path,
    *,
    user: model.UserCluster | None = DEMO_USER,
    say: Callable[[str], None] | None = None,
    **opts: object,
) -> Context:
    """A Context on the fake host with no waiting; `user` None leaves ctx.cluster unset."""
    options = Options(retry_delay=0, poll=0, poll_slow=0)
    for k, v in opts.items():
        setattr(options, k, v)
    ctx = Context(
        host=host,
        paths=Paths(data_dir=data_dir, wulin_dir=data_dir / "wulin", home=HOME),
        versions=versions.load(HOME / "versions.yaml"),
        options=options,
        say=say or host.lines.append,
    )
    if user is not None:
        ctx.cluster = resolved(user)
    return ctx


def argvs(host: FakeHost) -> list[list[str]]:
    return [c.argv for c in host.calls]


def phases_of(host: FakeHost) -> list[str]:
    return [line.split("]")[0][1:] for line in host.lines if line.startswith("[")]


def new_node_answers(host: FakeHost, name: str, *, ready_after: int) -> None:
    """The new node boots, is not a Node object until kubeadm join ran, then turns Ready.
    Specific prefixes go first: FakeHost takes the first matching prefix."""
    host.responses = {
        ("kubectl", "get", "node", name, "-o"): [
            *([Result(0, "False", "")] * ready_after),
            Result(0, "True", ""),
        ],
        ("kubectl", "get", "node", name): [Result(1, "", "not found"), Result(0, name, "")],
        ("podman", "exec", name, "systemctl", "is-system-running"): Result(0, "running\n", ""),
        **host.responses,
    }


def running_cluster_answers(host: FakeHost, ctx: Context) -> None:
    """demo's target is up, every node's systemd is running, the router booted and lists the
    control plane, the CNI plugins are installed: what nodes/up and nodes/cni-plugins find done."""
    host.responses[("id", "-u")] = Result(0, "1000\n", "")
    host.responses[("id", "-g")] = Result(0, "1000\n", "")
    host.responses[("podman", "exec", "demo-router", "cat", "/tmp/vyos-config-status")] = Result(
        0, "0\n", ""
    )
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(
        0, SHOW_HAPROXY, ""
    )
    for n in ("demo-control-plane-1", "demo-worker-1", "demo-worker-2"):
        host.responses[("podman", "exec", n, "systemctl", "is-system-running")] = Result(
            0, "running\n", ""
        )
    host.active.add("demo.target")
    host.files[ctx.paths.cni_dir / "bridge"] = "elf"


SHOW_HAPROXY = (
    "Proxy name            Role       Status    Req rate    Resp time    Last change\n"
    "--------------------  ---------  --------  ----------  -----------  -------------\n"
    "api                   FRONTEND   OPEN      0\n"
    "api                   BACKEND    UP                    0 ms         4s\n"
    "api                   demo-control-plane-1  UP         0 ms         4s\n"
)
OP = "/opt/vyatta/bin/vyatta-op-cmd-wrapper"


def vtysh_summary(router: str = "demo-router") -> tuple[str, ...]:
    return ("podman", "exec", router, "vtysh", "-c", "show bgp ipv4 unicast summary json")


def vtysh_route(router: str = "demo-router") -> tuple[str, ...]:
    return ("podman", "exec", router, "vtysh", "-c", "show ip route 172.23.0.1/32 json")


def vtysh_bfd(router: str = "demo-router") -> tuple[str, ...]:
    return ("podman", "exec", router, "vtysh", "-c", "show bfd peers json")


VTYSH_SUMMARY = vtysh_summary()
VTYSH_ROUTE = vtysh_route()
VTYSH_BFD = vtysh_bfd()

SHOW_VRRP = (
    "Name    Interface      VRID  State      Priority  Last Transition\n"
    "------  -----------  ------  -------  ----------  -----------------\n"
    "tk8s    eth0             10  {state}     {priority}  9s\n"
)
NO_VRRP = "VRRP data is not available (process not running or no active groups)\n"


def vrrp_answers(host: FakeHost, states: dict[str, str | None]) -> None:
    """What `show vrrp` prints on each router: the State column of group tk8s, or VyOS's line for
    a router with no VRRP running (None)."""
    for name, state in states.items():
        text = NO_VRRP if state is None else SHOW_VRRP.format(state=f"{state:<7}", priority=200)
        host.responses[("podman", "exec", name, OP, "show", "vrrp")] = Result(0, text, "")


def bgp_router_answers(
    host: FakeHost,
    *,
    router: str = "demo-router",
    established: int = 3,
    hops: int = 3,
    bfd_up: int = 3,
) -> None:
    """The demo bgp router (3 control planes .1 .2 .3) as vtysh reports it: the first `established`
    peers Established and the rest Active, `hops` ECMP next hops to the VIP, the first `bfd_up` BFD
    sessions up. The JSON shapes are FRR 10's, trimmed to the keys tkctl reads. `router` picks the
    container (demo-router-1/-2 for a two-router cluster)."""
    ips = ["172.22.0.1", "172.22.0.2", "172.22.0.3"]
    summary = {
        "as": 65000,
        "peers": {
            ip: {
                "remoteAs": 65001,
                "state": "Established" if i < established else "Active",
                "pfxRcd": 1 if i < established else 0,
            }
            for i, ip in enumerate(ips)
        },
    }
    route: dict[str, object] = {}
    if hops:
        route = {
            "172.23.0.1/32": [
                {
                    "prefix": "172.23.0.1/32",
                    "protocol": "bgp",
                    "selected": True,
                    "nexthops": [{"ip": ip, "active": True} for ip in ips[:hops]],
                }
            ]
        }
    bfd = [{"peer": ip, "status": "up" if i < bfd_up else "down"} for i, ip in enumerate(ips)]
    host.responses[("podman", "exec", router, "cat", "/tmp/vyos-config-status")] = Result(
        0, "0\n", ""
    )
    host.responses[vtysh_summary(router)] = Result(0, json.dumps(summary), "")
    host.responses[vtysh_route(router)] = Result(0, json.dumps(route), "")
    host.responses[vtysh_bfd(router)] = Result(0, json.dumps(bfd), "")


# the router reload: a configuration session fed on stdin, in VyOS's configuration group
VBASH = ("podman", "exec", "-i", "demo-router", "sg", "vyattacfg", "-c", "/bin/vbash -s")


def ready_host(
    host: FakeHost, data_dir: Path, name: str = "demo", *, user: model.UserCluster | None = None
) -> model.Cluster:
    """demo (1 control plane + 2 workers, or `user`'s shape) exists and is Ready here: every
    done() says yes."""
    user = (user or DEMO_USER).model_copy(update={"metadata": model.Metadata(name=name)})
    ctx = make_ctx(host, data_dir, user=user)
    c, paths = ctx.require_cluster(), ctx.paths
    state = paths.state_dir(name)
    host.files[state / "cluster.yaml"] = model.dumps(c)
    host.files[state / "init-config.yaml"] = kubeadm.render_init(ctx)
    for p, content in units.render(ctx).items():
        host.files[p] = content
    host.links.update(units.links(ctx))
    host.files[state / "kubeconfig"] = kubeconfig.rename(
        ADMIN_CONF, c.name, f"https://{c.endpoint_ip}:6443"
    )
    host.files[state / ".certificate-key"] = "abcd\n"
    host.files[paths.cni_dir / "bridge"] = "elf"
    host.dirs.update(
        {
            paths.data_dir,
            paths.clusters_dir,
            paths.cni_dir,
            paths.images_dir,
            paths.cache_dir,
            paths.wulin_dir,
            Path("/run/systemd/system"),
        }
    )
    host.active.add(f"{name}.target")
    r = host.responses
    r[("podman", "--version")] = Result(0, "podman version 5.4.2\n", "")
    r[("stat", "-fc", "%T", "/sys/fs/cgroup")] = Result(0, "cgroup2fs\n", "")
    r[("swapon",)] = Result(0, "", "")
    r[("lsmod",)] = Result(0, "br_netfilter 32768 0\n", "")
    r[("id", "-u")] = Result(0, "1000\n", "")
    r[("id", "-g")] = Result(0, "1000\n", "")
    r[("id", "-un")] = Result(0, "student\n", "")
    r[("nproc",)] = Result(0, "8\n", "")
    host.files[Path("/proc/meminfo")] = "MemTotal:       32873260 kB\nMemFree:        1 kB\n"
    for rt in c.routers:
        r[("podman", "exec", rt.name, "cat", "/tmp/vyos-config-status")] = Result(0, "0\n", "")
        r[("podman", "exec", rt.name, OP, "show", "haproxy")] = Result(0, SHOW_HAPROXY, "")
    for n in c.spec.nodes:
        r[("podman", "exec", n.name, "systemctl", "is-system-running")] = Result(0, "running\n", "")
        r[("podman", "exec", n.name, "test")] = Result(0, "", "")
    r[("kubectl", "get", "--raw", "/readyz")] = Result(0, "ok", "")
    r[("curl", "-sk", "--max-time", "3")] = Result(0, "ok", "")  # the LB answers /readyz
    r[("kubectl", "get", "node")] = Result(0, "x Ready", "")
    r[("kubectl", "get", "nodes", "--no-headers")] = Result(
        0, "".join(f"{n.name} Ready <none> 1m v1.37.1\n" for n in c.spec.nodes), ""
    )
    r[("kubectl", "-n", "kube-system", "get", "ds", "cilium")] = Result(0, "cilium", "")
    r[("kubectl", "get", "ciliumloadbalancerippool")] = Result(0, "default-pool", "")
    r[("kubectl", "get", "runtimeclass", "crun")] = Result(0, "crun", "")
    r[("kubectl", "get", "deploy", "-n", "kube-system", "metrics-server")] = Result(
        0, '["--secure-port=10250","--kubelet-insecure-tls"]', ""
    )
    r[("kubectl", "get", "storageclass", "local-path")] = Result(0, "local-path", "")
    r[("podman", "run", "--rm", "--entrypoint", "kubeadm")] = Result(
        0, f"registry.k8s.io/kube-apiserver:v{c.spec.kubernetes}\n", ""
    )
    r[("cilium", "install")] = Result(0, '      image: "quay.io/cilium/cilium:v1@sha256:a"\n', "")
    return c
