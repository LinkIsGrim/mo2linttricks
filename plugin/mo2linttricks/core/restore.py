"""Plan a restore: compare a profile's synced modlist with what is installed locally."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Row actions
INSTALLED = "installed"  # mod already present locally
INSTALL_ARCHIVE = "install-archive"  # archive already in the downloads folder
DOWNLOAD_NEXUS = "download-nexus"  # mod id + file id known
DOWNLOAD_URL = "download-url"
CREATE_SEPARATOR = "create-separator"
GAME = "game"  # unmanaged entry (DLC etc.): nothing to do
MANUAL = "manual"  # no known source: user must provide the archive


@dataclass
class PlanRow:
    name: str
    priority: int
    enabled: bool
    kind: str
    action: str
    info: dict = field(default_factory=dict)
    archive: str = ""  # local archive path when known
    status: str = ""  # UI status override

    @property
    def nexus_ids(self) -> tuple[int, int] | None:
        modid = int(self.info.get("modid") or 0)
        fileids = self.info.get("fileids") or []
        return (modid, int(fileids[-1])) if modid and fileids else None

    def nexus_page(self, game_nexus_name: str) -> str:
        modid = self.info.get("modid")
        if not modid or not game_nexus_name:
            return ""
        url = f"https://www.nexusmods.com/{game_nexus_name}/mods/{modid}?tab=files"
        ids = self.nexus_ids
        return url + (f"&file_id={ids[1]}" if ids else "")


def plan(
    modlist_json: bytes,
    mods_json: bytes,
    installed_mods: set[str],
    downloads_dir: Path,
) -> list[PlanRow]:
    entries = json.loads(modlist_json or b"[]")
    mods = json.loads(mods_json or b"{}")
    archives = {p.name.lower(): p for p in downloads_dir.iterdir()} if downloads_dir.is_dir() else {}
    rows = []
    for e in entries:
        name, kind = e["name"], e.get("kind", "mod")
        info = mods.get(name, {})
        row = PlanRow(name, int(e.get("priority", 0)), bool(e.get("enabled")), kind, MANUAL, info)
        inst_file = Path(str(info.get("installationFile", "")).replace("\\", "/")).name.lower()
        if kind == "foreign":
            row.action = GAME
        elif name in installed_mods:
            row.action = INSTALLED
        elif kind == "separator":
            row.action = CREATE_SEPARATOR
        elif inst_file and inst_file in archives:
            row.action = INSTALL_ARCHIVE
            row.archive = str(archives[inst_file])
        elif row.nexus_ids:
            row.action = DOWNLOAD_NEXUS
        elif info.get("downloadUrl") or info.get("url"):
            row.action = DOWNLOAD_URL
        rows.append(row)
    return rows


def parse_plugins(plugins_txt: bytes, loadorder_txt: bytes) -> list[tuple[str, bool]]:
    """(plugin, active) in load order, from MO2's loadorder.txt and plugins.txt (Bethesda games)."""
    active = set()
    for line in plugins_txt.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("*"):
            active.add(line[1:].lower())
    order = [
        line.strip()
        for line in loadorder_txt.decode("utf-8", "replace").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    return [(p, p.lower() in active) for p in order]
