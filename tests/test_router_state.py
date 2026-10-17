from tests.fixtures import argvs, vrrp_answers
from tk8s.create import router_state
from tk8s.host import FakeHost
from tk8s.phases import Context


def test_running_reads_the_container_state(two_router_ctx: Context, host: FakeHost) -> None:
    r1, r2 = two_router_ctx.require_cluster().routers
    host.podman.add("stopped:demo-router-2")
    assert router_state.running(two_router_ctx, r1)
    assert not router_state.running(two_router_ctx, r2)
    assert [
        "podman",
        "container",
        "inspect",
        "--format",
        "{{.State.Running}}",
        "demo-router-2",
    ] in argvs(host)


def test_vrrp_reads_the_state_column_of_show_vrrp(two_router_ctx: Context, host: FakeHost) -> None:
    r1, r2 = two_router_ctx.require_cluster().routers
    vrrp_answers(host, {"demo-router-1": "MASTER", "demo-router-2": None})
    assert router_state.vrrp(two_router_ctx, r1) == "MASTER"
    assert router_state.vrrp(two_router_ctx, r2) is None
