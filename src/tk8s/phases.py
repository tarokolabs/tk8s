"""kubeadm-style phases: a name, one line of what it does, done(ctx), run(ctx).

The runner skips a phase whose done() is already true, runs it otherwise and checks done() again:
a phase that ran and still is not done is a bug, not something to retry. done() looks at the host;
status.completed in cluster.yaml is a record for humans, never the source of truth.

tk8s_version() is the one command outside host.py: it only reads git, and going through the
host would make the version an empty string under --dry-run.
"""

import dataclasses
import subprocess
import time
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
    offline: bool = False  # TK_OFFLINE=1: never download
    assume_yes: bool = False  # TK_ASSUME_YES=1 or --yes
    refresh: bool = False  # pull images --refresh
    mode: str | None = None  # create --mode: the expansion line plan prints first
    retry_delay: float = 5.0  # seconds between the two attempts of a pull
    poll: float = 2.0  # seconds between polls while waiting for a node or the LB
    poll_slow: float = 5.0  # seconds between polls while waiting for pods and nodes to be Ready
    verify_timeout: int = 180  # TK_VERIFY_TIMEOUT: seconds to wait for each verify check


class PhaseFailed(Exception):
    pass


@dataclass
class Context:
    host: Host  # the operator: where tkctl runs, where cluster.yaml and the kubeconfig live
    paths: Paths
    versions: Versions
    options: Options
    say: Callable[[str], None]
    cluster: model.Cluster | None = None  # set by plan; later phases require it
    name: str | None = None  # the cluster named on the command line (delete works without a file)
    node: str | None = None  # the node a node verb works on (add node's plan phase sets it)
    request: model.NodeRequest | None = None  # what add node was asked for
    # multi-host: name -> Host for every host of the cluster (the operator's own entry is
    # `host` itself); empty on a single-host cluster
    hosts: dict[str, Host] = field(default_factory=dict)

    def host_for(self, container: str) -> Host:
        """The host running this node or router container; the operator on a single-host
        cluster or for a name the cluster does not have."""
        where = self.cluster.host_of(container) if self.cluster is not None else None
        return self.hosts.get(where, self.host) if where is not None else self.host

    def node_hosts(self) -> list[tuple[str, Host]]:
        """Every host nodes run on, in cluster.yaml order; a single-host cluster is [("", host)]."""
        if self.cluster is None or not self.cluster.multi_host:
            return [("", self.host)]
        for n in self.cluster.host_names:
            if n not in self.hosts:
                raise PhaseFailed(f"host {n} of cluster {self.cluster.name} has no connection")
        return [(n, self.hosts[n]) for n in self.cluster.host_names]

    def remote_hosts(self) -> list[tuple[str, Host]]:
        return [(n, h) for n, h in self.node_hosts() if h is not self.host]

    def target_hosts(self) -> list[tuple[str, Host]]:
        """Every host with a <cluster>.target: the node hosts, and the operator when it runs
        no node (its target carries the routes unit); the operator comes last."""
        hosts = self.node_hosts()
        mine = [(n, h) for n, h in hosts if h is self.host]
        others = [(n, h) for n, h in hosts if h is not self.host]
        if self.cluster is not None and self.cluster.multi_host and not mine:
            mine = [("operator", self.host)]
        return [*others, *mine]

    def on(self, host: Host) -> Context:
        """This context with `host` as the one commands go to (the cluster's hosts shared)."""
        return dataclasses.replace(self, host=host)

    def save_cluster(self, cluster: model.Cluster) -> None:
        """cluster.yaml on the operator, and a copy on every other host of the cluster."""
        self.cluster = cluster
        text = model.dumps(cluster)
        self.host.write(self.state_dir / "cluster.yaml", text)
        for _, h in self.remote_hosts():
            h.write(self.state_dir / "cluster.yaml", text)

    def require_cluster(self) -> model.Cluster:
        if self.cluster is None:
            raise PhaseFailed("no cluster in this context; plan runs first")
        return self.cluster

    @property
    def cluster_name(self) -> str:
        if self.cluster is not None:
            return self.cluster.name
        if self.name is None:
            raise PhaseFailed("no cluster in this context; plan runs first")
        return self.name

    @property
    def state_dir(self) -> Path:
        return self.paths.state_dir(self.cluster_name)

    def log_path(self, name: str) -> Path:
        """Where a command's captured output goes for a person to read: <state dir>/logs/."""
        return self.state_dir / "logs" / name


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
    record: bool = True  # status.completed is create's record; delete and node verbs leave it alone

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
            if self.record:
                self._record(ctx, full)

    def _selected(self) -> list[tuple[str, Phase]]:
        leaves = [leaf for p in self.phases for leaf in p.leaves()]
        if self.only is None:
            return leaves
        picked = [
            (f, p)
            for f, p in leaves
            if f == self.only or f.startswith(self.only + "/") or f.split("/")[-1] == self.only
        ]
        if not picked:
            names = ", ".join(f for f, _ in leaves)
            raise PhaseFailed(f"unknown phase {self.only!r}; phases: {names}")
        return picked

    @staticmethod
    def _record(ctx: Context, full: str) -> None:
        """status.completed in cluster.yaml, once plan has written that file (recording preflight
        earlier would create the file and make plan look already done)."""
        if ctx.cluster is None or ctx.cluster.status is None:
            return
        if not ctx.host.exists(ctx.state_dir / "cluster.yaml"):
            return
        completed = {**ctx.cluster.status.completed, full: datetime.now(UTC)}
        status = ctx.cluster.status.model_copy(update={"completed": completed})
        ctx.save_cluster(ctx.cluster.model_copy(update={"status": status}))


def tree(phases: list[Phase], indent: str = "") -> list[str]:
    out: list[str] = []
    for p in phases:
        out.append(f"{indent}{p.name:<18} {p.summary}")
        out += tree(list(p.children), indent + "  ")
    return out


def poll_until(probe: Callable[[], bool], *, rounds: int, delay: float) -> bool:
    """probe() up to `rounds` times, `delay` apart (no sleep after the last one); whether it
    ever held."""
    for i in range(rounds):
        if probe():
            return True
        if i < rounds - 1:
            time.sleep(delay)
    return False


def wait_for(
    ctx: Context,
    probe: Callable[[], bool],
    *,
    rounds: int,
    delay: float,
    failure: Callable[[], str],
) -> None:
    """poll_until, and PhaseFailed with the caller's message when the probe never held."""
    if not poll_until(probe, rounds=rounds, delay=delay):
        raise PhaseFailed(failure())


def confirm(ctx: Context, question: str, aborted: type[Exception]) -> None:
    """A [y/N] prompt, skipped under --yes / TK_ASSUME_YES; anything but y (EOF included) aborts
    with the caller's exception type."""
    if ctx.options.assume_yes:
        return
    try:
        answer = input(question)
    except EOFError:
        answer = ""
    if answer.strip().lower() != "y":
        raise aborted("aborted")


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
