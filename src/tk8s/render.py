"""Templates are plain files under templates/ that look like the file they become, with $variables.

Conditions and loops live in Python: the caller computes strings such as $after or $servers. A
missing or unused variable is an error, so a template and its caller cannot drift apart silently.
"""

from pathlib import Path
from string import Template


def template(home: Path, file: str, /, **vars: object) -> str:
    text = (home / "templates" / file).read_text()
    tpl = Template(text)
    wanted = {m.group("named") or m.group("braced") for m in tpl.pattern.finditer(text)} - {None}
    unused = set(vars) - wanted
    if unused:
        raise ValueError(f"templates/{file}: unused variables {sorted(unused)}")
    return tpl.substitute({k: str(v) for k, v in vars.items()})
