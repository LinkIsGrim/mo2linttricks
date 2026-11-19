"""Where the GitHub token lives: a per-user file, outside the MO2 instance.

Windows/Wine: %APPDATA%\\mo2linttricks\\github.json (inside the Wine prefix).
Elsewhere:    $XDG_CONFIG_HOME/mo2linttricks/github.json, mode 0600.
The token is never written into the instance or the synced repo.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def token_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "mo2linttricks" / "github.json"


def load() -> dict:
    try:
        return json.loads(token_path().read_text())
    except (OSError, ValueError):
        return {}


def save(token: str, login: str) -> None:
    p = token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"token": token, "login": login}))
    if os.name != "nt":
        os.chmod(p, 0o600)


def clear() -> None:
    try:
        token_path().unlink()
    except OSError:
        pass
