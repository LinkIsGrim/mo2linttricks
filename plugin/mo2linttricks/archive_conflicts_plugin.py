"""Cyberpunk 2077 .archive conflicts inside MO2.

MO2 only flags conflicts between loose files; two different .archive files that
replace the same game resource look conflict-free to it. This scans the archives
MO2's virtual data folder provides, applies the Archive Conflict Checker's rules
(rfuzzo/red4-conflicts) plus MO2/Cyberpunk-plugin load order, and shows:
  * a notification in MO2's problems indicator, and
  * Tools > mo2linttricks > Archive conflicts (winners/losers per file, per mod).
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import mobase
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal, qInfo, qWarning
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .core import archive_conflicts as ac
from .core.http import Transport
from .sync_plugin import NAME as SYNC_NAME, VERSION

GAME = "cyberpunk2077"
CP_PLUGIN = "Cyberpunk 2077 Support Plugin"
DIAG_NAME = "mo2linttricks Archive Conflicts"
PROBLEM_OVERRIDDEN = 0
PROBLEM_CONFLICTS = 1


class ArchiveModel:
    """Shared by the diagnose plugin and the tool: one scan, cached until something changes."""

    def __init__(self) -> None:
        self.organizer: mobase.IOrganizer | None = None
        self.report: ac.ConflictReport | None = None
        self.order_source = ""
        self.listeners: list = []
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.rescan)

    def attach(self, organizer: mobase.IOrganizer) -> None:
        if self.organizer:
            return
        self.organizer = organizer
        ml = organizer.modList()
        ml.onModStateChanged(lambda _s: self.invalidate())
        ml.onModMoved(lambda _n, _o, _p: self.invalidate())
        ml.onModInstalled(lambda _m: self.invalidate())
        ml.onModRemoved(lambda _n: self.invalidate())
        organizer.onProfileChanged(lambda _a, _b: self.invalidate())
        organizer.onFinishedRun(lambda _p, _c: self.invalidate())
        organizer.onUserInterfaceInitialized(lambda _w: self.invalidate())

    def applies(self) -> bool:
        return bool(self.organizer) and self.organizer.managedGame().gameShortName().lower() == GAME  # type: ignore[union-attr]

    def invalidate(self) -> None:
        if self.applies():
            self._timer.start(1500)

    # --- scanning ------------------------------------------------------------------

    def _layers(self) -> tuple[list[tuple[str, Path]], list[tuple[str, Path]]]:
        o = self.organizer
        assert o
        ml = o.modList()
        mods: list[tuple[str, Path]] = []
        for name in ml.allModsByProfilePriority():
            if not (ml.state(name) & mobase.ModState.ACTIVE):
                continue
            mod = ml.getMod(name)
            if mod is None or mod.isSeparator() or mod.isForeign():
                continue
            mods.append((name, Path(mod.absolutePath())))
        game = Path(o.managedGame().dataDirectory().absolutePath())
        layers = [("<game>", game)] + mods + [("<overwrite>", Path(o.overwritePath()))]
        return layers, mods

    def _modlist(self, mods: list[tuple[str, Path]]) -> tuple[list[str] | None, str]:
        o = self.organizer
        assert o
        try:
            enforce = bool(o.pluginSetting(CP_PLUGIN, "enforce_archive_load_order"))
            reverse = bool(o.pluginSetting(CP_PLUGIN, "reverse_archive_load_order"))
        except Exception:
            enforce = reverse = False
        if enforce:
            return ac.enforced_modlist(mods, reverse), (
                "MO2 mod priority" + (", reversed" if reverse else "") + " (Cyberpunk plugin enforces archive load order)"
            )
        found = o.findFiles("archive/pc/mod", "modlist.txt")
        if found:
            try:
                lines = Path(found[0]).read_text(encoding="utf-8", errors="replace").splitlines()
                return lines, f"archive/pc/mod/modlist.txt ({len(lines)} entries), then alphabetical"
            except OSError:
                pass
        return None, "alphabetical by file name (no modlist.txt)"

    def rescan(self) -> None:
        if not self.applies():
            return
        try:
            layers, mods = self._layers()
            modlist, self.order_source = self._modlist(mods)
            order = ac.load_order(ac.visible_archives(layers), modlist)
            report = ac.compute(order)
            report.names.update(load_name_cache(self.organizer))  # type: ignore[arg-type]
            self.report = report
        except Exception as e:
            qWarning(f"mo2linttricks archive conflicts: scan failed: {e}")
            return
        for cb in list(self.listeners):
            cb()

    # --- summaries -------------------------------------------------------------------

    def fully_overridden(self) -> list[ac.ArchiveResult]:
        if not self.report:
            return []
        return [r for r in self.report.order if r.files and len(r.loses) == r.files]


MODEL = ArchiveModel()


# --- vanilla name database (downloaded on request) --------------------------------------


def _data_dir(organizer: mobase.IOrganizer) -> Path:
    d = Path(organizer.pluginDataPath()) / "mo2linttricks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def name_db_path(organizer: mobase.IOrganizer) -> Path:
    return _data_dir(organizer) / "metadata-resources.zip"


def load_name_cache(organizer: mobase.IOrganizer) -> dict[int, str]:
    try:
        raw = json.loads((_data_dir(organizer) / "archive-names.json").read_text())
        return {int(k): v for k, v in raw.items()}
    except (OSError, ValueError):
        return {}


def save_name_cache(organizer: mobase.IOrganizer, names: dict[int, str]) -> None:
    cache = load_name_cache(organizer)
    cache.update(names)
    (_data_dir(organizer) / "archive-names.json").write_text(json.dumps({str(k): v for k, v in cache.items()}))


def download_name_db(dest: Path) -> None:
    r = Transport(timeout=300)("GET", ac.NAME_DB_URL)
    if r.status != 200:
        raise RuntimeError(f"download failed: HTTP {r.status}")
    digest = hashlib.sha256(r.body).hexdigest()
    if digest != ac.NAME_DB_SHA256:
        raise RuntimeError(f"checksum mismatch ({digest[:12]}...), not saved")
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(r.body)
    tmp.replace(dest)


# --- diagnose -------------------------------------------------------------------------------


class ArchiveConflictsDiagnose(mobase.IPluginDiagnose):
    def __init__(self) -> None:
        super().__init__()
        self._organizer: mobase.IOrganizer | None = None

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        MODEL.attach(organizer)
        MODEL.listeners.append(self._invalidate)
        return True

    def name(self) -> str:
        return DIAG_NAME

    def localizedName(self) -> str:
        return "mo2linttricks: Archive Conflicts"

    def author(self) -> str:
        return "LinkIsGrim"

    def description(self) -> str:
        return "Reports Cyberpunk 2077 .archive conflicts between mods (rules of rfuzzo's Archive Conflict Checker)."

    def version(self) -> mobase.VersionInfo:
        return VERSION

    def master(self) -> str:
        return SYNC_NAME

    def settings(self) -> list[mobase.PluginSetting]:
        return [
            mobase.PluginSetting("notify_conflicts", "Show a notification when any archive conflicts exist (not only fully overridden archives)", True),
        ]

    def _enabled(self) -> bool:
        return bool(self._organizer and self._organizer.isPluginEnabled(self) and MODEL.applies())

    def activeProblems(self) -> list[int]:
        if not self._enabled() or not MODEL.report:
            return []
        problems = []
        if MODEL.fully_overridden():
            problems.append(PROBLEM_OVERRIDDEN)
        notify = self._organizer.pluginSetting(DIAG_NAME, "notify_conflicts") if self._organizer else True
        if MODEL.report.conflicts and (notify is None or notify):
            problems.append(PROBLEM_CONFLICTS)
        return problems

    def shortDescription(self, key: int) -> str:
        if key == PROBLEM_OVERRIDDEN:
            return f"{len(MODEL.fully_overridden())} .archive file(s) are completely overridden by other mods"
        n = len(MODEL.report.conflicts) if MODEL.report else 0
        a = len(MODEL.report.conflicting_archives) if MODEL.report else 0
        return f"{n} game file(s) are replaced by more than one .archive ({a} archives involved)"

    def fullDescription(self, key: int) -> str:
        rep = MODEL.report
        if not rep:
            return ""
        if key == PROBLEM_OVERRIDDEN:
            rows = "".join(f"<li><b>{r.source.name}</b> ({r.source.mod}): all {r.files} files lose</li>" for r in MODEL.fully_overridden())
            return (
                "<p>Every file in these archives is also in an archive that loads earlier, so they have no effect. "
                "That's fine for an intentionally replaced mod, otherwise fix the load order.</p>"
                f"<ul>{rows}</ul><p>Load order: {MODEL.order_source}.</p>"
                "<p>Details: Tools &gt; mo2linttricks &gt; Archive conflicts.</p>"
            )
        top = sorted(rep.conflicting_archives, key=lambda r: -(len(r.wins) + len(r.loses)))[:15]
        rows = "".join(
            f"<li><b>{r.source.name}</b> ({r.source.mod}): wins {len(r.wins)}, loses {len(r.loses)}</li>" for r in top
        )
        return (
            "<p>Several .archive mods contain the same game files; the first one in load order wins. "
            "Often intended (patches, compatibility archives), but worth checking.</p>"
            f"<ul>{rows}</ul><p>Load order: {MODEL.order_source}.</p>"
            "<p>Details: Tools &gt; mo2linttricks &gt; Archive conflicts. "
            "Disable this notice in the plugin settings (notify_conflicts).</p>"
        )

    def hasGuidedFix(self, key: int) -> bool:
        return False

    def startGuidedFix(self, key: int) -> None:
        pass


# --- tool / dialog --------------------------------------------------------------------------


class _Done(QObject):
    done = pyqtSignal(str)


class ArchiveConflictsDialog(QDialog):
    def __init__(self, organizer: mobase.IOrganizer, parent: QWidget | None):
        super().__init__(parent)
        self._o = organizer
        self.setWindowTitle("Archive conflicts (Cyberpunk 2077)")
        self.resize(1000, 650)
        lay = QVBoxLayout(self)
        self._info = QLabel()
        self._info.setWordWrap(True)
        lay.addWidget(self._info)
        self._all = QCheckBox("Show archives without conflicts")
        self._all.toggled.connect(self._render)
        lay.addWidget(self._all)
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["#", "Archive / file", "Mod", "Wins", "Loses"])
        header = self._tree.header()
        if header:
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self._tree, 1)
        row = QHBoxLayout()
        refresh = QPushButton("Rescan")
        refresh.clicked.connect(self._rescan)
        self._names = QPushButton()
        self._names.clicked.connect(self._resolve_names)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(refresh)
        row.addWidget(self._names)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        self._sig = _Done()
        self._sig.done.connect(self._names_done)
        MODEL.rescan()
        self._render()

    def _rescan(self) -> None:
        MODEL.rescan()
        self._render()

    def _render(self) -> None:
        rep = MODEL.report
        self._tree.clear()
        has_db = name_db_path(self._o).exists()
        self._names.setText("Resolve file names" if has_db else f"Resolve file names (downloads {ac.NAME_DB_SIZE_MB} MB once)")
        if not rep:
            self._info.setText("No scan yet.")
            return
        conflicting = rep.conflicting_archives
        errors = [r for r in rep.order if r.error]
        self._info.setText(
            f"<b>{len(rep.order)}</b> archives, <b>{len(conflicting)}</b> with conflicts, "
            f"<b>{len(rep.conflicts)}</b> contested files. Load order: {MODEL.order_source}. "
            "First in load order wins."
            + (f"<br><span style='color:#c33'>{len(errors)} archive(s) could not be read.</span>" if errors else "")
        )
        for i, r in enumerate(rep.order):
            if not (r.wins or r.loses or r.error or self._all.isChecked()):
                continue
            top = QTreeWidgetItem([str(i), r.source.name, r.source.mod, str(len(r.wins)), str(len(r.loses))])
            if r.error:
                top.setText(1, f"{r.source.name}  [unreadable: {r.error}]")
            if r.files and len(r.loses) == r.files:
                top.setToolTip(1, "every file in this archive is overridden")
                top.setForeground(1, Qt.GlobalColor.red)
            for h in r.wins:
                losers = ", ".join(f"{rep.order[j].source.name} ({rep.order[j].source.mod})" for j in rep.conflicts[h][1:])
                top.addChild(QTreeWidgetItem(["", f"wins  {rep.name(h)}", f"over {losers}", "", ""]))
            for h in r.loses:
                w = rep.order[rep.conflicts[h][0]]
                top.addChild(QTreeWidgetItem(["", f"loses {rep.name(h)}", f"to {w.source.name} ({w.source.mod})", "", ""]))
            self._tree.addTopLevelItem(top)

    def _resolve_names(self) -> None:
        rep = MODEL.report
        if not rep:
            return
        wanted = {h for h in rep.conflicts if h not in rep.names}
        if not wanted:
            QMessageBox.information(self, "mo2linttricks", "All contested files already have names.")
            return
        db = name_db_path(self._o)
        if not db.exists():
            r = QMessageBox.question(
                self,
                "mo2linttricks",
                f"Download CDPR's resource name list ({ac.NAME_DB_SIZE_MB} MB, from rfuzzo/red4lib, checksum-verified) "
                f"to {db.parent}?",
            )
            if r != QMessageBox.StandardButton.Yes:
                return
        self._names.setEnabled(False)
        self._names.setText("Resolving...")

        def run() -> None:
            try:
                if not db.exists():
                    download_name_db(db)
                found = ac.resolve_names(db, wanted)
                save_name_cache(self._o, found)
                self._sig.done.emit(f"Resolved {len(found)} of {len(wanted)} file names.")
            except Exception as e:
                self._sig.done.emit(f"Failed: {e}")

        threading.Thread(target=run, daemon=True).start()

    def _names_done(self, message: str) -> None:
        self._names.setEnabled(True)
        qInfo(f"mo2linttricks archive conflicts: {message}")
        if MODEL.report:
            MODEL.report.names.update(load_name_cache(self._o))
        self._render()
        QMessageBox.information(self, "mo2linttricks", message + " Files still shown as hashes were added by mods whose archives don't store names.")


class ArchiveConflictsTool(mobase.IPluginTool):
    def __init__(self) -> None:
        super().__init__()
        self._organizer: mobase.IOrganizer | None = None
        self._parent: QWidget | None = None

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        MODEL.attach(organizer)
        return True

    def name(self) -> str:
        return "mo2linttricks Archive Conflicts Tool"

    def displayName(self) -> str:
        return "mo2linttricks/Archive conflicts (Cyberpunk 2077)"

    def tooltip(self) -> str:
        return "Which .archive mods replace the same game files, and which one wins"

    def description(self) -> str:
        return self.tooltip()

    def author(self) -> str:
        return "LinkIsGrim"

    def version(self) -> mobase.VersionInfo:
        return VERSION

    def master(self) -> str:
        return SYNC_NAME

    def settings(self) -> list[mobase.PluginSetting]:
        return []

    def icon(self) -> QIcon:
        return QIcon()

    def setParentWidget(self, parent: QWidget) -> None:
        self._parent = parent

    def display(self) -> None:
        if not self._organizer or not MODEL.applies():
            QMessageBox.information(self._parent, "mo2linttricks", "Archive conflicts are only checked for Cyberpunk 2077 instances.")
            return
        ArchiveConflictsDialog(self._organizer, self._parent).exec()
