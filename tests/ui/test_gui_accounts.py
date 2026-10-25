"""ACCT GUI: the Personalize page and the Applications && tools tab (offscreen)."""

from __future__ import annotations

import pytest

from gentoo_installer import account_text as text
from gentoo_installer.manifest import InstallManifest
from gentoo_installer.pages import accounts as accounts_page
from gentoo_installer.pages.accounts import AccountsPage, console_font_suggestion, detect_timezone, timezone_table

from tests.ui.conftest import sample_info


@pytest.fixture
def page(qtbot):
    widget = AccountsPage(InstallManifest())
    qtbot.addWidget(widget)
    widget.show()
    return widget


def used(manifest: InstallManifest | None = None, qtbot=None, **kwargs):
    widget = AccountsPage(manifest or InstallManifest(), **kwargs)
    qtbot.addWidget(widget)
    widget.show()
    return widget


def fill(page: AccountsPage, user="user-password-1", root="root-password-1") -> None:
    page.password.setText(user)
    page.password_confirm.setText(user)
    page.root_password.setText(root)
    page.root_confirm.setText(root)


# --- ACCT-01 time zone ---------------------------------------------------------------------------------


def test_the_time_zone_is_detected_and_shown_with_change(qtbot):
    widget = used(qtbot=qtbot, detect_zone=lambda: "Europe/Berlin")
    assert widget.tz_summary.text() == "Time zone: Europe/Berlin"
    assert "Detected" in widget.timezone_note.text() and not widget.tz_pickers.isVisibleTo(widget)
    widget.tz_change.click()
    assert widget.tz_pickers.isVisibleTo(widget)
    widget.save_manifest()
    assert widget.manifest.system.timezone == "Europe/Berlin"


def test_detection_failure_falls_back_to_utc_with_a_visible_note(qtbot):
    widget = used(qtbot=qtbot, detect_zone=lambda: "")
    assert widget.timezone_value() == "UTC"
    assert widget.timezone_note.isVisibleTo(widget) and widget.timezone_note.text() == text.TIMEZONE_FALLBACK
    assert widget.tz_pickers.isVisibleTo(widget)  # the pickers are open so the choice is one click away


def test_region_then_city_choose_the_zone(page):
    page.tz_region.setCurrentIndex(page.tz_region.findData("Asia"))
    page.tz_city.setCurrentIndex(page.tz_city.findData("Tokyo"))
    page.save_manifest()
    assert page.manifest.system.timezone == "Asia/Tokyo"
    assert page.tz_summary.text() == "Time zone: Asia/Tokyo"


def test_a_three_part_zone_round_trips(qtbot):
    manifest = InstallManifest()
    manifest.system.timezone = "America/Argentina/Buenos_Aires"
    widget = used(manifest, qtbot)
    widget.save_manifest()
    assert manifest.system.timezone == "America/Argentina/Buenos_Aires"


def test_etc_zones_are_hidden_in_guided_and_shown_in_advanced(page):
    assert "Etc" not in [page.tz_region.itemData(i) for i in range(page.tz_region.count())]
    assert "Etc" not in timezone_table() and "Etc" in timezone_table(True)
    page.set_advanced(True)
    assert "Etc" in [page.tz_region.itemData(i) for i in range(page.tz_region.count())]
    page.tz_region.setCurrentIndex(page.tz_region.findData("Etc"))
    assert page.etc_warning.isVisibleTo(page) and "BEHIND" in page.etc_warning.text()
    page.set_advanced(False)
    assert page.timezone_value().startswith("Etc/")  # nothing is lost when switching back


def test_a_loaded_etc_zone_is_kept_in_guided(qtbot):
    manifest = InstallManifest()
    manifest.system.timezone = "Etc/GMT+5"
    widget = used(manifest, qtbot)
    widget.save_manifest()
    assert manifest.system.timezone == "Etc/GMT+5"


def test_an_invalid_zone_is_rejected(qtbot):
    manifest = InstallManifest()
    manifest.system.timezone = "Mars/Olympus"
    widget = used(manifest, qtbot)
    fill(widget)
    ok, message = widget.validate_page()
    assert not ok and "timezone" in message.lower()


def test_detect_timezone_reads_files_only(tmp_path):
    assert detect_timezone(tmp_path) == ""
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/timezone").write_text("Europe/Paris\n")
    assert detect_timezone(tmp_path) == "Europe/Paris"
    (tmp_path / "etc/timezone").write_text("Etc/UTC\n")
    assert detect_timezone(tmp_path) == ""
    zones = tmp_path / "usr/share/zoneinfo/Asia"
    zones.mkdir(parents=True)
    (zones / "Tokyo").write_text("x")
    (tmp_path / "etc/localtime").symlink_to(zones / "Tokyo")
    assert detect_timezone(tmp_path) == "Asia/Tokyo"


def test_windows_ticks_the_local_clock_once(make_window):
    from tests.hardware.helpers import fixture_dict

    window = make_window(info=sample_info(hardware=fixture_dict("thinkpad-intel-nvidia-hybrid")))
    window._show_page(5)
    assert window.accounts.rtc_local.isChecked() and window.manifest.system.rtc_local
    quiet = make_window(info=sample_info(hardware=fixture_dict("qemu-q35-virtio")))
    quiet._show_page(5)
    assert not quiet.accounts.rtc_local.isChecked()


def test_the_keyboard_is_read_only_here_and_the_console_keymap_is_advanced(page):
    assert not hasattr(page.keyboard_summary, "textEdited")
    assert "Keyboard layout of the installed system" in page.keyboard_summary.text()
    assert not page.keymap.isVisibleTo(page)
    page.set_advanced(True)
    assert page.keymap.isVisibleTo(page)


# --- ACCT-02 locales -------------------------------------------------------------------------------------


def test_the_compatibility_note_and_extra_locales(page, qtbot):
    page.locale_box.setCurrentText("de_DE.UTF-8")
    assert text.LOCALE_COMPAT_NOTE in page.locale_note.text()
    page.set_advanced(True)
    items = {page.extra_locales.item(i).text(): page.extra_locales.item(i) for i in range(page.extra_locales.count())}
    assert "fr_FR.UTF-8" in items
    from PySide6.QtCore import Qt

    items["fr_FR.UTF-8"].setCheckState(Qt.CheckState.Checked)
    page.save_manifest()
    assert page.manifest.system.extra_locales == ["fr_FR.UTF-8"]
    page.locale_box.setCurrentText("fr_FR.UTF-8")
    page.save_manifest()
    assert page.manifest.system.extra_locales == []  # the main locale is not repeated


def test_legacy_charsets_are_hidden_until_asked_for_and_warn(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(accounts_page, "load_locale_entries", lambda path=None: [("en_US.UTF-8", "UTF-8"), ("en_US.ISO-8859-1", "ISO-8859-1"), ("de_DE.UTF-8", "UTF-8")])
    widget = used(qtbot=qtbot)
    names = lambda: [widget.extra_locales.item(i).text() for i in range(widget.extra_locales.count())]  # noqa: E731
    assert "en_US.ISO-8859-1" not in names() and not widget.legacy_warning.isVisibleTo(widget)
    widget.region_more.set_open(True)
    widget.legacy_charsets.setChecked(True)
    assert "en_US.ISO-8859-1" in names() and "UTF-8" in widget.legacy_warning.text()


def test_load_locale_entries_keeps_charmaps_and_modifiers(tmp_path):
    path = tmp_path / "SUPPORTED"
    path.write_text("# comment\nde_DE.UTF-8 UTF-8\nde_DE ISO-8859-1\nde_DE@euro ISO-8859-15\n")
    entries = dict(accounts_page.load_locale_entries(path))
    assert entries == {"de_DE.UTF-8": "UTF-8", "de_DE.ISO-8859-1": "ISO-8859-1", "de_DE.ISO-8859-15@euro": "ISO-8859-15"}
    assert accounts_page.load_locales(path) == ["de_DE.UTF-8"]


def test_c_and_posix_are_flagged_on_the_page(page):
    page.locale_box.setCurrentText("C")
    assert "not a language" in page.locale_note.text() and page.locale_note.isVisibleTo(page)


# --- ACCT-09 console font ---------------------------------------------------------------------------------


def test_the_console_font_reaches_the_manifest(page):
    page.set_advanced(True)
    page.console_font.setCurrentIndex(page.console_font.findData("ter-v24n"))
    page.save_manifest()
    assert page.manifest.system.console_font == "ter-v24n"
    page.console_font.setCurrentIndex(0)
    page.save_manifest()
    assert page.manifest.system.console_font == ""


def test_a_high_density_display_suggests_terminus_32(page):
    from tests.hardware.helpers import fixture_dict

    info = sample_info(hardware=fixture_dict("thinkpad-intel-nvidia-hybrid"))
    from gentoo_installer import recommend

    dpi = recommend.recommend_dpi(info.profile.displays)
    expected = "ter-v32n" if dpi >= 144 else "ter-v24n" if dpi >= 120 else ""
    assert console_font_suggestion(info) == expected
    assert console_font_suggestion(None) == "" and console_font_suggestion(sample_info()) == ""


def test_hidpi_fixture_gives_a_32_suggestion_and_note(page, monkeypatch):
    monkeypatch.setattr(accounts_page.recommend, "recommend_dpi", lambda displays: 192)
    from tests.hardware.helpers import fixture_dict

    info = sample_info(hardware=fixture_dict("thinkpad-intel-nvidia-hybrid"))
    assert console_font_suggestion(info) == "ter-v32n"
    page.set_info(info)
    assert "Terminus 32" in page.font_note.text()


# --- ACCT-03 root and passwords ---------------------------------------------------------------------------


def test_a_fresh_start_turns_root_login_off_and_uses_the_wider_groups(page):
    assert page.root_choice() == "off" and not page.root_password.isVisibleTo(page)
    page.password.setText("a-long-password-1")
    page.password_confirm.setText("a-long-password-1")
    page.save_manifest()
    accounts = page.manifest.accounts
    assert accounts.root_mode == "locked" and accounts.disable_root_login
    assert set(accounts.groups) >= {"wheel", "audio", "video", "usb", "cdrom", "users"}
    ok, message = page.validate_page()
    assert ok, message


def test_a_loaded_manifest_is_shown_exactly_as_it_is(qtbot):
    manifest = InstallManifest()
    manifest.accounts.password = manifest.accounts.root_password = "long-password-1"
    manifest.accounts.root_mode = "separate"
    manifest.accounts.groups = ["wheel", "audio", "libvirt"]
    widget = used(manifest, qtbot)
    assert widget.root_choice() == "separate" and widget.root_password.isVisibleTo(widget)
    widget.save_manifest()
    assert manifest.accounts.root_mode == "separate" and manifest.accounts.groups == ["wheel", "audio", "libvirt"]


def test_the_three_root_choices_write_root_mode(page):
    fill(page)
    for choice, mode in (("same", "same"), ("separate", "separate"), ("off", "locked")):
        page.root_buttons[choice].setChecked(True)
        page.save_manifest()
        assert page.manifest.accounts.root_mode == mode
    page.root_buttons["same"].setChecked(True)
    page.save_manifest()
    assert page.manifest.accounts.root_password == page.manifest.accounts.password


def test_the_root_choice_wording_comes_from_one_module_and_names_the_tool(page):
    label, _ = text.root_choice_text("off", "doas")
    page.set_advanced(True)
    page.privilege.setCurrentIndex(page.privilege.findData("doas"))
    assert page.root_buttons["off"].text() == label and "doas" in label
    assert "recommended" in page.root_buttons["off"].text()


def test_the_textui_uses_the_same_sentences():
    from gentoo_installer.textui import wizard

    source = open(wizard.__file__).read()
    assert "account_text" in source


def test_su_cannot_be_combined_with_a_locked_root(page):
    page.set_advanced(True)
    fill(page)
    page.root_buttons["off"].setChecked(True)
    page.privilege.setCurrentIndex(page.privilege.findData("su"))
    assert page.root_choice() == "separate" and not page.root_buttons["off"].isEnabled()


def test_run0_needs_systemd(qtbot):
    manifest = InstallManifest()
    widget = used(manifest, qtbot)
    index = widget.privilege.findData("run0")
    assert not widget.privilege.model().item(index).isEnabled()
    manifest.system.init = "systemd"
    widget.refresh()
    assert widget.privilege.model().item(index).isEnabled()


def test_equal_passwords_for_user_and_root_are_warned_about_inline(page):
    page.root_buttons["separate"].setChecked(True)
    page.password.setText("the-same-password")
    page.root_password.setText("the-same-password")
    assert text.PASSWORD_SAME_AS_ROOT in page.password_hint.text()
    page.root_password.setText("another-password-2")
    assert text.PASSWORD_SAME_AS_ROOT not in page.password_hint.text()


def test_the_password_hint_is_inline_and_8_stays_the_minimum(page):
    assert page.password_hint.text() == text.PASSWORD_HINT
    page.password.setText("short")
    assert page.password_hint.text() == text.PASSWORD_TOO_SHORT and page.password_hint.property("invalid")
    page.password.setText("exactly8")
    assert not page.password_hint.property("invalid")
    assert text.password_hint("x" * 16)[1] == "Good."
    assert text.password_hint("")[1] == text.PASSWORD_HINT


def test_hostname_has_a_placeholder_and_help(page):
    assert page.hostname.placeholderText() == "tux"
    assert "Letters, digits and hyphens only" in text.HOSTNAME_HELP


def test_locked_root_requires_wheel(page):
    page.set_advanced(True)
    fill(page)
    page.root_buttons["off"].setChecked(True)
    page.group_checks["wheel"].setEnabled(True)
    page.group_checks["wheel"].setChecked(False)
    # the box is locked on in the page: saving always keeps wheel, so the manifest check is the second guard
    page.save_manifest()
    assert "wheel" in page.manifest.accounts.groups  # saving always keeps it, so a locked root is never left without an administrator
    assert page.validate_page()[0]


# --- ACCT-04 groups ------------------------------------------------------------------------------------------


def test_the_group_list_has_the_guided_default_and_keeps_unknown_groups(qtbot):
    widget = used(qtbot=qtbot)
    assert {group for group, check in widget.group_checks.items() if check.isChecked()} == set(accounts_page.GUIDED_GROUPS) | {"cron"}  # cronie is the default
    assert not widget.group_checks["wheel"].isEnabled() and widget.group_checks["wheel"].isChecked()
    manifest = InstallManifest()
    manifest.accounts.password = "password-12345"
    manifest.accounts.groups = ["wheel", "kvm"]
    other = used(manifest, qtbot)
    other.group_checks["usb"].setChecked(True)
    other.save_manifest()
    assert manifest.accounts.groups == ["wheel", "kvm", "usb"]


def test_the_cron_group_needs_a_cron_daemon(qtbot):
    manifest = InstallManifest()
    manifest.options.cron = "none"
    manifest.options.logger = "none"
    widget = used(manifest, qtbot)
    assert not widget.group_checks["cron"].isEnabled() and "cron" not in widget._chosen_groups()
    manifest.options.cron = "cronie"
    widget.refresh()
    widget.group_checks["cron"].setChecked(True)
    assert "cron" in widget._chosen_groups()


# --- ACCT-06 domain and hosts ----------------------------------------------------------------------------


def test_domain_and_extra_hosts_lines(page):
    page.set_advanced(True)
    page.domain.setText("home.example")
    page.hosts_extra.setPlainText("192.168.1.10   nas  nas.home\n\n")
    page.save_manifest()
    assert page.manifest.system.domain == "home.example" and page.manifest.system.hosts_extra == ["192.168.1.10 nas nas.home"]
    fill(page)
    assert page.validate_page()[0]
    page.hosts_extra.setPlainText("nonsense")
    ok, message = page.validate_page()
    assert not ok and "Extra hosts" in message


# --- ACCT-08 SSH ---------------------------------------------------------------------------------------------


def test_ssh_is_on_by_default_with_the_key_box_under_it(page):
    assert page.ssh_enabled.isChecked() and page.ssh_box.isVisibleTo(page)
    assert "Root cannot log in remotely" in text.SSH_HELP
    page.ssh_enabled.setChecked(False)
    assert not page.ssh_box.isVisibleTo(page)
    page.save_manifest()
    assert page.manifest.options.ssh is False


def test_keys_only_without_a_key_is_refused_and_with_a_key_accepted(page):
    page.set_advanced(True)
    fill(page)
    page.ssh_keys_only.setChecked(True)
    ok, message = page.validate_page()
    assert not ok and "Nobody could log in" in message
    page.ssh_keys.setPlainText("ssh-ed25519 AAAAC3Nza key")
    assert page.validate_page()[0]
    assert page.manifest.accounts.ssh_password_login is False


def test_there_is_no_control_for_root_login_over_ssh(page):
    page.save_manifest()
    assert page.manifest.accounts.ssh_root_login is False


# --- the tools tab ---------------------------------------------------------------------------------------------


@pytest.fixture
def customize(make_window):
    window = make_window()
    window._show_page(4)
    window.customize.tabs.setCurrentIndex(3)
    return window, window.customize


def test_the_tools_tab_separates_system_tools_from_applications(customize):
    window, tab = customize
    titles = [label.text() for label in tab.findChildren(type(tab.hwmonux_note)) if label.objectName() == "cardTitle"]
    assert "System tools (Handbook)" in titles and "Applications" in titles and "Gaming (optional)" in titles


def test_the_tools_choices_round_trip(customize):
    window, tab = customize
    options = window.manifest.options
    tab.set_tools_advanced(True)
    tab.logger.setCurrentIndex(tab.logger.findData("metalog"))
    tab.cron.setCurrentIndex(tab.cron.findData("dcron"))
    tab.timesync.setCurrentIndex(tab.timesync.findData("none"))
    for key in ("gentoolkit", "eclean_kernel", "mlocate"):
        tab.checks[key].setChecked(True)
    tab.save_tools()
    assert (options.logger, options.cron, options.timesync) == ("metalog", "dcron", "none")
    assert options.gentoolkit and options.eclean_kernel and options.mlocate and options.file_indexing
    tab.load_tools()
    assert tab.logger.currentData() == "metalog" and tab.checks["mlocate"].isChecked()


def test_the_old_file_indexing_setting_shows_as_mlocate(make_window):
    manifest = InstallManifest()
    manifest.options.file_indexing = True
    window = make_window(manifest=manifest)
    assert window.customize.checks["mlocate"].isChecked()


def test_systemd_hides_the_logger_and_offers_the_journal(make_window):
    manifest = InstallManifest()
    manifest.system.init = "systemd"
    manifest.system.stage_variant = "amd64-systemd"
    window = make_window(manifest=manifest)
    window._show_page(4)
    tab = window.customize
    tab.tabs.setCurrentIndex(3)
    tab.set_tools_advanced(True)
    assert not tab.logger.isVisibleTo(tab) and tab.journal_note.isVisibleTo(tab) and tab.checks["journal_persistent"].isVisibleTo(tab)
    assert "journalctl" in text.JOURNAL_NOTE and not tab.cron.model().item(tab.cron.findData("fcron")).isEnabled()
    assert tab.timesync.model().item(tab.timesync.findData("timesyncd")).isEnabled()
    tab.checks["journal_persistent"].setChecked(True)
    tab.save_tools()
    assert manifest.options.journal_persistent


def test_openrc_hides_the_journal_and_timesyncd(customize):
    window, tab = customize
    tab.set_tools_advanced(True)
    assert tab.logger.isVisibleTo(tab) and not tab.journal_note.isVisibleTo(tab) and not tab.checks["journal_persistent"].isVisibleTo(tab)
    assert not tab.timesync.model().item(tab.timesync.findData("timesyncd")).isEnabled()


def test_the_cron_dependency_note(customize):
    window, tab = customize
    tab.set_tools_advanced(True)
    tab.logger.setCurrentIndex(tab.logger.findData("syslog-ng"))
    tab.cron.setCurrentIndex(tab.cron.findData("none"))
    assert tab.cron_note.isVisibleTo(tab) and "cronie is installed as well" in tab.cron_note.text()
    tab.logger.setCurrentIndex(tab.logger.findData("metalog"))
    assert not tab.cron_note.isVisibleTo(tab)


def test_ssh_is_no_longer_a_tools_tab_box(customize):
    _, tab = customize
    assert "ssh" not in tab.checks and "chrony" not in tab.checks and "cron" not in tab.checks and "logger" not in tab.checks


# --- gaming ------------------------------------------------------------------------------------------------------


def test_gaming_is_off_by_default_and_the_status_is_honest(customize):
    window, tab = customize
    assert tab.gaming_buttons["off"].isChecked() and window.manifest.options.gaming == "off"
    assert "24 October 2026" in tab.gaming_status.text() and "not playable on Linux" in tab.gaming_status.text()
    tab.save_tools()
    assert window.manifest.options.gaming == "off"
    assert not tab.gaming_accept.isVisibleTo(tab) and not tab.gaming_tweaks.isVisibleTo(tab)


def test_choosing_native_shows_the_build_note_and_the_accept_box(customize):
    window, tab = customize
    tab.gaming_buttons["native"].setChecked(True)
    assert tab.gaming_accept.isVisibleTo(tab) and tab.gaming_build_note.isVisibleTo(tab)
    tab.save_tools()
    assert window.manifest.options.gaming == "native"
    tab.gaming_accept.setChecked(True)
    tab.gaming_windows.setChecked(True)
    tab.gaming_tweaks.setChecked(False)
    tab.save_tools()
    options = window.manifest.options
    assert (options.gaming, options.gaming_accept_external, options.gaming_windows_tools, options.gaming_tweaks) == ("native", True, True, False)


def test_native_is_disabled_without_multilib_and_a_change_resets_it_with_a_notice(make_window):
    manifest = InstallManifest()
    manifest.options.gaming = "native"
    manifest.options.gaming_accept_external = True
    window = make_window(manifest=manifest)
    window._show_page(4)
    tab = window.customize
    window.manifest.system.multilib = False
    window.manifest.system.stage_variant = "amd64-nomultilib-openrc"
    tab.refresh_gaming()
    assert not tab.gaming_buttons["native"].isEnabled() and tab.gaming_buttons["flatpak"].isChecked()
    assert manifest.options.gaming == "flatpak" and "no 32-bit support" in tab.gaming_note.text()


def test_native_with_nomultilib_explains_and_offers_flatpak(make_window):
    manifest = InstallManifest()
    manifest.system.multilib = False
    manifest.system.stage_variant = "amd64-nomultilib-openrc"
    manifest.system.desktop = "xfce"
    window = make_window(manifest=manifest)
    window._show_page(4)
    tab = window.customize
    assert not tab.gaming_buttons["native"].isEnabled() and tab.gaming_buttons["flatpak"].isEnabled()
    assert "multilib" in tab.gaming_note.text() and "Flatpak" in tab.gaming_note.text()


def test_a_minimal_desktop_disables_gaming_and_turns_it_off(make_window):
    manifest = InstallManifest()
    manifest.options.gaming = "flatpak"
    window = make_window(manifest=manifest)
    window._show_page(4)
    tab = window.customize
    tab.desktop_changed("minimal")
    assert manifest.options.gaming == "off" and not tab.gaming_buttons["flatpak"].isEnabled() and "no desktop" in tab.gaming_note.text()


def test_check_this_pc_is_read_only_and_lists_the_rows(customize, tmp_path):
    window, tab = customize
    before = window.manifest.to_dict()
    tab.run_gaming_check(str(tmp_path))
    report = tab.gaming_report.text()
    assert tab.gaming_report.isVisibleTo(tab)
    for row in ("Secure Boot", "TPM 2.0", "IOMMU", "Kernel lockdown", "BPF security module", "kexec", "Signed kernel and modules"):
        assert row in report
    assert "decided after the installation" in report
    assert window.manifest.to_dict() == before
    for forbidden in ("ready for Fortnite", "supported"):
        assert forbidden not in report


def test_the_card_and_the_installer_share_one_status_text(customize):
    from gentoo_installerd import gaming_readiness

    _, tab = customize
    assert tab.gaming_status.text() == gaming_readiness.FORTNITE_STATUS
