"""Building blocks shared by the pages of the repair window.

Confirmations are inline panels, not pop-up dialogs: they cannot hide behind the window, they can be reached with the keyboard in the
order they appear, and on a small screen nothing opens that does not fit.
"""

from __future__ import annotations

import html
import re
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontDatabase, QTextOption
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..widgets import Card, Page, StatusRow, wrapped_label

SYMBOLS = {"ok": "✓", "warning": "!", "error": "×", "info": "i", "pending": "○", "running": "●", "skipped": "–"}


def soft_breaks(text: str, chunk: int = 36) -> str:
    """Allow a line break inside very long unbroken words (device paths, digests) so a label never makes the page wider than the window."""
    return re.sub(rf"(\S{{{chunk}}})(?=\S)", "\\1\u200b", text)


def mono_font() -> Any:
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)


class RescuePage(Page):
    """A page with a title for the window header and a footer row for its buttons (outside the scrolling area, so they are always in reach)."""

    headerChanged = Signal()
    title = ""
    subtitle = ""
    step = 0  # which entry of the sidebar is current while this page shows

    def set_header(self, title: str, subtitle: str | None = None) -> None:
        """Change the heading of the window while this page shows (a page that has several states)."""
        self.title = title
        if subtitle is not None:
            self.subtitle = subtitle
        self.headerChanged.emit()

    def __init__(self) -> None:
        super().__init__("")
        self.footer = QFrame()
        self.footer.setObjectName("contentHeader")
        self.footer_row = QHBoxLayout(self.footer)
        self.footer_row.setContentsMargins(24, 10, 24, 10)
        self._footer_split = -1
        outer = self.layout()
        assert outer is not None
        outer.addWidget(self.footer)
        self.content.setContentsMargins(24, 18, 24, 18)
        self.content.setSpacing(12)

    def add_button(self, text: str, name: str, *, primary: bool = False, danger: bool = False, right: bool = True, tip: str = "") -> QPushButton:
        """A footer button. Add the left-hand buttons (right=False) first: the first button added with right=True pushes the rest to the right."""
        button = QPushButton(text)
        button.setObjectName(name)
        if primary or danger:
            button.setStyleSheet(danger_style() if danger else primary_style())
        if tip:
            button.setToolTip(tip)
            button.setAccessibleDescription(tip)
        if right and self._footer_split < 0:
            self.footer_row.addStretch()
            self._footer_split = self.footer_row.count()
        self.footer_row.addWidget(button)
        return button

    def on_show(self) -> None:
        """Called by the window each time the page becomes the current one."""

    def close_blocker(self) -> str:
        """Why the window must not close right now ('' when it may)."""
        return ""


def primary_style() -> str:
    """The look of the main button of a page (read when the button is made, so dark mode is honoured)."""
    return (f"QPushButton {{ color: white; background: {theme.ACCENT_DARK}; border: 0; font-weight: 600; padding: 10px 22px; }} "
            f"QPushButton:disabled {{ color: #e4e0f0; background: #9a92c9; }} QPushButton:focus {{ border: 2px solid {theme.TEXT}; padding: 8px 20px; }}")


def danger_style() -> str:
    return ("QPushButton { color: white; background: #b83a4b; border: 0; font-weight: 600; padding: 10px 22px; } "
            f"QPushButton:disabled {{ color: #f0e0e3; background: #c9929b; }} QPushButton:focus {{ border: 2px solid {theme.TEXT}; padding: 8px 20px; }}")


class Notice(QFrame):
    """A short message with a status symbol: what happened, or what the person should know."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statusCard")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 12, 8)
        self.symbol = QLabel()
        self.symbol.setMinimumWidth(16)
        self.symbol.setAlignment(Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.symbol, 0, Qt.AlignmentFlag.AlignTop)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.text, 1)
        self.kind = "info"
        self.hide()

    def show_message(self, kind: str, text: str) -> None:
        self.kind = kind
        self.symbol.setText(SYMBOLS.get(kind, "i"))
        self.symbol.setStyleSheet(f"color: {theme.status_color({'info': 'unknown'}.get(kind, kind))}; font-weight: 700;")
        self.text.setText(soft_breaks(text))
        self.setAccessibleName(f"{kind}: {text[:60]}")
        self.setVisible(bool(text))

    def clear(self) -> None:
        self.text.setText("")
        self.hide()


class Busy(QWidget):
    """A moving bar with a sentence: something is being done and may take a moment."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        self.label = wrapped_label("", muted=True)
        column.addWidget(self.label)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(False)
        self.bar.setMaximumHeight(10)
        column.addWidget(self.bar)
        self.hide()

    def start(self, text: str) -> None:
        self.label.setText(text)
        self.bar.setRange(0, 0)
        self.show()

    def progress(self, text: str, percent: float | None = None) -> None:
        self.label.setText(soft_breaks(text))
        if percent is not None:
            self.bar.setRange(0, 100)
            self.bar.setValue(int(max(0, min(100, percent))))
        self.show()

    def stop(self) -> None:
        self.hide()


class SecretField(QWidget):
    """A hidden-text field with a Show / Hide switch (a passphrase can be checked before it is sent)."""

    returnPressed = Signal()
    textChanged = Signal(str)

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setAccessibleName(label)
        self.edit.setPlaceholderText(label)
        self.edit.returnPressed.connect(self.returnPressed)
        self.edit.textChanged.connect(self.textChanged)
        row.addWidget(self.edit, 1)
        self.toggle = QToolButton()
        self.toggle.setText("Show")
        self.toggle.setCheckable(True)
        self.toggle.setAccessibleName(f"Show {label.lower()}")
        self.toggle.setToolTip("Show the text you typed")
        self.toggle.toggled.connect(self._toggled)
        row.addWidget(self.toggle)
        self.setFocusProxy(self.edit)

    def _toggled(self, shown: bool) -> None:
        self.edit.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)
        self.toggle.setText("Hide" if shown else "Show")
        self.toggle.setToolTip("Hide the text again" if shown else "Show the text you typed")

    def text(self) -> str:
        return self.edit.text()

    def clear(self) -> None:
        self.edit.clear()
        self.toggle.setChecked(False)

    def setFocus(self, reason: Qt.FocusReason = Qt.FocusReason.OtherFocusReason) -> None:  # type: ignore[override]
        self.edit.setFocus(reason)


class Expander(QWidget):
    """A heading that opens and closes its content (the exact commands, the details of a problem)."""

    toggled = Signal(bool)

    def __init__(self, title: str, content: QWidget, parent: QWidget | None = None, *, open_: bool = False) -> None:
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        self.title = title
        self.button = QToolButton()
        self.button.setCheckable(True)
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.button.setStyleSheet("QToolButton { border: 0; text-align: left; padding: 3px; }")
        self.button.setAccessibleName(title)
        self.button.toggled.connect(self._toggled)
        column.addWidget(self.button, 0, Qt.AlignmentFlag.AlignLeft)
        self.content = content
        column.addWidget(content)
        self.button.setChecked(open_)
        self._toggled(open_)

    def _toggled(self, shown: bool) -> None:
        self.button.setText(("▾  " if shown else "▸  ") + self.title)
        self.content.setVisible(shown)
        self.toggled.emit(shown)

    def set_title(self, title: str) -> None:
        self.title = title
        self._toggled(self.button.isChecked())

    @property
    def is_open(self) -> bool:
        return self.button.isChecked()


class TextView(QTextEdit):
    """Read-only monospace text: command output, a log, a file difference."""

    def __init__(self, maximum_lines: int = 5000, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(mono_font())
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.document().setMaximumBlockCount(maximum_lines)
        self.setMinimumHeight(110)
        self.setAccessibleName("Output")

    def show_diff(self, diff: str) -> None:
        """A unified diff with removed lines in red and added lines in green."""
        lines = []
        for line in diff.splitlines():
            colour = "#c33d50" if line.startswith("-") and not line.startswith("---") else "#27855c" if line.startswith("+") and not line.startswith("+++") else ""
            safe = html.escape(line) or "&nbsp;"
            lines.append(f'<span style="color:{colour}">{safe}</span>' if colour else safe)
        self.setHtml('<pre style="margin:0">' + "<br>".join(lines) + "</pre>")

    def append_lines(self, lines: list[str]) -> None:
        if lines:
            self.append("\n".join(lines))


class FlowText(QTextEdit):
    """Read-only text that wraps anywhere and grows to show all its lines.

    For commands and paths: a very long word cannot push the page wider than the window, and what is copied is the exact text
    (a label would need invisible break characters in it, which break a pasted command).
    """

    def __init__(self, parent: QWidget | None = None, *, mono: bool = False) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("QTextEdit { background: transparent; border: 0; padding: 0; }")
        if mono:
            self.setFont(mono_font())
        self.setMinimumWidth(40)
        self.document().documentLayout().documentSizeChanged.connect(lambda _size: self._fit())

    def _fit(self) -> None:
        height = int(self.document().size().height()) + 4
        if height != self.height():
            self.setFixedHeight(max(height, 18))

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self.document().setTextWidth(self.viewport().width())
        self._fit()

    def set_lines(self, lines: list[str], *, html_text: bool = False) -> None:
        if html_text:
            self.setHtml("<br>".join(lines))
        else:
            self.setPlainText("\n".join(lines))
        self.document().setTextWidth(max(self.viewport().width(), 40))
        self._fit()


class InlineConfirm(QFrame):
    """A question that appears inside the page: a text, optionally a tick box and words to type, then Confirm or Cancel."""

    confirmed = Signal(str, str, bool)  # typed text, typed phrase, tick box
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        column = QVBoxLayout(self)
        column.setContentsMargins(14, 12, 14, 12)
        column.setSpacing(8)
        self.title = QLabel()
        self.title.setObjectName("cardTitle")
        self.title.setWordWrap(True)
        column.addWidget(self.title)
        self.body = wrapped_label("")
        column.addWidget(self.body)
        self.check = QCheckBox()
        column.addWidget(self.check)
        self.typed_label = wrapped_label("", muted=True)
        column.addWidget(self.typed_label)
        self.typed = QLineEdit()
        self.typed.setObjectName("confirmTyped")
        column.addWidget(self.typed)
        self.phrase_label = wrapped_label("", muted=True)
        column.addWidget(self.phrase_label)
        self.phrase = QLineEdit()
        self.phrase.setObjectName("confirmPhrase")
        column.addWidget(self.phrase)
        row = QHBoxLayout()
        row.addStretch()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("confirmCancel")
        self.cancel_button.clicked.connect(self._cancel)
        row.addWidget(self.cancel_button)
        self.confirm_button = QPushButton("Continue")
        self.confirm_button.setObjectName("confirmGo")
        self.confirm_button.clicked.connect(self._confirm)
        row.addWidget(self.confirm_button)
        column.addLayout(row)
        self._expected = ""
        self._phrase = ""
        self._needs_tick = False
        self.check.toggled.connect(self._update)
        self.typed.textChanged.connect(self._update)
        self.phrase.textChanged.connect(self._update)
        self.typed.returnPressed.connect(self._enter)
        self.phrase.returnPressed.connect(self._enter)
        self.hide()

    def ask(self, title: str, body: str, confirm_text: str, *, expected: str = "", phrase: str = "", tick: str = "", danger: bool = False, cancel_text: str = "Cancel") -> None:
        self.title.setText(title)
        self.body.setText(soft_breaks(body))
        self.check.setText(tick)
        self.check.setChecked(False)
        self.check.setVisible(bool(tick))
        self._expected, self._phrase, self._needs_tick = expected, phrase, bool(tick)
        self.typed_label.setText(f"Type {expected} to continue:" if expected else "")
        self.typed_label.setVisible(bool(expected))
        self.typed.clear()
        self.typed.setVisible(bool(expected))
        self.typed.setAccessibleName(f"Type {expected}")
        self.phrase_label.setText(f"This can destroy data. Type {phrase} to continue:" if phrase else "")
        self.phrase_label.setVisible(bool(phrase))
        self.phrase.clear()
        self.phrase.setVisible(bool(phrase))
        self.phrase.setAccessibleName(f"Type {phrase}")
        self.confirm_button.setText(confirm_text)
        self.cancel_button.setText(cancel_text)
        self.confirm_button.setStyleSheet(danger_style() if danger else primary_style())
        self._update()
        self.show()
        (self.typed if expected else self.confirm_button).setFocus()

    def _ready(self) -> bool:
        if self._expected and self.typed.text() != self._expected:
            return False
        if self._phrase and self.phrase.text() != self._phrase:
            return False
        return not self._needs_tick or self.check.isChecked()

    def _update(self) -> None:
        self.confirm_button.setEnabled(self._ready())

    def _enter(self) -> None:
        if self._ready():
            self._confirm()

    def _confirm(self) -> None:
        if self._ready():
            self.hide()
            self.confirmed.emit(self.typed.text(), self.phrase.text(), self.check.isChecked())

    def _cancel(self) -> None:
        self.hide()
        self.cancelled.emit()


__all__ = ["Busy", "Card", "Expander", "FlowText", "InlineConfirm", "Notice", "RescuePage", "SecretField", "StatusRow", "TextView", "mono_font", "soft_breaks", "wrapped_label"]
