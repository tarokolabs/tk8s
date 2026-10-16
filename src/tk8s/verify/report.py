"""What every check gets: the Report that counts and prints PASS/FAIL/SKIP lines, runs kubectl
against the cluster and polls; the namespace the test resources live in."""

import time
from dataclasses import dataclass, field

from ..create.kubeadm import kubectl_env
from ..phases import Context

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
    checked: dict[str, bool] = field(default_factory=dict)

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
        r = self.ctx.host.run(
            ["kubectl", *args], env=kubectl_env(self.ctx), input=input, capture=True, check=False
        )
        return r.returncode, r.stdout

    def poll(self, rounds: int, probe: object) -> bool:
        """Call probe() up to rounds times, poll_slow apart, until it returns true."""
        for _ in range(max(rounds, 1)):
            if probe():  # type: ignore[operator]
                return True
            time.sleep(self.ctx.options.poll_slow)
        return False
