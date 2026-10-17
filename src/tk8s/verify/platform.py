"""[nodes] [kubelet] [system] [metrics-server] [local-path]: the cluster itself, cilium and the
addons every cluster has."""

from pathlib import Path

from .. import podman
from ..create.kubeadm import kubectl_env
from ..render import template
from .report import NS, Report


def check_nodes(r: Report) -> None:
    n = len(r.ctx.require_cluster().spec.nodes)

    def ready() -> list[str]:
        _, out = r.kubectl("get", "nodes", "--no-headers")
        return [ln.split()[0] for ln in out.splitlines() if ln.split()[1:2] == ["Ready"]]

    r.poll(r.timeout // 5, lambda: len(ready()) == n)
    got = ready()
    if len(got) == n:
        r.ok(f"nodes: {n}/{n} Ready")
        _, out = r.kubectl(
            "get",
            "nodes",
            "-o",
            "custom-columns=NODE:.metadata.name,K8S:.status.nodeInfo.kubeletVersion,"
            "RUNTIME:.status.nodeInfo.containerRuntimeVersion,KERNEL:.status.nodeInfo.kernelVersion",
            "--no-headers",
        )
        for ln in out.splitlines():
            r.ctx.say(f"  {ln.strip()}")
    else:
        _, out = r.kubectl("get", "nodes", "--no-headers")
        bad = " ".join(ln.split()[0] for ln in out.splitlines() if ln.split()[1:2] != ["Ready"])
        r.ng(f"nodes: {len(got)}/{n} Ready", bad)


def check_kubelet(r: Report) -> None:
    """A kubelet whose --runtime-cgroups names a cgroup that never exists logs "Failed to get
    system container stats" every few seconds; two minutes of log is enough to tell."""
    cp = r.ctx.require_cluster().first_control_plane.name
    out = podman.exec_(
        r.ctx, cp, "journalctl", "-u", "kubelet", "--no-pager", "--since", "-2min"
    ).stdout
    noisy = [ln for ln in out.splitlines() if "Failed to get system container stats" in ln]
    if not noisy:
        r.ok("kubelet: no runtime cgroup errors")
        return
    what = ""
    if "cgroup stats for " in noisy[0]:
        what = "cgroup stats for " + noisy[0].split("cgroup stats for ", 1)[1].split('"')[0]
    r.ng("kubelet runtime cgroup", f"{len(noisy)} errors in 2 min ({what})")


def check_kube_system(r: Report) -> None:
    """Right after a boot or start the system pods are still coming up; same wait as the nodes."""

    def not_running() -> str:
        _, out = r.kubectl("get", "pods", "-n", "kube-system", "--no-headers")
        bad = [
            f"{f[0]}({f[2]})"
            for f in (ln.split() for ln in out.splitlines())
            if len(f) > 2 and f[2] not in ("Running", "Completed")
        ]
        return " ".join(bad)

    r.poll(r.timeout // 5, lambda: not not_running())
    bad = not_running()
    if bad:
        r.ng("kube-system pods", bad)
    else:
        r.ok("kube-system pods all Running")


def check_cilium(r: Report) -> None:
    c = r.ctx.require_cluster()
    have_cli = r.ctx.host.run(["cilium", "version", "--client"], capture=True, check=False)
    if have_cli.returncode != 0:
        r.skip("cilium status", "cilium CLI not on the host")
    else:
        st = r.ctx.host.run(
            ["cilium", "status", "--wait", "--wait-duration", f"{r.timeout}s"],
            env=kubectl_env(r.ctx),
            capture=True,
            check=False,
        )
        out = Path(f"/tmp/{c.name}-verify-cilium.out")
        r.ctx.host.write(out, st.stdout + st.stderr)
        if st.returncode == 0:
            r.ok("cilium status")
        else:
            r.ng("cilium status", f"see {out}")
    _, dp = r.kubectl(
        "-n", "kube-system", "get", "cm", "cilium-config", "-o", "jsonpath={.data.datapath-mode}"
    )
    r.ctx.say(f"  datapath: {dp.strip() or 'unknown'}")


OPERATOR_GATEWAY_OFF = "Required GatewayAPI resources are not found"


def check_operator(r: Report) -> None:
    """cilium-operator restarted into a flaky apiserver takes a CRD discovery error for "CRDs not
    installed" and then runs Ready with Gateway API off for the life of the container
    (cilium/cilium#48083); LB-IPAM still works, so the symptom surfaces three checks later as a
    Gateway without an address. A rollout restart is the recovery."""
    _, out = r.kubectl(
        "-n",
        "kube-system",
        "get",
        "pods",
        "-l",
        "name=cilium-operator",
        "-o",
        'jsonpath={range .items[*]}{.status.containerStatuses[0].restartCount}{"\\n"}{end}',
    )
    restarts = sum(int(x) for x in out.split() if x.isdigit())
    _, log = r.kubectl("-n", "kube-system", "logs", "deploy/cilium-operator", "--tail=5000")
    if OPERATOR_GATEWAY_OFF in log:
        r.ng(
            "cilium-operator",
            f"{restarts} restart(s), Gateway API off since the last start (cilium/cilium#48083); "
            "recover with: kubectl -n kube-system rollout restart deploy/cilium-operator",
        )
        return
    r.ok(f"cilium-operator: {restarts} restarts, Gateway API on")


def check_metrics(r: Report) -> None:
    """metrics-server serves data only after its first scrape; after a boot that takes a minute."""
    if r.poll(r.timeout // 5, lambda: r.kubectl("top", "nodes")[0] == 0):
        r.ok("kubectl top nodes")
    else:
        r.ng("kubectl top nodes", "metrics-server not ready")


def check_storage(r: Report) -> None:
    v = r.ctx.versions
    r.kubectl(
        "apply",
        "-n",
        NS,
        "-f",
        "-",
        input=template(r.ctx.paths.home, "verify/pvc.yaml", busybox=v.verify_busybox),
    )
    rc, _ = r.kubectl(
        "wait",
        "-n",
        NS,
        "pod/writer",
        "--for=jsonpath={.status.phase}=Succeeded",
        f"--timeout={r.timeout}s",
    )
    _, logs = r.kubectl("logs", "-n", NS, "writer")
    if rc == 0 and logs.strip() == "verify-ok":
        r.ok("PVC bound and pod wrote data")
        _, pv = r.kubectl("get", "pvc", "-n", NS, "pvc", "-o", "jsonpath={.spec.volumeName}")
        storage = r.ctx.state_dir / "storage"
        found = r.ctx.host.exists(storage / f"{pv.strip()}_{NS}_pvc")
        if found:
            r.ok(f"PV data on host ({storage})")
        else:
            r.ng("PV data on host", f"no {storage}/{pv.strip()}_*")
    else:
        _, pvc = r.kubectl("get", "pvc", "-n", NS, "pvc", "--no-headers")
        _, phase = r.kubectl("get", "pod", "-n", NS, "writer", "-o", "jsonpath={.status.phase}")
        pvc_state = f"pvc {pvc.split()[1]}" if len(pvc.split()) > 1 else ""
        r.ng("PVC / writer pod", f"{pvc_state}; {phase.strip()}")
