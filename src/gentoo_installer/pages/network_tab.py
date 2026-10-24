"""NET: the Network tab of the Customize page (network manager, interface, static address, proxy, Wi-Fi)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QCheckBox, QComboBox, QCompleter, QFormLayout, QLineEdit, QVBoxLayout, QWidget

from ..widgets import Card, wrapped_label

if TYPE_CHECKING:
    from ..manifest import InstallManifest
    from .hardware import HardwareTab


class NetworkTabMixin:
    """The Network tab's widgets (network_manager, interface, dhcp, proxy, ...) are attributes of the page that mixes this in."""

    if TYPE_CHECKING:
        manifest: InstallManifest
        hardware: HardwareTab

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

    def _network_changed(self) -> None:
        enabled = not self.dhcp.isChecked()
        for widget in (self.address, self.gateway, self.dns):
            widget.setEnabled(enabled)

    def load_network(self) -> None:
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

    def save_network(self) -> None:
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
