"""STOR-03: the table the Storage page shows lists exactly the partitions _erase_layout creates."""

from __future__ import annotations

import re

import pytest

from gentoo_installer import layout
from gentoo_installerd.storage import StorageManager  # noqa: F401

from tests.backend.test_storage_identity import erase, vda
from tests.backend.test_storage_support import StatefulRunner, make_manager

NAMES = {"EFI": "efi", "BIOS": "bios_boot", "boot": "boot", "swap": "swap", "home": "home", "root": "root"}

CASES = {
    "uefi-gpt": dict(),
    "bios-gpt": dict(firmware="bios"),
    "bios-dos": dict(firmware="bios", table="dos"),
    "luks-lvm": dict(luks=True, luks_passphrase="correct horse battery", lvm=True),
    "home-and-boot": dict(separate_home=True, home_mib=20480, separate_boot=True),
    "swap-file": dict(swap_kind="file", swap_mib=2048),
    "no-swap": dict(swap_choice="none"),
}


def created(runner: StatefulRunner) -> list[tuple[str, int]]:
    """(role, MiB; 0 = the rest) of every partition the runner was told to create, in order."""
    found: list[tuple[str, int]] = []
    sizes: dict[str, int] = {}
    parted_start = 1
    for argv in runner.commands:
        if argv[0] == "sgdisk":
            new = next((a for a in argv if a.startswith("--new=")), "")
            if new:
                number, _, size = new.removeprefix("--new=").split(":")
                sizes[number] = 0 if size == "0" else int(size.removeprefix("+").removesuffix("MiB"))
            name = next((a for a in argv if a.startswith("--change-name=")), "")
            if name:
                number, label = name.removeprefix("--change-name=").split(":")
                found.append((NAMES[label], sizes[number]))
        elif argv[:2] == ("parted", "--script") and "mkpart" in argv:
            start, end = argv[-2], argv[-1]
            begin = int(start.removesuffix("MiB"))
            size = 0 if end == "100%" else int(end.removesuffix("MiB")) - begin
            parted_start += size
            found.append(("?", size))
    return found


@pytest.mark.parametrize("name", CASES)
def test_the_table_equals_what_is_created(tmp_path, name):
    runner = StatefulRunner([vda()])
    manager = make_manager(runner, tmp_path)
    m = erase(**CASES[name])
    plan = manager.describe(m)
    rows = [row for row in layout.plan_table(plan.disks[0], lvm=m.storage.lvm) if row.role != "swapfile"]
    manager.prepare(m)
    made = created(runner)
    if m.storage.table == "dos":
        assert [size for _, size in made] == [row.size_mib for row in rows]
        # the real sizes also appear: the DOS table has no BIOS boot partition
        assert all(row.role != "bios_boot" for row in rows)
    else:
        assert made == [(row.role, row.size_mib) for row in rows]


GOLDEN = {
    "uefi-gpt": [("EFI system partition", "/efi", "FAT32", 1024), ("Swap", "-", "swap", 4096), ("System", "/", "ext4", 0)],
    "bios-gpt": [("BIOS boot partition", "-", "no filesystem", 3), ("Swap", "-", "swap", 4096), ("System", "/", "ext4", 0)],
    "bios-dos": [("Swap", "-", "swap", 4096), ("System", "/", "ext4", 0)],
    "luks-lvm": [("EFI system partition", "/efi", "FAT32", 1024), ("Boot", "/boot", "ext4", 1024), ("System", "/", "ext4", 0), ("Swap file", "-", "swap", 4096)],
}


@pytest.mark.parametrize("name", GOLDEN)
def test_golden_rows(tmp_path, name):
    plan = make_manager(StatefulRunner([vda()]), tmp_path).describe(erase(**CASES[name]))
    rows = layout.plan_table(plan.disks[0], lvm="lvm" in name)
    assert [(r.partition, r.mountpoint, r.filesystem, r.size_mib) for r in rows] == GOLDEN[name]


def test_purposes_are_plain_words():
    plan = layout.plan_storage(erase())
    rows = {row.role: row for row in layout.plan_table(plan.disks[0])}
    assert rows["efi"].purpose.startswith("EFI system partition: the small area your computer reads first; Gentoo puts its boot files there")
    bios = layout.plan_storage(erase(firmware="bios"))
    assert layout.plan_table(bios.disks[0])[0].purpose == "BIOS boot partition: lets GRUB start from a GPT disk on an old BIOS computer"
    assert rows["root"].size_text == "The rest of the disk" and rows["efi"].size_text == "1.0 GiB"
    encrypted = layout.plan_table(layout.plan_storage(erase(luks=True, lvm=True)).disks[0], lvm=True)
    assert "encrypted with LUKS2, with LVM inside" in [r for r in encrypted if r.role == "root"][0].purpose
