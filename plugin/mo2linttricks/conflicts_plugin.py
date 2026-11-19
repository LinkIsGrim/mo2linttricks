"""Cyberpunk 2077 conflict resolver: .archive, TweakXL and redscript conflicts inside MO2.

MO2 only sees loose-file conflicts. Cyberpunk mods mostly clash elsewhere:
  * .archive files replacing the same game resource (first in load order wins);
  * TweakXL files setting the same TweakDB value (outcome often depends on directory order);
  * redscript mods @replaceMethod-ing the same function (only one replacement runs).

This scans MO2's mods, reports through MO2's problems indicator, and offers
Tools > mo2linttricks > Conflict resolver, which writes an xEdit-style patch mod
("mo2linttricks Patch", to be placed last) for TweakXL values. Archive order is
left to MO2's mod priority via the Cyberpunk plugin's own load-order settings.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import subprocess
import threading
from pathlib import Path

import mobase
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal, qInfo, qWarning
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .core import archive_conflicts as ac
from .core import redscript as rs
from .core import rules as R
from .core import tweaks as tw
from .core.http import Transport
from .modlist_badges import Issue, ModListBadges
from .sync_plugin import NAME as SYNC_NAME, VERSION

GAME = "cyberpunk2077"
CP_PLUGIN = "Cyberpunk 2077 Support Plugin"
DIAG_NAME = "mo2linttricks Conflicts"
PATCH_MOD = "mo2linttricks Patch"

PROBLEM_ARCHIVE_OVERRIDDEN = 0
PROBLEM_ARCHIVE_CONFLICTS = 1
PROBLEM_TWEAKS_UNRESOLVED = 2
PROBLEM_PATCH_NOT_LAST = 3
PROBLEM_SCRIPTS = 4
PROBLEM_RULE_ORDER = 5
PROBLEM_RULE_INCOMPATIBLE = 6
PROBLEM_RULE_MISSING = 7

RULES_URL = "https://raw.githubusercontent.com/LinkIsGrim/mo2linttricks/main/rules/cyberpunk2077.json"
# Out-of-process YAML parser (Windows exe; runs under Wine too). Python parsing holds the
# GIL that MO2's UI thread needs for Python game plugins, so heavy parsing lives elsewhere.
SCAN_HELPER = Path(__file__).resolve().parent / "bin" / "mo2lt-scan.exe"


def _run_helper(cmd: list[str], data: bytes) -> bytes:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run(cmd, input=data, capture_output=True, timeout=300, creationflags=flags, check=True)
    return r.stdout
USER_RULES = "mo2linttricks-rules.json"


class _Fetched(QObject):
    done = pyqtSignal(str)


class _Scanned(QObject):
    done = pyqtSignal(object)


class ConflictModel:
    """One scan shared by the diagnose plugin and the dialog, redone when mods change."""

    def __init__(self) -> None:
        self.organizer: mobase.IOrganizer | None = None
        self.report: ac.ConflictReport | None = None
        self.order_source = ""
        self.tweaks: tw.TweakScan = tw.TweakScan()
        self.scripts: list[rs.ScriptConflict] = []
        self.mods: list[tuple[str, Path]] = []
        self.local: list[R.LocalMod] = []  # every MO2 mod, lowest priority first
        self.rules: dict[str, R.ModRules] = {}
        self.rules_report: R.Report = R.Report()
        self.rules_status = "community rules not loaded yet"
        self._fetched = _Fetched()
        self._fetched.done.connect(self._rules_fetched)
        self.listeners: list = []
        self.mod_issues: dict[str, list[Issue]] = {}
        self.badges = ModListBadges(lambda name: self.mod_issues.get(name, []))
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.rescan_async)
        self._scanned = _Scanned()
        self._scanned.done.connect(self._apply)
        self._scanned.done.connect(lambda _o: self._scan_finished())
        self._scan_lock = threading.Lock()
        self._scan_thread: threading.Thread | None = None
        self._scan_pending = False
        self._parse_cache: dict | None = None

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
        organizer.onPluginSettingChanged(self._setting_changed)
        self.fetch_rules()

    def _setting_changed(self, plugin: str, _key: str, _old: object, _new: object) -> None:
        if plugin == CP_PLUGIN:
            self.invalidate()

    def applies(self) -> bool:
        o = self.organizer
        return bool(o) and o.managedGame().gameShortName().lower() == GAME  # type: ignore[union-attr]

    def invalidate(self) -> None:
        if self.applies():
            self._timer.start(1500)

    # --- MO2 state -----------------------------------------------------------------------

    def _active_mods(self) -> list[tuple[str, Path]]:
        o = self.organizer
        assert o
        ml = o.modList()
        out: list[tuple[str, Path]] = []
        for name in ml.allModsByProfilePriority():
            if name == PATCH_MOD or not (ml.state(name) & mobase.ModState.ACTIVE):
                continue
            mod = ml.getMod(name)
            if mod is None or mod.isSeparator() or mod.isForeign():
                continue
            out.append((name, Path(mod.absolutePath())))
        return out

    def archive_settings(self) -> tuple[bool, bool]:
        o = self.organizer
        assert o
        try:
            return bool(o.pluginSetting(CP_PLUGIN, "enforce_archive_load_order")), bool(
                o.pluginSetting(CP_PLUGIN, "reverse_archive_load_order")
            )
        except Exception:
            return False, False

    def _modlist(self, mods: list[tuple[str, Path]]) -> tuple[list[str] | None, str]:
        o = self.organizer
        assert o
        enforce, reverse = self.archive_settings()
        if enforce:
            how = "higher in MO2 wins" if reverse else "LOWER in MO2 wins (reverse_archive_load_order is off)"
            return ac.enforced_modlist(mods, reverse), f"MO2 mod priority, {how}"
        found = o.findFiles("archive/pc/mod", "modlist.txt")
        if found:
            try:
                lines = Path(found[0]).read_text(encoding="utf-8", errors="replace").splitlines()
                return lines, f"archive/pc/mod/modlist.txt ({len(lines)} entries), then alphabetical"
            except OSError:
                pass
        return None, "alphabetical by file name (no modlist.txt)"

    def _archive_layers(self, mods: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
        o = self.organizer
        assert o
        game = Path(o.managedGame().dataDirectory().absolutePath())
        return [("<game>", game)] + mods + [("<overwrite>", Path(o.overwritePath()))]

    # --- load-order rules ----------------------------------------------------------------------

    def community_rules_path(self) -> Path:
        assert self.organizer
        return _data_dir(self.organizer) / "rules-cyberpunk2077.json"

    def user_rules_path(self) -> Path:
        assert self.organizer
        return Path(self.organizer.basePath()) / USER_RULES

    def fetch_rules(self) -> None:
        """Download the community rules (plain HTTPS GET), keep the cached copy on failure."""
        if not self.organizer:
            return
        url = str(self.organizer.pluginSetting(DIAG_NAME, "rules_url") or RULES_URL)
        dest = self.community_rules_path()

        def run() -> None:
            try:
                r = Transport(timeout=20)("GET", url, {"Cache-Control": "no-cache"})
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                data = json.loads(r.body)
                if not R.parse(data, "community"):
                    raise RuntimeError("no usable rules in file")
                dest.write_bytes(r.body)
                self._fetched.done.emit(f"community rules updated ({len(data.get('mods', {}))} mods)")
            except Exception as e:
                self._fetched.done.emit(f"could not update community rules ({e}); using cached copy" if dest.exists() else f"could not download community rules ({e})")

        threading.Thread(target=run, daemon=True).start()

    def _rules_fetched(self, status: str) -> None:
        self.rules_status = status
        qInfo(f"mo2linttricks: {status}")
        self.invalidate()

    def _local_mods(self) -> list[R.LocalMod]:
        o = self.organizer
        assert o
        ml = o.modList()
        out = []
        for name in ml.allModsByProfilePriority():
            mod = ml.getMod(name)
            enabled = bool(ml.state(name) & mobase.ModState.ACTIVE)
            if mod is None or mod.isSeparator() or mod.isForeign():
                out.append(R.LocalMod(name, 0, set(), enabled))
            else:
                out.append(R.read_local_mod(name, Path(mod.absolutePath()), enabled))
        return out

    # --- scanning: collect MO2 state on the UI thread, compute anywhere, apply on the UI thread ---

    def _cache_path(self) -> Path:
        assert self.organizer
        return _data_dir(self.organizer) / "tweak-parse-cache.pickle"

    def _load_cache(self) -> dict:
        if self._parse_cache is None:
            try:
                with open(self._cache_path(), "rb") as f:
                    data = pickle.load(f)  # our own cache file, written below
                self._parse_cache = data if isinstance(data, dict) else {}
            except Exception:
                self._parse_cache = {}
        return self._parse_cache

    def _collect(self) -> dict:
        mods = self._active_mods()
        modlist, order_source = self._modlist(mods)
        return {
            "local": self._local_mods(),
            "mods": mods,
            "modlist": modlist,
            "order_source": order_source,
            "layers": self._archive_layers(mods),
            "community": self.community_rules_path(),
            "user": self.user_rules_path(),
            "names": load_name_cache(self.organizer),  # type: ignore[arg-type]
            "cache_path": self._cache_path(),
            "cache": self._load_cache(),
        }

    @staticmethod
    def _compute(state: dict) -> dict:
        out: dict = {"local": state["local"], "mods": state["mods"], "order_source": state["order_source"]}
        try:
            rules = R.merge(R.load_file(state["community"], "community"), R.load_file(state["user"], "user"))
            out["rules"] = rules
            out["rules_report"] = R.check(state["local"], rules)
        except Exception as e:
            out["error"] = f"rules check failed: {e}"
        try:
            report = ac.compute(ac.load_order(ac.visible_archives(state["layers"]), state["modlist"]))
            report.names.update(state["names"])
            out["report"] = report
            cache = state["cache"]
            before = {k: v[:2] for k, v in cache.items()}
            if SCAN_HELPER.exists():
                try:
                    tw.prefill_with_helper(state["mods"], cache, SCAN_HELPER, _run_helper)
                except Exception as e:  # fall back to parsing in Python
                    qWarning(f"mo2linttricks conflicts: mo2lt-scan failed ({e}); parsing in Python")
            out["tweaks"] = tw.scan(state["mods"], cache)
            out["scripts"] = rs.scan(state["mods"])
            live = {str(p) for _, root in state["mods"] for p in (root / "r6" / "tweaks").rglob("*")}
            for k in [k for k in cache if k not in live]:
                del cache[k]  # files of removed/disabled mods
            if {k: v[:2] for k, v in cache.items()} != before:  # something was (re)parsed or removed
                tmp = state["cache_path"].with_suffix(".tmp")
                with open(tmp, "wb") as f:
                    pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)
                tmp.replace(state["cache_path"])
        except Exception as e:
            out["error"] = f"scan failed: {e}"
        return out

    def _apply(self, out: dict) -> None:
        if out.get("error"):
            qWarning(f"mo2linttricks conflicts: {out['error']}")
        self.local = out["local"]
        self.mods = out["mods"]
        self.order_source = out["order_source"]
        for key in ("rules", "rules_report", "report", "tweaks", "scripts"):
            if key in out:
                setattr(self, key, out[key])
        try:
            self.mod_issues = self._build_issues()
        except Exception as e:
            qWarning(f"mo2linttricks conflicts: mod list badges: {e}")
        self.badges.refresh()
        for cb in list(self.listeners):
            cb()

    def _build_issues(self) -> dict[str, list[Issue]]:
        """Per-mod summary for the mod-list badges and their tooltips."""
        out: dict[str, list[Issue]] = {}

        def add(mod: str, issue: Issue) -> None:
            out.setdefault(mod, []).append(issue)

        rep = self.report
        if rep:
            overridden = {id(r) for r in self.fully_overridden()}
            for r in rep.order:
                wins, loses = rep.real_counts(r)
                if not (wins or loses):
                    continue
                rivals = sorted(
                    {rep.order[j].source.mod for h in r.wins + r.loses if h not in rep.identical for j in rep.conflicts[h]}
                    - {r.source.mod}
                )
                text = f"{r.source.name}: wins {wins}, loses {loses} (vs {', '.join(rivals[:4])}{'...' if len(rivals) > 4 else ''})"
                if id(r) in overridden:
                    text += " - every file overridden, the archive has no effect"
                add(r.source.mod, Issue("A", id(r) in overridden, text))
        root = self.patch_root()
        patched = tw.patched_keys(root) if root else set()
        _unhandled, outdated = self.patch_state()
        per_mod: dict[str, list[str]] = {}
        unresolved: dict[str, int] = {}
        for c in self.tweaks.real:
            done = c.key in patched and not outdated
            for v in c.values:
                per_mod.setdefault(v.mod, []).append(c.key)
                if not done:
                    unresolved[v.mod] = unresolved.get(v.mod, 0) + 1
        for mod, keys in per_mod.items():
            n_open = unresolved.get(mod, 0)
            status = f"{n_open} not resolved by the patch" if n_open else "all resolved by mo2linttricks Patch"
            add(mod, Issue("T", bool(n_open), f"{len(keys)} TweakDB value(s) also set by other mods ({status}), e.g. {keys[0]}"))
        for sc in self.scripts:
            if sc.identical:
                continue
            mods = sorted({r.mod for r in sc.replacements})
            for m in mods:
                add(m, Issue("S", True, f"{sc.target} is also replaced by {', '.join(x for x in mods if x != m)}; only one runs"))
        rr = self.rules_report
        for e in rr.violations:
            add(e.after, Issue("R", True, f"should be below {e.before} ({e.rules.note or e.rule_key})"))
            add(e.before, Issue("R", True, f"should be above {e.after}"))
        for b in rr.bottom_violations:
            add(b, Issue("R", True, "should be at the bottom of the list"))
        for a, b, note in rr.incompatible:
            add(a, Issue("R", True, f"incompatible with {b}: {note}"))
            add(b, Issue("R", True, f"incompatible with {a}: {note}"))
        for m, req in rr.missing:
            add(m, Issue("R", True, f"requires {req}, not installed or not enabled"))
        return out

    def rescan(self) -> None:
        """Scan now and wait (dialog/user actions; fast once the parse cache is warm)."""
        if not self.applies():
            return
        with self._scan_lock:
            state = self._collect()
            self._apply(self._compute(state))

    def rescan_async(self) -> None:
        """Scan in a background thread so MO2's UI never blocks (startup, mod list changes)."""
        if not self.applies():
            return
        if self._scan_thread and self._scan_thread.is_alive():
            self._scan_pending = True
            return
        state = self._collect()

        def run() -> None:
            with self._scan_lock:
                result = self._compute(state)
            self._scanned.done.emit(result)

        self._scan_thread = threading.Thread(target=run, daemon=True)
        self._scan_thread.start()

    def _scan_finished(self) -> None:
        if self._scan_pending:
            self._scan_pending = False
            self.invalidate()

    # --- archive order preview --------------------------------------------------------------

    def preview_mo2_order(self) -> list[tuple[str, str, str]]:
        """Contested files whose winning archive changes under 'MO2 priority, higher wins'.

        Returns [(file, winner now, winner after)].
        """
        if not self.report:
            return []
        layers = self._archive_layers(self.mods)
        after = ac.compute(ac.load_order(ac.visible_archives(layers), ac.enforced_modlist(self.mods, True)))
        out = []
        for h, idxs in self.report.conflicts.items():
            if h in self.report.identical:
                continue
            now = self.report.order[idxs[0]].source
            later = after.order[after.conflicts[h][0]].source if h in after.conflicts else now
            if now.name != later.name:
                out.append((self.report.name(h), f"{now.name} ({now.mod})", f"{later.name} ({later.mod})"))
        return sorted(out)

    # --- patch -------------------------------------------------------------------------------

    def patch_root(self) -> Path | None:
        o = self.organizer
        mod = o.modList().getMod(PATCH_MOD) if o else None
        return Path(mod.absolutePath()) if mod else None

    def patch_state(self) -> tuple[int, bool]:
        """(number of real TweakXL conflicts not handled by the patch as written, patch is outdated)."""
        root = self.patch_root()
        if root is None or not self.organizer or not (self.organizer.modList().state(PATCH_MOD) & mobase.ModState.ACTIVE):
            return len(self.tweaks.real), False
        decisions = tw.load_decisions(root)
        wanted, _ = tw.build_patch(self.tweaks, decisions)
        current = root / tw.PATCH_DIR / tw.PATCH_FILE
        have = current.read_text(encoding="utf-8") if current.is_file() else ""
        patched = tw.patched_keys(root)
        unhandled = sum(1 for c in self.tweaks.real if c.key not in patched and not decisions.get(c.key, {}).get("skip"))
        return unhandled, wanted != have

    def patch_not_last(self) -> bool:
        o = self.organizer
        if not o or self.patch_root() is None:
            return False
        ml = o.modList()
        order = list(ml.allModsByProfilePriority())
        if PATCH_MOD not in order:
            return False
        for name in order[order.index(PATCH_MOD) + 1 :]:
            mod = ml.getMod(name)
            if mod and not mod.isSeparator() and not mod.isForeign():
                return True
        return False

    def fully_overridden(self) -> list[ac.ArchiveResult]:
        rep = self.report
        if not rep:
            return []
        return [r for r in rep.order if r.files and len(r.loses) == r.files and rep.real_counts(r)[1]]


MODEL = ConflictModel()


# --- vanilla name database (downloaded on request) ---------------------------------------------


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


# --- diagnose ------------------------------------------------------------------------------------


class ConflictsDiagnose(mobase.IPluginDiagnose):
    def __init__(self) -> None:
        super().__init__()
        self._organizer: mobase.IOrganizer | None = None

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        MODEL.attach(organizer)
        MODEL.listeners.append(self._invalidate)
        organizer.onUserInterfaceInitialized(self._ui_ready)
        return True

    def _ui_ready(self, window) -> None:
        enabled = self._organizer.pluginSetting(DIAG_NAME, "show_in_mod_list") if self._organizer else True
        if (enabled is None or enabled) and MODEL.applies():
            if not MODEL.badges.install(window):
                qWarning("mo2linttricks: could not find MO2's mod list view; badges disabled")

    def name(self) -> str:
        return DIAG_NAME

    def localizedName(self) -> str:
        return "mo2linttricks: Conflicts"

    def author(self) -> str:
        return "LinkIsGrim"

    def description(self) -> str:
        return "Reports Cyberpunk 2077 .archive, TweakXL and redscript conflicts between mods."

    def version(self) -> mobase.VersionInfo:
        return VERSION

    def master(self) -> str:
        return SYNC_NAME

    def settings(self) -> list[mobase.PluginSetting]:
        return [
            mobase.PluginSetting("notify_archive_conflicts", "Notify about any .archive conflicts, not only fully overridden archives", True),
            mobase.PluginSetting("rules_url", "Community load-order rules (JSON, fetched on startup)", RULES_URL),
            mobase.PluginSetting("show_in_mod_list", "Show conflict badges (A/T/S/R) in the mod list's Conflicts column (restart MO2)", True),
        ]

    def _enabled(self) -> bool:
        return bool(self._organizer and self._organizer.isPluginEnabled(self) and MODEL.applies())

    def activeProblems(self) -> list[int]:
        if not self._enabled() or not MODEL.report:
            return []
        problems = []
        if MODEL.fully_overridden():
            problems.append(PROBLEM_ARCHIVE_OVERRIDDEN)
        notify = self._organizer.pluginSetting(DIAG_NAME, "notify_archive_conflicts") if self._organizer else True
        if MODEL.report.real_conflicts and (notify is None or notify):
            problems.append(PROBLEM_ARCHIVE_CONFLICTS)
        unhandled, outdated = MODEL.patch_state()
        if unhandled or outdated:
            problems.append(PROBLEM_TWEAKS_UNRESOLVED)
        if MODEL.patch_not_last():
            problems.append(PROBLEM_PATCH_NOT_LAST)
        if any(not c.identical for c in MODEL.scripts):
            problems.append(PROBLEM_SCRIPTS)
        rr = MODEL.rules_report
        if rr.violations or rr.bottom_violations:
            problems.append(PROBLEM_RULE_ORDER)
        if rr.incompatible:
            problems.append(PROBLEM_RULE_INCOMPATIBLE)
        if rr.missing:
            problems.append(PROBLEM_RULE_MISSING)
        return problems

    def shortDescription(self, key: int) -> str:
        rep = MODEL.report
        if key == PROBLEM_ARCHIVE_OVERRIDDEN:
            return f"{len(MODEL.fully_overridden())} .archive file(s) are completely overridden by other mods"
        if key == PROBLEM_ARCHIVE_CONFLICTS:
            n = len(rep.real_conflicts) if rep else 0
            a = len(rep.really_conflicting_archives) if rep else 0
            return f"{n} game file(s) are replaced differently by more than one .archive ({a} archives involved)"
        if key == PROBLEM_TWEAKS_UNRESOLVED:
            unhandled, outdated = MODEL.patch_state()
            if unhandled:
                return f"{unhandled} TweakXL value(s) are set differently by several mods and not resolved by a patch"
            return "The mo2linttricks Patch is out of date"
        if key == PROBLEM_PATCH_NOT_LAST:
            return f"\"{PATCH_MOD}\" should be the last mod in the list"
        rr = MODEL.rules_report
        if key == PROBLEM_RULE_ORDER:
            return f"{len(rr.violations) + len(rr.bottom_violations)} load-order rule(s) are broken by the current mod order"
        if key == PROBLEM_RULE_INCOMPATIBLE:
            return f"{len(rr.incompatible)} pair(s) of enabled mods are marked incompatible"
        if key == PROBLEM_RULE_MISSING:
            return f"{len(rr.missing)} mod requirement(s) are not installed or not enabled"
        n = sum(1 for c in MODEL.scripts if not c.identical)
        return f"{n} redscript function(s) are replaced by more than one mod"

    def fullDescription(self, key: int) -> str:
        rep = MODEL.report
        tool = "<p>Details and fixes: Tools &gt; mo2linttricks &gt; Conflict resolver.</p>"
        if not rep:
            return ""
        if key == PROBLEM_ARCHIVE_OVERRIDDEN:
            rows = "".join(f"<li><b>{r.source.name}</b> ({r.source.mod}): all {r.files} files lose</li>" for r in MODEL.fully_overridden())
            return (
                "<p>Every file in these archives is also in an archive that loads earlier, so they have no effect.</p>"
                f"<ul>{rows}</ul><p>Load order: {MODEL.order_source}.</p>" + tool
            )
        if key == PROBLEM_ARCHIVE_CONFLICTS:
            arch = rep.really_conflicting_archives
            top = sorted(arch, key=lambda r: -sum(rep.real_counts(r)))[:15]
            rows = "".join(
                f"<li><b>{r.source.name}</b> ({r.source.mod}): wins {rep.real_counts(r)[0]}, loses {rep.real_counts(r)[1]}</li>" for r in top
            )
            more = f"<p>...and {len(arch) - len(top)} more.</p>" if len(arch) > len(top) else ""
            same = f" {len(rep.identical)} more contested file(s) are identical everywhere and not counted." if rep.identical else ""
            return (
                "<p>Several .archive mods contain different versions of the same game files; the first one in load order "
                f"wins.{same}</p><ul>{rows}</ul>{more}<p>Load order: {MODEL.order_source}.</p>" + tool
                + "<p>Turn this notice off with the plugin setting notify_archive_conflicts.</p>"
            )
        if key == PROBLEM_TWEAKS_UNRESOLVED:
            items = "".join(
                f"<li><b>{c.key}</b>: " + ", ".join(f"{v.mod} = {v.display[:40]}" for v in c.values) + "</li>"
                for c in MODEL.tweaks.real[:15]
            )
            return (
                "<p>TweakXL applies these files in directory order, so which mod's value the game uses is often arbitrary. "
                f"Write a patch to pick one explicitly (default: the mod highest in MO2's list).</p><ul>{items}</ul>" + tool
            )
        if key == PROBLEM_PATCH_NOT_LAST:
            return f"<p>Drag <b>{PATCH_MOD}</b> to the bottom of the mod list so nothing overrides it.</p>"
        rr = MODEL.rules_report
        if key == PROBLEM_RULE_ORDER:
            items = "".join(
                f"<li><b>{e.after}</b> should be below <b>{e.before}</b> ({e.rules.origin}: {e.rules.note or e.rule_key})</li>" for e in rr.violations
            ) + "".join(f"<li><b>{b}</b> should be at the bottom</li>" for b in rr.bottom_violations)
            return f"<p>Lower in MO2's list wins.</p><ul>{items}</ul><p>Fix: Conflict resolver &gt; Load order rules &gt; Sort.</p>"
        if key == PROBLEM_RULE_INCOMPATIBLE:
            items = "".join(f"<li><b>{a}</b> and <b>{b}</b>: {note}</li>" for a, b, note in rr.incompatible)
            return f"<p>Disable one of each pair.</p><ul>{items}</ul>"
        if key == PROBLEM_RULE_MISSING:
            items = "".join(
                f"<li><b>{m}</b> needs {req}"
                + (f" (<a href='https://www.nexusmods.com/cyberpunk2077/mods/{req.split(':')[1]}'>Nexus</a>)" if req.startswith("nexus:") else "")
                + "</li>"
                for m, req in rr.missing
            )
            return f"<ul>{items}</ul>"
        items = "".join(
            f"<li><b>{c.target}</b>: " + ", ".join(sorted({r.mod for r in c.replacements})) + "</li>" for c in MODEL.scripts if not c.identical
        )
        return (
            "<p>These mods each replace the same game function; only one replacement runs, so the other mods' "
            f"changes to it are lost. They usually need a compatibility patch from the authors.</p><ul>{items}</ul>" + tool
        )

    def hasGuidedFix(self, key: int) -> bool:
        return False

    def startGuidedFix(self, key: int) -> None:
        pass


# --- dialog ---------------------------------------------------------------------------------------


class _Done(QObject):
    done = pyqtSignal(str)


class TweakTab(QWidget):
    LEAVE = "(leave to TweakXL: unpatched)"
    CUSTOM = "custom value..."

    def __init__(self, organizer: mobase.IOrganizer, parent: QWidget):
        super().__init__(parent)
        self._o = organizer
        lay = QVBoxLayout(self)
        self._info = QLabel()
        self._info.setWordWrap(True)
        lay.addWidget(self._info)
        self._same = QCheckBox("Show values that are identical in every mod")
        self._same.toggled.connect(self.refresh_view)
        lay.addWidget(self._same)
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["TweakDB value", "Set by (lowest MO2 priority first)", "Game uses today", "Patch writes"])
        header = self._table.horizontalHeader()
        if header:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self._table, 1)
        row = QHBoxLayout()
        write = QPushButton("Write patch")
        write.clicked.connect(self._write)
        row.addStretch(1)
        row.addWidget(write)
        lay.addLayout(row)
        root = MODEL.patch_root()
        self._decisions: dict[str, dict] = tw.load_decisions(root) if root else {}
        self.refresh_view()

    def refresh_view(self) -> None:
        show = MODEL.tweaks.conflicts if self._same.isChecked() else MODEL.tweaks.real
        unhandled, outdated = MODEL.patch_state()
        root = MODEL.patch_root()
        self._info.setText(
            f"<b>{len(MODEL.tweaks.real)}</b> TweakDB values are set differently by several mods "
            f"({len(MODEL.tweaks.conflicts) - len(MODEL.tweaks.real)} more set identically). "
            "TweakXL reads files in directory order, so without a patch the result is often arbitrary. "
            f"The patch (<i>{PATCH_MOD}</i>, keep it last) sets each value explicitly; by default to the "
            "value of the mod highest in MO2's list."
            + (f"<br>Patch: {'not created yet' if root is None else ('out of date' if outdated else 'up to date')}"
               + (f", {unhandled} value(s) not covered" if unhandled else "") + ".")
            + (f"<br><span style='color:#c33'>{len(MODEL.tweaks.errors)} tweak file(s) could not be parsed.</span>" if MODEL.tweaks.errors else "")
        )
        self._table.setRowCount(len(show))
        for i, c in enumerate(show):
            self._table.setItem(i, 0, QTableWidgetItem(c.key))
            vals = QTableWidgetItem(" | ".join(f"{v.mod}: {v.display[:60]}" for v in c.values))
            vals.setToolTip("\n".join(f"{v.mod} ({v.file}): {v.display}" for v in c.values))
            self._table.setItem(i, 1, vals)
            w = c.runtime_winner()
            self._table.setItem(i, 2, QTableWidgetItem(w.mod if w else "undefined (directory order)"))
            for col in range(3):
                item = self._table.item(i, col)
                if item:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if c.identical:
                self._table.setItem(i, 3, QTableWidgetItem("(identical, nothing to do)"))
                continue
            box = QComboBox()
            default = c.default_choice()
            box.addItem(f"MO2 priority: {default.mod}" if default else "MO2 priority: (no patchable value)", None)
            for v in reversed(c.values):
                if v.patchable:
                    box.addItem(f"{v.mod}: {v.display[:50]}", {"mod": v.mod})
            box.addItem(self.LEAVE, {"skip": True})
            box.addItem(self.CUSTOM, "custom")
            d = self._decisions.get(c.key)
            if d and "custom" in d:
                box.insertItem(box.count() - 1, f"custom: {d['custom']}", d)
            for idx in range(box.count()):
                if box.itemData(idx) == d:
                    box.setCurrentIndex(idx)
            box.currentIndexChanged.connect(lambda _i, b=box, k=c.key: self._choose(k, b))
            self._table.setCellWidget(i, 3, box)

    def _choose(self, key: str, box: QComboBox) -> None:
        data = box.currentData()
        if data == "custom":
            text, ok = QInputDialog.getText(self, "Custom value", f"YAML value for {key}:")
            if not ok or not text.strip():
                box.setCurrentIndex(0)
                return
            try:
                tw.yaml.safe_load(text)
            except Exception as e:
                QMessageBox.warning(self, "mo2linttricks", f"Not valid YAML: {e}")
                box.setCurrentIndex(0)
                return
            self._decisions[key] = {"custom": text.strip()}
            self.refresh_view()
            return
        if data is None:
            self._decisions.pop(key, None)
        else:
            self._decisions[key] = data

    def _write(self) -> None:
        o = self._o
        root = MODEL.patch_root()
        if root is None:
            mod = o.createMod(mobase.GuessedString(PATCH_MOD))
            if mod is None:
                QMessageBox.warning(self, "mo2linttricks", f"Could not create the mod \"{PATCH_MOD}\".")
                return
            root = Path(mod.absolutePath())
        n = tw.write_patch(root, MODEL.tweaks, self._decisions)
        o.modList().setActive(PATCH_MOD, True)
        o.refresh(True)
        MODEL.rescan()
        self.refresh_view()
        msg = f"Wrote {n} value(s) to {PATCH_MOD}."
        if MODEL.patch_not_last():
            msg += f"<br><br>Now drag <b>{PATCH_MOD}</b> to the bottom of the mod list."
        QMessageBox.information(self, "mo2linttricks", msg)


class ArchiveTab(QWidget):
    def __init__(self, organizer: mobase.IOrganizer, parent: QWidget):
        super().__init__(parent)
        self._o = organizer
        lay = QVBoxLayout(self)
        self._info = QLabel()
        self._info.setWordWrap(True)
        lay.addWidget(self._info)
        opts = QHBoxLayout()
        self._all = QCheckBox("Show archives without conflicts")
        self._all.toggled.connect(self.refresh_view)
        self._same = QCheckBox("Show identical files")
        self._same.toggled.connect(self.refresh_view)
        opts.addWidget(self._all)
        opts.addWidget(self._same)
        opts.addStretch(1)
        lay.addLayout(opts)
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["#", "Archive / file", "Mod", "Wins", "Loses", "Identical"])
        header = self._tree.header()
        if header:
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self._tree, 1)
        row = QHBoxLayout()
        self._order = QPushButton("Use MO2 mod priority for archive order...")
        self._order.clicked.connect(self._use_mo2_order)
        self._names = QPushButton()
        self._names.clicked.connect(self._resolve_names)
        row.addWidget(self._order)
        row.addWidget(self._names)
        row.addStretch(1)
        lay.addLayout(row)
        self._sig = _Done()
        self._sig.done.connect(self._names_done)
        self.refresh_view()

    def refresh_view(self) -> None:
        rep = MODEL.report
        self._tree.clear()
        enforce, reverse = MODEL.archive_settings()
        self._order.setVisible(not (enforce and reverse))
        has_db = name_db_path(self._o).exists()
        self._names.setText("Resolve file names" if has_db else f"Resolve file names (downloads {ac.NAME_DB_SIZE_MB} MB once)")
        if not rep:
            self._info.setText("No scan yet.")
            return
        errors = [r for r in rep.order if r.error]
        self._info.setText(
            f"<b>{len(rep.order)}</b> archives, <b>{len(rep.really_conflicting_archives)}</b> with real conflicts, "
            f"<b>{len(rep.real_conflicts)}</b> files that differ (+{len(rep.identical)} identical duplicates). "
            f"Load order: {MODEL.order_source}. First in load order wins."
            + (f"<br><span style='color:#c33'>{len(errors)} archive(s) could not be read.</span>" if errors else "")
        )
        show_same = self._same.isChecked()
        grey = Qt.GlobalColor.gray
        for i, r in enumerate(rep.order):
            wins, loses = rep.real_counts(r)
            same = len(r.wins) + len(r.loses) - wins - loses
            if not (wins or loses or r.error or self._all.isChecked() or (show_same and same)):
                continue
            top = QTreeWidgetItem([str(i), r.source.name, r.source.mod, str(wins), str(loses), str(same)])
            if r.error:
                top.setText(1, f"{r.source.name}  [unreadable: {r.error}]")
            if not (wins or loses):
                for c in range(6):
                    top.setForeground(c, grey)
            for h in r.wins + r.loses:
                if h in rep.identical and not show_same:
                    continue
                if h in r.wins:
                    others = ", ".join(f"{rep.order[j].source.name} ({rep.order[j].source.mod})" for j in rep.conflicts[h][1:])
                    child = QTreeWidgetItem(["", f"wins  {rep.name(h)}", f"over {others}", "", "", "same" if h in rep.identical else ""])
                else:
                    w = rep.order[rep.conflicts[h][0]].source
                    child = QTreeWidgetItem(["", f"loses {rep.name(h)}", f"to {w.name} ({w.mod})", "", "", "same" if h in rep.identical else ""])
                if h in rep.identical:
                    child.setForeground(1, grey)
                top.addChild(child)
            self._tree.addTopLevelItem(top)

    def _use_mo2_order(self) -> None:
        changes = MODEL.preview_mo2_order()
        rows = "".join(f"<li>{f}<br>&nbsp;&nbsp;now: {a}<br>&nbsp;&nbsp;then: {b}</li>" for f, a, b in changes[:25])
        more = f"<p>...and {len(changes) - 25} more.</p>" if len(changes) > 25 else ""
        text = (
            "<p>Turns on the Cyberpunk plugin's <i>enforce_archive_load_order</i> and <i>reverse_archive_load_order</i>: "
            "before each launch it writes archive/pc/mod/modlist.txt from MO2's mod priority, so the mod "
            "<b>higher in MO2's list wins</b>, same as for loose files.</p>"
            + (f"<p>With your current mod order these contested files would change winner:</p><ul>{rows}</ul>{more}"
               "<p>Reorder mods in MO2 afterwards where you want a different winner.</p>" if changes else
               "<p>With your current mod order no contested file changes winner.</p>")
        )
        box = QMessageBox(QMessageBox.Icon.Question, "Use MO2 mod priority for archives?", text,
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setTextFormat(Qt.TextFormat.RichText)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        self._o.setPluginSetting(CP_PLUGIN, "enforce_archive_load_order", True)
        self._o.setPluginSetting(CP_PLUGIN, "reverse_archive_load_order", True)
        MODEL.rescan()
        self.refresh_view()

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
                self, "mo2linttricks",
                f"Download CDPR's resource name list ({ac.NAME_DB_SIZE_MB} MB, from rfuzzo/red4lib, checksum-verified) to {db.parent}?",
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
        qInfo(f"mo2linttricks conflicts: {message}")
        if MODEL.report:
            MODEL.report.names.update(load_name_cache(self._o))
        self.refresh_view()
        QMessageBox.information(self, "mo2linttricks", message + " Files still shown as hashes were added by mods whose archives don't store names.")


class ScriptTab(QWidget):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        info = QLabel(
            "Functions that several mods replace with <code>@replaceMethod</code>/<code>@replaceGlobal</code>. Only one "
            "replacement runs. Identical replacements are harmless; others usually need a compatibility patch from the "
            "mod authors (or hide one mod's script in MO2's Data tab). <code>@wrapMethod</code> combines and is not listed."
        )
        info.setWordWrap(True)
        lay.addWidget(info)
        tree = QTreeWidget()
        tree.setHeaderLabels(["Function", "Status"])
        header = tree.header()
        if header:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in MODEL.scripts:
            top = QTreeWidgetItem([c.target, "identical, harmless" if c.identical else "DIFFERENT, only one runs"])
            if c.identical:
                top.setForeground(0, Qt.GlobalColor.gray)
            for r in c.replacements:
                top.addChild(QTreeWidgetItem([f"{r.mod}: {r.file}", ""]))
            tree.addTopLevelItem(top)
        if not MODEL.scripts:
            tree.addTopLevelItem(QTreeWidgetItem(["No function is replaced by more than one mod.", ""]))
        lay.addWidget(tree, 1)


class RulesTab(QWidget):
    RELATIONS = [
        ("must be below (load after, wins over)", "load_after"),
        ("must be above (load before, loses to)", "load_before"),
        ("is incompatible with", "incompatible_with"),
        ("requires", "requires"),
    ]

    def __init__(self, organizer: mobase.IOrganizer, parent: QWidget):
        super().__init__(parent)
        self._o = organizer
        lay = QVBoxLayout(self)
        self._info = QLabel()
        self._info.setWordWrap(True)
        self._info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._info)
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Issue", "Rule", "Source"])
        header = self._tree.header()
        if header:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self._tree, 1)
        row = QHBoxLayout()
        sort = QPushButton("Sort mod list...")
        sort.clicked.connect(self._sort)
        add = QPushButton("Add rule...")
        add.clicked.connect(self._add_rule)
        reload = QPushButton("Update community rules")
        reload.clicked.connect(self._reload)
        row.addWidget(sort)
        row.addWidget(add)
        row.addWidget(reload)
        row.addStretch(1)
        lay.addLayout(row)
        MODEL.listeners.append(self.refresh_view)
        self.destroyed.connect(self._detach)
        self.refresh_view()

    def _detach(self) -> None:
        if self.refresh_view in MODEL.listeners:
            MODEL.listeners.remove(self.refresh_view)

    def refresh_view(self) -> None:
        rr = MODEL.rules_report
        applied = len({e.rule_key for e in rr.edges})
        self._info.setText(
            f"Rules: {MODEL.rules_status}. Your own rules: <code>{MODEL.user_rules_path()}</code> (synced to your modlist repo). "
            f"{len(rr.edges)} ordering constraint(s) from {applied} rule(s) apply to your enabled mods. "
            "Lower in MO2's list wins: loose files, archives (with the Cyberpunk plugin's MO2 order) and TweakXL patch defaults."
        )
        self._tree.clear()
        red, ok = Qt.GlobalColor.red, Qt.GlobalColor.darkGreen
        broken = {(e.before, e.after) for e in rr.violations}
        groups = [
            ("Order", [(f"{e.after} should be below {e.before}", e.rule_key, e.rules.source, (e.before, e.after) in broken) for e in rr.edges]
             + [(f"{b} should be at the bottom", "load_bottom", "", b in rr.bottom_violations) for b in rr.bottom]),
            ("Incompatible", [(f"{a} + {b}: {note}", "incompatible_with", "", True) for a, b, note in rr.incompatible]),
            ("Missing requirements", [(f"{m} requires {req}", "requires", "", True) for m, req in rr.missing]),
        ]
        for title, items in groups:
            top = QTreeWidgetItem([f"{title}: {sum(1 for i in items if i[3])} issue(s), {len(items)} rule(s)", "", ""])
            for text, key, src, bad in sorted(items, key=lambda i: not i[3]):
                child = QTreeWidgetItem([("BROKEN  " if bad else "ok  ") + text, key, src])
                child.setForeground(0, red if bad else ok)
                top.addChild(child)
            self._tree.addTopLevelItem(top)
            top.setExpanded(any(i[3] for i in items))

    def _reload(self) -> None:
        MODEL.rules_status = "updating community rules..."
        self.refresh_view()
        MODEL.fetch_rules()

    def _sort(self) -> None:
        rr = MODEL.rules_report
        new, dropped = R.sort(MODEL.local, rr)
        old = [m.name for m in MODEL.local]
        moved = R.moves(old, new)
        if not moved:
            QMessageBox.information(self, "mo2linttricks", "The mod list already satisfies every rule.")
            return
        rows = "".join(f"<li>{n}: {old.index(n)} &rarr; {new.index(n)}</li>" for n in moved[:30])
        more = f"<p>...and {len(moved) - 30} more.</p>" if len(moved) > 30 else ""
        cyc = "<p>Rules that contradict each other were skipped:</p><ul>" + "".join(f"<li>{d}</li>" for d in dropped) + "</ul>" if dropped else ""
        box = QMessageBox(
            QMessageBox.Icon.Question, "Sort mod list?",
            f"<p>{len(moved)} mod(s) move (position in MO2's list, 0 = top); everything else keeps its order:</p><ul>{rows}</ul>{more}{cyc}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self,
        )
        box.setTextFormat(Qt.TextFormat.RichText)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        ml = self._o.modList()
        for i, name in enumerate(new):
            if name in moved:
                ml.setPriority(name, i)
        MODEL.rescan()
        left = len(MODEL.rules_report.violations) + len(MODEL.rules_report.bottom_violations)
        QMessageBox.information(self, "mo2linttricks", f"Moved {len(moved)} mod(s). Broken rules left: {left}.")

    def _add_rule(self) -> None:
        candidates = [m for m in MODEL.local if m.modid]
        if len(candidates) < 2:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Add load-order rule")
        lay = QVBoxLayout(dlg)
        a, rel, b = QComboBox(), QComboBox(), QComboBox()
        for m in candidates:
            a.addItem(m.name, m)
            b.addItem(m.name, m)
        for text, key in self.RELATIONS:
            rel.addItem(text, key)
        for w in (a, rel, b):
            lay.addWidget(w)
        note = QLabel("Saved to your own rules (synced with your modlist repo). Share good ones by adding them to rules/cyberpunk2077.json in the mo2linttricks repo.")
        note.setWordWrap(True)
        lay.addWidget(note)
        btns = QHBoxLayout()
        okb, cancel = QPushButton("Add"), QPushButton("Cancel")
        okb.clicked.connect(dlg.accept)
        cancel.clicked.connect(dlg.reject)
        btns.addStretch(1)
        btns.addWidget(okb)
        btns.addWidget(cancel)
        lay.addLayout(btns)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        ma, mb = a.currentData(), b.currentData()
        if ma.name == mb.name:
            return
        R.save_user_rule(MODEL.user_rules_path(), R.key_for(ma, MODEL.rules), rel.currentData(), R.key_for(mb, MODEL.rules), ma.name)
        MODEL.rescan()


class ResolverDialog(QDialog):
    def __init__(self, organizer: mobase.IOrganizer, parent: QWidget | None):
        super().__init__(parent)
        self.setWindowTitle("Conflict resolver (Cyberpunk 2077)")
        self.resize(1150, 700)
        MODEL.rescan()
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        real_scripts = sum(1 for c in MODEL.scripts if not c.identical)
        rep = MODEL.report
        tabs.addTab(TweakTab(organizer, self), f"TweakXL values ({len(MODEL.tweaks.real)})")
        tabs.addTab(ArchiveTab(organizer, self), f"Archives ({len(rep.real_conflicts) if rep else 0})")
        tabs.addTab(ScriptTab(self), f"redscript ({real_scripts})")
        rr = MODEL.rules_report
        tabs.addTab(RulesTab(organizer, self), f"Load order rules ({len(rr.violations) + len(rr.bottom_violations) + len(rr.incompatible) + len(rr.missing)})")
        lay.addWidget(tabs, 1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)


class ConflictResolverTool(mobase.IPluginTool):
    def __init__(self) -> None:
        super().__init__()
        self._organizer: mobase.IOrganizer | None = None
        self._parent: QWidget | None = None

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        MODEL.attach(organizer)
        return True

    def name(self) -> str:
        return "mo2linttricks Conflict Resolver"

    def displayName(self) -> str:
        return "mo2linttricks/Conflict resolver (Cyberpunk 2077)"

    def tooltip(self) -> str:
        return ".archive, TweakXL and redscript conflicts between mods, with a TweakXL patch generator"

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
            QMessageBox.information(self._parent, "mo2linttricks", "The conflict resolver is for Cyberpunk 2077 instances.")
            return
        ResolverDialog(self._organizer, self._parent).exec()
