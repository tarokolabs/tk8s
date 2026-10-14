"""kubeadm-style phases: a name, one line of what it does, done(ctx), run(ctx).

The runner skips a phase whose done() is already true, runs it otherwise and checks done() again:
a phase that ran and still is not done is a bug, not something to retry. done() looks at the host;
status.completed in cluster.yaml is a record for humans, never the source of truth.

tk8s_version() is the one command outside host.py: it only reads git, and going through the
host would make the version an empty string under --dry-run.
"""

import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import model
from .host import Host, HostError
from .paths import Paths
from .versions import Versions


@dataclass
class Options:
    dry_run: bool = False
    quiet: bool = False
    offline: bool = False  # TK_OFFLINE=1: never download
    assume_yes: bool = False  # TK_ASSUME_YES=1 or --yes
    refresh: bool = False  # pull images --refresh
    retry_delay: float = 5.0  # seconds between the two attempts of a pull


class PhaseFailed(Exception):
    pass


@dataclass
class Context:
    host: Host
    paths: Paths
    versions: Versions
    options: Options
    say: Callable[[str], None]
    cluster: model.Cluster | None = None  # set by plan; later phases require it

    def require_cluster(self) -> model.Cluster:
        if self.cluster is None:
            raise PhaseFailed("no cluster in this context; plan runs first")
        return self.cluster

    @property
    def state_dir(self) -> Path:
        return self.paths.state_dir(self.require_cluster().name)


@dataclass(frozen=True)
class Phase:
    name: str
    summary: str
    done: Callable[[Context], bool] | None  # None: no completion state, runs every time
    run: Callable[[Context], None]
    children: tuple[Phase, ...] = ()

    def leaves(self, prefix: str = "") -> Iterator[tuple[str, Phase]]:
        full = f"{prefix}{self.name}"
        if self.children:
            for c in self.children:
                yield from c.leaves(f"{full}/")
        else:
            yield full, self


@dataclass
class Runner:
    phases: list[Phase]
    only: str | None = None
    skip: set[str] = field(default_factory=set)

    def run(self, ctx: Context) -> None:
        for full, phase in self._selected():
            if full in self.skip or full.split("/")[0] in self.skip:
                ctx.say(f"[{full}] skipped")
                continue
            try:
                if phase.done is not None and phase.done(ctx):
                    ctx.say(f"[{full}] already done")
                    continue
                ctx.say(f"[{full}] {phase.summary}")
                phase.run(ctx)
                if ctx.options.dry_run:
                    ctx.say("")  # keep the file blocks apart from the next phase line
                    continue
                if phase.done is not None and not phase.done(ctx):
                    raise PhaseFailed(f"[{full}] ran but is not done")
            except HostError as err:
                raise PhaseFailed(f"[{full}] failed: {err}") from err
            self._record(ctx, full)

    def _selected(self) -> list[tuple[str, Phase]]:
        leaves = [leaf for p in self.phases for leaf in p.leaves()]
        if self.only is None:
            return leaves
        picked = [(f, p) for f, p in leaves if f == self.only or f.startswith(self.only + "/")]
        if not picked:
            names = ", ".join(f for f, _ in leaves)
            raise PhaseFailed(f"unknown phase {self.only!r}; phases: {names}")
        return picked

    @staticmethod
    def _record(ctx: Context, full: str) -> None:
        if ctx.cluster is None or ctx.cluster.status is None:
            return
        completed = {**ctx.cluster.status.completed, full: datetime.now(UTC)}
        status = ctx.cluster.status.model_copy(update={"completed": completed})
        ctx.cluster = ctx.cluster.model_copy(update={"status": status})
        ctx.host.write(ctx.state_dir / "cluster.yaml", model.dumps(ctx.cluster))


def tree(phases: list[Phase], indent: str = "") -> list[str]:
    out: list[str] = []
    for p in phases:
        out.append(f"{indent}{p.name:<18} {p.summary}")
        out += tree(list(p.children), indent + "  ")
    return out


def tk8s_version(home: Path) -> str:
    """The checkout's version, for status.tk8s and `tkctl version`."""
    try:
        r = subprocess.run(
            ["git", "-C", str(home), "describe", "--tags", "--always"],
            capture_output=True,
            text=True,
            check=False,
        )
        return r.stdout.strip() or "dev"
    except OSError:
        return "dev"
