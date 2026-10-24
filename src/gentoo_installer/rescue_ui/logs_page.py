"""Look at the logs of the system that does not start (read-only; the tool shows the last lines of the files the system kept)."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton

from ..layout import human_size
from ..widgets import wrapped_label
from . import messages, model
from .base import Busy, InlineConfirm, Notice, RescuePage, TextView, soft_breaks
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
        journal = QHBoxLayout()
        self.journal_button = QPushButton("Read the &journal of the last boot…")
        self.journal_button.setObjectName("logJournal")
        self.journal_button.clicked.connect(self._ask_journal)
        journal.addWidget(self.journal_button)
        self.problems_only = QCheckBox("Only errors and worse")
        self.problems_only.setObjectName("logProblemsOnly")
        journal.addWidget(self.problems_only)
        self.kernel_only = QCheckBox("Only kernel messages")
        self.kernel_only.setObjectName("logKernelOnly")
        journal.addWidget(self.kernel_only)
        journal.addStretch()
        self.content.addLayout(journal)
        self._journal_widgets = (self.journal_button, self.problems_only, self.kernel_only)
        for widget in self._journal_widgets:
            widget.hide()
        self.confirm = InlineConfirm()
        self.confirm.confirmed.connect(self._journal_confirmed)
        self.confirm.cancelled.connect(self._journal_cancelled)
        self.content.addWidget(self.confirm)
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
        self.confirm.hide()
        for widget in self._journal_widgets:
            widget.hide()
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
        journal = data.get("journal", [])
        if not files:
            self.notice.show_message("info", "This system keeps its messages in a binary journal, not in text files. Use the button below to read it." if journal
                                     else "No log files were found in this system. It did not keep its messages from the failed boot.")
        elif journal:
            self.journal_note.setText("This system also keeps a binary journal. Reading it runs the system's own program as root, so you are asked first.")
        if journal:
            can_run = self.system is not None and self.system.allow_write
            for widget in self._journal_widgets:
                widget.show()
                widget.setEnabled(can_run)
            self.journal_button.setToolTip("" if can_run else "The system's own program does not run in look-only mode. Open the system again with repairs allowed to read the journal.")
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

    # ---- the journal: the system's own journalctl runs as root, so only after a question ------------------------------------------------
    def _ask_journal(self) -> None:
        self.confirm.ask("Read the journal of this system?", messages.JOURNAL_NOTICE, "&Read the journal", tick="I trust this system and want to read its journal")

    def _journal_cancelled(self) -> None:
        self.journal_button.setFocus()

    def _journal_confirmed(self, _typed: str, _phrase: str, _tick: bool) -> None:
        if self.system is None:
            return
        params: dict[str, Any] = {"confirm_exec": True, "lines": LINES, "boot": 0}
        if self.problems_only.isChecked():
            params["priority"] = "err"
        if self.kernel_only.isChecked():
            params["kernel"] = True
        self.notice.clear()
        self.show_button.setEnabled(False)
        self.journal_button.setEnabled(False)
        self.busy.start("Reading the journal… this starts the system's own program.")
        call = self.client.request("rescue.query", {"session_id": self.system.session_id, "query": "logs.journal", "params": params})
        call.then(self._journal_read, self._failed, self._lost)

    def _journal_read(self, result: dict[str, Any]) -> None:
        self.busy.stop()
        self.show_button.setEnabled(self.sources.count() > 0)
        self.journal_button.setEnabled(True)
        data = result.get("data", {})
        lines = [str(line) for line in data.get("lines", [])]
        if int(data.get("rc", 0) or 0) != 0 and not lines:
            self.notice.show_message("error", f"The journal could not be read (the system's journalctl stopped with code {data.get('rc')}).")
            return
        self.text.setPlainText(soft_breaks("\n".join(lines)) or "(The journal has no messages for this choice.)")
        self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())

    def _shown(self, result: dict[str, Any]) -> None:
        self.busy.stop()
        self.show_button.setEnabled(True)
        data = result.get("data", {})
        self.text.setPlainText(soft_breaks("\n".join(data.get("lines", []))) or "(The file is empty.)")
        self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())

    def _failed(self, _code: str, text: str, _data: dict[str, Any]) -> None:
        self.busy.stop()
        self.show_button.setEnabled(self.sources.count() > 0)
        self.journal_button.setEnabled(self.system is not None and self.system.allow_write)
        self.notice.show_message("error", text)

    def _lost(self, text: str) -> None:
        self._failed("", text, {})

    def focus_first(self) -> None:
        if self.show_button.isEnabled():
            self.show_button.setFocus()
        elif self.journal_button.isVisibleTo(self) and self.journal_button.isEnabled():
            self.journal_button.setFocus()
        else:
            self.back_button.setFocus()
