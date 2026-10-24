"""Step 3: what is wrong, and the repairs that fit. The recommended repair comes first; every other tool is grouped by what it touches."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ..widgets import Card, wrapped_label
from . import launcher, messages, model
from .base import Busy, Expander, InlineConfirm, Notice, RescuePage, danger_style, primary_style, soft_breaks
from .client import RescueClient
from .find_page import clear_layout

SEVERITY_COLORS = {"error": "#c33d50", "warn": "#b87818", "info": "#6d6979"}


class DiagnosisPage(RescuePage):
    title = "What is wrong"
    subtitle = "The checks only read the system. Choose a repair; you see every command before it runs."
    step = 2
    actionRequested = Signal(dict, bool)  # the action's entry in the catalogue, "the disk was moved to another computer"
    logsRequested = Signal()
    hardwareChanged = Signal(bool)
    refreshRequested = Signal()
    finishRequested = Signal()

    def __init__(self, client: RescueClient) -> None:
        super().__init__()
        self.client = client
        self.system: model.OpenSystem | None = None
        self.moved = False

        head = QHBoxLayout()
        self.system_label = wrapped_label("")
        self.system_label.setObjectName("cardTitle")
        head.addWidget(self.system_label, 1)
        self.mode_label = QLabel()
        self.mode_label.setObjectName("rescueMode")
        head.addWidget(self.mode_label)
        self.content.addLayout(head)
        self.summary = wrapped_label("", muted=True)
        self.content.addWidget(self.summary)
        self.notice = Notice()
        self.content.addWidget(self.notice)
        self.busy = Busy()
        self.content.addWidget(self.busy)

        self.recommended_card = Card("Recommended repair")
        self.recommended_text = wrapped_label("")
        self.recommended_card.content.addWidget(self.recommended_text)
        self.recommended_button = QPushButton("&Fix everything recommended")
        self.recommended_button.setObjectName(f"rescueAction:{model.RECOMMENDED_ACTION}")
        self.recommended_button.setStyleSheet(primary_style())
        self.recommended_button.clicked.connect(lambda: self._request(model.RECOMMENDED_ACTION))
        self.recommended_card.content.addWidget(self.recommended_button)
        self.recommended_reason = wrapped_label("", muted=True)
        self.recommended_card.content.addWidget(self.recommended_reason)
        self.content.addWidget(self.recommended_card)

        self.findings_box = QVBoxLayout()
        self.findings_box.setSpacing(8)
        self.content.addLayout(self.findings_box)

        self.moved_box = QCheckBox("This disk was moved to another computer, or the hardware changed")
        self.moved_box.setObjectName("rescueHardwareChanged")
        self.moved_box.setToolTip("The startup files of a moved system may miss the drivers of the new computer. This adds the matching advice.")
        self.moved_box.toggled.connect(self._moved_toggled)
        self.content.addWidget(self.moved_box)

        self.groups_box = QVBoxLayout()
        self.groups_box.setSpacing(10)
        self.content.addLayout(self.groups_box)

        self.look_card = Card("Look around", "Read the logs of the system, or open a terminal inside it.")
        self.logs_button = QPushButton("View the &logs…")
        self.logs_button.setObjectName("rescueLogs")
        self.logs_button.clicked.connect(self.logsRequested)
        self.look_card.content.addWidget(self.logs_button)
        self.terminal_button = QPushButton("Open a &terminal in this system…")
        self.terminal_button.setObjectName("rescueTerminal")
        self.terminal_button.clicked.connect(lambda: self._ask_terminal(False))
        self.look_card.content.addWidget(self.terminal_button)
        self.terminal_text = wrapped_label("", muted=True)
        self.look_card.content.addWidget(self.terminal_text)
        self.writable_terminal_button = QPushButton("Open a terminal that can chan&ge files…")
        self.writable_terminal_button.setObjectName("rescueTerminalWritable")
        self.writable_terminal_button.clicked.connect(lambda: self._ask_terminal(True))
        self.look_card.content.addWidget(self.writable_terminal_button)
        self.confirm = InlineConfirm()
        self.confirm.confirmed.connect(self._terminal_confirmed)
        self.look_card.content.addWidget(self.confirm)
        self.content.addWidget(self.look_card)
        self.content.addStretch()
        self._terminal_writable = False

        self.refresh_button = self.add_button("&Check again", "rescueRefresh", right=False, tip="Run the checks again")
        self.refresh_button.clicked.connect(self.refreshRequested)
        self.finish_button = self.add_button("Fi&nish…", "rescueFinish", tip="Close the system safely and leave the repair tool")
        self.finish_button.clicked.connect(self.finishRequested)

    # ---- showing a system -----------------------------------------------------------------------------------------------------------
    def show_system(self, system: model.OpenSystem) -> None:
        self.system = system
        self.system_label.setText(soft_breaks(f"{system.title} on {system.root_device}"))
        if not system.allow_write:
            self.mode_label.setText("LOOK ONLY")
            self.mode_label.setStyleSheet("color: #6d6979; font-weight: 700;")
        elif system.writable:
            self.mode_label.setText("REPAIRS ENABLED")
            self.mode_label.setStyleSheet("color: #b87818; font-weight: 700;")
        else:
            self.mode_label.setText("READ-ONLY")
            self.mode_label.setStyleSheet("color: #27855c; font-weight: 700;")
        findings = model.sorted_findings(system.findings)
        counts = model.finding_counts(findings)
        if not findings:
            self.summary.setText("The checks found no problem.")
        else:
            parts = [f"{counts['error']} problem{'s' if counts['error'] != 1 else ''}" if counts["error"] else "", f"{counts['warn']} warning{'s' if counts['warn'] != 1 else ''}" if counts["warn"] else "",
                     f"{counts['info']} note{'s' if counts['info'] != 1 else ''}" if counts["info"] else ""]
            self.summary.setText("The checks found " + ", ".join(part for part in parts if part) + ".")
        notes = [str(item) for item in system.info.get("warnings", [])]
        if notes:
            self.notice.show_message("warning", "\n".join(notes))
        else:
            self.notice.clear()
        self._draw_recommended(system)
        self._draw_findings(findings, system)
        self._draw_groups(system)
        self._draw_look(system)

    def _draw_recommended(self, system: model.OpenSystem) -> None:
        spec = model.action_by_id(system.actions, model.RECOMMENDED_ACTION)
        wanted = system.recommended
        if not wanted or spec is None:
            self.recommended_card.hide()
            return
        titles = {str(item.get("id")): str(item.get("title")) for item in system.actions}
        steps = [f"{number}. {titles.get(name, name)}" for number, name in enumerate(wanted, start=1)]
        self.recommended_text.setText("The checks found problems that can stop the computer from starting. This repair fixes them in the right order, as one plan you can read first:\n" + "\n".join(steps))
        available = bool(spec.get("available", True))
        self.recommended_button.setEnabled(available)
        self.recommended_reason.setText("" if available else f"Not available: {spec.get('reason', '')}")
        self.recommended_reason.setVisible(not available)
        self.recommended_card.show()

    def _draw_findings(self, findings: list[dict[str, Any]], system: model.OpenSystem) -> None:
        clear_layout(self.findings_box)
        if not findings:
            ok = Notice()
            ok.show_message("ok", "No problem was found. If the computer still does not start, read the logs or open a terminal.")
            self.findings_box.addWidget(ok)
            return
        specs = {str(item.get("id")): item for item in system.actions}
        for finding in findings:
            self.findings_box.addWidget(self._finding_card(finding, specs))

    def _finding_card(self, finding: dict[str, Any], specs: dict[str, dict[str, Any]]) -> Card:
        severity = str(finding.get("severity", "info"))
        card = Card()
        card.setProperty("finding", finding.get("id"))
        card.setAccessibleName(f"{messages.SEVERITY_TITLES.get(severity, severity)}: {finding.get('title')}")
        title = QLabel(f"{messages.SEVERITY_SYMBOLS.get(severity, 'i')}  {finding.get('title', '')}")
        title.setObjectName("cardTitle")
        title.setWordWrap(True)
        title.setStyleSheet(f"color: {SEVERITY_COLORS.get(severity, '#6d6979')};")
        card.content.addWidget(title)
        card.content.addWidget(wrapped_label(soft_breaks(str(finding.get("detail", "")))))
        evidence = [str(item) for item in finding.get("evidence", [])]
        if evidence:
            lines = wrapped_label(soft_breaks("\n".join(evidence)), muted=True, selectable=True)
            lines.setStyleSheet("font-family: monospace;")
            card.content.addWidget(Expander("Details", lines))
        for name in finding.get("actions", []):
            spec = specs.get(str(name))
            if spec is None:
                continue
            button = QPushButton(str(spec.get("title", name)))
            button.setObjectName(f"rescueFinding:{finding.get('id')}:{name}")
            button.setEnabled(bool(spec.get("available", True)))
            button.setToolTip("" if spec.get("available", True) else str(spec.get("reason", "")))
            button.clicked.connect(lambda _=False, action=str(name): self._request(action))
            card.content.addWidget(button)
        return card

    def _draw_groups(self, system: model.OpenSystem) -> None:
        clear_layout(self.groups_box)
        for group in model.group_actions(system.actions):
            card = Card(group.title, group.help)
            for spec in group.actions:
                self._add_action(card, spec)
            self.groups_box.addWidget(card)

    def _add_action(self, card: Card, spec: dict[str, Any]) -> None:
        name = str(spec["id"])
        button = QPushButton(str(spec.get("title", name)))
        button.setObjectName(f"rescueAction:{name}")
        available = bool(spec.get("available", True))
        button.setEnabled(available)
        if spec.get("recommended"):
            button.setText(button.text() + "  (recommended)")
        button.clicked.connect(lambda _=False, action=name: self._request(action))
        card.content.addWidget(button)
        text = str(spec.get("description", "")) if available else f"Not available: {spec.get('reason', '')}"
        if text:
            card.content.addWidget(wrapped_label(soft_breaks(text), muted=True))

    def _draw_look(self, system: model.OpenSystem) -> None:
        if not system.allow_write:
            self.terminal_button.setText("Open a &terminal in the mount folder…")
            self.terminal_text.setText("This system was opened for looking only, so its own programs do not run. The terminal is a shell of this live system in the folder where the system is mounted.")
        else:
            self.terminal_button.setText("Open a &terminal in this system…")
            self.terminal_text.setText("The terminal runs inside the system as the root user. It is read-only until you enable repairs.")
        self.writable_terminal_button.setVisible(system.writable and system.allow_write)

    # ---- actions --------------------------------------------------------------------------------------------------------------------
    def _request(self, action: str) -> None:
        if self.system is None:
            return
        spec = model.action_by_id(self.system.actions, action)
        if spec is not None:
            self.actionRequested.emit(spec, self.moved)

    def _moved_toggled(self, checked: bool) -> None:
        self.moved = checked
        self.hardwareChanged.emit(checked)

    def set_busy(self, text: str = "") -> None:
        if text:
            self.busy.start(text)
        else:
            self.busy.stop()
        for button in (self.refresh_button, self.finish_button):
            button.setEnabled(not text)

    # ---- the terminal ---------------------------------------------------------------------------------------------------------------
    def _ask_terminal(self, writable: bool) -> None:
        if self.system is None:
            return
        self._terminal_writable = writable
        name = self.system.root_device.rsplit("/", 1)[-1]
        if not self.system.allow_write:
            body = "A terminal is a shell for the root user. Everything you type there is done for real. Only open it if you know what you want to do."
        elif writable:
            body = "This terminal can change files in the system. A wrong command can stop the system from starting. Make a backup first if you can."
        else:
            body = "The terminal runs the system's own shell as the root user. It starts read-only, but programs can still do harm. Only open it if you know what you want to do."
        self.confirm.ask("Open a terminal?", body, "Open the &terminal", expected=name, phrase=model.BACKUP_PHRASE if writable else "", danger=writable)

    def _terminal_confirmed(self, _typed: str, _phrase: str, _tick: bool) -> None:
        system = self.system
        if system is None:
            return
        mode = "look only" if not system.allow_write else "read-write" if self._terminal_writable else "read-only"
        started, problem = launcher.open_terminal(self.client.socket_path, system.session_id, system.root_device, f"Repair: {system.title} ({mode})",
                                                  writable=self._terminal_writable, host=not system.allow_write)
        if started:
            self.notice.show_message("info", "A terminal window was opened. Type exit in it when you are done. Close the system with “Finish” when you leave the repair tool.")
        else:
            self.notice.show_message("error", problem)

    def focus_first(self) -> None:
        if self.recommended_card.isVisibleTo(self) and self.recommended_button.isEnabled():
            self.recommended_button.setFocus()
        else:
            self.logs_button.setFocus()
