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
    archives: list[str] = field(default_factory=list)  # local archive paths, in install order
    missing_files: list[int] = field(default_factory=list)  # Nexus file ids still to download
    status: str = ""  # UI status override

    @property
    def nexus_files(self) -> list[int]:
        """All Nexus file ids that make up this mod, in install order (merged mods have several)."""
        files = self.info.get("files") or []
        if files:
            return [int(f["fileid"]) for f in files]
        ids = self.info.get("fileids") or []
        return [int(ids[-1])] if ids else []

    @property
    def nexus_ids(self) -> tuple[int, int] | None:
        modid = int(self.info.get("modid") or 0)
        files = self.nexus_files
        return (modid, files[-1]) if modid and files else None

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
        files = info.get("files") or []
        if files:
            have = [archives.get(str(f.get("archive", "")).lower()) for f in files]
            row.archives = [str(a) for a in have if a]
            row.missing_files = [int(f["fileid"]) for f, a in zip(files, have) if not a]
        elif inst_file and inst_file in archives:
            row.archives = [str(archives[inst_file])]
        if kind == "foreign":
            row.action = GAME
        elif name in installed_mods:
            row.action = INSTALLED
        elif kind == "separator":
            row.action = CREATE_SEPARATOR
        elif row.archives and not row.missing_files:
            row.action = INSTALL_ARCHIVE
        elif row.nexus_ids:
            row.action = DOWNLOAD_NEXUS
            if not files:
                row.missing_files = row.nexus_files
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
