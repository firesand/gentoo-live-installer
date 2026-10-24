"""NET: the Network tab of the Customize page: how the installed system connects (manager, interface, address, Wi-Fi, tools).

Guided shows one line, "Network: NetworkManager (recommended, handles Wi-Fi and cable)", with a Change button; every other control
is behind "More network options" (Advanced opens it). The proxy is asked once, on the first page; here it is only shown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QCheckBox, QComboBox, QCompleter, QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget

from ..widgets import Card, wrapped_label
from .help import AdvancedSection, Badge, HelpNote
from .network_card import STATIC_HELP

if TYPE_CHECKING:
    from ..manifest import InstallManifest
    from .hardware import HardwareTab

TITLE = "Network on the installed system"
INTRO = "This is how the new Gentoo connects after the first start; the connection you used for the installation is separate."
MANAGERS = (
    ("NetworkManager - easiest for laptops and Wi-Fi", "networkmanager"),
    ("dhcpcd - wired and simple (no Wi-Fi manager)", "dhcpcd"),
    ("netifrc - OpenRC, one setting per network card", "netifrc"),
    ("systemd-networkd - wired only", "systemd-networkd"),
)
MANAGER_NAMES = {"networkmanager": "NetworkManager (recommended, handles Wi-Fi and cable)", "dhcpcd": "dhcpcd (wired, simple)",
                 "netifrc": "netifrc (OpenRC scripts, one per network card)", "systemd-networkd": "systemd-networkd (wired only)"}
PORTABLE_WARNING = "Network card names differ between computers; use NetworkManager for a system you move around."
WIFI_SECURITY_NOTE = "Very old Wi-Fi security (WEP, TKIP) will not connect."
PPP_HELP = "Only needed if your internet comes from a DSL modem that asks for a user name and password."
WIRELESS_HELP = "Installs iw and wpa_supplicant so this system can join Wi-Fi networks without NetworkManager."
PROXY_HINT = ("Set on the first page of the installer (“My network needs a proxy”). Without a password it is also kept in the "
              "installed system's Portage settings; a proxy with a password is used for the installation only.")


class InterfaceCombo(QComboBox):
    """The network card name: a list of the detected adapters that can also be typed into (a QLineEdit-like text API for the page)."""

    def __init__(self) -> None:
        super().__init__()
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)

    def text(self) -> str:
        return self.currentText()

    def setText(self, text: str) -> None:
        self.setCurrentIndex(-1)
        self.setEditText(text)

    def setPlaceholderText(self, text: str) -> None:
        line = self.lineEdit()
        if line is not None:
            line.setPlaceholderText(text)

    def set_names(self, names: list[str]) -> None:
        typed = self.currentText()
        self.clear()
        self.addItems(names)
        self.setText(typed)
        self.setCompleter(QCompleter(names, self))


class NetworkTabMixin:
    """The Network tab's widgets (network_manager, interface, dhcp, proxy, ...) are attributes of the page that mixes this in."""

    if TYPE_CHECKING:
        manifest: InstallManifest
        hardware: HardwareTab

    def _network_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        card = Card(TITLE, INTRO, parent=page)
        layout.addWidget(card)
        # Guided: one line and a Change button.
        line = QHBoxLayout()
        self.network_summary = wrapped_label("")
        line.addWidget(self.network_summary, 1)
        line.addWidget(Badge("Recommended"))
        self.network_change = QPushButton("Change…")
        self.network_change.clicked.connect(self._open_network_options)
        line.addWidget(self.network_change)
        card.content.addLayout(line)
        self.network_more = AdvancedSection("customize-network", "More network options")
        card.content.addWidget(self.network_more)

        form = QFormLayout()
        self.network_manager = QComboBox()
        for label, value in MANAGERS:
            self.network_manager.addItem(label, value)
        self.network_manager.currentIndexChanged.connect(self._network_manager_changed)
        self.network_manager.activated.connect(self._manager_picked)
        form.addRow("Network management", self.network_manager)
        self.interface = InterfaceCombo()
        self.interface.setPlaceholderText("enp1s0 (needed by netifrc, and for a static address with dhcpcd)")
        self.interface.setToolTip("The name of the network adapter. Network interface names differ between computers.")
        self.interface.editTextChanged.connect(lambda _text="": self._update_network_notes())
        form.addRow("Network interface", self.interface)
        self.dhcp = QCheckBox("Configure the network automatically with DHCP")
        self.dhcp.toggled.connect(self._network_changed)
        form.addRow(self.dhcp)
        static_help = QHBoxLayout()
        static_help.addWidget(wrapped_label("A fixed address is only needed when your network has no automatic address.", muted=True), 1)
        static_help.addWidget(HelpNote(STATIC_HELP, "Networking#Manual_and_static_IP_network_configuration"))
        form.addRow(static_help)
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
        self.proxy.setReadOnly(True)
        self.proxy.setPlaceholderText("none")
        self.proxy.setToolTip(PROXY_HINT)
        form.addRow("Download proxy", self.proxy)
        self.wifi_ssid = QLineEdit()
        form.addRow("Wi-Fi for the installed system", self.wifi_ssid)
        self.wifi_password = QLineEdit()
        self.wifi_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Wi-Fi password", self.wifi_password)
        self.network_more.add_layout(form)
        self.import_profiles = QCheckBox("Use the Wi-Fi networks saved in this live session")
        self.import_profiles.setToolTip("Copies the saved network connections, including their passwords, to the installed system.")
        self.import_profiles.toggled.connect(lambda _=False: self._update_network_notes())
        self.network_more.add_widget(self.import_profiles)
        wireless = QHBoxLayout()
        self.wireless_tools = QCheckBox("Install Wi-Fi tools (iw, wpa_supplicant)")
        self.wireless_tools.toggled.connect(lambda _=False: self._update_network_notes())
        wireless.addWidget(self.wireless_tools)
        wireless.addWidget(HelpNote(WIRELESS_HELP, "System#Wireless_networking"))
        wireless.addStretch(1)
        self.network_more.add_layout(wireless)
        ppp = QHBoxLayout()
        self.ppp = QCheckBox("DSL (PPPoE) support")
        ppp.addWidget(self.ppp)
        ppp.addWidget(HelpNote(PPP_HELP, "System#Network"))
        ppp.addStretch(1)
        self.network_more.add_layout(ppp)
        self.network_note = wrapped_label("", muted=True)
        self.network_note.hide()
        self.network_more.add_widget(self.network_note)
        self.portable_warning = wrapped_label("")
        self.portable_warning.setStyleSheet("color: #b87818;")
        self.portable_warning.hide()
        self.network_more.add_widget(self.portable_warning)
        self.import_warning = wrapped_label("", selectable=True)
        self.import_warning.setStyleSheet("color: #b87818;")
        self.import_warning.hide()
        self.network_more.add_widget(self.import_warning)
        layout.addStretch(1)
        return page

    def _open_network_options(self) -> None:
        self.network_more.set_open(True)

    def set_network_advanced(self, advanced: bool) -> None:
        self.network_more.set_advanced(advanced)
        self.network_change.setVisible(not advanced)

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
        self.wireless_tools.setChecked(network.wireless_tools)
        self.ppp.setChecked(network.ppp)

    def save_network(self) -> None:
        network = self.manifest.network
        network.manager = self.network_manager.currentData()
        network.interface = self.interface.text().strip()
        network.dhcp = self.dhcp.isChecked()
        network.address = self.address.text().strip()
        network.gateway = self.gateway.text().strip()
        network.dns = self.dns.text().split()
        network.wifi_ssid = self.wifi_ssid.text().strip()
        network.wifi_password = self.wifi_password.text()
        network.import_live_profiles = self.import_profiles.isChecked()
        network.wireless_tools = self.wireless_tools.isChecked()
        network.ppp = self.ppp.isChecked()

    def detected_adapters(self) -> tuple[list[str], str]:
        """(names of the detected network adapters, the wired one that is in use or the first wired one)."""
        info = self.hardware.info
        nics = [nic for nic in (info.profile.nics if info is not None else []) if nic.iface and not nic.no_driver]
        names = list(dict.fromkeys(nic.iface for nic in nics))
        wired = [nic for nic in nics if nic.kind == "ethernet"]
        chosen = next((nic for nic in wired if nic.primary), None) or next((nic for nic in wired if nic.carrier), None) or (wired[0] if wired else None)
        return names, chosen.iface if chosen else ""

    def _has_wifi_adapter(self) -> bool:
        info = self.hardware.info
        return bool(info is not None and any(nic.kind == "wifi" and not nic.no_driver for nic in info.profile.nics))

    def _manager_picked(self, _index: int = 0) -> None:
        """The user chose another network manager: Wi-Fi settings only exist for NetworkManager, so they are cleared."""
        manager = self.network_manager.currentData()
        if manager != "networkmanager":
            self.wifi_ssid.clear()
            self.wifi_password.clear()
            self.import_profiles.setChecked(False)
            if self._has_wifi_adapter() and manager in {"dhcpcd", "netifrc"}:
                self.wireless_tools.setChecked(True)  # a Wi-Fi card without NetworkManager needs the tools
        if manager in {"netifrc", "dhcpcd"} and not self.interface.text().strip():
            self.interface.setText(self.detected_adapters()[1])
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
        manager = self.network_manager.currentData()
        networkmanager = manager == "networkmanager"
        self.network_summary.setText(f"Network: {MANAGER_NAMES.get(manager, manager)}")
        notes = []
        if not networkmanager:
            notes.append("Wi-Fi networks can be set up with NetworkManager only.")
        if self.wireless_tools.isChecked() or self._has_wifi_adapter():
            notes.append(WIFI_SECURITY_NOTE)
        self.network_note.setText(" ".join(notes))
        self.network_note.setVisible(bool(notes))
        info = self.hardware.info
        names = list(info.profile.network_profiles) if info is not None else []
        self.import_profiles.setToolTip(
            "Copies the saved network connections, including their passwords, to the installed system."
            + (f" Saved here: {', '.join(names)}." if names else ""))
        portable = self._portable_target()
        self.portable_warning.setText(PORTABLE_WARNING if portable and manager in {"netifrc", "systemd-networkd"} else "")
        self.portable_warning.setVisible(bool(self.portable_warning.text()))
        risky = self.import_profiles.isChecked() and portable
        self.import_warning.setText(
            "The saved Wi-Fi passwords would be copied to a disk that is meant for other computers. Turn this off unless you trust everyone who may use that disk."
            if risky else "")
        self.import_warning.setVisible(risky)
