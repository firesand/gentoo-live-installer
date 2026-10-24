"""Step 2: show what will be opened (a dry run), then open the system read-only with exactly that plan."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..widgets import Card, wrapped_label
from . import messages, model
from .base import Busy, Expander, FlowText, Notice, RescuePage, soft_breaks, tone
from .client import Call, RescueClient
from .find_page import clear_layout


class MountPage(RescuePage):
    title = "Open the system"
    subtitle = "Nothing is changed yet. The system is opened for looking only."
    step = 1
    opened = Signal(dict)  # the reply of rescue.mount: the session, the findings and the actions
    backRequested = Signal()
    serviceProblem = Signal(str)
    adoptRequested = Signal(str)  # the root device of a system that may already be open in a session

    def __init__(self, client: RescueClient) -> None:
        super().__init__()
        self.client = client
        self.candidate: dict[str, Any] = {}
        self.choice: model.MountChoice | None = None
        self.mount_plan: dict[str, Any] = {}
        self._call: Call | None = None
        self._busy = False
        self._not_adopted_text = ""

        self.summary = wrapped_label("")
        self.summary.setObjectName("cardTitle")
        self.content.addWidget(self.summary)
        self.reassurance = wrapped_label(
            "The tool opens the disks of this system read-only, so nothing can be damaged while you look. "
            "A repair asks again before it changes anything.", muted=True)
        self.content.addWidget(self.reassurance)
        self.inspect_only = QCheckBox("Look only: never change anything on this system")
        self.inspect_only.setObjectName("rescueInspectOnly")
        self.inspect_only.setToolTip("Repairs are not possible in this mode. Use it to read logs or look at files.")
        self.inspect_only.toggled.connect(self._inspect_toggled)
        self.content.addWidget(self.inspect_only)
        self.notice = Notice()
        self.content.addWidget(self.notice)
        self.busy = Busy()
        self.content.addWidget(self.busy)

        self.table_card = Card("These file systems will be opened")
        self.table = QGridLayout()
        self.table.setHorizontalSpacing(14)
        self.table_card.content.addLayout(self.table)
        self.content.addWidget(self.table_card)

        self.missing_card = Card("Not found", "The system's file list (fstab) names disks that are not in this computer. They are not opened.")
        self.missing_rows = QVBoxLayout()
        self.missing_card.content.addLayout(self.missing_rows)
        self.content.addWidget(self.missing_card)

        self.suggest_card = Card("Suggestions", "The system's file list does not say where these belong. You can add them.")
        self.suggest_rows = QVBoxLayout()
        self.suggest_card.content.addLayout(self.suggest_rows)
        self.content.addWidget(self.suggest_card)

        self.warnings = wrapped_label("")
        self.warnings.setStyleSheet(f"color: {tone('warning')};")
        self.content.addWidget(self.warnings)
        self.commands_text = FlowText(mono=True)
        self.commands = Expander("Show the exact commands", self.commands_text)
        self.content.addWidget(self.commands)
        self.content.addStretch()

        self.back_button = self.add_button("&Back", "rescueBack", right=False)
        self.back_button.clicked.connect(self.backRequested)
        self.again_button = self.add_button("&Check again", "rescueCheckAgain", right=False, tip="Make the plan again")
        self.again_button.clicked.connect(self._dry_run)
        self.again_button.hide()
        self.open_button = self.add_button("&Open read-only", "rescueMount", primary=True, tip="Open the system exactly as shown above")
        self.open_button.clicked.connect(self._open)
        self.open_button.setEnabled(False)

    # ---- the plan -------------------------------------------------------------------------------------------------------------------
    def begin(self, candidate: dict[str, Any], scan: dict[str, Any]) -> None:
        snapshot = candidate.get("kind") == "snapshot"
        self.candidate = candidate
        self.choice = model.MountChoice(str(candidate["id"]), str(scan.get("scan_id", "")), allow_write=not snapshot)
        row = model.describe_system(candidate, scan.get("devices", []))
        self.summary.setText(soft_breaks(f"{row.title} on {row.device}"))
        self.inspect_only.blockSignals(True)
        self.inspect_only.setChecked(snapshot)
        self.inspect_only.blockSignals(False)
        self.inspect_only.setEnabled(not snapshot)
        self.inspect_only.setToolTip("A snapshot is read-only history: it can only be looked at." if snapshot else self.inspect_only.toolTip())
        self.mount_plan = {}
        self.notice.clear()
        self._draw()
        self._dry_run()

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self._busy = busy
        if busy:
            self.busy.start(text)
        else:
            self.busy.stop()
        self.back_button.setEnabled(not busy)
        self.inspect_only.setEnabled(not busy and self.candidate.get("kind") != "snapshot")
        self.open_button.setEnabled(not busy and bool(self.mount_plan))
        self.again_button.setEnabled(not busy)

    def _dry_run(self) -> None:
        if self.choice is None or self._busy:
            return
        self.notice.clear()
        self.again_button.hide()
        self._set_busy(True, "Working out what to open…")
        self._call = self.client.request("rescue.mount", {**self.choice.request(), "dry_run": True})
        self._call.then(self._planned, self._plan_failed, self._lost)

    def _planned(self, result: dict[str, Any]) -> None:
        self.mount_plan = result.get("mount_plan", {})
        self._set_busy(False)
        self._draw()

    def _plan_failed(self, code: str, text: str, data: dict[str, Any]) -> None:
        self.mount_plan = {}
        self._set_busy(False)
        self._draw()
        details = model.error_details(data)
        if code == "rescue.mounted_elsewhere":
            where = details.get("mountpoint", "another place")
            self._not_adopted_text = (f"The disk {details.get('device', '')} is already open at {where}. The file manager probably opened it.\n"
                                      "Close it there (use the eject button next to the disk), then press “Check again”.")
            # The mount may be the tool's own: a session that an earlier window left open. The window looks for it before blaming the file manager.
            self.notice.show_message("info", "Checking whether this system is already open in a repair session…")
            self.adoptRequested.emit(str(self.candidate.get("device", "")))
            return
        self.notice.show_message("error", text)

    def _lost(self, text: str) -> None:
        self._set_busy(False)
        if self._call is not None and not self._call.connected:
            self.serviceProblem.emit(text)
            return
        self.notice.show_message("error", text + " Press “Check again” to try once more.")
        self.again_button.show()

    # ---- drawing ----------------------------------------------------------------------------------------------------------------------
    def _draw(self) -> None:
        plan = self.mount_plan
        clear_layout(self.table)
        self.table_card.setVisible(bool(plan))
        for column, heading in enumerate(("Folder", "Disk", "Type", "Used for", "Note")):
            label = QLabel(heading)
            label.setProperty("muted", True)
            self.table.addWidget(label, 0, column)
        for index, row in enumerate(model.mount_rows(plan) if plan else [], start=1):
            if row.optional:
                first: QWidget = QCheckBox(row.mountpoint)
                first.setChecked(row.selected)  # type: ignore[attr-defined]
                first.toggled.connect(lambda checked, mountpoint=row.mountpoint: self._data_toggled(mountpoint, checked))  # type: ignore[attr-defined]
            else:
                first = QLabel(row.mountpoint)
            first.setObjectName(f"rescueMountRow:{row.mountpoint}")
            first.setAccessibleName(f"{row.mountpoint} on {row.device}")
            self.table.addWidget(first, index, 0)
            for column, text in enumerate((row.device, row.fstype, row.kind, row.note), start=1):
                cell = QLabel(soft_breaks(text))
                cell.setWordWrap(True)
                self.table.addWidget(cell, index, column)
        self.table.setColumnStretch(4, 1)
        self._draw_missing(plan)
        self._draw_suggestions(plan)
        warnings = list(plan.get("warnings", []))
        if plan.get("ignored_options"):
            warnings.append("Some mount options of the system were not used here: " + ", ".join(plan["ignored_options"]))
        self.warnings.setText(soft_breaks("\n".join(f"! {item}" for item in warnings)))
        self.warnings.setVisible(bool(warnings))
        commands = plan.get("commands") or []
        self.commands_text.set_lines([str(item) for item in commands])
        self.commands.setVisible(bool(commands))
        self.open_button.setEnabled(bool(plan) and not self._busy)

    def _draw_missing(self, plan: dict[str, Any]) -> None:
        clear_layout(self.missing_rows)
        entries = plan.get("unresolved", [])
        for entry in entries:
            self.missing_rows.addWidget(wrapped_label(soft_breaks(f"{entry['mountpoint']}: {entry['reason']} (fstab says {entry['spec']})")))
        self.missing_card.setVisible(bool(entries))

    def _draw_suggestions(self, plan: dict[str, Any]) -> None:
        clear_layout(self.suggest_rows)
        offered = [item for item in plan.get("suggestions", []) if not any(o["mountpoint"] == item["mountpoint"] for o in (self.choice.overrides if self.choice else []))]
        for item in offered:
            row = QHBoxLayout()
            row.addWidget(wrapped_label(soft_breaks(f"Open {item['device']} at {item['mountpoint']} ({item['reason']})")), 1)
            button = QPushButton("&Add")
            button.setObjectName(f"rescueSuggest:{item['mountpoint']}")
            button.setAccessibleName(f"Add {item['device']} at {item['mountpoint']}")
            button.clicked.connect(lambda _=False, entry=item: self._add_override(entry))
            row.addWidget(button)
            self.suggest_rows.addLayout(row)
        self.suggest_card.setVisible(bool(offered))

    # ---- changing the plan --------------------------------------------------------------------------------------------------------
    def _inspect_toggled(self, checked: bool) -> None:
        if self.choice is not None:
            self.choice.allow_write = not checked
            self._dry_run()

    def _data_toggled(self, mountpoint: str, checked: bool) -> None:
        if self.choice is None:
            return
        wanted = [item for item in self.choice.include_tier4 if item != mountpoint]
        self.choice.include_tier4 = wanted + ([mountpoint] if checked else [])
        self._dry_run()

    def _add_override(self, entry: dict[str, Any]) -> None:
        if self.choice is not None:
            self.choice.overrides.append({"mountpoint": str(entry["mountpoint"]), "device": str(entry["device"])})
            self._dry_run()

    # ---- opening --------------------------------------------------------------------------------------------------------------------
    def _open(self) -> None:
        if self.choice is None or not self.mount_plan or self._busy:
            return
        request = {**self.choice.request(), "plan_digest": self.mount_plan["digest"]}  # the same request the dry run made, plus the digest it returned
        self._set_busy(True, "Opening the system…")
        self.open_button.setEnabled(False)
        self._call = self.client.request("rescue.mount", request)
        self._call.eventReceived.connect(self._open_event)
        self._call.then(self._mounted, self._mount_failed, self._lost_while_opening)

    def _open_event(self, event: dict[str, Any]) -> None:
        if event.get("type") in {"stage", "progress"} and event.get("message"):
            self.busy.progress(str(event["message"]))

    def _mounted(self, info: dict[str, Any]) -> None:
        self._set_busy(False)
        self.opened.emit(info)

    def _mount_failed(self, code: str, text: str, data: dict[str, Any]) -> None:
        self._set_busy(False)
        if code in messages.STALE_PLAN_CODES:
            self.notice.show_message("warning", "The system changed while you were looking. The plan was made again; please read it before you continue.")
            self._dry_run()
            return
        self.notice.show_message("error", text)
        self.again_button.show()

    def _lost_while_opening(self, text: str) -> None:
        """The answer never arrived, but the system may be open: look for the session before anything is tried twice."""
        self._set_busy(False)
        self._not_adopted_text = "The system was not opened. Press “Open read-only” to try again."
        self.notice.show_message("warning", text + " Checking whether the system was opened…")
        self.adoptRequested.emit(str(self.candidate.get("device", "")))

    def not_adopted(self) -> None:
        """No session of the daemon holds this system: what to do instead (the message set by whoever asked)."""
        self.notice.show_message("error" if "file manager" in self._not_adopted_text else "warning", self._not_adopted_text)
        self.again_button.show()

    def focus_first(self) -> None:
        (self.open_button if self.open_button.isEnabled() else self.back_button).setFocus()
