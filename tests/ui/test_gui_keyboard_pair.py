"""One keyboard, one table: the console keymap, the XKB layout and its variant are always set together."""

from __future__ import annotations

import copy

import pytest

from gentoo_installer import keyboard, keymaps
from gentoo_installer.manifest import InstallManifest


def pick(page, console):
    index = page.keymap.findData(console)
    assert index >= 0, console
    page.keymap.setCurrentIndex(index)
    page.keymap.activated.emit(index)


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(keyboard, "start_detached", lambda program, arguments: (calls.append((program, arguments)), True)[1])
    return calls


def test_the_installer_uses_one_table_for_layout_and_console_keymap():
    assert keyboard.console_keymap("de", "nodeadkeys") == keymaps.console_keymap_for("de", "nodeadkeys")
    assert not hasattr(keyboard, "CONSOLE_KEYMAPS")
    assert keyboard.CONSOLE_KEYMAP_CHOICES == sorted({console for console, _ in keymaps.console_choices()})


def test_every_console_keymap_chosen_on_the_accounts_page_agrees_with_its_layout():
    """The passphrase prompt only uses the console map when it is what the layout would give: the pair must round-trip."""
    for console, _label in keymaps.console_choices():
        layout, variant = keymaps.xkb_for_console(console)
        assert keymaps.console_keymap_for(layout, variant) == console, (console, layout, variant)


def test_the_console_keymap_combo_lists_the_table_with_plain_names(make_window):
    window = make_window()
    window._show_page(5)
    combo = window.accounts.keymap
    assert not combo.isEditable()
    assert [combo.itemData(i) for i in range(combo.count())] == [console for console, _ in keymaps.console_choices()]
    assert "German (de-latin1)" in [combo.itemText(i) for i in range(combo.count())]


def test_choosing_a_console_keymap_sets_the_layout_and_applies_it(make_window, started):
    window = make_window()
    window._show_page(5)
    page = window.accounts
    pick(page, "de-latin1-nodeadkeys")
    system = window.manifest.system
    assert (system.keymap, system.keyboard_layout, system.keyboard_variant) == ("de-latin1-nodeadkeys", "de", "nodeadkeys")
    assert started[-1][0] == "setxkbmap" and started[-1][1][:4] == ["-layout", "de", "-variant", "nodeadkeys"]
    assert window.welcome.keyboard.value() == ("de", "nodeadkeys")
    assert "de (nodeadkeys)" in page.keyboard_summary.text()
    assert page.keymap_note.isVisibleTo(page) and "de (nodeadkeys)" in page.keymap_note.text()
    page.save_manifest()
    assert window.manifest.system.keymap == "de-latin1-nodeadkeys"


def test_a_change_of_layout_after_the_disk_passphrase_is_pointed_out(make_window, started):
    manifest = InstallManifest()
    manifest.storage.luks = True
    window = make_window(manifest=manifest)
    window._show_page(5)
    pick(window.accounts, "fr")
    assert "Storage step" in window.accounts.keymap_note.text()


def test_choosing_what_is_already_active_changes_nothing_and_says_nothing(make_window, started):
    window = make_window()
    window._show_page(5)
    pick(window.accounts, "us")
    assert (window.manifest.system.keymap, window.manifest.system.keyboard_layout) == ("us", "us")
    assert not window.accounts.keymap_note.isVisibleTo(window.accounts)


def test_visiting_the_accounts_page_never_rewrites_the_keyboard(make_window, started):
    manifest = InstallManifest()
    manifest.system.keyboard_layout, manifest.system.keyboard_variant, manifest.system.keymap = "ru", "", "us"
    window = make_window(manifest=manifest)
    before = copy.deepcopy(window.manifest.system)
    for _ in range(2):
        window._show_page(5)
        window.accounts.save_manifest()
        window._show_page(4)
    assert window.manifest.system == before and started == []


@pytest.mark.parametrize(("value", "shown"), [("sg-latin1", "sg-latin1 (custom value)"), ("auto", "Automatic, from the keyboard layout"), ("", "Automatic, from the keyboard layout")])
def test_a_keymap_that_is_not_in_the_table_is_shown_and_kept(make_window, value, shown):
    manifest = InstallManifest()
    manifest.system.keymap = value
    window = make_window(manifest=manifest)
    window._show_page(5)
    assert window.accounts.keymap.currentText() == shown
    window.accounts.save_manifest()
    assert window.manifest.system.keymap == value


def test_the_welcome_chooser_sets_all_three_names_together(make_window, started):
    window = make_window()
    chooser = window.welcome.keyboard
    chooser.layout_box.setCurrentIndex(chooser.layout_box.findData("fr"))
    chooser._layout_activated(0)
    system = window.manifest.system
    assert (system.keyboard_layout, system.keyboard_variant, system.keymap) == ("fr", "", "fr")
    chooser.layout_box.setCurrentIndex(chooser.layout_box.findData("ru"))
    chooser._layout_activated(0)
    assert (system.keyboard_layout, system.keymap) == ("ru", "us")  # the console of a non-Latin layout stays US


# --- the clock of a dual-boot computer ---------------------------------------------------------------------------------------------------


def test_the_hardware_clock_choice_round_trips_and_is_never_set_by_looking(make_window):
    manifest = InstallManifest()
    manifest.system.rtc_local = True
    window = make_window(manifest=manifest)
    window._show_page(5)
    assert window.accounts.rtc_local.isChecked()
    window.accounts.save_manifest()
    assert window.manifest.system.rtc_local is True
    window.accounts.rtc_local.setChecked(False)
    window.accounts.save_manifest()
    assert window.manifest.system.rtc_local is False


def test_windows_next_to_linux_suggests_the_local_time_clock(make_window):
    from tests.hardware.helpers import fixture_dict
    from tests.ui.conftest import sample_info

    info = sample_info(hardware=fixture_dict("thinkpad-intel-nvidia-hybrid"))  # a Windows boot entry and a clock in local time
    window = make_window(info=info)
    window._show_page(5)
    page = window.accounts
    assert page.clock_hint.isVisibleTo(page) and "Windows was found" in page.clock_hint.text()
    page.rtc_local.setChecked(True)
    assert not page.clock_hint.isVisibleTo(page)
    quiet = make_window(info=sample_info(hardware=fixture_dict("qemu-q35-virtio")))
    quiet._show_page(5)
    assert not quiet.accounts.clock_hint.isVisibleTo(quiet.accounts)
