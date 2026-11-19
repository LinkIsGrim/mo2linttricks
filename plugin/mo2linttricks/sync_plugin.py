"""Keeps the instance's modlist mirrored to a private GitHub repo."""

from __future__ import annotations

import re
import threading
import traceback
from pathlib import Path

import mobase
from PyQt6.QtCore import QCoreApplication, QObject, QTimer, pyqtSignal, qInfo, qWarning
from PyQt6.QtWidgets import QMainWindow

from .core import tokenstore
from .core.github import GitHub, GitHubError, describe_changes
from .core.snapshot import InstanceInfo, Snapshot, build

NAME = "mo2linttricks Modlist Sync"
VERSION = mobase.VersionInfo(0, 1, 0, mobase.ReleaseType.BETA)


class _Signals(QObject):
    finished = pyqtSignal(str, bool)  # message, ok


class ModlistSync(mobase.IPlugin):
    def __init__(self):
        super().__init__()
        self._organizer: mobase.IOrganizer | None = None
        self._window: QMainWindow | None = None
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self.sync_async("auto"))
        self._signals = _Signals()
        self._signals.finished.connect(self._on_finished)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pending: str | None = None
        self._dirty = False
        self._repo: str | None = None
        self._last_files: dict[str, bytes] | None = None
        self._warned_signed_out = False
        self.listeners: list = []  # callables(message, ok) e.g. the "Sync now" tool

    # --- IPlugin ---------------------------------------------------------------

    def name(self) -> str:
        return NAME

    def localizedName(self) -> str:
        return "mo2linttricks: Modlist Sync"

    def author(self) -> str:
        return "LinkIsGrim"

    def description(self) -> str:
        return (
            "Mirrors this instance's modlist (load order, enabled state, where each mod came from) "
            "to a private GitHub repository, automatically."
        )

    def version(self) -> mobase.VersionInfo:
        return VERSION

    def settings(self) -> list[mobase.PluginSetting]:
        return [
            mobase.PluginSetting("github_client_id", "Client ID of your GitHub OAuth App (device flow enabled)", ""),
            mobase.PluginSetting("repo_name", "Repository name; empty = mo2-mods-<game>", ""),
            mobase.PluginSetting("debounce_seconds", "Seconds of quiet after the last change before syncing", 30),
            mobase.PluginSetting("sync_downloads_meta", "Also sync downloads/*.meta (Nexus file ids, urls)", True),
        ]

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        ml = organizer.modList()
        ml.onModInstalled(lambda mod: self.mark_dirty(f"installed {mod.name()}"))
        ml.onModRemoved(lambda name: self.mark_dirty(f"removed {name}"))
        ml.onModStateChanged(lambda states: self.mark_dirty("mod state"))
        ml.onModMoved(lambda name, old, new: self.mark_dirty("mod order"))
        pl = organizer.pluginList()
        pl.onPluginMoved(lambda name, old, new: self.mark_dirty("plugin order"))
        pl.onPluginStateChanged(lambda states: self.mark_dirty("plugin state"))
        organizer.downloadManager().onDownloadComplete(lambda i: self.mark_dirty("download"))
        organizer.onProfileChanged(lambda old, new: self.mark_dirty("profile changed"))
        organizer.onProfileCreated(lambda p: self.mark_dirty("profile created"))
        organizer.onProfileRenamed(lambda p, old, new: self.mark_dirty("profile renamed"))
        organizer.onProfileRemoved(lambda name: self.mark_dirty("profile removed"))
        organizer.onAboutToRun(self._about_to_run)
        organizer.onUserInterfaceInitialized(self._ui_ready)
        return True

    # --- events ----------------------------------------------------------------

    def _active(self) -> bool:
        return bool(self._organizer and self._organizer.isPluginEnabled(self))

    def _ui_ready(self, window: QMainWindow) -> None:
        self._window = window
        app = QCoreApplication.instance()
        if app:
            app.aboutToQuit.connect(self._on_quit)
        # catch anything that changed while MO2 was closed
        QTimer.singleShot(5000, lambda: self.sync_async("startup"))

    def _about_to_run(self, path: str) -> bool:
        if self._dirty:
            self._timer.stop()
            self.sync_async("before launch")
        return True

    def mark_dirty(self, reason: str) -> None:
        if not self._active():
            return
        self._dirty = True
        secs = int(self._setting("debounce_seconds", 30) or 30)
        self._timer.start(max(5, secs) * 1000)

    def _on_quit(self) -> None:
        if not self._dirty or not self._active():
            return
        self._timer.stop()
        t = self.sync_async("exit")
        if t:
            t.join(20)

    # --- sync ------------------------------------------------------------------

    def _setting(self, key: str, default=None):
        v = self._organizer.pluginSetting(NAME, key) if self._organizer else None
        return default if v is None else v

    def repo_name(self) -> str:
        name = str(self._setting("repo_name", "") or "").strip()
        if not name:
            game = self._organizer.managedGame().gameShortName() if self._organizer else "game"
            name = f"mo2-mods-{game.lower()}"
        return re.sub(r"[^A-Za-z0-9._-]", "-", name)

    def instance_info(self) -> InstanceInfo:
        o = self._organizer
        assert o
        game = o.managedGame()
        return InstanceInfo(
            mods_dir=Path(o.modsPath()),
            profiles_dir=Path(o.profilePath()).parent,
            downloads_dir=Path(o.downloadsPath()),
            game_short_name=game.gameShortName(),
            game_name=game.gameName(),
            game_nexus_name=game.gameNexusName(),
            mo2_version=str(o.appVersion().canonicalString()),
            include_downloads=bool(self._setting("sync_downloads_meta", True)),
        )

    def client(self) -> GitHub | None:
        tok = tokenstore.load().get("token")
        return GitHub(tok) if tok else None

    def sync_async(self, reason: str) -> threading.Thread | None:
        if not self._active():
            return None
        if self.client() is None:
            if not self._warned_signed_out:
                qInfo("mo2linttricks: not signed in to GitHub; use Tools > mo2linttricks: Sign in to GitHub")
                self._warned_signed_out = True
            return None
        with self._lock:
            if self._thread and self._thread.is_alive():
                self._pending = reason
                return self._thread
            info = self.instance_info()  # touch the organizer on the UI thread only
            repo_name = self.repo_name()
            self._dirty = False
            self._thread = threading.Thread(target=self._worker, args=(info, repo_name, reason), daemon=True)
            self._thread.start()
            return self._thread

    def _worker(self, info: InstanceInfo, repo_name: str, reason: str) -> None:
        try:
            gh = self.client()
            assert gh
            if self._repo is None or not self._repo.endswith("/" + repo_name):
                self._repo, created = gh.ensure_repo(
                    repo_name, f"Mod Organizer 2 modlist for {info.game_name}, synced by mo2linttricks"
                )
                if created:
                    qInfo(f"mo2linttricks: created private repo {self._repo}")
            snap: Snapshot = build(info)
            if self._last_files is None:  # first sync this session: compare against GitHub
                self._last_files = gh.baseline(self._repo, snap)
            msg = describe_changes(self._last_files, snap.files)
            sha = gh.push_snapshot(self._repo, snap, msg)
            self._last_files = snap.files
            text = f"pushed {sha[:7]} to {self._repo}" if sha else f"{self._repo} already up to date"
            self._signals.finished.emit(text, True)
        except GitHubError as e:
            if e.status == 401:
                tokenstore.clear()
                self._signals.finished.emit("GitHub token rejected; sign in again", False)
            else:
                self._signals.finished.emit(f"sync failed: {e}", False)
        except Exception as e:  # never let a sync error take MO2 down
            qWarning(traceback.format_exc())
            self._signals.finished.emit(f"sync failed: {e}", False)

    def _on_finished(self, message: str, ok: bool) -> None:
        (qInfo if ok else qWarning)(f"mo2linttricks: {message}")
        bar = self._window.statusBar() if self._window else None
        if bar:
            bar.showMessage(f"mo2linttricks: {message}", 10000)
        if not ok:
            self._dirty = True
        for cb in list(self.listeners):
            cb(message, ok)
        with self._lock:
            pending, self._pending = self._pending, None
        if pending:
            self.sync_async(pending)
