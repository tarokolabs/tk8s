"""The one place tk8s touches the host: run a command, write a file, look at a file or a unit.

Commands are argv lists, never shell strings. Every command is printed before it runs, in a form
that can be pasted back into a terminal, so a student can redo any step by hand.
"""

import os
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

CLEAN_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C"}
PASS_THROUGH = ("http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")


def _base_env() -> dict[str, str]:
    """A clean environment: PATH and LANG fixed, only proxy settings inherited."""
    return CLEAN_ENV | {k: v for k, v in os.environ.items() if k in PASS_THROUGH}


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
    """A real host. `say` receives every line a student should see."""

    def __init__(self, say: Callable[[str], None], *, quiet: bool = False) -> None:
        self._say = say
        self._quiet = quiet

    def _show(self, argv: Sequence[str], sudo: bool, env: Mapping[str, str] | None) -> None:
        if not self._quiet:
            self._say("  $ " + shlex.join(_full_argv(argv, sudo, env)))

    def run(
        self,
        argv: Sequence[str],
        *,
        sudo: bool = False,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
        capture: bool = False,
        timeout: int | None = None,
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
                timeout=timeout,
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


class DryRunHost(Host):
    """Prints what would happen: commands as `$` lines, files as `# ---- path` + content (the
    golden format). Questions about the host answer "nothing there"."""

    def run(
        self,
        argv: Sequence[str],
        *,
        sudo: bool = False,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
        capture: bool = False,
        timeout: int | None = None,
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
        self._say(f"# ---- {path}")
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

    def writable(self, path: Path) -> bool:
        return True

    def mkdir(self, path: Path, *, sudo: bool = False, owner: str | None = None) -> None:
        return None

    def is_active(self, unit: str) -> bool:
        return False


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
    podman: set[str] = field(default_factory=set)  # "container:NAME", "volume:NAME", "network:NAME"
    lines: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(self.lines.append)

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
        timeout: int | None = None,
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

    def exists(self, path: Path) -> bool:
        return path in self.files or path in self.dirs

    def writable(self, path: Path) -> bool:
        return path not in self.unwritable

    def mkdir(self, path: Path, *, sudo: bool = False, owner: str | None = None) -> None:
        extra = [f"owner={owner}"] if owner else []
        self.calls.append(Call(["mkdir", str(path), *extra], sudo, {}))
        self.dirs.add(path)

    def is_active(self, unit: str) -> bool:
        return unit in self.active
