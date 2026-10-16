"""A fake host on which every create phase is already done, for whole-command tests."""

from pathlib import Path

from tk8s import model, versions
from tk8s.create import kubeadm, units
from tk8s.create.plan import resolve
from tk8s.host import FakeHost, Result
from tk8s.paths import Paths
from tk8s.phases import Context, Options

HOME = Path(__file__).resolve().parents[1]
SHOW_HAPROXY = (
    "Proxy name            Role       Status    Req rate    Resp time    Last change\n"
    "--------------------  ---------  --------  ----------  -----------  -------------\n"
    "api                   FRONTEND   OPEN      0\n"
    "api                   BACKEND    UP                    0 ms         4s\n"
    "api                   demo-control-plane-1  UP         0 ms         4s\n"
)
OP = "/opt/vyatta/bin/vyatta-op-cmd-wrapper"


def ready_host(host: FakeHost, data_dir: Path, name: str = "demo") -> model.Cluster:
    """demo (1 control plane + 2 workers) exists and is Ready here: every done() says yes."""
    paths = Paths(data_dir=data_dir, wulin_dir=data_dir / "wulin", home=HOME)
    vers = versions.load(HOME / "versions.yaml")
    user = model.UserCluster(
        metadata=model.Metadata(name=name),
        spec=model.UserSpec(
            nodes=[model.NodeGroup(role="control-plane"), model.NodeGroup(role="worker", count=2)]
        ),
    )
    c = resolve(
        user,
        k8s_default=vers.kubernetes.default,
        existing=None,
        used_indices=set(),
        allow_overlap=False,
        tk8s_version="test",
    )
    ctx = Context(host=host, paths=paths, versions=vers, options=Options(), say=host.lines.append)
    ctx.cluster = c
    state = paths.state_dir(name)
    host.files[state / "cluster.yaml"] = model.dumps(c)
    host.files[state / "init-config.yaml"] = kubeadm.render_init(ctx)
    for p, content in units.render(ctx).items():
        host.files[p] = content
    host.files[state / "kubeconfig"] = "kind: Config\n"
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
    r[("podman", "exec", f"{name}-router", "cat", "/tmp/vyos-config-status")] = Result(0, "0\n", "")
    r[("podman", "exec", f"{name}-router", OP, "show", "haproxy")] = Result(0, SHOW_HAPROXY, "")
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
