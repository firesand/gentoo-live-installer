from __future__ import annotations

import os
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import recommend, system_info
from ..manifest import InstallManifest
from ..widgets import Card, Page, wrapped_label
from .hardware import HardwareTab, hardware_plan
from .readiness import proxy_problem

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


def auto_jobs() -> int:
    """Parallel build jobs this computer can sustain: one per processor, but
    not more than one per 2 GiB of memory (big C++ and Rust builds)."""
    cpus = os.cpu_count() or 2
    return max(1, min(cpus, int(system_info._memory_gib() // 2) or 1))


def auto_make_opts() -> str:
    return f"-j{auto_jobs()} -l{os.cpu_count() or 2}"


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


def parse_jobs(make_opts: str) -> int | None:
    try:
        return int(make_opts.strip().removeprefix("-j").split()[0])
    except (ValueError, IndexError):
        return None


class CustomizePage(Page):
    def __init__(self, manifest: InstallManifest, info: system_info.SystemInfo | None = None):
        super().__init__("Make it yours", "Safe defaults are already selected. The Hardware tab shows what was found on this computer; the other tabs use the same Portage and network configuration files described in the Handbook.")
        self.manifest = manifest
        self._loaded_make_opts = ""
        self._cleared_companions: tuple[bool, bool] | None = None
        tabs = QTabWidget()
        self.hardware = HardwareTab(manifest, info)
        tabs.addTab(self.hardware, "Hardware")
        tabs.addTab(self._portage_tab(), "Portage")
        tabs.addTab(self._network_tab(), "Network")
        tabs.addTab(self._extras_tab(), "Applications && tools")
        self.tabs = tabs
        self.content.addWidget(tabs)
        self.hardware.changed.connect(self._hardware_changed)
        self.load_manifest()
        self.finish_layout()

    def _portage_tab(self) -> QWidget:
        page = QWidget()
        card = Card(parent=page)
        layout = QVBoxLayout(page)
        layout.addWidget(card)
        form = QFormLayout()
        self.mirror = QLineEdit()
        form.addRow("HTTPS download mirror", self.mirror)
        self.binhost = QCheckBox("Prefer official Gentoo binary packages when available")
        form.addRow(self.binhost)
        self.binhost_url = QLineEdit()
        form.addRow("Binary package host", self.binhost_url)
        jobs_row = QHBoxLayout()
        self.jobs = QSpinBox()
        self.jobs.setRange(1, 256)
        self.jobs.setValue(auto_jobs())
        self.jobs_auto = QCheckBox()
        self.jobs_auto.setText(f"Automatic ({auto_jobs()} jobs for {os.cpu_count() or 2} processors)")
        self.jobs_auto.setToolTip("Chosen from this computer's processors and memory so that builds neither crawl nor run out of memory.")
        self.jobs_auto.toggled.connect(lambda checked: self.jobs.setEnabled(not checked))
        jobs_row.addWidget(self.jobs_auto)
        jobs_row.addWidget(self.jobs)
        jobs_row.addStretch()
        form.addRow("Parallel build jobs", jobs_row)
        self.cflags = QLineEdit()
        self.cflags.setToolTip("Used for C, C++ and Fortran. -march=native can optimize for this computer, but the programs may not run on a different CPU.")
        form.addRow("COMMON_FLAGS", self.cflags)
        self.cxxflags = QLineEdit()
        self.cxxflags.setToolTip("Normally ${CFLAGS}, which follows COMMON_FLAGS.")
        form.addRow("CXXFLAGS", self.cxxflags)
        self.rustflags = QLineEdit()
        form.addRow("RUSTFLAGS", self.rustflags)
        self.emerge_opts = QLineEdit()
        form.addRow("EMERGE_DEFAULT_OPTS", self.emerge_opts)
        self.features = QLineEdit()
        self.features.setPlaceholderText("ccache getbinpkg (space separated)")
        form.addRow("FEATURES", self.features)
        self.licenses = QLineEdit()
        form.addRow("ACCEPT_LICENSE", self.licenses)
        self.use_flags = QPlainTextEdit()
        self.use_flags.setPlaceholderText("One global USE flag per line; prefix with - to disable")
        self.use_flags.setMaximumHeight(85)
        self.use_flags.setTabChangesFocus(True)
        form.addRow("Global USE flags", self.use_flags)
        self.package_use = QPlainTextEdit()
        self.package_use.setPlaceholderText("category/package flag -flag, one entry per line")
        self.package_use.setMaximumHeight(85)
        self.package_use.setTabChangesFocus(True)
        form.addRow("Per-package USE flags", self.package_use)
        self.cpu_flags = QLineEdit()
        self.cpu_flags.setPlaceholderText(AUTO_PLACEHOLDER)
        form.addRow("CPU_FLAGS_X86", self.cpu_flags)
        self.video_cards = QLineEdit()
        self.video_cards.setPlaceholderText(AUTO_PLACEHOLDER)
        form.addRow("VIDEO_CARDS", self.video_cards)
        card.content.addLayout(form)
        layout.addStretch(1)
        return page

    def _network_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        card = Card(parent=page)
        layout.addWidget(card)
        form = QFormLayout()
        self.network_manager = QComboBox()
        for label, value in (("NetworkManager — recommended for desktops", "networkmanager"), ("dhcpcd — simple DHCP", "dhcpcd"), ("netifrc — OpenRC network scripts", "netifrc"), ("systemd-networkd — systemd only", "systemd-networkd")):
            self.network_manager.addItem(label, value)
        self.network_manager.currentIndexChanged.connect(self._network_manager_changed)
        self.network_manager.activated.connect(self._manager_picked)
        form.addRow("Network management", self.network_manager)
        self.interface = QLineEdit()
        self.interface.setPlaceholderText("enp1s0 (needed by netifrc, and for a static address with dhcpcd)")
        self.interface.setToolTip("The name of the network adapter. Network interface names differ between computers.")
        form.addRow("Network interface", self.interface)
        self.dhcp = QCheckBox("Configure the network automatically with DHCP")
        self.dhcp.toggled.connect(self._network_changed)
        form.addRow(self.dhcp)
        self.address = QLineEdit()
        self.address.setPlaceholderText("192.168.1.50/24")
        form.addRow("Static address and prefix", self.address)
        self.gateway = QLineEdit()
        self.gateway.setPlaceholderText("192.168.1.1")
        form.addRow("Default gateway", self.gateway)
        self.dns = QLineEdit()
        self.dns.setPlaceholderText("1.1.1.1 9.9.9.9")
        form.addRow("DNS servers", self.dns)
        self.proxy = QLineEdit()
        self.proxy.setPlaceholderText("http://proxy.example:3128 (optional)")
        self.proxy.setToolTip("Used for downloads during installation, not stored on the installed system.")
        form.addRow("Download proxy", self.proxy)
        self.wifi_ssid = QLineEdit()
        form.addRow("Wi-Fi network (optional)", self.wifi_ssid)
        self.wifi_password = QLineEdit()
        self.wifi_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Wi-Fi password", self.wifi_password)
        self.import_profiles = QCheckBox("Use the Wi-Fi networks saved in this live session")
        self.import_profiles.setToolTip("Copies the saved network connections, including their passwords, to the installed system.")
        self.import_profiles.toggled.connect(lambda _=False: self._update_network_notes())
        form.addRow(self.import_profiles)
        card.content.addLayout(form)
        self.network_note = wrapped_label("", muted=True)
        self.network_note.hide()
        card.content.addWidget(self.network_note)
        self.import_warning = wrapped_label("", selectable=True)
        self.import_warning.setStyleSheet("color: #b87818;")
        self.import_warning.hide()
        card.content.addWidget(self.import_warning)
        layout.addStretch(1)
        return page

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

    # --- the hardware tab ----------------------------------------------------------------------------------

    def set_info(self, info: system_info.SystemInfo) -> None:
        self.hardware.set_info(info)
        self.refresh_placeholders()
        self._update_network_notes()
        self._update_hwmonux_note()
        names = sorted({nic.iface for nic in info.profile.nics if nic.iface})
        if names:
            self.interface.setCompleter(QCompleter(names, self.interface))

    def set_advanced(self, advanced: bool) -> None:
        self.hardware.set_advanced(advanced)

    def show_hardware(self) -> None:
        self.tabs.setCurrentWidget(self.hardware)

    def reveal_section(self, section: str) -> None:
        """Bring the tab to the front that holds the settings of a validation section."""
        if section == "hardware":
            self.show_hardware()
        elif section == "portage":
            self.tabs.setCurrentIndex(1)
        elif section == "network":
            self.tabs.setCurrentIndex(2)
        elif section == "options":
            self.tabs.setCurrentIndex(3)

    def _hardware_changed(self) -> None:
        """A hardware choice was edited: what 'automatic' means in the Portage tab may have changed."""
        self.refresh_placeholders()
        self._update_network_notes()

    def refresh_placeholders(self) -> None:
        """'Leave empty: chosen for this computer (now: ...)': say what an empty field will become."""
        info = self.hardware.info
        make_conf: dict[str, str] = {}
        plan_scalars: dict[str, Any] = {}
        if info is not None and info.hardware:
            plan = hardware_plan(self.hardware.working_manifest(), info.hardware)
            make_conf = dict(plan.make_conf) if plan is not None else {}
            plan_scalars = dict(plan.scalars) if plan is not None else {}
        self._update_hardware_note(info, plan_scalars)
        for edit, key in ((self.cpu_flags, "CPU_FLAGS_X86"), (self.video_cards, "VIDEO_CARDS")):
            value = make_conf.get(key, "").replace("${VIDEO_CARDS}", "profile default +")
            if info is None or not info.hardware:
                edit.setPlaceholderText(AUTO_PLACEHOLDER)
            elif value:
                shown = value if len(value) <= 48 else value[:45] + "..."
                edit.setPlaceholderText(f"{AUTO_PLACEHOLDER} (now: {shown})")
            else:
                edit.setPlaceholderText(f"{AUTO_PLACEHOLDER} (the profile default)")

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
            check.setChecked(self.manifest.options.is_on(key))
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

    # --- manifest -----------------------------------------------------------------------------------------------

    def _network_changed(self) -> None:
        enabled = not self.dhcp.isChecked()
        for widget in (self.address, self.gateway, self.dns):
            widget.setEnabled(enabled)

    def load_manifest(self) -> None:
        portage = self.manifest.portage
        self.mirror.setText(portage.mirror)
        self.binhost.setChecked(portage.binhost)
        self.binhost_url.setText(portage.binhost_url)
        self._loaded_make_opts = portage.make_opts.strip()
        explicit = parse_jobs(self._loaded_make_opts) if self._loaded_make_opts and self._loaded_make_opts != auto_make_opts() else None
        self.jobs_auto.setChecked(explicit is None)
        self.jobs.setValue(explicit if explicit is not None else auto_jobs())
        self.jobs.setEnabled(explicit is not None)
        self.cflags.setText(portage.cflags)
        self.cxxflags.setText(portage.cxxflags)
        self.rustflags.setText(portage.rustflags)
        self.emerge_opts.setText(portage.emerge_opts)
        self.features.setText(" ".join(portage.features))
        self.licenses.setText(portage.accept_license)
        self.use_flags.setPlainText("\n".join(portage.use_flags))
        self.package_use.setPlainText("\n".join(f"{atom} {' '.join(flags)}" for atom, flags in portage.package_use.items()))
        self.cpu_flags.setText(portage.cpu_flags)
        self.video_cards.setText(portage.video_cards)
        network = self.manifest.network
        self.network_manager.setCurrentIndex(max(0, self.network_manager.findData(network.manager)))
        self.interface.setText(network.interface)
        self.dhcp.setChecked(network.dhcp)
        self.address.setText(network.address)
        self.gateway.setText(network.gateway)
        self.dns.setText(" ".join(network.dns))
        self.proxy.setText(network.proxy)
        self.wifi_ssid.setText(network.wifi_ssid)
        self.wifi_password.setText(network.wifi_password)
        self.import_profiles.setChecked(network.import_live_profiles)
        self._load_checks()
        self._load_kde_apps()
        self.extra_packages.setText(" ".join(self.manifest.options.extra_packages))
        self._network_changed()
        self._network_manager_changed()
        self.hardware.load_manifest()
        self.refresh_placeholders()

    def save_manifest(self) -> None:
        portage = self.manifest.portage
        portage.mirror = self.mirror.text().strip().rstrip("/")
        portage.binhost = self.binhost.isChecked()
        portage.binhost_url = self.binhost_url.text().strip()
        if self.jobs_auto.isChecked():
            portage.make_opts = ""  # the installer sizes the jobs from this computer's processors, memory and swap
        elif self._loaded_make_opts and parse_jobs(self._loaded_make_opts) == self.jobs.value():
            portage.make_opts = self._loaded_make_opts  # untouched: keep extra flags such as -l
        else:
            portage.make_opts = f"-j{self.jobs.value()}"
        portage.cflags = self.cflags.text().strip()
        portage.cxxflags = self.cxxflags.text().strip()
        portage.rustflags = self.rustflags.text().strip()
        portage.emerge_opts = self.emerge_opts.text().strip()
        portage.features = self.features.text().split()
        portage.accept_license = self.licenses.text().strip()
        portage.use_flags = self.use_flags.toPlainText().split()
        portage.package_use = {}
        for line in self.package_use.toPlainText().splitlines():
            words = line.split()
            if words:
                portage.package_use[words[0]] = words[1:]
        portage.cpu_flags = self.cpu_flags.text().strip()
        portage.video_cards = self.video_cards.text().strip()
        network = self.manifest.network
        network.manager = self.network_manager.currentData()
        network.interface = self.interface.text().strip()
        network.dhcp = self.dhcp.isChecked()
        network.address = self.address.text().strip()
        network.gateway = self.gateway.text().strip()
        network.dns = self.dns.text().split()
        network.proxy = self.proxy.text().strip()
        network.wifi_ssid = self.wifi_ssid.text().strip()
        network.wifi_password = self.wifi_password.text()
        network.import_live_profiles = self.import_profiles.isChecked()
        for key, check in self.checks.items():
            if check.isChecked() != self.manifest.options.is_on(key):  # a chosen logger or cron daemon stays unless it is switched
                setattr(self.manifest.options, key, check.isChecked())
        self.manifest.options.kde_apps = str(self.kde_apps.currentData() or self.manifest.options.kde_apps)
        self.manifest.options.extra_packages = self.extra_packages.text().split()
        self.hardware.save_manifest()

    def validate_page(self) -> tuple[bool, str]:
        self.save_manifest()
        problem = proxy_problem(self.proxy.text())
        if problem:
            return False, problem
        sections = self.manifest.validate_sections()
        errors = [error for name in ("portage", "network", "options") for error in sections[name]]
        if errors:
            return False, errors[0]
        hardware = self.hardware.problems()
        if hardware:
            self.show_hardware()
            return False, hardware[0]
        return True, ""

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

    def _manager_picked(self, _index: int = 0) -> None:
        """The user chose another network manager: Wi-Fi settings only exist for NetworkManager, so they are cleared."""
        if self.network_manager.currentData() != "networkmanager":
            self.wifi_ssid.clear()
            self.wifi_password.clear()
            self.import_profiles.setChecked(False)
        self._network_manager_changed()

    def _network_manager_changed(self) -> None:
        manager = self.network_manager.currentData()
        self.interface.setEnabled(manager in {"netifrc", "dhcpcd"})
        networkmanager = manager == "networkmanager"
        # Fields that already hold a value stay editable, so a wrong combination can always be cleared.
        for widget in (self.wifi_ssid, self.wifi_password):
            widget.setEnabled(networkmanager or bool(widget.text()))
        self.import_profiles.setEnabled(networkmanager or self.import_profiles.isChecked())
        self._update_network_notes()

    def _portable_target(self) -> bool:
        """The installed system is meant for other computers: the portable choice, or a removable target disk."""
        if self.hardware.working_manifest().hardware.target == "portable":
            return True
        info = self.hardware.info
        disk = info.find_disk(self.manifest.storage.device) if info is not None and self.manifest.storage.device else None
        return bool(disk is not None and disk.removable)

    def _update_network_notes(self) -> None:
        networkmanager = self.network_manager.currentData() == "networkmanager"
        self.network_note.setText("" if networkmanager else "Wi-Fi networks can be set up with NetworkManager only.")
        self.network_note.setVisible(bool(self.network_note.text()))
        info = self.hardware.info
        names = list(info.profile.network_profiles) if info is not None else []
        self.import_profiles.setToolTip(
            "Copies the saved network connections, including their passwords, to the installed system."
            + (f" Saved here: {', '.join(names)}." if names else ""))
        risky = self.import_profiles.isChecked() and self._portable_target()
        self.import_warning.setText(
            "The saved Wi-Fi passwords would be copied to a disk that is meant for other computers. Turn this off unless you trust everyone who may use that disk."
            if risky else "")
        self.import_warning.setVisible(risky)
