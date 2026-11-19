"""Turn on Feral GameMode for the game (only) whenever MO2 launches it, under Wine/Proton.

MO2's Python runs inside Wine, so it cannot talk to gamemoded itself. Instead it
starts the bundled Linux helper (mo2lt-gamemode.exe, an ELF binary; Wine runs
Linux executables natively). The helper finds the game's process, registers it
over D-Bus, and exits when the game exits, so it never holds Steam's session open.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path

import mobase
from PyQt6.QtCore import QDir, qInfo, qWarning

from .sync_plugin import NAME as SYNC_NAME, VERSION

NAME = "mo2linttricks GameMode"
HELPER = Path(__file__).resolve().parent / "bin" / "mo2lt-gamemode.exe"


def running_under_wine() -> bool:
    if os.name != "nt":
        return False
    try:
        return hasattr(getattr(ctypes, "WinDLL")("ntdll"), "wine_get_version")
    except OSError:
        return False


def _base(p: str) -> str:
    return p.replace("\\", "/").rsplit("/", 1)[-1].lower()


class GameModeLauncher(mobase.IPlugin):
    def __init__(self):
        super().__init__()
        self._organizer: mobase.IOrganizer | None = None
        self._wine = False

    def name(self) -> str:
        return NAME

    def localizedName(self) -> str:
        return "mo2linttricks: GameMode"

    def author(self) -> str:
        return "LinkIsGrim"

    def description(self) -> str:
        return "Enables Feral GameMode for the game process when MO2 launches it (Linux, Wine/Proton only)."

    def version(self) -> mobase.VersionInfo:
        return VERSION

    def master(self) -> str:
        return SYNC_NAME

    def settings(self) -> list[mobase.PluginSetting]:
        return [
            mobase.PluginSetting("extra_targets", "Extra executables (comma separated) to put in GameMode, e.g. a launcher-spawned exe", ""),
            mobase.PluginSetting("all_executables", "Use GameMode for every executable MO2 runs, not just the game", False),
            mobase.PluginSetting("helper_path", "Override path to mo2lt-gamemode.exe", ""),
        ]

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        self._wine = running_under_wine()
        if not self._wine:
            qInfo("mo2linttricks GameMode: not running under Wine, disabled")
            return True
        organizer.onAboutToRun(self._about_to_run)
        return True

    def _setting(self, key: str, default):
        v = self._organizer.pluginSetting(NAME, key) if self._organizer else None
        return default if v is None else v

    def _about_to_run(self, app_path: str, wd: QDir, args: str) -> bool:
        try:
            self._maybe_start(app_path)
        except Exception as e:  # never block a launch
            qWarning(f"mo2linttricks GameMode: {e}")
        return True

    def _maybe_start(self, app_path: str) -> None:
        o = self._organizer
        if not o or not o.isPluginEnabled(self):
            return
        game = o.managedGame()
        launched = _base(app_path)
        binary = _base(game.binaryName())
        launcher = _base(game.getLauncherName() or "")
        extras = [_base(x.strip()) for x in str(self._setting("extra_targets", "")).split(",") if x.strip()]

        triggers = {binary, launcher, *extras} - {""}
        if not self._setting("all_executables", False) and launched not in triggers:
            return

        # Register the game binary (a launcher hands off to it), whatever was launched
        # unless it is just the launcher, and any extras.
        targets = sorted(({launched} - {launcher}) | {binary} | set(extras) - {""})
        helper = Path(str(self._setting("helper_path", "") or HELPER))
        if not helper.exists():
            qWarning(f"mo2linttricks GameMode: helper not found at {helper}")
            return
        cmd = [str(helper)]
        for t in targets:
            cmd += ["--target", t]
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        qInfo(f"mo2linttricks GameMode: watching {', '.join(targets)}")
