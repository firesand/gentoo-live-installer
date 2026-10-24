from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem

from ..manifest import InstallManifest
from ..widgets import Card, Page


class ReviewPage(Page):
    exportRequested = Signal()

    def __init__(self, manifest: InstallManifest):
        super().__init__("Review your installation", "Nothing has been changed yet. Check your choices carefully—especially the target disk—before starting.")
        self.manifest = manifest
        summary_card = Card("Installation summary")
        self.summary = QTableWidget(0, 2)
        self.summary.setHorizontalHeaderLabels(["Setting", "Selection"])
        self.summary.verticalHeader().setVisible(False)
        self.summary.horizontalHeader().setStretchLastSection(True)
        self.summary.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.summary.setMinimumHeight(350)
        summary_card.content.addWidget(self.summary)
        self.content.addWidget(summary_card)

        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("background: #fff1f2; color: #8d2636; border: 1px solid #f0c5cb; border-radius: 9px; padding: 14px;")
        self.content.addWidget(self.warning)
        confirm_card = Card("Final confirmation", "Type the target device path exactly to enable installation. This is the last step before disk changes begin.")
        form = QFormLayout()
        self.confirmation = QLineEdit()
        self.confirmation.setClearButtonEnabled(True)
        self.confirmation.setAccessibleName("Target disk confirmation")
        form.addRow("Target disk", self.confirmation)
        confirm_card.content.addLayout(form)
        self.content.addWidget(confirm_card)
        export = QPushButton("Save a redacted copy of these settings")
        export.clicked.connect(self._export)
        self.content.addWidget(export)

    def load_manifest(self) -> None:
        values = self.manifest.summary()
        self.summary.setRowCount(len(values))
        for row, (name, value) in enumerate(values):
            self.summary.setItem(row, 0, QTableWidgetItem(name))
            self.summary.setItem(row, 1, QTableWidgetItem(value))
        self.summary.resizeRowsToContents()
        if self.manifest.storage.mode == "erase":
            self.warning.setText(f"All existing data on {self.manifest.storage.device} will be permanently erased. Other disks will not be modified.")
        else:
            self.warning.setText(f"Manual installation on {self.manifest.storage.device}: only partitions explicitly marked for formatting will be changed.")
        self.confirmation.setPlaceholderText(self.manifest.storage.device)
        self.confirmation.clear()

    def validate_page(self) -> tuple[bool, str]:
        if self.confirmation.text().strip() != self.manifest.storage.device:
            return False, f"Type {self.manifest.storage.device} exactly to confirm the target disk."
        errors = self.manifest.validate()
        return (False, errors[0]) if errors else (True, "")

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save installation settings", "gentoo-installation.json", "JSON files (*.json)")
        if path:
            self.manifest.save(path, redact=True)
