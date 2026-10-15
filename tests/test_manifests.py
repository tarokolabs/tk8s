"""manifests/ holds what create applies; the vendored files carry their version in the name and
scripts/vendor-manifests.sh is the only way they change. Checked here without network."""

from pathlib import Path

from tk8s import versions

HOME = Path(__file__).resolve().parents[1]
MANIFESTS = HOME / "manifests"
SCRIPT = (HOME / "scripts" / "vendor-manifests.sh").read_text()
V = versions.load(HOME / "versions.yaml")


LOCAL_PATH = MANIFESTS / f"local-path-storage.{V.local_path_provisioner.lstrip('v')}.yaml"


def test_every_vendored_file_matches_versions_yaml() -> None:
    assert (MANIFESTS / f"gateway-api.{V.gateway_api.lstrip('v')}.yaml").is_file()
    assert (MANIFESTS / f"metrics-server.{V.metrics_server.lstrip('v')}.yaml").is_file()
    assert LOCAL_PATH.is_file()
    assert (MANIFESTS / f"cilium-{V.cilium}" / "Chart.yaml").is_file()


def test_local_path_keeps_the_three_platform_edits() -> None:
    text = LOCAL_PATH.read_text()
    assert "volumeBindingMode: Immediate" in text
    assert '"sharedFileSystemPath": "/opt/taroko/storage"' in text
    assert f"image: {V.verify_busybox}" in text
    assert "DEFAULT_PATH_FOR_NON_LISTED_NODES" not in text


def test_the_script_vendors_local_path_and_applies_the_edits() -> None:
    assert "local_path_provisioner" in SCRIPT
    assert "rancher/local-path-provisioner" in SCRIPT
    assert "volumeBindingMode: Immediate" in SCRIPT
    assert "sharedFileSystemPath" in SCRIPT
    assert "verify_busybox" in SCRIPT


def test_the_script_names_the_test_that_checks_it() -> None:
    assert "tests/test_manifests.py" in SCRIPT
    assert "versions.test.sh" not in SCRIPT
