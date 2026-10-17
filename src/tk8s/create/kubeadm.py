"""kubeadm: the init configuration, then init on the first control plane and join for the rest.

Plan 1 ships the render sub-phase; init, join and kubeconfig follow with the same shape.
"""

import re
import time
from pathlib import Path

from .. import model
from ..phases import Context, Phase, PhaseFailed
from ..render import template
from . import frr, router

KEY_RE = re.compile(r"--certificate-key ([0-9a-f]+)")


def kubectl_env(ctx: Context) -> dict[str, str]:
    """kubectl never sees the caller's KUBECONFIG; every call names the cluster's own."""
    return {"KUBECONFIG": str(ctx.state_dir / "kubeconfig")}


def render_init(ctx: Context) -> str:
    c = ctx.require_cluster()
    cp, net = c.first_control_plane, c.spec.network
    sans = [f"api.{c.domain}", str(net.router)]
    if net.vip is not None:
        sans.append(str(net.vip))
    sans += [str(n.ip) for n in c.control_planes]
    return template(
        ctx.paths.home,
        "init-config.yaml",
        cluster=c.name,
        cp_ip=cp.ip,
        cp_name=cp.name,
        k8s=c.spec.kubernetes,
        domain=c.domain,
        pods=net.pods,
        services=net.services,
        cert_sans="\n".join(f"    - {s}" for s in sans),
    )


def audit_policy(ctx: Context) -> tuple[Path, str]:
    """The apiserver's --audit-policy-file: the state dir is /opt/taroko inside the node, and
    logs/ belongs to the user while the apiserver (root in the node) writes audit.log into it."""
    path = ctx.state_dir / "logs" / "apiserver" / "audit-policy.yaml"
    return path, (ctx.paths.home / "manifests" / "audit-policy.yaml").read_text()


def render_done(ctx: Context) -> bool:
    policy, text = audit_policy(ctx)
    return (
        ctx.host.read(ctx.state_dir / "init-config.yaml") == render_init(ctx)
        and ctx.host.read(policy) == text
    )


def render_run(ctx: Context) -> None:
    ctx.host.write(ctx.state_dir / "init-config.yaml", render_init(ctx), mode=0o600)
    policy, text = audit_policy(ctx)
    ctx.host.write(policy, text)


RENDER = Phase(
    "render", "write the kubeadm init configuration into the state dir", render_done, render_run
)


# ---- init


def save_certificate_key(ctx: Context, key: str) -> None:
    ctx.host.write(ctx.state_dir / ".certificate-key", key + "\n", mode=0o600)


def wait_router_ready(ctx: Context) -> None:
    """kubeadm talked to the apiserver through api.<cluster>.k8s; make sure the endpoint (the
    router in proxy mode, the VIP the router routes in bgp mode) answers before anything else
    uses it."""
    c = ctx.require_cluster()
    endpoint = c.endpoint_ip
    for i in range(1, 31):
        r = ctx.host.run(
            ["curl", "-sk", "--max-time", "3", f"https://{endpoint}:6443/readyz"],
            capture=True,
            check=False,
        )
        if r.stdout.strip() == "ok":
            return
        if i == 30:
            raise PhaseFailed(
                f"the apiserver at {endpoint} (via {c.name}-router) answered no readyz within "
                f"60 s (podman exec {c.name}-router {router.show(c)})"
            )
        time.sleep(ctx.options.poll)


def _initialised(ctx: Context, cp: str) -> bool:
    r = ctx.host.run(
        ["podman", "exec", cp, "test", "-f", "/etc/kubernetes/admin.conf"], sudo=True, check=False
    )
    return r.returncode == 0


def _completed(ctx: Context, cp: str) -> bool:
    """kubeadm init got to the end: it uploaded its configuration (after the apiserver answered
    and the admin user was bootstrapped). A kubeadm init that died waiting for the control plane
    leaves admin.conf behind but none of this."""
    r = ctx.host.run(
        [
            "podman",
            "exec",
            cp,
            "kubectl",
            "--kubeconfig",
            "/etc/kubernetes/admin.conf",
            "-n",
            "kube-system",
            "get",
            "configmap",
            "kubeadm-config",
        ],
        sudo=True,
        capture=True,
        check=False,
    )
    return r.returncode == 0


def init_done(ctx: Context) -> bool:
    cp = ctx.require_cluster().first_control_plane.name
    return _initialised(ctx, cp) and _completed(ctx, cp)


def init_run(ctx: Context) -> None:
    """kubeadm init on the first control plane; keep the certificate key for the other control
    planes; wait until the router sees the apiserver (a resume right after a restart: the
    apiserver may still be coming up) before judging whether init completed."""
    cp = ctx.require_cluster().first_control_plane.name
    if _initialised(ctx, cp):
        ctx.say(f"  {cp} already initialized")
    else:
        ctx.say(f"  kubeadm init on {cp} ...")
        out = ctx.host.run(
            [
                "podman",
                "exec",
                cp,
                "kubeadm",
                "init",
                "--config=/opt/taroko/init-config.yaml",
                "--upload-certs",
                "--ignore-preflight-errors=SystemVerification",
            ],
            sudo=True,
            capture=True,
            check=False,
        )
        if out.returncode != 0:
            tail = "\n".join((out.stdout + out.stderr).splitlines()[-30:])
            raise PhaseFailed(f"kubeadm init on {cp} failed:\n{tail}")
        m = KEY_RE.search(out.stdout)
        if m is None:
            raise PhaseFailed("certificate key not found in kubeadm output")
        save_certificate_key(ctx, m.group(1))
        ctx.say(f"  {cp} initialized")
    wait_router_ready(ctx)
    if not _completed(ctx, cp):
        raise PhaseFailed(
            f"kubeadm init on {cp} did not complete (admin.conf exists but the cluster was "
            "never bootstrapped); delete the cluster and create it again"
        )


INIT = Phase("init", "kubeadm init on the first control plane", init_done, init_run)


# ---- kubeconfig


def kubeconfig_done(ctx: Context) -> bool:
    text = ctx.host.read(ctx.state_dir / "kubeconfig")
    if not text:
        return False
    r = ctx.host.run(
        ["kubectl", "get", "--raw", "/readyz"], env=kubectl_env(ctx), capture=True, check=False
    )
    return r.returncode == 0


def kubeconfig_run(ctx: Context) -> None:
    """The host cannot resolve api.<cluster>.k8s; its kubeconfig points straight at what the name
    means: the router, or the VIP in bgp mode (routes.service gives the host a route to it)."""
    c = ctx.require_cluster()
    cp = c.first_control_plane.name
    r = ctx.host.run(
        ["podman", "exec", cp, "cat", "/etc/kubernetes/admin.conf"],
        sudo=True,
        capture=True,
        check=False,
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise PhaseFailed(f"could not read /etc/kubernetes/admin.conf from {cp}")
    text = r.stdout.replace(
        f"server: https://api.{c.domain}:6443", f"server: https://{c.endpoint_ip}:6443"
    )
    ctx.host.write(ctx.state_dir / "kubeconfig", text, mode=0o600)
    # On a resume init is already done and skipped: this is where the apiserver gets waited for.
    wait_router_ready(ctx)


KUBECONFIG = Phase(
    "kubeconfig",
    "hand the host a kubeconfig that points at the router (proxy) or the VIP (bgp)",
    kubeconfig_done,
    kubeconfig_run,
)

# ---- join


def joined(ctx: Context, name: str) -> bool:
    r = ctx.host.run(
        ["kubectl", "get", "node", name], env=kubectl_env(ctx), capture=True, check=False
    )
    return r.returncode == 0


def join_command(ctx: Context) -> str:
    cp = ctx.require_cluster().first_control_plane.name
    r = ctx.host.run(
        ["podman", "exec", cp, "kubeadm", "token", "create", "--print-join-command"],
        sudo=True,
        capture=True,
    )
    return r.stdout.replace("\r", "").strip()


def certificate_key(ctx: Context, *, fresh: bool = False) -> str:
    """The stored key, or a fresh upload. Certificate keys expire after two hours: create reuses
    the one init just made, a join days later asks for a new one (fresh=True)."""
    stored = None if fresh else ctx.host.read(ctx.state_dir / ".certificate-key")
    if stored and stored.strip():
        return stored.strip()
    cp = ctx.require_cluster().first_control_plane.name
    r = ctx.host.run(
        ["podman", "exec", cp, "kubeadm", "init", "phase", "upload-certs", "--upload-certs"],
        sudo=True,
        capture=True,
        check=False,
    )
    lines = r.stdout.strip().splitlines()
    if r.returncode != 0 or not lines:
        raise PhaseFailed(f"could not obtain a certificate key from {cp}")
    key = lines[-1].strip()
    save_certificate_key(ctx, key)
    return key


def join_node(ctx: Context, node: model.Node, join: str, key: str) -> None:
    extra = (
        f" --control-plane --certificate-key {key} --apiserver-advertise-address {node.ip}"
        if node.role == "control-plane"
        else ""
    )
    cmd = f"{join}{extra} --ignore-preflight-errors=SystemVerification"
    r = ctx.host.run(
        ["podman", "exec", node.name, "bash", "-c", cmd], sudo=True, capture=True, check=False
    )
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).splitlines()[-20:])
        raise PhaseFailed(f"kubeadm join on {node.name} failed:\n{tail}")
    ctx.say(f"  {node.name} joined ({node.role})")


def untaint_control_planes(ctx: Context) -> None:
    """Control planes schedule workloads too (a teaching cluster is small)."""
    for n in ctx.require_cluster().control_planes:
        ctx.host.run(
            [
                "kubectl",
                "taint",
                "node",
                n.name,
                "node-role.kubernetes.io/control-plane:NoSchedule-",
            ],
            env=kubectl_env(ctx),
            capture=True,
            check=False,
        )


def _joining(ctx: Context) -> list[model.Node]:
    return [n for n in ctx.require_cluster().spec.nodes[1:] if n.join]


def join_done(ctx: Context) -> bool:
    return all(joined(ctx, n.name) for n in _joining(ctx))


def join_run(ctx: Context) -> None:
    c = ctx.require_cluster()
    join = join_command(ctx)
    needs_key = any(n.role == "control-plane" for n in _joining(ctx))
    key = certificate_key(ctx) if needs_key else ""
    for n in c.spec.nodes[1:]:
        if not n.join:
            ctx.say(
                f"  {n.name} left unjoined (join: false); join later with: "
                f"tkctl join node {c.name} {n.name}"
            )
        elif joined(ctx, n.name):
            ctx.say(f"  {n.name} already joined")
        else:
            join_node(ctx, n, join, key)
    untaint_control_planes(ctx)


JOIN = Phase("join", "join the other control planes and the workers", join_done, join_run)

PHASE = Phase(
    "kubeadm",
    "initialise the first control plane and join the others",
    None,
    lambda ctx: None,
    children=(RENDER, frr.PHASE, INIT, KUBECONFIG, JOIN),
)
