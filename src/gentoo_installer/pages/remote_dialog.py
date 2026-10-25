"""I9: the "Remote access (SSH)" dialog of the readiness page: log in to the live system from another computer.

It shows whether remote access is on, the ``ssh gentoo@ADDRESS`` line, the password (once, right after turning it on) and the keys'
fingerprints, and has the two buttons. The work is done by the installer service (``remote_enable``, ``remote_disable``,
``remote_status``); the password goes to it over the local socket and is shown only here.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from .. import netclient, remote_cli
from ..widgets import wrapped_label

INTRO = ("Remote access lets you control this installer from another computer on the same network, for example to read the Handbook "
         "on one screen and type commands on the other. It is off at first: nobody can log in until you turn it on here.")


class RemoteAccessDialog(QDialog):
    def __init__(self, caller: netclient.Caller | None, parent: QWidget | None = None, *, synchronous: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Remote access (SSH)")
        self.caller = caller
        self.synchronous = synchronous
        self.status: dict[str, Any] = {}
        self._workers: list[netclient.CallWorker] = []
        layout = QVBoxLayout(self)
        layout.addWidget(wrapped_label(INTRO))
        self.state = wrapped_label("Checking…")
        layout.addWidget(self.state)
        self.details = wrapped_label("", selectable=True)
        self.details.hide()
        layout.addWidget(self.details)
        form = QFormLayout()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("leave empty to get one made for you")
        self.password.setAccessibleName("Password for remote access")
        form.addRow("Password", self.password)
        layout.addLayout(form)
        self.show_password = QCheckBox("Show what I type")
        self.show_password.toggled.connect(lambda on: self.password.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        layout.addWidget(self.show_password)
        self.enable_button = QPushButton("Turn remote access &on")
        self.enable_button.clicked.connect(self.enable)
        self.disable_button = QPushButton("Turn remote access o&ff")
        self.disable_button.clicked.connect(self.disable)
        layout.addWidget(self.enable_button)
        layout.addWidget(self.disable_button)
        layout.addWidget(wrapped_label(remote_cli.HANDBOOK_WAY, muted=True))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self.setMinimumWidth(460)
        self.refresh()

    def call(self, action: str, payload: dict[str, Any], done: Any) -> None:
        if self.caller is None:
            done({"ok": False, "message": netclient.UNAVAILABLE})
        elif self.synchronous:
            done(self.caller(action, payload))
        else:
            worker = netclient.CallWorker(self.caller, action, payload, self)
            worker.answered.connect(lambda _a, answer, w=worker: self._answered(w, done, answer))
            self._workers.append(worker)
            worker.start()

    def _answered(self, worker: netclient.CallWorker, done: Any, answer: Any) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        done(answer)

    def refresh(self) -> None:
        self.call("remote_status", {}, lambda answer: self.show_answer(answer))

    def enable(self) -> None:
        self.enable_button.setEnabled(False)
        self.call("remote_enable", {"password": self.password.text()}, lambda answer: self.show_answer(answer, password=True))

    def disable(self) -> None:
        self.call("remote_disable", {}, lambda answer: self.show_answer(answer))

    def show_answer(self, answer: dict[str, Any], password: bool = False) -> None:
        self.enable_button.setEnabled(True)
        if not answer.get("ok"):
            self.state.setText(str(answer.get("message") or "That did not work."))
            self.details.hide()
            return
        self.status = answer
        lines = remote_cli.describe(answer, str(answer.get("password") or "") if password else "", console=False)
        enabled = bool(answer.get("enabled"))
        self.state.setText(lines[0] if lines else "")
        self.details.setText("\n".join(lines[1:]))
        self.details.setVisible(len(lines) > 1)
        self.enable_button.setVisible(not enabled)
        self.disable_button.setVisible(enabled)
        self.password.setVisible(not enabled)
        self.show_password.setVisible(not enabled)
        if enabled:
            self.password.clear()
