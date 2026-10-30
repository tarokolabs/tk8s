"""[nodes] [kubelet] [system] [metrics-server] [local-path]: the cluster itself, cilium and the
addons every cluster has."""

from .. import podman
from ..create import nodefs
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


SIZES = (
    "{range .items[*]}{.metadata.name} {.status.capacity.cpu} {.status.capacity.memory} "
    '{.status.allocatable.cpu} {.status.allocatable.memory}{"\\n"}{end}'
)


def check_node_sizes(r: Report) -> None:
    """Capacity is the node's cpu/memory and allocatable that minus kubeReserved and the eviction
    threshold. Allocatable is checked too: a host still running the old node image mounts the
    files fine but its kubelet has no --config-dir, so only the reservation goes missing."""
    c = r.ctx.require_cluster()
    if c.status is None or not c.status.node_capacity:
        r.skip(
            "node sizes",
            "cluster created before node capacity sizing; recreate it to get node-sized capacity",
        )
        return
    _, out = r.kubectl("get", "nodes", "-o", f"jsonpath={SIZES}")
    seen = {ln.split()[0]: ln.strip() for ln in out.splitlines() if ln.split()}
    bad = []
    for n in c.spec.nodes:
        if n.name not in seen:
            bad.append(f"{n.name}: not in kubectl get nodes")
            continue
        fields = seen[n.name].split()[1:]
        if len(fields) != 4:
            bad.append(f"{n.name}: unexpected line {seen[n.name]!r}")
            continue
        cap_cpu, cap_mem, alloc_cpu, alloc_mem = fields
        want_cpu, want_mem = nodefs.expected_allocatable(n)
        ok = (
            nodefs.cpu_milli(cap_cpu) == n.cpu * 1000
            and nodefs.quantity_bytes(cap_mem) == nodefs.memory_bytes(n.memory)
            and nodefs.cpu_milli(alloc_cpu) == want_cpu
            and nodefs.quantity_bytes(alloc_mem) == want_mem
        )
        if not ok:
            bad.append(
                f"{n.name}: capacity {cap_cpu}/{cap_mem} allocatable {alloc_cpu}/{alloc_mem}, "
                f"want {want_cpu}m/{want_mem // 2**20}Mi"
            )
    if bad:
        r.ng("node sizes", "; ".join(bad))
    else:
        r.ok(
            "node sizes: capacity and allocatable follow cluster.yaml "
            f"(kubeReserved {nodefs.KUBE_RESERVED_CPU_MILLI}m/"
            f"{nodefs.KUBE_RESERVED_MEMORY // 2**20}Mi)"
        )


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
        out = r.ctx.log_path("verify-cilium.out")
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
        # local-path writes on the node that ran the pod: on several hosts, whichever host that is
        found = any(h.exists(storage / f"{pv.strip()}_{NS}_pvc") for _, h in r.ctx.node_hosts())
        if found:
            r.ok(f"PV data on host ({storage})")
        else:
            r.ng("PV data on host", f"no {storage}/{pv.strip()}_*")
    else:
        _, pvc = r.kubectl("get", "pvc", "-n", NS, "pvc", "--no-headers")
        _, phase = r.kubectl("get", "pod", "-n", NS, "writer", "-o", "jsonpath={.status.phase}")
        pvc_state = f"pvc {pvc.split()[1]}" if len(pvc.split()) > 1 else ""
        r.ng("PVC / writer pod", f"{pvc_state}; {phase.strip()}")
