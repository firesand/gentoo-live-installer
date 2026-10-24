from __future__ import annotations

import re
from functools import partial
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout

from .. import diagnostics, netclient, theme
from ..manifest import InstallManifest
from ..system_info import MIN_DISK_BYTES, SystemInfo, desktop_disk_bytes, format_size, smallest_disk_bytes
from ..widgets import Card, FlowLayout, Page, StatusRow, wrapped_label
from .help import AdvancedSection, HelpNote
from .network_card import NetworkCard
from .remote_dialog import RemoteAccessDialog

DAEMON_LOG = Path("/var/log/gentoo-installerd.log")
PROXY_RE = re.compile(r"^(https?|socks5h?)://([^\s@/:]+(:[^\s@/]*)?@)?[A-Za-z0-9.-]+(:\d{1,5})?/?$")
PROXY_MESSAGE = "Download proxy must look like http://host:3128"
VIRTUAL_DISK_HINT = "QEMU: -device virtio-blk-pci,drive=d0,serial=MYDISK; libvirt: <serial>MYDISK</serial>"
# Findings from the daemon that stop the wizard everywhere (the others depend on
# what the user picks later; the daemon's preflight still refuses those).
GLOBAL_BLOCKERS = {"arch_unsupported", "memory_critical", "st_no_disk"}
WARNING_AREAS = {
    "fw": "Firmware", "st": "Storage", "bios": "Storage", "dos": "Storage", "disk": "Storage", "nvme": "Storage",
    "net": "Network", "gpu": "Graphics", "nvidia": "Graphics", "clock": "Clock", "memory": "Memory",
    "bat": "Power", "hibernate": "Power", "vm": "Virtual machine",
}
# Daemon error strings that the rows above already explain in structured form.
COVERED_ERRORS = ("Missing required tools", "No network connection", "Gentoo AMD64 requires", "At least 2 GiB", "Memory size could not")
CLOCK_ACTIONS = ("sync_time", "clock-sync")
CLOCK_EXPLAIN = "Downloads are checked with certificates that only work when the clock is right."
# NET-06: the charger finding is a banner of its own, not one more row; the Windows note is filed under "Windows", not "Firmware".
CHARGER_CODES = ("bat_low", "bat_ac_offline")
WINDOWS_CODES = ("fw_win_entry",)
PROXY_HELP = ("A proxy is a middle computer some company or school networks use for internet access; leave this empty at home. "
              "Without a password it is also kept in the installed system's Portage settings.")
KEEP_PROXY_CREDENTIALS = "A proxy with a password is used for the installation only: the password is never written to the installed system."
# Findings carry the name of a fix. With a daemon that offers it the row gets a button, otherwise a sentence says what to do.
ACTION_BUTTONS = {
    "sync_time": "Set clock from network",
    "set_time": "Set date and time…",
    "reboot_firmware": "Restart into firmware settings",
}
ACTION_HELP = {
    "reboot_firmware": "Restart the computer and open its firmware settings (often by pressing F2, F10, Del or Esc while it starts).",
    "sync_time": "Connect to the internet and press Check again, or set the date and time with the clock of the desktop.",
    "set_time": "Set the date and time with the clock of the desktop, then press Check again.",
    "safe_graphics": "Restart the installer and choose the compatibility entry in the boot menu.",
    "connect_charger": "Connect the charger, then press Check again.",
    "install_guest_tools": "Guest tools are installed for you. You can change that on the Hardware tab of the Customization step.",
}
SEVERITY_ORDER = {"error": 0, "warning": 1}


def proxy_has_credentials(proxy: str) -> bool:
    """Whether the proxy address carries a user name or password (the part before the last '@' of the authority)."""
    authority = proxy.strip().split("://", 1)[-1].split("/", 1)[0]
    return "@" in authority


def proxy_problem(proxy: str) -> str:
    """'' when the proxy is empty or well formed, else what is wrong with it."""
    proxy = proxy.strip()
    return "" if not proxy or PROXY_RE.match(proxy) else PROXY_MESSAGE


# The installer service's own checks (codes preflight.<what>) name the part of the computer they are about.
PREFLIGHT_AREAS = {
    "disks": "Storage", "memory_unknown": "Memory", "memory_low": "Memory", "network": "Network", "architecture": "Processor", "tools": "Installer tools",
}


def clean_code(code: str) -> str:
    return code[3:] if code.startswith("hw.") else code


def warning_area(code: str) -> str:
    """The row title of a finding: what part of the computer it is about."""
    if code.startswith("preflight."):
        return PREFLIGHT_AREAS.get(code.split(".", 1)[1], "Installer check")
    if clean_code(code) in WINDOWS_CODES:
        return "Windows"
    return WARNING_AREAS.get(clean_code(code).split("_", 1)[0], "Hardware")


class ReadinessPage(Page):
    refreshRequested = Signal()
    networkSettingsRequested = Signal()
    diagnosticsRequested = Signal()
    clockSyncRequested = Signal(str)  # name of the daemon action to run
    hardwareActionRequested = Signal(str)  # a finding's fix: sync_time, set_time or reboot_firmware

    def __init__(self, manifest: InstallManifest | None = None):
        super().__init__("A quick compatibility check", "We’ll confirm that the live environment is ready. Problems that would make the installation fail are marked as errors and must be fixed first.")
        self.manifest = manifest
        self.info: SystemInfo | None = None
        self.blocking: list[str] = []
        self.soft: list[str] = []
        self.card = Card()
        self.rows: dict[str, StatusRow] = {}
        for key, title in (
            ("service", "Installer service"),
            ("hardware", "This computer"),
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
        self.service_log = wrapped_label("", muted=True, selectable=True)
        self.service_log.hide()
        self.card.content.addWidget(self.service_log)
        self.hardware_hint = wrapped_label("Choices for this hardware (graphics driver, power, firmware and more) are on the Hardware tab of the Customization step.", muted=True)
        self.hardware_hint.hide()
        self.card.content.addWidget(self.hardware_hint)
        self.content.addWidget(self.card)

        self.notes_card = Card("Hardware notes", "What was found on this computer that needs your attention or is good to know.")
        self.notes_box = QVBoxLayout()
        self.notes_card.content.addLayout(self.notes_box)
        self.notes_card.hide()
        self.content.addWidget(self.notes_card)

        self.charger_banner = wrapped_label("", selectable=True)
        self.charger_banner.setObjectName("chargerBanner")
        self.charger_banner.setStyleSheet(
            f"QLabel#chargerBanner {{ color: {theme.status_color('warning')}; border: 1px solid {theme.status_color('warning')}; "
            f"border-radius: 8px; padding: 8px 12px; }}")
        self.charger_banner.hide()
        self.content.addWidget(self.charger_banner)

        self.network_card = NetworkCard(manifest)
        self.network_card.recheckRequested.connect(self.refreshRequested)
        self.network_card.networkSettingsRequested.connect(self.networkSettingsRequested)
        self.network_card.statusChanged.connect(self._render_network_row)
        self.network_card.hide()
        self.content.addWidget(self.network_card)

        self.proxy_section = AdvancedSection("readiness", "My network needs a proxy")
        proxy_title = QHBoxLayout()
        proxy_title.addWidget(wrapped_label("Only needed when this network reaches the internet through a proxy."), 1)
        proxy_title.addWidget(HelpNote(PROXY_HELP, "Networking"))
        self.proxy_section.add_layout(proxy_title)
        form = QFormLayout()
        self.proxy = QLineEdit()
        self.proxy.setPlaceholderText("http://proxy.example:3128 (optional)")
        self.proxy.setAccessibleName("Download proxy")
        self.proxy.textChanged.connect(self._proxy_changed)
        form.addRow("Proxy", self.proxy)
        self.proxy_section.add_layout(form)
        self.keep_proxy = QCheckBox("Keep the proxy on the installed system")
        self.keep_proxy.setChecked(True)
        self.proxy_section.add_widget(self.keep_proxy)
        self.keep_proxy_note = wrapped_label("", muted=True)
        self.keep_proxy_note.hide()
        self.proxy_section.add_widget(self.keep_proxy_note)
        self.content.addWidget(self.proxy_section)

        self.continue_anyway = QCheckBox("Continue anyway: I will fix the problems marked above myself")
        self.continue_anyway.hide()
        self.content.addWidget(self.continue_anyway)

        actions = FlowLayout()
        self.refresh_button = QPushButton("Check &again")
        self.refresh_button.clicked.connect(self.refreshRequested)
        actions.addWidget(self.refresh_button)
        network = QPushButton("&Network settings")
        network.clicked.connect(self.networkSettingsRequested)
        actions.addWidget(network)
        self.sync_button = QPushButton("Set clock from network")
        self.sync_button.hide()
        self.sync_button.clicked.connect(self._sync_clock)
        actions.addWidget(self.sync_button)
        self.remote_button = QPushButton("&Remote access (SSH)…")
        self.remote_button.setToolTip("Control this installer from another computer on your network.")
        self.remote_button.clicked.connect(self.open_remote_access)
        self.remote_button.hide()
        actions.addWidget(self.remote_button)
        self.diagnostics_button = QPushButton("Save &diagnostics…")
        self.diagnostics_button.setToolTip("Write a file with your hardware and settings (no passwords) that you can attach to a bug report.")
        self.diagnostics_button.clicked.connect(self.diagnosticsRequested)
        actions.addWidget(self.diagnostics_button)
        self.content.addLayout(actions)
        self.action_note = wrapped_label("", selectable=True)
        self.action_note.hide()
        self.content.addWidget(self.action_note)
        note = wrapped_label("An internet connection is required to download and verify the Gentoo stage3 archive and synchronize Portage.", muted=True)
        self.content.addWidget(note)
        self.clock_explain = wrapped_label(CLOCK_EXPLAIN, muted=True)
        self.content.addWidget(self.clock_explain)
        self.socket_path: str | None = None
        self._forced_off = False
        self._network_row: tuple[str, str] = ("checking", "Checking…")
        self.advance_on_enter(self.proxy)
        self.load_manifest()
        self.finish_layout()

    # --- manifest -------------------------------------------------------------------------------

    def load_manifest(self) -> None:
        if self.manifest is not None:
            self.proxy.setText(self.manifest.network.proxy)
            self.keep_proxy.setChecked(self.manifest.network.keep_proxy)
            self.network_card.manifest = self.manifest
            self.network_card.load_manifest()
            self._proxy_changed()

    def save_manifest(self) -> None:
        if self.manifest is not None:
            self.manifest.network.proxy = self.proxy.text().strip()
            # A proxy with a password is never written to the installed system, whatever the box says.
            self.manifest.network.keep_proxy = self.keep_proxy.isChecked() and not proxy_has_credentials(self.proxy.text())

    def set_advanced(self, advanced: bool) -> None:
        self.proxy_section.set_advanced(advanced)
        self.network_card.set_advanced(advanced)

    # --- the installer service ------------------------------------------------------------------

    def set_service(self, socket_path: str | None) -> None:
        """Tell the page where the installer service is (None: there is none, as in development runs)."""
        self.socket_path = socket_path
        self.network_card.caller = netclient.caller_for(socket_path) if socket_path else None

    def _proxy_changed(self, _text: str = "") -> None:
        proxy = self.proxy.text().strip()
        credentials = proxy_has_credentials(proxy)
        self.keep_proxy.setEnabled(bool(proxy) and not credentials)
        if credentials:
            self._forced_off = True
            self.keep_proxy.setChecked(False)
        elif self._forced_off:
            self._forced_off = False
            self.keep_proxy.setChecked(True)  # back to the default once the password is gone
        self.keep_proxy_note.setText(KEEP_PROXY_CREDENTIALS if credentials else "")
        self.keep_proxy_note.setVisible(credentials)

    def open_remote_access(self) -> None:
        RemoteAccessDialog(self.network_card.caller, self).exec()

    # --- rendering -------------------------------------------------------------------------------

    def set_checking(self) -> None:
        """Rows show 'Checking…' while a (re-)inspection runs in the background."""
        for row in self.rows.values():
            row.set_status("checking", "Checking…")
        self.service_log.hide()
        self.sync_button.hide()
        self.refresh_button.setEnabled(False)

    def update_info(self, info: SystemInfo) -> None:
        self.info = info
        self.blocking, self.soft = [], []
        self.refresh_button.setEnabled(True)
        if not info.inspected:
            self.set_checking()
            self.blocking.append("The hardware check is still running. Please wait a moment.")
            return
        self._service(info)
        self._hardware(info)
        self._architecture(info)
        self._memory(info)
        self._firmware(info)
        self._disks(info)
        self._network(info)
        self._clock(info)
        self._tools(info)
        self._notes(info)
        self._remote_and_card(info)
        self.continue_anyway.setVisible(bool(self.soft))
        if not self.soft:
            self.continue_anyway.setChecked(False)

    def _service(self, info: SystemInfo) -> None:
        row, daemon = self.rows["service"], info.daemon
        row.setVisible(daemon is None or daemon.kind != "dev")  # only developers run in development mode; the row would puzzle everyone else
        self.service_log.hide()
        if daemon is None:
            row.set_status("unknown", "Not checked (local detection only)")
        elif daemon.kind == "dev":
            row.set_status("info", "Development mode: the installer service is not used")
        elif daemon.reachable:
            row.set_status("ok", "Running." + (" An installation is currently running." if daemon.job_running else ""))
        else:
            message = daemon.message or "The installer service cannot be reached."
            row.set_status("error", message)
            self.blocking.append(message)
            tail = diagnostics.tail_file(DAEMON_LOG, 20)
            if tail:
                self.service_log.setText("Last lines of the service log:\n" + "\n".join(tail))
                self.service_log.show()

    def _architecture(self, info: SystemInfo) -> None:
        ok = info.architecture in {"x86_64", "amd64"}
        self.rows["architecture"].set_status("ok" if ok else "error", f"Detected {info.architecture}")
        if not ok:
            self.blocking.append("This installer supports AMD64/x86-64 systems only.")

    def _memory(self, info: SystemInfo) -> None:
        row = self.rows["memory"]
        if info.memory_gib and info.memory_gib < 1:
            row.set_status("error", f"{info.memory_gib:.1f} GiB available · at least 1 GiB is needed, 2 GiB or more recommended")
            self.blocking.append("This computer has less than 1 GiB of memory, which is not enough to install Gentoo.")
        else:
            row.set_status("ok" if info.memory_gib >= 2 else "warning", f"{info.memory_gib:.1f} GiB available · 2 GiB or more recommended")

    def _firmware(self, info: SystemInfo) -> None:
        firmware = info.hardware.get("firmware")
        details: dict = firmware if isinstance(firmware, dict) else {}
        text = "UEFI" if info.uefi else "Legacy BIOS"
        if info.uefi and details.get("efi_bits") in (32, 64):
            text = f"UEFI {details['efi_bits']}-bit"
        status = "ok"
        if details.get("secure_boot") == "enabled":
            status = "warning"
            text += ". Secure Boot is on: the installed system will not start unless it is signed. Turn Secure Boot off in the firmware settings."
        self.rows["firmware"].set_status(status, text)

    def _hardware(self, info: SystemInfo) -> None:
        row = self.rows["hardware"]
        summary = info.profile.summary() if info.hardware else ""
        unread = sorted({error.split(":", 1)[0] for error in (info.profile.errors if info.hardware else [])})
        if summary and unread:
            row.set_status("warning", f"{summary} · The detection could not read: {', '.join(unread)}. Those choices stay on Automatic.")
        elif summary:
            row.set_status("ok", summary)
        elif info.hardware_error:
            row.set_status("warning", f"The hardware detection failed ({info.hardware_error}). The installer will decide when it runs, and the Hardware tab keeps every choice on Automatic.")
        elif info.daemon is not None and info.daemon.kind == "dev":
            row.set_status("info", "Not detected in development mode")
        else:
            row.set_status("unknown", "The hardware could not be described. The installer will decide when it runs.")
        self.hardware_hint.setVisible(bool(summary))

    def _disks(self, info: SystemInfo) -> None:
        row = self.rows["disk"]
        manifest = self.manifest
        smallest = smallest_disk_bytes(manifest) if manifest is not None else MIN_DISK_BYTES
        usable = [disk for disk in info.disks if disk.selectable and disk.size >= smallest]
        with_identity = [disk for disk in usable if disk.has_identity]
        approvable = [disk for disk in usable if not disk.has_identity and disk.by_path]
        if not usable:
            small = [disk for disk in info.disks if disk.selectable]
            hint = f" ({', '.join(f'{d.path} {d.size_text}' for d in small)}: too small, {format_size(smallest)} needed)" if small else ""
            message = f"No suitable disk found: a writable disk of at least {format_size(smallest)} that is not the installer medium is required" + hint + "."
            # A storage controller in Intel RST mode hides the internal disks: the finding of the probe says what to change.
            message = next((str(w["message"]) for w in info.warnings if clean_code(str(w.get("code", ""))) == "st_rst_no_disks" and w.get("message")), message)
            row.set_status("error", message)
            self.blocking.append(message)
            return
        if with_identity:
            text = ", ".join(f"{disk.path} ({disk.size_text})" for disk in with_identity)
            extra = [disk for disk in usable if disk not in with_identity]
            if extra:
                many = len(extra) != 1
                text += f" · {len(extra)} more disk{'s' if many else ''} without a serial number {'need' if many else 'needs'} your explicit approval on the Storage page"
            status = "ok"
        elif approvable:
            names = ", ".join(f"{disk.path} ({disk.size_text})" for disk in approvable)
            text = f"{names}: no serial number or WWN (virtual disk?). You will be asked to approve the disk explicitly on the Storage page."
            status = "warning"
        else:
            names = ", ".join(disk.path for disk in usable)
            message = f"{names} has no serial number (virtual disk?) and no stable hardware path, so it cannot be identified safely. {VIRTUAL_DISK_HINT}"
            row.set_status("error", message)
            self.blocking.append(message)
            return
        largest = max(disk.size for disk in usable)
        wanted = desktop_disk_bytes(manifest) if manifest is not None else 0
        if wanted and largest < wanted:
            desktop = "a minimal system" if manifest is not None and manifest.system.desktop == "minimal" else manifest.system.desktop.upper() if manifest is not None else "the desktop"
            text += f" · The largest disk is too small for {desktop} (about {format_size(wanted)} needed); a lighter desktop or the minimal system still fits."
            status = "warning"
        row.set_status(status, text)

    def _network(self, info: SystemInfo) -> None:
        row = self.rows["network"]
        if info.network is None:
            self._network_row = ("checking", "Checking…")
            row.set_status("checking", "Checking…")
            return
        if info.network:
            self._network_row = ("ok", "Gentoo mirror is reachable" + (" through the proxy" if self.proxy.text().strip() else ""))
            self._render_network_row()
            return
        text = "No connection to the Gentoo mirror. Connect to a network, or enter a proxy under “My network needs a proxy” if this network needs one, then check again."
        if info.network_error:
            text += f" ({info.network_error})"
        self._network_row = ("error", text)
        self._render_network_row()
        self.soft.append(text)

    def _render_network_row(self) -> None:
        """The Internet row: the verdict, then the adapters, and for a failure the first Handbook test that failed."""
        status, text = self._network_row
        card = self.network_card
        card.setVisible(card.available and card.status is not None)
        if status != "checking" and card.status:
            problem = str(card.status.get("message") or "")
            if status == "error" and problem:
                text = problem
            summary = card.summary()
            if summary:
                text = f"{text}\n{summary}"
        self.rows["network"].set_status(status, text)

    def _remote_and_card(self, info: SystemInfo) -> None:
        """The connection card and the remote-access button exist only when the installer service offers them."""
        daemon = info.daemon
        card = self.network_card
        has_network = daemon is not None and daemon.supports("network.v1") and card.caller is not None
        card.setVisible(has_network and card.status is not None)
        if has_network:
            card.refresh(self.proxy.text().strip())
        self.remote_button.setVisible(daemon is not None and daemon.supports("remote.v1") and card.caller is not None)

    def _clock(self, info: SystemInfo) -> None:
        status, message = info.clock_status()
        self.rows["clock"].set_status(status, message)
        if status == "error":
            self.soft.append(message)
        daemon = info.daemon
        action = next((name for name in CLOCK_ACTIONS if daemon is not None and daemon.supports(name)), "")
        self.sync_button.setProperty("action", action)
        self.sync_button.setVisible(bool(action) and status in {"warning", "error"})

    def _tools(self, info: SystemInfo) -> None:
        if info.missing_tools:
            self.rows["tools"].set_status("error", "Missing: " + ", ".join(info.missing_tools))
            self.blocking.append("Required system tools are missing: " + ", ".join(info.missing_tools))
        else:
            self.rows["tools"].set_status("ok", "All required tools are available")

    def _notes(self, info: SystemInfo) -> None:
        while self.notes_box.count():
            item = self.notes_box.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        shown = 0
        daemon = info.daemon
        findings = sorted((w for w in info.warnings if str(w.get("message", "")).strip()), key=lambda w: SEVERITY_ORDER.get(str(w.get("severity", "info")), 2))
        seen: set[str] = set()
        charger = ""
        for warning in findings:
            code = clean_code(str(warning.get("code", "")))
            severity = str(warning.get("severity", "info"))
            message = str(warning.get("message", "")).strip()
            if code in CHARGER_CODES:
                charger = charger or message  # an amber banner at the top of the page, not one more row
                continue
            if message in seen:
                continue  # each note once
            seen.add(message)
            action = str(warning.get("action", "")).strip()
            action = "" if action == "none" else action
            button = ACTION_BUTTONS.get(action, "") if action and daemon is not None and daemon.supports(action) else ""
            if button:
                text = message
            elif action in ACTION_HELP:
                text = f"{message} {ACTION_HELP[action]}"
            else:
                text = message + (f" ({action.replace('_', ' ')})" if action else "")
            row = StatusRow(warning_area(str(warning.get("code", ""))), text)
            row.set_status({"error": "error", "warning": "warning"}.get(severity, "info"), text)
            if button:
                row.set_action(button, partial(self.hardwareActionRequested.emit, action))
            self.notes_box.addWidget(row)
            shown += 1
            if severity == "error" and code in GLOBAL_BLOCKERS:
                self.blocking.append(message)
        self.charger_banner.setText(charger)
        self.charger_banner.setVisible(bool(charger))
        for error in info.errors:
            if not error.startswith(COVERED_ERRORS) and error not in seen:
                seen.add(error)
                row = StatusRow("Other finding", error)
                row.set_status("warning", error)
                self.notes_box.addWidget(row)
                shown += 1
        self.notes_card.setVisible(bool(shown))

    def show_action_result(self, ok: bool, message: str) -> None:
        """What a button of this page (set the clock, restart into firmware) reported back."""
        self.action_note.setText(message)
        self.action_note.setStyleSheet(f"color: {theme.status_color('ok' if ok else 'error')};")
        self.action_note.setVisible(bool(message))

    def _sync_clock(self) -> None:
        action = str(self.sync_button.property("action") or "")
        if action:
            self.clockSyncRequested.emit(action)

    # --- gating -------------------------------------------------------------------------------------

    def validate_page(self) -> tuple[bool, str]:
        problem = proxy_problem(self.proxy.text())
        if problem:
            return False, problem
        problem = self.network_card.validate()
        if problem:
            return False, problem
        if self.info is None:
            return False, "The hardware check has not finished yet."
        if self.blocking:
            return False, self.blocking[0]
        if self.soft and not self.continue_anyway.isChecked():
            return False, (
                "Something that the installation needs is not working yet:\n\n" + "\n\n".join(self.soft)
                + "\n\nFix it and press “Check again”, or tick “Continue anyway” if you will fix it later "
                "(for example by entering a proxy on this page)."
            )
        return True, ""
