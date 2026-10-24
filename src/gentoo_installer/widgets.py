from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QAccessible, QAccessibleActionInterface, QAccessibleStateChangeEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QAccessibleWidget,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
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
        return [QAccessibleActionInterface.pressAction()]

    def doAction(self, action_name: str) -> None:
        if action_name == QAccessibleActionInterface.pressAction() and self._card().isEnabled():
            self._card().setChecked(True)


def _accessible_factory(class_name: str, obj: QObject):
    if isinstance(obj, ChoiceCard):
        return ChoiceCardAccessible(obj)
    return None


QAccessible.installFactory(_accessible_factory)


class FlowLayout(QLayout):
    """Lays widgets out left to right and starts a new line when the row is full.

    A row of buttons made with this layout never forces the page wider than the window: on a narrow window the
    buttons wrap onto a second line instead of being cut off."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 8):
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _arrange(self, rect: QRect, *, test_only: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, line_height = area.x(), area.y(), 0
        for item in self._items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            if x > area.x() and x + hint.width() > area.right() + 1:
                x, y, line_height = area.x(), y + line_height + self._gap, 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._gap
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


def wrap_long_rows(root: QWidget) -> None:
    """Forms below ``root`` put a label above its field when the window is too narrow for both side by side."""
    for form in root.findChildren(QFormLayout):
        if form.rowWrapPolicy() == QFormLayout.RowWrapPolicy.DontWrapRows:  # a form that always wraps keeps doing so
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)


def shrinkable_combos(root: QWidget, width: int = 120) -> None:
    """Let the combo boxes below ``root`` get narrower than their longest entry when the window is narrow.

    A combo box asks for the width of its longest item, which can push a page wider than a small window.
    With an explicit minimum the layout still gives it the full width when there is room."""
    for combo in root.findChildren(QComboBox):
        if combo.minimumWidth() == 0:
            combo.setMinimumWidth(width)


class Page(QWidget):
    advanceRequested = Signal()
    problemChanged = Signal(bool)  # an inline problem message appeared (True) or was cleared (False): the sidebar marks the step

    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea()  # not "scroll": that is a QWidget method
        self.scroll_area.setWidgetResizable(True)
        # Normally everything fits the width; on a very small window a scroll bar appears instead of cutting content off.
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        outer.addWidget(self.scroll_area)
        body = QWidget()
        self.scroll_area.setWidget(body)
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
        # CORE-09: a Continue that fails shows its reason here, in red, instead of in a dialog.
        self.problem_label = QLabel()
        self.problem_label.setObjectName("problemLabel")
        self.problem_label.setWordWrap(True)
        self.problem_label.setAccessibleName("Problem on this step")
        self.problem_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.problem_label.hide()
        self.content.addWidget(self.problem_label)

    def show_problem(self, text: str) -> None:
        """Show (or, with "", clear) the page's inline problem message: a plain sentence, then the technical detail."""
        shown = bool(text)
        was_shown = not self.problem_label.isHidden()
        self.problem_label.setText(text)
        self.problem_label.setVisible(shown)
        if shown:
            self.scroll_area.ensureVisible(0, 0)
        if shown != was_shown:
            self.problemChanged.emit(shown)

    def has_problem(self) -> bool:
        return not self.problem_label.isHidden()

    def summary_rows(self) -> list[tuple[str, str]]:
        """Review protocol (docs/contracts.md): (plain label, plain value) for every setting of this page that
        the Review page should list, in reading order. The default is none; the review adds the core rows itself."""
        return []

    def finish_layout(self) -> None:
        """Called at the end of a page's constructor: narrow-window rules for the widgets the page has built."""
        shrinkable_combos(self)
        wrap_long_rows(self)

    def showEvent(self, event) -> None:
        self.finish_layout()  # also reaches the combo boxes that were added after construction
        super().showEvent(event)

    def validate_page(self) -> tuple[bool, str]:
        return True, ""

    def load_manifest(self) -> None:
        pass

    def save_manifest(self) -> None:
        pass

    def set_advanced(self, advanced: bool) -> None:
        """The window's detail level changed (Guided or Advanced). A page with options that only an expert needs
        overrides this: Advanced shows them all, Guided collapses them, and values already entered survive the switch."""

    def advance_on_enter(self, *edits: QLineEdit) -> None:
        """Pressing Return in one of these fields presses Continue."""
        for edit in edits:
            edit.returnPressed.connect(self.advanceRequested)

    def focus_first(self) -> None:
        """Put the keyboard focus on the first useful control of the page."""
        for edit in self.findChildren(QLineEdit):
            if edit.isEnabled() and edit.isVisibleTo(self) and not edit.isReadOnly():
                edit.setFocus(Qt.FocusReason.TabFocusReason)
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
        layout = self.row_layout = QHBoxLayout(self)
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
        self.action: QPushButton | None = None
        self.set_status("unknown", detail)

    def set_action(self, text: str, callback: Callable[[], None] | None) -> None:
        """Show a button at the end of the row (None removes it), for the fix the finding offers."""
        if self.action is not None:
            self.row_layout.removeWidget(self.action)
            self.action.hide()
            self.action.setParent(None)
            self.action.deleteLater()
            self.action = None
        if callback is None or not text:
            return
        button = QPushButton(text)
        button.setAccessibleName(f"{text} - {self.title.text()}")
        button.clicked.connect(lambda _=False: callback())
        self.row_layout.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        self.action = button

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
