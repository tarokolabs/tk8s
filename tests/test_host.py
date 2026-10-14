from pathlib import Path

import pytest

from tk8s.host import DryRunHost, FakeHost, Host, HostError, Result


def test_run_prints_a_pasteable_line_before_running() -> None:
    lines: list[str] = []
    h = Host(lines.append)
    r = h.run(["sh", "-c", "echo hi; echo err >&2"], capture=True)
    assert r == Result(0, "hi\n", "err\n")
    assert lines == ["  $ sh -c 'echo hi; echo err >&2'"]


def test_run_uses_a_clean_environment_plus_what_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUBECONFIG", "/leak")
    h = Host(lambda _: None)
    assert h.run(["sh", "-c", "echo ${KUBECONFIG:-unset}"], capture=True).stdout == "unset\n"
    assert h.run(["sh", "-c", "echo $X"], env={"X": "1"}, capture=True).stdout == "1\n"


def test_sudo_and_env_ride_on_the_command_line() -> None:
    lines: list[str] = []
    h = DryRunHost(lines.append)
    h.run(["podman", "pull", "-q", "img"], sudo=True, env={"CONTAINERS_STORAGE_CONF": "/c.conf"})
    assert lines == ["  $ sudo -n env CONTAINERS_STORAGE_CONF=/c.conf podman pull -q img"]


def test_failure_raises_with_the_command_and_stderr_tail() -> None:
    h = Host(lambda _: None)
    with pytest.raises(HostError) as e:
        h.run(["sh", "-c", "echo one >&2; echo two >&2; exit 3"], capture=True)
    assert e.value.returncode == 3 and "exited 3: one | two" in str(e.value)
    assert h.run(["false"], check=False).returncode == 1


def test_quiet_hides_commands_not_phases() -> None:
    lines: list[str] = []
    h = DryRunHost(lines.append, quiet=True)
    h.run(["true"])
    assert lines == []


def test_write_without_sudo_creates_parents_and_sets_mode(tmp_path: Path) -> None:
    h = Host(lambda _: None)
    p = tmp_path / "a" / "b.conf"
    h.write(p, "x\n", mode=0o600)
    assert p.read_text() == "x\n" and (p.stat().st_mode & 0o777) == 0o600
    assert not (tmp_path / "a" / "b.conf.tmp").exists()  # written atomically, tmp renamed away
    assert h.read(p) == "x\n" and h.read(tmp_path / "nope") is None
    assert h.exists(p) and not h.exists(tmp_path / "nope")


def test_dry_run_write_prints_the_golden_block_and_knows_nothing() -> None:
    lines: list[str] = []
    h = DryRunHost(lines.append)
    h.write(Path("/etc/x.conf"), "a\nb\n", sudo=True)
    assert lines == ["# ---- /etc/x.conf", "a\nb", ""]
    assert h.read(Path("/etc/x.conf")) is None and not h.exists(Path("/etc/x.conf"))
    assert not h.is_active("x.target")


def test_fake_host_answers_from_the_table_in_order() -> None:
    h = FakeHost()
    h.responses[("podman", "pull", "-q", "img")] = [Result(125, "", "tls reset"), Result(0, "", "")]
    assert h.run(["podman", "pull", "-q", "img"], check=False).returncode == 125
    assert h.run(["podman", "pull", "-q", "img"]).returncode == 0
    assert h.run(["podman", "pull", "-q", "img"]).returncode == 0  # the last answer repeats
    with pytest.raises(HostError):
        h.responses[("false",)] = Result(1, "", "no")
        h.run(["false"])
    assert [c.argv for c in h.calls][:2] == [["podman", "pull", "-q", "img"]] * 2
    h.write(Path("/x"), "v")
    assert h.read(Path("/x")) == "v" and h.exists(Path("/x"))


def test_binary_run_passes_bytes_through() -> None:
    h = Host(lambda _: None)
    r = h.run_bytes(["cat"], input=b"\x00\xff")
    assert r == b"\x00\xff"


def test_missing_binary_is_exit_127_not_a_traceback() -> None:
    h = Host(lambda _: None)
    r = h.run(["definitely-not-a-command-xyz"], check=False, capture=True)
    assert r.returncode == 127 and "command not found" in r.stderr
    with pytest.raises(HostError, match="exited 127"):
        h.run(["definitely-not-a-command-xyz"])
    with pytest.raises(HostError, match="exited 127"):
        h.run_bytes(["definitely-not-a-command-xyz"])


def test_env_is_shown_for_non_sudo_commands_too() -> None:
    lines: list[str] = []
    h = DryRunHost(lines.append)
    h.run(["cilium", "install", "--dry-run"], env={"KUBECONFIG": "/nonexistent", "HOME": "/tmp"})
    assert lines == ["  $ env KUBECONFIG=/nonexistent HOME=/tmp cilium install --dry-run"]


def test_proxy_variables_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("https_proxy", "http://proxy:3128")
    monkeypatch.setenv("HOME", "/leak")
    h = Host(lambda _: None)
    out = h.run(["sh", "-c", "echo ${https_proxy}:${HOME:-unset}"], capture=True).stdout
    assert out == "http://proxy:3128:unset\n"


def test_write_uses_a_unique_temp_name_and_install_D(tmp_path: Path) -> None:
    h = Host(lambda _: None)
    p = tmp_path / "c.conf"
    h.write(p, "one\n")
    h.write(p, "two\n")
    assert p.read_text() == "two\n" and list(tmp_path.iterdir()) == [p]
    lines: list[str] = []
    f = FakeHost()
    f.write(Path("/etc/x/y.conf"), "v", sudo=True)
    assert f.calls[-1].argv[:3] == ["install", "-D", "-m"]
    d = DryRunHost(lines.append)
    d.write(Path("/etc/x/y.conf"), "v", sudo=True)
    assert lines[0] == "# ---- /etc/x/y.conf"
