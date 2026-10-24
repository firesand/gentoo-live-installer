"""UEFI boot entries: parsing what efibootmgr prints (a real capture of version 18 and the older spelling), matching entries to the ESP and files on it."""

from __future__ import annotations

from pathlib import Path

import pytest

from gentoo_installerd.rescue import efi

DATA = Path(__file__).parent / "data"
PARTUUID = "5f1d4b7a-6c3e-4a58-9b0d-3e7c2a91f4d6"


def test_a_real_efibootmgr_18_capture_is_read_completely() -> None:
    state = efi.parse_efibootmgr((DATA / "efibootmgr_v18_verbose.txt").read_text())
    assert state.usable and state.current == "0001" and state.timeout == "1 seconds" and state.order == ["0001", "0004", "0005", "0006"]
    assert [(e.number, e.active, e.label) for e in state.entries] == [
        ("0001", True, "Gentoo"), ("0004", True, "UEFI OS"), ("0005", True, "UEFI: PXE IPv4 Intel(R) Ethernet Controller I226-V"), ("0006", True, "UEFI: PXE IPv6 Intel(R) Ethernet Controller I226-V")]
    gentoo, fallback, pxe4, pxe6 = state.entries
    assert gentoo.loader == "\\EFI\\gentoo\\grubx64.efi" and gentoo.partuuid == PARTUUID and gentoo.partition == 1
    assert fallback.loader == "\\EFI\\BOOT\\BOOTX64.EFI" and fallback.partuuid == PARTUUID  # the optional data glued to the path (0000424f) is not part of the file name
    assert pxe4.loader == pxe6.loader == "" and pxe4.partuuid == "" and pxe4.partition == 0  # network boot entries have no file on any disk
    assert [e.number for e in efi.entries_for_esp(state, PARTUUID.upper())] == ["0001", "0004"]
    assert state.entry("0001") is gentoo and state.entry("0001".lower()) is gentoo and state.entry("0099") is None


def test_the_older_file_spelling_still_works() -> None:
    text = (f"BootCurrent: 0000\nBootOrder: 0000\nBoot0000* Gentoo\tHD(2,GPT,{PARTUUID},0x800,0x100000)/File(\\EFI\\Gentoo\\grubx64.efi)\n"
            f"Boot0001  Disabled thing\tHD(2,GPT,{PARTUUID},0x800,0x100000)/File(\\EFI\\BOOT\\BOOTX64.EFI)\n")
    state = efi.parse_efibootmgr(text)
    assert [(e.number, e.active, e.loader, e.partition) for e in state.entries] == [("0000", True, "\\EFI\\Gentoo\\grubx64.efi", 2), ("0001", False, "\\EFI\\BOOT\\BOOTX64.EFI", 2)]


@pytest.mark.parametrize("tail,expected", [
    ("\\EFI\\arch\\vmlinuz-linux.efiroot=UUID=1111 rw initrd=\\initramfs-linux.img", "\\EFI\\arch\\vmlinuz-linux.efi"),   # printable optional data (a unified kernel image or EFI stub entry)
    ("\\EFI\\Linux\\arch-linux.efi", "\\EFI\\Linux\\arch-linux.efi"),
    ("\\EFI\\Gentoo\\grubx64.EFI00004e4f", "\\EFI\\Gentoo\\grubx64.EFI"),
    ("\\EFI\\my.efi.d\\grubx64.efi0000", "\\EFI\\my.efi.d\\grubx64.efi"),                                            # a directory whose name contains .efi
    ("\\vmlinuz", "\\vmlinuz"),                                                                                    # not a .efi file: kept as printed
])
def test_the_file_of_an_entry_is_cut_before_its_optional_data(tail: str, expected: str) -> None:
    state = efi.parse_efibootmgr(f"BootOrder: 0003\nBoot0003* Linux\tHD(1,GPT,{PARTUUID},0x800,0x100000)/{tail}\n")
    assert state.entries[0].loader == expected


def test_entries_on_dos_disks_are_matched_by_signature_and_number() -> None:
    state = efi.parse_efibootmgr("BootOrder: 0002\nBoot0002* Old stick\tHD(1,MBR,0x1a2b3c4d,0x800,0x32000)/\\EFI\\BOOT\\BOOTX64.EFI\n")
    entry = state.entries[0]
    assert entry.partuuid == "1a2b3c4d-01" and entry.partition == 1 and entry.loader == "\\EFI\\BOOT\\BOOTX64.EFI"  # the PARTUUID lsblk prints for DOS partitions
    assert efi.entries_for_esp(state, "1A2B3C4D-01") == [entry]


def test_firmware_without_variables_is_reported_not_parsed() -> None:
    for text, code in (("EFI variables are not supported on this system.\n", 2), ("efibootmgr: option requires an argument\n", 1), ("", 1)):
        state = efi.parse_efibootmgr(text, code)
        assert state.usable is False and state.entries == [] and state.error
    assert efi.parse_efibootmgr("EFI variables are not supported on this system.\n", 0).usable is False  # some versions exit 0


def test_the_esp_is_searched_ignoring_case_like_the_firmware_does() -> None:
    tree = {"/boot/efi": ["EFI", "loader"], "/boot/efi/EFI": ["BOOT", "gentoo"], "/boot/efi/EFI/gentoo": ["grubx64.efi"], "/boot/efi/EFI/BOOT": ["BOOTX64.EFI"]}
    listdir = lambda path: tree.get(path, [])  # noqa: E731
    assert efi.esp_file_exists("\\EFI\\gentoo\\grubx64.efi", listdir, "/boot/efi")
    assert efi.esp_file_exists("\\efi\\GENTOO\\GRUBX64.EFI", listdir, "/boot/efi/")
    assert efi.esp_file_exists("/EFI/boot/bootx64.efi", listdir, "/boot/efi")
    assert not efi.esp_file_exists("\\EFI\\gentoo\\gone.efi", listdir, "/boot/efi") and not efi.esp_file_exists("", listdir, "/boot/efi")


def test_labels_and_loaders_are_normalized_before_they_reach_efibootmgr() -> None:
    assert efi.sanitize_label("Gentoo; rm -rf /") == "Gentoo rm -rf"
    assert efi.sanitize_label("äöü!!!") == "Linux" and efi.sanitize_label("", "Fallback") == "Fallback"
    assert len(efi.sanitize_label("x" * 200)) == 64
    assert efi.loader_for_entry("/EFI/Gentoo/grubx64.efi") == efi.loader_for_entry("\\EFI\\Gentoo\\grubx64.efi") == "\\EFI\\Gentoo\\grubx64.efi"
    assert efi.loader_for_entry("//EFI///x/") == "\\EFI\\x"
