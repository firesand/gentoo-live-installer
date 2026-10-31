from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QFontDatabase
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from .. import daemon_client, diagnostics, first_steps, theme
from ..backend import BackendWorker
from ..manifest import InstallManifest
from ..widgets import Card, FlowLayout, Page, wrapped_label
from .help import open_url

LOG = logging.getLogger("gentoo_installer.progress")

# The visible log keeps the newest lines only; the daemon keeps the whole log.
LOG_MAX_BLOCKS = 5000
LOG_FLUSH_MS = 100
SUMMARY_CHARS = 220
SUMMARY_SUFFIX = " (full output in the log below)"
TRUNCATED_NOTE = "(older lines are kept in the installer log; use Save log...)"

STAGE_ORDER = (
    "preflight", "storage", "stage3", "mounts", "repository", "portage", "kernel", "target",
    "base", "bootloader", "desktop", "validation", "cleanup",
)
STAGE_LABELS = {
    "preflight": "Checking the installation environment",
    "storage": "Preparing disks and filesystems",
    "stage3": "Downloading Gentoo and checking that the download is genuine",
    "mounts": "Preparing the new system",
    "repository": "Fetching the Portage tree",
    "portage": "Configuring Portage",
    "kernel": "Installing firmware and the kernel",
    "target": "Configuring accounts and network",
    "base": "Installing the base system",
    "bootloader": "Installing the bootloader",
    "desktop": "Installing the desktop and applications",
    "validation": "Checking the installed system",
    "cleanup": "Finishing and unmounting",
    "packages": "Installing applications",  # daemons that still have one packages stage
}
# The checklist shows the Handbook names of the 12 stages (cleanup is part of the last one).
CHECKLIST_STAGES = tuple(name for name in STAGE_ORDER if name != "cleanup")
CHECKLIST_NAMES = {
    "preflight": "Checking the installation environment",
    "storage": "Preparing the disks",
    "stage3": "Unpacking the stage file",
    "mounts": "Mounting the necessary filesystems",
    "repository": "Setting up binary package signatures and the Portage tree",
    "portage": "Configuring Portage",
    "kernel": "Installing the kernel",
    "target": "Setting up accounts and the network",
    "base": "Installing the base system",
    "bootloader": "Installing the bootloader",
    "desktop": "Installing the desktop and applications",
    "validation": "Checking the installed system",
}
LONG_STEPS_HINT = "The long steps are the kernel and the desktop; the others take a few minutes."
CANCEL_WARNING = "Cancelling after the disk was changed leaves a partly installed system that cannot start; you can resume or start over later."
CANCEL_WARNING_BOOTABLE = "The new system can already start. Cancelling stops the steps that are left; you can retry them later."
GENTOO_NEWS_URL = "https://www.gentoo.org/support/news-items/"
ERASE_AGAIN = "Erase again and restart installation"
# Share of the overall bar per stage (sum 100, cleanup 0). Used when the
# daemon does not send an overall percentage with its events.
STAGE_WEIGHTS = {
    "preflight": 2, "storage": 8, "stage3": 12, "mounts": 1, "repository": 5, "portage": 2,
    "kernel": 20, "target": 2, "base": 28, "bootloader": 6, "desktop": 12, "validation": 2, "cleanup": 0,
}

MERGE_LISTING = re.compile(r"^(?:>>>|---|<<<) /")
MERGED_FILE = re.compile(r"^>>> /")
EMERGING = re.compile(r"^>>> (?:Emerging(?: binary)?|Installing) \((\d+) of (\d+)\)\s*(\S*)")


def format_elapsed(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def soft_breaks(text: str, chunk: int = 40) -> str:
    """Allow a line break inside very long unbroken tokens (paths, hashes) so a
    wrapped label never forces the page wider than the window."""
    return re.sub(rf"(\S{{{chunk}}})(?=\S)", "\\1\u200b", text)


def summarize(message: str) -> str:
    """One readable line for the status card; the log pane keeps the full text."""
    lines = message.strip().splitlines()
    first = lines[0] if lines else ""
    truncated = len(lines) > 1
    if len(first) > SUMMARY_CHARS:
        first = first[: SUMMARY_CHARS - 1].rstrip() + "…"
        truncated = True
    return soft_breaks(first) + (SUMMARY_SUFFIX if truncated else "")


class ProgressModel:
    """Overall progress that never moves backwards.

    Daemons that send `data.stage_progress` put the overall percentage in
    every event's `progress`. Older ones send an overall percentage with
    `stage` events and a stage-local one with download `progress` events, so
    without `stage_progress` the model maps a `progress` event into the slice
    of its stage (weights below) and lets only `stage` events speak for the
    whole bar."""

    def __init__(self) -> None:
        self.overall = 0.0
        self.local = 0.0  # 0..100 inside the current stage

    def reset(self) -> None:
        self.overall = 0.0
        self.local = 0.0

    @staticmethod
    def base_of(stage: str) -> float | None:
        if stage not in STAGE_ORDER:
            return None
        return float(sum(STAGE_WEIGHTS[name] for name in STAGE_ORDER[: STAGE_ORDER.index(stage)]))

    def _raise(self, value: float) -> None:
        self.overall = max(self.overall, min(100.0, value))

    def stage_event(self, stage: str, status: str, progress: Any, stage_progress: Any) -> None:
        if isinstance(stage_progress, (int, float)):
            self.local = float(stage_progress)
        else:
            self.local = 100.0 if status in {"completed", "skipped"} else 0.0
        if isinstance(progress, (int, float)):
            self._raise(float(progress))
            return
        base = self.base_of(stage)
        if base is not None:
            self._raise(base + (STAGE_WEIGHTS[stage] if status in {"completed", "skipped"} else 0))

    def progress_event(self, stage: str, progress: Any, stage_progress: Any) -> None:
        if isinstance(stage_progress, (int, float)):
            self.local = float(stage_progress)
            if isinstance(progress, (int, float)):
                self._raise(float(progress))
                return
        elif isinstance(progress, (int, float)):
            self.local = max(0.0, min(100.0, float(progress)))
        else:
            return
        base = self.base_of(stage)
        if base is not None:
            self._raise(base + STAGE_WEIGHTS[stage] * self.local / 100)

    def package_event(self, stage: str, number: int, total: int) -> None:
        if total <= 0:
            return
        self.local = max(0.0, min(100.0, (number - 1) * 100 / total))
        base = self.base_of(stage)
        if base is not None:
            self._raise(base + STAGE_WEIGHTS[stage] * self.local / 100)

    def finish(self) -> None:
        self.local = 100.0
        self.overall = 100.0


class _FileJob(QThread):
    """Writes a file off the GUI thread and reports (ok, message)."""

    done = Signal(bool, str)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.path = path

    def work(self) -> str:
        raise NotImplementedError

    def run(self) -> None:
        # Nothing may leave run(): an exception that does freezes PySide6 on Python 3.14 (the window stops answering).
        try:
            self.done.emit(True, self.work())
        except OSError as exc:
            self.done.emit(False, f"Could not write {self.path}: {exc.strerror or exc}")
        except Exception as exc:  # noqa: BLE001 - whatever went wrong is reported, never raised out of the thread
            LOG.exception("Writing %s failed", self.path)
            self.done.emit(False, f"Could not write {self.path}: {type(exc).__name__}: {exc}")


class LogExporter(_FileJob):
    """Streams the daemon's redacted install log to a file; falls back to the
    lines shown in the window when the daemon cannot provide it."""

    def __init__(self, socket_path: str, path: str, visible_text: str, parent=None):
        super().__init__(path, parent)
        self.socket_path = socket_path
        self.visible_text = visible_text

    def work(self) -> str:
        count = 0
        reason = ""
        with open(self.path, "w", encoding="utf-8") as handle:
            os.chmod(self.path, 0o600)
            lines = daemon_client.fetch_log(self.socket_path)
            while True:
                try:
                    line = next(lines)
                except StopIteration:
                    break
                except daemon_client.DaemonError as exc:
                    reason = str(exc)
                    break
                except OSError as exc:
                    reason = daemon_client.describe_os_error(exc, self.socket_path)[1]
                    break
                handle.write(line + "\n")
                count += 1
            if count:
                return f"Saved the full installation log ({count:,} lines) to {self.path}"
            handle.write(self.visible_text.rstrip("\n") + "\n")
        reason = reason or "the installer service has no log yet"
        return f"Saved the lines shown in this window to {self.path} ({reason}). The full log stays on the live system."


class DiagnosticsExporter(_FileJob):
    def __init__(self, path: str, collect: Callable[[], dict], parent=None):
        super().__init__(path, parent)
        self.collect = collect

    def work(self) -> str:
        diagnostics.write_bundle(self.path, self.collect())
        return f"Saved diagnostics to {self.path}. Review it before sharing; serial numbers and other identifiers are shortened or removed, and passwords are removed."


class PanelCommand(QObject):
    """A command of the result panel. It reads like a button for the rest of the code (text(), setText(), isVisible(), setVisible(),
    click(), clicked) and is shown as the one primary button, as a quiet button next to it, or as an entry of the "More" menu:
    the panel decides (ProgressPage._arrange_commands), the command only says whether it is available."""

    clicked = Signal()

    def __init__(self, text: str, *, quiet: bool = False, danger: bool = False, parent: QObject | None = None):
        super().__init__(parent)
        self._text = text
        self.quiet = quiet  # a command that stays a visible quiet button (Save log) instead of going into the menu
        self.danger = danger  # never the primary button; its menu entry is drawn as a danger button
        self.available = False
        self.presentation = "hidden"  # primary | secondary | menu | hidden
        self.changed: Callable[[], None] = lambda: None
        self.button = QPushButton(text)
        self.button.hide()
        self.button.clicked.connect(self.clicked)
        self.entry_button: QPushButton | None = None
        self.entry: QAction | QWidgetAction | None = None

    def attach_menu(self, menu: QMenu) -> None:
        if self.danger:
            widget_action = QWidgetAction(menu)
            self.entry_button = QPushButton(self._text)
            self.entry_button.setObjectName("dangerButton")
            self.entry_button.clicked.connect(menu.close)
            self.entry_button.clicked.connect(self.clicked)
            widget_action.setDefaultWidget(self.entry_button)
            self.entry = widget_action
            menu.addAction(widget_action)
        else:
            self.entry = menu.addAction(self._text)
            self.entry.triggered.connect(lambda _=False: self.clicked.emit())
        self.entry.setVisible(False)

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        self._text = text
        self.button.setText(text)
        if self.entry_button is not None:
            self.entry_button.setText(text)
        elif self.entry is not None:
            self.entry.setText(text)

    def isVisible(self) -> bool:
        return self.available

    def isVisibleTo(self, _parent: QWidget | None = None) -> bool:
        return self.available

    def setVisible(self, visible: bool) -> None:
        if visible != self.available:
            self.available = visible
            self.changed()

    def click(self) -> None:
        self.clicked.emit()

    def present(self, how: str) -> None:
        """Show this command as "primary", "secondary" or "menu" ("hidden" when it is not available)."""
        self.presentation = how if self.available else "hidden"
        shown = self.presentation in {"primary", "secondary"}
        self.button.setObjectName("primaryButton" if self.presentation == "primary" else "")
        self.button.style().unpolish(self.button)
        self.button.style().polish(self.button)
        self.button.setVisible(shown)
        if self.entry is not None:
            self.entry.setVisible(self.presentation == "menu")


class StageChecklist(QFrame):
    """The stages of the installation as a vertical checklist: done, running, waiting, or the one that failed."""

    SYMBOLS = {"done": "✓", "running": "▶", "pending": "○", "failed": "✗"}

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(4)
        title = QLabel("What the installer does")
        title.setObjectName("cardTitle")
        outer.addWidget(title)
        self.hint = wrapped_label(LONG_STEPS_HINT, muted=True)
        outer.addWidget(self.hint)
        self.states: dict[str, str] = {}
        self.icons: dict[str, QLabel] = {}
        self.names: dict[str, QLabel] = {}
        for stage in CHECKLIST_STAGES:
            row = QHBoxLayout()
            icon = QLabel()
            icon.setMinimumWidth(18)
            icon.setAccessibleName("State")
            name = QLabel(CHECKLIST_NAMES[stage])
            row.addWidget(icon)
            row.addWidget(name, 1)
            outer.addLayout(row)
            self.icons[stage], self.names[stage] = icon, name
        self.reset()

    def reset(self) -> None:
        for stage in CHECKLIST_STAGES:
            self.set_state(stage, "pending")

    def set_state(self, stage: str, state: str) -> None:
        if stage not in self.icons:
            return
        self.states[stage] = state
        icon = self.icons[stage]
        icon.setText(self.SYMBOLS[state])
        colour = {"done": theme.status_color("ok"), "running": theme.ACCENT, "failed": theme.status_color("error")}.get(state, theme.MUTED)
        icon.setStyleSheet(f"color: {colour}; font-weight: 700;")
        name = self.names[stage]
        font = name.font()
        font.setBold(state in {"running", "failed"})
        name.setFont(font)
        name.setAccessibleDescription({"done": "done", "running": "in progress", "pending": "waiting", "failed": "did not finish"}[state])

    def stage_event(self, stage: str, status: str) -> None:
        """A stage began (running), finished (completed/skipped); every stage before it is done."""
        if stage not in CHECKLIST_STAGES:
            return
        position = CHECKLIST_STAGES.index(stage)
        for earlier in CHECKLIST_STAGES[:position]:
            if self.states.get(earlier) != "done":
                self.set_state(earlier, "done")
        self.set_state(stage, "done" if status in {"completed", "skipped"} else "running")

    def fail(self, stage: str) -> None:
        if stage in self.icons and self.states.get(stage) != "done":
            self.set_state(stage, "failed")
        else:  # the failing stage is unknown: mark the one that was running
            for name, state in self.states.items():
                if state == "running":
                    self.set_state(name, "failed")

    def finish(self) -> None:
        for stage in CHECKLIST_STAGES:
            self.set_state(stage, "done")


def linkify(text: str) -> str:
    """HTML for a plain text with web addresses: escaped, newlines kept, addresses clickable."""
    import html

    escaped = html.escape(text)
    return re.sub(r"(https?://[^\s<)]+)", r'<a href="\1">\1</a>', escaped).replace("\n", "<br>")


class ProgressPage(Page):
    cancelRequested = Signal()
    finished = Signal(bool)
    workerStopped = Signal()
    rebootRequested = Signal()
    started = Signal()  # a run (start, resume or attach) began: navigation must stay locked
    retryRequested = Signal()  # "Try again"/"Erase again and restart installation": a fresh start through the usual confirmations
    repairRequested = Signal()  # open the repair tool (the system on the disk does not start)
    settingsRequested = Signal()
    quitRequested = Signal()
    diagnosticsRequested = Signal()
    logSaved = Signal(bool, str)

    def __init__(self):
        super().__init__("Installing Gentoo", "You can follow the detailed log while the installer works. Keep the live session running until installation is complete.")
        self.worker: BackendWorker | None = None
        self.seconds = 0
        self.last_seq = 0
        self.model = ProgressModel()
        self._manifest: InstallManifest | None = None
        self._socket_path = ""
        self._stage = ""
        self._storage_started = False
        self._storage_done = False
        self._retryable: bool | None = None
        self._resumable: bool | None = None
        self._bootable = False  # the bootloader step finished: the new system starts even though a later step failed
        self._bootloader_done = False  # in this run: a resumed one has not got there yet, and until then the service says not bootable
        self._degraded: list[str] = []  # optional parts that could not be installed
        self._warnings: list[str] = []  # things that went wrong after the install itself succeeded (cleanup)
        self._restart_offered = False
        self._finish: dict[str, Any] = {}  # the success data of the completed event: boot check, news, login, hardware first steps
        self._cancelled = False
        self._busy = False
        self._blocked = False  # a repair session holds the disks: the start was refused, no installation is running
        self._terminal = False
        self._failure_reported = False
        self._finished_emitted = False
        self._merged_files = 0
        self._detail_dirty = False
        self._base_detail = ""
        self._pending: list[str] = []
        self._lines_seen = 0
        self._truncated = False
        self._last_error_line = ""
        self._failure_message = ""
        self._failure_stage = ""
        self._exporters: list[QThread] = []
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(LOG_FLUSH_MS)
        self._flush_timer.timeout.connect(self._flush_tick)

        status_card = Card()
        self.stage = QLabel("Preparing to start…")
        self.stage.setObjectName("cardTitle")
        self.stage.setWordWrap(True)
        self.stage.setProperty("severity", "")
        status_card.content.addWidget(self.stage)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        status_card.content.addWidget(self.progress)
        row = QHBoxLayout()
        self.detail = wrapped_label("Connecting to the installer backend", muted=True, selectable=True)
        self.detail.setMinimumWidth(0)
        policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.detail.setSizePolicy(policy)  # the label follows the window, never the other way round
        row.addWidget(self.detail, 1)
        self.elapsed = QLabel(format_elapsed(0))
        self.elapsed.setAccessibleName("Elapsed time")
        row.addWidget(self.elapsed, 0, Qt.AlignmentFlag.AlignTop)
        status_card.content.addLayout(row)
        self.secondary = wrapped_label("", muted=True)
        self.secondary.hide()
        status_card.content.addWidget(self.secondary)
        self.content.addWidget(status_card)

        # The result panel sits right under the status: Restart now must never be below the fold.
        self.result_panel = QFrame()
        self.result_panel.setObjectName("card")
        panel = QVBoxLayout(self.result_panel)
        panel.setContentsMargins(18, 14, 18, 14)
        self.result_hint = wrapped_label("", selectable=True)
        panel.addWidget(self.result_hint)
        self.outcome_details = wrapped_label("", selectable=True)
        self.outcome_details.setAccessibleName("What did not work")
        self.outcome_details.hide()
        panel.addWidget(self.outcome_details)
        buttons = FlowLayout()
        self.more_menu = QMenu(self)
        self.reconnect_button = PanelCommand("Reconnect", parent=self)
        self.resume_button = PanelCommand("Resume installation", parent=self)
        self.retry_button = PanelCommand("Try again", danger=True, parent=self)  # the menu entry is built here; only Erase again ever reaches it
        self.settings_button = PanelCommand("Back to settings", parent=self)
        self.save_log_button = PanelCommand("Save log…", quiet=True, parent=self)
        self.repair_button = PanelCommand("Open the repair tool", parent=self)
        self.diagnostics_button = PanelCommand("Save diagnostics…", parent=self)
        self.quit_button = PanelCommand("Quit", parent=self)
        self.reboot = QPushButton("Restart now")
        self.reboot.setObjectName("primaryButton")
        self.reboot.setVisible(False)
        self.reboot.clicked.connect(self.rebootRequested)
        # Priority when no restart is offered: the first available command is the one primary button.
        self.commands = [self.reconnect_button, self.resume_button, self.retry_button, self.settings_button, self.save_log_button,
                         self.repair_button, self.diagnostics_button, self.quit_button]
        buttons.addWidget(self.reboot)
        for command in self.commands:
            buttons.addWidget(command.button)
            command.attach_menu(self.more_menu)
            command.changed = self._arrange_commands
        self.more_button = QToolButton()
        self.more_button.setText("More")
        self.more_button.setMenu(self.more_menu)
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_button.setAccessibleName("More actions")
        self.more_button.hide()
        buttons.addWidget(self.more_button)
        panel.addLayout(buttons)
        self.notice = wrapped_label("", muted=True, selectable=True)
        self.notice.hide()
        panel.addWidget(self.notice)
        self.result_panel.hide()
        self.content.addWidget(self.result_panel)

        # CORE-08: what to do next, after a successful installation.
        self.first_steps_card = Card("Your first steps in Gentoo")
        self.first_steps_checks = wrapped_label("", selectable=True)
        self.first_steps_checks.setAccessibleName("Checks of the new system")
        self.first_steps_card.content.addWidget(self.first_steps_checks)
        self.first_steps_text = wrapped_label("", selectable=True)
        self.first_steps_text.setTextFormat(Qt.TextFormat.RichText)
        self.first_steps_text.setOpenExternalLinks(True)
        self.first_steps_text.setAccessibleName("First steps")
        self.first_steps_card.content.addWidget(self.first_steps_text)
        self.first_steps_hardware = wrapped_label("", selectable=True)
        self.first_steps_hardware.setAccessibleName("First steps for this computer")
        self.first_steps_card.content.addWidget(self.first_steps_hardware)
        self.news_button = QPushButton("Read Gentoo news")
        self.news_button.setToolTip("Open the Gentoo news page in the browser. In the new system, `eselect news read` shows the news that applies to it.")
        self.news_button.clicked.connect(lambda: open_url(GENTOO_NEWS_URL))
        self.news_button.hide()
        self.first_steps_card.content.addWidget(self.news_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.first_steps_note = wrapped_label("", muted=True, selectable=True)
        self.first_steps_card.content.addWidget(self.first_steps_note)
        self.first_steps_card.hide()
        self.content.addWidget(self.first_steps_card)

        self.checklist = StageChecklist()
        self.content.addWidget(self.checklist)

        self.details_button = QToolButton()
        self.details_button.setText("Details")
        self.details_button.setCheckable(True)
        self.details_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_button.setArrowType(Qt.ArrowType.RightArrow)
        self.details_button.setAutoRaise(True)
        self.details_button.setAccessibleName("Details: the installation log")
        self.details_button.toggled.connect(self._details_toggled)
        self.content.addWidget(self.details_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.truncated_label = wrapped_label("Showing the newest lines only. " + TRUNCATED_NOTE, muted=True)
        self.truncated_label.hide()
        self.content.addWidget(self.truncated_label)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setUndoRedoEnabled(False)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log.setMaximumBlockCount(LOG_MAX_BLOCKS)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log.setMinimumHeight(280)
        self.log.setAccessibleName("Installation log")
        self.log.hide()
        self.content.addWidget(self.log)
        actions = QHBoxLayout()
        self.cancel = QPushButton("Cancel installation")
        self.cancel.setObjectName("linkButton")
        self.cancel.setToolTip(CANCEL_WARNING)
        self.cancel.clicked.connect(self._cancel)
        actions.addWidget(self.cancel)
        self.cancel_note = wrapped_label(CANCEL_WARNING, muted=True)
        actions.addWidget(self.cancel_note, 1)
        self.content.addLayout(actions)

        self.retry_button.clicked.connect(self.retryRequested)
        self.resume_button.clicked.connect(self.resume)
        self.reconnect_button.clicked.connect(self.reconnect)
        self.settings_button.clicked.connect(self.settingsRequested)
        self.repair_button.clicked.connect(self.repairRequested)
        self.save_log_button.clicked.connect(self.save_log)
        self.diagnostics_button.clicked.connect(self.diagnosticsRequested)
        self.quit_button.clicked.connect(self.quitRequested)

    def _details_toggled(self, shown: bool) -> None:
        self.log.setVisible(shown)
        self.truncated_label.setVisible(shown and self._truncated)
        self.details_button.setArrowType(Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)

    def _arrange_commands(self) -> None:
        """Exactly one primary button; Save log stays a quiet button; everything else is in the More menu."""
        available = [command for command in self.commands if command.available]
        primary: PanelCommand | None = None
        secondary: PanelCommand | None = None
        candidates = [command for command in available if not command.danger and not command.quiet]
        waiting = self._failure_reported and not self._terminal  # the page asks for patience: Save diagnostics is not the next step
        if self._restart_offered:
            secondary = next((command for command in candidates if command in (self.reconnect_button, self.resume_button, self.retry_button)), None)
        else:
            primary = next((command for command in candidates if command is self.reconnect_button or not waiting), None)
        for command in self.commands:
            quiet_button = command is secondary or (command.quiet and command.available)
            command.present("primary" if command is primary else "secondary" if quiet_button else "menu")
        self.more_button.setVisible(any(command.presentation == "menu" for command in self.commands))

    def set_restart_offered(self, offered: bool) -> None:
        """Restart now is the primary button while the new system can start."""
        self._restart_offered = offered
        self.reboot.setVisible(offered)
        self._arrange_commands()

    # --- runs -------------------------------------------------------------------------------

    def start(self, manifest: InstallManifest, socket_path: str, *, resume: bool = False) -> None:
        self._manifest = manifest
        self._socket_path = socket_path
        self._begin_run(keep_log=resume)
        self.stage.setText("Resuming installation…" if resume else "Starting installation…")
        if resume:
            self.model.reset()
            self._append_log("--- resuming ---")
            self.worker = BackendWorker(manifest, socket_path, self, resume=True)
        else:
            self.worker = BackendWorker(manifest, socket_path, self)
        self._launch()

    def attach(self, socket_path: str, after_seq: int = 0) -> None:
        """Follow a job that is already running (or show how the last one ended)."""
        self._socket_path = socket_path
        self._begin_run(keep_log=after_seq > 0, keep_seq=after_seq > 0)
        self.stage.setText("Reconnecting to the installation…")
        self.detail.setText("Replaying what happened so far")
        self.worker = BackendWorker(None, socket_path, self, attach=True, after_seq=after_seq)
        self._launch()

    def reconnect(self) -> None:
        if self._socket_path:
            self.attach(self._socket_path, 0 if self._busy or self._failure_reported else self.last_seq)  # 0: the replay brings the error back

    def resume(self) -> None:
        """Resume with the manifest that started the job (its fingerprint must match)."""
        if self._manifest is not None:
            self.start(self._manifest, self._socket_path, resume=True)

    def _begin_run(self, *, keep_log: bool, keep_seq: bool = False) -> None:
        if not keep_seq:
            self.last_seq = 0  # sequence numbers belong to one job; a resumed run is a new job
        if not keep_log:
            self.log.clear()
            self._pending.clear()
            self._lines_seen = 0
            self._truncated = False
            self.truncated_label.hide()
            self.model.reset()
            self._storage_started = False
            self._storage_done = False
        self.seconds = 0
        self._stage = ""
        self._terminal = False
        self._failure_reported = False
        self._finished_emitted = False
        self._cancelled = False
        self._busy = False
        self._blocked = False
        self._retryable = None
        self._resumable = None
        if not keep_log:
            self._bootable = False  # a resumed run starts from a system that already boots: only the service can say it does not
        self._degraded = []
        self._warnings = []
        self._refresh_outcome_details()
        self._merged_files = 0
        self._last_error_line = ""
        self.elapsed.setText(format_elapsed(0))
        self.progress.setRange(0, 0)
        self.detail.setText("Connecting to the installer backend")
        self._base_detail = ""
        self.secondary.hide()
        self.secondary.setProperty("package", False)
        self.cancel.setEnabled(True)
        self.cancel.setVisible(True)
        self.cancel_note.setVisible(True)
        self._bootloader_done = False
        self.cancel_note.setText(CANCEL_WARNING)
        self.cancel.setToolTip(CANCEL_WARNING)
        self._finish = {}
        self.checklist.reset()
        self.first_steps_card.hide()
        self._set_severity("")
        for command in self.commands:
            command.available = False
        self.set_restart_offered(False)
        self.result_panel.hide()
        self.notice.hide()

    def _launch(self) -> None:
        assert self.worker is not None
        self.worker.eventReceived.connect(self.handle_event)
        self.worker.connectionFailed.connect(self._connection_failed)
        self.worker.cancelAcknowledged.connect(self._cancel_acknowledged)
        self.worker.finished.connect(self._worker_finished)
        self.started.emit()
        self.worker.start()
        self.timer.start()

    # --- events -----------------------------------------------------------------------------

    def _retired(self) -> bool:
        """True in a slot called by a worker that a later run replaced: Qt still delivers what it queued, and that is about the old job."""
        sender = self.sender()
        return sender is not None and sender is not self.worker

    def handle_event(self, event: dict) -> None:
        if self._retired():
            return
        kind = event.get("event") or event.get("type") or event.get("ev")
        stage = event.get("stage", "") or ""
        raw_data = event.get("data")
        data: dict = raw_data if isinstance(raw_data, dict) else {}
        message = event.get("message") or event.get("text") or event.get("line") or ""
        seq = event.get("seq")
        if isinstance(seq, int):
            self.last_seq = max(self.last_seq, seq)
        if kind == "log":
            if message:
                self._log_event(str(message))
            return
        if kind in {"stage", "progress", "error", "failed", "completed"}:
            self.flush_log()  # keep the pane in event order
        # Cleanup continues after failure, but its progress must not replace the
        # terminal failure status shown to the user.
        if self._failure_reported and kind in {"stage", "progress"}:
            return
        if kind in {"stage", "progress"}:
            self._stage_event(kind, stage, str(message), data, event)
        elif kind == "status":
            if data.get("warning") is True and message:
                self._add_warning(str(message))
            elif data.get("status") == "running" and not self._stage:
                self.detail.setText(str(message) or "Connected to the installer")
        elif kind in {"error", "failed"}:
            retryable, resumable = data.get("retryable"), data.get("resumable")
            self._absorb_outcome(data)
            refused = data.get("code") == "install.busy" and data.get("blocked") is True  # install.busy for a repair session, not a job
            self._fail(
                str(message or event.get("error") or "The installation failed. Review the log for details."),
                stage=stage,
                cancelled=event.get("error") == "cancelled" or data.get("code") == "install.cancelled",
                retryable=retryable if isinstance(retryable, bool) else None,
                busy=data.get("code") == "install.busy" and not refused,
                blocked=refused,
                resumable=resumable if isinstance(resumable, bool) else None,
            )
        elif kind == "completed":
            self._completed(str(message), data)

    def _stage_event(self, kind: str, stage: str, message: str, data: dict, event: dict) -> None:
        status = str(data.get("status", ""))
        if stage and stage != self._stage:
            self.secondary.setProperty("package", False)  # a new step: back to "Step N of M"
        if stage:
            self._stage = stage
        if stage == "storage":
            if status in {"completed", "skipped"}:
                self._storage_started = self._storage_done = True
            else:
                self._storage_started = True
        elif stage == "bootloader" and status in {"completed", "skipped"}:
            self._bootloader_done = True
            self.cancel_note.setText(CANCEL_WARNING_BOOTABLE)
            self.cancel.setToolTip(CANCEL_WARNING_BOOTABLE)
        self.stage.setText(STAGE_LABELS.get(stage, stage.replace("_", " ").title() or "Working…"))
        if kind == "stage" and stage:
            self.checklist.stage_event(stage, status)
        if message:
            self._base_detail = message
            if kind == "stage" and status == "running":
                self._merged_files = 0
            self._refresh_detail()
        self._absorb_outcome(data)
        percent = event.get("percent", event.get("progress"))
        stage_progress = data.get("stage_progress")
        if kind == "stage":
            self.model.stage_event(stage, status, percent, stage_progress)
        else:
            self.model.progress_event(stage, percent, stage_progress)
        self._show_progress()

    def _set_severity(self, severity: str) -> None:
        """Colour of the headline: "" (normal), "ok", "warning" (amber) or "error" (red)."""
        self.stage.setProperty("severity", severity)
        self.stage.style().unpolish(self.stage)
        self.stage.style().polish(self.stage)

    def _show_progress(self) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(min(100, round(self.model.overall)))
        order = [name for name in STAGE_ORDER if name != "cleanup"]
        if self._stage in order and not self.secondary.property("package"):
            self.secondary.setText(f"Step {order.index(self._stage) + 1} of {len(order)} · {round(self.model.local)}% of this step")
            self.secondary.show()

    def _refresh_detail(self) -> None:
        if not self._merged_files:
            self.detail.setText(self._base_detail)
        elif self._base_detail:
            self.detail.setText(f"{self._base_detail} · merged files: {self._merged_files:,}")
        else:
            self.detail.setText(f"Merged files: {self._merged_files:,}")

    def _log_event(self, text: str) -> None:
        for line in text.rstrip().splitlines() or [""]:
            if MERGE_LISTING.match(line):
                if MERGED_FILE.match(line):
                    self._merged_files += 1
                    self._detail_dirty = True
                    if not self._flush_timer.isActive():
                        self.flush_log()
                        self._flush_timer.start()
                continue
            emerging = EMERGING.match(line)
            if emerging:
                number, total, atom = int(emerging[1]), int(emerging[2]), emerging[3]
                self.model.package_event(self._stage, number, total)
                self.secondary.setText(f"Package {number} of {total}" + (f": {atom}" if atom else ""))
                self.secondary.setProperty("package", True)
                self.secondary.show()
                self.progress.setRange(0, 100)
                self.progress.setValue(min(100, round(self.model.overall)))
            self._append_log(line)

    # --- the log pane ---------------------------------------------------------------------------

    def _append_log(self, line: str) -> None:
        """Queue a line. The first line after a quiet moment is shown at once,
        a burst is shown in one append every LOG_FLUSH_MS."""
        self._pending.append(line)
        if not self._flush_timer.isActive():
            self.flush_log()
            self._flush_timer.start()

    def flush_log(self) -> None:
        if self._detail_dirty:
            self._detail_dirty = False
            self._refresh_detail()
        if not self._pending:
            return
        chunk = "\n".join(self._pending)
        self._lines_seen += len(self._pending)
        self._pending.clear()
        self.log.appendPlainText(chunk)
        if not self._truncated and self._lines_seen > LOG_MAX_BLOCKS:
            self._truncated = True
            self.truncated_label.show()
            self.log.appendPlainText(TRUNCATED_NOTE)

    def _flush_tick(self) -> None:
        if self._pending or self._detail_dirty:
            self.flush_log()
        else:
            self._flush_timer.stop()

    def visible_text(self) -> str:
        self.flush_log()
        return self.log.toPlainText()

    # --- outcome -----------------------------------------------------------------------------------

    @property
    def bootable(self) -> bool:
        """The new system can start although the run did not finish cleanly (the bootloader step was done)."""
        return self._bootable

    def _absorb_outcome(self, data: dict) -> bool:
        """Take bootable / degraded / warnings out of an event's data. True when something new was learned."""
        changed = False
        if data.get("bootable") is True and not self._bootable:
            self._bootable, changed = True, True
        elif data.get("bootable") is False and self._bootable:
            self._bootable, changed = False, True
        for key, store in (("degraded", self._degraded), ("warnings", self._warnings)):
            items = data.get(key)
            for item in items if isinstance(items, list) else []:
                text = str(item).strip()
                if text and text not in store:
                    store.append(text)
                    changed = True
        if changed:
            self._refresh_outcome_details()
        return changed

    def _add_warning(self, text: str) -> None:
        """A problem after the installation itself succeeded (cleanup): kept for the result panel and written to the log."""
        if text not in self._warnings:
            self._warnings.append(text)
            self._append_log("NOTE: " + text)
            self._refresh_outcome_details()

    def _refresh_outcome_details(self) -> None:
        lines = []
        if self._degraded:
            lines.append("Some optional parts could not be installed: " + "; ".join(self._degraded) + ". The system works without them; you can add them later.")
        lines += [f"Note: {warning}" for warning in self._warnings]
        self.outcome_details.setText("\n".join(lines))
        self.outcome_details.setVisible(bool(lines))

    def _completed(self, message: str, data: dict) -> None:
        self._terminal = True
        self.flush_log()
        self._absorb_outcome(data)
        if data.get("success", True) is True and not self._failure_reported:
            self.model.finish()
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            self.stage.setText("Installation finished: remove the USB stick, then restart.")
            self._set_severity("ok")
            self.checklist.finish()
            sentence = (message or "Gentoo is installed").strip()
            sentence += "" if sentence.endswith((".", "!", "?")) else "."
            self.detail.setText(f"{sentence} Remove the installation medium (the USB stick or disc) and restart the computer to start Gentoo.")
            self.secondary.hide()
            self.cancel.setVisible(False)
            self.cancel_note.setVisible(False)
            self._show_first_steps(data)
            self.result_hint.setText(
                "The new system passed its final checks. You can save the log for your records."
                if not self._degraded else "The new system works, but some optional parts are missing (see below). You can save the log for your records.")
            self.resume_button.setText("Resume installation")
            self._set_buttons(save=True, diagnostics=True, quit_=True, restart=True)
            self.result_panel.show()
            self.timer.stop()
            self._finished_emitted = True
            self.finished.emit(True)
        elif not self._failure_reported:
            self._fail(message or "The installation did not complete successfully.")
        else:
            self._job_over()

    def _job_over(self) -> None:
        """The failed job has let go of the disks: offer what comes next, and only now tell the window how the run ended."""
        self._terminal = True
        self._present_failure()
        if not self._finished_emitted:
            self._finished_emitted = True
            self.finished.emit(False)

    def _fail(self, message: str, *, stage: str = "", cancelled: bool = False, retryable: bool | None = None, busy: bool = False, blocked: bool = False,
              resumable: bool | None = None) -> None:
        self.flush_log()
        if self._failure_reported and stage == "cleanup":
            self._add_warning(message)  # the cause stays the headline; disks that could not be released are a note beside it
            return
        self._failure_reported = True
        self._cancelled = self._cancelled or cancelled
        self._busy = self._busy or busy
        self._blocked = self._blocked or blocked
        if retryable is not None:
            self._retryable = retryable
        if resumable is not None:
            self._resumable = resumable
        self._failure_message, self._failure_stage = message, stage or self._stage
        line = "ERROR: " + message
        if line != self._last_error_line:
            self._last_error_line = line
            self._append_log(line)
            self.flush_log()
        if self._terminal:
            self._job_over()
        else:
            self._present_failure()

    def _present_failure(self) -> None:
        message, failing = self._failure_message, self._failure_stage
        self.progress.setRange(0, 100)
        self.progress.setValue(min(100, round(self.model.overall)))  # keep how far it got
        label = STAGE_LABELS.get(failing, "")
        self.checklist.fail(failing if failing in CHECKLIST_STAGES else "")
        self._set_severity("warning" if self._cancelled or self._busy or self._blocked or self._bootable else "error")
        if self._cancelled and not self._bootable:
            self.stage.setText("Installation cancelled")
            if not self._storage_started:
                self.detail.setText("No disk has been changed." + (" Choose Try again or Back to settings." if self._terminal else ""))
            elif not self._terminal:
                self.detail.setText(f"{STAGE_LABELS['cleanup']}…")
            elif self._can_resume():
                self.detail.setText("The disk is partially installed; choose Resume or Back to settings.")
            else:
                self.detail.setText(f"The disk is partially installed; choose “{ERASE_AGAIN}” or Back to settings.")
        elif self._busy:
            self.stage.setText("An installation is already running")
            self.detail.setText("Another installation is in progress on this computer. Reconnect to follow it.")
        elif self._blocked:
            self.stage.setText("The installation cannot start yet")
            self.detail.setText(summarize(message))  # the service's own words: what holds the disks, and what to do
        elif self._bootable:
            self.stage.setText("Your new system is installed and can start")
            self.detail.setText(summarize((f"The last step did not finish ({label.lower()}). " if label else "A later step did not finish. ") + message))
        else:
            self.stage.setText(f"Installation needs attention · {label}" if label and failing != "cleanup" else "Installation needs attention")
            self.detail.setText(summarize(message))
        self.cancel.setVisible(False)
        self.cancel_note.setVisible(False)
        self.timer.stop()
        self.secondary.hide()
        self._show_result_panel()

    def _can_resume(self) -> bool:
        """Whether the Resume button is offered: the storage stage finished (it is checkpointed) and the failure can be retried."""
        return self._storage_done and (self._retryable is not False or self._cancelled) and self._manifest is not None and not self._busy

    def _show_result_panel(self) -> None:
        changed = self._storage_started
        resumable = self._can_resume()
        bootable = self._bootable and not self._busy
        over = self._terminal  # the job holds the disks until then: nothing that starts a run or changes a disk can work yet
        self.resume_button.setText("Resume installation")
        if self._busy:
            hint = "Nothing was started. The running installation keeps going; reconnect to follow it."
        elif self._blocked:
            hint = "Nothing was started and nothing on your disks was changed. Close the repair tool (or unmount what it holds), then try again."
        elif not over:
            hint = "The installer is finishing and releasing the disks. What you can do next appears here when that is done."
        elif bootable:
            step = "the desktop step" if self._failure_stage == "desktop" else "the last step"
            resumable = self._resumable is not False and self._retryable is not False and self._manifest is not None
            hint = ("The computer can start the new system now. One later step did not finish, so some programs may be missing. "
                    "You can restart now and fix that from the installed system" + (f", or retry {step} first." if resumable else "."))
            self.resume_button.setText("Retry the desktop step" if self._failure_stage == "desktop" else "Retry the last step")
        elif self._cancelled:
            hint = "The installation was cancelled. " + (
                "Your disk was already modified, so the system on it is incomplete. "
                + ("Resume continues where it stopped without formatting again; " if resumable else "")
                + f"“{ERASE_AGAIN}” erases the disk again."
                if changed else "Nothing on your disks was changed."
            )
        elif changed:
            hint = (
                "Your disk was already modified, so the system on it is incomplete. "
                + ("Resume continues where it stopped without formatting again. " if resumable else "")
                + f"“{ERASE_AGAIN}” erases and installs again; Back to settings lets you change your choices."
            )
        else:
            hint = "Nothing on your disks was changed. Fix the problem described above and try again, or go back to change your settings."
        self.result_hint.setText(hint)
        self.retry_button.setText(ERASE_AGAIN if changed else "Try again")
        self.retry_button.danger = changed
        self._set_buttons(
            retry=over and not self._busy and not bootable, resume=over and resumable, reconnect=self._busy, settings=over and not bootable, save=not self._busy,
            diagnostics=not self._busy, quit_=True, restart=over and bootable,
            repair=over and changed and not bootable and not self._busy and not self._blocked,
        )
        self.result_panel.show()

    def _set_buttons(self, *, retry: bool = False, resume: bool = False, reconnect: bool = False, settings: bool = False, save: bool = False,
                     diagnostics: bool = False, quit_: bool = False, restart: bool = False, repair: bool = False) -> None:
        for command, available in ((self.retry_button, retry), (self.resume_button, resume), (self.reconnect_button, reconnect),
                                   (self.settings_button, settings), (self.save_log_button, save), (self.diagnostics_button, diagnostics),
                                   (self.quit_button, quit_), (self.repair_button, repair)):
            command.available = available
        self.set_restart_offered(restart)

    def _show_first_steps(self, data: dict) -> None:
        """CORE-08 / G04: the card "Your first steps in Gentoo" with the boot check, the news count and the hardware lines."""
        for key in ("boot_check", "news_unread", "first_steps", "login"):
            if key in data:
                self._finish[key] = data[key]
        steps = self.first_steps_model()
        checks = first_steps.checks_lines(steps)
        self.first_steps_checks.setText("\n".join(checks))
        self.first_steps_checks.setVisible(bool(checks))
        self.news_button.setVisible(steps.news_unread is not None and steps.news_unread > 0)
        numbered = [f"{number}. {line}" for number, line in enumerate(first_steps.steps_lines(steps), 1)]
        self.first_steps_text.setText(linkify("\n".join(numbered + first_steps.help_lines())))
        extra = first_steps.hardware_lines(steps.hardware)
        self.first_steps_hardware.setText("For this computer:\n" + "\n".join(f"• {line}" for line in extra) if extra else "")
        self.first_steps_hardware.setVisible(bool(extra))
        where = f"/home/{steps.username}/{first_steps.FIRST_STEPS_FILE}"
        self.first_steps_note.setText(f"The same list is saved in the new system as {where}.")
        self.first_steps_card.show()

    def first_steps_model(self) -> first_steps.FirstSteps:
        """What the finish page tells the user: from the completed event (it also works after an attach), else from the settings."""
        raw_login = self._finish.get("login")
        login: dict = raw_login if isinstance(raw_login, dict) else {}
        manifest = self._manifest
        base = first_steps.from_manifest(manifest) if manifest is not None else first_steps.FirstSteps()
        news = self._finish.get("news_unread")
        return first_steps.FirstSteps(
            username=str(login.get("username") or base.username), hostname=str(login.get("hostname") or base.hostname),
            privilege=str(login.get("privilege") or base.privilege), ssh=bool(login.get("ssh", base.ssh)),
            minimal=bool(login.get("minimal", base.minimal)), bootloader=str(login.get("bootloader") or base.bootloader),
            boot_check=str(self._finish.get("boot_check") or ""), news_unread=news if isinstance(news, int) else None,
            hardware=tuple(str(line) for line in (self._finish.get("first_steps") or ()) if isinstance(line, str)))

    def _connection_failed(self, message: str) -> None:
        if self._retired():
            return
        self._connection_lost("Could not connect to the privileged installer backend. " + message)

    def _worker_finished(self) -> None:
        if self._retired():
            return
        if not self._terminal:
            self._connection_lost("The installer backend stopped before reporting completion.")
        self.workerStopped.emit()

    def _connection_lost(self, message: str) -> None:
        """The stream ended without a result. If the job still runs, offer to
        reconnect; otherwise report the daemon's own last error."""
        if self._terminal:
            return
        state = daemon_client.probe(self._socket_path, timeout=1.0) if self._socket_path else None
        if state is not None and state.reachable and state.job_running:
            self.stage.setText("Lost the connection to the installer")
            can_attach = state.supports("attach.v1")
            self.detail.setText(
                "The installation is still running in the background. Reconnect to follow it."
                if can_attach else
                "The installation is still running in the background, but this installer service cannot be re-attached. Wait for it to finish."
            )
            self.progress.setRange(0, 100)
            self.progress.setValue(min(100, round(self.model.overall)))
            self.cancel.setVisible(False)
            self.cancel_note.setVisible(False)
            self._set_severity("warning")
            self.timer.stop()
            self.result_hint.setText("Nothing was lost: the installer service keeps working while this window is disconnected.")
            self._set_buttons(reconnect=can_attach, save=True, diagnostics=True, quit_=True)
            self.result_panel.show()
            return
        if self._failure_reported:
            self._job_over()  # the stream ended after the error, with no completed event, and the job is gone: nothing more is coming
            return
        specific = str(state.status.get("error", "")) if state is not None and state.reachable else ""
        self._terminal = True
        self._fail(specific or message)

    # --- cancel ----------------------------------------------------------------------------------

    def cancel_dialog(self) -> QMessageBox:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Cancel the installation?")
        if self._bootloader_done:
            box.setText(f"{CANCEL_WARNING_BOOTABLE} Some programs may be missing until then.")
        elif self._storage_done:
            box.setText("The disk has already been modified. Cancelling leaves a partially installed system that cannot boot; you can resume later or start over.")
        elif self._storage_started:
            box.setText("The disk is being changed right now. Cancelling stops this half-way and leaves the disk half-prepared, so it cannot boot; you can only start over.")
        else:
            box.setText("No disk has been changed yet. The installation will stop at the next safe point.")
        keep = box.addButton("Keep installing", QMessageBox.ButtonRole.RejectRole)
        stop = box.addButton("Cancel installation", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        box.stop_button = stop  # type: ignore[attr-defined]
        return box

    def _confirm_cancel(self) -> bool:
        box = self.cancel_dialog()
        box.exec()
        return box.clickedButton() is box.stop_button  # type: ignore[attr-defined]

    def _cancel(self) -> None:
        if self._confirm_cancel():
            self.request_cancel()

    def request_cancel(self) -> None:
        if self._terminal or self._failure_reported:
            return  # the run ended while the question was open: the result stays on the page
        self.cancel.setEnabled(False)
        self.detail.setText("Cancelling… waiting for a safe stopping point")
        if self.worker:
            self.worker.cancel()
        self.cancelRequested.emit()

    def _cancel_acknowledged(self, accepted: bool, message: str) -> None:
        if accepted or self._terminal or self._failure_reported or self._retired():
            return
        self.cancel.setEnabled(True)
        self.detail.setText(f"The cancellation was not accepted: {message}")

    def _tick(self) -> None:
        self.seconds += 1
        self.elapsed.setText(format_elapsed(self.seconds))

    def stop(self) -> None:
        self.timer.stop()
        if self.worker and self.worker.isRunning():
            self.worker.cancel()

    # --- exports ---------------------------------------------------------------------------------------------

    def save_log(self) -> None:
        suggestion = str(Path.home() / f"gentoo-installer-{time.strftime('%Y%m%d-%H%M%S')}.log")
        path, _ = QFileDialog.getSaveFileName(self, "Save the installation log", suggestion, "Log files (*.log *.txt);;All files (*)")
        if path:
            self._run_export(LogExporter(self._socket_path, path, self.visible_text(), self))

    def save_diagnostics_to(self, path: str, collect: Callable[[], dict]) -> None:
        self._run_export(DiagnosticsExporter(path, collect, self))

    def _run_export(self, job: _FileJob) -> None:
        job.done.connect(self._export_done)
        job.finished.connect(lambda: self._exporters.remove(job) if job in self._exporters else None)
        self._exporters.append(job)
        job.start()

    def _export_done(self, ok: bool, message: str) -> None:
        self.notice.setText(message)
        self.notice.show()
        self.logSaved.emit(ok, message)

    def wait_for_exports(self, milliseconds: int = 3000) -> None:
        for job in list(self._exporters):
            job.wait(milliseconds)
