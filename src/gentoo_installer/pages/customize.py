from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QTabWidget,
    QWidget,
)

from ..manifest import InstallManifest
from ..widgets import Card, Page


class CustomizePage(Page):
    def __init__(self, manifest: InstallManifest):
        super().__init__("Make it yours", "Safe defaults are already selected. Advanced settings use the same Portage and network configuration files described in the Handbook.")
        self.manifest = manifest
        tabs = QTabWidget()
        tabs.addTab(self._portage_tab(), "Portage")
        tabs.addTab(self._network_tab(), "Network")
        tabs.addTab(self._extras_tab(), "Applications && tools")
        self.content.addWidget(tabs)
        self.load_manifest()

    def _portage_tab(self) -> QWidget:
        page = QWidget()
        card = Card(parent=page)
        from PySide6.QtWidgets import QVBoxLayout
        layout = QVBoxLayout(page)
        layout.addWidget(card)
        form = QFormLayout()
        self.mirror = QLineEdit()
        form.addRow("HTTPS download mirror", self.mirror)
        self.binhost = QCheckBox("Prefer official Gentoo binary packages when available")
        form.addRow(self.binhost)
        self.binhost_url = QLineEdit()
        form.addRow("Binary package host", self.binhost_url)
        self.jobs = QSpinBox()
        self.jobs.setRange(1, 256)
        self.jobs.setValue(min(32, os.cpu_count() or 2))
        form.addRow("Parallel build jobs", self.jobs)
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
        form.addRow("Global USE flags", self.use_flags)
        self.package_use = QPlainTextEdit()
        self.package_use.setPlaceholderText("category/package flag -flag, one entry per line")
        self.package_use.setMaximumHeight(85)
        form.addRow("Per-package USE flags", self.package_use)
        self.cpu_flags = QLineEdit()
        form.addRow("CPU_FLAGS_X86", self.cpu_flags)
        self.video_cards = QLineEdit()
        self.video_cards.setPlaceholderText("Detected automatically when empty")
        form.addRow("VIDEO_CARDS", self.video_cards)
        card.content.addLayout(form)
        return page

    def _network_tab(self) -> QWidget:
        page = QWidget()
        from PySide6.QtWidgets import QVBoxLayout
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
        form.addRow("Download proxy", self.proxy)
        self.wifi_ssid = QLineEdit()
        form.addRow("Wi-Fi network (optional)", self.wifi_ssid)
        self.wifi_password = QLineEdit()
        self.wifi_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Wi-Fi password", self.wifi_password)
        card.content.addLayout(form)
        return page

    def _extras_tab(self) -> QWidget:
        page = QWidget()
        from PySide6.QtWidgets import QGridLayout, QVBoxLayout
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
        self.extra_packages = QLineEdit()
        self.extra_packages.setPlaceholderText("category/package category/other-package")
        card.content.addWidget(self.extra_packages)
        return page

    def _network_changed(self) -> None:
        enabled = not self.dhcp.isChecked()
        for widget in (self.address, self.gateway, self.dns):
            widget.setEnabled(enabled)

    def load_manifest(self) -> None:
        portage = self.manifest.portage
        self.mirror.setText(portage.mirror)
        self.binhost.setChecked(portage.binhost)
        self.binhost_url.setText(portage.binhost_url)
        jobs = 2
        try:
            jobs = int(portage.make_opts.removeprefix("-j").split()[0])
        except (ValueError, IndexError):
            pass
        self.jobs.setValue(jobs)
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
        for key, check in self.checks.items():
            check.setChecked(getattr(self.manifest.options, key))
        self.extra_packages.setText(" ".join(self.manifest.options.extra_packages))
        self._network_changed()

    def save_manifest(self) -> None:
        portage = self.manifest.portage
        portage.mirror = self.mirror.text().strip().rstrip("/")
        portage.binhost = self.binhost.isChecked()
        portage.binhost_url = self.binhost_url.text().strip()
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
        errors = [error for error in self.manifest.validate() if any(word in error.lower() for word in ("network", "mirror", "package", "static", "binary"))]
        return (False, errors[0]) if errors else (True, "")

    def _network_manager_changed(self) -> None:
        self.interface.setEnabled(self.network_manager.currentData() == "netifrc")
