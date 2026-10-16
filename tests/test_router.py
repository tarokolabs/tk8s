import pytest

from tests.fixtures import OP, SHOW_HAPROXY
from tk8s.create import router
from tk8s.host import FakeHost, Result
from tk8s.phases import Context, PhaseFailed


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
    host.responses[("podman", "exec", "-i", "demo-router", "/bin/vbash", "-s")] = Result(0, "", "")
    router.reload(ctx)
    call = next(c for c in host.calls if c.argv[:3] == ["podman", "exec", "-i"])
    assert call.sudo and call.input == (
        "cp /opt/vyatta/etc/config/tkctl.boot /opt/vyatta/etc/config/config.boot\n"
        "grep '^// ' /opt/vyatta/etc/config.boot.default >> /opt/vyatta/etc/config/config.boot\n"
        "source /opt/vyatta/etc/functions/script-template\n"
        "configure\n"
        "load /opt/vyatta/etc/config/config.boot\n"
        "commit\n"
        "exit\n"
    )


def test_reload_retries_while_the_boot_commit_holds_the_lock(ctx: Context, host: FakeHost) -> None:
    ctx.options.poll = 0
    host.responses[("podman", "exec", "-i", "demo-router", "/bin/vbash", "-s")] = [
        Result(
            0, "Configuration system temporarily locked due to another commit in progress\n", ""
        ),
        Result(0, "", ""),
    ]
    router.reload(ctx)
    assert sum(1 for c in host.calls if c.argv[:3] == ["podman", "exec", "-i"]) == 2


def test_reload_fails_by_name_when_commit_fails(ctx: Context, host: FakeHost) -> None:
    host.responses[("podman", "exec", "-i", "demo-router", "/bin/vbash", "-s")] = Result(
        1, "", "Commit failed"
    )
    with pytest.raises(PhaseFailed, match=r"demo-router did not accept config\.boot"):
        router.reload(ctx)
