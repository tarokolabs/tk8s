from pathlib import Path

from tk8s import versions

HOME = Path(__file__).resolve().parents[1]


def test_real_versions_yaml_loads_and_pins_everything() -> None:
    v = versions.load(HOME / "versions.yaml")
    assert v.kubernetes.default == "1.37.1" and v.kubernetes.previous == "1.36.5"
    for k in (
        "cni_plugins",
        "cilium_cli",
        "calico",
        "metrics_server",
        "local_path_provisioner",
        "gvisor",
        "haproxy",
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
