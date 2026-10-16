import pytest

from tests.fixtures import OP, SHOW_HAPROXY
from tk8s.create import router
from tk8s.host import FakeHost, Result
from tk8s.phases import Context, PhaseFailed


def booted(host: FakeHost, haproxy: str = SHOW_HAPROXY) -> None:
    """VyOS finished its boot-time load and lists the demo control plane."""
    host.responses[("podman", "exec", "demo-router", "cat", "/tmp/vyos-config-status")] = Result(
        0, "0\n", ""
    )
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(
        0, haproxy, ""
    )


def test_backends_reads_the_server_rows_of_the_api_backend(ctx: Context, host: FakeHost) -> None:
    two = SHOW_HAPROXY + "api                   demo-control-plane-2  DOWN       0 ms         4s\n"
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(0, two, "")
    assert router.backends(ctx) == [
        ("demo-control-plane-1", "UP"),
        ("demo-control-plane-2", "DOWN"),
    ]


def test_backends_is_empty_before_vyos_answers(ctx: Context, host: FakeHost) -> None:
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(1, "", "")
    assert router.backends(ctx) == []


def test_wait_needs_the_boot_marker_and_every_control_plane_listed(
    ctx: Context, host: FakeHost
) -> None:
    host.responses[("podman", "exec", "demo-router", "cat", "/tmp/vyos-config-status")] = [
        Result(1, "", "No such file"),
        Result(0, "0\n", ""),
    ]
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(
        0, SHOW_HAPROXY, ""
    )
    ctx.options.poll = 0
    router.wait(ctx)
    assert "  demo-router: VyOS up, 1 control plane backend(s) at 172.22.0.253" in host.lines


def test_wait_names_the_router_logs_when_vyos_never_finishes(ctx: Context, host: FakeHost) -> None:
    ctx.options.poll = 0
    host.responses[("podman", "exec", "demo-router", "cat", "/tmp/vyos-config-status")] = Result(
        1, "", ""
    )
    with pytest.raises(PhaseFailed, match="demo-router did not finish booting within 60 s"):
        router.wait(ctx)


def test_reload_feeds_vyos_a_load_and_commit_script(ctx: Context, host: FakeHost) -> None:
    booted(host)
    host.responses[("podman", "exec", "-i", "demo-router", "/bin/vbash", "-s")] = Result(0, "", "")
    router.reload(ctx, ctx.require_cluster())
    call = next(c for c in host.calls if c.argv[:3] == ["podman", "exec", "-i"])
    assert call.sudo and call.input == (
        "cp /opt/vyatta/etc/config/tkctl.boot /opt/vyatta/etc/config/config.boot\n"
        "grep '^// ' /opt/vyatta/etc/config.boot.default >> /opt/vyatta/etc/config/config.boot\n"
        "source /opt/vyatta/etc/functions/script-template\n"
        "configure\n"
        "load /opt/vyatta/etc/config/config.boot\n"
        "loaded=$?\n"
        "commit\n"
        "exit\n"
        "builtin exit $loaded\n"
    )


def test_reload_waits_for_the_boot_commit_to_finish_first(ctx: Context, host: FakeHost) -> None:
    """A load alongside the boot-time commit fails with exit status 0 ("Failed to generate
    committed config"): the status file says when that commit is over."""
    ctx.options.poll = 0
    booted(host)
    host.responses[("podman", "exec", "demo-router", "cat", "/tmp/vyos-config-status")] = [
        Result(1, "", "No such file"),
        Result(0, "0\n", ""),
    ]
    host.responses[("podman", "exec", "-i", "demo-router", "/bin/vbash", "-s")] = Result(0, "", "")
    router.reload(ctx, ctx.require_cluster())
    a = [c.argv for c in host.calls]
    status = [i for i, x in enumerate(a) if x[-1:] == ["/tmp/vyos-config-status"]]
    assert len(status) == 2 and status[-1] < a.index(
        ["podman", "exec", "-i", "demo-router", "/bin/vbash", "-s"]
    )


def test_reload_fails_when_vyos_says_commit_failed_with_exit_0(
    ctx: Context, host: FakeHost
) -> None:
    booted(host)
    host.responses[("podman", "exec", "-i", "demo-router", "/bin/vbash", "-s")] = Result(
        0, "[[system conntrack]] failed\nCommit failed\n", ""
    )
    with pytest.raises(
        PhaseFailed, match=r"demo-router did not accept config\.boot: (?s:.*)Commit failed"
    ):
        router.reload(ctx, ctx.require_cluster())


def test_reload_fails_when_the_backends_do_not_follow(ctx: Context, host: FakeHost) -> None:
    """Neither load nor commit has a usable exit status in every case: the backend list is the
    proof the change took."""
    ctx.options.poll = 0
    stale = "api                   demo-control-plane-9  DOWN       0 ms      4s\n"
    booted(host, SHOW_HAPROXY + stale)
    host.responses[("podman", "exec", "-i", "demo-router", "/bin/vbash", "-s")] = Result(0, "", "")
    with pytest.raises(
        PhaseFailed,
        match=r"demo-router lists demo-control-plane-1, demo-control-plane-9 as backends, "
        r"cluster\.yaml has demo-control-plane-1 \(podman exec demo-router .* show haproxy\)",
    ):
        router.reload(ctx, ctx.require_cluster())


def test_synced_compares_backend_names_with_the_control_planes(
    ctx: Context, host: FakeHost
) -> None:
    c = ctx.require_cluster()
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(
        0, SHOW_HAPROXY, ""
    )
    assert router.synced(ctx, c)
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(1, "", "")
    assert not router.synced(ctx, c)


def test_reload_fails_by_name_when_commit_fails(ctx: Context, host: FakeHost) -> None:
    booted(host)
    host.responses[("podman", "exec", "-i", "demo-router", "/bin/vbash", "-s")] = Result(
        1, "", "Commit failed"
    )
    with pytest.raises(PhaseFailed, match=r"demo-router did not accept config\.boot"):
        router.reload(ctx, ctx.require_cluster())
