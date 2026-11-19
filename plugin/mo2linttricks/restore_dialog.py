"""Restore dialog: rebuild a synced profile into the current MO2 profile.

Steps, each a button so you stay in control of installers:
  1. Download missing - Nexus mods via MO2's download manager (the Nexus API only hands
     out direct links to Premium accounts; otherwise the file page opens in your browser
     and you click "Mod Manager Download"), url-sourced mods via their link.
  2. Install downloaded - runs MO2's normal installer for each archive, one at a time.
  3. Apply order - creates separators, sets mod priority and enabled state to match,
     and applies plugin load order for games that have one.
"""

from __future__ import annotations

import threading
from pathlib import Path

import mobase
from PyQt6.QtCore import QObject, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .core import restore
from .core.github import GitHub
from .core.inifile import as_int, read_ini

STATUS = {
    restore.INSTALLED: "installed",
    restore.INSTALL_ARCHIVE: "archive downloaded, ready to install",
    restore.DOWNLOAD_NEXUS: "missing (Nexus)",
    restore.DOWNLOAD_URL: "missing (url)",
    restore.CREATE_SEPARATOR: "separator to create",
    restore.GAME: "game/DLC, nothing to do",
    restore.MANUAL: "missing, no known source: add the archive to Downloads",
}


class RestoreRouter:
    """MO2 download callbacks cannot be removed, so register once and route to the open dialog."""

    def __init__(self, organizer: mobase.IOrganizer):
        self.dialog: RestoreDialog | None = None
        dm = organizer.downloadManager()
        dm.onDownloadComplete(self._complete)
        dm.onDownloadFailed(self._failed)

    def _complete(self, did: int) -> None:
        if self.dialog:
            self.dialog.download_complete(did)

    def _failed(self, did: int) -> None:
        if self.dialog:
            self.dialog.download_failed(did)


class _Loaded(QObject):
    done = pyqtSignal(object, str)  # payload dict, error


class RestoreDialog(QDialog):
    def __init__(self, organizer: mobase.IOrganizer, gh: GitHub, repo: str, router: RestoreRouter, parent: QWidget | None):
        super().__init__(parent)
        self._o = organizer
        self._gh = gh
        self._repo = repo
        self._router = router
        self._remote: dict[str, bytes | None] = {}
        self._rows: list[restore.PlanRow] = []
        self._downloads: dict[int, restore.PlanRow] = {}
        self._local_name: dict[str, str] = {}  # remote name -> name it got when installed

        self.setWindowTitle(f"Restore modlist from {repo}")
        self.resize(900, 600)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Synced profile:"))
        self._profiles = QComboBox()
        self._profiles.currentTextChanged.connect(self._load_profile)
        top.addWidget(self._profiles, 1)
        lay.addLayout(top)
        self._info = QLabel(f"Loading {repo}... Changes apply to the current profile: <b>{organizer.profileName()}</b>")
        self._info.setWordWrap(True)
        lay.addWidget(self._info)
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["#", "Mod", "Enabled", "Status"])
        header = self._table.horizontalHeader()
        if header:
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        lay.addWidget(self._table, 1)
        btns = QHBoxLayout()
        self._b_dl = QPushButton("1. Download missing")
        self._b_inst = QPushButton("2. Install downloaded")
        self._b_apply = QPushButton("3. Apply order && enabled state")
        close = QPushButton("Close")
        for b in (self._b_dl, self._b_inst, self._b_apply):
            b.setEnabled(False)
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(close)
        lay.addLayout(btns)
        self._b_dl.clicked.connect(self._download_missing)
        self._b_inst.clicked.connect(self._install_downloaded)
        self._b_apply.clicked.connect(self._apply)
        close.clicked.connect(self.accept)

        self._sig = _Loaded()
        self._sig.done.connect(self._loaded)
        router.dialog = self
        self.finished.connect(lambda _: setattr(router, "dialog", None))
        self._fetch(["mo2linttricks.json", "mods.json"], self._got_manifest)

    # --- remote loading (background thread) ---------------------------------------

    def _fetch(self, paths: list[str], then) -> None:
        self._then = then

        def run():
            try:
                self._sig.done.emit({p: self._gh.get_file(self._repo, p) for p in paths}, "")
            except Exception as e:
                self._sig.done.emit({}, str(e))

        threading.Thread(target=run, daemon=True).start()

    def _loaded(self, payload: dict, error: str) -> None:
        if error:
            self._info.setText(f"Could not read {self._repo}: {error}")
            return
        self._remote.update(payload)
        self._then()

    def _got_manifest(self) -> None:
        import json

        raw = self._remote.get("mo2linttricks.json")
        if not raw:
            self._info.setText(f"{self._repo} has no synced modlist yet.")
            return
        manifest = json.loads(raw)
        game = self._o.managedGame().gameShortName()
        if manifest.get("game") and manifest["game"].lower() != game.lower():
            self._info.setText(f"Repo is for {manifest['game']}, but this instance manages {game}.")
            return
        self._profiles.addItems(manifest.get("profiles", []))
        cur = self._o.profileName()
        if cur in manifest.get("profiles", []):
            self._profiles.setCurrentText(cur)

    def _load_profile(self, profile: str) -> None:
        if not profile:
            return
        base = f"profiles/{profile}/"
        self._fetch([base + "modlist.json", base + "plugins.txt", base + "loadorder.txt"], lambda: self._plan(profile))

    # --- planning -----------------------------------------------------------------

    def _plan(self, profile: str) -> None:
        installed = set(self._o.modList().allMods())
        self._rows = restore.plan(
            self._remote.get(f"profiles/{profile}/modlist.json") or b"[]",
            self._remote.get("mods.json") or b"{}",
            installed,
            Path(self._o.downloadsPath()),
        )
        self._render()
        counts: dict[str, int] = {}
        for r in self._rows:
            counts[r.action] = counts.get(r.action, 0) + 1
        summary = ", ".join(f"{n} {STATUS[a].split(',')[0]}" for a, n in counts.items())
        self._info.setText(
            f"Profile <b>{profile}</b>: {summary}.<br>Changes apply to the current profile: <b>{self._o.profileName()}</b>"
        )
        for b in (self._b_dl, self._b_inst, self._b_apply):
            b.setEnabled(True)

    def _render(self) -> None:
        self._table.setRowCount(len(self._rows))
        for i, r in enumerate(reversed(self._rows)):  # show like MO2: highest priority at the bottom
            vals = [str(r.priority), r.name, "yes" if r.enabled else "no", r.status or STATUS[r.action]]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(len(self._rows) - 1 - i, c, item)

    def _set(self, row: restore.PlanRow, status: str, action: str | None = None) -> None:
        if action:
            row.action = action
        row.status = status
        self._render()

    # --- 1. download ----------------------------------------------------------------

    def _download_missing(self) -> None:
        dm = self._o.downloadManager()
        nexus = self._o.managedGame().gameNexusName()
        for r in self._rows:
            if r.action == restore.DOWNLOAD_NEXUS and r.nexus_ids:
                modid = r.nexus_ids[0]
                for fid in list(r.missing_files):
                    did = dm.startDownloadNexusFile(modid, fid)
                    if did >= 0:
                        self._downloads[did] = r
                        self._set(r, f"downloading {len(r.missing_files)} file(s)...")
                    else:
                        self._open_page(r, nexus, fid)
            elif r.action == restore.DOWNLOAD_URL:
                url = r.info.get("downloadUrl") or ""
                if url:
                    did = dm.startDownloadURLs([url])
                    self._downloads[did] = r
                    self._set(r, "downloading...")
                elif r.info.get("url"):
                    QDesktopServices.openUrl(QUrl(r.info["url"]))
                    self._set(r, "opened source page: download it into MO2's Downloads")

    def _open_page(self, r: restore.PlanRow, nexus: str, fileid: int | None = None) -> None:
        page = r.nexus_page(nexus)
        if fileid and r.nexus_ids:
            page = f"https://www.nexusmods.com/{nexus}/mods/{r.nexus_ids[0]}?tab=files&file_id={fileid}"
        if page:
            QDesktopServices.openUrl(QUrl(page))
            n = len(r.missing_files)
            self._set(r, f"opened Nexus page: click Mod Manager Download ({n} file(s) needed)")

    def _row_for_download(self, did: int) -> restore.PlanRow | None:
        if did in self._downloads:
            return self._downloads.pop(did)
        # downloads started from the browser (nxm links) match by Nexus ids
        meta = read_ini(Path(self._o.downloadManager().downloadPath(did) + ".meta")).get("General", {})
        modid, fileid = as_int(meta.get("modID")), as_int(meta.get("fileID"))
        for r in self._rows:
            if r.action == restore.DOWNLOAD_NEXUS and r.nexus_ids and r.nexus_ids[0] == modid and fileid in r.missing_files:
                return r
        return None

    def download_complete(self, did: int) -> None:
        r = self._row_for_download(did)
        if not r:
            return
        path = self._o.downloadManager().downloadPath(did)
        meta = read_ini(Path(path + ".meta")).get("General", {})
        fid = as_int(meta.get("fileID"))
        if fid in r.missing_files:
            r.missing_files.remove(fid)
        r.archives.append(path)
        # keep the recorded install order (main files first, merged add-ons after)
        order = {f: i for i, f in enumerate(r.nexus_files)}
        r.archives.sort(key=lambda a: order.get(as_int(read_ini(Path(a + ".meta")).get("General", {}).get("fileID")), 999))
        if r.missing_files:
            self._set(r, f"downloaded {len(r.archives)}, {len(r.missing_files)} file(s) still needed")
        else:
            self._set(r, "downloaded, ready to install", restore.INSTALL_ARCHIVE)

    def download_failed(self, did: int) -> None:
        r = self._downloads.pop(did, None)
        if r and r.action == restore.DOWNLOAD_NEXUS:
            # typical for non-Premium accounts: the API refuses direct links
            self._open_page(r, self._o.managedGame().gameNexusName())
        elif r:
            self._set(r, "download failed")

    # --- 2. install -------------------------------------------------------------------

    def _install_downloaded(self) -> None:
        for r in self._rows:
            if r.action != restore.INSTALL_ARCHIVE or not r.archives:
                continue
            mod = None
            for i, archive in enumerate(r.archives):
                self._set(r, f"installing {i + 1}/{len(r.archives)}" + (" (choose Merge when asked)" if i else ""))
                mod = self._o.installMod(archive, self._local_name.get(r.name, r.name))
                if mod is None:
                    break
                self._local_name[r.name] = mod.name()
            if mod is None:
                self._set(r, "install cancelled or failed")
                continue
            self._set(r, "installed" + (f" as {mod.name()}" if mod.name() != r.name else ""), restore.INSTALLED)

    # --- 3. apply -----------------------------------------------------------------------

    def _apply(self) -> None:
        ml = self._o.modList()
        for r in self._rows:
            if r.action == restore.CREATE_SEPARATOR:
                if self._o.createMod(mobase.GuessedString(r.name)):
                    self._set(r, "created", restore.INSTALLED)
        existing = set(ml.allMods())
        ordered = [self._local_name.get(r.name, r.name) for r in self._rows if r.kind != "foreign"]
        ordered = [n for n in ordered if n in existing]
        for prio, name in enumerate(ordered):
            ml.setPriority(name, prio)
        enabled = [self._local_name.get(r.name, r.name) for r in self._rows if r.enabled and r.kind != "foreign"]
        disabled = [self._local_name.get(r.name, r.name) for r in self._rows if not r.enabled and r.kind != "foreign"]
        ml.setActive([n for n in enabled if n in existing], True)
        ml.setActive([n for n in disabled if n in existing], False)

        profile = self._profiles.currentText()
        plugins = restore.parse_plugins(
            self._remote.get(f"profiles/{profile}/plugins.txt") or b"",
            self._remote.get(f"profiles/{profile}/loadorder.txt") or b"",
        )
        missing = [r.name for r in self._rows if r.kind == "mod" and r.action not in (restore.INSTALLED,)]
        if plugins:
            self._o.onNextRefresh(lambda: self._apply_plugins(plugins), False)
            self._o.refresh(True)
        QMessageBox.information(
            self,
            "mo2linttricks",
            f"Applied order and enabled state for {len(ordered)} mods."
            + (f"<br>{len(missing)} mods are still missing." if missing else "")
            + ("<br>Plugin load order is applied after MO2 refreshes." if plugins else ""),
        )

    def _apply_plugins(self, plugins: list[tuple[str, bool]]) -> None:
        pl = self._o.pluginList()
        names = {n.lower(): n for n in pl.pluginNames()}
        prio = 0
        for name, active in plugins:
            real = names.get(name.lower())
            if not real:
                continue
            pl.setState(real, mobase.PluginState.ACTIVE if active else mobase.PluginState.INACTIVE)
            pl.setPriority(real, prio)
            prio += 1
