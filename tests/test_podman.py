"""podman.py: the argv every caller used to spell out, in one place."""

import pytest

from tk8s import podman
from tk8s.host import FakeHost, Result
from tk8s.phases import Context, PhaseFailed


def test_exists_asks_podman_and_reads_the_exit_code(ctx: Context, host: FakeHost) -> None:
    host.podman.add("container:demo-worker-1")
    assert podman.exists(ctx, "container", "demo-worker-1")
    assert not podman.exists(ctx, "volume", "demo-worker-1-var")
    host.responses[("podman", "image", "exists", "img:1")] = Result(0, "", "")
    assert podman.exists(ctx, "image", "img:1")
    a = [c.argv for c in host.calls]
    assert ["podman", "container", "exists", "demo-worker-1"] in a
    assert ["podman", "image", "exists", "img:1"] in a and all(c.sudo for c in host.calls)


def test_exec_runs_in_the_node_as_root_and_feeds_stdin_with_dash_i(
    ctx: Context, host: FakeHost
) -> None:
    host.responses[("podman", "exec", "n1", "cat", "/x")] = Result(0, "hi\n", "")
    r = podman.exec_(ctx, "n1", "cat", "/x")
    assert r.stdout == "hi\n" and host.calls[-1].argv == ["podman", "exec", "n1", "cat", "/x"]
    assert host.calls[-1].sudo and host.calls[-1].input is None
    podman.exec_(ctx, "n1", "tee", "/y", input="data")
    assert host.calls[-1].argv == ["podman", "exec", "-i", "n1", "tee", "/y"]
    assert host.calls[-1].input == "data"
    host.responses[("podman", "exec", "n1", "false")] = Result(1, "", "no")
    assert podman.exec_(ctx, "n1", "false").returncode == 1  # never raises


def test_node_has_read_and_active(ctx: Context, host: FakeHost) -> None:
    host.responses[("podman", "exec", "n1", "test", "-f", "/etc/a")] = Result(0, "", "")
    host.responses[("podman", "exec", "n1", "test", "-x", "/usr/bin/zstd")] = Result(1, "", "")
    assert podman.node_has(ctx, "n1", "/etc/a", "-f")
    assert not podman.node_has(ctx, "n1", "/usr/bin/zstd", "-x")
    assert host.calls[0].argv == ["podman", "exec", "n1", "test", "-f", "/etc/a"]
    host.responses[("podman", "exec", "n1", "cat", "/etc/a")] = Result(0, "v\n", "")
    host.responses[("podman", "exec", "n1", "cat", "/etc/b")] = Result(1, "", "No such file")
    assert podman.node_read(ctx, "n1", "/etc/a") == "v\n"
    assert podman.node_read(ctx, "n1", "/etc/b") is None
    host.responses[("podman", "exec", "n1", "systemctl", "is-active", "--quiet", "crio")] = Result(
        3, "", ""
    )
    assert not podman.node_active(ctx, "n1", "crio") and podman.node_active(ctx, "n1", "frr")


def test_must_raises_with_the_node_the_command_and_stderr(ctx: Context, host: FakeHost) -> None:
    host.responses[("podman", "exec", "n1", "sed")] = Result(1, "", "sed: no such file")
    with pytest.raises(PhaseFailed, match="n1: sed -i failed: sed: no such file"):
        podman.must(ctx, "n1", "sed", "-i", "s/a/b/", "/etc/x")
    assert podman.must(ctx, "n1", "true").returncode == 0
