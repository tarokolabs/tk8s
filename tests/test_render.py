from pathlib import Path

import pytest

from tk8s.render import template

HOME = Path(__file__).resolve().parents[1]


def test_every_template_looks_like_its_product() -> None:
    for f in (p for p in (HOME / "templates").rglob("*") if p.is_file()):
        text = f.read_text()
        assert "{{" not in text and "{%" not in text, f.name  # no template language, only $vars


def test_substitution_and_strictness(tmp_path: Path) -> None:
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "x.conf").write_text("name=$name\nport=$port\n")
    assert template(tmp_path, "x.conf", name="a", port=6443) == "name=a\nport=6443\n"
    with pytest.raises(KeyError):
        template(tmp_path, "x.conf", name="a")
    with pytest.raises(ValueError, match="unused variables \\['extra'\\]"):
        template(tmp_path, "x.conf", name="a", port=1, extra=2)
