from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QProcess, QSettings, QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import daemon_client, diagnostics, keyboard, system_info
from .backend import DEFAULT_SOCKET, BackendWorker
from .manifest import SECRET_KEYS, InstallManifest, ManifestError
from .pages.accounts import AccountsPage
from .pages.customize import CustomizePage, auto_make_opts
from .pages.progress import ProgressPage
from .pages.readiness import ReadinessPage
from .pages.review import ReviewPage, disk_description, format_lines, selection_problems
from .pages.storage import StoragePage
from .pages.system import SystemPage
from .pages.welcome import WelcomePage
from .system_info import SystemInfo, inspect_system
from .theme import apply_theme
from .widgets import Banner

LOG = logging.getLogger("gentoo_installer.app")

STEPS = [
    ("Welcome", "A simpler route to Gentoo"),
    ("Readiness", "Check your hardware and connection"),
    ("Storage", "Choose disks and filesystems"),
    ("Base system", "Init, stage3, kernel, and desktop"),
    ("Customization", "Portage, networking, and applications"),
    ("Accounts", "Users, locale, and regional settings"),
    ("Review", "Confirm your installation"),
    ("Install", "Build your new Gentoo system"),
]
WELCOME, READINESS, STORAGE, SYSTEM, CUSTOMIZE, ACCOUNTS, REVIEW, INSTALL = range(8)
# Which wizard page can fix the errors of a validate_sections() section.
SECTION_PAGES = {
    "storage": STORAGE, "system": SYSTEM, "portage": CUSTOMIZE, "network": CUSTOMIZE, "options": CUSTOMIZE,
    "accounts": ACCOUNTS, "locale": ACCOUNTS, "hardware": REVIEW, "general": ACCOUNTS,
}
SECTION_ORDER = ("general", "storage", "system", "portage", "network", "options", "accounts", "locale", "hardware")
HANDBOOK_URL = "https://wiki.gentoo.org/wiki/Handbook:AMD64/Full/Installation"
REPAIR_MISSING = (
    "The graphical repair tools are not part of this installer image.\n\n"
    "The repair tools are available from the terminal as 'gentoo-rescue'. "
    "Open a terminal and run: gentoo-rescue --help"
)

MIN_WINDOW_WIDTH = 800
MIN_WINDOW_HEIGHT = 560
DEFAULT_WINDOW_SIZE = (1180, 790)
WINDOW_FRAME_RESERVE = 48


def bounded_window_height(requested: int, available: int) -> int:
    """Keep the client area inside the desktop after title-bar/frame space."""
    maximum = max(MIN_WINDOW_HEIGHT, available - WINDOW_FRAME_RESERVE)
    return max(MIN_WINDOW_HEIGHT, min(requested, maximum))


def bounded_window_size(requested_width: int, requested_height: int, available_width: int, available_height: int) -> tuple[int, int]:
    """A window that fits the desktop (pages scroll, so 800x560 is enough)."""
    width = max(MIN_WINDOW_WIDTH, min(requested_width, max(MIN_WINDOW_WIDTH, available_width)))
    return width, bounded_window_height(requested_height, available_height)


def blank_secrets(value: Any) -> Any:
    """Remove everything that must not be saved in the draft: secrets, and the
    credentials inside a proxy URL."""
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if key in SECRET_KEYS:
                value[key] = ""
            elif key == "proxy" and isinstance(item, str):
                value[key] = diagnostics.strip_url_userinfo(item)
            else:
                value[key] = blank_secrets(item)
    elif isinstance(value, list):
        return [blank_secrets(item) for item in value]
    return value


def unredact(value: Any) -> Any:
    """'<redacted>' placeholders of an exported settings file become empty fields."""
    if isinstance(value, dict):
        return {key: unredact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unredact(item) for item in value]
    return "" if value == "<redacted>" else value


def load_redacted_manifest(path: str | Path) -> InstallManifest:
    """Read a settings file saved with 'Save a redacted copy' (no validation:
    the secrets are missing and must be typed again)."""
    loader = getattr(InstallManifest, "load_redacted", None)
    if callable(loader):
        return loader(path)
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"Cannot read the settings file: {exc}") from exc
    return InstallManifest.from_dict(unredact(raw))


def load_draft() -> InstallManifest:
    settings = QSettings("GentooLive", "Installer")
    text = settings.value("draft", "")
    if not isinstance(text, str) or not text:
        return InstallManifest()
    try:
        return InstallManifest.from_dict(json.loads(text))
    except (json.JSONDecodeError, ManifestError, TypeError, ValueError):
        return InstallManifest()


def create_repair_window(socket_path: str) -> QWidget | None:
    """The repair UI (gentoo_installer.rescue_app.RescueWindow), or None when
    this build does not contain it."""
    try:
        module = importlib.import_module("gentoo_installer.rescue_app")
    except ModuleNotFoundError as exc:
        if exc.name == "gentoo_installer.rescue_app":
            return None
        raise
    factory = getattr(module, "RescueWindow", None)
    return factory(socket_path) if callable(factory) else None


class InspectWorker(QThread):
    """Runs the (slow) inspection off the GUI thread."""

    done = Signal(object, int)

    def __init__(self, socket_path: str, payload: dict[str, Any], generation: int, parent=None):
        super().__init__(parent)
        self.socket_path = socket_path
        self.payload = payload
        self.generation = generation

    def run(self) -> None:
        # inspect_system is looked up at call time so tests can replace it.
        try:
            info = system_info.inspect_with_daemon(self.socket_path, local=inspect_system, request_payload=self.payload)
        except Exception as exc:  # noqa: BLE001 - never leave the page on "Checking..."
            LOG.exception("Inspection failed")
            info = SystemInfo.pending()
            info.inspected = True
            info.source = "local"
            info.errors = [f"The hardware check failed: {exc}"]
        self.done.emit(info, self.generation)


class InstallerWindow(QMainWindow):
    inspectionFinished = Signal()

    def __init__(
        self,
        manifest: InstallManifest,
        socket_path: str,
        *,
        firmware_explicit: bool = False,
        background_inspection: bool = True,
    ):
        super().__init__()
        self.manifest = manifest
        self.socket_path = socket_path
        self.firmware_explicit = firmware_explicit
        self.background_inspection = background_inspection
        self.settings = QSettings("GentooLive", "Installer")
        self.current = 0
        self.install_started = False
        self._install_succeeded = False
        self._close_after_worker = False
        self._inspect_generation = 0
        self._workers: list[QThread] = []
        self._repair_window: QWidget | None = None
        self.setWindowTitle("Gentoo Installer")
        self.setMinimumSize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        self.resize(*DEFAULT_WINDOW_SIZE)
        self.setAccessibleName("Gentoo Installer")

        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._sidebar())

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self._header())
        self.banner = Banner()
        content_layout.addWidget(self.banner)
        self.stack = QStackedWidget()
        content_layout.addWidget(self.stack, 1)
        layout.addWidget(content, 1)

        # The cheap facts (architecture, RAM, firmware) are known at once; the
        # disk scan and the network probes may take seconds and run in a thread.
        self.info = SystemInfo.pending() if background_inspection else self._collect_info()
        self._seed_firmware()
        self.welcome = WelcomePage(self.manifest)
        self.readiness = ReadinessPage(self.manifest)
        self.readiness.update_info(self.info)
        self.storage = StoragePage(self.manifest, self.info)
        self.system = SystemPage(self.manifest)
        self.customize = CustomizePage(self.manifest)
        self.accounts = AccountsPage(self.manifest)
        self.review = ReviewPage(self.manifest, self.info)
        self.progress = ProgressPage()
        self.pages = [self.welcome, self.readiness, self.storage, self.system, self.customize, self.accounts, self.review, self.progress]
        for page in self.pages:
            self.stack.addWidget(page)
            page.advanceRequested.connect(self._advance_requested)
        self.welcome.tryLiveRequested.connect(self.close)
        self.welcome.applicationRequested.connect(self.launch_application)
        self.welcome.handbookRequested.connect(self.open_handbook)
        self.welcome.installRequested.connect(lambda: self._show_page(READINESS))
        self.welcome.repairRequested.connect(self.open_repair)
        self.welcome.importRequested.connect(self.import_settings)
        self.welcome.keyboardChanged.connect(self._keyboard_changed)
        self.readiness.refreshRequested.connect(self.refresh_readiness)
        self.readiness.networkSettingsRequested.connect(lambda: QProcess.startDetached("nm-connection-editor", []))
        self.readiness.diagnosticsRequested.connect(lambda: self.save_diagnostics("readiness"))
        self.readiness.clockSyncRequested.connect(self.sync_clock)
        self.review.importRequested.connect(self.import_settings)
        self.accounts.keyboardChangeRequested.connect(lambda: self.go_to(WELCOME))
        self.system.desktopChanged.connect(self.customize.desktop_changed)
        self.progress.rebootRequested.connect(self.reboot)
        self.progress.started.connect(self._install_running)
        self.progress.finished.connect(self._install_finished)
        self.progress.workerStopped.connect(self._worker_stopped)
        self.progress.retryRequested.connect(self.start_installation)
        self.progress.settingsRequested.connect(self._return_to_settings)
        self.progress.quitRequested.connect(self.close)
        self.progress.diagnosticsRequested.connect(lambda: self.save_diagnostics("installation failed"))
        self.progress.logSaved.connect(self._export_saved)
        content_layout.addWidget(self._navigation())

        saved_mode = self.settings.value("mode")
        if isinstance(saved_mode, str) and self.mode.findData(saved_mode) >= 0:
            self.mode.setCurrentIndex(self.mode.findData(saved_mode))
        self._show_page(WELCOME)
        self._mode_changed()
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(*bounded_window_size(self.width(), self.height(), available.width(), available.height()))

        quit_action = QAction(self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        self.addAction(quit_action)
        QShortcut(QKeySequence("Alt+Left"), self).activated.connect(self.previous_page)
        QShortcut(QKeySequence("Alt+Right"), self).activated.connect(self._advance_requested)

        if background_inspection:
            self._start_inspection()
        else:
            self.apply_info(self.info)

    # --- chrome ---------------------------------------------------------------------------------------

    def _sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(230)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(20, 24, 20, 20)
        layout.setSpacing(4)
        brand_row = QHBoxLayout()
        mark = QLabel("g")
        mark.setObjectName("brandMark")
        mark.setFixedSize(40, 40)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_row.addWidget(mark)
        brand = QLabel("Gentoo\nInstaller")
        brand.setObjectName("brand")
        brand_row.addWidget(brand)
        brand_row.addStretch()
        layout.addLayout(brand_row)
        layout.addSpacing(24)
        self.step_buttons: list[QPushButton] = []
        for index, (name, _) in enumerate(STEPS):
            button = QPushButton(f"{index + 1}   {name}")
            button.setObjectName("stepButton")
            button.setProperty("current", False)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, target=index: self.go_to(target))
            layout.addWidget(button)
            self.step_buttons.append(button)
        layout.addStretch()
        help_label = QLabel("Based on the official\nAMD64 Gentoo Handbook")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        handbook = QPushButton("Handbook  ↗")
        handbook.setObjectName("stepButton")
        handbook.clicked.connect(self.open_handbook)
        layout.addWidget(handbook)
        return sidebar

    def _header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("contentHeader")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(32, 20, 32, 18)
        titles = QVBoxLayout()
        self.header_title = QLabel()
        self.header_title.setObjectName("pageTitle")
        titles.addWidget(self.header_title)
        self.header_subtitle = QLabel()
        self.header_subtitle.setObjectName("pageSubtitle")
        titles.addWidget(self.header_subtitle)
        layout.addLayout(titles)
        layout.addStretch()
        mode_label = QLabel("&Experience")
        mode_label.setProperty("muted", True)
        layout.addWidget(mode_label)
        self.mode = QComboBox()
        self.mode.addItem("Guided", "guided")
        self.mode.addItem("Advanced", "advanced")
        self.mode.setAccessibleName("Installer experience")
        mode_label.setBuddy(self.mode)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        layout.addWidget(self.mode)
        return header

    def _navigation(self) -> QFrame:
        navigation = QFrame()
        navigation.setObjectName("contentHeader")
        layout = QHBoxLayout(navigation)
        layout.setContentsMargins(32, 14, 32, 16)
        self.back = QPushButton("&Back")
        self.back.setToolTip("Alt+Left")
        self.back.clicked.connect(self.previous_page)
        layout.addWidget(self.back)
        layout.addStretch()
        self.next = QPushButton("&Continue")
        self.next.setObjectName("primaryButton")
        self.next.setToolTip("Alt+Right")
        self.next.clicked.connect(self.next_page)
        layout.addWidget(self.next)
        return navigation

    # --- navigation -------------------------------------------------------------------------------------

    def _show_page(self, index: int) -> None:
        self.current = index
        self.stack.setCurrentIndex(index)
        title, subtitle = STEPS[index]
        self.header_title.setText(title)
        self.header_subtitle.setText(subtitle)
        self.pages[index].load_manifest()
        self._update_navigation()
        self.pages[index].focus_first()

    def _update_navigation(self) -> None:
        index = self.current
        locked = self.install_started or self._install_succeeded
        for position, button in enumerate(self.step_buttons):
            button.setProperty("current", position == index)
            button.setEnabled(position < index and not locked)
            button.style().unpolish(button)
            button.style().polish(button)
        self.back.setVisible(index > 0 and index < INSTALL and not self.install_started)
        self.next.setVisible(index < INSTALL)
        self.next.setText("&Start installation" if index == REVIEW else "&Continue")
        self.next.setObjectName("dangerButton" if index == REVIEW else "primaryButton")
        self.next.style().unpolish(self.next)
        self.next.style().polish(self.next)
        # Page 1 needs the finished inspection; Continue waits for it.
        self.next.setEnabled(not (index == READINESS and not self.info.inspected))
        self.mode.setEnabled(index < INSTALL and not self.install_started)

    def go_to(self, index: int) -> None:
        if self.install_started or index >= self.current:
            return
        self.pages[self.current].save_manifest()
        self._show_page(index)

    def _advance_requested(self) -> None:
        if self.next.isVisible() and self.next.isEnabled():
            self.next.click()

    def next_page(self) -> None:
        page = self.pages[self.current]
        valid, message = page.validate_page()
        if not valid:
            QMessageBox.warning(self, "Check this step", message)
            return
        page.save_manifest()
        if self.current == REVIEW:
            self.start_installation()
            return
        if self.current + 1 == REVIEW:
            for earlier in self.pages[:REVIEW]:
                earlier.save_manifest()
            owner, error = self._first_manifest_error()
            if error:
                QMessageBox.warning(self, "Installation settings", error)
                if owner is not None and owner != self.current:
                    self._show_page(owner)
                return
        self._show_page(self.current + 1)

    def _first_manifest_error(self) -> tuple[int | None, str]:
        """The first problem of the whole manifest and the wizard page that owns it."""
        sections = self.manifest.validate_sections()
        for name in SECTION_ORDER:
            if sections.get(name):
                return self._page_for_section(name), sections[name][0]
        return None, ""

    def _page_for_section(self, section: str) -> int:
        if section == "general":  # a redacted secret: find where it sits
            if self.manifest.storage.luks_passphrase == "<redacted>":
                return STORAGE
            if self.manifest.network.wifi_password == "<redacted>":
                return CUSTOMIZE
        return SECTION_PAGES.get(section, REVIEW)

    def previous_page(self) -> None:
        if self.current > 0 and not self.install_started:
            self.pages[self.current].save_manifest()
            self._show_page(self.current - 1)

    def _mode_changed(self) -> None:
        advanced = self.mode.currentData() == "advanced"
        self.storage.encryption.setVisible(advanced or self.storage.luks.isChecked())
        suffix = " · All available controls are shown" if advanced else " · Recommended choices are selected for you"
        self.header_subtitle.setText(STEPS[self.current][1] + suffix)

    # --- hardware inspection ------------------------------------------------------------------------------

    def _inspect_payload(self) -> dict[str, Any]:
        """What the daemon needs to judge this selection (no accounts, no secrets)."""
        raw = self.manifest.to_dict()
        raw["storage"]["luks_passphrase"] = ""
        raw["network"]["wifi_password"] = ""
        return {"manifest": {name: raw[name] for name in ("storage", "system", "portage", "network", "options", "hardware") if name in raw}}

    def _collect_info(self) -> SystemInfo:
        system_info.set_probe_target(self.manifest.portage.mirror, self.manifest.network.proxy)
        return system_info.inspect_with_daemon(self.socket_path, local=inspect_system, request_payload=self._inspect_payload())

    def _start_inspection(self) -> None:
        self._inspect_generation += 1
        system_info.set_probe_target(self.manifest.portage.mirror, self.manifest.network.proxy)
        worker = InspectWorker(self.socket_path, self._inspect_payload(), self._inspect_generation, self)
        worker.done.connect(self._inspection_done)
        worker.finished.connect(lambda w=worker: self._workers.remove(w) if w in self._workers else None)
        worker.finished.connect(worker.deleteLater)
        self._workers.append(worker)
        worker.start()

    def _inspection_done(self, info: SystemInfo, generation: int) -> None:
        if generation != self._inspect_generation:
            return  # a newer check superseded this one
        self.apply_info(info)

    def refresh_readiness(self) -> None:
        self.readiness.save_manifest()
        if self.background_inspection:
            self.readiness.set_checking()
            self._start_inspection()
        else:
            self._inspect_generation += 1
            self.apply_info(self._collect_info())

    def apply_info(self, info: SystemInfo) -> None:
        """Make a (new) inspection result visible on every page."""
        self.info = info
        self._seed_firmware()
        self.readiness.update_info(info)
        self.storage.set_info(info)
        self.review.set_info(info)
        self._update_navigation()
        self._check_job_state(info)
        self.inspectionFinished.emit()

    def wait_for_inspection(self, milliseconds: int = 60000) -> bool:
        """Block (processing events) until the first inspection finished; for scripts."""
        deadline = time.monotonic() + milliseconds / 1000
        while not self.info.inspected and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        return self.info.inspected

    def _seed_firmware(self) -> None:
        """New installs default to the firmware the live session booted with; only
        a manifest passed on the command line or a user pick is left alone."""
        storage_page = getattr(self, "storage", None)
        if self.firmware_explicit or (storage_page is not None and storage_page.firmware_touched):
            return
        self.manifest.storage.firmware = "uefi" if self.info.uefi else "bios"

    # --- a running or interrupted installation -------------------------------------------------------------

    def _check_job_state(self, info: SystemInfo) -> None:
        state = info.daemon
        if state is None or not state.reachable or self.install_started:
            return
        status = state.status
        if state.job_running:
            started = status.get("started_at")
            when = time.strftime("%H:%M", time.localtime(started)) if isinstance(started, (int, float)) else "earlier"
            stage = str(status.get("stage") or "")
            text = f"An installation started at {when} is still running" + (f" (stage: {stage})" if stage else "") + "."
            actions = [("cancel", "Cancel it", self.cancel_running_job), ("ignore", "Ignore", self.banner.dismiss)]
            if state.supports("attach.v1"):
                actions.insert(0, ("attach", "Attach (show progress)", self.attach_running_job))
            else:
                text += " This installer service cannot show a running installation again; wait for it to finish, or cancel it."
            self.banner.show_message(text, actions)
        elif state.job_status in {"failed", "cancelled"} and status.get("resumable"):
            self.banner.show_message(
                f"An earlier installation stopped ({state.job_status}) after the disk was prepared"
                + (f" while it was at '{status['stage']}'" if status.get("stage") else "")
                + ". Choose the same settings again and tick “Resume” on the review page to continue without erasing the disk, or start over.",
                [("ok", "OK", self.banner.dismiss)],
                error=True,
            )
        else:
            self.banner.dismiss()

    def attach_running_job(self) -> None:
        self.banner.dismiss()
        self.install_started = True
        self._show_page(INSTALL)
        self.progress.attach(self.socket_path)

    def cancel_running_job(self) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Cancel the running installation?")
        box.setText("The installation will stop at the next safe point. A disk that was already changed stays partially installed; you can resume or start over later.")
        keep = box.addButton("Keep it running", QMessageBox.ButtonRole.RejectRole)
        stop = box.addButton("Cancel installation", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(keep)
        box.exec()
        if box.clickedButton() is not stop:
            return
        accepted, message = daemon_client.request_cancel(self.socket_path)
        self.banner.show_message(message if accepted else f"The cancellation was not accepted: {message}", [("ok", "OK", self.banner.dismiss)], error=not accepted)

    # --- starting ---------------------------------------------------------------------------------------------

    def _service_ready(self) -> bool:
        """Right before anything destructive: is the daemon still there and idle?"""
        if os.environ.get("GENTOO_INSTALLER_DEV") == "1":
            return True
        state = daemon_client.ping(self.socket_path, timeout=1.5)
        if not state.reachable:
            QMessageBox.warning(self, "Installer service unavailable", state.message)
            return False
        if state.job_running:
            if not daemon_client.probe(self.socket_path, timeout=1.5).supports("attach.v1"):
                QMessageBox.warning(self, "An installation is already running", "The installer service is already running an installation, and this version cannot show it again. Wait for it to finish.")
                return False
            answer = QMessageBox.question(
                self, "An installation is already running",
                "The installer service is already running an installation. Show its progress instead?",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.attach_running_job()
            return False
        return True

    def start_installation(self) -> None:
        storage = self.manifest.storage
        if not self._service_ready():
            return
        problems = selection_problems(self.manifest, self.info)
        if problems:
            QMessageBox.warning(self, "Cannot start", problems[0])
            return
        resume = self.review.resume_requested() and self.current == REVIEW
        if not resume and storage.mode == "erase":
            disk = self.info.find_disk(storage.device)
            contents = f"\n\nIt holds: {disk.contents_text}." if disk is not None else ""
            answer = QMessageBox.warning(
                self,
                "Erase the selected disk?",
                f"This will permanently erase all data on {disk_description(disk, storage.device)}.{contents}\n\n"
                "No other disk is selected. Do you want to continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        elif not resume and storage.mode == "manual" and any(partition.format for partition in storage.partitions):
            lines = [line for line in format_lines(self.manifest, self.info) if line.startswith("FORMAT")]
            answer = QMessageBox.warning(
                self,
                "Format these partitions?",
                "These partitions will be formatted and everything on them will be lost:\n\n" + "\n".join(lines),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.banner.dismiss()
        self.install_started = True
        self._show_page(INSTALL)
        self.progress.start(self.manifest, self.socket_path, resume=resume)

    def _install_running(self) -> None:
        self.install_started = True
        self._install_succeeded = False
        self._update_navigation()

    def _install_finished(self, success: bool) -> None:
        self._install_succeeded = success
        self._update_navigation()

    def _worker_stopped(self) -> None:
        self.install_started = False
        self._update_navigation()
        if self._close_after_worker:
            QTimer.singleShot(0, self.close)

    def _return_to_settings(self) -> None:
        """After a failed or cancelled run: unlock the wizard and show the review."""
        self.install_started = False
        self._install_succeeded = False
        self._show_page(REVIEW)

    # --- keyboard, settings files, repair ---------------------------------------------------------------------

    def _keyboard_changed(self, layout: str, variant: str, console_keymap: str) -> None:
        system = self.manifest.system
        system.keyboard_layout, system.keyboard_variant, system.keymap = layout, variant, console_keymap

    def import_settings(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import installation settings", "", "JSON files (*.json);;All files (*)")
        if not path:
            return
        try:
            loaded = load_redacted_manifest(path)
        except (ManifestError, OSError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "Cannot import settings", str(exc))
            return
        for name in ("storage", "system", "portage", "network", "accounts", "options", "hardware"):
            setattr(self.manifest, name, getattr(loaded, name))
        self._seed_firmware()
        self.pages[self.current].load_manifest()
        QMessageBox.information(self, "Settings imported", "The settings were imported. Passwords are never saved in a settings file: enter them again on the Accounts page (and the disk passphrase on the Storage page).")

    def open_repair(self) -> None:
        try:
            window = create_repair_window(self.socket_path)
        except Exception as exc:  # noqa: BLE001 - show it instead of closing the installer
            LOG.exception("Repair window failed")
            QMessageBox.critical(self, "Repair an existing system", f"The repair tools could not be started: {exc}")
            return
        if window is None:
            QMessageBox.information(self, "Repair an existing system", REPAIR_MISSING)
            return
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        window.destroyed.connect(self._repair_closed)
        self._repair_window = window
        self.hide()
        window.show()

    def _repair_closed(self) -> None:
        self._repair_window = None
        self.show()

    # --- diagnostics ------------------------------------------------------------------------------------------

    def save_diagnostics(self, reason: str = "") -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save diagnostics", str(Path.home() / diagnostics.default_bundle_name()), "JSON files (*.json)")
        if not path:
            return

        def daemon_document() -> dict[str, Any] | None:
            if os.environ.get("GENTOO_INSTALLER_DEV") == "1":
                return None
            return {"state": self.info.daemon.kind if self.info.daemon else "", "document": daemon_client.fetch_diagnostics(self.socket_path)}

        def collect() -> dict[str, Any]:
            return diagnostics.collect_bundle(
                manifest=self.manifest, info=self.info, socket_path=self.socket_path,
                visible_log=self.progress.visible_text(), reason=reason, daemon_document=daemon_document)

        self.progress.save_diagnostics_to(path, collect)

    def _export_saved(self, ok: bool, message: str) -> None:
        if self.current != INSTALL:
            (QMessageBox.information if ok else QMessageBox.warning)(self, "Diagnostics", message)

    def sync_clock(self, action: str) -> None:
        worker = BackendWorker(None, self.socket_path, self, request={"action": action})
        worker.finished.connect(self.refresh_readiness)
        worker.finished.connect(lambda w=worker: self._workers.remove(w) if w in self._workers else None)
        worker.finished.connect(worker.deleteLater)
        self._workers.append(worker)
        self.readiness.sync_button.setEnabled(False)
        worker.start()

    # --- launchers --------------------------------------------------------------------------------------------

    def launch_application(self, name: str) -> None:
        if not QProcess.startDetached(name, []):
            QMessageBox.information(
                self, "Application unavailable",
                f"{name} could not be started. It is included in the live ISO; in a development environment it may not be installed.\n\nYou can also try it from a terminal: {name}")

    def open_handbook(self) -> None:
        if QDesktopServices.openUrl(QUrl(HANDBOOK_URL)):
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Open the Handbook")
        box.setText(f"No web browser could be started. Open this address on another device:\n\n{HANDBOOK_URL}")
        copy = box.addButton("Copy link", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is copy:
            QGuiApplication.clipboard().setText(HANDBOOK_URL)

    def reboot(self) -> None:
        for program, arguments in (("loginctl", ["reboot"]), ("systemctl", ["reboot"]), ("reboot", [])):
            if QProcess.startDetached(program, arguments):
                return
        QMessageBox.information(self, "Restart", "The computer could not be restarted automatically. Please remove the installation medium and restart it manually.")

    # --- closing -------------------------------------------------------------------------------------------------

    def _confirm_close_during_install(self) -> bool:
        """True when the user wants to cancel the installation and close."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Installation is running")
        box.setText("An installation is running. Closing the installer cancels it at the next safe point; a disk that was already changed stays partially installed.")
        keep = box.addButton("Keep installer open", QMessageBox.ButtonRole.RejectRole)
        stop = box.addButton("Cancel installation and close", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        box.exec()
        return box.clickedButton() is stop

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.install_started:
            if not self._confirm_close_during_install():
                event.ignore()
                return
            self._close_after_worker = True
            self.progress.stop()
            event.ignore()
            return
        for worker in list(self._workers):
            worker.wait(3000)
        self.progress.wait_for_exports()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("draft", json.dumps(blank_secrets(self.manifest.to_dict(redact=True))))
        self.settings.setValue("mode", self.mode.currentData())
        self.settings.sync()
        self._protect_settings_file()
        super().closeEvent(event)

    def _protect_settings_file(self) -> None:
        """The draft holds a private configuration: keep it readable by the user only."""
        try:
            path = Path(self.settings.fileName())
            if path.is_file():
                path.chmod(0o600)
            if path.parent.name == "GentooLive":
                path.parent.chmod(0o700)
        except OSError:
            pass


def show_unexpected_error(text: str, log_file: Path | None) -> None:
    """Last-resort dialog for an unhandled exception in the GUI."""
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle("Unexpected error")
    where = f" The details were saved to {log_file}." if log_file else ""
    box.setText("The installer hit an unexpected problem." + where + " You can continue, but if something looks wrong, restart the installer.")
    box.setDetailedText(text)
    copy = box.addButton("Copy details", QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Close)
    box.exec()
    if box.clickedButton() is copy:
        QGuiApplication.clipboard().setText(text)


def bootstrap_session(manifest: InstallManifest) -> None:
    """Re-apply a keyboard layout that was chosen in an earlier run."""
    system = manifest.system
    if system.keyboard_layout != "us" or system.keyboard_variant:
        keyboard.apply_layout(system.keyboard_layout, system.keyboard_variant, system.keyboard_options, system.keyboard_model)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guided AMD64 Gentoo live installer")
    parser.add_argument("--socket", default=os.environ.get("GENTOO_INSTALLER_SOCKET", DEFAULT_SOCKET))
    parser.add_argument("--manifest", help="Load an installation manifest")
    parser.add_argument("--mode", choices=("install", "repair"), default="install", help="Open the installer wizard or the repair tools")
    args = parser.parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Gentoo Installer")
    app.setOrganizationName("GentooLive")
    app.setDesktopFileName("gentoo-installer")
    apply_theme(app)
    diagnostics.setup_gui_logging()
    diagnostics.install_excepthook(show=show_unexpected_error)
    try:
        if args.manifest:
            manifest = InstallManifest.load(args.manifest)
        else:
            manifest = load_draft()
            if not QSettings("GentooLive", "Installer").value("draft"):
                manifest.portage.make_opts = auto_make_opts()  # a fresh install builds with what this computer can sustain
    except (OSError, ManifestError) as exc:
        QMessageBox.critical(None, "Cannot load installation settings", str(exc))
        return 2
    bootstrap_session(manifest)
    if args.mode == "repair":
        try:
            repair = create_repair_window(args.socket)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("Repair window failed")
            QMessageBox.critical(None, "Repair an existing system", f"The repair tools could not be started: {exc}")
            return 1
        if repair is None:
            QMessageBox.information(None, "Repair an existing system", REPAIR_MISSING)
            return 0
        repair.show()
        return app.exec()
    window = InstallerWindow(manifest, args.socket, firmware_explicit=bool(args.manifest))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
