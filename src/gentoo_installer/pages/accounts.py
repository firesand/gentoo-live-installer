"""ACCT: the Personalize page: the daily account and root, remote login (SSH), language, time zone, keyboard and clock."""

from __future__ import annotations

import zoneinfo
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton,
    QRadioButton, QWidget,
)

from .. import account_text as text
from .. import keymaps, recommend
from ..manifest import InstallManifest
from ..system_info import SystemInfo
from ..widgets import Card, Page, wrapped_label
from .help import AdvancedSection, HelpNote


SUPPORTED_LOCALES = Path("/usr/share/i18n/SUPPORTED")
COMMON_LOCALES = ("en_US.UTF-8", "en_GB.UTF-8", "de_DE.UTF-8", "es_ES.UTF-8", "fr_FR.UTF-8", "it_IT.UTF-8", "ja_JP.UTF-8", "nl_NL.UTF-8", "pl_PL.UTF-8", "pt_BR.UTF-8", "tr_TR.UTF-8")
REGIONS = ("Africa", "America", "Antarctica", "Arctic", "Asia", "Atlantic", "Australia", "Europe", "Indian", "Pacific")
FACTORY_GROUPS = ["audio", "video", "wheel"]  # the manifest's own default, which the Guided list replaces on a fresh start
# (group, label, hint). wheel is always on: without it nobody can administer the system.
GROUPS = (
    ("wheel", "Administrator commands (wheel)", ""),
    ("audio", "Sound (audio)", ""),
    ("video", "Graphics (video)", ""),
    ("usb", "USB devices (usb)", ""),
    ("cdrom", "CD/DVD drives (cdrom)", ""),
    ("cron", "Scheduled jobs (cron)", "Only when a cron daemon is installed."),
    ("users", "Shared users group (users)", ""),
)
GUIDED_GROUPS = ["wheel", "audio", "video", "usb", "cdrom", "users"]
TOOLS = (("sudo", "sudo"), ("doas", "doas"), ("run0", "run0 (systemd only)"), ("su", "su (needs the root password)"))


def load_locale_entries(path: Path = SUPPORTED_LOCALES) -> list[tuple[str, str]]:
    """(locale name, charmap) of everything glibc can generate; a short UTF-8 list when the file is not there."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [(name, "UTF-8") for name in COMMON_LOCALES]
    found: dict[str, str] = {}
    for line in lines:
        parts = line.split()
        if len(parts) != 2 or line.startswith("#"):
            continue
        name, _, modifier = parts[0].partition("@")
        base = name.split(".")[0]
        full = f"{base}.{parts[1]}" + (f"@{modifier}" if modifier else "")
        found[full] = parts[1]
    if not found:
        return [(name, "UTF-8") for name in COMMON_LOCALES]
    return sorted(found.items(), key=lambda item: (item[0] != "en_US.UTF-8", item[0]))


def load_locales(path: Path = SUPPORTED_LOCALES) -> list[str]:
    """UTF-8 locales glibc can generate (en_US.UTF-8 first); a short list when the file is not there."""
    entries = load_locale_entries(path)
    names = [name.replace(".utf8", ".UTF-8") for name, charmap in entries if charmap == "UTF-8"]
    return names or list(COMMON_LOCALES)


def detect_timezone(root: str | Path = "/") -> str:
    """The time zone of the live session, read from files only ('' when it is not known or is plain UTC)."""
    base = Path(root)
    zone = ""
    try:
        target = (base / "etc/localtime").resolve() if (base / "etc/localtime").is_symlink() else None
        if target is not None and "zoneinfo/" in str(target):
            zone = str(target).split("zoneinfo/", 1)[1]
    except OSError:
        zone = ""
    if not zone:
        try:
            zone = (base / "etc/timezone").read_text().strip()
        except OSError:
            zone = ""
    return "" if zone in {"", "UTC", "Etc/UTC", "Etc/UCT", "Universal"} else zone


def timezone_table(include_etc: bool = False) -> dict[str, list[str]]:
    """region -> cities, from the zoneinfo database. Etc/* (with its confusing signs) only on request."""
    table: dict[str, list[str]] = {"UTC": ["UTC"]}
    for name in sorted(zoneinfo.available_timezones()):
        region, _, city = name.partition("/")
        if city and (region in REGIONS or (include_etc and region == "Etc")):
            table.setdefault(region, []).append(city)
    return table


def console_font_suggestion(info: SystemInfo | None) -> str:
    """ter-v32n for a high-density display, ter-v24n for a medium one, else '' (the default)."""
    if info is None or not info.hardware:
        return ""
    try:
        dpi = recommend.recommend_dpi(info.profile.displays)
    except Exception:  # noqa: BLE001 - a suggestion only
        return ""
    return "ter-v32n" if dpi >= 144 else "ter-v24n" if dpi >= 120 else ""


class AccountsPage(Page):
    keyboardChangeRequested = Signal()
    keyboardPicked = Signal(str, str, str)  # XKB layout, variant, console keymap: chosen here, to be applied to the session

    def __init__(self, manifest: InstallManifest, detect_zone: Callable[[], str] | None = None):
        super().__init__("Personalize your system", "Create your daily account and choose regional settings. Passwords are sent only to the privileged installer and are never written to logs.")
        self.manifest = manifest
        self.info: SystemInfo | None = None
        self._detect_zone = detect_zone or (lambda: detect_timezone())  # looked up when used, so a test can replace it
        self._extra_groups: list[str] = []
        self._table: dict[str, list[str]] = {}
        self._advanced = False
        self._auto_clock = False
        self._fresh = self._is_fresh()
        self._build_account_card()
        self._build_ssh_card()
        self._build_region_card()
        self.advance_on_enter(self.hostname, self.full_name, self.username, self.password, self.password_confirm, self.root_password, self.root_confirm)
        self.load_manifest()
        self.finish_layout()

    # --- building ---------------------------------------------------------------------------------------------

    def _build_account_card(self) -> None:
        account = Card("User accounts")
        form = QFormLayout()
        self.hostname = QLineEdit()
        self.hostname.setPlaceholderText(text.HOSTNAME_PLACEHOLDER)
        form.addRow("Computer &name", self._with_help(self.hostname, text.HOSTNAME_HELP, "Hostname"))
        self.full_name = QLineEdit()
        form.addRow("Your name (optional)", self.full_name)
        self.username = QLineEdit()
        form.addRow("&Username", self._with_help(self.username, text.USERNAME_HELP, "Users"))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("At least 8 characters")
        form.addRow("&Password", self.password)
        self.password_confirm = QLineEdit()
        self.password_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Repeat password", self.password_confirm)
        self.password_hint = wrapped_label(text.PASSWORD_HINT, muted=True)
        form.addRow("", self.password_hint)
        account.content.addLayout(form)

        account.content.addWidget(QLabel("Root (administrator) account"))
        self.root_group = QButtonGroup(self)
        self.root_buttons: dict[str, QRadioButton] = {}
        for key in text.ROOT_CHOICES:
            button = QRadioButton()
            self.root_buttons[key] = button
            self.root_group.addButton(button)
            account.content.addWidget(button)
            button.toggled.connect(lambda _=False: self._root_choice_changed())
        self.root_help = wrapped_label(text.ROOT_HELP, muted=True)
        account.content.addWidget(self.root_help)
        root_form = QFormLayout()
        self.root_password = QLineEdit()
        self.root_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.root_password.setPlaceholderText("At least 8 characters")
        self.root_password_label = QLabel("&Root password")
        self.root_password_label.setBuddy(self.root_password)
        root_form.addRow(self.root_password_label, self.root_password)
        self.root_confirm = QLineEdit()
        self.root_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self.root_confirm_label = QLabel("Repeat root password")
        root_form.addRow(self.root_confirm_label, self.root_confirm)
        account.content.addLayout(root_form)
        self.show_passwords = QCheckBox("Show the passwords while typing")
        self.show_passwords.toggled.connect(self._show_passwords)
        account.content.addWidget(self.show_passwords)
        for edit in (self.password, self.password_confirm, self.root_password, self.root_confirm):
            edit.textChanged.connect(lambda _="": self._update_password_hint())

        self.account_more = AdvancedSection("accounts", "More account options")
        more = QFormLayout()
        self.privilege = QComboBox()
        for value, label in TOOLS:
            self.privilege.addItem(label, value)
        self.privilege.setAccessibleName("Administrator command")
        self.privilege.currentIndexChanged.connect(lambda _=0: self._tool_changed())
        self.tool_help = wrapped_label("", muted=True)
        more.addRow("Administrator command", self.privilege)
        more.addRow("", self.tool_help)
        self.account_more.add_layout(more)
        self.account_more.add_widget(QLabel("This user may use"))
        self.group_checks: dict[str, QCheckBox] = {}
        for group, label, hint in GROUPS:
            check = QCheckBox(label)
            if hint:
                check.setToolTip(hint)
            self.group_checks[group] = check
            self.account_more.add_widget(check)
        self.group_checks["wheel"].setEnabled(False)
        self.group_checks["wheel"].setToolTip("Always on: without it nobody could administer the system.")
        self.extra_groups_note = wrapped_label("", muted=True)
        self.extra_groups_note.hide()
        self.account_more.add_widget(self.extra_groups_note)
        names = QFormLayout()
        self.domain = QLineEdit()
        self.domain.setPlaceholderText("none")
        names.addRow("Domain name (optional)", self._with_help(self.domain, text.DOMAIN_HELP, "Hostname"))
        self.account_more.add_layout(names)
        self.hosts_extra = QPlainTextEdit()
        self.hosts_extra.setPlaceholderText("192.168.1.10 nas nas.home")
        self.hosts_extra.setMaximumHeight(70)
        self.hosts_extra.setTabChangesFocus(True)
        hosts_row = QFormLayout()
        hosts_row.addRow("Extra /etc/hosts lines", self._with_help(self.hosts_extra, text.HOSTS_HELP, "System#The_hosts_file"))
        self.account_more.add_layout(hosts_row)
        account.content.addWidget(self.account_more)
        self.content.addWidget(account)

    def _build_ssh_card(self) -> None:
        card = Card("Remote login")
        row = QHBoxLayout()
        self.ssh_enabled = QCheckBox(text.SSH_TEXT)
        row.addWidget(self.ssh_enabled)
        row.addWidget(HelpNote(text.SSH_HELP, "Tools"))
        row.addStretch(1)
        card.content.addLayout(row)
        self.ssh_box = QWidget()
        box = QFormLayout(self.ssh_box)
        box.setContentsMargins(0, 0, 0, 0)
        self.ssh_keys = QPlainTextEdit()
        self.ssh_keys.setPlaceholderText("Optional SSH public keys, one per line")
        self.ssh_keys.setMaximumHeight(80)
        self.ssh_keys.setTabChangesFocus(True)  # Tab leaves the box instead of inserting a tab
        box.addRow("SSH authorized keys", self.ssh_keys)
        self.ssh_note = wrapped_label(text.SSH_HELP, muted=True)
        box.addRow("", self.ssh_note)
        card.content.addWidget(self.ssh_box)
        self.ssh_more = AdvancedSection("accounts-ssh", "More remote login options")
        self.ssh_keys_only = QCheckBox(text.SSH_KEYS_ONLY_TEXT)
        self.ssh_keys_only.setToolTip(text.SSH_KEYS_ONLY_HELP)
        self.ssh_more.add_widget(self.ssh_keys_only)
        self.ssh_more.add_widget(wrapped_label(text.SSH_KEYS_ONLY_HELP, muted=True))
        card.content.addWidget(self.ssh_more)
        self.ssh_enabled.toggled.connect(lambda _=False: self._ssh_changed())
        self.content.addWidget(card)

    def _build_region_card(self) -> None:
        regional = Card("Region and input")
        form = QFormLayout()
        self.locale_box = QComboBox()
        self.locale_box.setEditable(True)
        self.locale_box.addItems(load_locales())
        self.locale_box.setAccessibleName("System locale")
        self.locale_box.currentTextChanged.connect(lambda _="": self._update_locale_note())
        form.addRow("Language (locale)", self._with_help(self.locale_box, text.LOCALE_HELP, "Locale"))
        regional.content.addLayout(form)
        self.locale_note = wrapped_label(text.LOCALE_COMPAT_NOTE, muted=True)
        regional.content.addWidget(self.locale_note)

        tz_row = QHBoxLayout()
        self.tz_summary = QLabel()
        tz_row.addWidget(self.tz_summary, 1)
        self.tz_change = QPushButton("Change…")
        self.tz_change.setCheckable(True)
        self.tz_change.toggled.connect(self._tz_pickers_shown)
        tz_row.addWidget(self.tz_change)
        tz_row.addWidget(HelpNote(text.TIMEZONE_HELP, "Timezone"))
        regional.content.addLayout(tz_row)
        self.tz_pickers = QWidget()
        pickers = QHBoxLayout(self.tz_pickers)
        pickers.setContentsMargins(0, 0, 0, 0)
        self.tz_region = QComboBox()
        self.tz_region.setAccessibleName("Time zone region")
        self.tz_city = QComboBox()
        self.tz_city.setAccessibleName("Time zone city")
        self.tz_region.currentIndexChanged.connect(lambda _=0: self._region_changed())
        self.tz_city.currentIndexChanged.connect(lambda _=0: self._update_tz_summary())
        pickers.addWidget(self.tz_region)
        pickers.addWidget(self.tz_city, 1)
        self.tz_pickers.hide()
        regional.content.addWidget(self.tz_pickers)
        self.timezone_note = wrapped_label("", muted=True)
        self.timezone_note.hide()
        regional.content.addWidget(self.timezone_note)
        self.etc_warning = wrapped_label(text.ETC_SIGN_WARNING, muted=True)
        self.etc_warning.hide()
        regional.content.addWidget(self.etc_warning)
        self.rtc_local = QCheckBox(text.RTC_LOCAL_TEXT)
        self.rtc_local.setToolTip("Windows keeps local time in the hardware clock, Linux normally keeps UTC. With one choice for both, the time stays right after switching systems.")
        self.rtc_local.toggled.connect(lambda _=False: self._update_clock_hint())
        regional.content.addWidget(self.rtc_local)
        self.clock_hint = wrapped_label("", muted=True)
        self.clock_hint.hide()
        regional.content.addWidget(self.clock_hint)

        keyboard_row = QHBoxLayout()
        self.keyboard_summary = QLabel()
        self.keyboard_summary.setProperty("muted", True)
        self.keyboard_summary.setWordWrap(True)
        keyboard_row.addWidget(self.keyboard_summary, 1)
        change = QPushButton("Change keyboard layout…")
        change.clicked.connect(self.keyboardChangeRequested)
        keyboard_row.addWidget(change)
        keyboard_form = QFormLayout()
        keyboard_form.addRow("Keyboard", keyboard_row)
        regional.content.addLayout(keyboard_form)

        self.region_more = AdvancedSection("accounts-region", "More region options")
        more = QFormLayout()
        self.keymap = QComboBox()
        self.keymap.setAccessibleName("Console keymap")
        for console, label in keymaps.console_choices():
            self.keymap.addItem(f"{label} ({console})", console)
        self.keymap.activated.connect(self._keymap_picked)
        more.addRow("Console keymap", self.keymap)
        self.console_font = QComboBox()
        self.console_font.setAccessibleName("Console font")
        for value, label in text.CONSOLE_FONTS:
            self.console_font.addItem(label, value)
        more.addRow("Console font", self._with_help(self.console_font, text.CONSOLE_FONT_HELP, "System"))
        self.region_more.add_layout(more)
        self.region_more.add_widget(wrapped_label("The console keymap is used on the text console and when a disk passphrase is requested at start-up. "
                                                  "Choosing one here also sets the keyboard layout of the desktop to match.", muted=True))
        self.keymap_note = wrapped_label("", muted=True)
        self.keymap_note.hide()
        self.region_more.add_widget(self.keymap_note)
        self.font_note = wrapped_label("", muted=True)
        self.font_note.hide()
        self.region_more.add_widget(self.font_note)
        self.region_more.add_widget(QLabel("Locales to generate"))
        self.locale_filter = QLineEdit()
        self.locale_filter.setPlaceholderText("Search, for example fr_ or Japanese")
        self.locale_filter.textChanged.connect(self._filter_locales)
        self.region_more.add_widget(self.locale_filter)
        self.extra_locales = QListWidget()
        self.extra_locales.setMaximumHeight(140)
        self.extra_locales.setAccessibleName("Locales to generate")
        self.region_more.add_widget(self.extra_locales)
        self.legacy_charsets = QCheckBox("Show older character sets (not UTF-8)")
        self.legacy_charsets.toggled.connect(lambda _=False: self._fill_locale_list())
        self.region_more.add_widget(self.legacy_charsets)
        self.legacy_warning = wrapped_label(text.LEGACY_CHARSET_WARNING, muted=True)
        self.legacy_warning.hide()
        self.region_more.add_widget(self.legacy_warning)
        self.etc_zones = QCheckBox("Show the Etc/GMT zones")
        self.etc_zones.toggled.connect(lambda _=False: self._fill_regions(self.timezone_value()))
        self.region_more.add_widget(self.etc_zones)
        regional.content.addWidget(self.region_more)
        self.content.addWidget(regional)
        self._locale_entries = load_locale_entries()
        self._checked_extra: set[str] = set()

    @staticmethod
    def _with_help(widget: QWidget, note: str, anchor: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widget, 1)
        layout.addWidget(HelpNote(note, anchor))
        return row

    # --- freshness: what a first visit may pre-select -------------------------------------------------------------

    def _is_fresh(self) -> bool:
        """A manifest nobody has edited yet: the Guided defaults (root login off, the wider group list) may be pre-selected.
        A loaded or already-edited manifest shows exactly what it holds."""
        accounts = self.manifest.accounts
        return (not accounts.password and not accounts.root_password and accounts.root_mode == "separate"
                and list(accounts.groups) == FACTORY_GROUPS and accounts.privilege == "sudo")

    # --- information from the rest of the window ---------------------------------------------------------------

    def set_info(self, info: SystemInfo) -> None:
        """What was found on this computer decides the clock advice and the console font suggestion."""
        self.info = info
        suggest = False
        if info.hardware:
            profile = info.profile
            suggest = bool(profile.firmware.windows_boot_entry) or profile.clock.rtc_mode == "local"
        if suggest and not self.rtc_local.isChecked() and not self._auto_clock:
            self._auto_clock = True  # once: Windows is there, so the clock is set to local time; the user can still untick it
            self.manifest.system.rtc_local = True  # also in the manifest: the page reloads it whenever it is shown
            self.rtc_local.setChecked(True)
        self._update_clock_hint()
        self._update_font_note()

    def _update_clock_hint(self) -> None:
        shown = self._auto_clock and self.rtc_local.isChecked()
        self.clock_hint.setText("Windows was found on this computer, or its clock already keeps local time, so this is ticked for you. Untick it only if you are sure." if shown else "")
        self.clock_hint.setVisible(shown)

    def _update_font_note(self) -> None:
        suggestion = console_font_suggestion(self.info)
        label = dict(text.CONSOLE_FONTS).get(suggestion, "")
        shown = bool(suggestion) and not self.console_font.currentData()
        self.font_note.setText(f"This display has a high resolution: {label} makes the text screen easier to read." if shown else "")
        self.font_note.setVisible(shown)

    def refresh(self) -> None:
        """The init system, desktop or tools may have changed on other pages: update what depends on them."""
        model = self.privilege.model()
        systemd = self.manifest.system.init == "systemd"
        if isinstance(model, QStandardItemModel):
            item = model.item(self.privilege.findData("run0"))
            if item is not None:
                item.setEnabled(systemd)
        cron = self.cron_installed()
        self.group_checks["cron"].setEnabled(cron)
        if not cron:
            self.group_checks["cron"].setChecked(False)
        self._tool_changed()

    def showEvent(self, event) -> None:
        self.refresh()
        super().showEvent(event)

    def cron_installed(self) -> bool:
        options = self.manifest.options
        return options.cron_enabled or (options.logger == "syslog-ng" and self.manifest.system.init == "openrc")

    def set_advanced(self, advanced: bool) -> None:
        self._advanced = advanced
        for section in (self.account_more, self.ssh_more, self.region_more):
            section.set_advanced(advanced)
        self._fill_regions(self.timezone_value())

    # --- passwords and root -----------------------------------------------------------------------------------

    def root_choice(self) -> str:
        return next((key for key, button in self.root_buttons.items() if button.isChecked()), "separate")

    def _tool(self) -> str:
        return str(self.privilege.currentData() or "sudo")

    def _tool_changed(self) -> None:
        tool = self._tool()
        for key, button in self.root_buttons.items():
            label, detail = text.root_choice_text(key, tool)
            button.setText(label)
            button.setToolTip(detail)
            button.setAccessibleDescription(detail)
        self.tool_help.setText(text.TOOL_HELP.get(tool, ""))
        self.root_buttons["off"].setEnabled(tool != "su")
        if tool == "su" and self.root_buttons["off"].isChecked():
            self.root_buttons["separate"].setChecked(True)

    def _root_choice_changed(self) -> None:
        separate = self.root_choice() == "separate"
        for widget in (self.root_password, self.root_confirm, self.root_password_label, self.root_confirm_label):
            widget.setVisible(separate)
        self._update_password_hint()

    def _update_password_hint(self) -> None:
        root = self.root_password.text() if self.root_choice() == "separate" else ""
        ok, line = text.password_hint(self.password.text(), root)
        confirm = self.password_confirm.text()
        if ok and confirm and confirm != self.password.text():
            ok, line = False, text.PASSWORDS_DIFFER
        elif ok and confirm and confirm == self.password.text() and line == "Good.":
            line = "Good. The passwords match."
        self.password_hint.setText(("OK: " if ok and not line.startswith("Your password and") else "") + line if self.password.text() else text.PASSWORD_HINT)
        self.password_hint.setProperty("invalid", bool(self.password.text()) and not ok)
        self.password_hint.style().unpolish(self.password_hint)
        self.password_hint.style().polish(self.password_hint)

    def _show_passwords(self, shown: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        for edit in (self.password, self.password_confirm, self.root_password, self.root_confirm):
            edit.setEchoMode(mode)

    # --- ssh -----------------------------------------------------------------------------------------------------

    def _ssh_changed(self) -> None:
        on = self.ssh_enabled.isChecked()
        self.ssh_box.setVisible(on)
        self.ssh_more.setEnabled(on)

    # --- time zone ------------------------------------------------------------------------------------------------

    def timezone_value(self) -> str:
        region, city = str(self.tz_region.currentData() or ""), str(self.tz_city.currentData() or "")
        if not region:
            return "UTC"
        return city if region == "UTC" else f"{region}/{city}"

    def set_timezone(self, zone: str) -> None:
        self._fill_regions(zone)

    def _fill_regions(self, zone: str) -> None:
        """Rebuild the region list. Etc is offered in Advanced (or when asked for, or when the chosen zone is one); a zone that is
        not in the database stays visible under its own region so loading never loses it."""
        include_etc = self._advanced or self.etc_zones.isChecked() or zone.startswith("Etc/")
        table = timezone_table(include_etc)
        region, _, city = zone.partition("/")
        if zone == "UTC" or not city:
            region, city = "UTC", "UTC"
        if city not in table.get(region, []):
            table.setdefault(region, []).append(city)
        for combo in (self.tz_region, self.tz_city):
            combo.blockSignals(True)
        self.tz_region.clear()
        for name in table:
            self.tz_region.addItem("UTC" if name == "UTC" else name, name)
        self.tz_region.setCurrentIndex(max(0, self.tz_region.findData(region)))
        self._fill_cities(table, region, city)
        for combo in (self.tz_region, self.tz_city):
            combo.blockSignals(False)
        self._table = table
        self._update_tz_summary()

    def _fill_cities(self, table: dict[str, list[str]], region: str, city: str) -> None:
        self.tz_city.clear()
        for name in table.get(region, []):
            self.tz_city.addItem(name.replace("_", " "), name)
        self.tz_city.setCurrentIndex(max(0, self.tz_city.findData(city)))

    def _region_changed(self) -> None:
        region = str(self.tz_region.currentData() or "")
        self.tz_city.blockSignals(True)
        self._fill_cities(self._table, region, "")
        self.tz_city.blockSignals(False)
        self._update_tz_summary()

    def _update_tz_summary(self) -> None:
        zone = self.timezone_value()
        self.tz_summary.setText(f"Time zone: {zone}")
        self.etc_warning.setVisible(zone.startswith("Etc/"))

    def _tz_pickers_shown(self, shown: bool) -> None:
        self.tz_pickers.setVisible(shown)
        self.tz_change.setText("Done" if shown else "Change…")

    # --- locale ----------------------------------------------------------------------------------------------------

    def _update_locale_note(self) -> None:
        name = self.locale_box.currentText().strip()
        if name in {"C", "POSIX"}:
            self.locale_note.setText(f"{name} is not a language: programs show English text and cannot display accents or other scripts. Choose a UTF-8 locale for daily use.")
        elif name == "en_US.UTF-8":
            self.locale_note.setText("")
        else:
            self.locale_note.setText(text.LOCALE_COMPAT_NOTE)
        self.locale_note.setVisible(bool(self.locale_note.text()))

    def _fill_locale_list(self) -> None:
        legacy = self.legacy_charsets.isChecked()
        self.legacy_warning.setVisible(legacy)
        checked = self._checked_locales()
        self.extra_locales.clear()
        names = [name for name, charmap in self._locale_entries if legacy or charmap == "UTF-8"]
        names += sorted(checked - set(names))  # a chosen locale stays on the list whatever the filter shows
        for name in names:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in checked else Qt.CheckState.Unchecked)
            self.extra_locales.addItem(item)
        self._filter_locales(self.locale_filter.text())

    def _checked_locales(self) -> set[str]:
        shown = {self.extra_locales.item(i).text(): self.extra_locales.item(i).checkState() == Qt.CheckState.Checked for i in range(self.extra_locales.count())}
        return {name for name in self._checked_extra if name not in shown} | {name for name, on in shown.items() if on}

    def _filter_locales(self, query: str) -> None:
        query = query.strip().lower()
        for i in range(self.extra_locales.count()):
            item = self.extra_locales.item(i)
            item.setHidden(bool(query) and query not in item.text().lower() and item.checkState() != Qt.CheckState.Checked)

    # --- keymap ------------------------------------------------------------------------------------------------------

    def _show_keymap(self, keymap: str) -> None:
        """Show the manifest's console keymap; one that is not in the table is added as a flagged entry, never replaced."""
        index = self.keymap.findData(keymap)
        if index < 0:
            label = "Automatic, from the keyboard layout" if keymap in {"", "auto"} else f"{keymap} (custom value)"
            self.keymap.addItem(label, keymap)
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
        if note:
            self.region_more.set_open(True)  # the note belongs to the combo the user just used
        self.keymap_note.setVisible(bool(note))
        self.keyboard_summary.setText(f"Keyboard layout of the installed system: {layout}{f' ({variant})' if variant else ''}. Pick it on the welcome page.")

    # --- groups ------------------------------------------------------------------------------------------------------

    def _load_groups(self, groups: list[str]) -> None:
        for group, check in self.group_checks.items():
            check.setChecked(group in groups or group == "wheel")
        known = {group for group, _, _ in GROUPS}
        extra = [group for group in groups if group not in known]
        self.extra_groups_note.setText("Also kept: " + ", ".join(extra) if extra else "")
        self.extra_groups_note.setVisible(bool(extra))
        self._extra_groups = extra

    def _chosen_groups(self) -> list[str]:
        chosen = {group for group, check in self.group_checks.items() if check.isChecked() and (group != "cron" or self.cron_installed())}
        kept = [group for group in self.manifest.accounts.groups if group in chosen or group in self._extra_groups]  # the order of the loaded list stays
        return list(dict.fromkeys([*kept, *(group for group, _, _ in GROUPS if group in chosen)]))

    # --- manifest ----------------------------------------------------------------------------------------------------

    def load_manifest(self) -> None:
        accounts = self.manifest.accounts
        system = self.manifest.system
        fresh = self._fresh
        self.hostname.setText(accounts.hostname)
        self.full_name.setText(accounts.full_name)
        self.username.setText(accounts.username)
        self.password.setText(accounts.password)
        self.password_confirm.setText(accounts.password)
        self.root_password.setText(accounts.root_password)
        self.root_confirm.setText(accounts.root_password)
        self.privilege.setCurrentIndex(max(0, self.privilege.findData(accounts.privilege)))
        root = "off" if fresh else text.CHOICE_OF_ROOT_MODE.get("locked" if accounts.root_locked() else accounts.root_mode, "separate")
        self.root_buttons[root].setChecked(True)
        self._tool_changed()
        self._root_choice_changed()
        self._load_groups([*GUIDED_GROUPS, *(["cron"] if self.cron_installed() else [])] if fresh else list(accounts.groups))
        self.domain.setText(system.domain)
        self.hosts_extra.setPlainText("\n".join(system.hosts_extra))
        self.ssh_enabled.setChecked(self.manifest.options.ssh)
        self.ssh_keys.setPlainText("\n".join(accounts.ssh_authorized_keys))
        self.ssh_keys_only.setChecked(not accounts.ssh_password_login)
        self._ssh_changed()
        self.locale_box.setCurrentText(system.locale)
        self._update_locale_note()
        self._checked_extra = set(system.extra_locales)
        self._fill_locale_list()
        zone = system.timezone
        note = ""
        if zone == "UTC":
            detected = self._detect_zone()
            if detected:
                zone, note = detected, f"Detected from this session: {detected}. Change it if that is not where you are."
            elif fresh:
                note = text.TIMEZONE_FALLBACK
        self.timezone_note.setText(note)
        self.timezone_note.setVisible(bool(note))
        self.set_timezone(zone)
        if note == text.TIMEZONE_FALLBACK:
            self.tz_change.setChecked(True)
        self._show_keymap(system.keymap)
        self.rtc_local.setChecked(system.rtc_local)
        self.console_font.setCurrentIndex(self._font_index(system.console_font))
        self._update_font_note()
        layout = system.keyboard_layout + (f" ({system.keyboard_variant})" if system.keyboard_variant else "")
        self.keyboard_summary.setText(f"Keyboard layout of the installed system: {layout}. Pick it on the welcome page.")
        self._update_password_hint()
        self.refresh()

    def _font_index(self, font: str) -> int:
        index = self.console_font.findData(font)
        if index < 0:  # a font from a settings file is shown, never replaced
            self.console_font.addItem(f"{font} (custom value)", font)
            index = self.console_font.count() - 1
        return index

    def save_manifest(self) -> None:
        accounts = self.manifest.accounts
        system = self.manifest.system
        accounts.hostname = self.hostname.text().strip()
        accounts.full_name = self.full_name.text().strip()
        accounts.username = self.username.text().strip()
        accounts.password = self.password.text()
        choice = self.root_choice()
        accounts.root_mode = text.ROOT_MODE_OF_CHOICE[choice]
        accounts.root_password = accounts.password if choice == "same" else self.root_password.text()
        accounts.privilege = self._tool()
        accounts.groups = self._chosen_groups()
        accounts.ssh_authorized_keys = [line.strip() for line in self.ssh_keys.toPlainText().splitlines() if line.strip()]
        accounts.ssh_password_login = not self.ssh_keys_only.isChecked()
        self.manifest.options.ssh = self.ssh_enabled.isChecked()
        system.domain = self.domain.text().strip()
        system.hosts_extra = [" ".join(line.split()) for line in self.hosts_extra.toPlainText().splitlines() if line.strip()]
        system.locale = self.locale_box.currentText().strip()
        self._checked_extra = self._checked_locales()
        system.extra_locales = sorted(name for name in self._checked_extra if name != system.locale)
        system.timezone = self.timezone_value()
        system.keymap = str(self.keymap.currentData() or system.keymap)
        system.rtc_local = self.rtc_local.isChecked()
        system.console_font = str(self.console_font.currentData() or "")

    def validate_page(self) -> tuple[bool, str]:
        self.save_manifest()
        accounts = self.manifest.accounts
        if self.password.text() != self.password_confirm.text():
            return False, "Your user passwords do not match."
        if self.root_choice() == "separate" and self.root_password.text() != self.root_confirm.text():
            return False, "The root passwords do not match."
        if accounts.root_locked() and "wheel" not in accounts.groups:
            return False, "Turning root login off needs an administrator account in the wheel group."
        sections = self.manifest.validate_sections()
        errors = sections["accounts"] + sections["locale"]
        if not errors:
            from gentoo_installerd.configure_system import account_problems  # lazy: the daemon's own list of choices it cannot carry out

            errors = account_problems(self.manifest)
        return (False, errors[0]) if errors else (True, "")
