"""Workflow structure no unit can run: L3 proves the offline create on the node image built from
the checkout under test; tokens stay read-only except where a job writes."""

from pathlib import Path

from tk8s import versions

HOME = Path(__file__).resolve().parents[1]
WF = HOME / ".github" / "workflows"
L3 = (WF / "l3.yaml").read_text()
RELEASE = (WF / "release.yml").read_text()
CI = (WF / "ci.yaml").read_text()


def line_of(text: str, needle: str) -> int:
    return next(i for i, line in enumerate(text.splitlines()) if needle in line)


def test_every_workflow_token_is_read_only_at_the_top() -> None:
    for w in sorted(WF.glob("*.y*ml")):
        lines = w.read_text().splitlines()
        i = lines.index("permissions:")
        assert lines[i + 1] == "  contents: read", w.name


def test_l3_builds_the_node_image_fills_the_store_blocks_downloads_then_creates() -> None:
    build = line_of(L3, "podman build")
    pull = line_of(L3, "tkctl pull images --k8s")
    plugins = line_of(L3, "--phase nodes/cni-plugins")
    offline = line_of(L3, "tk8s-offline")
    create = line_of(L3, "run: time tkctl create cluster ci --workers 1")
    assert build < pull < plugins < offline < create
    assert "nothing to pull" in L3 and 'TK_OFFLINE: "1"' in L3 and "Pulled image" in L3
    assert '--build-arg CRIO="$crio"' in L3 and " helm.cilium.io " in L3
    assert "task " not in L3 and "install.sh" in L3
    assert line_of(L3, "tk8s-offline$/d") < line_of(L3, "upload-artifact")
    assert "branches: [v2, main]" in L3


def test_l3_installs_with_the_installer_from_the_checkout() -> None:
    assert (
        'TK_VERSION="${GITHUB_REF_NAME}" TK_REPO="$GITHUB_WORKSPACE" '
        'TK_RAW="file://$GITHUB_WORKSPACE" sh install.sh'
    ) in L3


def test_release_waits_for_l3_inline_and_renders_notes() -> None:
    assert "scripts/wait-l3.sh" not in RELEASE
    assert "gh run list" in RELEASE and "--workflow l3.yaml" in RELEASE
    assert '"conclusion":"success"' in RELEASE
    assert "scripts/release-notes.sh" in RELEASE and "gh release create" in RELEASE


def test_ci_uses_the_pinned_uv_and_runs_lint_types_tests() -> None:
    v = versions.load(HOME / "versions.yaml")
    assert f'version: "{v.uv}"' in CI
    for s in ("uv sync --locked", "ruff check", "ruff format --check", "mypy src tests", "pytest"):
        assert s in CI, s
    assert "shellcheck -s sh -S warning install.sh" in CI
