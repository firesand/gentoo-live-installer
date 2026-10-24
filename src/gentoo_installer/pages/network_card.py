"""NET-01, NET-02, NET-04, I11: the connection card of the readiness page.

It shows the adapters ("Wired: enp5s0 (connected, 192.168.1.20)"), says why the internet check failed in one plain sentence with
the Handbook's steps behind "Why not?", lets the user pick a Wi-Fi network and type its password without leaving the wizard, sets
a fixed address for the live session, and carries the live Wi-Fi connection over to the installed system.

The card talks to the installer service through ``caller(action, payload) -> answer`` (see ``netclient``); with no caller (no
service) it shows nothing. ``synchronous`` runs the calls on the GUI thread (tests); otherwise they run on a worker thread.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QToolButton, QVBoxLayout,
    QWidget,
)

from .. import netclient, netdiag, theme
from ..manifest import InstallManifest, LiveStatic
from ..widgets import Card, wrapped_label
from .help import AdvancedSection, HelpNote

WIFI_HELP = "Pick your Wi-Fi network and type its password; the installer will also remember it for the new system."
STATIC_HELP = ("Only needed when your network has no automatic address (DHCP); ask your network administrator for the address, "
               "gateway and DNS.")
INTERNET_HELP = "The installer downloads Gentoo from the internet, so it first checks that your connection works."
DSL_HINT = "For DSL (PPPoE) or VPN connections add a connection in the network settings tool, then check again."
CAPTIVE_HINT = "This network is open. If it asks you to log in on a web page, open a browser to log in, then check again."
OLD_SECURITY = "A very old Wi-Fi security (WEP) is not supported. Change the router to WPA2 or WPA3."


def signal_bars(signal: int) -> str:
    return "▂▄▆█"[min(3, max(0, signal) * 4 // 101)] if signal else "·"


class NetworkCard(Card):
    changed = Signal()  # a setting of the manifest was changed by this card
    recheckRequested = Signal()  # the connection changed: check the internet again
    networkSettingsRequested = Signal()
    statusChanged = Signal()

    def __init__(self, manifest: InstallManifest | None = None, parent: QWidget | None = None, *, caller: netclient.Caller | None = None,
                 synchronous: bool = False, secret_reader: Callable[[str], str] = netclient.read_live_secret) -> None:
        super().__init__("Your connection", "", parent)
        self.manifest = manifest
        self.caller = caller
        self.synchronous = synchronous
        self.secret_reader = secret_reader
        self.status: dict[str, Any] | None = None
        self.networks: list[dict[str, Any]] = []
        self.live_ssid = ""
        self._workers: list[netclient.CallWorker] = []

        head = QHBoxLayout()
        self.adapters = wrapped_label("", selectable=True)
        head.addWidget(self.adapters, 1)
        head.addWidget(HelpNote(INTERNET_HELP, "Networking"))
        self.content.addLayout(head)
        self.problem = wrapped_label("", selectable=True)
        self.problem.hide()
        self.content.addWidget(self.problem)
        self.why_button = QToolButton()
        self.why_button.setText("Why not?")
        self.why_button.setCheckable(True)
        self.why_button.setAutoRaise(True)
        self.why_button.hide()
        self.content.addWidget(self.why_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.why_box = wrapped_label("", muted=True, selectable=True)
        self.why_box.hide()
        self.content.addWidget(self.why_box)
        self.why_button.toggled.connect(self.why_box.setVisible)

        # --- Wi-Fi ---
        self.wifi_box = QWidget()
        wifi = QVBoxLayout(self.wifi_box)
        wifi.setContentsMargins(0, 4, 0, 0)
        title = QHBoxLayout()
        label = QLabel("Connect to Wi-Fi")
        label.setObjectName("cardTitle")
        title.addWidget(label)
        title.addWidget(HelpNote(WIFI_HELP, "Networking#Wireless_Setup"))
        title.addStretch(1)
        wifi.addLayout(title)
        self.rfkill_hint = wrapped_label("", selectable=True)
        self.rfkill_hint.setStyleSheet(f"color: {theme.status_color('warning')};")
        self.rfkill_hint.hide()
        wifi.addWidget(self.rfkill_hint)
        self.radio_button = QPushButton("Switch Wi-Fi &on")
        self.radio_button.hide()
        self.radio_button.clicked.connect(self.switch_wifi_on)
        wifi.addWidget(self.radio_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.network_list = QListWidget()
        self.network_list.setAccessibleName("Wi-Fi networks")
        self.network_list.setMinimumHeight(110)
        self.network_list.setMaximumHeight(170)
        self.network_list.currentRowChanged.connect(self._network_picked)
        wifi.addWidget(self.network_list)
        row = QHBoxLayout()
        self.scan_button = QPushButton("Scan &again")
        self.scan_button.clicked.connect(self.scan)
        row.addWidget(self.scan_button)
        row.addStretch(1)
        wifi.addLayout(row)
        form = QFormLayout()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setAccessibleName("Wi-Fi password")
        self.password.returnPressed.connect(self.connect_selected)
        form.addRow("Password", self.password)
        wifi.addLayout(form)
        self.show_password = QCheckBox("Show the password")
        self.show_password.toggled.connect(lambda on: self.password.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        wifi.addWidget(self.show_password)
        self.open_note = wrapped_label("", muted=True)
        self.open_note.hide()
        wifi.addWidget(self.open_note)
        self.remember = QCheckBox("Remember this network on the new system")
        self.remember.setChecked(True)
        self.remember.setToolTip("The installed system connects to this network by itself after the first start.")
        wifi.addWidget(self.remember)
        self.connect_button = QPushButton("&Connect")
        self.connect_button.setEnabled(False)
        self.connect_button.clicked.connect(self.connect_selected)
        wifi.addWidget(self.connect_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.wifi_result = wrapped_label("", selectable=True)
        self.wifi_result.hide()
        wifi.addWidget(self.wifi_result)
        self.other_networks = wrapped_label("A hidden network or one that needs a user name (WPA-Enterprise): use More network settings below.", muted=True)
        wifi.addWidget(self.other_networks)
        self.wifi_box.hide()
        self.content.addWidget(self.wifi_box)

        # --- the live connection's password, asked once (I11) ---
        self.again_box = QWidget()
        again = QVBoxLayout(self.again_box)
        again.setContentsMargins(0, 4, 0, 0)
        self.again_label = wrapped_label("")
        again.addWidget(self.again_label)
        self.again_password = QLineEdit()
        self.again_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.again_password.setAccessibleName("Wi-Fi password for the installed system")
        self.again_password.textChanged.connect(self._again_typed)
        again.addWidget(self.again_password)
        self.again_box.hide()
        self.content.addWidget(self.again_box)

        # --- more network settings ---
        self.more = AdvancedSection("readiness", "More network settings")
        self.more.add_widget(wrapped_label(DSL_HINT, muted=True))
        self.settings_button = QPushButton("Open the network settings tool")
        self.settings_button.clicked.connect(self.networkSettingsRequested)
        self.more.add_widget(self.settings_button)
        static_title = QHBoxLayout()
        self.static_check = QCheckBox("Set the address by hand")
        self.static_check.toggled.connect(self._static_toggled)
        static_title.addWidget(self.static_check)
        static_title.addWidget(HelpNote(STATIC_HELP, "Networking#Manual_and_static_IP_network_configuration"))
        static_title.addStretch(1)
        self.more_layout = QVBoxLayout()
        self.more_layout.addLayout(static_title)
        static_form = QFormLayout()
        self.address = QLineEdit()
        self.address.setPlaceholderText("192.168.1.50/24")
        self.address.setAccessibleName("Address and prefix length")
        self.gateway = QLineEdit()
        self.gateway.setPlaceholderText("192.168.1.1")
        self.gateway.setAccessibleName("Gateway")
        self.dns_preset = QComboBox()
        for name, servers in netclient.DNS_PRESETS:
            self.dns_preset.addItem(name, list(servers))
        self.dns_preset.addItem("My own…", None)
        self.dns_preset.currentIndexChanged.connect(self._preset_changed)
        self.dns_own = QLineEdit()
        self.dns_own.setPlaceholderText("1.1.1.1 9.9.9.9")
        self.dns_own.setAccessibleName("DNS servers")
        self.dns_own.hide()
        static_form.addRow("Address and prefix", self.address)
        static_form.addRow("Gateway", self.gateway)
        static_form.addRow("DNS servers", self.dns_preset)
        static_form.addRow("", self.dns_own)
        self.more_layout.addLayout(static_form)
        self.same_on_installed = QCheckBox("Use the same settings on the installed system")
        self.same_on_installed.setChecked(True)
        self.more_layout.addWidget(self.same_on_installed)
        buttons = QHBoxLayout()
        self.apply_button = QPushButton("&Apply")
        self.apply_button.clicked.connect(self.apply_static)
        self.automatic_button = QPushButton("Back to &automatic")
        self.automatic_button.clicked.connect(self.use_dhcp)
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.automatic_button)
        buttons.addStretch(1)
        self.more_layout.addLayout(buttons)
        self.static_result = wrapped_label("", selectable=True)
        self.static_result.hide()
        self.more_layout.addWidget(self.static_result)
        self.static_widgets = (self.address, self.gateway, self.dns_preset, self.dns_own, self.same_on_installed, self.apply_button, self.automatic_button)
        self.more.add_layout(self.more_layout)
        self.content.addWidget(self.more)
        self._static_toggled(False)

    # --- calls -----------------------------------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.caller is not None

    def call(self, action: str, payload: dict[str, Any], done: Callable[[dict[str, Any]], None]) -> None:
        if self.caller is None:
            done({"ok": False, "message": netclient.UNAVAILABLE})
            return
        if self.synchronous:
            done(self.caller(action, payload))
            return
        worker = netclient.CallWorker(self.caller, action, payload, self)
        worker.answered.connect(lambda _action, answer, w=worker: self._answered(w, done, answer))
        self._workers.append(worker)
        worker.start()

    def _answered(self, worker: netclient.CallWorker, done: Callable[[dict[str, Any]], None], answer: Any) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        worker.deleteLater()
        done(answer if isinstance(answer, dict) else {"ok": False, "message": "No answer."})

    def wait(self, milliseconds: int = 5000) -> None:
        """Wait for running calls (for scripts and tests)."""
        for worker in list(self._workers):
            worker.wait(milliseconds)

    # --- status ----------------------------------------------------------------------------------------------

    def refresh(self, proxy: str = "") -> None:
        """Ask the service how the network is doing (NET-01); the answer is shown by ``show_status``."""
        if not self.available:
            return
        self.call("network_status", {"proxy": proxy, "check_mirror": bool(proxy)}, self.show_status)

    def show_status(self, status: dict[str, Any]) -> None:
        if not status.get("ok", True) and "devices" not in status:
            return
        self.status = status
        adapters = [netdiag.Adapter(str(d.get("name", "")), str(d.get("kind", "other")), str(d.get("state", "")), ipv4=list(d.get("ipv4") or []),
                                    carrier=d.get("state") == "connected") for d in status.get("devices") or []]
        diagnosis = netdiag.Diagnosis(adapters, [])
        lines = diagnosis.summary_lines()
        self.adapters.setText("\n".join(lines) if lines else "No network adapter was found.")
        problem = str(status.get("problem") or "")
        message = str(status.get("message") or "")
        self.problem.setText(message)
        self.problem.setVisible(bool(problem and message))
        steps = status.get("steps") or []
        self.why_box.setText("\n".join(f"{self._mark(step.get('ok'))} {step.get('label', '')}" + (f" ({step['detail']})" if step.get("detail") else "")
                                       for step in steps))
        self.why_button.setVisible(bool(problem and steps))
        if not problem:
            self.why_button.setChecked(False)
        wifi_present = bool(status.get("wifi_present"))
        offline = bool(problem)
        self.wifi_box.setVisible(wifi_present and offline)
        hard = bool(status.get("rfkill_hard"))
        off = wifi_present and not status.get("wifi_enabled", True)
        self.rfkill_hint.setText(netdiag.MESSAGES["wifi_hard_off"] if hard else "Wireless is switched off." if off else "")
        self.rfkill_hint.setVisible(hard or off)
        self.radio_button.setVisible(off and not hard)
        self.scan_button.setEnabled(not (hard or off))
        if wifi_present and offline and not hard and not off and not self.networks:
            self.scan()
        if wifi_present:
            self.call("wifi_live_connections", {}, self.show_live_connections)
        self.statusChanged.emit()

    @staticmethod
    def _mark(ok: Any) -> str:
        return "✓" if ok is True else "×" if ok is False else "–"

    def summary(self) -> str:
        """The adapter lines, for the Internet row of the page."""
        return self.adapters.text().replace("\n", " · ") if self.status else ""

    # --- Wi-Fi -----------------------------------------------------------------------------------------------

    def switch_wifi_on(self) -> None:
        self.call("wifi_radio", {"on": True}, self._radio_done)

    def _radio_done(self, answer: dict[str, Any]) -> None:
        if answer.get("ok"):
            self.recheckRequested.emit()
        else:
            self.wifi_result.setText(str(answer.get("message") or ""))
            self.wifi_result.show()

    def scan(self) -> None:
        self.scan_button.setEnabled(False)
        self.call("wifi_scan", {"rescan": True}, self.show_networks)

    def show_networks(self, answer: dict[str, Any]) -> None:
        self.scan_button.setEnabled(True)
        self.networks = list(answer.get("networks") or [])
        self.network_list.clear()
        for row in self.networks:
            known = " · saved" if row.get("known") else ""
            use = " · connected" if row.get("in_use") else ""
            security = netclient.SECURITY_LABELS.get(str(row.get("security")), str(row.get("security")))
            item = QListWidgetItem(f"{signal_bars(int(row.get('signal') or 0))}  {row.get('ssid')}   ({security}, {row.get('band')} GHz){known}{use}")
            item.setData(Qt.ItemDataRole.UserRole, row.get("ssid"))
            self.network_list.addItem(item)
        if not self.networks:
            self.wifi_result.setText(str(answer.get("message") or "") if not answer.get("ok", True) else "No Wi-Fi network was found. Move closer to the router and scan again.")
            self.wifi_result.show()
        else:
            self.wifi_result.hide()

    def selected(self) -> dict[str, Any] | None:
        row = self.network_list.currentRow()
        return self.networks[row] if 0 <= row < len(self.networks) else None

    def _network_picked(self, _row: int = 0) -> None:
        network = self.selected()
        security = str(network.get("security")) if network else ""
        needs_password = security in {"wpa-psk", "sae"}
        self.password.setEnabled(needs_password)
        self.show_password.setEnabled(needs_password)
        if not needs_password:
            self.password.clear()
        text = ""
        if security == "open":
            text = "Open network (no password). " + CAPTIVE_HINT
        elif security == "wep":
            text = OLD_SECURITY
        elif security == "enterprise":
            text = "This network needs a user name and certificate. Use More network settings below."
        elif security == "owe":
            text = "This network is encrypted without a password."
        self.open_note.setText(text)
        self.open_note.setVisible(bool(text))
        self.connect_button.setEnabled(security in {"open", "owe", "wpa-psk", "sae"})

    def connect_selected(self) -> None:
        network = self.selected()
        if network is None or not self.connect_button.isEnabled():
            return
        self.connect_button.setEnabled(False)
        self.wifi_result.setText(f"Connecting to {network.get('ssid')}…")
        self.wifi_result.setStyleSheet("")
        self.wifi_result.show()
        self.call("wifi_connect", {"ssid": network.get("ssid"), "password": self.password.text()}, self._connected)

    def _connected(self, answer: dict[str, Any]) -> None:
        self.connect_button.setEnabled(self.selected() is not None)
        ok = bool(answer.get("ok"))
        message = str(answer.get("message") or ("Connected." if ok else "The connection could not be made."))
        self.wifi_result.setText(message)
        self.wifi_result.setStyleSheet(f"color: {theme.status_color('ok' if ok else 'error')};")
        self.wifi_result.show()
        if not ok:
            return
        self.password.clear()
        if self.manifest is not None:
            network = self.manifest.network
            if self.remember.isChecked() and network.manager == "networkmanager":
                network.copy_live_wifi = str(answer.get("uuid") or "")
                network.wifi_ssid = network.wifi_password = ""
            elif not self.remember.isChecked() and network.copy_live_wifi == answer.get("uuid"):
                network.copy_live_wifi = ""
            self.changed.emit()
        self.again_box.hide()
        self.recheckRequested.emit()

    # --- carrying the live Wi-Fi over (I11) --------------------------------------------------------------------

    def portable(self) -> bool:
        return self.manifest is not None and self.manifest.hardware.target == "portable"

    def show_live_connections(self, answer: dict[str, Any]) -> None:
        manifest = self.manifest
        if manifest is None or manifest.network.manager != "networkmanager":
            return
        rows = [row for row in answer.get("connections") or [] if isinstance(row, dict)]
        active = next((row for row in rows if row.get("active")), None)
        self.again_box.hide()
        if active is None:
            return
        network = manifest.network
        if network.copy_live_wifi or network.wifi_ssid:
            return  # the user (or an earlier visit) already decided
        self.live_ssid = str(active.get("ssid") or "")
        if active.get("secret_stored"):
            if not self.portable():
                network.copy_live_wifi = str(active.get("uuid"))
                self.changed.emit()
            return
        secret = self.secret_reader(str(active.get("uuid")))
        if secret:
            network.wifi_ssid, network.wifi_password = self.live_ssid, secret
            self.changed.emit()
            return
        self.again_label.setText(f"Type the password of {self.live_ssid} once more so the installed system can connect.")
        self.again_box.show()

    def _again_typed(self, text: str) -> None:
        if self.manifest is not None and self.live_ssid:
            self.manifest.network.wifi_ssid = self.live_ssid if text else ""
            self.manifest.network.wifi_password = text
            self.changed.emit()

    # --- a fixed address for the live session (NET-04) ---------------------------------------------------------

    def _static_toggled(self, on: bool) -> None:
        for widget in self.static_widgets:
            widget.setEnabled(on)
        self.automatic_button.setEnabled(True)

    def _preset_changed(self, _index: int = 0) -> None:
        self.dns_own.setVisible(self.dns_preset.currentData() is None)

    def dns_servers(self) -> list[str]:
        preset = self.dns_preset.currentData()
        return list(preset) if preset is not None else self.dns_own.text().replace(",", " ").split()

    def static_error(self) -> str:
        try:
            netdiag.validate_static(self.address.text(), self.gateway.text(), self.dns_servers())
        except ValueError as exc:
            return str(exc)
        return ""

    def apply_static(self) -> None:
        problem = self.static_error()
        if problem:
            self._static_message(problem, False)
            return
        address, gateway, servers = netdiag.validate_static(self.address.text(), self.gateway.text(), self.dns_servers())
        self.apply_button.setEnabled(False)
        self.call("network_static", {"address": address, "gateway": gateway, "dns": servers, "connection": self._live_connection()},
                  lambda answer: self._static_done(answer, LiveStatic(address, gateway, servers)))

    def _live_connection(self) -> str:
        for device in (self.status or {}).get("devices") or []:
            if device.get("state") == "connected" and device.get("connection"):
                return str(device["connection"])
        return ""

    def _static_done(self, answer: dict[str, Any], live: LiveStatic | None) -> None:
        self.apply_button.setEnabled(self.static_check.isChecked())
        ok = bool(answer.get("ok"))
        self._static_message(str(answer.get("message") or ""), ok)
        if not ok or self.manifest is None:
            return
        network = self.manifest.network
        network.live_static = live or LiveStatic()
        if live is not None and self.same_on_installed.isChecked():
            self._copy_to_installed(live)
        self.changed.emit()
        self.recheckRequested.emit()

    def _copy_to_installed(self, live: LiveStatic) -> None:
        network = self.manifest.network if self.manifest is not None else None
        if network is None:
            return
        network.dhcp, network.address, network.gateway, network.dns = False, live.address, live.gateway, list(live.dns)
        if network.manager in {"netifrc", "dhcpcd"} and not network.interface:
            wired = next((d for d in (self.status or {}).get("devices") or [] if d.get("kind") == "ethernet" and d.get("state") == "connected"), None)
            if wired:
                network.interface = str(wired.get("name"))

    def use_dhcp(self) -> None:
        self.call("network_static", {"dhcp": True, "connection": self._live_connection()}, lambda answer: self._static_done(answer, None))

    def _static_message(self, text: str, ok: bool) -> None:
        self.static_result.setText(text)
        self.static_result.setStyleSheet(f"color: {theme.status_color('ok' if ok else 'error')};")
        self.static_result.setVisible(bool(text))

    # --- manifest ----------------------------------------------------------------------------------------------

    def load_manifest(self) -> None:
        if self.manifest is None:
            return
        live = self.manifest.network.live_static
        if live.is_set():
            self.static_check.setChecked(True)
            self.address.setText(live.address)
            self.gateway.setText(live.gateway)
            index = next((i for i in range(self.dns_preset.count()) if self.dns_preset.itemData(i) == list(live.dns)), -1)
            if index >= 0:
                self.dns_preset.setCurrentIndex(index)
            else:
                self.dns_preset.setCurrentIndex(self.dns_preset.count() - 1)
                self.dns_own.setText(" ".join(live.dns))

    def validate(self) -> str:
        """A sentence when the hand-set address is ticked but cannot be applied (empty when fine)."""
        if self.static_check.isChecked() and (self.address.text().strip() or self.gateway.text().strip()):
            return self.static_error()
        return ""

    def set_advanced(self, advanced: bool) -> None:
        self.more.set_advanced(advanced)
