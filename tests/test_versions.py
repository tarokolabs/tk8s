import re
from pathlib import Path

from tk8s import versions

HOME = Path(__file__).resolve().parents[1]


def test_real_versions_yaml_loads_and_pins_everything() -> None:
    v = versions.load(HOME / "versions.yaml")
    assert v.kubernetes.default == "1.37.2" and v.kubernetes.previous == "1.36.6"
    for k in (
        "cni_plugins",
        "cilium_cli",
        "metrics_server",
        "local_path_provisioner",
        "gvisor",
        "vyos",
        "vyos_sha256",
        "verify_busybox",
        "verify_alpine",
        "verify_agnhost",
    ):
        assert getattr(v, k), k


def test_crio_table_covers_both_supported_minors() -> None:
    v = versions.load(HOME / "versions.yaml")
    assert v.crio_for(v.kubernetes.default) == "v1.37.2"
    assert v.crio_for(v.kubernetes.previous) == "v1.36.7"
    assert v.crio_for("1.35.5") is None


def test_uv_is_pinned() -> None:
    v = versions.load(HOME / "versions.yaml")
    assert v.uv == "0.13.6"


def test_vyos_is_pinned_by_build_and_checksum_and_haproxy_is_gone() -> None:
    v = versions.load(HOME / "versions.yaml")
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d{2}-\d{4}", v.vyos), v.vyos
    assert re.fullmatch(r"[0-9a-f]{64}", v.vyos_sha256)
    assert not hasattr(v, "haproxy")
