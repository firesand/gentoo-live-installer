"""Look at the logs of the system that does not start (read-only; the tool shows the last lines of the files the system kept)."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton

from ..layout import human_size
from ..widgets import wrapped_label
from . import messages, model
from .base import Busy, Notice, RescuePage, TextView, soft_breaks
from .client import RescueClient

LINES = 200


class LogsPage(RescuePage):
    title = "Logs of the system"
    subtitle = "Read-only: the last lines of the log files the system kept."
    step = 2
    backRequested = Signal()

    def __init__(self, client: RescueClient) -> None:
        super().__init__()
        self.client = client
        self.system: model.OpenSystem | None = None
        self.content.addWidget(wrapped_label(messages.LOGS_NOTICE, muted=True))
        self.notice = Notice()
        self.content.addWidget(self.notice)
        self.busy = Busy()
        self.content.addWidget(self.busy)
        row = QHBoxLayout()
        row.addWidget(QLabel("Log file:"))
        self.sources = QComboBox()
        self.sources.setObjectName("logSources")
        self.sources.setAccessibleName("Log file")
        row.addWidget(self.sources, 1)
        self.show_button = QPushButton("&Show the last lines")
        self.show_button.setObjectName("logShow")
        self.show_button.clicked.connect(self._show)
        row.addWidget(self.show_button)
        self.content.addLayout(row)
        self.journal_note = wrapped_label("", muted=True)
        self.content.addWidget(self.journal_note)
        self.text = TextView(4000)
        self.text.setObjectName("logText")
        self.text.setMinimumHeight(220)
        self.text.setAccessibleName("Contents of the log file")
        self.content.addWidget(self.text, 1)
        self.back_button = self.add_button("&Back", "rescueBack", right=False)
        self.back_button.clicked.connect(self.backRequested)

    def begin(self, system: model.OpenSystem) -> None:
        self.system = system
        self.sources.clear()
        self.text.clear()
        self.notice.clear()
        self.journal_note.clear()
        self.show_button.setEnabled(False)
        self.busy.start("Looking for log files…")
        call = self.client.request("rescue.query", {"session_id": system.session_id, "query": "logs.sources"})
        call.then(self._sources, self._failed, self._lost)

    def _sources(self, result: dict[str, Any]) -> None:
        self.busy.stop()
        data = result.get("data", {})
        files = data.get("files", [])
        for item in files:
            self.sources.addItem(f"{item['path']}  ({human_size(int(item.get('size', 0)))})", item["path"])
        self.show_button.setEnabled(bool(files))
        if not files:
            self.notice.show_message("info", "No log files were found in this system.")
        journal = data.get("journal", [])
        if journal:
            self.journal_note.setText("This system also keeps a binary journal. Reading it runs the system's own program as root, so it is not offered here. Use the terminal for it.")
        if files:
            self._show()

    def _show(self) -> None:
        path = self.sources.currentData()
        if self.system is None or not path:
            return
        self.show_button.setEnabled(False)
        self.busy.start("Reading the file…")
        call = self.client.request("rescue.query", {"session_id": self.system.session_id, "query": "logs.tail", "params": {"path": path, "lines": LINES}})
        call.then(self._shown, self._failed, self._lost)

    def _shown(self, result: dict[str, Any]) -> None:
        self.busy.stop()
        self.show_button.setEnabled(True)
        data = result.get("data", {})
        self.text.setPlainText(soft_breaks("\n".join(data.get("lines", []))) or "(The file is empty.)")
        self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())

    def _failed(self, _code: str, text: str, _data: dict[str, Any]) -> None:
        self.busy.stop()
        self.show_button.setEnabled(self.sources.count() > 0)
        self.notice.show_message("error", text)

    def _lost(self, text: str) -> None:
        self._failed("", text, {})

    def focus_first(self) -> None:
        (self.show_button if self.show_button.isEnabled() else self.back_button).setFocus()
