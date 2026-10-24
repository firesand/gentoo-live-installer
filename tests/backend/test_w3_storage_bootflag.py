"""STOR-1 / TGT-4: legacy BIOS firmware wants a bootable flag on the disk.

On a GPT disk that is the boot flag of the protective MBR (`parted disk_set pmbr_boot on`), on an MBR disk
the active flag of the partition that holds /boot, else the root. sgdisk and `parted mkpart` leave both
off, and `grub-install --target=i386-pc` only writes boot.img and core.img. UEFI never gets either: some
UEFI firmware ignores a disk whose protective MBR is active.
"""

from __future__ import annotations

import pytest

from gentoo_installer.manifest import Partition
from gentoo_installerd.storage import StorageError

from tests.backend.test_storage_identity import erase, machine, manual, vda
from tests.backend.test_storage_support import BIOS_BOOT, ESP, FakeDisk, GiB, MiB, StatefulRunner, commands_text, make_manager

PMBR = ("parted", "--script", "/dev/vda", "disk_set", "pmbr_boot", "on")


def parted_flag_commands(runner: StatefulRunner) -> list[tuple[str, ...]]:
    return [c for c in runner.commands if c[0] == "parted" and ("disk_set" in c or "set" in c) and "esp" not in c]


def erased(tmp_path, **changes):
    runner = StatefulRunner([vda()])
    manager = make_manager(runner, tmp_path)
    manager.prepare(erase(**changes))
    return runner, manager


class TestGuidedBios:
    def test_a_gpt_disk_gets_the_protective_mbr_boot_flag(self, tmp_path):
        runner, _ = erased(tmp_path, firmware="bios", table="gpt")
        assert PMBR in runner.commands
        assert runner.disks["/dev/vda"].pmbr_boot is True
        assert parted_flag_commands(runner) == [PMBR]

    def test_the_flag_comes_after_the_last_partition_and_is_followed_by_partprobe_and_settle(self, tmp_path):
        runner, _ = erased(tmp_path, firmware="bios", table="gpt")
        text = commands_text(runner)
        flag = text.index(" ".join(PMBR))
        last_sgdisk = max(i for i, line in enumerate(text) if line.startswith("sgdisk"))
        assert last_sgdisk < flag
        assert text[flag + 1 : flag + 3] == ["partprobe /dev/vda", "udevadm settle"]
        # BIOS boot, swap, root: the partitions are wiped and formatted only once the table has settled again
        assert flag + 2 < text.index("wipefs --all /dev/vda3") < text.index("mkfs.ext4 -F /dev/vda3")

    def test_a_gpt_disk_with_luks_swap_and_home_gets_the_same_single_flag(self, tmp_path):
        runner, _ = erased(tmp_path, firmware="bios", table="gpt", luks=True, luks_passphrase="long enough passphrase", swap_mib=2048)
        assert parted_flag_commands(runner) == [PMBR]

    def test_an_mbr_disk_gets_the_active_flag_on_the_root_partition(self, tmp_path):
        runner, _ = erased(tmp_path, firmware="bios", table="dos", swap_mib=0)
        assert parted_flag_commands(runner) == [("parted", "--script", "/dev/vda", "set", "1", "boot", "on")]
        assert [p["num"] for p in runner.disks["/dev/vda"].parts if "boot" in p.get("flags", ())] == [1]

    def test_the_active_partition_follows_the_layout(self, tmp_path):
        # swap and a separate /home come before the root: root is partition 3
        runner, _ = erased(tmp_path, firmware="bios", table="dos", swap_mib=2048, separate_home=True, home_mib=4096)
        assert parted_flag_commands(runner) == [("parted", "--script", "/dev/vda", "set", "3", "boot", "on")]
        flagged = [p for p in runner.disks["/dev/vda"].parts if "boot" in p.get("flags", ())]
        assert [p["num"] for p in flagged] == [3] and flagged[0]["size"] > 40 * GiB  # the root, not swap or home

    def test_an_encrypted_mbr_disk_marks_the_unencrypted_boot_partition(self, tmp_path):
        runner, _ = erased(tmp_path, firmware="bios", table="dos", luks=True, luks_passphrase="long enough passphrase", swap_mib=0)
        # partition 1 is /boot, partition 2 the LUKS root: the firmware can only start from the readable one
        assert parted_flag_commands(runner) == [("parted", "--script", "/dev/vda", "set", "1", "boot", "on")]
        assert runner.disks["/dev/vda"].parts[0]["size"] == 1024 * MiB

    def test_the_active_flag_is_followed_by_partprobe_and_settle(self, tmp_path):
        runner, _ = erased(tmp_path, firmware="bios", table="dos")
        text = commands_text(runner)
        flag = next(i for i, line in enumerate(text) if line.endswith("boot on"))
        assert text[flag + 1 : flag + 3] == ["partprobe /dev/vda", "udevadm settle"]

    @pytest.mark.parametrize("table", ["gpt", "dos"])
    def test_uefi_never_gets_a_boot_flag(self, tmp_path, table):
        runner, _ = erased(tmp_path, firmware="uefi", table=table)
        assert parted_flag_commands(runner) == []
        assert runner.disks["/dev/vda"].pmbr_boot is False
        assert not any("disk_set" in c for c in runner.commands)

    def test_a_dry_run_preview_lists_the_flag_command(self, tmp_path):
        runner = StatefulRunner([vda()])
        make_manager(runner, tmp_path).prepare(erase(firmware="bios"), dry_run=True)
        assert PMBR in runner.commands


def bios_gpt_disk(*, esp: bool = False) -> FakeDisk:
    parts = [{"num": 1, "size": 1 * MiB, "fstype": None, "parttype": BIOS_BOOT}]
    if esp:
        parts.append({"num": 3, "size": 512 * MiB, "fstype": "vfat", "parttype": ESP})
    parts.append({"num": 2, "size": 100 * GiB, "fstype": "ext4"})
    return FakeDisk("/dev/sda", 500 * GiB, serial="SER-A", wwn="WWN-A", pttype="gpt", partitions=parts)


def bios_dos_disk(*, active: int | None = None) -> FakeDisk:
    parts = [
        {"num": 1, "size": 100 * MiB, "fstype": "ntfs", "label": "System Reserved"},
        {"num": 2, "size": 200 * GiB, "fstype": "ntfs", "label": "Windows"},
        {"num": 3, "size": 100 * GiB, "fstype": "ext4"},
    ]
    for part in parts:
        part["parttype"] = "0x83"
        if part["num"] == active:
            part["flags"] = {"boot"}
    return FakeDisk("/dev/sda", 500 * GiB, serial="SER-A", wwn="WWN-A", pttype="dos", partitions=parts)


class TestManualBios:
    def prepare(self, tmp_path, disk, *rows, **changes):
        runner = StatefulRunner([disk])
        manager = make_manager(runner, tmp_path)
        manager.prepare(manual(*rows, firmware="bios", **changes))
        return runner, manager

    def test_a_gpt_disk_without_an_efi_partition_gets_the_protective_mbr_boot_flag(self, tmp_path):
        runner, _ = self.prepare(tmp_path, bios_gpt_disk(), Partition("/dev/sda2", "/", "ext4", format=True))
        pmbr = ("parted", "--script", "/dev/sda", "disk_set", "pmbr_boot", "on")
        assert pmbr in runner.commands
        assert runner.disks["/dev/sda"].pmbr_boot is True
        assert runner.commands[runner.commands.index(pmbr) + 1] == ("udevadm", "settle")

    def test_a_gpt_disk_that_carries_an_efi_partition_is_left_alone(self, tmp_path):
        # a dual-boot disk: some EFI firmware ignores a disk whose protective MBR is active
        runner, _ = self.prepare(tmp_path, bios_gpt_disk(esp=True), Partition("/dev/sda2", "/", "ext4", format=True))
        assert not any("disk_set" in c for c in runner.commands)
        assert runner.disks["/dev/sda"].pmbr_boot is False

    def test_uefi_never_gets_the_flag(self, tmp_path):
        runner = StatefulRunner([bios_gpt_disk(esp=True)])
        make_manager(runner, tmp_path).prepare(manual(Partition("/dev/sda2", "/", "ext4", format=True), Partition("/dev/sda3", "/efi", "vfat", format=False), firmware="uefi"))
        assert not any(c[0] == "parted" for c in runner.commands)
        assert runner.disks["/dev/sda"].pmbr_boot is False

    def test_an_mbr_disk_with_no_active_partition_gets_one_on_the_boot_partition(self, tmp_path):
        rows = (Partition("/dev/sda3", "/", "ext4", format=True),)
        runner, manager = self.prepare(tmp_path, bios_dos_disk(), *rows)
        assert ("parted", "--script", "/dev/sda", "set", "3", "boot", "on") in runner.commands
        assert any("active" in note and "/dev/sda3" in note for note in manager.notes)

    def test_the_separate_boot_partition_is_the_one_marked(self, tmp_path):
        disk = bios_dos_disk()
        disk.parts[0]["size"] = 1024 * MiB  # a /boot of its own
        rows = (Partition("/dev/sda1", "/boot", "ext4", format=True), Partition("/dev/sda3", "/", "ext4", format=True))
        runner, _ = self.prepare(tmp_path, disk, *rows)
        assert [p["num"] for p in runner.disks["/dev/sda"].parts if "boot" in p.get("flags", ())] == [1]

    def test_a_windows_dual_boot_disk_keeps_the_partition_that_is_already_active(self, tmp_path):
        rows = (Partition("/dev/sda3", "/", "ext4", format=True),)
        runner, manager = self.prepare(tmp_path, bios_dos_disk(active=1), *rows)
        assert not any(c[:4] == ("parted", "--script", "/dev/sda", "set") for c in runner.commands)
        assert [p["num"] for p in runner.disks["/dev/sda"].parts if "boot" in p.get("flags", ())] == [1]
        assert not any("active" in note for note in manager.notes)

    def test_a_disk_that_parted_cannot_flag_does_not_stop_the_install(self, tmp_path):
        runner = StatefulRunner([bios_gpt_disk()], fail={"parted --script /dev/sda disk_set": (1, "Error: Fix/Ignore? unhandled")})
        manager = make_manager(runner, tmp_path)
        manager.prepare(manual(Partition("/dev/sda2", "/", "ext4", format=True), firmware="bios"))
        assert any("boot flag" in note and "/dev/sda" in note for note in manager.notes)
        assert ("mkfs.ext4", "-F", "/dev/sda2") in runner.commands

    def test_a_row_that_is_not_a_partition_of_the_disk_gets_no_flag(self, tmp_path):
        # root on a disk other than the one that carries the bootloader: nothing to mark on the MBR disk
        disks = machine()
        disks[0].pttype = "dos"
        for part in disks[0].parts:
            part["parttype"] = "0x83"
        runner = StatefulRunner(disks)
        rows = (Partition("/dev/sda1", "/data", "vfat", format=False), Partition("/dev/sdb1", "/", "ext4", format=True))
        make_manager(runner, tmp_path).prepare(manual(*rows, additional_device_ids=["SER-B"], firmware="bios"))
        assert not any(c[:4] == ("parted", "--script", "/dev/sda", "set") for c in runner.commands)

    def test_the_flag_is_not_set_when_the_install_is_refused(self, tmp_path):
        runner = StatefulRunner([bios_gpt_disk()])
        with pytest.raises(StorageError):
            make_manager(runner, tmp_path).prepare(manual(Partition("/dev/sda2", "/", "ext4", format=True), Partition("/dev/sdz9", "/data", "ext4", format=True), firmware="bios"))
        assert runner.disks["/dev/sda"].pmbr_boot is False
        assert not any(c[0] == "parted" for c in runner.commands)
