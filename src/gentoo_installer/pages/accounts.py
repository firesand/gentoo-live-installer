from __future__ import annotations

import zoneinfo

from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QLineEdit, QPlainTextEdit

from ..manifest import InstallManifest
from ..widgets import Card, Page


class AccountsPage(Page):
    def __init__(self, manifest: InstallManifest):
        super().__init__("Personalize your system", "Create your daily account and choose regional settings. Passwords are sent only to the privileged installer and are never written to logs.")
        self.manifest = manifest
        account = Card("User accounts")
        form = QFormLayout()
        self.hostname = QLineEdit()
        form.addRow("Computer name", self.hostname)
        self.full_name = QLineEdit()
        form.addRow("Your name (optional)", self.full_name)
        self.username = QLineEdit()
        form.addRow("Username", self.username)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("At least 8 characters")
        form.addRow("Password", self.password)
        self.password_confirm = QLineEdit()
        self.password_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Repeat password", self.password_confirm)
        self.root_password = QLineEdit()
        self.root_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.root_password.setPlaceholderText("At least 8 characters")
        form.addRow("Root password", self.root_password)
        self.root_confirm = QLineEdit()
        self.root_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Repeat root password", self.root_confirm)
        self.privilege = QComboBox()
        self.privilege.addItem("sudo", "sudo")
        self.privilege.addItem("doas", "doas")
        form.addRow("Administrator command", self.privilege)
        self.disable_root = QCheckBox("Disable direct root login after creating the administrator account")
        form.addRow(self.disable_root)
        self.ssh_keys = QPlainTextEdit()
        self.ssh_keys.setPlaceholderText("Optional SSH public keys, one per line")
        self.ssh_keys.setMaximumHeight(80)
        form.addRow("SSH authorized keys", self.ssh_keys)
        account.layout.addLayout(form)
        self.layout.addWidget(account)

        regional = Card("Region and input")
        regional_form = QFormLayout()
        self.locale = QComboBox()
        self.locale.setEditable(True)
        self.locale.addItems(["en_US.UTF-8", "en_GB.UTF-8", "de_DE.UTF-8", "es_ES.UTF-8", "fr_FR.UTF-8", "it_IT.UTF-8", "ja_JP.UTF-8", "nl_NL.UTF-8", "pl_PL.UTF-8", "pt_BR.UTF-8", "tr_TR.UTF-8"])
        regional_form.addRow("System locale", self.locale)
        self.timezone = QComboBox()
        self.timezone.setEditable(True)
        zones = sorted(zoneinfo.available_timezones())
        self.timezone.addItems(zones)
        self.timezone.setCurrentText("UTC")
        regional_form.addRow("Timezone", self.timezone)
        self.keymap = QComboBox()
        self.keymap.setEditable(True)
        self.keymap.addItems(["us", "gb", "de", "es", "fr", "it", "jp106", "nl", "pl", "pt", "trq"])
        regional_form.addRow("Console keymap", self.keymap)
        regional.layout.addLayout(regional_form)
        self.layout.addWidget(regional)
        self.load_manifest()

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
        self.locale.setCurrentText(self.manifest.system.locale)
        self.timezone.setCurrentText(self.manifest.system.timezone)
        self.keymap.setCurrentText(self.manifest.system.keymap)

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
        self.manifest.system.locale = self.locale.currentText().strip()
        self.manifest.system.timezone = self.timezone.currentText().strip()
        self.manifest.system.keymap = self.keymap.currentText().strip()

    def validate_page(self) -> tuple[bool, str]:
        self.save_manifest()
        if self.password.text() != self.password_confirm.text():
            return False, "Your user passwords do not match."
        if self.root_password.text() != self.root_confirm.text():
            return False, "The root passwords do not match."
        if self.disable_root.isChecked() and "wheel" not in self.manifest.accounts.groups:
            return False, "Disabling root login requires an administrator account in the wheel group."
        errors = [error for error in self.manifest.validate() if any(word in error.lower() for word in ("password", "username", "hostname", "timezone", "privilege"))]
        return (False, errors[0]) if errors else (True, "")
