"""redscript annotation conflicts: several mods @replaceMethod-ing the same function.

Only one replacement can win, so the other mods' version of that function never runs.
Replacements with byte-identical bodies (common when one author ships the same helper
in several mods) are harmless and reported as such. @wrapMethod chains and is fine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_REPLACE = re.compile(
    r"@(replaceMethod|replaceGlobal)\s*\(\s*(\w*)\s*\)\s*"
    r"(?:(?:public|private|protected|final|static|native|cb|const|quest|exec)\s+)*func\s+(\w+)"
)


def _body(text: str, start: int) -> str:
    """The annotated function's source from `start` to its matching closing brace."""
    i = text.find("{", start)
    if i < 0:
        return text[start:]
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return " ".join(text[start : j + 1].split())
    return text[start:]


@dataclass
class Replacement:
    mod: str
    file: str
    body: str


@dataclass
class ScriptConflict:
    target: str  # Class::method or <global>::func
    replacements: list[Replacement] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        return len({r.body for r in self.replacements}) == 1


def scan(mods_by_priority: list[tuple[str, Path]]) -> list[ScriptConflict]:
    found: dict[str, list[Replacement]] = {}
    for mod, root in mods_by_priority:
        for p in sorted(root.rglob("*.reds")):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in _REPLACE.finditer(text):
                target = f"{m.group(2) or '<global>'}::{m.group(3)}"
                found.setdefault(target, []).append(
                    Replacement(mod, str(p.relative_to(root)).replace("\\", "/"), _body(text, m.start()))
                )
    return [
        ScriptConflict(t, reps) for t, reps in sorted(found.items()) if len({r.mod for r in reps}) > 1
    ]
