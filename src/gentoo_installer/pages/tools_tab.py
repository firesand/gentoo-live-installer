"""ACCT: the Applications && tools tab of the Customize page (companion apps, services, KDE applications, extra packages)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QGridLayout, QLineEdit, QVBoxLayout, QWidget

from .. import recommend, system_info
from ..manifest import InstallManifest
from ..widgets import Card, wrapped_label

if TYPE_CHECKING:
    from .hardware import HardwareTab

COMPANIONS = ("install_hwmonux", "install_portagehub")
AUTO_PLACEHOLDER = "Leave empty: chosen for this computer"
# HWMonux needs PySide6, and the binary host has no matching PySide6 package, so it is compiled on every installation.
# The text installer says the same (textui/summary.py); a test keeps the two sentences equal.
HWMONUX_LOW_MEMORY = "HWMonux needs PySide6, which is compiled from source: about 40 minutes and up to 4 GiB of memory per compile job."
HWMONUX_MEMORY_GIB = 8  # a computer with less memory than this gets the note
HWMONUX_ADVICE = "rec.hwmonux_low_memory"  # the same advice from recommend.recommend(), when it gives one
KDE_APPS = (
    ("core", "Core: file manager, terminal, editor and help"),
    ("full", "Full: all KDE applications (games, education, multimedia; a long build)"),
)


def hwmonux_memory_note(info: system_info.SystemInfo | None, manifest: InstallManifest) -> str:
    """What the PySide6 build of HWMonux costs on a computer with little memory ('' when there is enough, or nothing is known).

    The advice of recommend.recommend() is used when it gives one; otherwise the memory of this computer decides.
    It only informs: nothing here unticks HWMonux."""
    if info is None:
        return ""
    if info.hardware:
        try:
            for item in recommend.recommend(info.profile, manifest):
                if item.code == HWMONUX_ADVICE:
                    return item.message
        except Exception:  # noqa: BLE001 - advice only
            pass
    return HWMONUX_LOW_MEMORY if 0 < info.memory_gib < HWMONUX_MEMORY_GIB else ""



class ToolsTabMixin:
    """The tab's widgets (checks, kde_apps, extra_packages, ...) are attributes of the page that mixes this in."""

    if TYPE_CHECKING:
        manifest: InstallManifest
        hardware: HardwareTab
        _cleared_companions: tuple[bool, bool] | None

    def _extras_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        card = Card("Applications and system services", parent=page)
        layout.addWidget(card)
        grid = QGridLayout()
        self.checks: dict[str, QCheckBox] = {}
        entries = [
            ("install_hwmonux", "HWMonux hardware monitor"),
            ("install_portagehub", "PortageHub package manager"),
            ("pipewire", "PipeWire audio"),
            ("bluetooth", "Bluetooth support"),
            ("printing", "Printing with CUPS"),
            ("flatpak", "Flatpak application support"),
            ("firmware", "Linux firmware"),
            ("microcode", "CPU microcode updates"),
            ("chrony", "Network time synchronization"),
            ("cron", "Cron scheduled tasks"),
            ("logger", "System logger for OpenRC"),
            ("ssh", "OpenSSH remote access"),
            ("file_indexing", "File indexing with mlocate"),
            ("shell_completion", "Bash completion"),
        ]
        for index, (key, label) in enumerate(entries):
            check = QCheckBox(label)
            self.checks[key] = check
            grid.addWidget(check, index // 2 + (1 if index >= 2 else 0), index % 2)  # row 1 is for the note under HWMonux
        self.hwmonux_note = wrapped_label("", muted=True)
        self.hwmonux_note.hide()
        grid.addWidget(self.hwmonux_note, 1, 0, 1, 2)
        self.checks["install_hwmonux"].toggled.connect(lambda _=False: self._update_hwmonux_note())
        card.content.addLayout(grid)
        self.companion_note = wrapped_label(
            "HWMonux and PortageHub are graphical applications; they only start inside a desktop session. They are switched off for the minimal system, tick them again if you plan to add a desktop later.",
            muted=True,
        )
        self.companion_note.hide()
        card.content.addWidget(self.companion_note)
        self.hardware_note = wrapped_label("", muted=True)
        self.hardware_note.hide()
        card.content.addWidget(self.hardware_note)
        apps_form = QFormLayout()
        self.kde_apps = QComboBox()
        for value, label in KDE_APPS:
            self.kde_apps.addItem(label, value)
        self.kde_apps.setAccessibleName("KDE applications")
        apps_form.addRow("KDE applications", self.kde_apps)
        card.content.addLayout(apps_form)
        self.extra_packages = QLineEdit()
        self.extra_packages.setPlaceholderText("category/package category/other-package")
        self.extra_packages.setAccessibleName("Extra packages")
        card.content.addWidget(self.extra_packages)
        layout.addStretch(1)
        return page

    def _update_hardware_note(self, info: system_info.SystemInfo | None, scalars: dict[str, Any]) -> None:
        """Firmware and microcode can be decided by the hardware: say so next to the two tick boxes."""
        lines = []
        reasons = scalars.get("firmware_reasons") or []
        if scalars.get("firmware_forced") and reasons:
            lines.append("Linux firmware is installed even though it is not ticked, because this computer needs it: " + ", ".join(reasons) + ".")
        if info is not None and info.hardware and info.profile.machine.hypervisor and self.manifest.options.microcode:
            lines.append("CPU microcode does nothing in a virtual machine: the host owns it.")
        elif scalars.get("microcode") == "both":
            lines.append("Microcode for both Intel and AMD processors is installed, because it is not known which processor will run this system.")
        self.hardware_note.setText("\n".join(lines))
        self.hardware_note.setVisible(bool(lines))

    # --- reacting to the desktop choice ------------------------------------------------------------------

    def desktop_changed(self, desktop: str) -> None:
        """The user picked a desktop on the System page. A minimal system has no
        display for the graphical companion apps, so they start unticked (and are
        restored when a desktop is chosen again); the user can tick them anyway."""
        options = self.manifest.options
        self.manifest.system.desktop = desktop
        if desktop == "minimal":
            if options.install_hwmonux or options.install_portagehub:
                self._cleared_companions = (options.install_hwmonux, options.install_portagehub)
                options.install_hwmonux = options.install_portagehub = False
        elif self._cleared_companions is not None:
            options.install_hwmonux, options.install_portagehub = self._cleared_companions
            self._cleared_companions = None
        self._load_checks()
        self._load_kde_apps()

    def _load_checks(self) -> None:
        for key, check in self.checks.items():
            check.setChecked(getattr(self.manifest.options, key))
        minimal = self.manifest.system.desktop == "minimal"
        self.companion_note.setVisible(minimal and self._cleared_companions is not None)
        self._update_hwmonux_note()

    def _update_hwmonux_note(self) -> None:
        """Say what the PySide6 build costs under the HWMonux box while it is ticked. The box is the user's choice: it is never changed here."""
        box = self.checks["install_hwmonux"]
        note = hwmonux_memory_note(self.hardware.info, self.manifest)
        shown = bool(note) and box.isChecked()
        self.hwmonux_note.setText(note)
        self.hwmonux_note.setVisible(shown)
        box.setToolTip(note if shown else "")

    def _load_kde_apps(self) -> None:
        value = self.manifest.options.kde_apps
        index = self.kde_apps.findData(value)
        if index < 0:  # a loaded value this installer does not offer is shown, not replaced
            self.kde_apps.addItem(f"{value} (custom value)", value)
            index = self.kde_apps.count() - 1
        self.kde_apps.setCurrentIndex(index)
        kde = self.manifest.system.desktop == "kde"
        self.kde_apps.setEnabled(kde)
        self.kde_apps.setToolTip("" if kde else "The KDE application set only matters when KDE Plasma is the desktop.")

    def load_tools(self) -> None:
        self._load_checks()
        self._load_kde_apps()
        self.extra_packages.setText(" ".join(self.manifest.options.extra_packages))

    def save_tools(self) -> None:
        for key, check in self.checks.items():
            setattr(self.manifest.options, key, check.isChecked())
        self.manifest.options.kde_apps = str(self.kde_apps.currentData() or self.manifest.options.kde_apps)
        self.manifest.options.extra_packages = self.extra_packages.text().split()
