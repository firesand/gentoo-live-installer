"""Small dialogs of the installer window."""

from __future__ import annotations

import zoneinfo
from datetime import datetime, timezone

from PySide6.QtCore import QDateTime
from PySide6.QtWidgets import QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout, QWidget

UTC_FORMAT = "%Y-%m-%d %H:%M:%S"


def utc_text(local: datetime, zone: str) -> str:
    """'2026-10-24 12:00:00' (UTC) for a wall-clock time in a time zone; ValueError for an unknown zone."""
    try:
        tz = zoneinfo.ZoneInfo(zone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"Unknown time zone: {zone}") from exc
    return local.replace(tzinfo=tz).astimezone(timezone.utc).strftime(UTC_FORMAT)


class SetTimeDialog(QDialog):
    """Ask for the current date and time, for a computer whose clock is far off and cannot reach a time server."""

    def __init__(self, parent: QWidget | None = None, zone: str = "UTC"):
        super().__init__(parent)
        self.setWindowTitle("Set the date and time")
        layout = QVBoxLayout(self)
        explanation = QLabel(
            "Type the time that a clock you trust shows (a phone, for example) and choose the time zone of that clock. "
            "The installer converts it to UTC, the time the computer keeps."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        form = QFormLayout()
        self.when = QDateTimeEdit(QDateTime.currentDateTime())
        self.when.setCalendarPopup(True)
        self.when.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.when.setAccessibleName("Date and time")
        form.addRow("Date and time", self.when)
        self.zone = QComboBox()
        self.zone.setEditable(True)
        self.zone.addItems(sorted(zoneinfo.available_timezones()))
        self.zone.setCurrentText(zone)
        self.zone.setAccessibleName("Time zone of that clock")
        form.addRow("Time zone", self.zone)
        layout.addLayout(form)
        self.problem = QLabel("")
        self.problem.setWordWrap(True)
        self.problem.hide()
        layout.addWidget(self.problem)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self) -> str:
        """The chosen moment as 'YYYY-MM-DD HH:MM:SS' in UTC (ValueError for an unknown zone)."""
        picked = self.when.dateTime().toPython()
        assert isinstance(picked, datetime)
        return utc_text(picked.replace(tzinfo=None), self.zone.currentText().strip())

    def _accept(self) -> None:
        try:
            self.value()
        except ValueError as exc:
            self.problem.setText(str(exc))
            self.problem.show()
            return
        self.accept()


def ask_utc_time(parent: QWidget | None = None) -> str:
    """Show the dialog; the UTC text the user chose, or '' when cancelled."""
    dialog = SetTimeDialog(parent)
    return dialog.value() if dialog.exec() == QDialog.DialogCode.Accepted else ""
