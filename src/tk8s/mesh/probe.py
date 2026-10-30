"""Whether two clusters reach each other: a node of one pings a node of the other (the
control-plane-1 containers), then a short-lived pod of one pings a pod of the other (coredns,
which every cluster has). Both directions; the pod probe is retried, the routes Cilium installs
for a new peer take a few seconds."""

from .. import podman
from ..create.kubeadm import kubectl
from ..phases import Context, poll_until


def _name(ctx: Context) -> str:
    return ctx.require_cluster().name


def node_ping(src: Context, dst: Context) -> str | None:
    """None when control-plane-1 of src pings control-plane-1 of dst, else what failed."""
    cp, target = (
        src.require_cluster().first_control_plane,
        dst.require_cluster().first_control_plane,
    )
    r = podman.exec_(src, cp.name, "ping", "-c", "1", "-W", "3", str(target.ip))
    if r.returncode == 0:
        return None
    return f"{_name(src)} -> {_name(dst)}: node {target.ip} unreachable from {cp.name}"


def peer_pod_ip(dst: Context) -> str:
    """A pod IP in dst to aim at: its first coredns pod; empty when there is none."""
    r = kubectl(
        dst,
        "-n",
        "kube-system",
        "get",
        "pod",
        "-l",
        "k8s-app=kube-dns",
        "-o",
        "jsonpath={.items[0].status.podIP}",
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def pod_ping(src: Context, dst: Context) -> str | None:
    """None when a busybox pod run in src pings a coredns pod of dst, else what failed."""
    ip = peer_pod_ip(dst)
    if not ip:
        return f"{_name(dst)}: no coredns pod with an IP to probe"
    pod = f"tk-mesh-probe-{_name(dst)}"
    r = kubectl(
        src,
        "run",
        pod,
        f"--image={src.versions.verify_busybox}",
        "--restart=Never",
        "--rm",
        "-i",
        "--attach",
        "--quiet",
        "--command",
        "--",
        "ping",
        "-c",
        "1",
        "-W",
        "3",
        ip,
    )
    kubectl(src, "delete", "pod", pod, "--ignore-not-found", "--wait=false")
    if r.returncode == 0:
        return None
    return f"{_name(src)} -> {_name(dst)}: pod {ip} unreachable from a pod in {_name(src)}"


def _pods_reach(src: Context, dst: Context, *, rounds: int, delay: float) -> str | None:
    """pod_ping retried up to rounds: None once it held, else the last failure."""
    last: str | None = None

    def once() -> bool:
        nonlocal last
        last = pod_ping(src, dst)
        return last is None

    poll_until(once, rounds=rounds, delay=delay)
    return last


def probe_pair(a: Context, b: Context, *, rounds: int, delay: float) -> list[str]:
    """Every failure between a and b, both directions, the pod probes retried up to rounds."""
    failures: list[str] = []
    for src, dst in ((a, b), (b, a)):
        for failure in (node_ping(src, dst), _pods_reach(src, dst, rounds=rounds, delay=delay)):
            if failure:
                failures.append(failure)
    return failures
