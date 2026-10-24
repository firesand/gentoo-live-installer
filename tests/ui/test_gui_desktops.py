"""The desktop cards come from the registry; the other desktops sit under "More desktops"."""

from __future__ import annotations

from gentoo_installer import desktops
from gentoo_installer.manifest import InstallManifest

from tests.ui.test_gui_wizard import choose_stage


def test_the_cards_are_the_registry_and_the_guided_four_are_always_visible(make_window):
    window = make_window()
    page = window.system
    group = page.desktop_group
    assert list(group.cards) == list(desktops.GUIDED_DESKTOPS) + list(desktops.MORE_DESKTOPS)
    assert all(group.guided.cards[key].isVisibleTo(page) for key in desktops.GUIDED_DESKTOPS)


def test_more_desktops_are_collapsed_in_guided_open_in_advanced_and_open_for_a_chosen_card(make_window):
    window = make_window()
    page = window.system
    section = page.desktop_section.more_section
    page.set_advanced(False)
    section.set_open(False)
    assert not section.is_open()
    page.set_advanced(True)
    assert section.is_open()
    page.set_advanced(False)
    page.desktop_group.set_value("mate")
    assert section.is_open() and page.desktop_group.value() == "mate"
    assert not any(card.isChecked() for card in page.desktop_group.guided.cards.values())
    page.desktop_group.set_value("xfce")  # one radio group over both
    assert not page.desktop_group.more.cards["mate"].isChecked()


def test_a_loaded_more_desktop_is_selected_and_saved(make_window):
    manifest = InstallManifest()
    manifest.system.desktop = "lxqt"
    window = make_window(manifest=manifest)
    page = window.system
    page.set_advanced(False)
    assert page.desktop_group.value() == "lxqt" and page.desktop_section.more_section.is_open()
    page.save_manifest()
    assert window.manifest.system.desktop == "lxqt" and window.manifest.system.profile == ""
    assert window.manifest.effective_profile().endswith("/23.0/desktop")


def test_cinnamon_card_says_it_needs_the_gnome_profile_and_a_hardened_stage_disables_it(make_window):
    window = make_window()
    page = window.system
    page.desktop_group.set_value("cinnamon")
    choose_stage(window, "amd64-hardened-openrc")
    assert not page.desktop_group.cards["cinnamon"].isEnabled() and "desktop/gnome" in page.desktop_group.cards["cinnamon"].toolTip()
    assert page.desktop_group.cards["mate"].isEnabled()
    assert page.desktop_group.value() == "minimal" and "desktop/gnome" in page.family_note.text()


def test_an_unverified_family_warns_for_a_more_desktop(make_window):
    window = make_window()
    page = window.system
    choose_stage(window, "amd64-hardened-openrc")
    page.desktop_group.set_value("mate")
    assert "Not verified" in page.family_note.text() and "MATE" in page.family_note.text()
