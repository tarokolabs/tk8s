"""The complete --dry-run output of two shapes must not change unnoticed.
Regenerate deliberately: UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py"""

import os
import re
from pathlib import Path

import pytest

from tk8s.cli import main

GOLDEN = Path(__file__).resolve().parent / "golden"


def normalise(out: str, tmp: Path) -> str:
    """Paths and the checkout's version vary per machine and per commit; the rest must not."""
    out = re.sub(re.escape(str(tmp)), "<TMP>", out)
    return re.sub(r"^  tk8s: .*$", "  tk8s: <VERSION>", out, flags=re.M)


@pytest.mark.parametrize(
    ("name", "argv"),
    [
        ("default", ["create", "cluster", "--dry-run"]),
        (
            "ha",
            [
                "create",
                "cluster",
                "ha",
                "--control-planes",
                "3",
                "--workers",
                "2",
                "--cpu",
                "4",
                "--memory",
                "6G",
                "--defer-join",
                "--dry-run",
            ],
        ),
        (
            "bgp",
            ["create", "cluster", "bgp", "--control-planes", "3", "--ha", "bgp", "--dry-run"],
        ),
    ],
)
def test_dry_run_matches_golden(
    name: str,
    argv: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TK_DATA_DIR", str(tmp_path))
    assert main(argv) == 0
    out = normalise(capsys.readouterr().out, tmp_path)
    golden = GOLDEN / f"{name}.dry-run.txt"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        golden.write_text(out)
    assert out == golden.read_text()
