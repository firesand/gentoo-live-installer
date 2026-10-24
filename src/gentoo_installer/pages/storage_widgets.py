"""STOR: the small widgets of the Storage page: coloured notes, the disk bar, the "what will be created" table and the "on this disk now" table."""

from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from .. import layout, theme
from ..system_info import DiskInfo
from ..widgets import wrapped_label

ROLE_COLORS = {
    "efi": "#3b82f6", "bios_boot": "#64748b", "boot": "#8b5cf6", "swap": "#e0902b", "swapfile": "#e0902b",
    "root": "#2fa37a", "home": "#d65b9a", "data": "#94a3b8",
}


class Note(QLabel):
    """A coloured box with a sentence: a red or yellow warning, or a plain information."""

    def __init__(self, kind: str = "warning", parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self.kind = kind
        self.setWordWrap(True)
        self.setObjectName("storageNote")
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.apply_theme()
        self.hide()

    def apply_theme(self) -> None:
        color = theme.status_color("error" if self.kind == "error" else "warning")
        self.setStyleSheet(f"QLabel#storageNote {{ border: 2px solid {color}; border-radius: 8px; padding: 8px 10px; }}")

    def show_text(self, text: str) -> None:
        self.setText(text)
        self.setAccessibleName(("Warning: " if self.kind == "error" else "Note: ") + text)
        self.setVisible(bool(text))


class DiskBar(QWidget):
    """The disk as one horizontal bar, one coloured piece per partition (the rest of the disk is the system)."""

    MINIMUM_SHARE = 0.05

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.segments: list[tuple[str, int]] = []  # (role, MiB; 0 = the rest)
        self.setMinimumHeight(26)
        self.setMaximumHeight(26)
        self.setAccessibleName("Disk layout")

    def set_rows(self, rows: Sequence[layout.TableRow]) -> None:
        self.segments = [(row.role, row.size_mib) for row in rows if row.role != "swapfile"]
        self.setAccessibleDescription(", ".join(f"{row.partition} {row.size_text}" for row in rows))
        self.update()

    def shares(self) -> list[float]:
        """Width of every piece as a share of the bar: sizes in proportion, none thinner than MINIMUM_SHARE, the rest-of-disk piece takes what is left."""
        known = sum(size for _, size in self.segments)
        rest_pieces = sum(1 for _, size in self.segments if not size)
        if not self.segments:
            return []
        total = known / 0.6 if rest_pieces and known else (known or 1)
        raw = [size / total if size else (1 - known / total) / max(rest_pieces, 1) for _, size in self.segments]
        raw = [max(share, self.MINIMUM_SHARE) for share in raw]
        scale = sum(raw)
        return [share / scale for share in raw]

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        x = 0.0
        width = float(self.width())
        for (role, _), share in zip(self.segments, self.shares(), strict=True):
            piece = QRectF(x, 0, max(share * width - 2, 1), float(self.height()))
            painter.setBrush(QColor(ROLE_COLORS.get(role, ROLE_COLORS["data"])))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(piece, 5, 5)
            x += share * width
        painter.end()


class PlanPreview(QWidget):
    """"What will be created": the bar and the table of partitions, in the order the installer creates them."""

    COLUMNS = ("Partition", "Mount point", "Filesystem", "Size", "What it is for")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel("What will be created")
        self.title.setObjectName("cardTitle")
        box.addWidget(self.title)
        self.bar = DiskBar()
        box.addWidget(self.bar)
        self.legend = wrapped_label("", muted=True)
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        box.addWidget(self.legend)
        self.table = _table(self.COLUMNS)
        self.table.setAccessibleName("Partitions that will be created")
        box.addWidget(self.table)
        self.caption = wrapped_label("This is exactly what will be written to the disk; nothing else is touched.", muted=True)
        box.addWidget(self.caption)
        self.rows: list[layout.TableRow] = []

    def set_rows(self, rows: Sequence[layout.TableRow]) -> None:
        self.rows = list(rows)
        self.bar.set_rows(self.rows)
        self.legend.setText("  ".join(f'<span style="color:{ROLE_COLORS.get(r.role, ROLE_COLORS["data"])}">■</span> {r.partition} {r.size_text}' for r in self.rows if r.role != "swapfile"))
        self.table.setRowCount(len(self.rows))
        for index, row in enumerate(self.rows):
            for column, text in enumerate((row.partition, row.mountpoint, row.filesystem, row.size_text, row.purpose)):
                self.table.setItem(index, column, _cell(text))
        _fit(self.table)


class ExistingTable(QWidget):
    """"On this disk now": the partitions of the chosen disk, read only."""

    COLUMNS = ("Partition", "Filesystem", "Label", "Size")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel("On this disk now")
        self.title.setObjectName("cardTitle")
        box.addWidget(self.title)
        self.table = _table(self.COLUMNS)
        self.table.setAccessibleName("Partitions on the chosen disk now")
        box.addWidget(self.table)
        self.empty = wrapped_label("The disk has no partitions.", muted=True)
        box.addWidget(self.empty)
        self.note = wrapped_label("Everything shown here is removed if you continue; the final screen asks you to type the disk name.", muted=True)
        box.addWidget(self.note)

    def set_disk(self, disk: DiskInfo | None) -> None:
        parts = [part for part in disk.partitions if part.type == "part"] if disk is not None else []
        self.table.setRowCount(len(parts))
        for index, part in enumerate(parts):
            for column, text in enumerate((part.path, part.fstype or part.parttype_name or "unformatted", part.label or part.partlabel, part.size_text)):
                self.table.setItem(index, column, _cell(text))
        self.table.setVisible(bool(parts))
        self.empty.setVisible(disk is not None and not parts)
        _fit(self.table)


def _table(columns: Sequence[str]) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(list(columns))
    table.verticalHeader().hide()
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    table.setWordWrap(True)
    table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setStretchLastSection(True)
    return table


def _cell(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemFlag.ItemIsEnabled)
    return item


def _fit(table: QTableWidget) -> None:
    """Make the table as tall as its rows, so the page scrolls instead of the table."""
    table.resizeRowsToContents()
    height = table.horizontalHeader().height() + 2 * table.frameWidth() + sum(table.rowHeight(row) for row in range(table.rowCount()))
    table.setMinimumHeight(height)
    table.setMaximumHeight(height)
