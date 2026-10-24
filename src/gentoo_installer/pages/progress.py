from __future__ import annotations

from datetime import timedelta

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QPushButton

from ..backend import BackendWorker
from ..manifest import InstallManifest
from ..widgets import Card, Page


STAGE_LABELS = {
    "preflight": "Checking the installation environment",
    "storage": "Preparing disks and filesystems",
    "stage3": "Downloading and verifying Gentoo",
    "base": "Configuring the base system",
    "portage": "Synchronizing Portage",
    "kernel": "Installing firmware and the kernel",
    "configure": "Configuring your system",
    "desktop": "Installing the desktop and applications",
    "bootloader": "Installing the bootloader",
    "validate": "Checking the installed system",
    "cleanup": "Finishing and unmounting",
}


class ProgressPage(Page):
    cancelRequested = Signal()
    finished = Signal(bool)
    workerStopped = Signal()
    rebootRequested = Signal()

    def __init__(self):
        super().__init__("Installing Gentoo", "You can follow the detailed log while the installer works. Keep the live session running until installation is complete.")
        self.worker: BackendWorker | None = None
        self.seconds = 0
        self._terminal = False
        self._failure_reported = False
        self._finished_emitted = False
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        status_card = Card()
        self.stage = QLabel("Preparing to start…")
        self.stage.setObjectName("cardTitle")
        status_card.layout.addWidget(self.stage)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        status_card.layout.addWidget(self.progress)
        row = QHBoxLayout()
        self.detail = QLabel("Connecting to the installer backend")
        self.detail.setProperty("muted", True)
        row.addWidget(self.detail, 1)
        self.elapsed = QLabel("00:00")
        row.addWidget(self.elapsed)
        status_card.layout.addLayout(row)
        self.layout.addWidget(status_card)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log.setMinimumHeight(280)
        self.layout.addWidget(self.log)
        actions = QHBoxLayout()
        self.cancel = QPushButton("Cancel installation")
        self.cancel.clicked.connect(self._cancel)
        actions.addWidget(self.cancel)
        actions.addStretch()
        self.reboot = QPushButton("Restart into Gentoo")
        self.reboot.setObjectName("primaryButton")
        self.reboot.setVisible(False)
        self.reboot.clicked.connect(self.rebootRequested)
        actions.addWidget(self.reboot)
        self.layout.addLayout(actions)

    def start(self, manifest: InstallManifest, socket_path: str) -> None:
        self.log.clear()
        self.seconds = 0
        self._terminal = False
        self._failure_reported = False
        self._finished_emitted = False
        self.elapsed.setText("00:00")
        self.progress.setRange(0, 0)
        self.stage.setText("Starting installation…")
        self.detail.setText("Connecting to the installer backend")
        self.cancel.setEnabled(True)
        self.cancel.setVisible(True)
        self.reboot.setVisible(False)
        self.worker = BackendWorker(manifest, socket_path, self)
        self.worker.eventReceived.connect(self.handle_event)
        self.worker.connectionFailed.connect(self._connection_failed)
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()
        self.timer.start()

    def handle_event(self, event: dict) -> None:
        kind = event.get("event") or event.get("type") or event.get("ev")
        stage = event.get("stage", "")
        message = event.get("message") or event.get("text") or event.get("line") or ""
        # Cleanup continues after failure, but its progress must not replace the
        # terminal failure status shown to the user.
        if self._failure_reported and kind in {"stage", "progress"}:
            return
        if kind in {"stage", "progress"}:
            label = STAGE_LABELS.get(stage, stage.replace("_", " ").title() or "Working…")
            self.stage.setText(label)
            if message:
                self.detail.setText(message)
            percent = event.get("percent", event.get("progress"))
            if isinstance(percent, (int, float)) and percent >= 0:
                self.progress.setRange(0, 100)
                self.progress.setValue(min(100, round(percent)))
            else:
                self.progress.setRange(0, 0)
        elif kind == "log" and message:
            self.log.appendPlainText(str(message).rstrip())
        elif kind in {"error", "failed"}:
            self._fail(message or "The installation failed. Review the log for details.")
        elif kind == "completed":
            self._terminal = True
            success = event.get("data", {}).get("success", True)
            if success is True and not self._failure_reported:
                self.progress.setRange(0, 100)
                self.progress.setValue(100)
                self.stage.setText("Gentoo is ready")
                self.detail.setText(message or "Installation completed and the target checks passed.")
                self.cancel.setVisible(False)
                self.reboot.setVisible(True)
                self.timer.stop()
                self._finished_emitted = True
                self.finished.emit(True)
            else:
                if not self._failure_reported:
                    self._fail(message or "The installation did not complete successfully.")

    def _connection_failed(self, message: str) -> None:
        self._fail("Could not connect to the privileged installer backend: " + message)

    def _fail(self, message: str) -> None:
        self._failure_reported = True
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.stage.setText("Installation needs attention")
        self.detail.setText(message)
        if not self.log.toPlainText().endswith("ERROR: " + message):
            self.log.appendPlainText("ERROR: " + message)
        self.cancel.setVisible(False)
        self.timer.stop()
        if not self._finished_emitted:
            self._finished_emitted = True
            self.finished.emit(False)

    def _cancel(self) -> None:
        self.cancel.setEnabled(False)
        self.detail.setText("Cancellation requested; waiting for a safe stopping point…")
        if self.worker:
            self.worker.cancel()
        self.cancelRequested.emit()

    def _worker_finished(self) -> None:
        if not self._terminal and not self._failure_reported:
            self._fail("The installer backend stopped before reporting completion.")
        self.workerStopped.emit()

    def _tick(self) -> None:
        self.seconds += 1
        self.elapsed.setText(str(timedelta(seconds=self.seconds)))

    def stop(self) -> None:
        self.timer.stop()
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
