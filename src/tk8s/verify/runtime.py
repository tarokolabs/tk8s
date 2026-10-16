"""[runtimeclass]: a pod under each RuntimeClass, and the API reachable from a gVisor sandbox."""

from .report import NS, Report


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
