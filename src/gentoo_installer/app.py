from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QSettings, QTimer, Qt, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
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

from .manifest import SECRET_KEYS, InstallManifest, ManifestError
from .pages.accounts import AccountsPage
from .pages.customize import CustomizePage
from .pages.progress import ProgressPage
from .pages.readiness import ReadinessPage
from .pages.review import ReviewPage
from .pages.storage import StoragePage
from .pages.system import SystemPage
from .pages.welcome import WelcomePage
from .system_info import inspect_system
from .theme import apply_theme


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

MIN_WINDOW_HEIGHT = 640
WINDOW_FRAME_RESERVE = 48


def bounded_window_height(requested: int, available: int) -> int:
    """Keep the client area inside the desktop after title-bar/frame space."""
    maximum = max(MIN_WINDOW_HEIGHT, available - WINDOW_FRAME_RESERVE)
    return max(MIN_WINDOW_HEIGHT, min(requested, maximum))


class InstallerWindow(QMainWindow):
    def __init__(self, manifest: InstallManifest, socket_path: str):
        super().__init__()
        self.manifest = manifest
        self.socket_path = socket_path
        self.settings = QSettings("GentooLive", "Installer")
        self.current = 0
        self.install_started = False
        self._close_after_worker = False
        self.setWindowTitle("Gentoo Installer")
        self.setMinimumSize(940, MIN_WINDOW_HEIGHT)
        self.resize(1180, 790)
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
        self.stack = QStackedWidget()
        content_layout.addWidget(self.stack, 1)
        layout.addWidget(content, 1)

        self.info = inspect_system()
        self.welcome = WelcomePage()
        self.readiness = ReadinessPage()
        self.readiness.update_info(self.info)
        self.storage = StoragePage(self.manifest, self.info)
        self.system = SystemPage(self.manifest)
        self.customize = CustomizePage(self.manifest)
        self.accounts = AccountsPage(self.manifest)
        self.review = ReviewPage(self.manifest)
        self.progress = ProgressPage()
        self.pages = [self.welcome, self.readiness, self.storage, self.system, self.customize, self.accounts, self.review, self.progress]
        for page in self.pages:
            self.stack.addWidget(page)
        self.welcome.tryLiveRequested.connect(self.close)
        self.welcome.applicationRequested.connect(self.launch_application)
        self.welcome.handbookRequested.connect(self.open_handbook)
        self.readiness.refreshRequested.connect(self.refresh_readiness)
        self.readiness.networkSettingsRequested.connect(lambda: QProcess.startDetached("nm-connection-editor", []))
        self.progress.rebootRequested.connect(self.reboot)
        self.progress.finished.connect(self._install_finished)
        self.progress.workerStopped.connect(self._worker_stopped)
        content_layout.addWidget(self._navigation())
        self._show_page(0)
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            self.resize(self.width(), bounded_window_height(self.height(), screen.availableGeometry().height()))

        quit_action = QAction(self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        self.addAction(quit_action)

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
        mode_label = QLabel("Experience")
        mode_label.setProperty("muted", True)
        layout.addWidget(mode_label)
        self.mode = QComboBox()
        self.mode.addItem("Guided", "guided")
        self.mode.addItem("Advanced", "advanced")
        self.mode.setAccessibleName("Installer experience")
        self.mode.currentIndexChanged.connect(self._mode_changed)
        layout.addWidget(self.mode)
        return header

    def _navigation(self) -> QFrame:
        navigation = QFrame()
        navigation.setObjectName("contentHeader")
        layout = QHBoxLayout(navigation)
        layout.setContentsMargins(32, 14, 32, 16)
        self.back = QPushButton("Back")
        self.back.clicked.connect(self.previous_page)
        layout.addWidget(self.back)
        layout.addStretch()
        self.next = QPushButton("Continue")
        self.next.setObjectName("primaryButton")
        self.next.setDefault(True)
        self.next.clicked.connect(self.next_page)
        layout.addWidget(self.next)
        return navigation

    def _show_page(self, index: int) -> None:
        self.current = index
        self.stack.setCurrentIndex(index)
        title, subtitle = STEPS[index]
        self.header_title.setText(title)
        self.header_subtitle.setText(subtitle)
        for position, button in enumerate(self.step_buttons):
            button.setProperty("current", position == index)
            button.setEnabled(position < index and not self.install_started)
            button.style().unpolish(button)
            button.style().polish(button)
        self.back.setVisible(index > 0 and index < 7 and not self.install_started)
        self.next.setVisible(index < 7)
        self.next.setText("Start installation" if index == 6 else "Continue")
        self.next.setObjectName("dangerButton" if index == 6 else "primaryButton")
        self.next.style().unpolish(self.next)
        self.next.style().polish(self.next)
        self.mode.setEnabled(index < 7 and not self.install_started)
        self.pages[index].load_manifest()
        self.pages[index].setFocus()

    def go_to(self, index: int) -> None:
        if self.install_started or index >= self.current:
            return
        self.pages[self.current].save_manifest()
        self._show_page(index)

    def next_page(self) -> None:
        valid, message = self.pages[self.current].validate_page()
        if not valid:
            QMessageBox.warning(self, "Check this step", message)
            return
        self.pages[self.current].save_manifest()
        if self.current == 6:
            self.start_installation()
            return
        if self.current + 1 == 6:
            for page in self.pages[:6]:
                page.save_manifest()
            errors = self.manifest.validate()
            if errors:
                QMessageBox.warning(self, "Installation settings", errors[0])
                return
        self._show_page(self.current + 1)

    def previous_page(self) -> None:
        if self.current > 0 and not self.install_started:
            self.pages[self.current].save_manifest()
            self._show_page(self.current - 1)

    def _mode_changed(self) -> None:
        advanced = self.mode.currentData() == "advanced"
        self.storage.encryption.setVisible(advanced or self.storage.luks.isChecked())
        self.storage.mode.setVisible(advanced or self.storage.mode.currentData() == "manual")
        if not advanced:
            self.header_subtitle.setText(STEPS[self.current][1] + " · Recommended choices are selected for you")
        else:
            self.header_subtitle.setText(STEPS[self.current][1] + " · All available controls are shown")

    def refresh_readiness(self) -> None:
        self.info = inspect_system()
        self.readiness.update_info(self.info)

    def start_installation(self) -> None:
        storage = self.manifest.storage
        if storage.mode == "erase":
            answer = QMessageBox.warning(
                self,
                "Erase the selected disk?",
                f"This will permanently erase all data on {storage.device}.\n\nNo other disk is selected. Do you want to continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.install_started = True
        self._show_page(7)
        self.progress.start(self.manifest, self.socket_path)

    def _install_finished(self, success: bool) -> None:
        self.back.setVisible(False)
        self.next.setVisible(False)

    def _worker_stopped(self) -> None:
        self.install_started = False
        self.back.setVisible(False)
        self.next.setVisible(False)
        if self._close_after_worker:
            QTimer.singleShot(0, self.close)

    def launch_application(self, name: str) -> None:
        if not QProcess.startDetached(name, []):
            QMessageBox.information(self, "Application unavailable", f"{name} is not available in this development environment. It is included in the live ISO.")

    def open_handbook(self) -> None:
        QDesktopServices.openUrl(QUrl("https://wiki.gentoo.org/wiki/Handbook:AMD64/Full/Installation"))

    def reboot(self) -> None:
        if not QProcess.startDetached("systemctl", ["reboot"]):
            QProcess.startDetached("loginctl", ["reboot"])

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.install_started:
            answer = QMessageBox.question(self, "Installation is running", "Keep the installer window open to monitor the installation? Choose No to request cancellation and close after the backend stops.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._close_after_worker = True
            self.progress.stop()
            event.ignore()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        raw = self.manifest.to_dict(redact=True)
        def blank_secrets(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    value[key] = "" if key in SECRET_KEYS else blank_secrets(item)
            elif isinstance(value, list):
                return [blank_secrets(item) for item in value]
            return value
        self.settings.setValue("draft", json.dumps(blank_secrets(raw)))
        self.settings.setValue("mode", self.mode.currentData())
        super().closeEvent(event)


def load_draft() -> InstallManifest:
    settings = QSettings("GentooLive", "Installer")
    text = settings.value("draft", "")
    if not text:
        return InstallManifest()
    try:
        return InstallManifest.from_dict(json.loads(text))
    except (json.JSONDecodeError, ManifestError):
        return InstallManifest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guided AMD64 Gentoo live installer")
    parser.add_argument("--socket", default=os.environ.get("GENTOO_INSTALLER_SOCKET", "/run/gentoo-installer/server.sock"))
    parser.add_argument("--manifest", help="Load an installation manifest")
    args = parser.parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Gentoo Installer")
    app.setOrganizationName("GentooLive")
    app.setDesktopFileName("gentoo-installer")
    apply_theme(app)
    try:
        manifest = InstallManifest.load(args.manifest) if args.manifest else load_draft()
    except (OSError, ManifestError) as exc:
        QMessageBox.critical(None, "Cannot load installation settings", str(exc))
        return 2
    window = InstallerWindow(manifest, args.socket)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
