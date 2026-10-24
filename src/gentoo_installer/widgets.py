from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAccessible, QAccessibleStateChangeEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QAccessibleWidget,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import theme


class Card(QFrame):
    # The layout is called `content`, not `layout`: assigning to `layout`
    # replaces QWidget.layout() and confuses Qt, type checkers and subclasses.
    def __init__(self, title: str = "", description: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(18, 16, 18, 16)
        self.content.setSpacing(8)
        if title:
            label = QLabel(title)
            label.setObjectName("cardTitle")
            self.content.addWidget(label)
        if description:
            label = QLabel(description)
            label.setWordWrap(True)
            label.setProperty("muted", True)
            self.content.addWidget(label)


class ChoiceCard(Card):
    """One option of a ChoiceGroup. Behaves as a radio button for keyboard and
    assistive technology: it is checkable, has a focus ring, and the arrow keys
    ask the group to move the selection."""

    toggled = Signal(bool)
    navigate = Signal(int)

    def __init__(self, title: str, description: str, recommended: bool = False, parent: QWidget | None = None):
        super().__init__("", "", parent)
        self.setObjectName("choiceCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("selected", False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._checked = False
        row = QHBoxLayout()
        self.title_label = QLabel(title)
        self.title_label.setObjectName("cardTitle")
        self.title_label.setWordWrap(True)
        row.addWidget(self.title_label, 1)
        if recommended:
            badge = QLabel("Recommended")
            badge.setStyleSheet(f"color: {theme.ACCENT_DARK}; background: #eee9ff; border-radius: 7px; padding: 3px 7px; font-size: 11px;")
            row.addWidget(badge)
        self.content.addLayout(row)
        self.description_label = QLabel(description)
        self.description_label.setProperty("muted", True)
        self.description_label.setWordWrap(True)
        self.content.addWidget(self.description_label)
        self.setAccessibleName(title)
        self.setAccessibleDescription(description)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool) -> None:
        if self._checked == checked:
            return
        self._checked = checked
        self.setProperty("selected", checked)
        self.style().unpolish(self)
        self.style().polish(self)
        QAccessible.updateAccessibility(_state_change_event(self))
        self.toggled.emit(checked)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.setChecked(True)
            self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            self.setChecked(True)
            event.accept()
            return
        if key in {Qt.Key.Key_Left, Qt.Key.Key_Up}:
            self.navigate.emit(-1)
            event.accept()
            return
        if key in {Qt.Key.Key_Right, Qt.Key.Key_Down}:
            self.navigate.emit(1)
            event.accept()
            return
        super().keyPressEvent(event)


def _state_change_event(card: ChoiceCard) -> QAccessibleStateChangeEvent:
    changed = QAccessible.State()
    changed.checked = True
    return QAccessibleStateChangeEvent(card, changed)


class ChoiceCardAccessible(QAccessibleWidget):
    """Exposes a ChoiceCard as a radio button with a checked state."""

    def __init__(self, widget: ChoiceCard):
        super().__init__(widget, QAccessible.Role.RadioButton)

    def _card(self) -> ChoiceCard:
        return self.widget()  # type: ignore[return-value]

    def state(self):
        state = super().state()
        state.checkable = True
        state.checked = self._card().isChecked()
        state.focusable = self._card().focusPolicy() != Qt.FocusPolicy.NoFocus
        return state

    def actionNames(self):
        return [QAccessible.pressAction()]

    def doAction(self, action_name: str) -> bool:
        if action_name == QAccessible.pressAction() and self._card().isEnabled():
            self._card().setChecked(True)
            return True
        return False


def _accessible_factory(class_name: str, obj: QObject):
    if isinstance(obj, ChoiceCard):
        return ChoiceCardAccessible(obj)
    return None


QAccessible.installFactory(_accessible_factory)


class Page(QWidget):
    advanceRequested = Signal()

    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll)
        body = QWidget()
        self.scroll.setWidget(body)
        self.content = QVBoxLayout(body)
        self.content.setContentsMargins(32, 28, 32, 28)
        self.content.setSpacing(18)
        self.content.setAlignment(Qt.AlignmentFlag.AlignTop)
        if title:
            heading = QLabel(title)
            heading.setObjectName("sectionTitle")
            self.content.addWidget(heading)
        if subtitle:
            text = QLabel(subtitle)
            text.setWordWrap(True)
            text.setProperty("muted", True)
            self.content.addWidget(text)

    def validate_page(self) -> tuple[bool, str]:
        return True, ""

    def load_manifest(self) -> None:
        pass

    def save_manifest(self) -> None:
        pass

    def advance_on_enter(self, *edits: QLineEdit) -> None:
        """Pressing Return in one of these fields presses Continue."""
        for edit in edits:
            edit.returnPressed.connect(self.advanceRequested)

    def focus_first(self) -> None:
        """Put the keyboard focus on the first useful control of the page."""
        for widget in self.findChildren(QLineEdit):
            if widget.isEnabled() and widget.isVisibleTo(self) and not widget.isReadOnly():
                widget.setFocus(Qt.FocusReason.TabFocusReason)
                return
        for card in self.findChildren(ChoiceCard):
            if card.isEnabled() and card.isVisibleTo(self) and card.focusPolicy() != Qt.FocusPolicy.NoFocus:
                card.setFocus(Qt.FocusReason.TabFocusReason)
                return
        for widget in self.findChildren(QWidget):
            if (
                isinstance(widget, (QComboBox, QAbstractButton, QPlainTextEdit))
                and widget.isEnabled()
                and widget.isVisibleTo(self)
                and widget.focusPolicy() != Qt.FocusPolicy.NoFocus
            ):
                widget.setFocus(Qt.FocusReason.TabFocusReason)
                return
        self.setFocus()


class FormGrid(QGridLayout):
    def __init__(self):
        super().__init__()
        self.setHorizontalSpacing(18)
        self.setVerticalSpacing(12)
        self.setColumnStretch(1, 1)

    def add_field(self, row: int, label: str, widget: QWidget, hint: str = "") -> None:
        caption = QLabel(label)
        caption.setBuddy(widget)
        self.addWidget(caption, row, 0, Qt.AlignmentFlag.AlignTop)
        self.addWidget(widget, row, 1)
        if hint:
            help_text = QLabel(hint)
            help_text.setProperty("muted", True)
            help_text.setWordWrap(True)
            self.addWidget(help_text, row + 1, 1)


class ChoiceGroup(QWidget):
    """A set of ChoiceCards of which exactly one can be selected (radio group).

    Only the selected card (or the first enabled one) is in the Tab chain; the
    arrow keys move the selection, as in a native radio group.
    """

    selectionChanged = Signal(str)

    def __init__(self, choices: list[tuple[str, str, str, bool]], columns: int = 2, parent: QWidget | None = None):
        super().__init__(parent)
        self.cards: dict[str, ChoiceCard] = {}
        self.columns = columns
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(12)
        for column in range(columns):
            self._grid.setColumnStretch(column, 1)
        self._build(choices)

    def _build(self, choices: list[tuple[str, str, str, bool]]) -> None:
        for index, (key, title, description, recommended) in enumerate(choices):
            card = ChoiceCard(title, description, recommended)
            card.toggled.connect(lambda checked, name=key: self._select(name) if checked else None)
            card.navigate.connect(lambda step, name=key: self._move(name, step))
            self.cards[key] = card
            self._grid.addWidget(card, index // self.columns, index % self.columns)
        self._update_tab_stops()

    def set_choices(self, choices: list[tuple[str, str, str, bool]]) -> bool:
        """Replace the cards, keeping this widget. Returns whether the previous
        selection still exists (and is still selected)."""
        previous = self.value()
        for card in self.cards.values():
            self._grid.removeWidget(card)
            card.hide()
            card.setParent(None)
            card.deleteLater()
        self.cards = {}
        self._build(choices)
        if previous and previous in self.cards:
            self.cards[previous].setChecked(True)
            self._update_tab_stops()
            return True
        return False

    def _select(self, key: str) -> None:
        for name, card in self.cards.items():
            card.setChecked(name == key)
        self._update_tab_stops()
        self.selectionChanged.emit(key)

    def _move(self, key: str, step: int) -> None:
        keys = [name for name, card in self.cards.items() if card.isEnabled()]
        if key not in keys:
            return
        target = keys[(keys.index(key) + step) % len(keys)]
        self._select(target)
        self.cards[target].setFocus(Qt.FocusReason.TabFocusReason)

    def _update_tab_stops(self) -> None:
        enabled = [card for card in self.cards.values() if card.isEnabled()]
        stop = next((card for card in enabled if card.isChecked()), enabled[0] if enabled else None)
        for card in self.cards.values():
            card.setFocusPolicy(Qt.FocusPolicy.StrongFocus if card is stop else Qt.FocusPolicy.NoFocus)

    def set_card_enabled(self, key: str, enabled: bool, reason: str = "") -> None:
        card = self.cards.get(key)
        if card is None:
            return
        card.setEnabled(enabled)
        card.setToolTip("" if enabled else reason)
        self._update_tab_stops()

    def value(self) -> str:
        return next((key for key, card in self.cards.items() if card.isChecked()), "")

    def set_value(self, key: str) -> None:
        if key in self.cards:
            self._select(key)

    def clear_value(self) -> None:
        for card in self.cards.values():
            card.setChecked(False)
        self._update_tab_stops()


class StatusRow(QWidget):
    SYMBOLS = {"ok": "✓", "warning": "!", "error": "×", "unknown": "●", "checking": "…", "info": "i"}

    def __init__(self, title: str, detail: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 3)
        self.status = "unknown"
        self.indicator = QLabel("●")
        self.indicator.setAccessibleName("Status")
        self.indicator.setMinimumWidth(14)
        layout.addWidget(self.indicator, 0, Qt.AlignmentFlag.AlignTop)
        self.title = QLabel(title)
        self.title.setMinimumWidth(180)
        layout.addWidget(self.title, 0, Qt.AlignmentFlag.AlignTop)
        self.detail = QLabel(detail)
        self.detail.setProperty("muted", True)
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.detail, 1)
        self.set_status("unknown", detail)

    def set_status(self, status: str, detail: str = "") -> None:
        self.status = status
        self.indicator.setText(self.SYMBOLS.get(status, "●"))
        self.indicator.setStyleSheet(f"color: {theme.status_color(status)}; font-weight: 700;")
        self.detail.setText(detail)
        self.setAccessibleDescription(f"{status}: {detail}")


class Banner(QFrame):
    """A notice above the pages with action buttons (running job, resumable job)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("banner")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(32, 12, 32, 12)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.label, 1)
        self.buttons_row = QHBoxLayout()
        layout.addLayout(self.buttons_row)
        self.buttons: dict[str, QPushButton] = {}
        self.hide()

    def show_message(self, text: str, actions: list[tuple[str, str, Callable[[], None]]], *, error: bool = False) -> None:
        """actions are (key, button text, callback)."""
        for button in self.buttons.values():
            self.buttons_row.removeWidget(button)
            button.hide()
            button.setParent(None)
            button.deleteLater()
        self.buttons = {}
        self.setObjectName("bannerError" if error else "banner")
        self.style().unpolish(self)
        self.style().polish(self)
        self.label.setText(text)
        for key, caption, callback in actions:
            button = QPushButton(caption)
            button.clicked.connect(lambda _=False, fn=callback: fn())
            self.buttons_row.addWidget(button)
            self.buttons[key] = button
        self.show()

    def dismiss(self) -> None:
        self.hide()


def wrapped_label(text: str = "", *, muted: bool = False, selectable: bool = False) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    if muted:
        label.setProperty("muted", True)
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.LinksAccessibleByMouse)
    return label


def horizontal_line() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet(f"color: {theme.BORDER};")
    return line
