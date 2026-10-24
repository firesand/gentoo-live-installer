"""Step 5: release everything the tool holds (mounts, unlocked disks), keep a record of what was done, and restart."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QFileDialog, QPushButton

from ..widgets import Card, wrapped_label
from . import launcher, messages, model
from .base import Busy, InlineConfirm, Notice, RescuePage, soft_breaks, tone
from .client import Call, RescueClient

FORCE_WARNING = (
    "Forcing detaches the system even though programs still use it. Files that are open in those programs may be damaged. "
    "Close the programs listed above first if you can."
)


class FinishPage(RescuePage):
    title = "Finish"
    subtitle = "Close the system safely before you restart or switch the computer off."
    step = 4
    backRequested = Signal()
    anotherRequested = Signal()
    quitRequested = Signal()
    released = Signal()  # everything the tool held is released

    def __init__(self, client: RescueClient) -> None:
        super().__init__()
        self.client = client
        self.system: model.OpenSystem | None = None
        self.state = "ready"
        self.report_path = ""
        self._call: Call | None = None

        self.did = wrapped_label("")
        self.did.setTextFormat(Qt.TextFormat.RichText)
        self.content.addWidget(self.did)
        self.notice = Notice()
        self.content.addWidget(self.notice)
        self.busy = Busy()
        self.content.addWidget(self.busy)

        self.record_card = Card("Keep a record", "The tool writes down every command it runs. This record is useful if something goes wrong later.")
        self.record_where = wrapped_label("", muted=True, selectable=True)
        self.record_card.content.addWidget(self.record_where)
        self.save_inside = QCheckBox("Also save a copy of the record inside the repaired system")
        self.save_inside.setObjectName("rescueSaveInside")
        self.save_inside.setChecked(True)
        self.record_card.content.addWidget(self.save_inside)
        self.save_button = QPushButton("&Save a copy to a file…")
        self.save_button.setObjectName("rescueSaveReport")
        self.save_button.setToolTip("Save the record to a file, for example on a USB stick")
        self.save_button.clicked.connect(self._save_report)
        self.record_card.content.addWidget(self.save_button)
        self.content.addWidget(self.record_card)

        self.holders = wrapped_label("", selectable=True)
        self.holders.setTextFormat(Qt.TextFormat.RichText)
        self.content.addWidget(self.holders)
        self.confirm = InlineConfirm()
        self.confirm.confirmed.connect(self._force_confirmed)
        self.content.addWidget(self.confirm)
        self.after = Card("The system is closed", "You can remove the live disc and restart the computer now.")
        self.where_report = wrapped_label("", selectable=True)
        self.after.content.addWidget(self.where_report)
        self.content.addWidget(self.after)
        self.content.addStretch()

        self.back_button = self.add_button("&Back", "rescueBack", right=False)
        self.back_button.clicked.connect(self.backRequested)
        self.quit_button = self.add_button("&Quit the repair tool", "rescueQuit", right=False)
        self.quit_button.clicked.connect(self.quitRequested)
        self.again_button = self.add_button("Repair &another system", "rescueAgain", right=False)
        self.again_button.clicked.connect(self.anotherRequested)
        self.force_button = self.add_button("&Force…", "rescueForce", tip="Detach the system even though programs still use it")
        self.force_button.clicked.connect(self._ask_force)
        self.reboot_button = self.add_button("&Restart the computer", "rescueReboot", primary=True)
        self.reboot_button.clicked.connect(self._reboot)
        self.unmount_button = self.add_button("&Close the system and finish", "rescueUnmount", primary=True, tip="Release every disk the tool opened")
        self.unmount_button.clicked.connect(lambda: self.unmount())
        self._set_state("ready")

    # ---- showing ----------------------------------------------------------------------------------------------------------------
    def begin(self, system: model.OpenSystem | None, history: list[tuple[str, str]]) -> None:
        self.system = system
        self.report_path = ""
        self.notice.clear()
        self.holders.clear()
        lines = [f'<span style="color:{tone(messages.OUTCOME_STATUS.get(outcome, "error"))}">'
                 f'{"✓" if outcome == "success" else "!" if outcome in {"warning", "cancelled"} else "×"}</span> {html.escape(title)} — '
                 f'{html.escape(messages.OUTCOME_TITLES.get(outcome, outcome or "result not known").rstrip("."))}' for title, outcome in history]
        self.did.setText("<b>What was done in this session</b><br>" + "<br>".join(lines) if lines else "No repair was made. The system was only looked at.")
        audit = system.audit_path if system is not None else ""
        self.record_where.setText(soft_breaks(f"The record is kept in the live system at {audit}. It is lost when you restart, unless you save it." if audit else "There is no record for this session."))
        self.record_card.setVisible(system is not None)
        self.save_inside.setVisible(system is not None and system.writable)
        self._set_state("ready")

    def _set_state(self, state: str) -> None:
        self.state = state
        ready = state in {"ready", "busy"}
        working = state == "working"
        done = state == "done"
        self.record_card.setVisible(not done and self.system is not None)
        self.after.setVisible(done)
        self.back_button.setVisible(ready)
        self.back_button.setEnabled(not working)
        self.unmount_button.setVisible(ready or working)
        self.unmount_button.setEnabled(not working)
        self.unmount_button.setText("&Try again" if state == "busy" else "&Close the system and finish")
        self.force_button.setVisible(state == "busy")
        self.reboot_button.setVisible(done)
        self.quit_button.setVisible(done)
        self.again_button.setVisible(done)
        self.save_button.setEnabled(not working and not done)
        self.did.setVisible(not done)

    # ---- the record --------------------------------------------------------------------------------------------------------------------
    def choose_report_path(self, suggestion: str) -> str:
        path, _ = QFileDialog.getSaveFileName(self, "Save the record", str(Path.home() / suggestion), "Text files (*.txt);;All files (*)")
        return path

    def _save_report(self) -> None:
        if self.system is None:
            return
        call = self.client.request("rescue.audit", {"session_id": self.system.session_id, "format": "text"})
        call.then(self._report_ready, lambda _c, text, _d: self.notice.show_message("error", text), lambda text: self.notice.show_message("error", text))

    def _report_ready(self, data: dict[str, Any]) -> None:
        name = str(data.get("name", "repair-record")).rsplit(".", 1)[0] + ".txt"
        path = self.choose_report_path(name)
        if not path:
            return
        try:
            Path(path).write_text(str(data.get("text", "")), encoding="utf-8")
        except OSError as exc:
            self.notice.show_message("error", f"The record could not be saved: {exc.strerror or exc}")
            return
        self.notice.show_message("ok", f"The record was saved as {path}.")

    # ---- releasing ----------------------------------------------------------------------------------------------------------------------
    def unmount(self, *, force: bool = False) -> None:
        if self.state == "working":
            return
        self.notice.clear()
        self.holders.clear()
        self._set_state("working")
        self.busy.start("Closing the system…")
        params: dict[str, Any] = {"force": force}
        if self.system is not None:
            params.update({"session_id": self.system.session_id, "save_report": self.save_inside.isChecked() and self.system.writable})
        self._call = self.client.request("rescue.unmount", params)
        self._call.then(self._done, self._failed, self._lost)

    def _done(self, result: dict[str, Any]) -> None:
        self.busy.stop()
        self.report_path = str(result.get("report", ""))
        audit = self.system.audit_path if self.system is not None else ""
        lines = []
        if self.report_path:
            lines.append(f"A copy of the record was saved inside the repaired system: {self.report_path}")
        if audit:
            lines.append(f"The record in the live system: {audit} (lost when you restart)")
        self.where_report.setText(soft_breaks("\n".join(lines)))
        self.where_report.setVisible(bool(lines))
        self._set_state("done")
        self.reboot_button.setFocus()
        self.released.emit()

    def _failed(self, code: str, text: str, data: dict[str, Any]) -> None:
        self.busy.stop()
        result: dict[str, Any] = self._call.result if self._call is not None else {}
        if code == "rescue.not_found":  # it was released already (for example by a try whose answer was lost)
            self._done(result)
            return
        holders = result.get("busy_holders") or []
        details = model.error_details(data)
        holders = holders or details.get("busy_holders") or []
        lines = []
        if holders:
            lines.append("These programs still use the system. Close them, then press “Try again”:")
            lines.extend(f"&nbsp;&nbsp;{html.escape(str(item.get('command', '?')))} (user {html.escape(str(item.get('user', '?')))}, process {item.get('pid', '?')})" for item in holders)
        elif details.get("shells"):
            lines.append(f"{details['shells']} terminal window(s) are still open in the system. Close them, then press “Try again”.")
        self.holders.setText("<br>".join(lines))
        self.holders.setVisible(bool(lines))
        self.notice.show_message("error", text)
        self._set_state("busy")

    def _lost(self, text: str) -> None:
        self.busy.stop()
        self.notice.show_message("error", text + " Press “Try again”: if the system was already closed, the tool says so.")
        self._set_state("busy")
        self.force_button.setVisible(False)

    def _ask_force(self) -> None:
        self.confirm.ask("Force the system closed?", FORCE_WARNING, "&Force", danger=True)

    def _force_confirmed(self, _typed: str, _phrase: str, _tick: bool) -> None:
        self.unmount(force=True)

    def _reboot(self) -> None:
        if not launcher.restart_computer():
            self.notice.show_message("warning", "The computer could not be restarted automatically. Remove the disc and restart it with its power button.")

    def close_blocker(self) -> str:
        return "The system is being closed. Please wait a moment." if self.state == "working" else ""

    def focus_first(self) -> None:
        (self.unmount_button if self.unmount_button.isVisibleTo(self) else self.reboot_button).setFocus()
