"""Show mo2linttricks conflicts in MO2's mod list (left pane), next to MO2's own conflict icons.

MO2's plugin API can't add mod-list columns or flags, so this wraps the Qt delegate that
draws the "Conflicts" column (ModConflictIconDelegate, column ModList::COL_CONFLICTFLAGS
in modorganizer/src/modlistview.cpp). MO2 paints its icons first (from the left); we add
small letter badges on the right and append our details to the cell's tooltip:

    A  .archive conflicts      T  TweakXL values      S  redscript replacements
    R  load-order rules

Red = needs attention (unresolved TweakXL value, archive fully overridden, differing
redscript replacement, broken rule, incompatible/missing mod). Amber = a conflict that is
resolved or expected (patched value, archive overrides that follow the load order).
Nothing in MO2 is modified; removing the plugin restores the original delegate.
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QEvent, QModelIndex, QRect, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QHelpEvent, QPainter
from PyQt6.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QMainWindow,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolTip,
    QTreeView,
)

COL_NAME = 0
COL_CONFLICTFLAGS = 1  # ModList::EColumn in MO2 2.5
BADGE = 14
GAP = 2
RED = QColor(220, 60, 60)
AMBER = QColor(230, 160, 40)
KINDS = ("A", "T", "S", "R")
KIND_NAMES = {"A": "Archives", "T": "TweakXL", "S": "redscript", "R": "Load-order rules"}


@dataclass
class Issue:
    kind: str  # A/T/S/R
    severe: bool
    text: str


class BadgeDelegate(QStyledItemDelegate):
    def __init__(self, view: QTreeView, original: QAbstractItemDelegate | None, issues_for):
        super().__init__(view)
        self._view = view
        self._original = original  # MO2's delegate; we keep a reference so it isn't freed
        self._issues_for = issues_for  # callable(mod name) -> list[Issue]

    def _mod(self, index: QModelIndex) -> str:
        name = index.siblingAtColumn(COL_NAME).data(Qt.ItemDataRole.DisplayRole)
        return str(name) if name is not None else ""

    def _badges(self, index: QModelIndex) -> list[tuple[str, bool]]:
        issues = self._issues_for(self._mod(index))
        out = []
        for k in KINDS:
            mine = [i for i in issues if i.kind == k]
            if mine:
                out.append((k, any(i.severe for i in mine)))
        return out

    def paint(self, painter: QPainter | None, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        if self._original is not None:
            self._original.paint(painter, option, index)
        else:
            super().paint(painter, option, index)
        badges = self._badges(index)
        if not badges or painter is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(painter.font())
        font.setPixelSize(10)
        font.setBold(True)
        painter.setFont(font)
        r = option.rect
        x = r.right() - GAP - len(badges) * (BADGE + GAP) + GAP
        y = r.top() + (r.height() - BADGE) // 2
        for letter, severe in badges:
            box = QRect(x, y, BADGE, BADGE)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(RED if severe else AMBER)
            painter.drawRoundedRect(box, 3, 3)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, letter)
            x += BADGE + GAP
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        base = self._original.sizeHint(option, index) if self._original is not None else super().sizeHint(option, index)
        n = len(self._badges(index))
        return QSize(base.width() + n * (BADGE + GAP) + GAP, max(base.height(), BADGE + 2)) if n else base

    def helpEvent(self, event: QHelpEvent | None, view: QAbstractItemView | None, option: QStyleOptionViewItem, index: QModelIndex) -> bool:
        if event is None or event.type() != QEvent.Type.ToolTip:
            return super().helpEvent(event, view, option, index)
        issues = self._issues_for(self._mod(index))
        if not issues:
            if self._original is not None:
                return self._original.helpEvent(event, view, option, index)
            return super().helpEvent(event, view, option, index)
        base = index.data(Qt.ItemDataRole.ToolTipRole)
        parts = [str(base)] if base else []
        lines = []
        for k in KINDS:
            mine = [i for i in issues if i.kind == k]
            if mine:
                lines.append(f"<b>{KIND_NAMES[k]}</b>")
                lines += [("&#9888; " if i.severe else "&bull; ") + i.text for i in mine[:12]]
                if len(mine) > 12:
                    lines.append(f"&hellip; and {len(mine) - 12} more")
        parts.append("<b>mo2linttricks</b><br>" + "<br>".join(lines))
        QToolTip.showText(event.globalPos(), "<br><br>".join(parts), view)
        return True


class ModListBadges:
    def __init__(self, issues_for):
        self._issues_for = issues_for
        self._view: QTreeView | None = None
        self._delegate: BadgeDelegate | None = None

    def install(self, window: QMainWindow) -> bool:
        view = window.findChild(QTreeView, "modList")
        if view is None:
            return False
        original = view.itemDelegateForColumn(COL_CONFLICTFLAGS)
        self._delegate = BadgeDelegate(view, original, self._issues_for)
        view.setItemDelegateForColumn(COL_CONFLICTFLAGS, self._delegate)
        self._view = view
        return True

    def refresh(self) -> None:
        if self._view is not None:
            vp = self._view.viewport()
            if vp is not None:
                vp.update()
