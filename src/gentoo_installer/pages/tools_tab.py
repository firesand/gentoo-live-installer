"""ACCT: the Applications && tools tab of the Customize page (system tools, applications, KDE applications, extra packages, gaming)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

from .. import account_text as text
from .. import recommend, system_info
from ..manifest import InstallManifest
from ..widgets import Card, wrapped_label
from .help import AdvancedSection, HelpNote

if TYPE_CHECKING:
    from .hardware import HardwareTab

COMPANIONS = ("install_hwmonux", "install_portagehub")
AUTO_PLACEHOLDER = "Leave empty: chosen for this computer"
# HWMonux needs PySide6, and the binary host has no matching PySide6 package, so it is compiled on every installation.
# The text installer says the same (textui/summary.py); a test keeps the two sentences equal.
HWMONUX_LOW_MEMORY = "HWMonux needs PySide6, which is compiled from source: about 40 minutes and up to 4 GiB of memory per compile job."
HWMONUX_MEMORY_GIB = 8  # a computer with less memory than this gets the note
HWMONUX_ADVICE = "rec.hwmonux_low_memory"  # the same advice from recommend.recommend(), when it gives one
LOGGERS = (("sysklogd", "sysklogd (small and simple)"), ("syslog-ng", "syslog-ng (with log rotation)"), ("metalog", "metalog"), ("none", "No system logger"))
CRONS = (("cronie", "cronie (the Handbook's default)"), ("dcron", "dcron"), ("fcron", "fcron (OpenRC only)"), ("none", "No cron daemon"))
TIMESYNCS = (("chrony", "chrony (recommended)"), ("timesyncd", "systemd-timesyncd (systemd only)"), ("none", "Do not keep the clock correct"))
GAMING_CHOICES = (("off", "No gaming setup"), ("flatpak", "Quick: Steam and Heroic from Flathub"), ("native", "Native Gentoo: Steam and 32-bit libraries built here"))
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


def _choice_combo(choices: tuple[tuple[str, str], ...], name: str) -> QComboBox:
    combo = QComboBox()
    for value, label in choices:
        combo.addItem(label, value)
    combo.setAccessibleName(name)
    return combo


def _with_help(widget: QWidget, note: str, anchor: str) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(widget, 1)
    layout.addWidget(HelpNote(note, anchor))
    return row


def _select(combo: QComboBox, value: str) -> None:
    """Show a value; one that this installer does not offer is added and flagged, never replaced."""
    index = combo.findData(value)
    if index < 0:
        combo.addItem(f"{value} (custom value)", value)
        index = combo.count() - 1
    combo.setCurrentIndex(index)


class ToolsTabMixin:
    """The tab's widgets (checks, kde_apps, extra_packages, ...) are attributes of the page that mixes this in."""

    if TYPE_CHECKING:
        manifest: InstallManifest
        hardware: HardwareTab
        _cleared_companions: tuple[bool, bool] | None

    def _extras_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.checks: dict[str, QCheckBox] = {}
        layout.addWidget(self._system_tools_card(page))
        card = Card("Applications", parent=page)
        layout.addWidget(card)
        grid = QGridLayout()
        entries = [
            ("install_hwmonux", "HWMonux hardware monitor"),
            ("install_portagehub", "PortageHub package manager"),
            ("pipewire", "PipeWire audio"),
            ("bluetooth", "Bluetooth support"),
            ("printing", "Printing with CUPS"),
            ("flatpak", "Flatpak application support"),
            ("firmware", "Linux firmware"),
            ("microcode", "CPU microcode updates"),
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
        layout.addWidget(self._gaming_card(page))
        layout.addStretch(1)
        return page

    # --- system tools (the Handbook's Tools chapter) -----------------------------------------------------------

    def _system_tools_card(self, parent: QWidget) -> Card:
        card = Card("System tools (Handbook)", "Small programs every Gentoo system needs: the clock, the log and scheduled jobs. The defaults are right for most computers.", parent=parent)
        form = QFormLayout()
        self.timesync = _choice_combo(TIMESYNCS, "Time synchronisation")
        form.addRow(text.TIMESYNC_TEXT, _with_help(self.timesync, "Sets the clock from the internet now and then, so the time stays right.", "Tools"))
        card.content.addLayout(form)
        completion = QCheckBox("Bash completion")
        completion.setToolTip(text.COMPLETION_HELP)
        self.checks["shell_completion"] = completion
        card.content.addWidget(completion)
        card.content.addWidget(wrapped_label(text.COMPLETION_HELP, muted=True))
        self.tools_more = AdvancedSection("tools", "More system tools")
        more = QFormLayout()
        self.logger = _choice_combo(LOGGERS, "System logger")
        self.logger_label = QLabel("System logger")
        more.addRow(self.logger_label, _with_help(self.logger, text.LOGGER_HELP, "Tools"))
        self.cron = _choice_combo(CRONS, "Cron daemon")
        more.addRow("Scheduled jobs (cron)", _with_help(self.cron, text.CRON_HELP, "Tools"))
        self.tools_more.add_layout(more)
        self.journal_note = wrapped_label(text.JOURNAL_NOTE, muted=True)
        self.tools_more.add_widget(self.journal_note)
        journal = QCheckBox(text.JOURNAL_PERSISTENT_TEXT)
        journal.setToolTip(text.JOURNAL_PERSISTENT_HELP)
        self.checks["journal_persistent"] = journal
        self.tools_more.add_widget(journal)
        self.cron_note = wrapped_label("", muted=True)
        self.cron_note.hide()
        self.tools_more.add_widget(self.cron_note)
        for key, label, tip in (("mlocate", "File search with locate (mlocate)", text.MLOCATE_HELP),
                                ("gentoolkit", "Gentoo toolkit (gentoolkit)", text.GENTOOLKIT_HELP),
                                ("eclean_kernel", "Old kernel cleaner (eclean-kernel)", "Removes kernels you no longer use, with their modules.")):
            check = QCheckBox(label)
            check.setToolTip(tip)
            self.checks[key] = check
            self.tools_more.add_widget(check)
        card.content.addWidget(self.tools_more)
        self.logger.currentIndexChanged.connect(lambda _=0: self._update_tool_notes())
        self.cron.currentIndexChanged.connect(lambda _=0: self._update_tool_notes())
        return card

    def _update_tool_notes(self) -> None:
        """Hide what the chosen init system does not use, and say when one choice brings another along."""
        systemd = self.manifest.system.init == "systemd"
        self.logger.parentWidget().setVisible(not systemd)
        self.logger_label.setVisible(not systemd)
        self.journal_note.setVisible(systemd)
        self.checks["journal_persistent"].setVisible(systemd)
        for combo, value, usable in ((self.cron, "fcron", not systemd), (self.timesync, "timesyncd", systemd)):
            index, model = combo.findData(value), combo.model()
            item = model.item(index) if index >= 0 and hasattr(model, "item") else None
            if item is not None:
                item.setEnabled(usable)
        self.cron.setToolTip(text.CRON_SYSTEMD_HELP if systemd else text.CRON_HELP)
        cron, logger = str(self.cron.currentData()), str(self.logger.currentData())
        note = ""
        if not systemd and logger == "syslog-ng" and cron == "none":
            note = "syslog-ng rotates its logs from cron, so cronie is installed as well. Choose another logger to avoid that."
        elif systemd and cron != "none":
            note = text.CRON_SYSTEMD_HELP + " Choose No cron daemon unless a program of yours needs one."
        self.cron_note.setText(note)
        self.cron_note.setVisible(bool(note))

    def set_tools_advanced(self, advanced: bool) -> None:
        self.tools_more.set_advanced(advanced)

    # --- gaming (optional) -------------------------------------------------------------------------------------------

    def _gaming_card(self, parent: QWidget) -> Card:
        from gentoo_installerd import gaming_readiness as gaming  # lazy: the wording lives next to the readiness check

        card = Card("Gaming (optional)", gaming.CARD_INTRO, parent=parent)
        self.gaming_group = QButtonGroup(card)
        self.gaming_buttons: dict[str, QRadioButton] = {}
        for value, label in GAMING_CHOICES:
            button = QRadioButton(label)
            button.setToolTip(gaming.GAMING_MODES_TEXT[value])
            self.gaming_buttons[value] = button
            self.gaming_group.addButton(button)
            card.content.addWidget(button)
            button.toggled.connect(lambda _=False: self._gaming_changed())
        self.gaming_note = wrapped_label("", muted=True)
        self.gaming_note.hide()
        card.content.addWidget(self.gaming_note)
        self.gaming_tweaks = QCheckBox("Game tweaks: more memory maps, NTSYNC and GameMode")
        self.gaming_windows = QCheckBox("Windows game tools: Heroic, Wine, DXVK, VKD3D-Proton, protontricks")
        self.gaming_accept = QCheckBox(gaming.ACCEPT_EXTERNAL_TEXT)
        for box in (self.gaming_tweaks, self.gaming_windows, self.gaming_accept):
            card.content.addWidget(box)
        self.gaming_build_note = wrapped_label(gaming.BUILD_TIME_NOTE, muted=True)
        card.content.addWidget(self.gaming_build_note)
        self.gaming_status = wrapped_label(gaming.FORTNITE_STATUS, muted=True)
        self.gaming_status.setObjectName("fortniteStatus")
        card.content.addWidget(self.gaming_status)
        self.gaming_check = QPushButton(gaming.CHECK_BUTTON)
        self.gaming_check.clicked.connect(lambda _=False: self.run_gaming_check())
        card.content.addWidget(self.gaming_check)
        self.gaming_report = wrapped_label("", muted=True)
        self.gaming_report.hide()
        card.content.addWidget(self.gaming_report)
        return card

    def run_gaming_check(self, root: str = "/") -> None:
        """The read-only list: it reads a few files, installs nothing and changes nothing."""
        from gentoo_installerd import gaming_readiness as gaming

        rows = gaming.check_this_pc(root, live=True)
        self.gaming_report.setText(gaming.format_rows(rows) + "\n\n" + gaming.LIVE_NOTE)
        self.gaming_report.show()

    def _gaming_mode(self) -> str:
        return next((value for value, button in self.gaming_buttons.items() if button.isChecked()), "off")

    def _gaming_changed(self) -> None:
        mode = self._gaming_mode()
        self.gaming_tweaks.setVisible(mode != "off")
        self.gaming_windows.setVisible(mode != "off")
        self.gaming_accept.setVisible(mode == "native")
        self.gaming_build_note.setVisible(mode == "native")

    def refresh_gaming(self) -> None:
        """Gaming needs a desktop; native also needs a multilib stage3. A choice that no longer fits is turned off or moved to Flatpak, with a notice."""
        from gentoo_installerd import gaming_readiness as gaming

        system, options = self.manifest.system, self.manifest.options
        no_desktop = system.desktop == "minimal"
        no_multilib = not system.multilib or "nomultilib" in system.stage_variant
        note = ""
        if no_desktop and options.gaming != "off":
            options.gaming, note = "off", "Gaming was turned off because a minimal system has no desktop."
        elif no_multilib and options.gaming == "native":
            options.gaming, note = "flatpak", "Native gaming was changed to the quick Flatpak mode because this stage3 has no 32-bit support."
        note = note or (gaming.NEEDS_DESKTOP if no_desktop else gaming.NATIVE_NEEDS_MULTILIB if no_multilib else "")
        self.gaming_note.setText(note)
        self.gaming_note.setVisible(bool(note))
        for value, button in self.gaming_buttons.items():
            button.setEnabled(not no_desktop or value == "off")
        self.gaming_buttons["native"].setEnabled(not no_desktop and not no_multilib)
        for box in (self.gaming_tweaks, self.gaming_windows, self.gaming_accept):
            box.setEnabled(not no_desktop)
        self.gaming_buttons[options.gaming if options.gaming in self.gaming_buttons else "off"].setChecked(True)
        self._gaming_changed()

    def showEvent(self, event) -> None:
        self.refresh_gaming()
        self._update_tool_notes()
        super().showEvent(event)  # type: ignore[misc]

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

    # --- reacting to the desktop choice ------------------------------------------------------------------------------

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
        self.refresh_gaming()

    def _load_checks(self) -> None:
        options = self.manifest.options
        for key, check in self.checks.items():
            check.setChecked(options.file_indexing or options.mlocate if key == "mlocate" else options.is_on(key))
        minimal = self.manifest.system.desktop == "minimal"
        self.companion_note.setVisible(minimal and self._cleared_companions is not None)
        self._update_hwmonux_note()

    def _load_system_tools(self) -> None:
        options = self.manifest.options
        _select(self.timesync, options.timesync)
        _select(self.logger, options.logger)
        _select(self.cron, options.cron)
        self._update_tool_notes()

    def _load_gaming(self) -> None:
        options = self.manifest.options
        self.gaming_tweaks.setChecked(options.gaming_tweaks)
        self.gaming_windows.setChecked(options.gaming_windows_tools)
        self.gaming_accept.setChecked(options.gaming_accept_external)
        self.refresh_gaming()

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
        self._load_system_tools()
        self._load_kde_apps()
        self._load_gaming()
        self.extra_packages.setText(" ".join(self.manifest.options.extra_packages))

    def save_tools(self) -> None:
        options = self.manifest.options
        for key, check in self.checks.items():
            if key == "mlocate":  # the older file_indexing setting means the same package
                if check.isChecked() != (options.file_indexing or options.mlocate):
                    options.mlocate = options.file_indexing = check.isChecked()
            elif check.isChecked() != options.is_on(key):  # a chosen value stays unless the box is switched
                setattr(options, key, check.isChecked())
        options.timesync = str(self.timesync.currentData() or options.timesync)
        options.logger = str(self.logger.currentData() or options.logger)
        options.cron = str(self.cron.currentData() or options.cron)
        options.kde_apps = str(self.kde_apps.currentData() or options.kde_apps)
        options.extra_packages = self.extra_packages.text().split()
        options.gaming = self._gaming_mode()
        options.gaming_tweaks = self.gaming_tweaks.isChecked()
        options.gaming_windows_tools = self.gaming_windows.isChecked()
        options.gaming_accept_external = self.gaming_accept.isChecked()
