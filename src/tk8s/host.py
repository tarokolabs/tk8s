"""The one place tk8s touches the host: run a command, write a file, look at a file or a unit.

Commands are argv lists, never shell strings. Every command is printed before it runs, in a form
that can be pasted back into a terminal, so a student can redo any step by hand.
"""

import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

CLEAN_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C"}
PASS_THROUGH = ("http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")
SSH_PASS_THROUGH = ("SSH_AUTH_SOCK", "HOME")  # the agent socket and ~/.ssh for the ssh client


def _base_env(*extra: str) -> dict[str, str]:
    """A clean environment: PATH and LANG fixed, only proxy settings (and `extra`) inherited."""
    keep = (*PASS_THROUGH, *extra)
    return CLEAN_ENV | {k: v for k, v in os.environ.items() if k in keep}


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str


class HostError(Exception):
    def __init__(self, argv: Sequence[str], returncode: int, stderr: str) -> None:
        tail = stderr.strip().splitlines()[-3:]
        super().__init__(f"{shlex.join(argv)} exited {returncode}: {' | '.join(tail)}")
        self.argv = list(argv)
        self.returncode = returncode


def _full_argv(argv: Sequence[str], sudo: bool, env: Mapping[str, str] | None) -> list[str]:
    """What actually runs, and what the `$` line shows: the environment a command needs rides on
    the command line (`env K=V cmd`), so the printed line is the executed one; under sudo it is
    also the only way, because sudo drops the caller's environment."""
    envs = [f"{k}={v}" for k, v in (env or {}).items()]
    full = [*(["env", *envs] if envs else []), *argv]
    return ["sudo", "-n", *full] if sudo else full


class Host:
    """A real host. `say` receives every line a student should see. `label` names a remote host
    (user@host): the `$` lines then show the ssh form a student would paste."""

    def __init__(
        self, say: Callable[[str], None], *, quiet: bool = False, label: str | None = None
    ) -> None:
        self._say = say
        self._quiet = quiet
        self.label = label

    def _show(self, argv: Sequence[str], sudo: bool, env: Mapping[str, str] | None) -> None:
        if self._quiet:
            return
        full = _full_argv(argv, sudo, env)
        if self.label is None:
            self._say("  $ " + shlex.join(full))
        else:  # the label is already in ssh's own words (user@host, -p port)
            self._say(f"  $ ssh {self.label} {shlex.quote(shlex.join(full))}")

    def run(
        self,
        argv: Sequence[str],
        *,
        sudo: bool = False,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
        capture: bool = False,
        check: bool = True,
    ) -> Result:
        self._show(argv, sudo, env)
        full = _full_argv(argv, sudo, env)
        try:
            p = subprocess.run(
                full,
                input=input,
                text=True,
                env=_base_env(),
                capture_output=capture,
                check=False,
            )
            r = Result(p.returncode, p.stdout or "", p.stderr or "")
        except FileNotFoundError:  # shell semantics: 127, a message, no traceback
            r = Result(127, "", f"{full[0]}: command not found")
        if check and r.returncode != 0:
            raise HostError(full, r.returncode, r.stderr)
        return r

    def run_bytes(
        self,
        argv: Sequence[str],
        *,
        sudo: bool = False,
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
    ) -> bytes:
        """run() for binary streams (image tarballs); stdout comes back as bytes, failures raise."""
        self._show(argv, sudo, env)
        full = _full_argv(argv, sudo, env)
        try:
            p = subprocess.run(full, input=input, env=_base_env(), capture_output=True, check=False)
        except FileNotFoundError:
            raise HostError(full, 127, f"{full[0]}: command not found") from None
        if p.returncode != 0:
            raise HostError(full, p.returncode, p.stderr.decode(errors="replace"))
        return p.stdout

    def write(self, path: Path, content: str, *, sudo: bool = False, mode: int = 0o644) -> None:
        if sudo:  # -D creates the parent directories (a cluster's unit directory)
            self.run(
                ["install", "-D", "-m", f"{mode:o}", "/dev/stdin", str(path)],
                sudo=True,
                input=content,
                capture=True,
            )
        else:  # atomic, from a private temp file: two writers never share a name, and a crash
            # mid-write never leaves half a file behind
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp_name, mode)
                os.replace(tmp_name, path)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise

    def read(self, path: Path, *, sudo: bool = False) -> str | None:
        if sudo:
            r = self.run(["cat", str(path)], sudo=True, capture=True, check=False)
            return r.stdout if r.returncode == 0 else None
        return path.read_text() if path.exists() else None

    def read_bytes(self, path: Path) -> bytes | None:
        return path.read_bytes() if path.exists() else None

    def remove(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def exists(self, path: Path) -> bool:
        return path.exists()

    def listdir(self, path: Path) -> list[str]:
        """Entry names of a directory on the host (empty when it is not there)."""
        return sorted(p.name for p in path.iterdir()) if path.is_dir() else []

    def writable(self, path: Path) -> bool:
        return os.access(path, os.W_OK)

    def mkdir(self, path: Path, *, sudo: bool = False, owner: str | None = None) -> None:
        if sudo:
            own = ["-o", owner.split(":")[0], "-g", owner.split(":")[1]] if owner else []
            self.run(["install", "-d", *own, str(path)], sudo=True, capture=True)
        else:
            path.mkdir(parents=True, exist_ok=True)

    def is_active(self, unit: str) -> bool:
        return self.run(["systemctl", "is-active", "--quiet", unit], check=False).returncode == 0

    def symlink(self, path: Path, target: Path) -> None:
        """path -> target, created or replaced (ln -sfn); parents created."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            if os.readlink(path) == str(target):
                return
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        os.symlink(target, path)

    def readlink(self, path: Path) -> Path | None:
        """The target of a symlink, None when path is not one."""
        return Path(os.readlink(path)) if path.is_symlink() else None


# ---- a host reached over ssh


@dataclass(frozen=True)
class SshTarget:
    """[user@]host[:port] as cluster.yaml spells an ssh host."""

    user: str | None
    host: str
    port: int | None

    def __str__(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    @property
    def label(self) -> str:
        """What the `$ ssh` line shows: the port option too, so the line can be pasted."""
        return f"-p {self.port} {self}" if self.port else str(self)


def parse_ssh(text: str) -> SshTarget:
    m = re.fullmatch(r"(?:([^@\s:]+)@)?([^@\s:]+)(?::([0-9]+))?", text)
    if m is None:
        raise ValueError(f"not an ssh target ([user@]host[:port]): {text!r}")
    user, host, port = m.groups()
    return SshTarget(user, host, int(port) if port else None)


def default_control_dir() -> Path:
    """Where the ControlMaster sockets go: the user's runtime directory (XDG), or, where there
    is none to create it in, a private directory under the temp dir (XDG's own fallback)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    for d in (Path(runtime) / "tk8s", Path(tempfile.gettempdir()) / f"tk8s-{os.getuid()}"):
        if _private_dir(d):
            return d
    raise HostError(["mkdir", str(runtime)], 1, "no private directory for ssh control sockets")


def _private_dir(d: Path) -> bool:
    """d exists (created if needed) as a real directory of ours that nobody else can enter: a
    socket directory someone else planted (a symlink, a world-writable one) would let them
    hijack the ssh connections."""
    try:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = os.lstat(d)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and (st.st_mode & 0o077) == 0


class SshHost(Host):
    """A host reached with OpenSSH: the same commands and files, each command one ssh call.
    BatchMode: no prompts, an untrusted host key or a refused key fails at once (255) instead
    of hanging; ControlMaster: a create runs hundreds of commands per host, one connection
    carries them all."""

    def __init__(
        self,
        say: Callable[[str], None],
        target: SshTarget,
        *,
        quiet: bool = False,
        ssh: str = "ssh",
        control_dir: Path | None = None,
    ) -> None:
        super().__init__(say, quiet=quiet, label=target.label)
        self.target = target
        self._ssh = ssh
        self._control_dir = control_dir or default_control_dir()
        self._control_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def remote_argv(
        self, argv: Sequence[str], sudo: bool, env: Mapping[str, str] | None
    ) -> list[str]:
        """The ssh command line: options, the target, the remote command as one word."""
        port = ["-p", str(self.target.port)] if self.target.port else []
        return [
            self._ssh,
            "-o",
            "BatchMode=yes",
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPersist=60",
            "-o",
            f"ControlPath={self._control_dir}/ssh-%C",
            *port,
            str(self.target),
            shlex.join(_full_argv(argv, sudo, env)),
        ]

    def run(
        self,
        argv: Sequence[str],
        *,
        sudo: bool = False,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
        capture: bool = False,
        check: bool = True,
    ) -> Result:
        self._show(argv, sudo, env)
        full = self.remote_argv(argv, sudo, env)
        try:
            p = subprocess.run(
                full,
                input=input,
                stdin=subprocess.DEVNULL if input is None else None,  # never tkctl's own stdin
                text=True,
                env=_base_env(*SSH_PASS_THROUGH),
                capture_output=capture,
                check=False,
            )
            r = Result(p.returncode, p.stdout or "", p.stderr or "")
        except FileNotFoundError:
            r = Result(127, "", f"{full[0]}: command not found")
        if r.returncode == 255:
            # ssh's own failure (no route, host key, auth), not the command's: an unreachable
            # host must never read as "nothing there"
            raise HostError(full, 255, r.stderr or "ssh failed")
        if check and r.returncode != 0:
            raise HostError(full, r.returncode, r.stderr)
        return r

    def run_bytes(
        self,
        argv: Sequence[str],
        *,
        sudo: bool = False,
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
    ) -> bytes:
        self._show(argv, sudo, env)
        full = self.remote_argv(argv, sudo, env)
        try:
            p = subprocess.run(
                full,
                input=input,
                stdin=subprocess.DEVNULL if input is None else None,
                env=_base_env(*SSH_PASS_THROUGH),
                capture_output=True,
                check=False,
            )
        except FileNotFoundError:
            raise HostError(full, 127, f"{full[0]}: command not found") from None
        if p.returncode != 0:
            raise HostError(full, p.returncode, p.stderr.decode(errors="replace"))
        return p.stdout

    def write(self, path: Path, content: str, *, sudo: bool = False, mode: int = 0o644) -> None:
        self.run(
            ["install", "-D", "-m", f"{mode:o}", "/dev/stdin", str(path)],
            sudo=sudo,
            input=content,
            capture=True,
        )

    def read(self, path: Path, *, sudo: bool = False) -> str | None:
        r = self.run(["cat", str(path)], sudo=sudo, capture=True, check=False)
        return r.stdout if r.returncode == 0 else None

    def read_bytes(self, path: Path) -> bytes | None:
        try:
            return self.run_bytes(["cat", str(path)])
        except HostError:
            return None

    def remove(self, path: Path) -> None:
        self.run(["rm", "-f", str(path)], capture=True)

    def exists(self, path: Path) -> bool:
        return self.run(["test", "-e", str(path)], check=False).returncode == 0

    def listdir(self, path: Path) -> list[str]:
        r = self.run(["ls", "-1A", str(path)], capture=True, check=False)
        return sorted(r.stdout.split()) if r.returncode == 0 else []

    def writable(self, path: Path) -> bool:
        return self.run(["test", "-w", str(path)], check=False).returncode == 0

    def mkdir(self, path: Path, *, sudo: bool = False, owner: str | None = None) -> None:
        if sudo:
            own = ["-o", owner.split(":")[0], "-g", owner.split(":")[1]] if owner else []
            self.run(["install", "-d", *own, str(path)], sudo=True, capture=True)
        else:
            self.run(["mkdir", "-p", str(path)], capture=True)

    def symlink(self, path: Path, target: Path) -> None:
        if self.readlink(path) == target:
            return
        self.run(["mkdir", "-p", str(path.parent)], capture=True)
        self.run(["rm", "-rf", str(path)], capture=True)
        self.run(["ln", "-s", str(target), str(path)], capture=True)

    def readlink(self, path: Path) -> Path | None:
        r = self.run(["readlink", str(path)], capture=True, check=False)
        return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


class DryRunHost(Host):
    """Prints what would happen: commands as `$` lines, files as `# ---- path` + content (the
    golden format). Questions about the host answer "nothing there"."""

    def __init__(
        self, say: Callable[[str], None], *, quiet: bool = False, label: str | None = None
    ) -> None:
        super().__init__(say, quiet=quiet, label=label)

    def run(
        self,
        argv: Sequence[str],
        *,
        sudo: bool = False,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
        capture: bool = False,
        check: bool = True,
    ) -> Result:
        self._show(argv, sudo, env)
        return Result(0, "", "")

    def run_bytes(
        self,
        argv: Sequence[str],
        *,
        sudo: bool = False,
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
    ) -> bytes:
        self._show(argv, sudo, env)
        return b""

    def write(self, path: Path, content: str, *, sudo: bool = False, mode: int = 0o644) -> None:
        where = path if self.label is None else f"{self.label}:{path}"
        self._say(f"# ---- {where}")
        self._say(content.rstrip("\n"))
        self._say("")

    def read(self, path: Path, *, sudo: bool = False) -> str | None:
        return None

    def read_bytes(self, path: Path) -> bytes | None:
        return None

    def remove(self, path: Path) -> None:
        return None

    def exists(self, path: Path) -> bool:
        return False

    def listdir(self, path: Path) -> list[str]:
        return []

    def writable(self, path: Path) -> bool:
        return True

    def mkdir(self, path: Path, *, sudo: bool = False, owner: str | None = None) -> None:
        return None

    def is_active(self, unit: str) -> bool:
        return False

    def symlink(self, path: Path, target: Path) -> None:
        self.run(["ln", "-sfn", str(target), str(path)])

    def readlink(self, path: Path) -> Path | None:
        return None


@dataclass
class Call:
    argv: list[str]
    sudo: bool
    env: dict[str, str]
    input: str | None = None


@dataclass
class FakeHost(Host):
    """For tests: records calls, answers from a table, keeps files in memory.

    `responses` maps an argv prefix to a Result, an exception to raise, or a list consumed one per
    call (a pull that fails once, then succeeds; the last entry repeats). The first matching prefix
    wins, so put specific prefixes before general ones. Unlisted commands succeed with no output.
    """

    calls: list[Call] = field(default_factory=list)
    responses: dict[tuple[str, ...], object] = field(default_factory=dict)
    files: dict[Path, str] = field(default_factory=dict)
    dirs: set[Path] = field(default_factory=set)
    unwritable: set[Path] = field(default_factory=set)
    active: set[str] = field(default_factory=set)
    # "container:NAME", "volume:NAME", "network:NAME" exist; "stopped:NAME" makes
    # `podman container inspect --format {{.State.Running}} NAME` answer false (default true)
    podman: set[str] = field(default_factory=set)
    lines: list[str] = field(default_factory=list)
    links: dict[Path, Path] = field(default_factory=dict)  # symlink path -> target
    label: str | None = None  # a remote host: `$` lines take the ssh form

    def __post_init__(self) -> None:
        super().__init__(self.lines.append, label=self.label)

    def _answer(self, argv: Sequence[str]) -> object:
        for prefix, a in self.responses.items():
            if tuple(argv[: len(prefix)]) == prefix:
                if isinstance(a, list):
                    return a.pop(0) if len(a) > 1 else a[0]
                return a
        a = list(argv)
        kinds = ("container", "volume", "network")
        if a[:1] == ["podman"] and len(a) == 4 and a[1] in kinds and a[2] == "exists":
            return Result(0 if f"{a[1]}:{a[3]}" in self.podman else 1, "", "")
        if a[:5] == ["podman", "container", "inspect", "--format", "{{.State.Running}}"]:
            return Result(0, "false\n" if f"stopped:{a[5]}" in self.podman else "true\n", "")
        return Result(0, "", "")

    def _track(self, argv: Sequence[str]) -> None:
        """What a successful command changes on this host: units started and stopped, podman
        objects and files removed. Enough for done() to turn true after run()."""
        a = list(argv)
        if a[:1] == ["systemctl"] and len(a) > 2:
            units = {u for u in a[2:] if not u.startswith("-")}
            if a[1] in ("start", "enable"):
                self.active |= units
            elif a[1] in ("stop", "disable"):
                self.active -= units
        elif a[:3] == ["podman", "rm", "-f"]:
            self.podman.discard(f"container:{a[3]}")
        elif a[:4] in (["podman", "volume", "rm", "-f"], ["podman", "network", "rm", "-f"]):
            self.podman.discard(f"{a[1]}:{a[4]}")
        elif a[:2] in (["rm", "-f"], ["rm", "-rf"]):
            for path in map(Path, a[2:]):
                self.files.pop(path, None)
                self.dirs.discard(path)

    def run(
        self,
        argv: Sequence[str],
        *,
        sudo: bool = False,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
        capture: bool = False,
        check: bool = True,
    ) -> Result:
        self._show(argv, sudo, env)
        self.calls.append(Call(list(argv), sudo, dict(env or {}), input))
        answer = self._answer(argv)
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, Result)
        if answer.returncode == 0:
            self._track(argv)
        if check and answer.returncode != 0:
            raise HostError(_full_argv(argv, sudo, env), answer.returncode, answer.stderr)
        return answer

    def run_bytes(
        self,
        argv: Sequence[str],
        *,
        sudo: bool = False,
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
    ) -> bytes:
        text = input.decode(errors="replace") if input is not None else None
        return self.run(argv, sudo=sudo, env=env, input=text, capture=True).stdout.encode()

    def write(self, path: Path, content: str, *, sudo: bool = False, mode: int = 0o644) -> None:
        self.calls.append(Call(["write", str(path), f"mode={mode:o}"], sudo, {}, content))
        if sudo:
            self.calls.append(
                Call(
                    ["install", "-D", "-m", f"{mode:o}", "/dev/stdin", str(path)], True, {}, content
                )
            )
        self.files[path] = content

    def read(self, path: Path, *, sudo: bool = False) -> str | None:
        return self.files.get(path)

    def read_bytes(self, path: Path) -> bytes | None:
        text = self.files.get(path)
        return text.encode() if text is not None else None

    def remove(self, path: Path) -> None:
        self.calls.append(Call(["rm", "-f", str(path)], False, {}))
        self.files.pop(path, None)
        self.links.pop(path, None)

    def exists(self, path: Path) -> bool:
        return path in self.files or path in self.dirs

    def listdir(self, path: Path) -> list[str]:
        return sorted({p.name for p in (*self.files, *self.dirs, *self.links) if p.parent == path})

    def writable(self, path: Path) -> bool:
        return path not in self.unwritable

    def mkdir(self, path: Path, *, sudo: bool = False, owner: str | None = None) -> None:
        extra = [f"owner={owner}"] if owner else []
        self.calls.append(Call(["mkdir", str(path), *extra], sudo, {}))
        self.dirs.add(path)

    def is_active(self, unit: str) -> bool:
        return unit in self.active

    def symlink(self, path: Path, target: Path) -> None:
        self.calls.append(Call(["ln", "-sfn", str(target), str(path)], False, {}))
        self.links[path] = target

    def readlink(self, path: Path) -> Path | None:
        return self.links.get(path)
