"""Tools menu entries: sign in to GitHub, sync now, restore from GitHub."""

from __future__ import annotations

import threading

import mobase
from PyQt6.QtCore import QObject, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QGuiApplication, QIcon
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from .core import tokenstore
from .core.github import GitHub, GitHubError, poll_device_flow, start_device_flow
from .core.http import Transport
from .sync_plugin import NAME as SYNC_NAME, VERSION, ModlistSync

OAUTH_HELP = """<p>mo2linttricks signs in with a GitHub <b>OAuth App</b> that you own. One-time setup:</p>
<ol>
<li>Open <a href="https://github.com/settings/applications/new">github.com/settings/applications/new</a></li>
<li>Name: <code>mo2linttricks</code>. Homepage URL and callback URL: <code>http://localhost</code> (not used)</li>
<li>Tick <b>Enable Device Flow</b>, then <b>Register application</b></li>
<li>Copy the <b>Client ID</b> (no secret needed) into<br>
<i>Settings &gt; Plugins &gt; mo2linttricks Modlist Sync &gt; github_client_id</i></li>
</ol>
<p>Then run this tool again.</p>"""


class _Tool(mobase.IPluginTool):
    def __init__(self, sync: ModlistSync):
        super().__init__()
        self._sync = sync
        self._organizer: mobase.IOrganizer | None = None
        self._parent: QWidget | None = None

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        return True

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

    def _client_id(self) -> str:
        return str(self._organizer.pluginSetting(SYNC_NAME, "github_client_id") or "").strip() if self._organizer else ""


# --- sign in -------------------------------------------------------------------


class _FlowSignals(QObject):
    code = pyqtSignal(str, str)  # user code, verification url
    done = pyqtSignal(str, str)  # login, error


class SignInDialog(QDialog):
    def __init__(self, client_id: str, parent: QWidget | None):
        super().__init__(parent)
        self.setWindowTitle("Sign in to GitHub")
        self._client_id = client_id
        self._cancelled = False
        self._url = ""
        self.login = ""
        lay = QVBoxLayout(self)
        self._label = QLabel("Requesting a sign-in code from GitHub...")
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._label.setWordWrap(True)
        lay.addWidget(self._label)
        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._open = self._buttons.addButton("Open GitHub", QDialogButtonBox.ButtonRole.ActionRole)
        assert self._open is not None
        self._open.setEnabled(False)
        self._open.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self._url)))
        self._buttons.rejected.connect(self.reject)
        lay.addWidget(self._buttons)
        self._sig = _FlowSignals()
        self._sig.code.connect(self._show_code)
        self._sig.done.connect(self._finish)
        threading.Thread(target=self._run, daemon=True).start()

    def reject(self) -> None:
        self._cancelled = True
        super().reject()

    def _run(self) -> None:
        t = Transport()
        try:
            code = start_device_flow(self._client_id, t)
            self._sig.code.emit(code.user_code, code.verification_uri)
            token = poll_device_flow(self._client_id, code, t, cancelled=lambda: self._cancelled)
            login = GitHub(token, t).user()
            tokenstore.save(token, login)
            self._sig.done.emit(login, "")
        except Exception as e:
            if not self._cancelled:
                self._sig.done.emit("", str(e))

    def _show_code(self, user_code: str, url: str) -> None:
        self._url = url
        clip = QGuiApplication.clipboard()
        if clip:
            clip.setText(user_code)
        self._label.setText(
            f"<p>Enter this code at <a href='{url}'>{url}</a>:</p>"
            f"<h2 style='font-family:monospace'>{user_code}</h2>"
            "<p>(already copied to your clipboard). This window closes once you approve.</p>"
        )
        self._label.setOpenExternalLinks(True)
        if self._open:
            self._open.setEnabled(True)
        QDesktopServices.openUrl(QUrl(url))

    def _finish(self, login: str, error: str) -> None:
        if error:
            QMessageBox.warning(self, "Sign in failed", error)
            super().reject()
        else:
            self.login = login
            self.accept()


class SignInTool(_Tool):
    def name(self) -> str:
        return "mo2linttricks Sign In"

    def displayName(self) -> str:
        return "mo2linttricks/Sign in to GitHub"

    def tooltip(self) -> str:
        return "Connect mo2linttricks to your GitHub account"

    def description(self) -> str:
        return self.tooltip()

    def display(self) -> None:
        current = tokenstore.load().get("login")
        if current:
            r = QMessageBox.question(
                self._parent,
                "mo2linttricks",
                f"Signed in to GitHub as <b>{current}</b>.<br>Sign out?",
            )
            if r == QMessageBox.StandardButton.Yes:
                tokenstore.clear()
            return
        cid = self._client_id()
        if not cid:
            box = QMessageBox(QMessageBox.Icon.Information, "mo2linttricks: GitHub OAuth App needed", OAUTH_HELP, parent=self._parent)
            box.setTextFormat(Qt.TextFormat.RichText)
            box.exec()
            return
        dlg = SignInDialog(cid, self._parent)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            QMessageBox.information(
                self._parent,
                "mo2linttricks",
                f"Signed in as <b>{dlg.login}</b>. Syncing to private repo <b>{self._sync.repo_name()}</b> now.",
            )
            self._sync.sync_async("signed in")


# --- sync now ------------------------------------------------------------------


class SyncNowTool(_Tool):
    def name(self) -> str:
        return "mo2linttricks Sync Now"

    def displayName(self) -> str:
        return "mo2linttricks/Sync modlist now"

    def tooltip(self) -> str:
        return "Push the current modlist to GitHub immediately"

    def description(self) -> str:
        return self.tooltip()

    def display(self) -> None:
        if not tokenstore.load().get("token"):
            QMessageBox.information(self._parent, "mo2linttricks", "Sign in to GitHub first (Tools > mo2linttricks > Sign in).")
            return

        def report(message: str, ok: bool) -> None:
            self._sync.listeners.remove(report)
            (QMessageBox.information if ok else QMessageBox.warning)(self._parent, "mo2linttricks", message)

        self._sync.listeners.append(report)
        self._sync.sync_async("manual")


# --- restore -------------------------------------------------------------------


class RestoreTool(_Tool):
    def name(self) -> str:
        return "mo2linttricks Restore"

    def displayName(self) -> str:
        return "mo2linttricks/Restore modlist from GitHub"

    def tooltip(self) -> str:
        return "Download, install and order mods to match a profile stored on GitHub"

    def description(self) -> str:
        return self.tooltip()

    def init(self, organizer: mobase.IOrganizer) -> bool:
        super().init(organizer)
        from .restore_dialog import RestoreRouter

        self._router = RestoreRouter(organizer)
        return True

    def display(self) -> None:
        tok = tokenstore.load().get("token")
        if not tok or not self._organizer:
            QMessageBox.information(self._parent, "mo2linttricks", "Sign in to GitHub first (Tools > mo2linttricks > Sign in).")
            return
        from .restore_dialog import RestoreDialog

        try:
            repo = f"{GitHub(tok).user()}/{self._sync.repo_name()}"
        except GitHubError as e:
            QMessageBox.warning(self._parent, "mo2linttricks", str(e))
            return
        dlg = RestoreDialog(self._organizer, GitHub(tok), repo, self._router, self._parent)
        dlg.exec()
