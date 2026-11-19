"""Tolerant reader for the QSettings-style INI files MO2 writes (meta.ini, *.meta)."""

from __future__ import annotations

import configparser
from pathlib import Path


def read_ini(path: Path) -> dict[str, dict[str, str]]:
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # keep key case (modID vs modid both occur)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        parser.read_string(text)
    except (OSError, configparser.Error):
        return {}
    return {s: {k: _unquote(v) for k, v in parser.items(s)} for s in parser.sections()}


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return v


def as_int(v: str | None) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def as_bool(v: str | None) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")
