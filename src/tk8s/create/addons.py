"""addons: RuntimeClass crun (CRI-O ships crun), gVisor when the cluster asked for it,
metrics-server, local-path. Each is idempotent: an interrupted create resumes without redoing
work. Anything installed into a node lives under /usr/local/bin or /etc, the paths kept in named
volumes across restarts and reboots."""

import hashlib
from pathlib import Path

from .. import podman
from ..phases import Context, Phase, PhaseFailed
from .kubeadm import kubectl

GVISOR_RC = (
    "apiVersion: node.k8s.io/v1\nkind: RuntimeClass\nmetadata:\n  name: gvisor\nhandler: runsc\n"
)
GVISOR_CONF = "/etc/crio/crio.conf.d/20-gvisor.conf"
ZSTD_APT = "apt-get update -qq >/dev/null && apt-get install -y -qq zstd >/dev/null"


def _kubectl(ctx: Context, *args: str, input: str | None = None) -> None:
    r = kubectl(ctx, *args, input=input)
    if r.returncode != 0:
        raise PhaseFailed(f"kubectl {' '.join(args)} failed: {r.stderr.strip()}")


def _exists(ctx: Context, *args: str) -> bool:
    return kubectl(ctx, *args).returncode == 0


def _rollout(ctx: Context, ns: str, deploy: str, what: str, hint: str) -> None:
    r = kubectl(ctx, "rollout", "status", "-n", ns, f"deploy/{deploy}", "--timeout=300s")
    if r.returncode != 0:
        raise PhaseFailed(f"{what} did not become ready ({hint})")


# ---- runtimeclass


def runtimeclass_done(ctx: Context) -> bool:
    return _exists(ctx, "get", "runtimeclass", "crun")


def runtimeclass_run(ctx: Context) -> None:
    _kubectl(ctx, "apply", "-f", str(ctx.paths.home / "manifests" / "runtimeclass.yaml"))
    ctx.say("  RuntimeClass crun ok")


RUNTIMECLASS = Phase(
    "runtimeclass",
    "RuntimeClass crun (CRI-O's default runtime)",
    runtimeclass_done,
    runtimeclass_run,
)


# ---- gvisor


def gvisor_marker(ctx: Context) -> str:
    """Written after a complete extraction; a bare runsc (baked image, interrupted tar) is not
    enough."""
    return f"/usr/local/bin/.gvisor-{ctx.versions.gvisor}"


def gvisor_done(ctx: Context) -> bool:
    c = ctx.require_cluster()
    if not c.spec.gvisor:
        return True
    marker = gvisor_marker(ctx)
    nodes_ok = all(
        podman.node_has(ctx, n.name, marker) and podman.node_has(ctx, n.name, GVISOR_CONF)
        for n in c.spec.nodes
    )
    return nodes_ok and _exists(ctx, "get", "runtimeclass", "gvisor")


def gvisor_tarball(ctx: Context) -> Path:
    """Download and verify once on the host; each node extracts its own copy."""
    v = ctx.versions.gvisor
    cache = ctx.paths.cache_dir
    final = cache / f"gvisor-{v}.tar.zstd"
    if ctx.host.exists(final):
        return final
    url = f"https://storage.googleapis.com/gvisor/releases/release/{v}/x86_64"
    ctx.host.mkdir(cache)
    tmp, sha = cache / "gvisor.tar.zstd", cache / "gvisor.tar.zstd.sha512"
    for target, name in ((tmp, "gvisor.tar.zstd"), (sha, "gvisor.tar.zstd.sha512")):
        ctx.host.run(
            [
                "curl",
                "-fsSL",
                "--retry",
                "3",
                "--retry-all-errors",
                "-o",
                str(target),
                f"{url}/{name}",
            ]
        )
    data = ctx.host.read_bytes(tmp)
    expected = (ctx.host.read(sha) or "").split()[:1]
    if not data or not expected or hashlib.sha512(data).hexdigest() != expected[0]:
        ctx.host.remove(tmp)
        ctx.host.remove(sha)
        raise PhaseFailed("gvisor.tar.zstd checksum mismatch")
    ctx.host.run(["mv", str(tmp), str(final)])
    ctx.host.remove(sha)
    return final


def install_gvisor_into(ctx: Context, node: str, tarball: Path | None = None) -> None:
    """The tarball is fetched once per run (gvisor_run passes it); a single-node call fetches it."""
    v = ctx.versions.gvisor
    marker = gvisor_marker(ctx)
    if not podman.node_has(ctx, node, marker):
        tarball = tarball or gvisor_tarball(ctx)
        # Node images published before zstd was added to the recipe cannot unpack the tarball.
        if not podman.node_has(ctx, node, "/usr/bin/zstd", "-x"):
            podman.must(ctx, node, "sh", "-c", ZSTD_APT)
        ctx.host.run(["podman", "cp", str(tarball), f"{node}:/tmp/gvisor.tar.zstd"], sudo=True)
        podman.must(
            ctx, node, "tar", "--zstd", "-xf", "/tmp/gvisor.tar.zstd", "-C", "/usr/local/bin"
        )
        podman.must(ctx, node, "rm", "-f", "/tmp/gvisor.tar.zstd")
        podman.must(ctx, node, "touch", marker)
    if not podman.node_has(ctx, node, GVISOR_CONF):
        src = ctx.paths.home / "templates" / "crio" / "20-gvisor.conf"
        ctx.host.run(["podman", "cp", str(src), f"{node}:{GVISOR_CONF}"], sudo=True)
        podman.must(ctx, node, "systemctl", "restart", "crio")
        ctx.say(f"  {node}: gVisor {v} handler runsc, crio restarted")


def gvisor_run(ctx: Context) -> None:
    c = ctx.require_cluster()
    marker = gvisor_marker(ctx)
    tarball = None
    if any(not podman.node_has(ctx, n.name, marker) for n in c.spec.nodes):
        tarball = gvisor_tarball(ctx)
    for n in c.spec.nodes:
        install_gvisor_into(ctx, n.name, tarball)
    _kubectl(ctx, "apply", "-f", "-", input=GVISOR_RC)
    v = ctx.versions.gvisor
    ctx.say(f"  RuntimeClass gvisor ok (gVisor {v}; datapath must be veth, see --gvisor)")


GVISOR = Phase(
    "gvisor",
    "gVisor into every node and RuntimeClass gvisor (only with --gvisor)",
    gvisor_done,
    gvisor_run,
)


# ---- metrics-server


def _metrics_args(ctx: Context) -> str | None:
    r = kubectl(
        ctx,
        "get",
        "deploy",
        "-n",
        "kube-system",
        "metrics-server",
        "-o",
        "jsonpath={.spec.template.spec.containers[0].args}",
    )
    return r.stdout if r.returncode == 0 else None


def metrics_server_done(ctx: Context) -> bool:
    """Deployed AND patched: an interrupted create must not skip the patch on rerun."""
    args = _metrics_args(ctx)
    return args is not None and "kubelet-insecure-tls" in args


def metrics_server_run(ctx: Context) -> None:
    v = ctx.versions.metrics_server
    manifest = ctx.paths.home / "manifests" / f"metrics-server.{v.lstrip('v')}.yaml"
    if not manifest.exists():
        raise PhaseFailed(
            f"{manifest} is missing; versions.yaml pins metrics_server {v} but "
            "scripts/vendor-manifests.sh was not rerun"
        )
    _kubectl(ctx, "apply", "-f", str(manifest))
    # Node kubelet certs are self-signed; patch instead of sed on the upstream text.
    if "kubelet-insecure-tls" not in (_metrics_args(ctx) or ""):
        _kubectl(
            ctx,
            "-n",
            "kube-system",
            "patch",
            "deploy",
            "metrics-server",
            "--type=json",
            "-p",
            '[{"op":"add","path":"/spec/template/spec/containers/0/args/-",'
            '"value":"--kubelet-insecure-tls"}]',
        )
    _rollout(
        ctx,
        "kube-system",
        "metrics-server",
        "metrics-server",
        "kubectl -n kube-system describe deploy metrics-server",
    )
    ctx.say(f"  metrics-server {v} ok")


METRICS_SERVER = Phase(
    "metrics-server", "metrics-server (kubectl top)", metrics_server_done, metrics_server_run
)


# ---- local-path


def local_path_done(ctx: Context) -> bool:
    return _exists(ctx, "get", "storageclass", "local-path")


def local_path_run(ctx: Context) -> None:
    v = ctx.versions.local_path_provisioner
    manifest = ctx.paths.home / "manifests" / f"local-path-storage.{v.lstrip('v')}.yaml"
    if not manifest.exists():
        raise PhaseFailed(
            f"{manifest} is missing; versions.yaml pins local_path_provisioner {v} "
            "but the customized manifest was not updated"
        )
    _kubectl(ctx, "apply", "-f", str(manifest))
    _rollout(
        ctx,
        "local-path-storage",
        "local-path-provisioner",
        "local-path-provisioner",
        "kubectl -n local-path-storage get pods",
    )
    ctx.say(f"  local-path {v} ok (PV data under {ctx.state_dir}/storage)")


LOCAL_PATH = Phase(
    "local-path",
    "local-path-provisioner with PV data under the state dir",
    local_path_done,
    local_path_run,
)

PHASE = Phase(
    "addons",
    "RuntimeClass, gVisor, metrics-server, local-path",
    None,
    lambda ctx: None,
    children=(RUNTIMECLASS, GVISOR, METRICS_SERVER, LOCAL_PATH),
)
