"""STOR (GUI): no preselected disk, what is on the disk, derived firmware, the table of what is created, swap, filesystems, /boot, fstab names."""

from __future__ import annotations

import dataclasses

import pytest

from gentoo_installer import layout
from gentoo_installer.manifest import InstallManifest
from gentoo_installer.pages import storage as storage_module
from gentoo_installer.system_info import DiskInfo, PartitionInfo
from tests.ui.conftest import sample_info
from tests.ui.test_gui_storage_safety import ESP, GIB, data_disk, info_of, laptop, select


def plain_disk(path="/dev/sda", size=200 * GIB, model="Samsung SSD 870", **kwargs):
    return DiskInfo(path, model, size, serial="SER" + path[-1], pttype="gpt", transport="sata", **kwargs)


def with_memory(info, gib):
    info.memory_gib = gib
    info.hardware = {"memory": {"total_kib": int(gib * 1024 * 1024)}, "firmware": {"mode": "uefi" if info.uefi else "bios"}}
    return info


# --- STOR-01 -------------------------------------------------------------------------------------------------------------


def test_nothing_is_selected_and_the_layout_is_greyed_out_with_a_hint(make_window):
    window = make_window(info=info_of(plain_disk(), plain_disk("/dev/sdb")))
    page = window.storage
    assert page.disk_group.value() == ""
    assert page.choose_hint.isVisibleTo(page) and "Choose a disk" in page.choose_hint.text()
    assert not page.guided.isEnabled() and not page.encryption.isEnabled() and not page.advanced_section.isEnabled()
    assert not page.existing.isVisibleTo(page) and not page.preview_card.isVisibleTo(page)
    assert page.validate_page() == (False, "Select the disk you want to use.")
    select(window, "/dev/sda")
    assert page.guided.isEnabled() and page.encryption.isEnabled() and not page.choose_hint.isVisibleTo(page)


def test_the_mode_names_say_what_happens(make_window):
    page = make_window().storage
    assert [page.mode.itemText(i) for i in range(page.mode.count())] == [
        "Erase the whole disk and install Gentoo (all data lost)", "Choose partitions myself"]


def test_the_disk_now_table_lists_every_partition(make_window):
    window = make_window(info=info_of(laptop()))
    page = select(window, "/dev/nvme0n1")
    table = page.existing.table
    assert page.existing.isVisibleTo(page) and table.rowCount() == 3
    assert [table.item(1, c).text() for c in range(4)] == ["/dev/nvme0n1p2", "ntfs", "Windows", "450.0 GiB"]
    assert "Everything shown here is removed if you continue" in page.existing.note.text()


def test_a_blank_disk_says_it_has_no_partitions(make_window):
    page = select(make_window(info=info_of(plain_disk())), "/dev/sda")
    assert not page.existing.table.isVisibleTo(page) and page.existing.empty.isVisibleTo(page)


def test_windows_gets_a_red_card_in_the_erase_mode_only(make_window):
    window = make_window(info=info_of(laptop(), plain_disk()))
    page = select(window, "/dev/nvme0n1")
    assert page.windows_note.isVisibleTo(page)
    assert page.windows_note.text() == "Windows is installed on this disk. Guided mode will ERASE it, including Windows and its recovery partition."
    page.mode.setCurrentIndex(page.mode.findData("manual"))
    assert not page.windows_note.isVisibleTo(page)
    page.mode.setCurrentIndex(page.mode.findData("erase"))
    select(window, "/dev/sda")
    assert not page.windows_note.isVisibleTo(page)


@pytest.mark.parametrize("disk", [
    plain_disk("/dev/mmcblk0", 64 * GIB, "SD card"),
    plain_disk("/dev/sdb", 32 * GIB, "USB stick", removable=True),
])
def test_sd_cards_and_removable_disks_get_the_yellow_note_but_stay_allowed(make_window, disk):
    window = make_window(info=info_of(disk))
    page = select(window, disk.path)
    assert page.removable_note.isVisibleTo(page) and "wear out quickly" in page.removable_note.text()
    assert page.disk_group.cards[disk.path].isEnabled()
    valid, message = page.validate_page()
    assert "wear" not in message and "removable" not in message.lower() and "SD card" not in message


def test_an_internal_disk_has_no_notes(make_window):
    page = select(make_window(info=info_of(plain_disk())), "/dev/sda")
    assert not page.removable_note.isVisibleTo(page) and not page.windows_note.isVisibleTo(page)


# --- STOR-02 -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("uefi", "words", "firmware"), [
    (True, "This computer started in UEFI mode, so Gentoo will boot with UEFI. The disk gets a GPT partition table with a small EFI system partition.", "uefi"),
    (False, "This computer started in BIOS mode, so Gentoo will boot with BIOS. The disk gets a GPT partition table with a small BIOS boot partition", "bios"),
])
def test_guided_derives_firmware_and_table_from_the_boot_mode(make_window, uefi, words, firmware):
    window = make_window(info=info_of(plain_disk(), uefi=uefi))
    page = select(window, "/dev/sda")
    assert words in page.boot_mode_text.text()
    assert not page.firmware.isVisibleTo(page) and not page.table.isVisibleTo(page)  # behind "More options"
    assert page.validate_page() == (True, "")
    assert (window.manifest.storage.firmware, window.manifest.storage.table) == (firmware, "gpt")


def test_advanced_shows_firmware_and_table_with_help_and_validates_the_overrides(make_window):
    window = make_window(info=info_of(plain_disk(size=3 * 1024 * GIB), uefi=False))
    page = select(window, "/dev/sda")
    window.mode.setCurrentIndex(window.mode.findData("advanced"))
    assert page.firmware.isVisibleTo(page) and page.table.isVisibleTo(page)
    page.table.setCurrentIndex(page.table.findData("dos"))
    assert "DOS (MBR) partition table" in page.boot_mode_text.text()
    valid, message = page.validate_page()
    assert not valid and "cannot address more than 2 TiB" in message
    page.firmware.setCurrentIndex(page.firmware.findData("uefi"))
    assert "You chose UEFI in the Advanced options" in page.boot_mode_text.text()


def test_the_help_notes_use_the_handbook_words(make_window):
    page = make_window().storage
    notes = page.findChildren(storage_module.HelpNote)
    texts = " ".join(note.note_text for note in notes)
    assert "GPT is the modern way to divide a disk. Choose MBR only for a very old computer" in texts
    assert "Almost every computer made after 2012 uses UEFI; the installer detects this for you." in texts


# --- STOR-03 -------------------------------------------------------------------------------------------------------------


def test_the_page_shows_the_table_and_the_bar_of_what_will_be_created(make_window):
    window = make_window(info=with_memory(info_of(plain_disk()), 16))
    page = select(window, "/dev/sda")
    assert page.preview_card.isVisibleTo(page)
    table = page.preview.table
    rows = [[table.item(r, c).text() for c in range(5)] for r in range(table.rowCount())]
    assert [row[:3] for row in rows] == [["EFI system partition", "/efi", "FAT32"], ["Swap", "-", "swap"], ["System", "/", "ext4"]]
    assert rows[0][4].startswith("EFI system partition: the small area your computer reads first; Gentoo puts its boot files there")
    assert rows[0][3] == "1.0 GiB" and rows[1][3] == "8.0 GiB"  # 16 GiB of memory: the Handbook's 8 GiB
    assert len(page.preview.bar.segments) == 3 and abs(sum(page.preview.bar.shares()) - 1) < 1e-9
    assert "exactly what will be written to the disk" in page.preview.caption.text()
    page.home.setChecked(True)
    assert table.rowCount() == 4 and table.item(2, 1).text() == "/home"
    page.mode.setCurrentIndex(page.mode.findData("manual"))
    assert not page.preview_card.isVisibleTo(page)


def test_the_table_equals_the_plan_the_review_uses(make_window):
    window = make_window(info=info_of(plain_disk()))
    page = select(window, "/dev/sda")
    page.luks.setChecked(True)
    page.save_manifest()
    plan = layout.plan_storage(window.manifest, window.info.block_devices(), memory_kib=page._memory_kib())
    assert page.preview.rows == layout.plan_table(plan.disks[0], lvm=False)
    assert [r.role for r in page.preview.rows] == ["efi", "boot", "root", "swapfile"]


# --- STOR-04 -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("gib", "text", "mib"), [(1, "4", 4096), (4, "4", 4096), (16, "8", 8192), (48, "16", 16384), (96, "8", 8192)])
def test_the_recommended_swap_follows_the_handbook_and_is_written_as_gib_and_mib(make_window, gib, text, mib):
    window = make_window(info=with_memory(info_of(plain_disk()), gib))
    page = select(window, "/dev/sda")
    assert page.swap_mode.currentData() == "recommended" or page.swap_mode.currentData() == "custom"
    page.swap_mode.setCurrentIndex(page.swap_mode.findData("recommended"))
    assert page.swap_mode.itemText(0) == f"Recommended for this computer ({text} GiB)"
    page.save_manifest()
    storage = window.manifest.storage
    assert (storage.swap_choice, storage.swap_mib) == ("recommended", mib)


@pytest.mark.parametrize(("gib", "mib"), [(4, 8192), (16, 24576), (48, 73728), (96, 8192)])
def test_hibernation_sizes_the_swap_for_the_memory(make_window, gib, mib):
    window = make_window(info=with_memory(info_of(plain_disk(size=400 * GIB)), gib))
    page = select(window, "/dev/sda")
    window.mode.setCurrentIndex(window.mode.findData("advanced"))
    page.swap_mode.setCurrentIndex(page.swap_mode.findData("recommended"))
    page.hibernate.setChecked(True)
    page.save_manifest()
    storage = window.manifest.storage
    if gib > 64:
        assert not page.hibernate.isEnabled() and storage.swap_choice == "recommended"
    else:
        assert (storage.swap_choice, storage.swap_mib) == ("hibernate", mib)


def test_none_custom_and_zram(make_window):
    window = make_window(info=info_of(plain_disk()))
    page = select(window, "/dev/sda")
    page.swap_mode.setCurrentIndex(page.swap_mode.findData("none"))
    page.save_manifest()
    assert (window.manifest.storage.swap_choice, window.manifest.storage.swap_mib) == ("none", 0)
    assert layout.fstab_preview(window.manifest).count(" swap ") == 0
    page.swap.setValue(6)  # typing a size switches to Custom
    assert page.swap_mode.currentData() == "custom"
    page.save_manifest()
    assert (window.manifest.storage.swap_choice, window.manifest.storage.swap_mib) == ("custom", 6144)
    window.mode.setCurrentIndex(window.mode.findData("advanced"))
    page.swap_kind.setCurrentIndex(page.swap_kind.findData("zram"))
    page.save_manifest()
    storage = window.manifest.storage
    assert (storage.swap_kind, storage.swap_mib) == ("zram", 0)
    assert [r.role for r in page.preview.rows] == ["efi", "root"]


def test_an_encrypted_disk_keeps_its_swap_inside_the_root(make_window):
    window = make_window(info=info_of(plain_disk()))
    page = select(window, "/dev/sda")
    window.mode.setCurrentIndex(window.mode.findData("advanced"))
    page.swap_kind.setCurrentIndex(page.swap_kind.findData("partition"))
    page.luks.setChecked(True)
    assert page.swap_kind.currentData() == "auto" and "required with encryption" in page.swap_kind.currentText()
    model = page.swap_kind.model()
    assert not model.item(page.swap_kind.findData("partition")).isEnabled()
    assert "swapfile" in [r.role for r in page.preview.rows]
    page.luks.setChecked(False)
    assert model.item(page.swap_kind.findData("partition")).isEnabled() and "swap" in [r.role for r in page.preview.rows]


def test_a_swap_size_that_is_not_whole_gib_survives_a_visit(make_window):
    manifest = InstallManifest()
    manifest.storage.swap_mib = 1536
    window = make_window(manifest=manifest, info=sample_info())
    window.storage.save_manifest()
    assert (window.manifest.storage.swap_choice, window.manifest.storage.swap_mib) == ("custom", 1536)


def test_hibernation_with_zram_is_refused_on_the_page(make_window):
    window = make_window(info=with_memory(info_of(plain_disk()), 16))
    page = select(window, "/dev/sda")
    window.mode.setCurrentIndex(window.mode.findData("advanced"))
    page.swap_kind.setCurrentIndex(page.swap_kind.findData("zram"))
    page.hibernate.setChecked(True)
    valid, message = page.validate_page()
    assert not valid and "zram" in message


# --- STOR-05 -------------------------------------------------------------------------------------------------------------


def test_filesystem_choices_carry_the_handbook_one_liners_and_f2fs_is_advanced(make_window):
    window = make_window(info=info_of(plain_disk()))
    page = select(window, "/dev/sda")
    assert [page.filesystem.itemData(i) for i in range(page.filesystem.count())] == ["ext4", "xfs", "btrfs"]
    assert page.filesystem.currentData() == "ext4" and "The Handbook recommends XFS" in page.fs_note.text()
    page.filesystem.setCurrentIndex(page.filesystem.findData("xfs"))
    assert page.fs_note.text() == "XFS: fast and the Handbook's recommendation; cannot be made smaller later."
    window.mode.setCurrentIndex(window.mode.findData("advanced"))
    assert [page.filesystem.itemData(i) for i in range(page.filesystem.count())] == ["ext4", "xfs", "btrfs", "f2fs"]
    assert page.filesystem.currentData() == "xfs"  # nothing was lost when the list was rebuilt
    page.filesystem.setCurrentIndex(page.filesystem.findData("f2fs"))
    window.mode.setCurrentIndex(window.mode.findData("guided"))
    assert page.filesystem.currentData() == "f2fs"  # a chosen F2FS stays visible


def test_f2fs_with_grub_needs_a_separate_boot(make_window):
    window = make_window(info=info_of(plain_disk()))
    page = select(window, "/dev/sda")
    window.mode.setCurrentIndex(window.mode.findData("advanced"))
    page.filesystem.setCurrentIndex(page.filesystem.findData("f2fs"))
    valid, message = page.validate_page()
    assert not valid and "F2FS" in message and "separate_boot" not in message
    page.separate_boot.setChecked(True)
    assert page.validate_page() == (True, "")


@pytest.mark.parametrize(("model", "shown"), [("INTEL SSDPEKKW512G7", True), ("INTEL SSDPEKKF256G8", True), ("Samsung SSD 870", False), ("INTEL SSDPEKNW512G8", False)])
def test_xfs_on_an_early_intel_nvme_warns(make_window, model, shown):
    window = make_window(info=info_of(plain_disk(model=model)))
    page = select(window, "/dev/sda")
    page.filesystem.setCurrentIndex(page.filesystem.findData("xfs"))
    assert page.xfs_note.isVisibleTo(page) is shown
    if shown:
        assert page.xfs_note.text() == "Some early Intel NVMe drives can corrupt XFS; update the drive firmware or choose ext4."
        page.filesystem.setCurrentIndex(page.filesystem.findData("ext4"))
        assert not page.xfs_note.isVisibleTo(page)


# --- STOR-06 / STOR-10 ---------------------------------------------------------------------------------------------------


def test_guided_has_one_encryption_checkbox_and_it_asks_for_a_passphrase(make_window):
    window = make_window(info=info_of(plain_disk()))
    page = select(window, "/dev/sda")
    assert not page.luks.isChecked() and page.luks.text() == "Encrypt my disk (asks for a passphrase at every start)"
    page.luks.setChecked(True)
    assert not page.validate_page()[0]  # no passphrase yet
    page.passphrase.setText("correct horse battery")
    page.passphrase_confirm.setText("correct horse battery")
    assert page.validate_page() == (True, "")
    assert window.manifest.storage.luks and [r.role for r in page.preview.rows][:2] == ["efi", "boot"]


def test_separate_boot_and_noauto(make_window):
    window = make_window(info=info_of(plain_disk()))
    page = select(window, "/dev/sda")
    window.mode.setCurrentIndex(window.mode.findData("advanced"))
    assert not page.separate_boot.isChecked() and not page.boot_noauto.isChecked() and not page.boot_noauto_note.isVisibleTo(page)
    page.separate_boot.setChecked(True)
    assert [r.role for r in page.preview.rows] == ["efi", "boot", "swap", "root"]
    page.boot_noauto.setChecked(True)
    assert "Kernel updates need /boot to be mounted" in page.boot_noauto_note.text() and page.boot_noauto_note.isVisibleTo(page)
    assert "defaults,noatime,noauto" in layout.fstab_preview(dataclasses.replace(window.manifest, storage=page._planned_storage()))
    page.boot_noauto.setChecked(False)
    page.separate_boot.setChecked(False)
    page.boot_noauto.setChecked(True)  # asking for noauto brings the partition back
    assert page.separate_boot.isChecked()


def test_lvm_help_and_the_fstab_choices(make_window):
    window = make_window(info=info_of(plain_disk()))
    page = select(window, "/dev/sda")
    window.mode.setCurrentIndex(window.mode.findData("advanced"))
    assert "Use LVM (volumes that can be resized later)" == page.lvm.text()
    texts = " ".join(note.note_text for note in page.findChildren(storage_module.HelpNote))
    assert "LVM lets you resize or add storage later without repartitioning; most home users do not need it." in texts
    assert "noatime stops Linux writing a timestamp every time a file is read, which is faster and gentler on SSDs." in texts
    page.fstab_ident.setCurrentIndex(page.fstab_ident.findData("partuuid"))
    page.atime.setCurrentIndex(page.atime.findData("lazytime"))
    page.save_manifest()
    storage = window.manifest.storage
    assert (storage.fstab_ident, storage.atime) == ("partuuid", "lazytime")
    page.table.setCurrentIndex(page.table.findData("dos"))
    valid, message = page.validate_page()
    assert not valid and "GPT" in message


def test_every_new_control_round_trips_without_a_change(make_window):
    manifest = InstallManifest()
    storage = manifest.storage
    storage.swap_choice, storage.swap_mib, storage.swap_kind = "hibernate", 24576, "file"
    storage.filesystem, storage.separate_boot, storage.boot_noauto = "f2fs", True, True
    storage.fstab_ident, storage.atime, storage.lvm = "label", "relatime", True
    storage.firmware = "uefi"
    window = make_window(manifest=manifest, info=with_memory(info_of(plain_disk()), 16))
    before = dataclasses.replace(window.manifest.storage)
    for _ in range(2):
        window.storage.save_manifest()
        window.storage.load_manifest()
    after = window.manifest.storage
    for name in ("swap_choice", "swap_mib", "swap_kind", "filesystem", "separate_boot", "boot_noauto", "fstab_ident", "atime", "lvm", "firmware", "table", "luks", "separate_home"):
        assert getattr(after, name) == getattr(before, name), name
    assert window.storage.advanced_section.is_open()  # values the group holds open it


def test_collapsing_to_guided_loses_nothing(make_window):
    window = make_window(info=info_of(plain_disk()))
    page = select(window, "/dev/sda")
    window.mode.setCurrentIndex(window.mode.findData("advanced"))
    page.atime.setCurrentIndex(page.atime.findData("relatime"))
    page.lvm.setChecked(True)
    window.mode.setCurrentIndex(window.mode.findData("guided"))
    page.save_manifest()
    assert window.manifest.storage.atime == "relatime" and window.manifest.storage.lvm


def test_the_detected_swap_reaches_the_page_and_a_chosen_one_is_left_alone(make_window):
    window = make_window(info=with_memory(info_of(plain_disk()), 16))
    page = window.storage
    assert page.swap_mode.currentData() == "recommended" and page.swap_mode.currentText() == "Recommended for this computer (8 GiB)"
    window.apply_info(with_memory(info_of(plain_disk()), 48))
    assert page.swap_mode.currentText() == "Recommended for this computer (16 GiB)"  # Check again corrected the first look
    page.swap_mode.setCurrentIndex(page.swap_mode.findData("custom"))
    page.swap.setValue(3)
    window.apply_info(with_memory(info_of(plain_disk()), 4))
    assert page.swap_mode.currentData() == "custom" and page.swap.value() == 3


# --- review fixes: the inline hint and the fstab preview ---------------------------------------------------------------


def test_continue_without_a_disk_shows_the_hint_and_no_dialog(make_window, monkeypatch):
    window = make_window(info=info_of(plain_disk()))
    window.current = window.pages.index(window.storage)
    shown = []
    monkeypatch.setattr("gentoo_installer.app.QMessageBox.warning", lambda *a, **k: shown.append(a))
    window.next_page()
    assert shown == [] and window.storage.choose_hint.isVisibleTo(window.storage)


def test_the_review_page_shows_the_fstab_preview(make_window):
    window = make_window(info=info_of(plain_disk()))
    review = window.review
    window.manifest.storage.device = "/dev/sda"
    review.set_manifest(window.manifest) if hasattr(review, "set_manifest") else None
    review.load_manifest()
    text = review.fstab_text.text()
    assert "/etc/fstab" in text or "UUID=" in text or "LABEL=" in text
    assert "noatime" in text
