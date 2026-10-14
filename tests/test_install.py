"""install.sh is shell; L3 runs it for real. Here: the shape a person reads and the pins it
honours, without executing it."""

import re
from pathlib import Path

from tk8s import versions

HOME = Path(__file__).resolve().parents[1]
SCRIPT = (HOME / "install.sh").read_text()


def test_the_body_runs_only_from_main_on_the_last_line() -> None:
    assert SCRIPT.rstrip().splitlines()[-1] == 'main "$@"'
    assert SCRIPT.count("\nmain() {") == 1


def test_uv_is_pinned_from_versions_yaml_and_checksummed() -> None:
    v = versions.load(HOME / "versions.yaml")
    assert 'sed -nE \'s/^uv: *"?([^"]+)"?.*/\\1/p\'' in SCRIPT
    assert 'base="https://github.com/astral-sh/uv/releases/download/$uv_v"' in SCRIPT
    assert '"$base/uv-x86_64-unknown-linux-gnu.tar.gz"' in SCRIPT
    assert '"$base/uv-x86_64-unknown-linux-gnu.tar.gz.sha256"' in SCRIPT
    assert re.search(r'\[ "\$want" = "\$got" \] \|\| fail "uv \$uv_v checksum mismatch', SCRIPT)
    assert "task" not in SCRIPT  # no go-task left anywhere in the installer
    assert v.uv  # the pin exists; the value is whatever versions.yaml says


def test_kubectl_is_still_downloaded_and_checked() -> None:
    assert "https://dl.k8s.io/release/v$k8s_v/bin/linux/amd64" in SCRIPT
    assert 'fail "kubectl $k8s_v checksum mismatch' in SCRIPT


def test_the_checkout_is_synced_from_the_lock_and_linked() -> None:
    assert '"$TK_BIN_DIR/uv" sync --locked --no-dev --project "$TK_INSTALL_DIR"' in SCRIPT
    assert 'sudo ln -sfn "$TK_INSTALL_DIR/.venv/bin/tkctl" "$TK_BIN_DIR/tkctl"' in SCRIPT
    assert 'sudo install -d -o "$(id -u)" -g "$(id -g)" "$TK_INSTALL_DIR"' in SCRIPT
    assert "sudo git" not in SCRIPT and "sudo uv" not in SCRIPT  # both belong to the user


def test_prerequisites_are_named() -> None:
    for s in (
        "podman $pv is too old; 5.4 or newer is required",
        "cgroup v2 is required",
        "swapoff -a",
        "sudo without a password prompt",
    ):
        assert s in SCRIPT, s
