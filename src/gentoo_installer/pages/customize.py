from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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

from .. import system_info
from ..manifest import InstallManifest
from ..widgets import Card, Page, wrapped_label
from .readiness import proxy_problem

COMPANIONS = ("install_hwmonux", "install_portagehub")


def auto_jobs() -> int:
    """Parallel build jobs this computer can sustain: one per processor, but
    not more than one per 2 GiB of memory (big C++ and Rust builds)."""
    cpus = os.cpu_count() or 2
    return max(1, min(cpus, int(system_info._memory_gib() // 2) or 1))


def auto_make_opts() -> str:
    return f"-j{auto_jobs()} -l{os.cpu_count() or 2}"


def parse_jobs(make_opts: str) -> int | None:
    try:
        return int(make_opts.strip().removeprefix("-j").split()[0])
    except (ValueError, IndexError):
        return None


class CustomizePage(Page):
    def __init__(self, manifest: InstallManifest):
        super().__init__("Make it yours", "Safe defaults are already selected. Advanced settings use the same Portage and network configuration files described in the Handbook.")
        self.manifest = manifest
        self._loaded_make_opts = ""
        self._cleared_companions: tuple[bool, bool] | None = None
        tabs = QTabWidget()
        tabs.addTab(self._portage_tab(), "Portage")
        tabs.addTab(self._network_tab(), "Network")
        tabs.addTab(self._extras_tab(), "Applications && tools")
        self.tabs = tabs
        self.content.addWidget(tabs)
        self.load_manifest()

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
        self.jobs_auto = QCheckBox()
        self.jobs_auto.setText(f"Automatic ({auto_jobs()} jobs for {os.cpu_count() or 2} processors)")
        self.jobs_auto.setToolTip("Chosen from this computer's processors and memory so that builds neither crawl nor run out of memory.")
        self.jobs_auto.toggled.connect(lambda checked: self.jobs.setEnabled(not checked))
        jobs_row.addWidget(self.jobs_auto)
        self.jobs = QSpinBox()
        self.jobs.setRange(1, 256)
        self.jobs.setValue(auto_jobs())
        jobs_row.addWidget(self.jobs)
        jobs_row.addStretch()
        form.addRow("Parallel build jobs", jobs_row)
        self.cflags = QLineEdit()
        self.cflags.setToolTip("-march=native can optimize for this computer, but binaries may not run on a different CPU.")
        form.addRow("CFLAGS", self.cflags)
        self.cxxflags = QLineEdit()
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
        form.addRow("CPU_FLAGS_X86", self.cpu_flags)
        self.video_cards = QLineEdit()
        self.video_cards.setPlaceholderText("Detected automatically when empty")
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
        form.addRow("Network management", self.network_manager)
        self.interface = QLineEdit()
        self.interface.setPlaceholderText("enp1s0 (required by netifrc)")
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
        card.content.addLayout(form)
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
            grid.addWidget(check, index // 2, index % 2)
        card.content.addLayout(grid)
        self.companion_note = wrapped_label(
            "HWMonux and PortageHub are graphical applications; they only start inside a desktop session. They are switched off for the minimal system, tick them again if you plan to add a desktop later.",
            muted=True,
        )
        self.companion_note.hide()
        card.content.addWidget(self.companion_note)
        self.extra_packages = QLineEdit()
        self.extra_packages.setPlaceholderText("category/package category/other-package")
        card.content.addWidget(self.extra_packages)
        layout.addStretch(1)
        return page

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

    def _load_checks(self) -> None:
        for key, check in self.checks.items():
            check.setChecked(getattr(self.manifest.options, key))
        minimal = self.manifest.system.desktop == "minimal"
        self.companion_note.setVisible(minimal and self._cleared_companions is not None)

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
        self._load_checks()
        self.extra_packages.setText(" ".join(self.manifest.options.extra_packages))
        self._network_changed()
        self._network_manager_changed()

    def save_manifest(self) -> None:
        portage = self.manifest.portage
        portage.mirror = self.mirror.text().strip().rstrip("/")
        portage.binhost = self.binhost.isChecked()
        portage.binhost_url = self.binhost_url.text().strip()
        if self.jobs_auto.isChecked():
            portage.make_opts = auto_make_opts()
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
        for key, check in self.checks.items():
            setattr(self.manifest.options, key, check.isChecked())
        self.manifest.options.extra_packages = self.extra_packages.text().split()

    def validate_page(self) -> tuple[bool, str]:
        self.save_manifest()
        problem = proxy_problem(self.proxy.text())
        if problem:
            return False, problem
        sections = self.manifest.validate_sections()
        errors = [error for name in ("portage", "network", "options") for error in sections[name]]
        return (False, errors[0]) if errors else (True, "")

    def _network_manager_changed(self) -> None:
        self.interface.setEnabled(self.network_manager.currentData() == "netifrc")
