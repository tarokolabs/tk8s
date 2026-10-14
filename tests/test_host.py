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


def test_read_bytes_and_remove(tmp_path: Path) -> None:
    h = Host(lambda _: None)
    p = tmp_path / "blob"
    p.write_bytes(b"\x00\x01")
    assert h.read_bytes(p) == b"\x00\x01" and h.read_bytes(tmp_path / "nope") is None
    h.remove(p)
    h.remove(p)  # a second remove is fine
    assert not p.exists()
    f = FakeHost()
    f.files[Path("/x")] = "ab"
    assert f.read_bytes(Path("/x")) == b"ab" and f.read_bytes(Path("/y")) is None
    f.remove(Path("/x"))
    assert Path("/x") not in f.files


def test_fake_host_tracks_units_systemctl_starts_and_stops() -> None:
    host = FakeHost()
    host.run(["systemctl", "enable", "--now", "--quiet", "demo.target"], sudo=True)
    assert host.is_active("demo.target")
    host.run(["systemctl", "stop", "demo.target", "demo-lb.service"], sudo=True)
    assert not host.is_active("demo.target")
    host.run(["systemctl", "start", "x.service"], sudo=True)
    assert host.is_active("x.service")
    host.run(["systemctl", "disable", "--now", "x.service"], sudo=True, check=False)
    assert not host.is_active("x.service")


def test_fake_host_tracks_podman_containers_volumes_and_networks() -> None:
    host = FakeHost(podman={"container:demo-cp-1", "volume:demo-cp-1-var", "network:demo"})
    ok = host.run(["podman", "container", "exists", "demo-cp-1"], sudo=True, check=False)
    missing = host.run(["podman", "volume", "exists", "nope"], sudo=True, check=False)
    assert (ok.returncode, missing.returncode) == (0, 1)
    host.run(["podman", "rm", "-f", "demo-cp-1"], sudo=True)
    host.run(["podman", "volume", "rm", "-f", "demo-cp-1-var"], sudo=True)
    host.run(["podman", "network", "rm", "-f", "demo"], sudo=True)
    assert host.podman == set()
    host.podman.add("container:x")
    host.responses[("podman", "rm", "-f", "x")] = Result(125, "", "in use")
    host.run(["podman", "rm", "-f", "x"], sudo=True, check=False)
    assert host.podman == {"container:x"}  # a failed rm removes nothing
    host.responses[("podman", "image", "exists")] = Result(0, "", "")
    assert host.run(["podman", "image", "exists", "i"], check=False).returncode == 0  # as before


def test_fake_host_rm_forgets_files_and_dirs() -> None:
    host = FakeHost(files={Path("/etc/x/a.container"): "a"}, dirs={Path("/etc/x"), Path("/opt/s")})
    host.run(["rm", "-f", "/etc/x/a.container"], sudo=True)
    host.run(["rm", "-rf", "/etc/x", "/opt/s"], sudo=True)
    assert not host.exists(Path("/etc/x/a.container")) and not host.exists(Path("/opt/s"))
