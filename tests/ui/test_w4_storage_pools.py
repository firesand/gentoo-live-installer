"""A disk that holds one half of a Btrfs or ZFS pool is not free to erase: the window asks for the consent first (STOR-4, GUI half)."""

from __future__ import annotations

from gentoo_installer.manifest import Partition
from gentoo_installer.pages.storage import holder_reasons
from gentoo_installer.system_info import DiskInfo, PartitionInfo
from tests.ui.conftest import sample_info

GIB = 1024**3
POOL = "6b1d4c52-2f0a-4c55-9b1e-0a3c1f2e9d77"
OTHER = "0e7a2f90-91d5-4a57-8d55-5b1c6c9a41e3"


def pool_disk(path: str, serial: str, *, fstype: str = "btrfs", uuid: str = POOL, extra=()) -> DiskInfo:
    """A disk with one partition; lsblk shows the filesystem's own UUID on every member of a multi-device filesystem."""
    return DiskInfo(
        path, "Data disk", 500 * GIB, serial=serial, pttype="gpt", transport="sata",
        partitions=[PartitionInfo(f"{path}1", 400 * GIB, fstype, uuid=uuid, parent=path), *extra],
    )


def window_with(make_window, *disks):
    window = make_window(info=sample_info(disks=list(disks)))
    window.storage.disk_group.set_value(disks[0].path)
    return window, window.storage


def test_half_of_a_btrfs_pool_asks_for_the_consent(make_window):
    window, page = window_with(make_window, pool_disk("/dev/sdb", "B1"), pool_disk("/dev/sdc", "C1"))
    assert page.holders_card.isVisibleTo(page)
    assert "/dev/sdb1 is part of a Btrfs filesystem that also uses /dev/sdc1" in page.holders_text.text()
    valid, message = page.validate_page()
    assert not valid and "still in use" in message
    assert window.manifest.storage.release_holders is False
    page.release_holders.setChecked(True)
    assert page.validate_page() == (True, "") and window.manifest.storage.release_holders is True


def test_a_zfs_pool_is_named_the_same_way(make_window):
    _, page = window_with(make_window, pool_disk("/dev/sdb", "B1", fstype="zfs_member"), pool_disk("/dev/sdc", "C1", fstype="zfs_member"))
    assert page.holders_card.isVisibleTo(page) and "/dev/sdb1 is part of a ZFS pool that also uses /dev/sdc1" in page.holders_text.text()


def test_a_filesystem_on_one_device_asks_nothing(make_window):
    _, page = window_with(make_window, pool_disk("/dev/sdb", "B1"))
    assert not page.holders_card.isVisibleTo(page)
    # two Btrfs filesystems that merely sit on neighbouring disks are two filesystems
    _, page = window_with(make_window, pool_disk("/dev/sdb", "B1"), pool_disk("/dev/sdc", "C1", uuid=OTHER))
    assert not page.holders_card.isVisibleTo(page) and page.validate_page() == (True, "")


def test_both_halves_on_the_selected_disk_are_not_a_pool_that_reaches_another_disk(make_window):
    disk = pool_disk("/dev/sdb", "B1", extra=[PartitionInfo("/dev/sdb2", 90 * GIB, "btrfs", uuid=POOL, parent="/dev/sdb")])
    _, page = window_with(make_window, disk, pool_disk("/dev/sdc", "C1", uuid=OTHER))
    assert not page.holders_card.isVisibleTo(page)


def test_the_pool_comes_after_the_volume_group_in_the_list(make_window):
    disk = pool_disk("/dev/sdb", "B1", extra=[PartitionInfo("/dev/sdb2", 90 * GIB, "LVM2_member", parent="/dev/sdb")])
    info = sample_info(disks=[disk, pool_disk("/dev/sdc", "C1")])
    assert holder_reasons(info, ["/dev/sdb"]) == ["/dev/sdb2 is part of an LVM volume group", "/dev/sdb1 is part of a Btrfs filesystem that also uses /dev/sdc1"]


def test_the_tick_box_names_the_pools(make_window):
    _, page = window_with(make_window, pool_disk("/dev/sdb", "B1"), pool_disk("/dev/sdc", "C1"))
    tip = page.release_holders.toolTip()
    assert "Btrfs" in tip and "ZFS" in tip and "volume group" in tip and "RAID array" in tip


def test_in_manual_mode_only_a_formatted_member_counts(make_window):
    window, page = window_with(make_window, pool_disk("/dev/sdb", "B1"), pool_disk("/dev/sdc", "C1"))
    page.mode.setCurrentIndex(1)
    page.manual_editor.set_rows([Partition("/dev/sdb1", "/", "ext4", format=True)])
    assert page.holders_card.isVisibleTo(page) and "/dev/sdb1 is part of a Btrfs filesystem" in page.holders_text.text()
    page.manual_editor.set_rows([Partition("/dev/sdb1", "/", "btrfs", format=False)])
    assert not page.holders_card.isVisibleTo(page)
