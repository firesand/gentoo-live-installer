"""Steps 4 and 5: the options of a repair, the plan with everything that will change, the confirmation, the run and its result.

The window never decides what a repair does: the engine makes a plan (the exact commands, file changes, warnings) and says what must
be confirmed before it runs (``confirm``: a review, the root device's name, the backup phrase). This page shows the plan, collects
exactly that, and sends the plan's digest back, so the plan that runs is the plan that was read.

While a command of the plan runs that is not atomic (a boot loader or initramfs tool, a mount), stopping it halfway can leave the
system half-written: Cancel and closing the window are refused then, and only a deliberate "Stop now" is possible.
"""

from __future__ import annotations

import html
from typing import Any

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QCheckBox, QLabel, QLineEdit, QListWidget, QListWidgetItem, QProgressBar, QVBoxLayout, QWidget

from ..widgets import Card, wrapped_label
from . import messages, model
from .base import Busy, Expander, FlowText, InlineConfirm, Notice, RescuePage, SecretField, TextView, danger_style, primary_style, soft_breaks, tone
from .client import Call, RescueClient
from .find_page import clear_layout
from .options_form import OptionsForm

RISK_TONES = {"info": "ok", "low": "ok", "medium": "warning", "high": "error", "dangerous": "error"}
STEP_SYMBOLS = {"pending": "○", "running": "●", "done": "✓", "failed": "×", "skipped": "–"}
LOG_FLUSH_MS = 100


class PlanPage(RescuePage):
    title = "Review and repair"
    subtitle = "Read what will change. Nothing is written before you confirm."
    step = 3
    leaveRequested = Signal(bool)  # back to the diagnosis; True when the system may have changed
    runningChanged = Signal(bool)

    RECONNECT_MS = 2000
    RECONNECT_TRIES = 30

    def __init__(self, client: RescueClient) -> None:
        super().__init__()
        self.client = client
        self.system: model.OpenSystem | None = None
        self.spec: dict[str, Any] = {}
        self.moved = False
        self.params: dict[str, Any] = {}
        self.plan: dict[str, Any] = {}
        self.confirmation = model.Confirmation()
        self.tracker: model.RunTracker | None = None
        self.state = "idle"
        self._ran = False
        self._cancel_requested = False
        self._reconnect_tries = 0
        self._gave_up = False
        self._pending_log: list[str] = []
        self._log_timer = QTimer(self)
        self._log_timer.setSingleShot(True)
        self._log_timer.timeout.connect(self._flush_log)
        self._secret_fields: dict[str, tuple[SecretField, SecretField]] = {}
        self._form: OptionsForm | None = None
        self._calls: list[Call] = []
        self._generation = 0  # counts the repairs started on this page: the late answer to an earlier one must not land in a later one

        self.heading = wrapped_label("")
        self.heading.setObjectName("sectionTitle")
        self.content.addWidget(self.heading)
        self.risk_label = wrapped_label("")
        self.content.addWidget(self.risk_label)
        self.notice = Notice()
        self.content.addWidget(self.notice)
        self.busy = Busy()
        self.content.addWidget(self.busy)

        self.options_card = Card("Options")
        self.options_holder = QVBoxLayout()
        self.options_card.content.addLayout(self.options_holder)
        self.content.addWidget(self.options_card)

        self._build_review()
        self._build_run()
        self.content.addStretch()

        self.cancel_button = self.add_button("Cancel", "planCancel", right=False)
        self.cancel_button.clicked.connect(self._cancel_clicked)
        self.stop_button = self.add_button("Stop &now…", "planStop", right=False, tip="Kill the command that is running. Only for a step that is stuck.")
        self.stop_button.clicked.connect(self._ask_stop)
        self.options_button = self.add_button("Change &options", "planOptions", right=False)
        self.options_button.clicked.connect(self._show_options)
        self.reconnect_button = self.add_button("&Reconnect", "planReconnect", right=False)
        self.reconnect_button.clicked.connect(self._reconnect_now)
        self.continue_button = self.add_button("&Show me what will change", "planContinue", primary=True)
        self.continue_button.clicked.connect(lambda: self._request_plan())
        self.run_button = self.add_button("&Start the repair", "planRun", primary=True)
        self.run_button.clicked.connect(self._start_run)
        self.done_button = self.add_button("&Back to the diagnosis", "planDone", primary=True)
        self.done_button.clicked.connect(lambda: self.leaveRequested.emit(self._ran))
        self._set_state("idle")

    # ---- building the sections -------------------------------------------------------------------------------------------------------
    def _build_review(self) -> None:
        self.review_box = QWidget()
        column = QVBoxLayout(self.review_box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)
        self.summary = wrapped_label("")
        column.addWidget(self.summary)
        self.first_write = Notice()
        column.addWidget(self.first_write)
        self.checks = wrapped_label("")
        self.checks.setTextFormat(Qt.TextFormat.RichText)
        column.addWidget(self.checks)
        self.advice = wrapped_label("")
        self.advice.setTextFormat(Qt.TextFormat.RichText)
        column.addWidget(self.advice)
        self.prep_text = FlowText()
        prep_holder = QWidget()
        prep_column = QVBoxLayout(prep_holder)
        prep_column.setContentsMargins(0, 0, 0, 0)
        self.prep_note = wrapped_label("", muted=True)
        prep_column.addWidget(self.prep_note)
        prep_column.addWidget(self.prep_text)
        self.prep = Expander("Preparation", prep_holder)
        self.commands_text = FlowText()
        commands_holder = QWidget()
        commands_column = QVBoxLayout(commands_holder)
        commands_column.setContentsMargins(0, 0, 0, 0)
        commands_column.addWidget(self.prep)
        commands_column.addWidget(self.commands_text)
        self.commands = Expander("The commands that will run", commands_holder, open_=True)
        column.addWidget(self.commands)
        self.diff_view = TextView(2000)
        self.diff_view.setMaximumHeight(220)
        self.diff_view.setAccessibleName("Changes to files")
        self.diff_choices = wrapped_label("", muted=True)
        diff_holder = QWidget()
        diff_column = QVBoxLayout(diff_holder)
        diff_column.setContentsMargins(0, 0, 0, 0)
        diff_column.addWidget(self.diff_choices)
        diff_column.addWidget(self.diff_view)
        self.diff = Expander("Changes to files", diff_holder, open_=True)
        column.addWidget(self.diff)
        self.after_text = wrapped_label("", muted=True)
        self.after = Expander("Afterwards, the tool also runs", self.after_text)
        column.addWidget(self.after)
        self.secrets_card = Card("New password", "It is sent once, to the system, and is not stored or logged.")
        self.secrets_rows = QVBoxLayout()
        self.secrets_card.content.addLayout(self.secrets_rows)
        column.addWidget(self.secrets_card)
        self.confirm_card = Card("Confirm")
        self.reviewed = QCheckBox("I have read these commands and want to run them")
        self.reviewed.setObjectName("planReviewed")
        self.confirm_card.content.addWidget(self.reviewed)
        self.typed_label = wrapped_label("")
        self.confirm_card.content.addWidget(self.typed_label)
        self.typed = QLineEdit()
        self.typed.setObjectName("planTyped")
        self.confirm_card.content.addWidget(self.typed)
        self.phrase_label = wrapped_label("")
        self.confirm_card.content.addWidget(self.phrase_label)
        self.phrase = QLineEdit()
        self.phrase.setObjectName("planPhrase")
        self.confirm_card.content.addWidget(self.phrase)
        column.addWidget(self.confirm_card)
        for widget in (self.reviewed,):
            widget.toggled.connect(self._update_run)
        for edit in (self.typed, self.phrase):
            edit.textChanged.connect(self._update_run)
            edit.returnPressed.connect(self._enter_pressed)
        self.content.addWidget(self.review_box)

    def _build_run(self) -> None:
        self.run_box = QWidget()
        column = QVBoxLayout(self.run_box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        self.result_box = QWidget()
        result = QVBoxLayout(self.result_box)
        result.setContentsMargins(0, 0, 0, 0)
        result.setSpacing(8)
        self.result_title = wrapped_label("")
        self.result_title.setObjectName("sectionTitle")
        result.addWidget(self.result_title)
        self.result_text = wrapped_label("", selectable=True)
        self.result_text.setTextFormat(Qt.TextFormat.RichText)
        result.addWidget(self.result_text)
        column.addWidget(self.result_box)
        self.current_step = wrapped_label("")
        self.current_step.setObjectName("cardTitle")
        column.addWidget(self.current_step)
        self.step_warning = wrapped_label("")
        self.step_warning.setStyleSheet(f"color: {tone('warning')};")
        column.addWidget(self.step_warning)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setAccessibleName("Progress of the repair")
        column.addWidget(self.bar)
        self.stop_confirm = InlineConfirm()
        self.stop_confirm.confirmed.connect(self._stop_confirmed)
        column.addWidget(self.stop_confirm)
        self.steps_list = QListWidget()
        self.steps_list.setObjectName("planSteps")
        self.steps_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.steps_list.setMinimumHeight(110)
        self.steps_list.setMaximumHeight(190)
        self.steps_list.setAccessibleName("Steps of the repair")
        column.addWidget(self.steps_list)
        self.log = TextView(5000)
        self.log.setObjectName("planLog")
        self.log.setAccessibleName("Output of the repair")
        self.log.setMinimumHeight(140)
        column.addWidget(self.log)
        self.content.addWidget(self.run_box)

    # ---- states -----------------------------------------------------------------------------------------------------------------------
    def _set_state(self, state: str) -> None:
        was_running = self.state == "running"
        self.state = state
        self.options_card.setVisible(state == "options")
        self.risk_label.setVisible(state == "review")
        self.review_box.setVisible(state == "review")
        self.run_box.setVisible(state in {"running", "result"})
        self.result_box.setVisible(state == "result")
        self.steps_list.setVisible(state in {"running", "result"})
        self.bar.setVisible(state == "running")
        self.current_step.setVisible(state == "running")
        self.continue_button.setVisible(state == "options")
        self.run_button.setVisible(state == "review")
        self.done_button.setVisible(state == "result")
        self.options_button.setVisible(state in {"review", "failed"} and self._has_options())
        self.cancel_button.setVisible(state in {"options", "review", "running", "planning", "failed"})
        self.stop_button.setVisible(False)
        self.reconnect_button.setVisible(False)
        self.cancel_button.setText("Cancel the repair" if state == "running" else "Back" if state == "failed" else "Cancel")
        self.cancel_button.setEnabled(True)
        if state in {"result", "options", "review", "running", "planning", "failed"}:
            self.set_header({"result": "Result of the repair", "running": "Repairing…", "options": "Choose the options"}.get(state, "Review and repair"),
                            "The repair is running. Please do not switch the computer off." if state == "running" else
                            "Read what will change. Nothing is written before you confirm." if state in {"review", "options", "planning", "failed"} else
                            "What was done, and what to do next.")
        if (state == "running") != was_running:
            self.runningChanged.emit(state == "running")

    def is_running(self) -> bool:
        return self.state == "running"

    def _has_options(self) -> bool:
        return any(param.type != "object" for param in model.params_of(self.spec))

    def close_blocker(self) -> str:
        """While the last step the window heard of cannot be stopped safely, closing is refused. It stays refused while the connection is
        being restored (the step may still run); once the window has given up, there is nothing left to protect by waiting."""
        if self.state == "running" and self.tracker is not None and self.tracker.non_atomic_running and not self._gave_up:
            return messages.CANNOT_CLOSE_TEXT
        return ""

    # ---- starting ---------------------------------------------------------------------------------------------------------------------
    def _current(self, handler: Any) -> Any:
        """The handler, silent when the page has moved on to another repair since the request was sent."""
        generation = self._generation

        def guarded(*args: Any) -> None:
            if generation == self._generation:
                handler(*args)

        return guarded

    def begin(self, system: model.OpenSystem, spec: dict[str, Any], *, hardware_changed: bool = False) -> None:
        self._generation += 1
        self.system, self.spec, self.moved = system, spec, hardware_changed
        self.plan, self.tracker, self._ran, self._cancel_requested, self._form = {}, None, False, False, None
        self.params = {param.name: param.default for param in model.params_of(spec) if param.default not in (None, "") and param.type != "object"}
        if hardware_changed and spec.get("id") == "boot.initramfs":
            self.params["generic"] = True
        self.heading.setText(str(spec.get("title", "")))
        self.risk_label.clear()
        self.notice.clear()
        self.busy.stop()
        if model.asks_first(spec, hardware_changed=hardware_changed):
            self._show_options()
        else:
            self._request_plan()

    def _show_options(self) -> None:
        if self.system is None:
            return
        system = self.system
        clear_layout(self.options_holder)
        self._form = OptionsForm(self.spec, system.kernels, system.devices, system.root_device, self.params)
        self.options_holder.addWidget(self._form)
        self._set_state("options")
        names = {param.name for param in model.params_of(self.spec)}
        if "username" in names:
            self._fetch("acct.users", lambda data: self._form.offer_users(data.get("users", [])) if self._form is not None else None)
        if names & {"bootnum", "order"}:
            self._fetch("efi.list", lambda data: self._form.offer_boot_entries(data) if self._form is not None else None)
        self.focus_first()

    def _fetch(self, query: str, use: Any) -> None:
        if self.system is None:
            return
        call = self.client.request("rescue.query", {"session_id": self.system.session_id, "query": query})
        call.then(self._current(lambda result: use(result.get("data", {}))))
        self._calls.append(call)

    # ---- the plan -----------------------------------------------------------------------------------------------------------------
    def _request_plan(self, notice: str = "") -> None:
        if self.system is None:
            return
        if self._form is not None and self.state == "options":
            missing = self._form.missing()
            if missing:
                self.notice.show_message("error", "Please fill in: " + ", ".join(missing))
                return
            self.params = self._form.values()
        if notice:
            self.notice.show_message("warning", notice)
        else:
            self.notice.clear()
        self._set_state("planning")
        self.busy.start("Making the plan…")
        call = self.client.request("rescue.plan", {"session_id": self.system.session_id, "action_id": self.spec.get("id"), "params": self.params})
        call.then(self._current(self._planned), self._current(self._plan_failed), self._current(self._plan_lost))

    def _planned(self, plan: dict[str, Any]) -> None:
        self.busy.stop()
        self.plan = plan
        self._fill_review(plan)
        self._set_state("review")
        self._update_run()
        (self.reviewed if self.reviewed.isVisibleTo(self) else self.typed if self.typed.isVisibleTo(self) else self.run_button).setFocus()

    def _plan_failed(self, _code: str, text: str, _data: dict[str, Any]) -> None:
        self.busy.stop()
        self.notice.show_message("error", text)
        self._set_state("failed")

    def _plan_lost(self, text: str) -> None:
        self._plan_failed("", text + " Press “Back” and try again.", {})

    def _fill_review(self, plan: dict[str, Any]) -> None:
        risk = str(plan.get("risk", "info"))
        self.confirmation = model.Confirmation.of(plan)
        self.heading.setText(str(plan.get("title", "")))
        self.risk_label.setText(f"{messages.RISK_TITLES.get(risk, risk)}. {messages.RISK_TEXTS.get(risk, '')}")
        self.risk_label.setStyleSheet(f"color: {tone(RISK_TONES.get(risk, 'muted'))}; font-weight: 600;")
        self.summary.setText(soft_breaks(str(plan.get("summary", ""))))
        if self.confirmation.required:
            self.first_write.show_message("warning", messages.FIRST_WRITE_NOTICE)
        else:
            self.first_write.clear()
        lines = []
        for item in plan.get("preconditions", []):
            ok = item.get("ok", True)
            lines.append(f'<span style="color:{tone("ok" if ok else "error")}">{"✓" if ok else "×"}</span> {html.escape(str(item.get("text", "")))}')
        self.checks.setText("<br>".join(lines))
        self.checks.setVisible(bool(lines))
        advice = [f'<span style="color:{tone("warning")}">!</span> {html.escape(soft_breaks(str(w)))}' for w in plan.get("warnings", [])]
        advice += [f'<span style="color:{tone("muted")}">Note:</span> {html.escape(soft_breaks(str(n)))}' for n in plan.get("notes", [])]
        self.advice.setText("<br>".join(advice))
        self.advice.setVisible(bool(advice))
        steps = plan.get("steps", [])
        preparation: list[str] = []
        main: list[str] = []
        for number, step in enumerate(steps, start=1):
            text = html.escape(str(step.get("display", "")))
            condition = model.CONDITION_TEXTS.get(str(step.get("when", "")))
            tail = f' <span style="color:{tone("muted")}">[{condition}]</span>' if condition else ""
            (preparation if model.is_preparation(step) else main).append(f"{number}. {text}{tail}")
        self.commands_text.set_lines(main, html_text=True)
        self.commands_text.setVisible(bool(main))
        self.prep_text.set_lines(preparation, html_text=True)
        self.prep_note.setText("The system is switched from read-only to read-write and made ready to run its own programs." if any(step.get("group") == "enter-write" for step in steps)
                               else "These steps make the system ready for the repair.")
        self.prep.set_title(f"Preparation ({len(preparation)} step{'s' if len(preparation) != 1 else ''})")
        self.prep.setVisible(bool(preparation))
        self.commands.set_title(f"The commands that will run ({len(steps)})")
        self.diff_view.show_diff(str(plan.get("diff", "")))
        choices = (plan.get("params") or {}).get("choices") or {}
        self.diff_choices.setText(soft_breaks("\n".join(self._describe_choice(line, choice) for line, choice in sorted(choices.items(), key=lambda kv: int(kv[0])))))
        self.diff_choices.setVisible(bool(choices))
        self.diff.setVisible(bool(plan.get("diff")))
        cleanup = [str(step.get("display", "")) for step in plan.get("cleanup", [])]
        self.after_text.setText(soft_breaks("\n".join(cleanup)))
        self.after.setVisible(bool(cleanup))
        self._build_secrets(plan)
        self._build_confirm(plan)

    @staticmethod
    def _describe_choice(line: str, choice: dict[str, str]) -> str:
        action = choice.get("action", "")
        what = {"replace": f"point it at {choice.get('spec', 'the right disk')}", "nofail": "keep it, but do not wait for it at start-up", "comment": "turn it into a comment",
                "keep": "leave it as it is"}.get(action, action)
        return f"Line {line} of fstab: {what}"

    def _build_secrets(self, plan: dict[str, Any]) -> None:
        clear_layout(self.secrets_rows)
        self._secret_fields = {}
        names = [str(name) for name in plan.get("secrets", [])]
        for name in names:
            first = SecretField(f"New {name}")
            first.edit.setObjectName(f"planSecret:{name}")
            second = SecretField(f"New {name} again")
            second.edit.setObjectName(f"planSecretAgain:{name}")
            for field in (first, second):
                field.textChanged.connect(self._update_run)
                field.returnPressed.connect(self._enter_pressed)
            self.secrets_rows.addWidget(QLabel(f"New {name}"))
            self.secrets_rows.addWidget(first)
            self.secrets_rows.addWidget(QLabel(f"Type the {name} again"))
            self.secrets_rows.addWidget(second)
            self._secret_fields[name] = (first, second)
        self.secrets_card.setVisible(bool(names))

    def _build_confirm(self, plan: dict[str, Any]) -> None:
        confirmation = self.confirmation
        for widget in (self.reviewed, self.typed, self.phrase):
            widget.blockSignals(True)
        self.reviewed.setChecked(False)
        self.typed.clear()
        self.phrase.clear()
        for widget in (self.reviewed, self.typed, self.phrase):
            widget.blockSignals(False)
        self.reviewed.setVisible(confirmation.required and confirmation.review)
        self.typed_label.setText(f"To confirm that this system may be changed, type its disk name: {confirmation.typed}" if confirmation.typed else "")
        self.typed_label.setVisible(bool(confirmation.typed))
        self.typed.setVisible(bool(confirmation.typed))
        self.typed.setAccessibleName(f"Type {confirmation.typed}")
        self.phrase_label.setText(f"This can destroy data. Type {confirmation.phrase} to continue:" if confirmation.phrase else "")
        self.phrase_label.setVisible(bool(confirmation.phrase))
        self.phrase.setVisible(bool(confirmation.phrase))
        self.phrase.setAccessibleName(f"Type {confirmation.phrase}")
        self.confirm_card.setVisible(confirmation.required or bool(plan.get("secrets")))
        self.run_button.setStyleSheet(danger_style() if confirmation.risk in {"high", "dangerous"} else primary_style())

    def can_run(self) -> bool:
        if not self.plan or self.state != "review":
            return False
        if model.failed_preconditions(self.plan):
            return False
        secrets = {name: (first.text(), second.text()) for name, (first, second) in self._secret_fields.items()}
        if not model.secrets_ready(self.plan, secrets):
            return False
        return self.confirmation.satisfied(self.reviewed.isChecked(), self.typed.text(), self.phrase.text())

    def _update_run(self, *_args: object) -> None:
        self.run_button.setEnabled(self.can_run())
        failed = model.failed_preconditions(self.plan) if self.plan else []
        if failed and self.state == "review":
            self.notice.show_message("error", "This repair cannot run yet:\n" + "\n".join(f"• {text}" for text in failed))

    def _enter_pressed(self) -> None:
        if self.run_button.isEnabled():
            self.run_button.click()

    # ---- running ---------------------------------------------------------------------------------------------------------------------
    def _start_run(self) -> None:
        if not self.can_run() or self.system is None:
            return
        plan = self.plan
        request: dict[str, Any] = {"session_id": self.system.session_id, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True,
                                   "typed": self.typed.text(), "phrase": self.phrase.text()}
        if self._secret_fields:
            request["secrets"] = {name: first.text() for name, (first, _second) in self._secret_fields.items()}
        for first, second in self._secret_fields.values():
            first.clear()
            second.clear()
        self.tracker = model.RunTracker(plan)
        self._ran = True
        self._cancel_requested = False
        self._reconnect_tries = 0
        self._gave_up = False
        self.notice.clear()
        self.log.clear()
        self.steps_list.clear()
        for step in self.tracker.steps:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, step.id)
            self.steps_list.addItem(item)
            self._paint_step(step, item)
        self.bar.setValue(0)
        self.current_step.setText("Starting…")
        self.step_warning.clear()
        self._set_state("running")
        self._update_cancel()
        call = self.client.request("rescue.run", request)
        call.eventReceived.connect(self._run_event)
        call.then(self._run_finished, self._run_failed, self._run_lost)

    def _item_for(self, step_id: str) -> QListWidgetItem | None:
        for index in range(self.steps_list.count()):
            item = self.steps_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == step_id:
                return item
        return None

    def _paint_step(self, step: model.StepState, item: QListWidgetItem | None = None) -> None:
        item = item or self._item_for(step.id)
        if item is None:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, step.id)
            self.steps_list.addItem(item)
        suffix = f" (exit code {step.rc})" if step.status == "failed" and step.rc is not None else ""
        item.setText(f"{STEP_SYMBOLS.get(step.status, '○')}  {soft_breaks(step.text)}{suffix}")
        quiet = step.cleanup or step.group in model.PREPARATION_GROUPS
        colour = {"failed": tone("error"), "done": tone("ok"), "running": "", "skipped": tone("muted")}.get(step.status, tone("muted") if quiet or step.status == "pending" else "")
        item.setForeground(QBrush(QColor(colour)) if colour else QBrush())
        if step.status == "running":
            self.steps_list.scrollToItem(item)

    def _run_event(self, event: dict[str, Any]) -> None:
        tracker = self.tracker
        if tracker is None or not tracker.feed(event):
            return
        kind = event.get("type")
        data = model.event_data(event)
        if kind == "stage" and data.get("step"):
            step = next((item for item in tracker.steps if item.id == data["step"]), None)
            if step is not None:
                self._paint_step(step)
            self._show_progress()
        elif kind == "log" and event.get("message") is not None:
            self._pending_log.append(str(event["message"]))
            if not self._log_timer.isActive():
                self._log_timer.start(LOG_FLUSH_MS)
        elif kind == "stage" and event.get("stage") == "rollback":
            self._pending_log.append(f"— {event.get('message', '')}")

    def _flush_log(self) -> None:
        lines, self._pending_log = self._pending_log, []
        self.log.append_lines(lines)

    def _show_progress(self) -> None:
        tracker = self.tracker
        if tracker is None:
            return
        self.bar.setValue(tracker.percent)
        main = [step for step in tracker.steps if not step.cleanup]
        running = tracker.running
        if running is not None:
            position = next((index for index, step in enumerate(tracker.steps) if step is running), 0) + 1
            self.current_step.setText(soft_breaks(f"Step {position} of {len(tracker.steps)}: {running.text}"))
        else:
            finished = sum(1 for step in main if step.status in {"done", "failed", "skipped"})
            self.current_step.setText(f"{finished} of {len(main)} steps done")
        self.step_warning.setText("This step cannot be stopped safely. Please wait until it has finished." if tracker.non_atomic_running else "")
        self._update_cancel()

    def _update_cancel(self) -> None:
        tracker = self.tracker
        if self.state != "running" or tracker is None:
            return
        blocked = tracker.non_atomic_running
        self.cancel_button.setEnabled(not blocked and not self._cancel_requested)
        self.cancel_button.setToolTip("A step is running that cannot be stopped safely. Wait until it has finished, or use “Stop now”." if blocked else "Stop after the step that is running")
        self.stop_button.setVisible(blocked)
        if not blocked:
            self.stop_confirm.hide()

    # ---- the end of a run ---------------------------------------------------------------------------------------------------------------
    def _run_finished(self, _result: dict[str, Any]) -> None:
        self._show_result()

    def _run_failed(self, code: str, text: str, _data: dict[str, Any]) -> None:
        tracker = self.tracker
        if tracker is not None and tracker.result is not None:
            self._show_result()
            return
        started = tracker is not None and any(step.status != "pending" for step in tracker.steps)
        self._flush_log()
        if started:  # steps ran but no result arrived: say so plainly instead of guessing
            self._show_result(error=text)
            return
        # Refused before anything ran (a stale plan, a missing condition, no network): the plan was used up, so make a new one.
        self._ran = False
        self.state = "idle"
        self._request_plan(notice=text)

    def _run_lost(self, text: str) -> None:
        tracker = self.tracker
        if tracker is None or self.state != "running":
            return
        self.notice.show_message("warning", messages.RECONNECTING_TEXT)
        if not tracker.job_id:
            self._find_job()
            return
        self._schedule_reattach()

    def _find_job(self) -> None:
        """Nothing arrived before the connection broke: ask the session what it is doing, instead of guessing which operation was ours."""
        if self.system is None:
            return
        call = self.client.request("rescue.session", {"session_id": self.system.session_id})
        call.then(self._job_found, lambda *_a: self._schedule_reattach(), lambda *_a: self._schedule_reattach())

    def _job_found(self, info: dict[str, Any]) -> None:
        busy = info.get("busy") or {}
        if busy.get("op") and self.tracker is not None:
            self.tracker.job_id = str(busy["op"])
            self._schedule_reattach()
        else:
            self._show_result(unknown=True)

    def _schedule_reattach(self) -> None:
        QTimer.singleShot(self.RECONNECT_MS, self._reattach)

    def _reconnect_now(self) -> None:
        self._reconnect_tries = 0
        self._gave_up = False
        self.reconnect_button.setVisible(False)
        self.notice.show_message("warning", messages.RECONNECTING_TEXT)
        self._reattach()

    def _reattach(self) -> None:
        tracker = self.tracker
        if tracker is None or self.state != "running" or self.system is None:
            return
        params: dict[str, Any] = {"after_seq": tracker.last_seq, "session_id": self.system.session_id}
        if tracker.job_id:
            params["job_id"] = tracker.job_id
        call = self.client.request("rescue.events", params)
        call.eventReceived.connect(self._run_event)
        call.then(self._reattached, self._reattach_failed, self._reattach_lost)

    def _reattached(self, _result: dict[str, Any]) -> None:
        self.notice.clear()
        self._show_result()

    def _reattach_failed(self, code: str, text: str, _data: dict[str, Any]) -> None:
        if code == "rescue.not_found":  # the daemon does not know the operation any more (it was restarted, or it is too old)
            self._show_result(unknown=True)
            return
        self._reattach_lost(text)

    def _reattach_lost(self, _text: str) -> None:
        self._reconnect_tries += 1
        if self._reconnect_tries >= self.RECONNECT_TRIES:
            self._gave_up = True
            self.notice.show_message("error", messages.RECONNECT_FAILED_TEXT)
            self.reconnect_button.setVisible(True)
            self.stop_button.setVisible(False)
            self.cancel_button.setEnabled(True)
            return
        self._schedule_reattach()

    def _show_result(self, *, unknown: bool = False, error: str = "") -> None:
        tracker = self.tracker
        if tracker is None:
            return
        self._flush_log()
        self.bar.setValue(100 if tracker.result else tracker.percent)
        self.notice.clear()
        result = tracker.result
        if result is not None and result.get("plan_id") not in {None, self.plan.get("plan_id")}:
            result, unknown = None, True
        outcome = str(result.get("outcome", "")) if result else ""
        if result is None:
            self.result_title.setText("The result is not known" if unknown else "The repair did not finish")
            lines = ["The connection to the installer service was lost before the repair reported its result. Use “Back to the diagnosis” and check the system again before you repair anything else."
                     if unknown else (html.escape(error) or "The repair stopped without a result.")]
        else:
            self.result_title.setText(messages.OUTCOME_TITLES.get(outcome, outcome))
            lines = self._result_lines(result, tracker)
        status = "error" if result is None else messages.OUTCOME_STATUS.get(outcome, "info")
        self.result_title.setStyleSheet(f"color: {tone(status)};")
        self.result_text.setText("<br>".join(lines))
        self._set_state("result")
        self.done_button.setFocus()

    def _result_lines(self, result: dict[str, Any], tracker: model.RunTracker) -> list[str]:
        lines = []
        for step in tracker.failed_steps():
            detail = f" (exit code {step.rc})" if step.rc is not None else ""
            lines.append(f'<span style="color:{tone("error")}">×</span> This step failed{detail}: {html.escape(soft_breaks(step.text))}' + (f"<br>&nbsp;&nbsp;{html.escape(step.error)}" if step.error else ""))
        for check in result.get("post_checks", []):
            ok = check.get("ok")
            lines.append(f'<span style="color:{tone("ok" if ok else "error")}">{"✓" if ok else "×"}</span> {html.escape(soft_breaks(str(check.get("text", ""))))}')
        for warning in result.get("warnings", []):
            lines.append(f'<span style="color:{tone("warning")}">!</span> {html.escape(soft_breaks(str(warning)))}')
        for note in tracker.notes:
            lines.append(html.escape(note))
        for hint in result.get("next_hints", []):
            lines.append(f"Next: {html.escape(soft_breaks(str(hint)))}")
        mode = (result.get("session") or {}).get("mode")
        if mode == "rw":
            lines.append("The system is now open with permission to write. Close it with “Finish” when you are done.")
        return lines

    # ---- cancelling ---------------------------------------------------------------------------------------------------------------------
    def _cancel_clicked(self) -> None:
        if self.state != "running":
            self.leaveRequested.emit(self._ran)
            return
        self.request_cancel()

    def request_cancel(self) -> bool:
        """Stop after the step that is running. Refused while a step runs that cannot be stopped safely."""
        tracker = self.tracker
        if self.system is None or self.state != "running" or tracker is None or tracker.non_atomic_running or self._cancel_requested:
            return False
        self._cancel_requested = True
        self.cancel_button.setEnabled(False)
        self.notice.show_message("info", "The repair stops after the step that is running. The tool then tidies up.")
        call = self.client.request("rescue.cancel", {"session_id": self.system.session_id})
        call.then(None, lambda *_a: None, lambda *_a: None)
        self._calls.append(call)
        return True

    def _ask_stop(self) -> None:
        self.stop_confirm.ask("Stop the command now?", messages.STOP_NOW_WARNING, "&Stop now", danger=True, cancel_text="Keep waiting")

    def _stop_confirmed(self, _typed: str, _phrase: str, _tick: bool) -> None:
        if self.system is None:
            return
        self.notice.show_message("warning", "Stopping the command…")
        call = self.client.request("rescue.cancel", {"session_id": self.system.session_id, "force": True})
        call.then(None, lambda *_a: None, lambda *_a: None)
        self._calls.append(call)

    def focus_first(self) -> None:
        if self.state == "options" and self._form is not None:
            first = next((w for w in self._form.findChildren(QWidget) if isinstance(w, (QLineEdit, QCheckBox)) and w.isEnabled()), None)
            (first or self.continue_button).setFocus()
        elif self.state == "review":
            self.run_button.setFocus()
        else:
            self.cancel_button.setFocus()

