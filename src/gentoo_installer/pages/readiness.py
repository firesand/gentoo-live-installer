from __future__ import annotations

import platform

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton

from ..system_info import SystemInfo, format_size
from ..widgets import Card, Page, StatusRow


class ReadinessPage(Page):
    refreshRequested = Signal()
    networkSettingsRequested = Signal()

    def __init__(self):
        super().__init__("A quick compatibility check", "We’ll confirm that the live environment is ready. You can continue with warnings, but errors should be resolved first.")
        self.card = Card()
        self.rows: dict[str, StatusRow] = {}
        for key, title in (
            ("architecture", "AMD64 architecture"),
            ("memory", "Memory"),
            ("firmware", "Boot firmware"),
            ("disk", "Installation disks"),
            ("network", "Internet connection"),
            ("clock", "System clock"),
            ("tools", "Required system tools"),
        ):
            row = StatusRow(title, "Checking…")
            self.rows[key] = row
            self.card.content.addWidget(row)
        self.content.addWidget(self.card)
        actions = QHBoxLayout()
        refresh = QPushButton("Check again")
        refresh.clicked.connect(self.refreshRequested)
        actions.addWidget(refresh)
        network = QPushButton("Network settings")
        network.clicked.connect(self.networkSettingsRequested)
        actions.addWidget(network)
        actions.addStretch()
        self.content.addLayout(actions)
        note = QLabel("An internet connection is required to download and verify the Gentoo stage3 archive and synchronize Portage.")
        note.setWordWrap(True)
        note.setProperty("muted", True)
        self.content.addWidget(note)

    def update_info(self, info: SystemInfo) -> None:
        arch_ok = info.architecture in {"x86_64", "amd64"}
        self.rows["architecture"].set_status("ok" if arch_ok else "error", f"Detected {info.architecture}")
        self.rows["memory"].set_status("ok" if info.memory_gib >= 2 else "warning", f"{info.memory_gib:.1f} GiB available · 2 GiB or more recommended")
        self.rows["firmware"].set_status("ok", "UEFI" if info.uefi else "Legacy BIOS")
        usable = [disk for disk in info.disks if disk.size >= 16 * 1024**3 and not disk.read_only]
        detail = ", ".join(f"{disk.path} ({disk.size_text})" for disk in usable) or "No suitable disk found"
        self.rows["disk"].set_status("ok" if usable else "error", detail)
        self.rows["network"].set_status("ok" if info.network else "error", "Gentoo mirror is reachable" if info.network else "Connect to a network, then check again")
        self.rows["clock"].set_status("ok" if info.synchronized_clock else "warning", "Synchronized" if info.synchronized_clock else "Synchronization was not confirmed. Check the system time if download verification fails.")
        self.rows["tools"].set_status("ok" if not info.missing_tools else "error", "All required tools are available" if not info.missing_tools else "Missing: " + ", ".join(info.missing_tools))

    def validate_page(self) -> tuple[bool, str]:
        if self.rows["architecture"].accessibleDescription().startswith("error"):
            return False, "This installer supports AMD64/x86-64 systems only."
        return True, ""
