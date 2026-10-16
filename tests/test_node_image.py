"""The node image recipe, checked as text: bgp mode needs FRR and the health timer in every node,
both off until tkctl turns them on in a control plane."""

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


def test_health_script_moves_the_vip_with_readyz() -> None:
    script = (ROOT / "sbin" / "tk8s-apiserver-health").read_text()
    assert script.startswith("#!/bin/sh\n")
    assert "vip=$(cat /etc/tk8s/vip 2>/dev/null) || exit 0" in script
    assert "curl -ks --max-time 1 https://127.0.0.1:6443/readyz" in script
    assert 'ip addr add "$vip/32" dev lo' in script and 'ip addr del "$vip/32" dev lo' in script


def test_health_timer_fires_every_two_seconds() -> None:
    timer = (ROOT / "systemd" / "tk8s-apiserver-health.timer").read_text()
    service = (ROOT / "systemd" / "tk8s-apiserver-health.service").read_text()
    assert "OnUnitActiveSec=2\n" in timer and "OnBootSec=5\n" in timer
    assert "WantedBy=timers.target" in timer
    assert "Type=oneshot\n" in service
    assert "ExecStart=/usr/local/sbin/tk8s-apiserver-health\n" in service
