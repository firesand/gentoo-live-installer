"""Storage page, firmware default, disks without identity, hot-plug, manual layouts, review text."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox

from gentoo_installer.manifest import InstallManifest, Partition
from gentoo_installer.pages import manual_storage
from gentoo_installer.pages.review import (
    disk_warning,
    format_lines,
    manifest_warnings,
    required_confirmations,
    save_redacted_copy,
)
from gentoo_installer.system_info import DiskInfo, PartitionInfo, SystemInfo
from tests.ui.conftest import sample_info

GIB = 1024**3
ESP = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
MSDATA = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
BIOSBOOT = "21686148-6449-6e6f-744e-656564454649"


def laptop_disk(path="/dev/nvme0n1", serial="S5Y1NX0T123456", **kwargs):
    parent = path
    return DiskInfo(
        path, "Samsung SSD 870", 500 * GIB, serial=serial, pttype="gpt", transport="nvme",
        partitions=[
            PartitionInfo(f"{path}p1", 260 * 1024**2, "vfat", label="SYSTEM", parttype=ESP, parent=parent),
            PartitionInfo(f"{path}p2", 450 * GIB, "ntfs", label="Windows", parttype=MSDATA, parent=parent),
            PartitionInfo(f"{path}p3", 40 * GIB, "ext4", label="old-gentoo", parent=parent),
            PartitionInfo(f"{path}p4", 8 * GIB, "swap", parent=parent),
        ],
        **kwargs,
    )


def info_with(*disks, uefi=True):
    return sample_info(uefi=uefi, disks=list(disks))


# --- G2: firmware follows the boot mode ---------------------------------------------------------


def test_bios_boot_seeds_legacy_bios_before_the_storage_page_is_shown(make_window):
    window = make_window(info=sample_info(uefi=False))
    assert window.manifest.storage.firmware == "bios"
    assert window.storage.firmware.currentData() == "bios"
    window._show_page(2)
    assert window.storage.firmware.currentData() == "bios"


def test_a_saved_draft_with_uefi_is_reseeded_on_a_bios_machine(make_window):
    manifest = InstallManifest()
    manifest.storage.firmware = "uefi"
    window = make_window(info=sample_info(uefi=False), manifest=manifest)
    assert window.manifest.storage.firmware == "bios"


def test_an_explicit_firmware_is_kept_but_refused_when_it_cannot_work(make_window):
    manifest = InstallManifest()
    manifest.storage.firmware = "uefi"
    window = make_window(info=sample_info(uefi=False), manifest=manifest, firmware_explicit=True)
    window.storage.disk_group.set_value("/dev/sda")
    valid, message = window.storage.validate_page()
    assert not valid
    assert "booted in legacy BIOS mode" in message and "Choose Legacy BIOS" in message


def test_uefi_machines_keep_uefi_and_bios_on_uefi_is_only_a_note(make_window):
    window = make_window()
    assert window.storage.firmware.currentData() == "uefi"
    window.storage.disk_group.set_value("/dev/sda")
    window.storage.firmware.setCurrentIndex(window.storage.firmware.findData("bios"))
    window.storage._firmware_picked()
    assert window.storage.firmware_note.isVisibleTo(window.storage) and "CSM" in window.storage.firmware_note.text()
    assert window.storage.validate_page() == (True, "")


def test_a_users_firmware_pick_survives_a_new_inspection(make_window):
    window = make_window()
    window.storage.firmware.setCurrentIndex(window.storage.firmware.findData("bios"))
    window.storage._firmware_picked()
    window.apply_info(sample_info())
    assert window.manifest.storage.firmware == "bios" or window.storage.firmware.currentData() == "bios"
    assert window.storage.firmware_touched


# --- G3: disks without serial or WWN -------------------------------------------------------------------


def virtual_disk(**kwargs):
    return DiskInfo("/dev/vda", "QEMU HARDDISK", 64 * GIB, by_path="pci-0000:00:04.0", **kwargs)


def test_a_disk_without_identity_is_flagged_and_needs_an_explicit_approval(make_window):
    window = make_window(info=info_with(virtual_disk()))
    card = window.storage.disk_group.cards["/dev/vda"]
    assert "no serial/WWN" in card.description_label.text() and "approval needed" in card.description_label.text()
    window.storage.disk_group.set_value("/dev/vda")
    assert window.storage.approval.isVisibleTo(window.storage) and window.storage.approve_path.isVisibleTo(window.storage)
    assert "pci-0000:00:04.0" in window.storage.approval_text.text()
    valid, message = window.storage.validate_page()
    assert not valid and "approve" in message


def test_approving_records_the_by_path_identity_the_daemon_verifies(make_window):
    disk = virtual_disk()
    window = make_window(info=info_with(disk))
    window.storage.disk_group.set_value("/dev/vda")
    window.storage.approve_path.setChecked(True)
    assert window.storage.validate_page() == (True, "")
    storage = window.manifest.storage
    assert storage.device_id_kind == "path"
    assert storage.device_id == f"pci-0000:00:04.0|{64 * GIB}|QEMU HARDDISK"
    assert not [e for e in window.manifest.validate_sections()["storage"] if "serial" in e]
    window._show_page(1)
    window._show_page(2)  # reloading the page restores the approval
    assert window.storage.approve_path.isChecked()
    assert "no serial number" in "\n".join(manifest_warnings(window.manifest, window.info))


def test_a_disk_without_any_stable_path_is_refused_with_a_hint(make_window):
    disk = DiskInfo("/dev/vda", "QEMU HARDDISK", 64 * GIB)  # no serial, no WWN, no by-path link
    window = make_window(info=info_with(disk))
    window.storage.disk_group.set_value("/dev/vda")
    assert not window.storage.approve_path.isVisibleTo(window.storage)
    valid, message = window.storage.validate_page()
    assert not valid and "virtio-blk-pci" in message


def test_switching_to_a_disk_with_identity_resets_the_approval(make_window):
    window = make_window(info=info_with(virtual_disk(), laptop_disk()))
    window.storage.disk_group.set_value("/dev/vda")
    window.storage.approve_path.setChecked(True)
    window.storage.disk_group.set_value("/dev/nvme0n1")
    assert not window.storage.approval.isVisibleTo(window.storage)
    window.storage.validate_page()
    assert window.manifest.storage.device_id_kind == "" and window.manifest.storage.device_id == "S5Y1NX0T123456"


def test_a_disk_that_is_too_small_is_refused_for_the_guided_layout(make_window):
    window = make_window(info=info_with(DiskInfo("/dev/sdb", "Tiny", 8 * GIB, serial="t")))
    window.storage.disk_group.set_value("/dev/sdb")
    valid, message = window.storage.validate_page()
    assert not valid and "only 8.0 GiB" in message and "at least 16 GiB" in message


# --- the live medium and read-only disks --------------------------------------------------------------------


def test_the_live_medium_and_read_only_disks_cannot_be_picked(make_window):
    usb = DiskInfo("/dev/sdb", "USB stick", 16 * GIB, serial="U1", live_medium=True, mounted=True)
    locked = DiskInfo("/dev/sdc", "Card", 32 * GIB, serial="C1", read_only=True)
    window = make_window(info=info_with(laptop_disk(), usb, locked))
    cards = window.storage.disk_group.cards
    assert cards["/dev/nvme0n1"].isEnabled()
    assert not cards["/dev/sdb"].isEnabled() and "installer was started from" in cards["/dev/sdb"].toolTip()
    assert "live installer medium" in cards["/dev/sdb"].description_label.text()
    assert not cards["/dev/sdc"].isEnabled()


# --- S2: the cards say what the disk is and what is on it -------------------------------------------------------------


def test_cards_show_model_size_serial_tail_and_current_contents(make_window):
    window = make_window(info=info_with(laptop_disk()))
    card = window.storage.disk_group.cards["/dev/nvme0n1"]
    assert "Samsung SSD 870" in card.title_label.text()
    text = card.description_label.text()
    assert "500.0 GiB" in text and "serial …123456" in text and "NVMe" in text
    assert "4 partitions" in text and 'ntfs 450.0 GiB “Windows”' in text


def test_review_names_the_disk_by_model_serial_and_contents_and_keeps_the_typed_gate(make_window):
    window = make_window(info=info_with(laptop_disk()))
    window.storage.disk_group.set_value("/dev/nvme0n1")
    window.storage.save_manifest()
    for page in window.pages[:6]:
        page.save_manifest()
    window.accounts.password.setText("user-password")
    window.accounts.password_confirm.setText("user-password")
    window.accounts.root_password.setText("root-password")
    window.accounts.root_confirm.setText("root-password")
    window.accounts.save_manifest()
    window._show_page(6)
    warning = window.review.warning.text()
    assert "All existing data on /dev/nvme0n1 (Samsung SSD 870, 500.0 GiB, serial …123456) will be permanently erased" in warning
    assert 'ntfs 450.0 GiB “Windows”' in warning and "Other disks will not be modified" in warning
    assert window.review.validate_page()[0] is False
    window.review.confirmation.setText("/dev/nvme0n1")  # the typed phrase is exactly the device path
    assert window.review.validate_page() == (True, "")
    disk_row = next(window.review.summary.item(r, 1).text() for r in range(window.review.summary.rowCount()) if window.review.summary.item(r, 0).text() == "Disk")
    assert "Samsung SSD 870" in disk_row and "serial …123456" in disk_row


def test_manual_review_lists_every_row_as_format_or_keep_with_its_current_contents():
    disk = laptop_disk()
    info = info_with(disk)
    manifest = InstallManifest()
    manifest.storage.mode = "manual"
    manifest.storage.device = disk.path
    manifest.storage.partitions = [
        Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False),
        Partition("/dev/nvme0n1p3", "/", "ext4", format=True),
        Partition("/dev/nvme0n1p4", "", "swap", format=True),
    ]
    lines = format_lines(manifest, info)
    assert lines[0].startswith("KEEP    /dev/nvme0n1p1 → /efi (vfat) on Samsung SSD 870 serial …123456, now vfat 260.0 MiB “SYSTEM”")
    assert lines[1].startswith("FORMAT  /dev/nvme0n1p3 → / (ext4)") and "old-gentoo" in lines[1]
    assert "→ swap (swap)" in lines[2]
    warning = disk_warning(manifest, info)
    assert warning.startswith("Manual installation on /dev/nvme0n1 (Samsung SSD 870") and "marked FORMAT" in warning
    assert required_confirmations(manifest, info) == ["/dev/nvme0n1"]


def test_every_touched_disk_needs_its_own_typed_confirmation(make_window):
    other = DiskInfo("/dev/sdb", "Data disk", 1000 * GIB, serial="D1", partitions=[PartitionInfo("/dev/sdb1", 900 * GIB, "ntfs", parent="/dev/sdb")])
    window = make_window(info=info_with(laptop_disk(), other))
    window.manifest.storage.mode = "manual"
    window.manifest.storage.device = "/dev/nvme0n1"
    window.manifest.storage.partitions = [
        Partition("/dev/nvme0n1p3", "/", "ext4", format=True), Partition("/dev/sdb1", "/home", "ext4", format=True),
    ]
    window._show_page(6)
    assert [path for path, _ in window.review.extra_confirmations] == ["/dev/sdb"]
    window.review.confirmation.setText("/dev/nvme0n1")
    valid, message = window.review.validate_page()
    assert not valid and "/dev/sdb" in message


def test_manual_installs_ask_before_formatting(make_window, monkeypatch):
    window = make_window(info=info_with(laptop_disk()))
    window.manifest.storage.mode = "manual"
    window.manifest.storage.device = "/dev/nvme0n1"
    window.manifest.storage.partitions = [
        Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False), Partition("/dev/nvme0n1p3", "/", "ext4", format=True)]
    shown = []

    def warning(parent, title, text, buttons, default):
        shown.append((title, text))
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "warning", warning)
    window.start_installation()
    assert shown[0][0] == "Format these partitions?"
    assert "/dev/nvme0n1p3" in shown[0][1] and "/dev/nvme0n1p1" not in shown[0][1]


def test_review_blocks_when_the_live_system_lacks_a_tool_the_selection_needs(make_window):
    window = make_window(info=sample_info(tools={"cryptsetup": False, "mkfs.vfat": True, "swapon": True, "swapoff": True},
                                          missing_for_selection=["mkfs.xfs"]))  # stale hint: ignored
    window.manifest.storage.device = "/dev/sda"
    window.manifest.storage.luks = True
    window.manifest.storage.luks_passphrase = "long-enough-passphrase"
    window._show_page(6)
    window.review.confirmation.setText("/dev/sda")
    valid, message = window.review.validate_page()
    assert not valid and "cryptsetup" in message and "mkfs.xfs" not in message
    window.manifest.storage.luks = False
    window.manifest.storage.luks_passphrase = ""
    window._show_page(6)
    window.review.confirmation.setText("/dev/sda")
    assert "cryptsetup" not in window.review.validate_page()[1]


def test_exported_settings_carry_no_secret_and_no_proxy_password(tmp_path):
    manifest = InstallManifest()
    manifest.accounts.password = "user-password"
    manifest.network.proxy = "http://alice:S3cretPass@proxy.corp:3128"
    path = tmp_path / "out.json"
    save_redacted_copy(manifest, path)
    text = path.read_text()
    assert "user-password" not in text and "S3cretPass" not in text and "alice:<redacted>@proxy.corp" in text
    assert oct(path.stat().st_mode & 0o777) == "0o600"


# --- NOBS-8: Check again refreshes the Storage page too --------------------------------------------------------------------


def test_a_hot_plugged_disk_appears_and_the_selection_is_kept(make_window):
    window = make_window()
    window.storage.disk_group.set_value("/dev/sda")
    assert list(window.storage.disk_group.cards) == ["/dev/sda"]
    window.apply_info(sample_info(disks=[DiskInfo("/dev/sda", "Virtual disk", 64 * GIB, serial="test-disk"), DiskInfo("/dev/sdb", "Dock", 32 * GIB, serial="dock")]))
    assert list(window.storage.disk_group.cards) == ["/dev/sda", "/dev/sdb"]
    assert window.storage.disk_group.value() == "/dev/sda"


def test_a_disk_that_vanished_clears_the_selection_with_a_message(make_window):
    window = make_window(info=info_with(DiskInfo("/dev/sdb", "Dock", 32 * GIB, serial="dock"), laptop_disk()))
    window.storage.disk_group.set_value("/dev/sdb")
    window.apply_info(info_with(laptop_disk()))
    assert window.storage.disk_group.value() == ""
    assert "no longer present" in window.storage.disk_notice.text()
    valid, message = window.storage.validate_page()
    assert not valid and "no longer present" in message


def test_pending_inspection_does_not_wipe_the_manifest_disk(make_window):
    manifest = InstallManifest()
    manifest.storage.device = "/dev/sda"
    manifest.storage.device_id = "test-disk"
    window = make_window(manifest=manifest, background_inspection=False)
    pending = SystemInfo.pending()
    window.apply_info(pending)
    window.storage.save_manifest()
    assert window.manifest.storage.device == "/dev/sda" and window.manifest.storage.device_id == "test-disk"
    window.apply_info(sample_info())
    assert window.storage.disk_group.value() == "/dev/sda"


# --- NEW-GD-6: the manual editor picks from what exists ---------------------------------------------------------------------------


@pytest.fixture
def manual(make_window):
    window = make_window(info=info_with(laptop_disk()))
    window.storage.disk_group.set_value("/dev/nvme0n1")
    window.storage.mode.setCurrentIndex(1)
    return window, window.storage.manual_editor


def device_choices(editor, row=0):
    combo = editor.table.cellWidget(row, 0)
    return [combo.itemData(i) for i in range(combo.count())]


def pick(editor, row, device):
    combo = editor.table.cellWidget(row, 0)
    combo.setCurrentIndex(combo.findData(device))


def test_the_device_combo_offers_the_partitions_of_the_chosen_disk(manual):
    window, editor = manual
    editor.add_row()
    assert device_choices(editor) == ["", "/dev/nvme0n1p1", "/dev/nvme0n1p2", "/dev/nvme0n1p3", "/dev/nvme0n1p4"]
    text = editor.table.cellWidget(0, 0).itemText(1)
    assert "260.0 MiB" in text and "vfat" in text and "SYSTEM" in text and "EFI System" in text


def test_picking_the_esp_proposes_efi_unformatted_and_ntfs_is_kept(manual):
    window, editor = manual
    editor.add_row()
    pick(editor, 0, "/dev/nvme0n1p1")
    row = editor.rows()[0]
    assert (row.mountpoint, row.filesystem, row.format) == ("/efi", "vfat", False)
    editor.add_row()
    pick(editor, 1, "/dev/nvme0n1p3")
    root = editor.rows()[1]
    assert (root.mountpoint, root.filesystem, root.format) == ("/", "ext4", False)  # existing data stays
    assert "Windows partitions stay untouched" in editor.note.text() and "/dev/nvme0n1p2" in editor.note.text()
    assert editor.table.item(1, 5).text().startswith("ext4 40.0 GiB")


def test_a_device_that_is_not_in_the_scan_is_rejected(manual):
    window, editor = manual
    editor.set_rows([Partition("/dev/sdq9", "/", "ext4"), Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False)])
    problems = editor.problems("uefi")
    assert any("/dev/sdq9" in p and "was not found" in p for p in problems)
    valid, message = window.storage.validate_page()
    assert not valid and "/dev/sdq9" in message


def test_partitions_of_other_disks_mounted_or_duplicate_rows_are_rejected(make_window):
    other = DiskInfo("/dev/sdb", "Data", 100 * GIB, serial="D1", partitions=[PartitionInfo("/dev/sdb1", 50 * GIB, "ext4", parent="/dev/sdb")])
    mounted = laptop_disk()
    mounted.partitions[2].mountpoints = ("/mnt/old",)
    window = make_window(info=info_with(mounted, other))
    window.storage.disk_group.set_value("/dev/nvme0n1")
    editor = window.storage.manual_editor
    editor.set_rows([
        Partition("/dev/sdb1", "/home", "ext4", format=False),
        Partition("/dev/nvme0n1p3", "/", "ext4", format=False),
        Partition("/dev/nvme0n1p3", "/var", "ext4", format=False),
        Partition("/dev/nvme0n1p1", "/", "vfat", format=False),
    ])
    problems = "\n".join(editor.problems("bios"))
    assert "/dev/sdb1 is on /dev/sdb, not on the selected disk /dev/nvme0n1" in problems
    assert "mounted at /mnt/old" in problems
    assert "used twice" in problems and "/ is used by two rows" in problems


def test_kept_partitions_must_match_their_declared_filesystem(manual):
    window, editor = manual
    editor.set_rows([Partition("/dev/nvme0n1p2", "/", "ext4", format=False), Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False)])
    assert any("contains ntfs, not ext4" in p and "Tick Format" in p for p in editor.problems("uefi"))
    editor.set_rows([Partition("/dev/nvme0n1p3", "/", "ext4", format=False), Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False)])
    assert editor.problems("uefi") == []


def test_uefi_needs_an_esp_and_bios_on_gpt_needs_a_bios_boot_partition(manual):
    window, editor = manual
    editor.set_rows([Partition("/dev/nvme0n1p3", "/", "ext4", format=False)])
    assert any("EFI System Partition" in p for p in editor.problems("uefi"))
    assert any("BIOS boot partition" in p for p in editor.problems("bios"))
    window.info.disks[0].partitions.append(PartitionInfo("/dev/nvme0n1p5", 1024**2, "", parttype=BIOSBOOT, parent="/dev/nvme0n1"))
    assert not any("BIOS boot partition" in p for p in editor.problems("bios"))


def test_swap_rows_round_trip_with_an_empty_mountpoint(manual):
    window, editor = manual
    editor.set_rows([Partition("/dev/nvme0n1p4", "", "swap", format=False)])
    row = editor.rows()[0]
    assert (row.mountpoint, row.filesystem) == ("", "swap")
    assert editor.table.cellWidget(0, 1).currentText() == "swap"
    editor.add_row()
    pick(editor, 1, "/dev/nvme0n1p4")  # picking a swap partition proposes swap
    assert editor.rows()[1].filesystem == "swap" and editor.rows()[1].mountpoint == ""


def test_suggest_layout_fills_root_esp_and_swap_from_the_disk(manual):
    window, editor = manual
    assert editor.suggest_layout(confirm=False)
    rows = {row.device: row for row in editor.rows()}
    assert rows["/dev/nvme0n1p3"].mountpoint == "/" and rows["/dev/nvme0n1p3"].format is False
    assert rows["/dev/nvme0n1p1"].mountpoint == "/efi"
    assert rows["/dev/nvme0n1p4"].filesystem == "swap"
    assert editor.problems("uefi") == []


def test_open_gparted_starts_it_detached_and_explains_a_failure(manual, monkeypatch):
    window, editor = manual
    calls = []
    monkeypatch.setattr(manual_storage, "start_detached", lambda program, arguments: (calls.append(program), True)[1])
    assert editor.open_gparted() and calls == ["gparted"]
    monkeypatch.setattr(manual_storage, "start_detached", lambda program, arguments: False)
    assert not editor.open_gparted() and editor.gparted_message.isVisibleTo(editor)


def test_rescan_asks_the_window_to_check_again(manual, qtbot):
    window, editor = manual
    with qtbot.waitSignal(editor.rescanRequested, timeout=500):
        editor.rescan_button.click()


def test_manual_rows_survive_a_navigation_round_trip_exactly(manual):
    window, editor = manual
    rows = [Partition("/dev/nvme0n1p3", "/", "ext4", format=False, label="gentoo"), Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False)]
    editor.set_rows(rows)
    window.storage.save_manifest()
    window._show_page(1)
    window._show_page(2)
    assert window.manifest.storage.partitions == rows
    assert window.storage.manual_editor.rows() == rows


# --- K4 / B12: only what the installer can finish is offered --------------------------------------------------------------------------


def test_raid_is_not_offered_and_a_loaded_raid_manifest_is_refused(make_window):
    window = make_window()
    assert not window.storage.raid_widget.isVisibleTo(window.storage)
    window.manifest.storage.raid_level = "1"
    window.storage.disk_group.set_value("/dev/sda")
    window.storage.load_manifest()
    valid, message = window.storage.validate_page()
    assert not valid and "RAID" in message


def test_manual_kernel_genkernel_and_none_are_not_offered_nor_silently_replaced(make_window):
    manifest = InstallManifest()
    manifest.system.kernel = "manual"
    manifest.system.initramfs = "none"
    window = make_window(manifest=manifest)
    window._show_page(3)
    kernel = window.system.kernel
    assert kernel.currentData() == "manual" and "not available" in kernel.currentText()
    assert window.manifest.system.kernel == "manual"  # never replaced behind the user's back
    valid, message = window.system.validate_page()
    assert not valid and "kernel" in message


def test_dual_boot_needs_grub(make_window):
    window = make_window()
    window.system.bootloader.setCurrentIndex(window.system.bootloader.findData("systemd-boot"))
    assert not window.system.dual_boot.isEnabled()
    window.system.bootloader.setCurrentIndex(window.system.bootloader.findData("grub"))
    assert window.system.dual_boot.isEnabled()


def test_uefi_only_bootloaders_are_greyed_out_for_a_bios_install(make_window):
    window = make_window(info=sample_info(uefi=False))
    window._show_page(3)
    model = window.system.bootloader.model()
    assert model.item(window.system.bootloader.findData("grub")).isEnabled()
    assert not model.item(window.system.bootloader.findData("systemd-boot")).isEnabled()
    assert not model.item(window.system.bootloader.findData("efi-stub")).isEnabled()


def test_the_storage_mode_choice_is_always_reachable(make_window):
    window = make_window()
    window.mode.setCurrentIndex(window.mode.findData("guided"))
    assert window.storage.mode.isVisibleTo(window.storage)
