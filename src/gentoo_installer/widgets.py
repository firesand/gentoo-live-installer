from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .theme import BORDER, MUTED, SURFACE


class Card(QFrame):
    def __init__(self, title: str = "", description: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(18, 16, 18, 16)
        self.layout.setSpacing(8)
        if title:
            label = QLabel(title)
            label.setObjectName("cardTitle")
            self.layout.addWidget(label)
        if description:
            label = QLabel(description)
            label.setWordWrap(True)
            label.setProperty("muted", True)
            self.layout.addWidget(label)


class ChoiceCard(Card):
    toggled = Signal(bool)

    def __init__(self, title: str, description: str, recommended: bool = False, parent: QWidget | None = None):
        super().__init__("", "", parent)
        self.setObjectName("choiceCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("selected", False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._checked = False
        row = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        row.addWidget(title_label)
        row.addStretch()
        if recommended:
            badge = QLabel("Recommended")
            badge.setStyleSheet("color: #6554c0; background: #eee9ff; border-radius: 7px; padding: 3px 7px; font-size: 11px;")
            row.addWidget(badge)
        self.layout.addLayout(row)
        text = QLabel(description)
        text.setProperty("muted", True)
        text.setWordWrap(True)
        self.layout.addWidget(text)
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
        self.toggled.emit(checked)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(True)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            self.setChecked(True)
            event.accept()
            return
        super().keyPressEvent(event)


class Page(QWidget):
    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        self.layout = QVBoxLayout(body)
        self.layout.setContentsMargins(32, 28, 32, 28)
        self.layout.setSpacing(18)
        self.layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        if title:
            heading = QLabel(title)
            heading.setObjectName("sectionTitle")
            self.layout.addWidget(heading)
        if subtitle:
            text = QLabel(subtitle)
            text.setWordWrap(True)
            text.setProperty("muted", True)
            self.layout.addWidget(text)

    def validate_page(self) -> tuple[bool, str]:
        return True, ""

    def load_manifest(self) -> None:
        pass

    def save_manifest(self) -> None:
        pass


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
    selectionChanged = Signal(str)

    def __init__(self, choices: list[tuple[str, str, str, bool]], columns: int = 2, parent: QWidget | None = None):
        super().__init__(parent)
        self.cards: dict[str, ChoiceCard] = {}
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)
        for index, (key, title, description, recommended) in enumerate(choices):
            card = ChoiceCard(title, description, recommended)
            card.toggled.connect(lambda checked, name=key: self._select(name) if checked else None)
            self.cards[key] = card
            grid.addWidget(card, index // columns, index % columns)
        for column in range(columns):
            grid.setColumnStretch(column, 1)

    def _select(self, key: str) -> None:
        for name, card in self.cards.items():
            card.setChecked(name == key)
        self.selectionChanged.emit(key)

    def value(self) -> str:
        return next((key for key, card in self.cards.items() if card.isChecked()), "")

    def set_value(self, key: str) -> None:
        if key in self.cards:
            self._select(key)


class StatusRow(QWidget):
    def __init__(self, title: str, detail: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 3)
        self.indicator = QLabel("●")
        self.indicator.setAccessibleName("Status")
        layout.addWidget(self.indicator)
        name = QLabel(title)
        name.setMinimumWidth(180)
        layout.addWidget(name)
        self.detail = QLabel(detail)
        self.detail.setProperty("muted", True)
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail, 1)
        self.set_status("unknown", detail)

    def set_status(self, status: str, detail: str = "") -> None:
        colors = {"ok": "#27855c", "warning": "#b87818", "error": "#c33d50", "unknown": MUTED}
        symbols = {"ok": "✓", "warning": "!", "error": "×", "unknown": "●"}
        self.indicator.setText(symbols.get(status, "●"))
        self.indicator.setStyleSheet(f"color: {colors.get(status, MUTED)}; font-weight: 700;")
        self.detail.setText(detail)
        self.setAccessibleDescription(f"{status}: {detail}")


def horizontal_line() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet(f"color: {BORDER};")
    return line
