"""STOR-07 (the part that needs no partition writing): suggestion on entry, more mount points with help, presets, the disk bar, F2FS rules."""

from __future__ import annotations

from gentoo_installer.manifest import Partition
from gentoo_installer.pages import manual_storage
from gentoo_installer.system_info import DiskInfo, PartitionInfo
from tests.ui.test_gui_storage_safety import ESP, GIB, info_of, laptop, select


def disk_with_free_partition():
    path = "/dev/sda"
    return DiskInfo(path, "Disk", 300 * GIB, serial="S1", pttype="gpt", partitions=[
        PartitionInfo(f"{path}1", 512 * 1024**2, "vfat", parttype=ESP, parent=path),
        PartitionInfo(f"{path}2", 60 * GIB, "ext4", label="old", parent=path),
        PartitionInfo(f"{path}3", 100 * GIB, "", parent=path),
        PartitionInfo(f"{path}4", 40 * GIB, "ext4", label="data", parent=path),
    ])


def manual_page(make_window, disk=None):
    window = make_window(info=info_of(disk or laptop()))
    page = select(window, (disk or laptop()).path)
    return window, page


def test_entering_the_manual_mode_fills_the_table_with_a_suggestion(make_window):
    window, page = manual_page(make_window)
    assert page.manual_editor.row_count() == 0
    page.mode.setCurrentIndex(page.mode.findData("manual"))
    assert [(r.device, r.mountpoint) for r in page.manual_editor.rows()] == [("/dev/nvme0n1p3", "/"), ("/dev/nvme0n1p1", "/efi")]
    page.manual_editor.remove_selected()
    page.mode.setCurrentIndex(page.mode.findData("erase"))
    page.mode.setCurrentIndex(page.mode.findData("manual"))
    assert page.manual_editor.row_count() >= 1  # rows the user left are not replaced


def test_rows_the_user_made_are_not_replaced_when_the_mode_is_entered_again(make_window):
    window, page = manual_page(make_window)
    page.mode.setCurrentIndex(page.mode.findData("manual"))
    page.manual_editor.set_rows([Partition("/dev/nvme0n1p3", "/", "ext4", format=True)])
    page.mode.setCurrentIndex(page.mode.findData("erase"))
    page.mode.setCurrentIndex(page.mode.findData("manual"))
    assert [r.format for r in page.manual_editor.rows()] == [True]


def test_the_mount_choices_include_var_opt_usr_tmp_and_each_has_a_line_of_help(make_window):
    window, page = manual_page(make_window)
    editor = page.manual_editor
    editor.add_row()
    combo = editor.table.cellWidget(0, 1)
    choices = [combo.itemText(i) for i in range(combo.count())]
    assert choices == ["/", "/boot", "/efi", "/home", "/var", "/opt", "/usr", "/tmp", "/var/tmp", "swap"]
    assert all(manual_storage.MOUNT_HELP[c] for c in choices)
    combo.setCurrentText("/home")
    editor.table.setCurrentCell(0, 1)
    assert editor.mount_help.text() == "A separate /home keeps your files safe if you reinstall the system."
    combo.setCurrentText("/usr")
    assert "needs an initramfs" in editor.mount_help.text() and "newcomers should leave it" in editor.mount_help.text()


def test_a_separate_usr_needs_dracut(make_window):
    window, page = manual_page(make_window)
    editor = page.manual_editor
    editor.set_rows([Partition("/dev/nvme0n1p3", "/", "ext4", format=False), Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False),
                     Partition("/dev/nvme0n1p4", "/usr", "ext4", format=False)])
    assert not any("/usr" in p for p in editor.problems("uefi"))
    assert any("separate /usr partition needs an initramfs" in p for p in editor.problems("uefi", initramfs="none"))


def test_an_f2fs_root_with_grub_needs_a_boot_row(make_window):
    window, page = manual_page(make_window, disk_with_free_partition())
    editor = page.manual_editor
    editor.set_rows([Partition("/dev/sda2", "/", "f2fs", format=True), Partition("/dev/sda1", "/efi", "vfat", format=False)])
    assert any("GRUB cannot reliably read an F2FS root" in p for p in editor.problems("uefi"))
    assert not any("F2FS" in p for p in editor.problems("uefi", bootloader="systemd-boot"))
    editor.set_rows([Partition("/dev/sda2", "/", "f2fs", format=True), Partition("/dev/sda1", "/efi", "vfat", format=False), Partition("/dev/sda4", "/boot", "ext4", format=True)])
    assert not any("F2FS" in p for p in editor.problems("uefi"))
    fs = editor.table.cellWidget(0, 2)
    assert "f2fs" in [fs.itemData(i) for i in range(fs.count())]


def test_the_presets_add_a_row_and_only_choose_an_empty_partition(make_window):
    window, page = manual_page(make_window, disk_with_free_partition())
    editor = page.manual_editor
    page.mode.setCurrentIndex(page.mode.findData("manual"))
    before = editor.row_count()
    index = editor.add_preset("/home")
    assert editor.row_count() == before + 1
    row = editor.rows()[index]
    assert (row.device, row.mountpoint) == ("/dev/sda3", "/home")  # the only partition without a filesystem
    editor.add_preset("/var")
    var = editor.rows()[-1]
    assert var.mountpoint == "/var" and var.device == ""  # nothing empty is left: the user chooses, and sees what is on the candidates


def test_the_preset_never_picks_a_partition_with_data(make_window):
    window, page = manual_page(make_window)
    page.mode.setCurrentIndex(page.mode.findData("manual"))
    editor = page.manual_editor
    editor.add_preset("/home")
    assert editor.rows()[-1].device == ""


def test_the_bar_shows_every_partition_and_marks_what_is_not_changed(make_window):
    window, page = manual_page(make_window, disk_with_free_partition())
    page.mode.setCurrentIndex(page.mode.findData("manual"))
    editor = page.manual_editor
    editor.set_rows([Partition("/dev/sda2", "/", "ext4", format=True), Partition("/dev/sda1", "/efi", "vfat", format=False)])
    roles = [role for role, _ in editor.bar.segments]
    assert roles == ["efi", "root", "data", "data"]
    assert "not changed" in editor.legend.text()
