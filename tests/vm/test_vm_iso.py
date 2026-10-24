"""scripts/vm_iso.py: reading the live ISO's boot menu without xorriso, and resolving a menu entry to kernel, initramfs and arguments."""
from __future__ import annotations

from pathlib import Path

import pytest

import vm_iso
from tests.vm.iso_fixture import SECTOR, build_iso, write_iso

ROOT = Path(__file__).resolve().parents[2]
MENU = """\
set default=0
set timeout=10
menuentry 'Desktop' --class gnu-linux --class os {
\tsearch --no-floppy --set=root -l LIVE
\tlinux /boot/gentoo quiet root=live:CDLABEL=LIVE rd.live.dir=/ cdroot
\tinitrd /boot/gentoo.igz
}
menuentry 'Text console only (repair, headless)' {
\tlinux /boot/gentoo quiet root=live:CDLABEL=LIVE cdroot nox dokeymap
\tinitrd /boot/gentoo.igz
}
submenu 'More options' {
\tmenuentry 'Serial console ttyS0 115200 (text only)' {
\t\tlinux /boot/gentoo root=live:CDLABEL=LIVE cdroot nox console=tty0 console=ttyS0,115200n8
\t\tinitrd /boot/gentoo.igz
\t}
\tmenuentry 'Text console in RAM' {
\t\tlinux /boot/gentoo root=live:CDLABEL=LIVE cdroot nox rd.live.ram=1
\t\tinitrd /boot/gentoo.igz
\t}
}
if [ "x$grub_platform" = xefi ]; then
\tmenuentry 'UEFI firmware settings' {
\t\tinsmod efifwsetup
\t\tfwsetup
\t}
fi
menuentry 'Power off' {
\tinsmod halt
\thalt
}
"""


@pytest.fixture
def entries():
    return vm_iso.parse_boot_menu(MENU)


# --- the image ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("rock_ridge", [False, True])
def test_a_file_in_a_nested_directory_is_found_and_read(tmp_path, rock_ridge):
    path = write_iso(tmp_path / "live.iso", {"/boot/grub/grub.cfg": MENU.encode(), "/boot/gentoo": b"kernel", "/README": b"hi"}, rock_ridge=rock_ridge)
    image = vm_iso.IsoImage(path)
    assert image.read_text("/boot/grub/grub.cfg") == MENU
    assert image.read_text("boot/GRUB/grub.cfg") == MENU  # case does not matter: level 1 names are upper case
    assert image.find("/boot").directory and not image.find("/boot/gentoo").directory


def test_rock_ridge_names_win_over_the_mangled_iso_names(tmp_path):
    long_name = "a-very-long-file-name.initramfs.igz"
    path = write_iso(tmp_path / "live.iso", {f"/boot/{long_name}": b"x"}, rock_ridge=True)
    assert vm_iso.IsoImage(path).read_text(f"/boot/{long_name}") == "x"


def test_a_missing_file_is_reported_by_its_path(tmp_path):
    path = write_iso(tmp_path / "live.iso", {"/boot/gentoo": b"kernel"})
    with pytest.raises(vm_iso.IsoError, match="/boot/grub/grub.cfg is not in the image"):
        vm_iso.IsoImage(path).read_text("/boot/grub/grub.cfg")


def test_a_big_file_is_copied_out_whole(tmp_path):
    payload = bytes(range(256)) * (3 * SECTOR + 17)  # more than a megabyte, not a multiple of a sector
    path = write_iso(tmp_path / "live.iso", {"/boot/gentoo.igz": payload, "/boot/gentoo": b"k"})
    size = vm_iso.IsoImage(path).extract("/boot/gentoo.igz", tmp_path / "out" / "initrd")
    assert size == len(payload) and (tmp_path / "out" / "initrd").read_bytes() == payload


def test_something_that_is_not_an_iso_is_refused(tmp_path):
    other = tmp_path / "disk.img"
    other.write_bytes(b"\0" * (40 * SECTOR))
    with pytest.raises(vm_iso.IsoError, match="not an ISO 9660"):
        vm_iso.IsoImage(other)
    with pytest.raises(vm_iso.IsoError, match="cannot read"):
        vm_iso.IsoImage(tmp_path / "missing.iso")


def test_a_directory_is_not_a_text_file(tmp_path):
    path = write_iso(tmp_path / "live.iso", {"/boot/gentoo": b"k"})
    with pytest.raises(vm_iso.IsoError, match="not a small text file"):
        vm_iso.IsoImage(path).read_text("/boot")


# --- the menu ----------------------------------------------------------------------------------------------------

def test_the_entries_keep_their_kernel_initrd_and_arguments(entries):
    desktop = entries[0]
    assert (desktop.title, desktop.kernel, desktop.initrd) == ("Desktop", "/boot/gentoo", "/boot/gentoo.igz")
    assert desktop.arguments == ("quiet", "root=live:CDLABEL=LIVE", "rd.live.dir=/", "cdroot")
    serial = next(entry for entry in entries if entry.title.startswith("Serial console"))
    assert serial.submenu == ("More options",) and serial.label == "More options > Serial console ttyS0 115200 (text only)"
    assert "console=ttyS0,115200n8" in serial.arguments and "nox" in serial.arguments


def test_entries_that_do_not_boot_linux_are_listed_but_not_bootable(entries):
    by_title = {entry.title: entry for entry in entries}
    assert not by_title["Power off"].bootable and not by_title["UEFI firmware settings"].bootable
    assert by_title["Desktop"].bootable


def test_unbalanced_menus_are_reported():
    with pytest.raises(vm_iso.IsoError, match="ends inside a block"):
        vm_iso.parse_boot_menu("menuentry 'x' {\n\tlinux /k\n")
    with pytest.raises(vm_iso.IsoError, match="closing brace"):
        vm_iso.parse_boot_menu("}\n")


def test_the_real_template_of_the_iso_parses():
    """iso/cd_overlay/boot/grub/grub.cfg.in is what the ISO's menu is made from."""
    template = (ROOT / "iso" / "cd_overlay" / "boot" / "grub" / "grub.cfg.in").read_text()
    rendered = template.replace("@VOLID@", "GENTOO-LIVE").replace("@BOOTARGS@", "quiet")
    menu = [entry for entry in vm_iso.parse_boot_menu(rendered) if entry.bootable and "memtest" not in entry.kernel]
    assert len(menu) >= 3
    assert all(any(argument.startswith("root=live:CDLABEL=GENTOO-LIVE") for argument in entry.arguments) for entry in menu)
    assert any("nox" in entry.arguments for entry in menu), "the ISO has a text console entry"
    assert any("console=ttyS0,115200n8" in entry.arguments for entry in menu), "the ISO has a serial console entry"
    assert vm_iso.select_entry(menu, "default") is menu[0]


# --- choosing an entry -------------------------------------------------------------------------------------------

def test_default_is_the_first_entry_that_boots_linux(entries):
    assert vm_iso.select_entry(entries, "default").title == "Desktop"


@pytest.mark.parametrize(("query", "title"), [
    ("text", "Text console only (repair, headless)"),
    ("serial", "Serial console ttyS0 115200 (text only)"),
    ("Desktop", "Desktop"),
    ("text-ram", "Text console in RAM"),
    ("^Text console in", "Text console in RAM"),
])
def test_aliases_titles_and_expressions_name_one_entry(entries, query, title):
    assert vm_iso.select_entry(entries, query).title == title


def test_the_top_level_entry_wins_over_a_submenu_entry_of_the_same_kind(entries):
    assert vm_iso.select_entry(entries, "text console").title == "Text console only (repair, headless)"


def test_an_unknown_entry_lists_what_there_is(entries):
    with pytest.raises(vm_iso.IsoError) as raised:
        vm_iso.select_entry(entries, "rescue")
    assert "no boot menu entry matches" in str(raised.value) and "Desktop" in str(raised.value) and "Power off" not in str(raised.value)


def test_a_vague_name_is_ambiguous(entries):
    with pytest.raises(vm_iso.IsoError, match="matches several"):
        vm_iso.select_entry(entries, "t")  # the desktop and the text console are both top-level entries


def test_a_broken_expression_is_reported(entries):
    with pytest.raises(vm_iso.IsoError, match="not a menu entry name or a regular expression"):
        vm_iso.select_entry(entries, "(")


def test_a_menu_without_linux_entries_has_nothing_to_boot():
    with pytest.raises(vm_iso.IsoError, match="no entry that boots Linux"):
        vm_iso.select_entry(vm_iso.parse_boot_menu("menuentry 'Power off' {\n\thalt\n}\n"), "default")


# --- booting an entry without GRUB -------------------------------------------------------------------------------------

def test_an_entry_becomes_the_kernel_initrd_and_arguments_qemu_boots(tmp_path):
    files = {"/boot/grub/grub.cfg": MENU.encode(), "/boot/gentoo": b"KERNEL", "/boot/gentoo.igz": b"INITRD"}
    iso = write_iso(tmp_path / "live.iso", files)
    boot = vm_iso.prepare_direct_boot(iso, "serial", tmp_path / "entry")
    assert boot.kernel.read_bytes() == b"KERNEL" and boot.initrd is not None and boot.initrd.read_bytes() == b"INITRD"
    assert boot.cmdline == "root=live:CDLABEL=LIVE cdroot nox console=tty0 console=ttyS0,115200n8"
    assert boot.qemu_arguments() == ["-kernel", str(boot.kernel), "-initrd", str(boot.initrd), "-append", boot.cmdline]


def test_an_entry_that_needs_grub_variables_cannot_be_started_without_grub(tmp_path):
    menu = "menuentry 'x' {\n\tlinux /boot/gentoo root=live:CDLABEL=$volid\n}\n"
    iso = write_iso(tmp_path / "live.iso", {"/boot/grub/grub.cfg": menu.encode(), "/boot/gentoo": b"K"})
    with pytest.raises(vm_iso.IsoError, match="uses GRUB variables"):
        vm_iso.prepare_direct_boot(iso, "x", tmp_path / "entry")


def test_the_running_live_system_must_have_the_arguments_of_the_entry(entries):
    serial = vm_iso.select_entry(entries, "serial")
    assert vm_iso.cmdline_problems(serial, "BOOT_IMAGE=/boot/gentoo root=live:CDLABEL=LIVE cdroot nox console=tty0 console=ttyS0,115200n8") == []
    assert vm_iso.cmdline_problems(serial, "BOOT_IMAGE=/boot/gentoo root=live:CDLABEL=LIVE cdroot") == ["nox", "console=tty0", "console=ttyS0,115200n8"]


def test_the_image_builder_is_itself_sound():
    image = build_iso({"/a/b": b"x"})
    assert image[16 * SECTOR:16 * SECTOR + 6] == b"\x01CD001" and len(image) % SECTOR == 0
