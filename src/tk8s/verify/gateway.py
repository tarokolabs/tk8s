"""[gateway-api]: GatewayClass, a Gateway with an LB-IPAM address, HTTPRoute, X-Forwarded-For
and TCPRoute reachable from the host."""

import time

from ..render import template
from .report import NS, Report


def check_gatewayclass(r: Report) -> None:
    rc, _ = r.kubectl("wait", "gatewayclass", "cilium", "--for=condition=Accepted", "--timeout=60s")
    if rc == 0:
        r.ok("GatewayClass cilium Accepted")
    else:
        r.ng("GatewayClass cilium", "not Accepted")


def check_gateway(r: Report) -> None:
    c = r.ctx.require_cluster()
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
    if not r.gateway_ip:
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
    if not r.gateway_ip:
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
    if c.multi_host:  # whatever the operator sends from towards the LB address (shim or LAN)
        route = r.ctx.host.run(
            ["ip", "-4", "route", "get", r.gateway_ip], capture=True, check=False
        ).stdout.split()
        host_ip = route[route.index("src") + 1] if "src" in route else "?"
    else:
        host_ip = str(c.spec.network.gateway)  # the host's address on the node network
    if xff == host_ip:
        r.ok(f"X-Forwarded-For carries the client IP ({xff})")
    else:
        r.ng("X-Forwarded-For", f"got '{xff}', expected {host_ip}")


def check_tcproute(r: Report) -> None:
    if not r.gateway_ip:
        return
    tcp = r.ctx.host.run(
        ["curl", "-s", "-m", "8", f"http://{r.gateway_ip}:9000/hostname"], capture=True, check=False
    ).stdout
    if tcp:
        r.ok(f"TCPRoute reachable from host (backend {tcp})")
    else:
        r.ng("TCPRoute from host", f"no response from {r.gateway_ip}:9000")
