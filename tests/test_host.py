import os
from pathlib import Path

import pytest

from tk8s.host import (
    DryRunHost,
    FakeHost,
    Host,
    HostError,
    Result,
    SshHost,
    SshTarget,
    default_control_dir,
    parse_ssh,
)


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
    host = FakeHost(
        podman={"container:demo-control-plane-1", "volume:demo-control-plane-1-var", "network:demo"}
    )
    ok = host.run(["podman", "container", "exists", "demo-control-plane-1"], sudo=True, check=False)
    missing = host.run(["podman", "volume", "exists", "nope"], sudo=True, check=False)
    assert (ok.returncode, missing.returncode) == (0, 1)
    host.run(["podman", "rm", "-f", "demo-control-plane-1"], sudo=True)
    host.run(["podman", "volume", "rm", "-f", "demo-control-plane-1-var"], sudo=True)
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


def test_write_keeps_the_old_file_when_the_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Host(lambda _: None)
    p = tmp_path / "cluster.yaml"
    p.write_text("old: file\n")

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        h.write(p, "new: file\n")
    assert p.read_text() == "old: file\n"
    assert list(tmp_path.iterdir()) == [p]  # the temp file is gone too


def test_symlink_creates_replaces_and_reads_back(tmp_path: Path) -> None:
    h = Host(print)
    link = tmp_path / "a" / "cpu0"
    h.symlink(link, Path("/sys/devices/system/cpu/cpu0"))
    assert h.readlink(link) == Path("/sys/devices/system/cpu/cpu0")
    h.symlink(link, Path("/sys/devices/system/cpu/cpu1"))
    assert h.readlink(link) == Path("/sys/devices/system/cpu/cpu1")
    assert h.readlink(tmp_path / "a") is None and h.readlink(tmp_path / "missing") is None


def test_fake_host_records_symlinks_and_answers_readlink() -> None:
    h = FakeHost()
    h.symlink(Path("/x/cpu0"), Path("/sys/devices/system/cpu/cpu0"))
    assert h.readlink(Path("/x/cpu0")) == Path("/sys/devices/system/cpu/cpu0")
    assert h.readlink(Path("/x/other")) is None
    assert h.calls[-1].argv == ["ln", "-sfn", "/sys/devices/system/cpu/cpu0", "/x/cpu0"]


def test_dry_run_host_prints_the_ln_command() -> None:
    out: list[str] = []
    h = DryRunHost(out.append)
    h.symlink(Path("/x/cpu0"), Path("/sys/devices/system/cpu/cpu0"))
    assert out == ["  $ ln -sfn /sys/devices/system/cpu/cpu0 /x/cpu0"]
    assert h.readlink(Path("/x/cpu0")) is None


def test_symlink_replaces_a_regular_file_in_its_way(tmp_path: Path) -> None:
    h = Host(print)
    link = tmp_path / "cpu0"
    link.write_text("not a link")
    h.symlink(link, Path("/sys/devices/system/cpu/cpu0"))
    assert h.readlink(link) == Path("/sys/devices/system/cpu/cpu0")


def test_fake_host_listdir_and_remove_see_symlinks() -> None:
    h = FakeHost()
    h.symlink(Path("/x/node0/cpu0"), Path("/sys/devices/system/cpu/cpu0"))
    h.files[Path("/x/node0/cpulist")] = "0\n"
    assert h.listdir(Path("/x/node0")) == ["cpu0", "cpulist"]
    h.remove(Path("/x/node0/cpu0"))
    assert h.readlink(Path("/x/node0/cpu0")) is None


# ---- SshHost


def stub_ssh(tmp_path: Path, body: str) -> Path:
    """A fake ssh: records its argv and stdin under tmp_path, then runs `body`."""
    stub = tmp_path / "ssh"
    stub.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {tmp_path / 'argv'}\n"
        f"cat > {tmp_path / 'stdin'}\n"
        f"{body}\n"
    )
    stub.chmod(0o755)
    return stub


def test_parse_ssh_accepts_user_host_port_and_refuses_garbage() -> None:
    assert parse_ssh("tux@192.168.1.231:2222") == SshTarget("tux", "192.168.1.231", 2222)
    assert parse_ssh("pve-b") == SshTarget(None, "pve-b", None)
    assert str(parse_ssh("tux@h")) == "tux@h" and str(parse_ssh("h:22")) == "h"
    for bad in ("", "a b", "h:x", "@h", "u@"):
        with pytest.raises(ValueError):
            parse_ssh(bad)


def test_ssh_host_prints_a_pasteable_ssh_line_and_runs_through_a_stub(tmp_path: Path) -> None:
    stub = stub_ssh(tmp_path, "echo out")
    lines: list[str] = []
    h = SshHost(lines.append, parse_ssh("tux@h:2222"), ssh=str(stub), control_dir=tmp_path / "cm")
    r = h.run(["podman", "ps"], sudo=True, capture=True)
    assert r == Result(0, "out\n", "")
    assert lines == ["  $ ssh tux@h 'sudo -n podman ps'"]
    argv = (tmp_path / "argv").read_text().splitlines()
    assert argv[:2] == ["-o", "BatchMode=yes"]
    assert argv[argv.index("-p") + 1] == "2222"
    assert argv[-2:] == ["tux@h", "sudo -n podman ps"]
    assert f"ControlPath={tmp_path / 'cm'}/ssh-%C" in argv
    assert (tmp_path / "cm").stat().st_mode & 0o777 == 0o700


def test_ssh_host_write_goes_through_install_from_stdin(tmp_path: Path) -> None:
    stub = stub_ssh(tmp_path, "")
    h = SshHost(lambda _: None, parse_ssh("h"), ssh=str(stub), control_dir=tmp_path / "cm")
    h.write(Path("/x/y"), "hi\n", mode=0o600)
    argv = (tmp_path / "argv").read_text().splitlines()
    assert argv[-2:] == ["h", "install -D -m 600 /dev/stdin /x/y"]
    assert (tmp_path / "stdin").read_text() == "hi\n"
    h.write(Path("/etc/z"), "v", sudo=True)
    assert (tmp_path / "argv").read_text().splitlines()[
        -1
    ] == "sudo -n install -D -m 644 /dev/stdin /etc/z"


def test_ssh_host_questions_map_to_test_cat_ls_and_readlink(tmp_path: Path) -> None:
    stub = stub_ssh(
        tmp_path, 'case "$*" in *"ls -1A"*) echo b; echo a;; *readlink*) echo /t;; *) echo c;; esac'
    )
    h = SshHost(lambda _: None, parse_ssh("h"), ssh=str(stub), control_dir=tmp_path / "cm")
    assert h.read(Path("/f")) == "c\n" and h.exists(Path("/f"))
    assert h.listdir(Path("/d")) == ["a", "b"]
    assert h.readlink(Path("/l")) == Path("/t")
    assert (tmp_path / "argv").read_text().splitlines()[-1] == "readlink /l"
    h.symlink(Path("/d/l"), Path("/t"))  # readlink already answers /t: nothing more is run
    assert (tmp_path / "argv").read_text().splitlines()[-1] == "readlink /d/l"
    h.mkdir(Path("/d/e"), sudo=True, owner="1000:1000")
    assert (tmp_path / "argv").read_text().splitlines()[
        -1
    ] == "sudo -n install -d -o 1000 -g 1000 /d/e"


def test_ssh_failure_is_a_host_error_with_the_stderr(tmp_path: Path) -> None:
    stub = stub_ssh(tmp_path, 'echo "Host key verification failed." >&2; exit 255')
    h = SshHost(lambda _: None, parse_ssh("h"), ssh=str(stub), control_dir=tmp_path / "cm")
    with pytest.raises(HostError, match="exited 255: Host key verification failed") as e:
        h.run(["true"], capture=True)
    assert e.value.returncode == 255
    assert h.read(Path("/f")) is None and not h.exists(Path("/f")) and h.listdir(Path("/d")) == []


def test_fake_and_dry_run_hosts_with_a_label_print_ssh_lines() -> None:
    f = FakeHost(label="tux@h")
    f.run(["true"])
    assert f.lines == ["  $ ssh tux@h true"]
    lines: list[str] = []
    d = DryRunHost(lines.append, label="tux@h")
    d.run(["podman", "ps"], sudo=True)
    d.write(Path("/etc/x"), "v\n")
    assert lines == ["  $ ssh tux@h 'sudo -n podman ps'", "# ---- tux@h:/etc/x", "v", ""]


def test_control_dir_refuses_a_planted_or_shared_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert default_control_dir() == tmp_path / "run" / "tk8s"
    (tmp_path / "run" / "tk8s").chmod(0o755)  # someone opened it up: not ours to use any more
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path / "tmp"))
    assert default_control_dir() == tmp_path / "tmp" / f"tk8s-{os.getuid()}"
    (tmp_path / "tmp" / f"tk8s-{os.getuid()}").rmdir()
    (tmp_path / "tmp" / f"tk8s-{os.getuid()}").symlink_to(tmp_path)  # a planted symlink
    with pytest.raises(HostError, match="no private directory"):
        default_control_dir()
