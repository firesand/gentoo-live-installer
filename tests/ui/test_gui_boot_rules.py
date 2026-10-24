"""Base system and Customization pages: what only works together is offered together, and nothing loaded is rewritten."""

from __future__ import annotations

import copy

import pytest
from PySide6.QtWidgets import QLabel

from gentoo_installer.manifest import InstallManifest
from tests.hardware.helpers import fixture_dict
from tests.ui.conftest import sample_info


def choose_bootloader(page, name):
    """Pick a bootloader the way a user does."""
    index = page.bootloader.findData(name)
    page.bootloader.setCurrentIndex(index)
    page.bootloader.activated.emit(index)


def system_page(make_window, manifest=None, **kwargs):
    window = make_window(manifest=manifest, **kwargs)
    window._show_page(3)
    return window, window.system


# --- Secure Boot, UKI, GRUB, dual boot ------------------------------------------------------------------------------------------------------


def test_ticking_secure_boot_forces_the_unified_kernel_image(make_window):
    window, page = system_page(make_window)
    choose_bootloader(page, "systemd-boot")
    assert page.secure_boot.isEnabled() and page.uki.isEnabled() and not page.uki.isChecked()
    page.secure_boot.click()
    assert page.secure_boot.isChecked() and page.uki.isChecked() and not page.uki.isEnabled()
    assert "signed Unified Kernel Image" in page.uki.toolTip()
    assert page.signing_key.isEnabled() and page.signing_cert.isEnabled()
    page.secure_boot.click()  # unticking gives the image back to the user, still ticked
    assert not page.secure_boot.isChecked() and page.uki.isEnabled() and page.uki.isChecked()
    assert not page.signing_key.isEnabled()


def test_the_efi_stub_works_like_systemd_boot(make_window):
    window, page = system_page(make_window)
    choose_bootloader(page, "efi-stub")
    page.secure_boot.click()
    assert page.uki.isChecked() and not page.uki.isEnabled()


def test_choosing_grub_switches_secure_boot_off_and_explains_why(make_window):
    window, page = system_page(make_window)
    choose_bootloader(page, "systemd-boot")
    page.secure_boot.click()
    choose_bootloader(page, "grub")
    assert not page.secure_boot.isChecked() and not page.secure_boot.isEnabled()
    assert "GRUB" in page.secure_boot.toolTip() and "systemd-boot" in page.secure_boot.toolTip()
    assert page.dual_boot.isEnabled()
    assert page.uki.isEnabled()  # the forced image is released too


def test_dual_boot_is_a_grub_feature(make_window):
    window, page = system_page(make_window)
    page.dual_boot.setChecked(True)
    choose_bootloader(page, "systemd-boot")
    assert not page.dual_boot.isChecked() and not page.dual_boot.isEnabled()
    assert "systemd-boot finds Windows" in page.dual_boot.toolTip()
    choose_bootloader(page, "grub")
    assert page.dual_boot.isEnabled()


def test_secure_boot_and_the_image_need_uefi(make_window):
    window, page = system_page(make_window, info=sample_info(uefi=False))
    assert window.manifest.storage.firmware == "bios"
    assert not page.secure_boot.isEnabled() and "UEFI" in page.secure_boot.toolTip()
    assert not page.uki.isEnabled() and "UEFI" in page.uki.toolTip()


def test_a_loaded_secure_boot_with_grub_is_shown_as_loaded_and_can_be_unticked(make_window):
    manifest = InstallManifest()
    manifest.system.secure_boot = True
    manifest.system.secure_boot_key, manifest.system.secure_boot_certificate = "/k.key", "/c.crt"
    window, page = system_page(make_window, manifest)
    before = copy.deepcopy(window.manifest.system)
    assert page.secure_boot.isChecked() and page.secure_boot.isEnabled()  # a ticked box stays editable
    valid, message = page.validate_page()
    assert not valid and "GRUB" in message
    assert window.manifest.system == before  # looking at it changed nothing


def test_a_loaded_secure_boot_without_the_image_is_not_fixed_silently(make_window):
    manifest = InstallManifest()
    manifest.system.bootloader, manifest.system.secure_boot, manifest.system.uki = "systemd-boot", True, False
    manifest.system.secure_boot_key, manifest.system.secure_boot_certificate = "/k.key", "/c.crt"
    window, page = system_page(make_window, manifest)
    assert not page.uki.isChecked() and page.uki.isEnabled()
    valid, message = page.validate_page()
    assert not valid and "Unified Kernel Image" in message and window.manifest.system.uki is False


def test_a_consistent_loaded_setup_shows_the_image_as_forced(make_window):
    manifest = InstallManifest()
    manifest.system.bootloader, manifest.system.secure_boot, manifest.system.uki = "efi-stub", True, True
    manifest.system.secure_boot_key, manifest.system.secure_boot_certificate = "/k.key", "/c.crt"
    window, page = system_page(make_window, manifest)
    assert page.uki.isChecked() and not page.uki.isEnabled()
    assert page.validate_page() == (True, "")


# --- Customization -------------------------------------------------------------------------------------------------------------------------


def customize_page(make_window, manifest=None, info=None):
    window = make_window(manifest=manifest, info=info)
    window._show_page(4)
    window.customize.network_more.set_open(True)
    window.customize.tabs.setCurrentIndex(2)  # Network: the tab most of these tests are about (Hardware, Portage, Network, Applications)
    return window, window.customize


def machine(name, **overrides):
    hardware = fixture_dict(name)
    return sample_info(hardware=hardware, warnings=copy.deepcopy(hardware["warnings"]), **overrides)


def labels(widget):
    return {label.text() for label in widget.findChildren(QLabel)}


def test_the_flags_field_is_named_for_what_it_feeds(make_window):
    window, page = customize_page(make_window)
    assert "COMMON_FLAGS" in labels(page) and "CFLAGS" not in labels(page)
    assert "COMMON_FLAGS" in page.cflags.toolTip() or "Fortran" in page.cflags.toolTip()


def test_empty_flag_fields_say_what_they_will_become_on_this_computer(make_window):
    window, page = customize_page(make_window)
    assert page.cpu_flags.placeholderText() == "Leave empty: chosen for this computer"  # nothing detected
    window, page = customize_page(make_window, info=machine("asus-desktop-rtx4090"))
    assert page.cpu_flags.placeholderText() == "Leave empty: chosen for this computer (the profile default)"
    assert page.video_cards.placeholderText().endswith("(the profile default)")


def test_building_from_source_tunes_for_the_cpu_and_the_field_shows_the_value(make_window):
    manifest = InstallManifest()
    manifest.portage.binhost = False
    window, page = customize_page(make_window, manifest, machine("asus-desktop-rtx4090"))
    text = page.cpu_flags.placeholderText()
    assert "(now: " in text and "avx2" in text
    window.manifest.hardware.target = "portable"
    page.hardware.load_manifest()
    page.refresh_placeholders()
    assert page.cpu_flags.placeholderText().endswith("(the profile default)")  # a portable disk keeps generic flags


def test_the_nvidia_driver_shows_up_in_the_video_cards_placeholder(make_window):
    manifest = InstallManifest()
    manifest.hardware.gpu_driver = "nvidia"
    manifest.hardware.nvidia_license_accepted = True
    window, page = customize_page(make_window, manifest, machine("asus-desktop-rtx4090"))
    assert "profile default + nvidia" in page.video_cards.placeholderText()


def test_choosing_a_hardware_option_refreshes_the_placeholders(make_window):
    manifest = InstallManifest()
    manifest.portage.binhost = False
    window, page = customize_page(make_window, manifest, machine("asus-desktop-rtx4090"))
    assert "(now: " in page.cpu_flags.placeholderText()
    page.hardware.target.set_value("portable")
    assert page.cpu_flags.placeholderText().endswith("(the profile default)")


def test_explicit_flag_values_survive_a_visit(make_window):
    manifest = InstallManifest()
    manifest.portage.cpu_flags = "aes avx2"
    manifest.portage.video_cards = "amdgpu radeonsi"
    window, page = customize_page(make_window, manifest, machine("asus-desktop-rtx4090"))
    page.save_manifest()
    assert (window.manifest.portage.cpu_flags, window.manifest.portage.video_cards) == ("aes avx2", "amdgpu radeonsi")


# --- KDE applications ---------------------------------------------------------------------------------------------------------------------------


def test_the_kde_application_set_is_a_choice_for_kde_only(make_window):
    window, page = customize_page(make_window)
    page.tabs.setCurrentIndex(3)
    assert page.kde_apps.currentData() == "core" and page.kde_apps.isEnabled()
    page.kde_apps.setCurrentIndex(page.kde_apps.findData("full"))
    page.save_manifest()
    assert window.manifest.options.kde_apps == "full"
    window.system.desktop_group.set_value("gnome")
    assert not page.kde_apps.isEnabled() and "KDE Plasma" in page.kde_apps.toolTip()
    window.system.desktop_group.set_value("kde")
    assert page.kde_apps.isEnabled() and page.kde_apps.currentData() == "full"


def test_an_unknown_kde_application_set_is_shown_and_kept(make_window):
    manifest = InstallManifest()
    manifest.options.kde_apps = "everything"
    window, page = customize_page(make_window, manifest)
    assert "everything" in page.kde_apps.currentText()
    page.save_manifest()
    assert window.manifest.options.kde_apps == "everything"
    assert not page.validate_page()[0]  # the manifest refuses it, and says so


# --- network -------------------------------------------------------------------------------------------------------------------------------


def pick_manager(page, value):
    index = page.network_manager.findData(value)
    page.network_manager.setCurrentIndex(index)
    page.network_manager.activated.emit(index)


@pytest.mark.parametrize(("manager", "interface"), [("networkmanager", False), ("dhcpcd", True), ("netifrc", True), ("systemd-networkd", False)])
def test_the_interface_field_is_for_the_managers_that_need_it(make_window, manager, interface):
    window, page = customize_page(make_window)
    pick_manager(page, manager)
    assert page.interface.isEnabled() is interface


def test_wi_fi_belongs_to_networkmanager_and_is_cleared_when_the_user_leaves_it(make_window):
    window, page = customize_page(make_window)
    assert page.wifi_ssid.isEnabled() and page.wifi_password.isEnabled() and page.import_profiles.isEnabled()
    page.wifi_ssid.setText("Home")
    page.wifi_password.setText("a-long-password")
    page.import_profiles.setChecked(True)
    pick_manager(page, "dhcpcd")
    assert page.wifi_ssid.text() == "" and page.wifi_password.text() == "" and not page.import_profiles.isChecked()
    assert not page.wifi_ssid.isEnabled() and not page.wifi_password.isEnabled() and not page.import_profiles.isEnabled()
    assert "NetworkManager only" in page.network_note.text()
    pick_manager(page, "networkmanager")
    assert page.wifi_ssid.isEnabled() and not page.network_note.isVisibleTo(page)


def test_a_loaded_wi_fi_with_another_manager_is_kept_editable_and_refused(make_window):
    manifest = InstallManifest()
    manifest.network.manager = "dhcpcd"
    manifest.network.wifi_ssid, manifest.network.wifi_password = "Home", "a-long-password"
    window, page = customize_page(make_window, manifest)
    assert page.wifi_ssid.isEnabled() and page.wifi_password.isEnabled()  # filled fields stay editable so they can be cleared
    valid, message = page.validate_page()
    assert not valid and "NetworkManager" in message
    assert window.manifest.network.wifi_ssid == "Home"


def test_importing_the_saved_networks_is_off_and_warns_for_a_portable_disk(make_window):
    window, page = customize_page(make_window, info=machine("asus-desktop-rtx4090"))
    assert page.import_profiles.text() == "Use the Wi-Fi networks saved in this live session" and not page.import_profiles.isChecked()
    page.import_profiles.setChecked(True)
    assert not page.import_warning.isVisibleTo(page)
    page.hardware.target.set_value("portable")
    page._update_network_notes()
    assert page.import_warning.isVisibleTo(page) and "other computers" in page.import_warning.text()
    page.save_manifest()
    assert window.manifest.network.import_live_profiles is True and window.manifest.hardware.target == "portable"


def test_a_removable_target_disk_also_warns(make_window):
    from gentoo_installer.system_info import DiskInfo

    stick = DiskInfo("/dev/sdb", "USB stick", 64 * 1024**3, serial="U1", removable=True)
    manifest = InstallManifest()
    manifest.storage.device = "/dev/sdb"
    manifest.network.import_live_profiles = True
    window, page = customize_page(make_window, manifest, sample_info(disks=[stick]))
    assert page.import_warning.isVisibleTo(page)


def test_the_names_of_the_saved_networks_are_listed_for_the_user(make_window):
    info = machine("thinkpad-intel-nvidia-hybrid")
    info.hardware["network_profiles"] = ["Home", "Office"]
    window, page = customize_page(make_window, info=info)
    assert "Home, Office" in page.import_profiles.toolTip()


def test_adapter_names_are_offered_while_typing_the_interface(make_window):
    info = machine("thinkpad-intel-nvidia-hybrid")
    window, page = customize_page(make_window, info=info)
    completer = page.interface.completer()
    names = {nic["iface"] for nic in info.hardware["nics"] if nic["iface"]}
    assert completer is not None and names and names <= {completer.model().index(i, 0).data() for i in range(completer.model().rowCount())}
