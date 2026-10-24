"""The graphical repair tool: find a Linux system that does not start, open it safely, see what is wrong and repair it.

``RescueWindow(socket_path)`` is a top-level window that talks to the installer daemon's ``rescue.*`` actions (docs/protocol.md) and
deletes itself when it is closed. The installer window opens it from the Welcome page and comes back when it is gone;
``gentoo-installer --mode repair`` shows it alone. The window itself only moves between the pages in ``rescue_ui`` and keeps the
facts they share: the system that is open, what was repaired, and what must be released before the program ends.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QCloseEvent, QGuiApplication, QKeySequence
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from .rescue_ui import launcher, messages, model
from .rescue_ui.base import RescuePage
from .rescue_ui.client import RescueClient
from .rescue_ui.diagnose_page import DiagnosisPage
from .rescue_ui.find_page import FindPage
from .rescue_ui.finish_page import FinishPage
from .rescue_ui.logs_page import LogsPage
from .rescue_ui.mount_page import MountPage
from .rescue_ui.plan_page import PlanPage
from .rescue_ui.service_page import ServicePage

DEFAULT_SOCKET = "/run/gentoo-installer/server.sock"
STEPS = ("Find the system", "Open it", "See what is wrong", "Repair", "Finish")
MIN_WIDTH, MIN_HEIGHT = 800, 520
DEFAULT_SIZE = (1100, 740)
FRAME_RESERVE = 48
SERVICE, FIND, MOUNT, DIAGNOSE, PLAN, LOGS, FINISH = range(7)
UNREACHABLE_TITLE = "The installer service cannot be reached"


def bounded_size(requested: tuple[int, int], available: tuple[int, int]) -> tuple[int, int]:
    """A window that fits the desktop (the pages scroll, so 800x520 is enough)."""
    width = max(MIN_WIDTH, min(requested[0], max(MIN_WIDTH, available[0])))
    height = max(MIN_HEIGHT, min(requested[1], max(MIN_HEIGHT, available[1] - FRAME_RESERVE)))
    return width, height


class RescueWindow(QMainWindow):
    def __init__(self, socket_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.socket_path = socket_path
        self.client = RescueClient(socket_path, self)
        self.system: model.OpenSystem | None = None
        self.history: list[tuple[str, str]] = []
        self.storage_opened = False  # an encrypted disk was unlocked or a volume activated: it must be released
        self._close_after_run = False  # the window was closed during a run: it closes when the run has stopped
        self._close_after_release = False  # the window was closed with a system open: it closes when everything is released
        self._force = False
        self.setWindowTitle("Repair an existing system")
        self.setAccessibleName("Repair an existing system")
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        self.resize(*DEFAULT_SIZE)

        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._sidebar())
        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self._header())
        self.stack = QStackedWidget()
        column.addWidget(self.stack, 1)
        layout.addWidget(content, 1)

        self.service_page = ServicePage()
        self.find_page = FindPage(self.client)
        self.mount_page = MountPage(self.client)
        self.diagnose_page = DiagnosisPage(self.client)
        self.plan_page = PlanPage(self.client)
        self.logs_page = LogsPage(self.client)
        self.finish_page = FinishPage(self.client)
        self.pages: list[RescuePage] = [self.service_page, self.find_page, self.mount_page, self.diagnose_page, self.plan_page, self.logs_page, self.finish_page]
        for page in self.pages:
            self.stack.addWidget(page)
            page.headerChanged.connect(self._refresh_header)
        self._connect()

        quit_action = QAction(self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        self.addAction(quit_action)
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(*bounded_size((self.width(), self.height()), (available.width(), available.height())))
        self.show_page(FIND)
        self.find_page.show_connecting()
        QTimer.singleShot(0, self.start)

    # ---- chrome -----------------------------------------------------------------------------------------------------------------
    def _sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(190)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 20, 14, 16)
        layout.setSpacing(4)
        row = QHBoxLayout()
        mark = QLabel("g")
        mark.setObjectName("brandMark")
        mark.setFixedSize(36, 36)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(mark)
        brand = QLabel("Gentoo\nRepair")
        brand.setObjectName("brand")
        row.addWidget(brand)
        row.addStretch()
        layout.addLayout(row)
        layout.addSpacing(18)
        self.step_buttons: list[QPushButton] = []
        for index, name in enumerate(STEPS):
            button = QPushButton(f"{index + 1}   {name}")
            button.setObjectName("stepButton")
            button.setProperty("current", False)
            button.setEnabled(False)  # the steps show where you are; the buttons of each page move you on
            self.step_buttons.append(button)
            layout.addWidget(button)
        layout.addStretch()
        note = QLabel("Nothing is changed until you have read a plan and confirmed it.")
        note.setWordWrap(True)
        layout.addWidget(note)
        return sidebar

    def _header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("contentHeader")
        layout = QVBoxLayout(header)
        layout.setContentsMargins(24, 12, 24, 10)
        layout.setSpacing(2)
        self.header_title = QLabel()
        self.header_title.setObjectName("pageTitle")
        self.header_title.setWordWrap(True)
        layout.addWidget(self.header_title)
        self.header_subtitle = QLabel()
        self.header_subtitle.setObjectName("pageSubtitle")
        self.header_subtitle.setWordWrap(True)
        layout.addWidget(self.header_subtitle)
        return header

    def _refresh_header(self) -> None:
        page = self.current_page
        self.header_title.setText(page.title)
        self.header_subtitle.setText(page.subtitle)

    def show_page(self, index: int) -> None:
        page = self.pages[index]
        self.stack.setCurrentIndex(index)
        self._refresh_header()
        for position, button in enumerate(self.step_buttons):
            button.setProperty("current", position == page.step)
            button.style().unpolish(button)
            button.style().polish(button)
        page.on_show()
        page.focus_first()

    @property
    def current_page(self) -> RescuePage:
        return self.pages[self.stack.currentIndex()]

    # ---- wiring -------------------------------------------------------------------------------------------------------------------
    def _connect(self) -> None:
        self.service_page.retryRequested.connect(self.start)
        self.service_page.terminalToolRequested.connect(self._open_tool_terminal)
        self.service_page.quitRequested.connect(self.close)
        self.find_page.systemChosen.connect(self._system_chosen)
        self.find_page.resumeRequested.connect(self._resume)
        self.find_page.refreshRequested.connect(self._look_for_open_sessions)
        self.find_page.serviceProblem.connect(lambda text: self._show_service(UNREACHABLE_TITLE, messages.describe_daemon_problem("", text)))
        self.find_page.storageChanged.connect(self._storage_changed)
        self.find_page.quitRequested.connect(self.close)
        self.mount_page.backRequested.connect(lambda: self.show_page(FIND))
        self.mount_page.opened.connect(self._opened)
        self.mount_page.adoptRequested.connect(self._adopt)
        self.mount_page.serviceProblem.connect(lambda text: self._show_service(UNREACHABLE_TITLE, messages.describe_daemon_problem("", text)))
        self.diagnose_page.actionRequested.connect(self._action_requested)
        self.diagnose_page.logsRequested.connect(self._show_logs)
        self.diagnose_page.hardwareChanged.connect(lambda moved: self.refresh_system(moved))
        self.diagnose_page.refreshRequested.connect(lambda: self.refresh_system())
        self.diagnose_page.finishRequested.connect(self._show_finish)
        self.plan_page.leaveRequested.connect(self._plan_left)
        self.plan_page.runningChanged.connect(self._running_changed)
        self.logs_page.backRequested.connect(lambda: self.show_page(DIAGNOSE))
        self.finish_page.backRequested.connect(self._back_from_finish)
        self.finish_page.anotherRequested.connect(self._another)
        self.finish_page.quitRequested.connect(self._quit_released)
        self.finish_page.released.connect(self._released)

    # ---- starting ---------------------------------------------------------------------------------------------------------------
    def start(self) -> None:
        """Ask the daemon what it can do (status and capabilities, off the GUI thread); scan when it can repair."""
        if self.client.stopping:
            return
        self.find_page.show_connecting()
        self.show_page(FIND)
        probe = self.client.probe()
        probe.done.connect(self._probed)

    def _probed(self, state: Any) -> None:
        if self.client.stopping:
            return
        if not state.reachable:
            self._show_service(UNREACHABLE_TITLE, messages.describe_daemon_problem(state.kind, state.message))
        elif not state.supports("rescue.v1"):
            self._show_service(messages.NO_RESCUE_TITLE, messages.NO_RESCUE_TEXT)
        elif state.job_running:
            self._show_service("An installation is running", messages.INSTALL_RUNNING_TEXT)
        else:
            self._look_for_open_sessions()

    def _look_for_open_sessions(self) -> None:
        """Systems an earlier window left open are offered first; the scan starts either way."""
        def listed(result: dict[str, Any]) -> None:
            self.find_page.show_open_sessions([item for item in result.get("sessions", []) if str(item.get("state", "")).startswith("mounted")])
            self.find_page.show_leftovers(result.get("orphans", []), result.get("pool", []))
            self.find_page.start_scan()

        def failed(*_args: Any) -> None:
            self.find_page.start_scan()

        self.client.request("rescue.session", {}).then(listed, failed, failed)

    def _show_service(self, title: str, text: str) -> None:
        self.find_page.busy.stop()
        self.service_page.show_problem(title, text)
        self.show_page(SERVICE)

    def _open_tool_terminal(self) -> None:
        started, problem = launcher.open_tool_terminal()
        self.service_page.notice.show_message("info" if started else "error", "A terminal window was opened." if started else problem)

    # ---- the flow -----------------------------------------------------------------------------------------------------------------
    def _storage_changed(self) -> None:
        self.storage_opened = True

    def _system_chosen(self, candidate: dict[str, Any], scan: dict[str, Any]) -> None:
        self.mount_page.begin(candidate, scan)
        self.show_page(MOUNT)

    def _opened(self, info: dict[str, Any]) -> None:
        self.system = model.OpenSystem(info)
        self.history = []
        self.diagnose_page.show_system(self.system)
        self.show_page(DIAGNOSE)

    def _action_requested(self, spec: dict[str, Any], moved: bool) -> None:
        if self.system is None:
            return
        self.plan_page.begin(self.system, spec, hardware_changed=moved)
        self.show_page(PLAN)

    def _plan_left(self, changed: bool) -> None:
        if changed and self.system is not None:
            tracker = self.plan_page.tracker
            outcome = tracker.outcome if tracker is not None else ""
            self.history.append((self._history_title(), outcome))
        self.show_page(DIAGNOSE)
        if changed:
            self.refresh_system()

    def _history_title(self) -> str:
        """What the repair was, in words: the combined repair is listed by the repairs it contained."""
        plan = self.plan_page.plan
        titles = {str(item.get("id")): str(item.get("title")) for item in (self.system.actions if self.system is not None else [])}
        included = (plan.get("params") or {}).get("actions") if plan.get("action") == model.RECOMMENDED_ACTION else None
        if included:
            return ", ".join(titles.get(str(name), str(name)) for name in included)
        return str(plan.get("title") or self.plan_page.spec.get("title") or "Repair")

    def _show_logs(self) -> None:
        if self.system is not None:
            self.logs_page.begin(self.system)
            self.show_page(LOGS)

    def _show_finish(self) -> None:
        self.finish_page.begin(self.system, self.history)
        self.show_page(FINISH)

    def _another(self) -> None:
        self.system, self.history, self.storage_opened = None, [], False
        self.show_page(FIND)
        self.find_page.start_scan()

    def _back_from_finish(self) -> None:
        self._close_after_release = False  # the person changed their mind about leaving
        self.show_page(DIAGNOSE)

    def _released(self) -> None:
        self.system, self.storage_opened = None, False
        self._maybe_finish_closing()

    def _quit_released(self) -> None:
        self._force = True
        self.close()

    # ---- keeping the diagnosis current -------------------------------------------------------------------------------------------------------
    def refresh_system(self, hardware_changed: bool | None = None) -> None:
        """Ask again for the state of the system, the findings and the actions (a repair changes all three)."""
        system = self.system
        if system is None:
            return
        moved = self.diagnose_page.moved if hardware_changed is None else hardware_changed
        self.diagnose_page.set_busy("Checking the system again…")
        sid = system.session_id

        def failed(*args: Any) -> None:
            self.diagnose_page.set_busy("")
            self.diagnose_page.show_system(system)
            self.diagnose_page.notice.show_message("error", "The system could not be checked again: " + str(args[1] if len(args) > 1 else args[0]))

        def got_session(info: dict[str, Any]) -> None:
            system.merge(mode=info.get("mode"), state=info.get("state"), mounts=info.get("mounts"), warnings=info.get("warnings"), generation=info.get("generation"))
            self.client.request("rescue.query", {"session_id": sid, "query": "diag.summary", "params": {"hardware_changed": moved}}).then(got_findings, failed, failed)

        def got_findings(result: dict[str, Any]) -> None:
            data = result.get("data", {})
            system.merge(findings=data.get("findings"), recommended=data.get("recommended"))
            self.client.request("rescue.query", {"session_id": sid, "query": "actions.list"}).then(got_actions, failed, failed)

        def got_actions(result: dict[str, Any]) -> None:
            system.merge(actions=result.get("data", {}).get("actions"))
            self.diagnose_page.set_busy("")
            self.diagnose_page.show_system(system)

        self.client.request("rescue.session", {"session_id": sid}).then(got_session, failed, failed)

    def _adopt(self, device: str) -> None:
        """A system may already be open in a session of the daemon (the connection broke while it was being opened, or an earlier window left it)."""
        def listed(result: dict[str, Any]) -> None:
            found = next((item for item in result.get("sessions", []) if (item.get("root") or {}).get("device") == device and str(item.get("state", "")).startswith("mounted")), None)
            if found is None:
                self.mount_page.not_adopted()
            else:
                self._resume(found)

        self.client.request("rescue.session", {}).then(listed, lambda *_a: self.mount_page.not_adopted(), lambda *_a: self.mount_page.not_adopted())

    def _resume(self, session: dict[str, Any]) -> None:
        """Continue with a session that is open already: the reply of rescue.session lacks the findings and actions, so they are asked for."""
        sid = session["session_id"]

        def failed(*args: Any) -> None:
            self.find_page.notice.show_message("error", "The open system could not be read: " + str(args[1] if len(args) > 1 else args[0]))

        def with_findings(result: dict[str, Any]) -> None:
            data = result.get("data", {})
            info = {**session, "findings": data.get("findings", []), "recommended": data.get("recommended", [])}
            self.client.request("rescue.query", {"session_id": sid, "query": "actions.list"}).then(lambda r: with_actions(info, r), failed, failed)

        def with_actions(info: dict[str, Any], result: dict[str, Any]) -> None:
            info["actions"] = result.get("data", {}).get("actions", [])
            self._opened(info)

        self.client.request("rescue.query", {"session_id": sid, "query": "diag.summary"}).then(with_findings, failed, failed)

    # ---- closing ------------------------------------------------------------------------------------------------------------------
    def ask_close_choice(self, what: str) -> str:
        """'close' (release first, then quit), 'leave' (quit and leave it open) or 'stay'. Replaceable in tests."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Close the system first?")
        box.setText(f"{what}\n\nIf you quit now, the disks stay open, and the computer may not shut down cleanly.")
        close = box.addButton("Close it and quit", QMessageBox.ButtonRole.AcceptRole)
        leave = box.addButton("Quit and leave it open", QMessageBox.ButtonRole.DestructiveRole)
        stay = box.addButton("Stay", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stay)
        box.setEscapeButton(stay)
        box.exec()
        clicked = box.clickedButton()
        return "close" if clicked is close else "leave" if clicked is leave else "stay"

    def ask_stop_and_close(self) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("A repair is running")
        box.setText("A repair is running. If you close the window, the repair stops after the step that is running, and then the window closes.")
        stop = box.addButton("Stop the repair and close", QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton("Keep the window open", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        box.exec()
        return box.clickedButton() is stop

    def show_info(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._force:
            self._shutdown()
            super().closeEvent(event)
            return
        blocker = self.plan_page.close_blocker() or self.finish_page.close_blocker()
        if blocker:
            self.show_info("Please wait", blocker)
            event.ignore()
            return
        if self.plan_page.is_running():
            if self.ask_stop_and_close():
                self._close_after_run = True
                self.plan_page.request_cancel()
            event.ignore()
            return
        if self.finish_page.state == "working":
            event.ignore()
            return
        if self.system is not None or self.storage_opened:
            what = (f"The system {self.system.title} on {self.system.root_device} is still open." if self.system is not None
                    else "Encrypted disks or volumes that you opened are still open.")
            choice = self.ask_close_choice(what)
            if choice == "stay":
                event.ignore()
                return
            if choice == "close":
                self._close_after_release = True
                self.finish_page.begin(self.system, self.history)
                self.show_page(FINISH)
                self.finish_page.unmount()
                event.ignore()
                return
        self._shutdown()
        super().closeEvent(event)

    def _maybe_finish_closing(self) -> None:
        if self._close_after_release and self.finish_page.state == "done":
            self._force = True
            QTimer.singleShot(0, self.close)

    def _shutdown(self) -> None:
        """End every request so that no thread is still reading from the daemon when the window is gone."""
        self.client.abort_all()
        self.client.wait_all(3000)

    def force_close(self) -> None:
        """Close without asking (a test, or the end of the program)."""
        self._force = True
        self.close()

    def _running_changed(self, running: bool) -> None:
        """A run ended while the window was waiting to close: close it now (it still asks about a system that is open)."""
        if not running and self._close_after_run and self.finish_page.state != "working":
            self._close_after_run = False
            QTimer.singleShot(0, self.close)


def main(argv: list[str] | None = None) -> int:
    """``python -m gentoo_installer.rescue_app``: the repair tool on its own (the installer starts it with ``--mode repair``)."""
    from .theme import apply_theme

    parser = argparse.ArgumentParser(description="Graphical repair tool for Linux systems that do not start")
    parser.add_argument("--socket", default=os.environ.get("GENTOO_INSTALLER_SOCKET", DEFAULT_SOCKET))
    args = parser.parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Gentoo Repair")
    app.setOrganizationName("GentooLive")
    apply_theme(app)
    window = RescueWindow(args.socket)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
