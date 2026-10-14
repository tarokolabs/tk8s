"""The README's command table is tkctl --help, and its phase tree is --list-phases."""

import re
from pathlib import Path

from tk8s.cli import USAGE
from tk8s.create import PHASES
from tk8s.phases import tree

README = (Path(__file__).resolve().parents[1] / "README.md").read_text()


def verbs_in(block: str) -> set[str]:
    out: set[str] = set()
    for line in block.splitlines():
        m = re.match(r"\s*tkctl (\S+) (\S+)", line)
        if m and not m.group(1).startswith("-") and not m.group(2).startswith("<"):
            out.add(f"{m.group(1)} {m.group(2)}")
    return out


def block_under(heading: str) -> str:
    """The first fenced code block after a heading."""
    start = README.index(heading)
    open_ = README.index("```\n", start) + 4
    return README[open_ : README.index("```", open_)]


def test_readme_command_table_matches_help() -> None:
    usage = USAGE.split("Usage:")[1].split("\n\n")[0]
    assert verbs_in(block_under("## 指令")) == verbs_in(usage)


def test_readme_phase_tree_is_list_phases() -> None:
    marker = "$ tkctl create cluster --list-phases\n"
    start = README.index(marker) + len(marker)
    block = README[start : README.index("```", start)]
    assert block.rstrip("\n") == "\n".join(tree(PHASES))
