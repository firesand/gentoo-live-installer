"""BOOT-01..07: the System page order, the stage3 derived from the desktop, kernel and boot loader cards, dual boot and Secure Boot."""

from __future__ import annotations

import copy

from PySide6.QtWidgets import QApplication, QComboBox

from gentoo_installer.manifest import Hardware, InstallManifest
from gentoo_installer.pages.boot_section import SECURE_BOOT_CONFIRM, SECURE_BOOT_NVIDIA, detect_other_systems, firmware_secure_boot
from gentoo_installer.system_info import DiskInfo, PartitionInfo, SystemInfo
from tests.hardware.helpers import fixture_dict
from tests.ui.conftest import sample_info

GIB = 1024**3


def system_page(make_window, manifest=None, **kwargs):
    window = make_window(manifest=manifest, **kwargs)
    window._show_page(3)
    QApplication.processEvents()
    return window, window.system


def with_windows(**overrides) -> SystemInfo:
    info = sample_info(**overrides)
    info.disks = [
        DiskInfo("/dev/sda", "Disk", 500 * GIB, serial="a", partitions=[PartitionInfo("/dev/sda1", 450 * GIB, "ntfs", label="Windows", parent="/dev/sda")]),
        DiskInfo("/dev/sdb", "Empty disk", 500 * GIB, serial="b"),
    ]
    return info


def with_firmware(secure_boot: str, **overrides) -> SystemInfo:
    hardware = fixture_dict("qemu-q35-virtio")
    hardware["firmware"]["secure_boot"] = secure_boot
    return sample_info(hardware=hardware, **overrides)


# --- BOOT-01: the order and the Expert section ----------------------------------------------------------------------------------------------


def test_the_desktop_is_visible_without_scrolling_on_a_1366_by_768_screen(make_window):
    window = make_window()
    window.resize(1366, 768)
    window._show_page(3)
    QApplication.processEvents()
    page = window.system
    viewport = page.scroll_area.viewport()
    group = page.desktop_group
    bottom = group.mapTo(viewport, group.rect().bottomLeft()).y()
    assert 0 < bottom <= viewport.height()


def test_the_page_order_is_desktop_init_system_kernel_boot_loader_then_expert_options(make_window):
    window, page = system_page(make_window)
    body = page.scroll_area.widget()

    def top(widget) -> int:
        return widget.mapTo(body, widget.rect().topLeft()).y()

    order = [top(page.desktop_section), top(page.init_group), top(page.kernel), top(page.bootloader), top(page.expert)]
    assert order == sorted(order) and len(set(order)) == 5


def test_no_combo_box_on_the_page_is_disabled_and_the_init_system_is_explained(make_window):
    window, page = system_page(make_window)
    window.mode.setCurrentIndex(1)
    assert [combo.objectName() or combo.accessibleName() for combo in page.findChildren(QComboBox) if combo.isVisibleTo(page) and not combo.isEnabled()] == []
    assert "You cannot easily switch later" in page.init_note.text() and "OpenRC" in page.init_note.text()
    assert "Dracut (required by Gentoo kernels, automatic)" in page.boot_section.initramfs_line.text()


def test_guided_shows_the_derived_profile_and_hides_the_expert_options(make_window):
    window, page = system_page(make_window)
    assert not page.expert.is_open() and not page.stage.isVisibleTo(page)
    assert "default/linux/amd64/23.0/desktop/plasma (chosen for KDE Plasma and OpenRC)" in page.profile_summary.text()
    page.init_group.set_value("systemd")
    assert "plasma/systemd (chosen for KDE Plasma and systemd)" in page.profile_summary.text()
    window.mode.setCurrentIndex(1)
    assert page.expert.is_open() and page.stage.isVisibleTo(page) and page.pure64.isVisibleTo(page)
    window.mode.setCurrentIndex(0)
    assert not page.expert.is_open()


# --- BOOT-02: the stage3 follows the desktop -----------------------------------------------------------------------------------------------------


def test_a_desktop_preselects_the_desktop_stage3_and_minimal_the_standard_one(make_window):
    window, page = system_page(make_window)
    assert page.stage_auto.isChecked() and page.stage.currentData() == "amd64-desktop-openrc"
    page.desktop_group.set_value("minimal")
    assert page.stage.currentData() == "amd64-openrc"
    page.desktop_group.set_value("xfce")
    assert page.stage.currentData() == "amd64-desktop-openrc"
    page.save_manifest()
    assert window.manifest.system.stage_variant == "amd64-desktop-openrc" and window.manifest.system.stage_auto is True
    assert window.manifest.system.multilib is True


def test_picking_a_stage3_by_hand_ends_the_automatic_choice(make_window):
    window, page = system_page(make_window)
    page.stage.setCurrentIndex(page.stage.findData("amd64-hardened-openrc"))
    assert not page.stage_auto.isChecked()
    page.desktop_group.set_value("gnome")
    assert page.stage.currentData() == "amd64-hardened-openrc"
    page.save_manifest()
    assert window.manifest.system.stage_auto is False and window.manifest.system.stage_variant == "amd64-hardened-openrc"


def test_a_loaded_hardened_stage_is_kept_and_a_loaded_standard_one_follows_the_desktop(make_window):
    manifest = InstallManifest()
    manifest.system.stage_variant = "amd64-hardened-openrc"
    window, page = system_page(make_window, manifest)
    assert page.stage.currentData() == "amd64-hardened-openrc" and not page.stage_auto.isChecked()
    window, page = system_page(make_window, InstallManifest())
    assert page.stage.currentData() == "amd64-desktop-openrc"  # the default KDE install gets the Desktop stage3


def test_pure_64_bit_is_off_by_default_warns_and_a_loaded_pure_64_bit_kde_is_a_plain_error(make_window):
    window, page = system_page(make_window)
    window.mode.setCurrentIndex(1)
    assert not page.pure64.isChecked() and not page.pure64_warning.isVisibleTo(page)
    page.desktop_group.set_value("gnome")
    page.pure64.setChecked(True)
    assert page.pure64_warning.isVisibleTo(page) and "No 32-bit programs at all" in page.pure64_warning.text()
    assert page.stage.currentData() == "amd64-nomultilib-openrc"
    manifest = InstallManifest()
    manifest.system.multilib, manifest.system.stage_variant = False, "amd64-nomultilib-openrc"
    window, page = system_page(make_window, manifest)
    valid, message = page.validate_page()
    assert not valid and "KDE Plasma needs a Standard or Desktop stage3" in message


def test_the_world_update_checkbox_moved_to_the_portage_tab(make_window):
    window, page = system_page(make_window)
    assert not hasattr(page, "world_update")
    manifest = InstallManifest()
    manifest.system.world_update = "now"
    window, page = system_page(make_window, manifest)
    page.save_manifest()
    assert window.manifest.system.world_update == "now"  # not touched here


# --- BOOT-03: kernel labels and the extra options -------------------------------------------------------------------------------------------------


def test_the_kernel_cards_use_plain_labels_and_the_ready_made_one_is_the_default(make_window):
    window, page = system_page(make_window)
    titles = {key: card.title_label.text() for key, card in page.kernel.cards.items()}
    assert titles == {"gentoo-kernel-bin": "Gentoo kernel, ready-made (about 5 min)",
                      "gentoo-kernel": "Gentoo kernel, built on your PC (slow, allows custom settings)"}
    assert page.kernel.value() == "gentoo-kernel-bin"
    assert "Handbook’s recommended start" in page.kernel_guidance.text()


def test_extra_startup_options_and_the_config_snippet_are_saved(make_window):
    window, page = system_page(make_window)
    window.mode.setCurrentIndex(1)
    assert page.extra_options.placeholderText() == "quiet" and page.extra_options.isVisibleTo(page)
    assert not page.config_snippet.isVisibleTo(page)  # only for the kernel that is built on this PC
    page.extra_options.setText("quiet  nomodeset")
    page.kernel.set_value("gentoo-kernel")
    assert page.config_snippet.isVisibleTo(page)
    page.config_snippet.setPlainText("CONFIG_HZ_1000=y\n")
    valid, message = page.validate_page()
    assert valid, message
    system = window.manifest.system
    assert system.kernel_cmdline_extra == "quiet nomodeset" and system.kernel_config_snippet == "CONFIG_HZ_1000=y"
    page.config_snippet.setPlainText("this is not a config line")
    valid, message = page.validate_page()
    assert not valid and "CONFIG_NAME" in message


def test_a_snippet_survives_switching_kernels(make_window):
    manifest = InstallManifest()
    manifest.system.kernel, manifest.system.kernel_config_snippet = "gentoo-kernel", "CONFIG_HZ_1000=y"
    window, page = system_page(make_window, manifest)
    page.kernel.set_value("gentoo-kernel-bin")
    page.save_manifest()
    assert window.manifest.system.kernel_config_snippet == "CONFIG_HZ_1000=y"


def test_loaded_choices_without_an_effect_are_shown_and_refused_not_replaced(make_window):
    manifest = InstallManifest()
    manifest.system.kernel = "custom"
    window, page = system_page(make_window, manifest)
    assert page.kernel.value() == "custom" and "not available" in page.kernel.cards["custom"].title_label.text()
    valid, message = page.validate_page()
    assert not valid and "custom" in message and window.manifest.system.kernel == "custom"


# --- BOOT-05: boot loader cards, notices and fallback --------------------------------------------------------------------------------------------


def test_the_boot_loader_cards_explain_themselves_in_one_line(make_window):
    window, page = system_page(make_window)
    text = {key: card.description_label.text() for key, card in page.bootloader.cards.items()}
    assert "every PC, old (BIOS) and new (UEFI)" in text["grub"]
    assert "small, simple UEFI boot menu" in text["systemd-boot"]
    assert "no boot menu" in page.bootloader.cards["efi-stub"].title_label.text() and "rescue USB" in text["efi-stub"]


def test_the_boot_loader_state_machine_for_bios_uefi_and_secure_boot(make_window):
    # BIOS: only GRUB, the others greyed with the reason; no Secure Boot, no UKI
    window, page = system_page(make_window, info=sample_info(uefi=False))
    cards = page.bootloader.cards
    assert cards["grub"].isEnabled() and not cards["systemd-boot"].isEnabled() and not cards["efi-stub"].isEnabled()
    assert "UEFI" in cards["efi-stub"].toolTip()
    assert not page.secure_boot.isEnabled() and not page.uki.isEnabled() and not page.fallback.isVisibleTo(page)
    # UEFI with GRUB: Secure Boot is off the table (GRUB cannot be signed here), dual boot is possible
    window, page = system_page(make_window)
    assert all(card.isEnabled() for card in page.bootloader.cards.values())
    assert not page.secure_boot.isEnabled() and page.dual_boot.isEnabled() and page.fallback.isVisibleTo(page)
    # UEFI with systemd-boot or the EFI stub: Secure Boot possible, dual boot is not needed
    for key in ("systemd-boot", "efi-stub"):
        page.bootloader.set_value(key)
        assert page.secure_boot.isEnabled() and not page.dual_boot.isEnabled()
    # firmware Secure Boot is on: say what to do
    window, page = system_page(make_window, info=with_firmware("enabled"))
    assert firmware_secure_boot(window.info) == "enabled"
    assert "turn it off in the firmware screen" in page.boot_section.firmware_note.text() and page.boot_section.firmware_note.isVisibleTo(page)
    window, page = system_page(make_window, info=with_firmware("disabled"))
    assert not page.boot_section.firmware_note.isVisibleTo(page)


def test_the_efi_stub_starts_with_the_image_ticked_and_grub_hides_it_in_guided(make_window):
    window, page = system_page(make_window)
    assert not page.boot_section.uki_row.isVisibleTo(page)
    page.bootloader.set_value("efi-stub")
    assert page.uki.isChecked() and page.boot_section.uki_row.isVisibleTo(page)
    page.bootloader.set_value("grub")
    window.mode.setCurrentIndex(1)
    assert page.boot_section.uki_row.isVisibleTo(page)  # Advanced shows it for every boot loader


def test_the_fallback_checkbox_maps_to_the_hardware_setting_and_only_when_used(make_window):
    window, page = system_page(make_window)
    page.save_manifest()
    assert window.manifest.hardware.removable_fallback == "auto"  # looking changes nothing
    page.fallback.click()
    page.save_manifest()
    assert window.manifest.hardware.removable_fallback == "always"
    page.fallback.click()
    page.save_manifest()
    assert window.manifest.hardware.removable_fallback == "auto"
    manifest = InstallManifest()
    manifest.hardware = Hardware(removable_fallback="always")
    window, page = system_page(make_window, manifest)
    assert page.fallback.isChecked()
    page.save_manifest()
    assert window.manifest.hardware.removable_fallback == "always"


def test_no_bootloader_is_an_advanced_choice_that_keeps_the_cards(make_window):
    window, page = system_page(make_window)
    window.mode.setCurrentIndex(1)
    page.no_bootloader.setChecked(True)
    assert not page.bootloader.isEnabled() and not page.secure_boot.isEnabled()
    valid, message = page.validate_page()
    assert valid, message
    assert window.manifest.system.bootloader == "none"
    page.no_bootloader.setChecked(False)
    page.save_manifest()
    assert window.manifest.system.bootloader == "grub"


# --- BOOT-06: dual boot ---------------------------------------------------------------------------------------------------------------------------


def test_windows_found_on_a_disk_preselects_dual_boot_and_says_so(make_window):
    window, page = system_page(make_window, info=with_windows())
    assert detect_other_systems(window.info, window.manifest) == ["Windows on /dev/sda1"]
    assert page.dual_boot.isChecked() and page.boot_section.dual_boot_found.isVisibleTo(page)
    assert page.boot_section.dual_boot_found.text() == "Found: Windows on /dev/sda1 - it will be added to the boot menu."
    page.save_manifest()
    assert window.manifest.system.dual_boot is True


def test_no_other_system_leaves_dual_boot_off(make_window):
    window, page = system_page(make_window)
    assert detect_other_systems(window.info, window.manifest) == []
    assert not page.dual_boot.isChecked() and not page.boot_section.dual_boot_found.isVisibleTo(page)


def test_a_disk_that_is_erased_does_not_count_and_the_users_choice_persists(make_window):
    manifest = InstallManifest()
    manifest.storage.device = "/dev/sda"
    window, page = system_page(make_window, manifest, info=with_windows())
    assert detect_other_systems(window.info, window.manifest) == []
    assert not page.dual_boot.isChecked()
    manifest = InstallManifest()
    window, page = system_page(make_window, manifest, info=with_windows())
    page.dual_boot.click()  # the user says no
    assert not page.dual_boot.isChecked()
    window.apply_info(with_windows())  # a new inspection must not undo it
    assert not page.dual_boot.isChecked()


def test_a_windows_boot_entry_alone_is_found_too(make_window):
    hardware = fixture_dict("qemu-q35-virtio")
    hardware["firmware"]["windows_boot_entry"] = True
    window, page = system_page(make_window, info=sample_info(hardware=hardware))
    assert detect_other_systems(window.info, window.manifest) == ["Windows (Boot Manager)"]
    assert page.dual_boot.isChecked()


# --- BOOT-07: Secure Boot ------------------------------------------------------------------------------------------------------------------------


def test_secure_boot_is_one_tick_that_opens_the_choices_and_needs_a_confirmation(make_window):
    window, page = system_page(make_window)
    page.bootloader.set_value("systemd-boot")
    assert not page.boot_section.secure_frame.isVisibleTo(page)
    page.secure_boot.click()
    assert page.boot_section.secure_frame.isVisibleTo(page) and page.uki.isChecked()
    assert page.secure_mode.currentData() == "create" and not page.signing_key.isVisibleTo(page)
    assert page.secure_confirm.text() == SECURE_BOOT_CONFIRM and not page.secure_confirm.isChecked()
    valid, message = page.validate_page()
    assert not valid and "confirms you understand" in message
    page.secure_confirm.setChecked(True)
    valid, message = page.validate_page()
    assert valid, message
    system = window.manifest.system
    assert (system.secure_boot, system.secure_boot_mode, system.secure_boot_key) == (True, "create", "")
    assert window.manifest.validate_sections()["system"] == []


def test_the_own_key_mode_asks_for_the_files_and_shim_is_listed_but_not_available(make_window):
    window, page = system_page(make_window)
    page.bootloader.set_value("efi-stub")
    page.secure_boot.click()
    page.secure_confirm.setChecked(True)
    page.secure_mode.setCurrentIndex(page.secure_mode.findData("own-key"))
    assert page.signing_key.isVisibleTo(page) and page.signing_cert.isVisibleTo(page)
    valid, message = page.validate_page()
    assert not valid and "signing files" in message
    page.signing_key.setText("/root/db.key")
    page.signing_cert.setText("/root/db.crt")
    assert page.validate_page() == (True, "")
    assert window.manifest.system.secure_boot_mode == "own-key"
    assert not page.secure_mode.model().item(page.secure_mode.findData("shim")).isEnabled()
    assert "not available yet" in page.secure_mode.itemText(page.secure_mode.findData("shim"))


def test_a_loaded_shim_setup_is_shown_and_refused_with_the_plain_message(make_window):
    manifest = InstallManifest()
    manifest.system.bootloader, manifest.system.uki, manifest.system.secure_boot_mode = "systemd-boot", True, "shim"
    window, page = system_page(make_window, manifest)
    assert page.secure_boot.isChecked() and page.secure_mode.currentData() == "shim"
    valid, message = page.validate_page()
    assert not valid and "shim and MOK is not available yet" in message


def test_secure_boot_with_the_nvidia_driver_is_refused_precisely(make_window):
    manifest = InstallManifest()
    manifest.hardware.gpu_driver = "nvidia"
    window, page = system_page(make_window, manifest)
    page.bootloader.set_value("systemd-boot")
    page.secure_boot.click()
    page.secure_confirm.setChecked(True)
    assert page.boot_section.secure_nvidia_note.isVisibleTo(page)
    assert page.validate_page() == (False, SECURE_BOOT_NVIDIA)


def test_a_saved_secure_boot_setup_counts_as_confirmed(make_window):
    manifest = InstallManifest()
    manifest.system.bootloader, manifest.system.uki, manifest.system.secure_boot_mode = "systemd-boot", True, "create"
    window, page = system_page(make_window, manifest)
    assert page.secure_boot.isChecked() and page.secure_confirm.isChecked()
    before = copy.deepcopy(window.manifest.hardware)
    assert page.validate_page() == (True, "")
    assert window.manifest.hardware == before
