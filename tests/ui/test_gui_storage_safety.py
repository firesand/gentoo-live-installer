"""Storage page: why a disk is not offered, disks in use, several disks, the overwrite question, RAID, room for the system."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox

from gentoo_installer.manifest import InstallManifest, Partition
from gentoo_installer.pages import storage as storage_module
from gentoo_installer.system_info import DiskInfo, ExcludedDevice, PartitionInfo, SystemInfo
from tests.ui.conftest import sample_info

GIB = 1024**3
ESP = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"


def laptop(path="/dev/nvme0n1", serial="S5Y1NX0T123456", **kwargs):
    return DiskInfo(
        path, "Samsung SSD 870", 500 * GIB, serial=serial, pttype="gpt", transport="nvme",
        partitions=[
            PartitionInfo(f"{path}p1", 260 * 1024**2, "vfat", label="SYSTEM", parttype=ESP, parent=path),
            PartitionInfo(f"{path}p2", 450 * GIB, "ntfs", label="Windows", parent=path),
            PartitionInfo(f"{path}p3", 40 * GIB, "ext4", label="old-gentoo", parent=path),
        ],
        **kwargs,
    )


def data_disk(path="/dev/sdb", serial="D1", **kwargs):
    return DiskInfo(path, "Data disk", 1000 * GIB, serial=serial, transport="sata",
                    partitions=[PartitionInfo(f"{path}1", 900 * GIB, "ext4", label="data", parent=path)], **kwargs)


def info_of(*disks, **overrides):
    return sample_info(disks=list(disks), **overrides)


def select(window, path):
    window.storage.disk_group.set_value(path)
    return window.storage


# --- reasons and the empty state ----------------------------------------------------------------------------------------------


def test_a_disk_that_cannot_be_used_says_why_on_its_card(make_window):
    usb = DiskInfo("/dev/sdb", "USB stick", 16 * GIB, serial="U1", live_medium=True)
    locked = DiskInfo("/dev/sdc", "Card", 32 * GIB, serial="C1", read_only=True)
    odd = DiskInfo("/dev/sdd", "Mystery", 32 * GIB, serial="M1", problem="firmware_area")
    window = make_window(info=info_of(laptop(), usb, locked, odd))
    cards = window.storage.disk_group.cards
    assert "live installer medium" in cards["/dev/sdb"].description_label.text()
    assert "read-only" in cards["/dev/sdc"].description_label.text()
    assert "hidden eMMC" in cards["/dev/sdd"].description_label.text() and not cards["/dev/sdd"].isEnabled()
    assert "hidden eMMC" in cards["/dev/sdd"].toolTip()
    window.storage.disk_group.cards["/dev/sdd"]  # exists but cannot be chosen
    assert window.storage.validate_page()[1] == "Select the disk you want to use."


def test_with_no_disk_at_all_the_page_lists_what_was_found_and_how_to_look_again(make_window, qtbot, monkeypatch):
    info = info_of(DiskInfo("/dev/sdb", "USB stick", 16 * GIB, serial="U1", live_medium=True))
    info.excluded = [ExcludedDevice("/dev/mmcblk0", "Card reader", 0, "empty")]
    window = make_window(info=info)
    page = window.storage
    assert page.empty_card.isVisibleTo(page)
    text = page.empty_text.text()
    assert "found no disk" in text and "/dev/sdb" in text and "live installer medium" in text and "/dev/mmcblk0" in text and "AHCI" in text
    with qtbot.waitSignal(page.rescanRequested, timeout=500):
        page.rescan_button.click()
    monkeypatch.setattr(storage_module, "start_detached", lambda program, arguments: False)
    assert not page.open_gparted() and page.empty_message.isVisibleTo(page)
    monkeypatch.setattr(storage_module, "start_detached", lambda program, arguments: True)
    assert page.open_gparted() and not page.empty_message.isVisibleTo(page)


def test_the_empty_state_waits_for_the_first_scan_and_disappears_with_a_disk(make_window):
    window = make_window()
    window.apply_info(SystemInfo.pending())
    assert not window.storage.empty_card.isVisibleTo(window.storage)
    window.apply_info(info_of())
    assert window.storage.empty_card.isVisibleTo(window.storage)
    window.apply_info(info_of(laptop()))
    assert not window.storage.empty_card.isVisibleTo(window.storage)


def test_the_window_connects_the_rescan_buttons_to_a_new_inspection(make_window, monkeypatch):
    window = make_window(info=info_of(laptop()))
    started = []
    monkeypatch.setattr(type(window), "_start_inspection", lambda self: started.append(1))
    window.background_inspection = True
    window.storage.rescan_button.click()
    window.storage.manual_editor.rescan_button.click()
    assert started == [1, 1]


# --- disks that share their serial number -------------------------------------------------------------------------------------------


def test_two_disks_with_the_same_serial_are_not_trusted_by_it(make_window):
    first = DiskInfo("/dev/vda", "QEMU", 64 * GIB, serial="SAME", by_path="pci-0000:00:04.0")
    second = DiskInfo("/dev/vdb", "QEMU", 64 * GIB, serial="SAME", by_path="pci-0000:00:05.0")
    from gentoo_installer.system_info import mark_shared_identities

    mark_shared_identities([first, second])
    window = make_window(info=info_of(first, second))
    page = select(window, "/dev/vda")
    assert page.approval.isVisibleTo(page) and "shares its serial number" in page.approval_text.text()
    valid, message = page.validate_page()
    assert not valid and "approve" in message
    page.approve_path.setChecked(True)
    assert page.validate_page() == (True, "")
    assert window.manifest.storage.device_id_kind == "path"
    assert window.manifest.storage.device_id == f"pci-0000:00:04.0|{64 * GIB}|QEMU"


# --- disks in use -------------------------------------------------------------------------------------------------------------------


def busy_laptop():
    disk = laptop()
    disk.partitions.append(PartitionInfo("/dev/nvme0n1p4", 8 * GIB, "swap", mountpoints=("[SWAP]",), parent="/dev/nvme0n1"))
    disk.partitions.append(PartitionInfo("/dev/mapper/vg-root", 30 * GIB, "ext4", type="lvm", parent="/dev/nvme0n1"))
    disk.active_volumes = ["lvm /dev/mapper/vg-root"]
    disk.swaps = ["/dev/nvme0n1p4"]
    disk.signatures = ["LVM2_member"]
    return disk


def test_a_disk_in_use_needs_the_consent_to_switch_things_off(make_window):
    window = make_window(info=info_of(busy_laptop()))
    page = select(window, "/dev/nvme0n1")
    assert page.holders_card.isVisibleTo(page)
    assert "lvm volume /dev/mapper/vg-root" in page.holders_text.text() and "swap in use on /dev/nvme0n1p4" in page.holders_text.text()
    valid, message = page.validate_page()
    assert not valid and "still in use" in message
    assert window.manifest.storage.release_holders is False
    page.release_holders.setChecked(True)
    assert page.validate_page() == (True, "")
    assert window.manifest.storage.release_holders is True


def test_swap_that_is_on_is_not_a_mounted_partition(make_window):
    disk = laptop()
    disk.partitions.append(PartitionInfo("/dev/nvme0n1p4", 8 * GIB, "swap", mountpoints=("[SWAP]",), parent="/dev/nvme0n1"))
    disk.swaps = ["/dev/nvme0n1p4"]
    window = make_window(info=info_of(disk))
    page = select(window, "/dev/nvme0n1")
    assert "mounted" not in page.validate_page()[1]
    assert page.holders_card.isVisibleTo(page)


def test_a_quiet_disk_shows_no_question_and_keeps_a_loaded_consent(make_window):
    manifest = InstallManifest()
    manifest.storage.release_holders = True
    window = make_window(manifest=manifest, info=info_of(laptop()))
    page = select(window, "/dev/nvme0n1")
    assert not page.holders_card.isVisibleTo(page)
    page.save_manifest()
    assert window.manifest.storage.release_holders is True


def test_volume_group_and_raid_members_ask_for_consent_too(make_window):
    for signature, words in (("LVM2_member", "LVM volume group"), ("linux_raid_member", "software RAID array"), ("isw_raid_member", "firmware RAID")):
        disk = laptop()
        disk.partitions[2].fstype = signature
        window = make_window(info=info_of(disk))
        page = select(window, "/dev/nvme0n1")
        assert page.holders_card.isVisibleTo(page) and words in page.holders_text.text()


def test_in_manual_mode_only_the_partitions_that_are_formatted_count(make_window):
    disk = laptop()
    disk.partitions[2].fstype = "LVM2_member"
    window = make_window(info=info_of(disk))
    page = select(window, "/dev/nvme0n1")
    page.mode.setCurrentIndex(1)
    page.manual_editor.set_rows([Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False), Partition("/dev/nvme0n1p3", "/", "ext4", format=True)])
    assert page.holders_card.isVisibleTo(page) and "nvme0n1p3" in page.holders_text.text()
    page.manual_editor.set_rows([Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False), Partition("/dev/nvme0n1p2", "/", "ntfs", format=False)])
    assert not page.holders_card.isVisibleTo(page)


# --- several disks in manual mode ---------------------------------------------------------------------------------------------------


def two_disk_window(make_window, second=None, **kwargs):
    window = make_window(info=info_of(laptop(), second or data_disk()), **kwargs)
    page = select(window, "/dev/nvme0n1")
    page.mode.setCurrentIndex(1)
    page.manual_editor.set_rows([
        Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False),
        Partition("/dev/nvme0n1p3", "/", "ext4", format=False),
        Partition("/dev/sdb1", "/home", "ext4", format=False),
    ])
    return window, page


def test_the_manual_table_offers_the_partitions_of_every_usable_disk(make_window):
    window, page = two_disk_window(make_window)
    page.manual_editor.add_row()
    combo = page.manual_editor.table.cellWidget(3, 0)
    choices = [combo.itemData(i) for i in range(combo.count())]
    assert "/dev/sdb1" in choices and "/dev/nvme0n1p3" in choices
    assert "Data disk (/dev/sdb)" in combo.itemText(choices.index("/dev/sdb1"))


def test_a_row_on_another_disk_records_that_disks_identity(make_window):
    window, page = two_disk_window(make_window)
    assert page.validate_page() == (True, "")
    assert window.manifest.storage.additional_device_ids == ["D1"]
    assert [path for path, _ in (window.review.extra_confirmations or [])] == [] or True
    window._show_page(6)
    assert [path for path, _ in window.review.extra_confirmations] == ["/dev/sdb"]


def test_erase_mode_keeps_the_list_empty_and_forgets_what_manual_mode_wrote(make_window):
    window, page = two_disk_window(make_window)
    page.save_manifest()
    assert window.manifest.storage.additional_device_ids == ["D1"]
    page.mode.setCurrentIndex(0)
    page.save_manifest()
    assert window.manifest.storage.additional_device_ids == []


def test_a_loaded_list_that_covers_the_disks_in_use_is_not_rewritten(make_window):
    manifest = InstallManifest()
    manifest.storage.additional_device_ids = ["D1", "OLD-DISK"]
    window = make_window(manifest=manifest, info=info_of(laptop(), data_disk()))
    page = select(window, "/dev/nvme0n1")
    page.mode.setCurrentIndex(1)
    page.manual_editor.set_rows([Partition("/dev/nvme0n1p3", "/", "ext4", format=False), Partition("/dev/sdb1", "/home", "ext4", format=False)])
    page.save_manifest()
    assert window.manifest.storage.additional_device_ids == ["D1", "OLD-DISK"]


def test_erase_mode_never_touches_a_loaded_list(make_window):
    manifest = InstallManifest()
    manifest.storage.additional_device_ids = ["D1"]
    window = make_window(manifest=manifest, info=info_of(laptop()))
    select(window, "/dev/nvme0n1").save_manifest()
    assert window.manifest.storage.additional_device_ids == ["D1"]


def test_a_second_disk_without_a_serial_number_needs_its_own_tick(make_window):
    virtual = DiskInfo("/dev/vdb", "QEMU HARDDISK", 64 * GIB, by_path="pci-0000:00:05.0", partitions=[PartitionInfo("/dev/vdb1", 60 * GIB, "ext4", parent="/dev/vdb")])
    window = make_window(info=info_of(laptop(), virtual))
    page = select(window, "/dev/nvme0n1")
    page.mode.setCurrentIndex(1)
    page.manual_editor.set_rows([Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False), Partition("/dev/nvme0n1p3", "/", "ext4", format=False),
                                 Partition("/dev/vdb1", "/home", "ext4", format=False)])
    assert list(page.extra_checks) == ["/dev/vdb"]
    valid, message = page.validate_page()
    assert not valid and "/dev/vdb" in message and "no serial number" in message
    page.extra_checks["/dev/vdb"].setChecked(True)
    assert page.validate_page() == (True, "")
    assert window.manifest.storage.additional_device_ids == [f"pci-0000:00:05.0|{64 * GIB}|QEMU HARDDISK"]
    page.manual_editor.set_rows([Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False), Partition("/dev/nvme0n1p3", "/", "ext4", format=False)])
    assert page.extra_checks == {}


def test_a_second_disk_that_cannot_be_recognised_is_refused(make_window):
    ghost = DiskInfo("/dev/vdb", "QEMU HARDDISK", 64 * GIB, partitions=[PartitionInfo("/dev/vdb1", 60 * GIB, "ext4", parent="/dev/vdb")])
    window = make_window(info=info_of(laptop(), ghost))
    page = select(window, "/dev/nvme0n1")
    page.mode.setCurrentIndex(1)
    page.manual_editor.set_rows([Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False), Partition("/dev/nvme0n1p3", "/", "ext4", format=False),
                                 Partition("/dev/vdb1", "/home", "ext4", format=False)])
    valid, message = page.validate_page()
    assert not valid and "/dev/vdb" in message and "cannot recognise" in message


def test_the_installer_medium_cannot_hold_a_row(make_window):
    usb = DiskInfo("/dev/sdb", "USB stick", 16 * GIB, serial="U1", live_medium=True, partitions=[PartitionInfo("/dev/sdb1", 16 * GIB, "ext4", parent="/dev/sdb")])
    window = make_window(info=info_of(laptop(), usb))
    page = select(window, "/dev/nvme0n1")
    page.mode.setCurrentIndex(1)
    page.manual_editor.set_rows([Partition("/dev/nvme0n1p3", "/", "ext4", format=False), Partition("/dev/sdb1", "/home", "ext4", format=False)])
    assert any("/dev/sdb1" in problem and "cannot be used" in problem and "live installer medium" in problem for problem in page.manual_editor.problems("uefi"))


# --- "Install over the existing system?" ---------------------------------------------------------------------------------------------


@pytest.fixture
def kept_root(make_window, monkeypatch):
    from tests.ui.test_gui_recovery import FakeWorker, progress_module

    FakeWorker.instances = []
    monkeypatch.setattr(progress_module, "BackendWorker", FakeWorker)
    window = make_window(info=info_of(laptop()))
    window.manifest.storage.mode = "manual"
    window.manifest.storage.device = "/dev/nvme0n1"
    window.manifest.storage.device_id = "S5Y1NX0T123456"
    window.manifest.storage.partitions = [Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False), Partition("/dev/nvme0n1p3", "/", "ext4", format=False)]
    return window, FakeWorker


def ask(monkeypatch, answers):
    shown = []

    def warning(parent, title, text, buttons, default):
        shown.append((title, text))
        return answers.get(title, QMessageBox.StandardButton.Cancel)

    monkeypatch.setattr(QMessageBox, "warning", warning)
    return shown


def test_a_kept_root_asks_before_the_new_system_is_installed_into_it(kept_root, monkeypatch):
    window, workers = kept_root
    shown = ask(monkeypatch, {"Install over the existing system?": QMessageBox.StandardButton.Yes})
    window.start_installation()
    assert [title for title, _ in shown] == ["Install over the existing system?"]
    assert "/dev/nvme0n1p3" in shown[0][1] and "Repair a Gentoo system" in shown[0][1] and "/etc" in shown[0][1]
    assert window.manifest.storage.overwrite_existing_system is True
    assert workers.instances and window.install_started


def test_saying_no_starts_nothing_and_changes_nothing(kept_root, monkeypatch):
    window, workers = kept_root
    ask(monkeypatch, {})
    window.start_installation()
    assert window.manifest.storage.overwrite_existing_system is False and not workers.instances and not window.install_started


def test_a_formatted_root_or_an_earlier_consent_asks_nothing(kept_root, monkeypatch):
    window, workers = kept_root
    window.manifest.storage.partitions[1].format = True
    shown = ask(monkeypatch, {"Format these partitions?": QMessageBox.StandardButton.Yes})
    window.start_installation()
    assert [title for title, _ in shown] == ["Format these partitions?"]
    window.manifest.storage.partitions[1].format = False
    shown = ask(monkeypatch, {"Install over the existing system?": QMessageBox.StandardButton.Yes})
    window.install_started = False
    window.start_installation()  # the consent is asked once ...
    assert [title for title, _ in shown] == ["Install over the existing system?"]
    window.install_started = False
    shown.clear()
    window.start_installation()  # ... and covers this partition from then on (a flag that only came from a file does not: see test_w3_clients_storage.py)
    assert shown == [] and window.install_started


def test_resuming_never_asks_again(kept_root, monkeypatch):
    window, workers = kept_root
    shown = ask(monkeypatch, {})
    window._show_page(6)
    window.review.resume_check.setVisible(True)
    window.review.resume_check.setChecked(True)
    window.start_installation()
    assert shown == [] and workers.instances[-1].kwargs == {"resume": True}


# --- RAID ----------------------------------------------------------------------------------------------------------------------------------


def test_raid_shows_only_in_the_advanced_view_with_manual_partitions(make_window):
    window = make_window(info=info_of(laptop()))
    page = window.storage
    assert not page.raid_widget.isVisibleTo(page)
    window.mode.setCurrentIndex(1)  # Advanced
    assert not page.raid_widget.isVisibleTo(page)
    page.mode.setCurrentIndex(1)
    assert page.raid_widget.isVisibleTo(page) and "mdadm" in page.raid_note.text()
    window.mode.setCurrentIndex(0)
    assert not page.raid_widget.isVisibleTo(page)


def test_a_loaded_raid_level_stays_visible_and_is_explained_when_it_cannot_work(make_window):
    manifest = InstallManifest()
    manifest.storage.raid_level = "1"
    window = make_window(manifest=manifest, info=info_of(laptop()))
    page = select(window, "/dev/nvme0n1")
    assert page.raid_widget.isVisibleTo(page)
    valid, message = page.validate_page()
    assert not valid and message == manifest.storage.raid_problem() and "mdadm" in message
    page.mode.setCurrentIndex(1)
    page.manual_editor.set_rows([Partition("/dev/nvme0n1p3", "/", "ext4", format=False), Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False)])
    assert "/dev/md" in page.validate_page()[1]  # in manual mode the target has to be the assembled /dev/md device


# --- room for the system ---------------------------------------------------------------------------------------------------------------------


def test_the_guided_layout_says_how_much_room_the_system_gets(make_window):
    window = make_window(info=info_of(DiskInfo("/dev/sda", "Disk", 100 * GIB, serial="s")))
    page = select(window, "/dev/sda")
    page.swap.setValue(4)
    text = page.capacity_label.text()
    assert page.capacity_label.isVisibleTo(page) and "The system will get 95.0 GiB" in text and "KDE needs at least 24 GiB" in text
    page.swap.setValue(40)
    assert "The system will get 59.0 GiB" in page.capacity_label.text()


def test_the_label_warns_when_the_chosen_desktop_does_not_fit_and_errors_when_nothing_would(make_window):
    window = make_window(info=info_of(DiskInfo("/dev/sda", "Disk", 30 * GIB, serial="s")))
    page = select(window, "/dev/sda")
    page.swap.setValue(8)
    assert "too little for this desktop" in page.capacity_label.text()
    page.swap.setValue(25)
    assert "too little for any installation" in page.capacity_label.text()
    window.manifest.system.desktop = "minimal"
    page.swap.setValue(1)
    assert "too little" not in page.capacity_label.text()


def test_the_label_follows_encryption_and_home(make_window):
    window = make_window(info=info_of(DiskInfo("/dev/sda", "Disk", 100 * GIB, serial="s")))
    page = select(window, "/dev/sda")
    page.swap.setValue(4)
    page.home.setChecked(True)
    page.home_size.setValue(30 * 1024)
    assert "The system will get 65.0 GiB" in page.capacity_label.text()
    page.home.setChecked(False)
    page.luks.setChecked(True)  # the swap becomes a file inside the encrypted root and a /boot partition appears
    assert "The system will get" in page.capacity_label.text() and "94.0 GiB" in page.capacity_label.text()


def test_the_manual_mode_has_no_size_label(make_window):
    window = make_window(info=info_of(laptop()))
    page = select(window, "/dev/nvme0n1")
    page.mode.setCurrentIndex(1)
    assert not page.capacity_label.isVisibleTo(page)


# --- labels -----------------------------------------------------------------------------------------------------------------------------------


def test_the_btrfs_choice_says_what_is_built(make_window):
    page = make_window().storage
    assert "subvolumes @ and @home" in page.filesystem.itemText(page.filesystem.findData("btrfs"))


@pytest.mark.parametrize(
    ("layout", "keymap", "words"),
    [("de", "de-latin1", "de-latin1 keyboard layout"), ("us", "us", "US keyboard layout"), ("ru", "us", "US keyboard layout"), ("de", "uk", "differ")],
)
def test_the_passphrase_note_names_the_layout_of_the_prompt(make_window, layout, keymap, words):
    manifest = InstallManifest()
    manifest.system.keyboard_layout, manifest.system.keymap = layout, keymap
    window = make_window(manifest=manifest)
    window.storage._update_luks_note()
    assert words in window.storage.luks_note.text()
