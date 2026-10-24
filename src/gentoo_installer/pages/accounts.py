from __future__ import annotations

import zoneinfo
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton

from .. import keymaps
from ..manifest import InstallManifest
from ..widgets import Card, Page, wrapped_label


SUPPORTED_LOCALES = Path("/usr/share/i18n/SUPPORTED")
COMMON_LOCALES = ("en_US.UTF-8", "en_GB.UTF-8", "de_DE.UTF-8", "es_ES.UTF-8", "fr_FR.UTF-8", "it_IT.UTF-8", "ja_JP.UTF-8", "nl_NL.UTF-8", "pl_PL.UTF-8", "pt_BR.UTF-8", "tr_TR.UTF-8")


def load_locales(path: Path = SUPPORTED_LOCALES) -> list[str]:
    """UTF-8 locales glibc can generate (en_US.UTF-8 first); a short list when
    the file is not there."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return list(COMMON_LOCALES)
    found: set[str] = set()
    for line in lines:
        parts = line.split()
        if len(parts) != 2 or parts[1] != "UTF-8" or line.startswith("#"):
            continue
        name, _, modifier = parts[0].partition("@")
        found.add(name.split(".")[0] + ".UTF-8" + (f"@{modifier}" if modifier else ""))
    if not found:
        return list(COMMON_LOCALES)
    return sorted(found, key=lambda locale: (locale != "en_US.UTF-8", locale))


class AccountsPage(Page):
    keyboardChangeRequested = Signal()
    keyboardPicked = Signal(str, str, str)  # XKB layout, variant, console keymap: chosen here, to be applied to the session

    def __init__(self, manifest: InstallManifest):
        super().__init__("Personalize your system", "Create your daily account and choose regional settings. Passwords are sent only to the privileged installer and are never written to logs.")
        self.manifest = manifest
        account = Card("User accounts")
        form = QFormLayout()
        self.hostname = QLineEdit()
        form.addRow("Computer &name", self.hostname)
        self.full_name = QLineEdit()
        form.addRow("Your name (optional)", self.full_name)
        self.username = QLineEdit()
        form.addRow("&Username", self.username)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("At least 8 characters")
        form.addRow("&Password", self.password)
        self.password_confirm = QLineEdit()
        self.password_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Repeat password", self.password_confirm)
        self.root_password = QLineEdit()
        self.root_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.root_password.setPlaceholderText("At least 8 characters")
        form.addRow("&Root password", self.root_password)
        self.root_confirm = QLineEdit()
        self.root_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Repeat root password", self.root_confirm)
        self.show_passwords = QCheckBox("Show the passwords while typing")
        self.show_passwords.toggled.connect(self._show_passwords)
        form.addRow(self.show_passwords)
        self.privilege = QComboBox()
        self.privilege.addItem("sudo", "sudo")
        self.privilege.addItem("doas", "doas")
        form.addRow("Administrator command", self.privilege)
        self.disable_root = QCheckBox("Disable direct root login after creating the administrator account")
        form.addRow(self.disable_root)
        self.ssh_keys = QPlainTextEdit()
        self.ssh_keys.setPlaceholderText("Optional SSH public keys, one per line")
        self.ssh_keys.setMaximumHeight(80)
        self.ssh_keys.setTabChangesFocus(True)  # Tab leaves the box instead of inserting a tab
        form.addRow("SSH authorized keys", self.ssh_keys)
        account.content.addLayout(form)
        self.content.addWidget(account)

        regional = Card("Region and input")
        regional_form = QFormLayout()
        self.locale_box = QComboBox()
        self.locale_box.setEditable(True)
        self.locale_box.addItems(load_locales())
        regional_form.addRow("System locale", self.locale_box)
        self.timezone = QComboBox()
        self.timezone.setEditable(True)
        zones = sorted(zoneinfo.available_timezones())
        self.timezone.addItems(zones)
        self.timezone.setCurrentText("UTC")
        regional_form.addRow("Timezone", self.timezone)
        self.keymap = QComboBox()
        self.keymap.setAccessibleName("Console keymap")
        for console, label in keymaps.console_choices():
            self.keymap.addItem(f"{label} ({console})", console)
        self.keymap.activated.connect(self._keymap_picked)
        regional_form.addRow("Console keymap", self.keymap)
        keyboard_row = QHBoxLayout()
        self.keyboard_summary = QLabel()
        self.keyboard_summary.setProperty("muted", True)
        self.keyboard_summary.setWordWrap(True)
        keyboard_row.addWidget(self.keyboard_summary, 1)
        change = QPushButton("Change keyboard layout…")
        change.clicked.connect(self.keyboardChangeRequested)
        keyboard_row.addWidget(change)
        regional_form.addRow("Keyboard", keyboard_row)
        regional.content.addLayout(regional_form)
        regional.content.addWidget(wrapped_label("The console keymap is used on the text console and when a disk passphrase is requested at start-up. "
                                                 "Choosing one here also sets the keyboard layout of the desktop to match.", muted=True))
        self.keymap_note = wrapped_label("", muted=True)
        self.keymap_note.hide()
        regional.content.addWidget(self.keymap_note)
        self.content.addWidget(regional)
        self.advance_on_enter(self.hostname, self.full_name, self.username, self.password, self.password_confirm, self.root_password, self.root_confirm)
        self.load_manifest()
        self.finish_layout()

    def _show_keymap(self, keymap: str) -> None:
        """Show the manifest's console keymap; one that is not in the table is added as a flagged entry, never replaced."""
        index = self.keymap.findData(keymap)
        if index < 0:
            text = "Automatic, from the keyboard layout" if keymap in {"", "auto"} else f"{keymap} (custom value)"
            self.keymap.addItem(text, keymap)
            index = self.keymap.count() - 1
        self.keymap.setCurrentIndex(index)

    def _keymap_picked(self, _index: int = 0) -> None:
        """The user chose a console keymap: the desktop layout follows, so the passphrase prompt and the session agree."""
        console = str(self.keymap.currentData())
        layout, variant = keymaps.xkb_for_console(console)
        system = self.manifest.system
        moved = (layout, variant) != (system.keyboard_layout, system.keyboard_variant)
        system.keymap, system.keyboard_layout, system.keyboard_variant = console, layout, variant
        self.keyboardPicked.emit(layout, variant, console)
        note = ""
        if moved:
            note = f"The keyboard layout of this session is now {layout}{f' ({variant})' if variant else ''}. Type the passwords on this page again if you typed them before."
            if self.manifest.storage.luks:
                note += " The disk passphrase is asked for with this layout at every start: go back to the Storage step and type it again."
        self.keymap_note.setText(note)
        self.keymap_note.setVisible(bool(note))
        self.keyboard_summary.setText(f"Keyboard layout of the installed system: {layout}{f' ({variant})' if variant else ''}. Pick it on the welcome page.")

    def _show_passwords(self, shown: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        for edit in (self.password, self.password_confirm, self.root_password, self.root_confirm):
            edit.setEchoMode(mode)

    def load_manifest(self) -> None:
        accounts = self.manifest.accounts
        self.hostname.setText(accounts.hostname)
        self.full_name.setText(accounts.full_name)
        self.username.setText(accounts.username)
        self.password.setText(accounts.password)
        self.password_confirm.setText(accounts.password)
        self.root_password.setText(accounts.root_password)
        self.root_confirm.setText(accounts.root_password)
        self.privilege.setCurrentIndex(max(0, self.privilege.findData(accounts.privilege)))
        self.disable_root.setChecked(accounts.disable_root_login)
        self.ssh_keys.setPlainText("\n".join(accounts.ssh_authorized_keys))
        system = self.manifest.system
        self.locale_box.setCurrentText(system.locale)
        self.timezone.setCurrentText(system.timezone)
        self._show_keymap(system.keymap)
        layout = system.keyboard_layout + (f" ({system.keyboard_variant})" if system.keyboard_variant else "")
        self.keyboard_summary.setText(f"Keyboard layout of the installed system: {layout}. Pick it on the welcome page.")

    def save_manifest(self) -> None:
        accounts = self.manifest.accounts
        accounts.hostname = self.hostname.text().strip()
        accounts.full_name = self.full_name.text().strip()
        accounts.username = self.username.text().strip()
        accounts.password = self.password.text()
        accounts.root_password = self.root_password.text()
        accounts.privilege = self.privilege.currentData()
        accounts.disable_root_login = self.disable_root.isChecked()
        accounts.ssh_authorized_keys = [line.strip() for line in self.ssh_keys.toPlainText().splitlines() if line.strip()]
        self.manifest.system.locale = self.locale_box.currentText().strip()
        self.manifest.system.timezone = self.timezone.currentText().strip()
        self.manifest.system.keymap = str(self.keymap.currentData() or self.manifest.system.keymap)

    def validate_page(self) -> tuple[bool, str]:
        self.save_manifest()
        if self.password.text() != self.password_confirm.text():
            return False, "Your user passwords do not match."
        if self.root_password.text() != self.root_confirm.text():
            return False, "The root passwords do not match."
        if self.disable_root.isChecked() and "wheel" not in self.manifest.accounts.groups:
            return False, "Disabling root login requires an administrator account in the wheel group."
        sections = self.manifest.validate_sections()
        errors = sections["accounts"] + sections["locale"]
        return (False, errors[0]) if errors else (True, "")
