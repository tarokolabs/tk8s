"""The node image recipe, checked as text: bgp mode needs FRR and the health timer in every node,
both off until tkctl turns them on in a control plane."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "images" / "node"
RECIPE = (ROOT / "Containerfile").read_text()


def test_recipe_installs_frr_and_leaves_it_disabled() -> None:
    apt = RECIPE.split("apt-get install -y --no-install-recommends")[1].split("&&")[0]
    assert " frr " in apt.replace("\\\n", " ") + " "
    assert "systemctl disable frr.service" in RECIPE


def test_recipe_ships_the_health_timer_disabled() -> None:
    assert (
        "COPY systemd/tk8s-apiserver-health.service systemd/tk8s-apiserver-health.timer "
        "/etc/systemd/system/"
    ) in RECIPE
    assert "COPY sbin/tk8s-apiserver-health /usr/local/sbin/tk8s-apiserver-health" in RECIPE
    assert "tk8s-apiserver-health" not in RECIPE.split("systemctl enable ")[1].split("&&")[0]


def test_health_script_reads_the_vip_tkctl_writes() -> None:
    script = (ROOT / "sbin" / "tk8s-apiserver-health").read_text()
    assert script.startswith("#!/bin/sh\n")
    assert "${TK8S_VIP_FILE:-/etc/tk8s/vip}" in script
    assert "curl -ks --max-time 0.75 https://127.0.0.1:6443/readyz" in script


class Health:
    """The health script run for real, with curl and ip replaced by shims on PATH: curl answers
    what `answer` says, ip keeps lo's addresses in a file and logs every add/del."""

    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.lo = tmp_path / "lo"
        self.lo.write_text("    inet 127.0.0.1/8 scope host lo\n")
        self.log = tmp_path / "ip.log"
        self.log.write_text("")
        (tmp_path / "vip").write_text("172.23.0.1\n")
        curl = self.bin / "curl"
        curl.write_text(
            "#!/bin/sh\n"
            'case "$(cat "$ANSWER")" in\n'
            "ok) echo ok; exit 0 ;;\n"
            "slow) exit 28 ;;\n"
            "refused) exit 7 ;;\n"
            "cut) printf ok; exit 56 ;;\n"
            "*) printf '[+]ping ok\\n[-]etcd failed\\n'; exit 0 ;;\n"
            "esac\n"
        )
        curl.chmod(0o755)
        ip = self.bin / "ip"
        ip.write_text(
            "#!/bin/sh\n"
            '[ -n "$IP_FAIL" ] && { echo "RTNETLINK answers: $IP_FAIL" >&2; exit 2; }\n'
            'case "$*" in\n'
            '"-4 addr show dev lo") cat "$LO" ;;\n'
            '"addr add "*) echo "$*" >> "$LOG"; echo "    inet $3 scope global lo" >> "$LO" ;;\n'
            '"addr del "*) echo "$*" >> "$LOG"; grep -v " $3 " "$LO" > "$LO.new";'
            ' mv "$LO.new" "$LO" ;;\n'
            '*) echo "unexpected ip $*" >&2; exit 1 ;;\n'
            "esac\n"
        )
        ip.chmod(0o755)

    def tick(self, answer: str, ip_fail: str = "") -> int:
        (self.dir / "answer").write_text(answer)
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "ANSWER": str(self.dir / "answer"),
            "LO": str(self.lo),
            "LOG": str(self.log),
            "IP_FAIL": ip_fail,
            "TK8S_VIP_FILE": str(self.dir / "vip"),
            "TK8S_STREAK_FILE": str(self.dir / "streak"),
        }
        r = subprocess.run(
            ["sh", str(ROOT / "sbin" / "tk8s-apiserver-health")], env=env, capture_output=True
        )
        self.stderr = r.stderr.decode()
        return r.returncode

    @property
    def announced(self) -> bool:
        return " 172.23.0.1/32 " in self.lo.read_text()

    @property
    def changes(self) -> list[str]:
        return self.log.read_text().splitlines()


def test_health_announces_after_three_good_probes_and_withdraws_after_two_bad(
    tmp_path: Path,
) -> None:
    h = Health(tmp_path)
    assert [h.tick("ok"), h.tick("ok")] == [0, 0] and not h.announced
    assert h.tick("ok") == 0 and h.announced
    assert "announcing 172.23.0.1" in h.stderr  # the journal shows every transition
    # one slow answer, then a good one: the VIP stays (no ECMP rehash for a hiccup)
    assert h.tick("slow") == 0 and h.announced and "failed once" in h.stderr
    assert h.tick("ok") == 0 and h.announced and h.stderr == ""
    assert h.tick("refused") == 0 and h.announced
    assert h.tick("refused") == 0 and not h.announced and "withdrawing 172.23.0.1" in h.stderr
    # back up: three good answers in a row, a failure in between starts over
    assert [h.tick("ok"), h.tick("ok"), h.tick("500")] == [0, 0, 0] and not h.announced
    assert [h.tick("ok"), h.tick("ok")] == [0, 0] and not h.announced
    assert h.tick("ok") == 0 and h.announced
    add, delete = "addr add 172.23.0.1/32 dev lo", "addr del 172.23.0.1/32 dev lo"
    assert h.changes == [add, delete, add]


def test_health_counts_a_failed_transfer_as_bad_whatever_the_body(tmp_path: Path) -> None:
    h = Health(tmp_path)
    for _ in range(3):
        h.tick("ok")
    assert h.announced
    assert [h.tick("cut"), h.tick("cut")] == [0, 0] and not h.announced


def test_health_reports_a_failed_ip_and_retries_next_tick(tmp_path: Path) -> None:
    h = Health(tmp_path)
    assert [h.tick("ok"), h.tick("ok")] == [0, 0]
    assert h.tick("ok", ip_fail="Operation not permitted") == 1
    assert "Operation not permitted" in h.stderr and not h.announced
    assert h.tick("ok") == 0 and h.announced


def test_health_does_nothing_without_a_vip(tmp_path: Path) -> None:
    h = Health(tmp_path)
    (tmp_path / "vip").unlink()
    assert h.tick("ok") == 0 and h.tick("ok") == 0 and h.tick("ok") == 0
    assert not h.announced and h.changes == []


def test_health_timer_fires_every_second() -> None:
    timer = (ROOT / "systemd" / "tk8s-apiserver-health.timer").read_text()
    service = (ROOT / "systemd" / "tk8s-apiserver-health.service").read_text()
    assert "OnUnitActiveSec=1\n" in timer and "OnBootSec=5\n" in timer
    assert "WantedBy=timers.target" in timer
    assert "Type=oneshot\n" in service
    # a oneshot started every second trips systemd's default start rate limit (5 per 10 s)
    assert "StartLimitIntervalSec=0\n" in service.split("[Service]")[0]
    assert "ExecStart=/usr/local/sbin/tk8s-apiserver-health\n" in service
