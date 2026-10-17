"""What every check gets: the Report that counts and prints PASS/FAIL/SKIP lines, runs kubectl
against the cluster and polls; the namespace the test resources live in."""

from collections.abc import Callable
from dataclasses import dataclass

from ..create import kubeadm
from ..phases import Context, poll_until

NS = "tk-verify"


class VerifyError(Exception):
    pass


@dataclass
class Report:
    ctx: Context
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    gateway_ip: str = ""
    gvisor_out: bool | None = None  # what the gvisor pod printed, for check_gvisor_network

    def ok(self, msg: str) -> None:
        self.passed += 1
        self.ctx.say(f"PASS  {msg}")

    def ng(self, msg: str, detail: str = "") -> None:
        self.failed += 1
        self.ctx.say(f"FAIL  {msg}{' — ' + detail if detail else ''}")

    def skip(self, msg: str, why: str) -> None:
        self.skipped += 1
        self.ctx.say(f"SKIP  {msg} — {why}")

    @property
    def timeout(self) -> int:
        return self.ctx.options.verify_timeout

    def kubectl(self, *args: str, input: str | None = None) -> tuple[int, str]:
        r = kubeadm.kubectl(self.ctx, *args, input=input)
        return r.returncode, r.stdout

    def poll(self, rounds: int, probe: Callable[[], bool]) -> bool:
        """Call probe() up to rounds times, poll_slow apart, until it returns true."""
        return poll_until(probe, rounds=max(rounds, 1), delay=self.ctx.options.poll_slow)
