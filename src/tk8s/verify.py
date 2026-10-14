"""Platform health checks, one PASS/FAIL/SKIP line each, exit 1 on any FAIL. Test resources live
in namespace tk-verify and are removed at the end, including on interruption. Shared by people
and the L3 CI. TK_VERIFY_TIMEOUT is the per-check wait in seconds (default 180).

Each check is a phase (tkctl verify cluster NAME --phase <check>); verify walks them itself so
the output keeps the [section] lines people and tklab's logs expect."""

import time
from dataclasses import dataclass, field
from pathlib import Path

from .create.kubeadm import kubectl_env
from .create.nodes import stats_csv
from .phases import Context, Phase, PhaseFailed
from .render import template

NS = "tk-verify"


class VerifyError(Exception):
    pass


@dataclass
class Report:
    ctx: Context
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    gateway_ip: str = ""
    checked: dict[str, bool] = field(default_factory=dict)

    def ok(self, msg: str) -> None:
        self.passed += 1
        self.ctx.say(f"PASS  {msg}")

    def ng(self, msg: str, detail: str = "") -> None:
        self.failed += 1
        self.ctx.say(f"FAIL  {msg}{' — ' + detail if detail else ''}")

    def skip(self, msg: str, why: str) -> None:
        self.skipped += 1
        self.ctx.say(f"SKIP  {msg} — {why}")

    @property
    def timeout(self) -> int:
        return self.ctx.options.verify_timeout

    def kubectl(self, *args: str, input: str | None = None) -> tuple[int, str]:
        r = self.ctx.host.run(
            ["kubectl", *args], env=kubectl_env(self.ctx), input=input, capture=True, check=False
        )
        return r.returncode, r.stdout

    def poll(self, rounds: int, probe: object) -> bool:
        """Call probe() up to rounds times, poll_slow apart, until it returns true."""
        for _ in range(max(rounds, 1)):
            if probe():  # type: ignore[operator]
                return True
            time.sleep(self.ctx.options.poll_slow)
        return False


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
    out = r.ctx.host.run(
        ["podman", "exec", cp, "journalctl", "-u", "kubelet", "--no-pager", "--since", "-2min"],
        sudo=True,
        capture=True,
        check=False,
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
    if c.spec.cni != "cilium":
        return
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


def run_rc(r: Report, rc: str, name: str, cmd: str) -> str:
    """A pod under a RuntimeClass; its output."""
    r.kubectl(
        "run",
        "-n",
        NS,
        name,
        f"--image={r.ctx.versions.verify_alpine}",
        "--restart=Never",
        f"--pod-running-timeout={r.timeout}s",
        f'--overrides={{"spec":{{"runtimeClassName":"{rc}"}}}}',
        "--",
        "sh",
        "-c",
        cmd,
    )
    r.kubectl(
        "wait",
        "-n",
        NS,
        f"pod/{name}",
        "--for=jsonpath={.status.phase}=Succeeded",
        f"--timeout={r.timeout}s",
    )
    _, out = r.kubectl("logs", "-n", NS, name)
    return out.strip()


def check_runtimeclass_crun(r: Report) -> None:
    if r.kubectl("get", "runtimeclass", "crun")[0] != 0:
        r.ng("RuntimeClass crun", "missing")
        return
    out = run_rc(r, "crun", "rc-crun", "uname -r")
    if out:
        r.ok(f"RuntimeClass crun (kernel {out})")
    else:
        r.ng("RuntimeClass crun", "pod did not succeed")


def check_runtimeclass_gvisor(r: Report) -> None:
    c = r.ctx.require_cluster()
    if not c.spec.gvisor:
        r.skip("RuntimeClass gvisor", "not enabled (create the cluster with --gvisor)")
        return
    if r.kubectl("get", "runtimeclass", "gvisor")[0] != 0:
        r.ng("RuntimeClass gvisor", "missing although the cluster was created with --gvisor")
        return
    out = run_rc(
        r,
        "gvisor",
        "rc-gvisor",
        "uname -r; wget -qO- -T 10 --no-check-certificate "
        "https://kubernetes.default.svc/version 2>/dev/null | grep -o gitVersion",
    )
    r.checked["gvisor-out"] = "gitVersion" in out
    if "gvisor" in out:
        r.ok(f"RuntimeClass gvisor (kernel {out.splitlines()[0]})")
    else:
        r.ng("RuntimeClass gvisor", f"pod did not succeed or kernel is not gVisor: {out[:80]}")


def check_gvisor_network(r: Report) -> None:
    c = r.ctx.require_cluster()
    if not c.spec.gvisor or r.kubectl("get", "runtimeclass", "gvisor")[0] != 0:
        return
    if r.checked.get("gvisor-out"):
        r.ok("gvisor sandbox network (kubernetes API reachable)")
    else:
        dp = c.spec.datapath_resolved or c.spec.datapath
        r.ng(
            "gvisor sandbox network",
            f"kubernetes.default.svc unreachable from the sandbox (datapath {dp})",
        )


def check_apiserver_lb(r: Report) -> None:
    """The LB's stats CSV (column 18 = status) says how many control planes it sees UP."""
    c = r.ctx.require_cluster()
    lb = c.spec.network.lb
    joined = sum(1 for n in c.control_planes if n.join)
    rows = [ln.split(",") for ln in stats_csv(r.ctx).splitlines()]
    up = sum(
        1
        for f in rows
        if len(f) > 17 and f[0] == "control-planes" and f[1] != "BACKEND" and f[17].startswith("UP")
    )  # "UP 1/2" (rise window) serves too
    ready = r.ctx.host.run(
        ["curl", "-sk", "--max-time", "5", f"https://{lb}:6443/readyz"], capture=True, check=False
    ).stdout[:40]
    if ready == "ok" and up == joined:
        r.ok(f"apiserver LB {lb} healthy ({up}/{joined} control planes UP)")
    else:
        r.ng(
            f"apiserver LB {lb}",
            f"readyz='{ready}', {up} of {joined} control planes UP (podman logs {c.name}-lb)",
        )


def _gateway_ready(r: Report) -> bool:
    c = r.ctx.require_cluster()
    if c.spec.cni != "cilium":
        r.skip("Gateway API", f"cni {c.spec.cni} (only the cilium path provides it)")
        return False
    return True


def check_gatewayclass(r: Report) -> None:
    if not _gateway_ready(r):
        return
    rc, _ = r.kubectl("wait", "gatewayclass", "cilium", "--for=condition=Accepted", "--timeout=60s")
    if rc == 0:
        r.ok("GatewayClass cilium Accepted")
    else:
        r.ng("GatewayClass cilium", "not Accepted")


def check_gateway(r: Report) -> None:
    c = r.ctx.require_cluster()
    if c.spec.cni != "cilium":
        return
    v = r.ctx.versions
    r.kubectl(
        "apply",
        "-n",
        NS,
        "-f",
        "-",
        input=template(
            r.ctx.paths.home, "verify/gateway.yaml", cluster=c.name, echo_image=v.verify_agnhost
        ),
    )
    rc, _ = r.kubectl("rollout", "status", "-n", NS, "deploy/echo", f"--timeout={r.timeout}s")
    if rc != 0:
        r.ng("echo backend rollout", "pods not ready")

    def addr() -> str:
        _, out = r.kubectl(
            "get", "gateway", "-n", NS, "gw", "-o", "jsonpath={.status.addresses[0].value}"
        )
        return out.strip()

    r.poll(r.timeout // 5, lambda: bool(addr()))
    r.gateway_ip = addr()
    if not r.gateway_ip:
        r.ng("Gateway address", "LB-IPAM assigned no IP")
    else:
        r.ok(f"Gateway programmed ({r.gateway_ip})")
        time.sleep(r.ctx.options.poll_slow)


def check_httproute(r: Report) -> None:
    if r.ctx.require_cluster().spec.cni != "cilium" or not r.gateway_ip:
        return
    c = r.ctx.require_cluster()
    host = ""
    for _ in range(6):
        host = r.ctx.host.run(
            [
                "curl",
                "-s",
                "-m",
                "8",
                "-H",
                f"Host: verify.{c.name}.test",
                f"http://{r.gateway_ip}/hostname",
            ],
            capture=True,
            check=False,
        ).stdout
        if host:
            break
        time.sleep(r.ctx.options.poll_slow)
    if host:
        r.ok(f"HTTPRoute reachable from host (backend {host})")
    else:
        r.ng("HTTPRoute from host", f"no response from http://{r.gateway_ip}/")


def check_xff(r: Report) -> None:
    c = r.ctx.require_cluster()
    if c.spec.cni != "cilium" or not r.gateway_ip:
        return
    xff = r.ctx.host.run(
        [
            "curl",
            "-s",
            "-m",
            "8",
            "-H",
            f"Host: verify.{c.name}.test",
            f"http://{r.gateway_ip}/header?key=X-Forwarded-For",
        ],
        capture=True,
        check=False,
    ).stdout
    host_ip = str(c.spec.network.gateway)  # the host's address on the node network
    if xff == host_ip:
        r.ok(f"X-Forwarded-For carries the client IP ({xff})")
    else:
        r.ng("X-Forwarded-For", f"got '{xff}', expected {host_ip}")


def check_tcproute(r: Report) -> None:
    if r.ctx.require_cluster().spec.cni != "cilium" or not r.gateway_ip:
        return
    tcp = r.ctx.host.run(
        ["curl", "-s", "-m", "8", f"http://{r.gateway_ip}:9000/hostname"], capture=True, check=False
    ).stdout
    if tcp:
        r.ok(f"TCPRoute reachable from host (backend {tcp})")
    else:
        r.ng("TCPRoute from host", f"no response from {r.gateway_ip}:9000")


CHECKS = [
    ("nodes", "every node Ready", check_nodes),
    ("kubelet", "no runtime cgroup errors in the kubelet log", check_kubelet),
    ("kube-system", "every kube-system pod Running", check_kube_system),
    ("cilium", "cilium status", check_cilium),
    ("metrics", "kubectl top nodes", check_metrics),
    ("storage", "a PVC bound and written, data under the state dir", check_storage),
    ("runtimeclass-crun", "a pod under RuntimeClass crun", check_runtimeclass_crun),
    ("runtimeclass-gvisor", "a pod under RuntimeClass gvisor", check_runtimeclass_gvisor),
    ("gvisor-network", "the kubernetes API reachable from a gVisor sandbox", check_gvisor_network),
    ("apiserver-lb", "the LB healthy with every joined control plane UP", check_apiserver_lb),
    ("gatewayclass", "GatewayClass cilium Accepted", check_gatewayclass),
    ("gateway", "a Gateway programmed by LB-IPAM", check_gateway),
    ("httproute", "an HTTPRoute reachable from the host", check_httproute),
    ("xff", "X-Forwarded-For carries the client IP", check_xff),
    ("tcproute", "a TCPRoute reachable from the host", check_tcproute),
]
PHASES: list[Phase] = [Phase(name, summary, None, lambda ctx: None) for name, summary, _ in CHECKS]
# A check that reads what an earlier one set up (the Gateway's address, the gVisor pod's output)
# runs that one first when asked for on its own.
PREREQUISITES = {
    "httproute": ["gateway"],
    "xff": ["gateway"],
    "tcproute": ["gateway"],
    "gvisor-network": ["runtimeclass-gvisor"],
}
SECTIONS = {
    "nodes": "nodes",
    "kubelet": "kubelet",
    "kube-system": "system",
    "metrics": "metrics-server",
    "storage": "local-path",
    "runtimeclass-crun": "runtimeclass",
    "apiserver-lb": "apiserver-lb",
    "gatewayclass": "gateway-api",
}


def _section_of(check: str) -> str:
    """The [section] a check prints under: the nearest section heading at or before it."""
    current = ""
    for name, _, _ in CHECKS:
        current = SECTIONS.get(name, current)
        if name == check:
            return current
    return current


def run(ctx: Context, only: str | None = None) -> int:
    """Every check (or one), then the summary line; the number of FAILs."""
    c = ctx.require_cluster()
    if c.status is None or c.status.phase != "Ready":
        raise VerifyError(
            f"cluster {c.name} is not fully created; rerun tkctl create cluster {c.name}"
        )
    if not ctx.host.is_active(f"{c.name}.target"):
        raise VerifyError(f"cluster {c.name} is not running (tkctl start cluster {c.name})")
    if only is not None and only not in {n for n, _, _ in CHECKS}:
        raise PhaseFailed(f"unknown phase {only!r}; phases: {', '.join(n for n, _, _ in CHECKS)}")
    r = Report(ctx)
    ctx.say(f"verify {c.name} (cni {c.spec.cni}, {len(c.spec.nodes)} nodes, timeout {r.timeout}s)")
    r.kubectl("delete", "ns", NS, "--ignore-not-found", "--wait=true", "--timeout=60s")
    try:
        if r.kubectl("create", "ns", NS)[0] != 0:
            r.ng(f"create namespace {NS}")
            return r.failed
        wanted = None if only is None else {*PREREQUISITES.get(only, []), only}
        printed = ""
        for name, _, check in CHECKS:
            if wanted is not None and name not in wanted:
                continue
            section = _section_of(name)
            if section != printed:
                ctx.say("")
                ctx.say(f"[{section}]")
                printed = section
            check(r)
    finally:
        r.kubectl("delete", "ns", NS, "--ignore-not-found", "--wait=false")
        ctx.say("")
        ctx.say(f"PASS {r.passed}  FAIL {r.failed}  SKIP {r.skipped}")
    return r.failed
