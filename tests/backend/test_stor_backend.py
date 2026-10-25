"""STOR backend: partition type codes, /tmp permissions, swap kinds and sizes, /boot, filesystems and mkfs details, fstab names."""

from __future__ import annotations

import os
import stat

import pytest

from gentoo_installer import layout
from gentoo_installer.manifest import InstallManifest, Partition
from gentoo_installerd.configure import render_fstab
from gentoo_installerd.configure_fstab import FstabMixin
from gentoo_installerd.storage import LayoutEntry, StorageError

from tests.backend.test_storage_identity import ESP_ROW, ROOT_ON_B, erase, machine, manual, vda
from tests.backend.test_storage_support import GiB, StatefulRunner, make_manager

KIB_PER_GIB = 1024 * 1024


def typecodes(runner: StatefulRunner) -> dict[str, str]:
    """name -> GPT type code of every partition sgdisk created."""
    found: dict[str, str] = {}
    pending: dict[str, str] = {}
    for argv in runner.commands:
        if argv[0] != "sgdisk":
            continue
        for item in argv:
            if item.startswith("--typecode="):
                number, code = item.removeprefix("--typecode=").split(":")
                pending[number] = code
            if item.startswith("--change-name="):
                number, name = item.removeprefix("--change-name=").split(":")
                found[name] = pending.get(number, "")
    return found


def with_memory(tmp_path, gib: float) -> None:
    (tmp_path / "proc").mkdir(exist_ok=True)
    (tmp_path / "proc" / "meminfo").write_text(f"MemTotal:       {int(gib * KIB_PER_GIB)} kB\nMemFree: 1 kB\n")


# --- STOR-09 -------------------------------------------------------------------------------------


class TestTypeCodes:
    @pytest.mark.parametrize(("bootloader", "uki", "root", "home"), [
        ("grub", False, "8300", "8300"),
        ("systemd-boot", False, "8304", "8302"),
        ("efi-stub", False, "8304", "8302"),
        ("grub", True, "8304", "8302"),
    ])
    def test_root_and_home_codes_follow_the_bootloader(self, tmp_path, bootloader, uki, root, home):
        runner = StatefulRunner([vda()])
        m = erase(separate_home=True, home_mib=20480)
        m.system.bootloader, m.system.uki = bootloader, uki
        make_manager(runner, tmp_path).prepare(m)
        codes = typecodes(runner)
        assert (codes["root"], codes["home"]) == (root, home)
        assert codes["EFI"] == "EF00" and codes["swap"] == "8200"

    def test_an_encrypted_root_keeps_the_generic_code_and_boot_stays_generic(self, tmp_path):
        runner = StatefulRunner([vda()])
        m = erase(luks=True, luks_passphrase="correct horse battery")
        m.system.bootloader = "systemd-boot"
        make_manager(runner, tmp_path).prepare(m)
        assert typecodes(runner)["root"] == "8300" and typecodes(runner)["boot"] == "8300"


class TestTmpPermissions:
    def rows(self):
        return (ESP_ROW, ROOT_ON_B, Partition("/dev/sdb2", "/tmp", "ext4", format=True), Partition("/dev/sdb3", "/var/tmp", "ext4", format=True))

    def test_separate_tmp_directories_are_world_writable_with_the_sticky_bit(self, tmp_path):
        disks = machine()
        disks[1].parts[1].update(size=20 * GiB, fstype="ext4")
        disks[1].parts.append({"num": 3, "size": 20 * GiB, "fstype": "ext4"})
        runner = StatefulRunner(disks)
        make_manager(runner, tmp_path).prepare(manual(*self.rows(), additional_device_ids=["SER-B"]))
        for name in ("tmp", "var/tmp"):
            mode = (tmp_path / "target" / name).stat().st_mode
            assert stat.S_IMODE(mode) == 0o1777, name
        assert not (stat.S_IMODE((tmp_path / "target").stat().st_mode) & stat.S_ISVTX)


# --- STOR-04: swap -------------------------------------------------------------------------------


class TestSwap:
    @pytest.mark.parametrize(("gib", "choice", "mib"), [
        (1, "recommended", 4096), (4, "recommended", 4096), (16, "recommended", 8192), (48, "recommended", 16384), (96, "recommended", 8192),
        (4, "hibernate", 8192), (16, "hibernate", 24576), (96, "hibernate", 8192),
    ])
    def test_the_recommended_size_follows_the_handbook_table(self, tmp_path, gib, choice, mib):
        with_memory(tmp_path, gib)
        runner = StatefulRunner([vda()])
        m = erase(swap_choice=choice, swap_mib=1)
        make_manager(runner, tmp_path).prepare(m)
        sizes = [a for argv in runner.commands if argv[0] == "sgdisk" for a in argv if ":0:+" in a and "--new=" in a]
        assert any(item.endswith(f":0:+{mib}MiB") for item in sizes), sizes

    def test_48_gib_with_hibernation_gets_one_and_a_half_times_the_memory(self, tmp_path):
        from tests.backend.test_storage_support import FakeDisk

        with_memory(tmp_path, 48)
        runner = StatefulRunner([FakeDisk("/dev/vda", 200 * GiB, serial="SER", wwn="WWN-1")])
        make_manager(runner, tmp_path).prepare(erase(swap_choice="hibernate"))
        assert any(a.endswith(":0:+73728MiB") for argv in runner.commands if argv[0] == "sgdisk" for a in argv)

    def test_none_creates_no_swap(self, tmp_path):
        runner = StatefulRunner([vda()])
        make_manager(runner, tmp_path).prepare(erase(swap_choice="none", swap_mib=4096))
        assert "swap" not in typecodes(runner) and not any(c[0] in {"mkswap", "swapon"} for c in runner.commands)

    def test_zram_creates_no_disk_swap(self, tmp_path):
        runner = StatefulRunner([vda()])
        make_manager(runner, tmp_path).prepare(erase(swap_kind="zram"))
        assert "swap" not in typecodes(runner) and not any(c[0] == "mkswap" for c in runner.commands)

    def test_a_swap_file_without_encryption(self, tmp_path):
        runner = StatefulRunner([vda()])
        manager = make_manager(runner, tmp_path)
        manager.prepare(erase(swap_kind="file", swap_mib=2048))
        assert "swap" not in typecodes(runner)
        assert ("mkswap", "--file", "--size", "2048MiB", str(tmp_path / "target" / "swapfile")) in runner.commands
        assert any(entry.source == "/swapfile" for entry in manager.layout)

    def test_a_btrfs_swap_file_uses_mkswapfile(self, tmp_path):
        runner = StatefulRunner([vda()])
        make_manager(runner, tmp_path).prepare(erase(swap_kind="file", swap_mib=2048, filesystem="btrfs"))
        assert ("btrfs", "filesystem", "mkswapfile", "--size", "2048m", str(tmp_path / "target" / "swap" / "swapfile")) in runner.commands

    def test_encrypted_auto_swap_is_still_a_file(self, tmp_path):
        runner = StatefulRunner([vda()])
        manager = make_manager(runner, tmp_path)
        manager.prepare(erase(luks=True, luks_passphrase="correct horse battery"))
        assert "swap" not in typecodes(runner) and any(entry.source for entry in manager.layout)

    def test_a_swap_partition_with_encryption_is_refused(self, tmp_path):
        runner = StatefulRunner([vda()])
        with pytest.raises(StorageError, match="without encryption"):
            make_manager(runner, tmp_path).prepare(erase(luks=True, luks_passphrase="correct horse battery", swap_kind="partition"))
        assert not any(c[0] == "sgdisk" for c in runner.commands)

    def test_a_recommended_swap_partition_on_an_encrypted_disk_is_refused_even_when_swap_mib_is_zero(self, tmp_path):
        with_memory(tmp_path, 16)
        runner = StatefulRunner([vda()])
        m = erase(luks=True, luks_passphrase="correct horse battery", swap_kind="partition", swap_choice="recommended", swap_mib=0)
        with pytest.raises(StorageError, match="without encryption"):
            make_manager(runner, tmp_path).prepare(m)
        assert not any(c[0] in {"sgdisk", "parted", "wipefs", "cryptsetup"} for c in runner.commands)

    def test_hibernation_with_zram_is_refused(self, tmp_path):
        with pytest.raises(StorageError, match="Hibernation needs swap on the disk"):
            make_manager(StatefulRunner([vda()]), tmp_path).prepare(erase(swap_kind="zram", swap_choice="hibernate"))

    def test_the_plan_shows_the_resolved_size(self, tmp_path):
        with_memory(tmp_path, 16)
        plan = make_manager(StatefulRunner([vda()]), tmp_path).describe(erase(swap_choice="recommended"))
        assert [v.size_mib for v in plan.disks[0].volumes if v.role == "swap"] == [8192]


# --- STOR-06: /boot -------------------------------------------------------------------------------


class TestSeparateBoot:
    @pytest.mark.parametrize(("firmware", "table", "expected"), [("uefi", "gpt", ["EFI", "boot", "swap", "root"]), ("bios", "gpt", ["BIOS", "boot", "swap", "root"])])
    def test_gpt_layouts(self, tmp_path, firmware, table, expected):
        runner = StatefulRunner([vda()])
        manager = make_manager(runner, tmp_path)
        manager.prepare(erase(firmware=firmware, table=table, separate_boot=True))
        assert list(typecodes(runner)) == expected
        assert any(e.mountpoint == "/boot" and e.filesystem == "ext4" for e in manager.layout)

    def test_dos_with_bios_counts_the_boot_partition_among_four(self, tmp_path):
        runner = StatefulRunner([vda()])
        make_manager(runner, tmp_path).prepare(erase(firmware="bios", table="dos", separate_boot=True))
        assert [c[5] for c in runner.commands if c[:2] == ("parted", "--script") and "mkpart" in c] == ["ext4", "linux-swap", "ext4"]
        runner = StatefulRunner([vda()])
        with pytest.raises(StorageError, match="four primary"):
            make_manager(runner, tmp_path).prepare(erase(firmware="uefi", table="dos", separate_boot=True, separate_home=True, home_mib=20480, swap_mib=4096))

    @pytest.mark.parametrize("change", [{"lvm": True}, {"luks": True, "luks_passphrase": "correct horse battery"}])
    def test_lvm_and_luks_force_the_boot_partition(self, change):
        m = erase(**change)
        assert layout.has_boot_partition(m.storage)

    def test_noauto_is_written_to_fstab(self):
        entries = [LayoutEntry("/dev/vda2", "/boot", "ext4"), LayoutEntry("/dev/vda3", "/", "ext4")]
        text = render_fstab(entries, {"/dev/vda2": "b", "/dev/vda3": "r"}, boot_noauto=True)
        assert "UUID=b /boot ext4 defaults,noatime,noauto 0 2" in text and "UUID=r / ext4 defaults,noatime 0 1" in text

    def test_noauto_without_a_boot_partition_is_refused(self, tmp_path):
        with pytest.raises(StorageError, match="noauto"):
            make_manager(StatefulRunner([vda()]), tmp_path).prepare(erase(boot_noauto=True))


# --- STOR-05: filesystems -------------------------------------------------------------------------


class TestFilesystems:
    def mkfs(self, runner, name):
        return next(c for c in runner.commands if c[0] == name)

    def test_small_ext4_gets_the_small_profile(self, tmp_path):
        runner = StatefulRunner([vda()])
        runner._cmd_blockdev = lambda args: runner.ok(args, f"{4 * GiB}\n")
        make_manager(runner, tmp_path).prepare(erase(device="/dev/vda"))
        # 1 KiB blocks: without the orphan file, which the kernel refuses at that block size (the first real VM run of a separate /boot)
        assert self.mkfs(runner, "mkfs.ext4") == ("mkfs.ext4", "-F", "-T", "small", "-O", "^orphan_file", "/dev/vda3")

    def test_large_ext4_does_not(self, tmp_path):
        runner = StatefulRunner([vda()])
        runner._cmd_blockdev = lambda args: runner.ok(args, f"{60 * GiB}\n")
        make_manager(runner, tmp_path).prepare(erase())
        assert self.mkfs(runner, "mkfs.ext4") == ("mkfs.ext4", "-F", "/dev/vda3")

    def test_xfs_reads_the_newest_config_when_present(self, tmp_path):
        config = tmp_path / "xfsconf"
        config.mkdir()
        for name in ("lts_5.15.conf", "lts_6.12.conf", "lts_6.6.conf", "other.conf"):
            (config / name).write_text("")
        runner = StatefulRunner([vda()])
        make_manager(runner, tmp_path, xfs_config_dir=config).prepare(erase(filesystem="xfs"))
        assert self.mkfs(runner, "mkfs.xfs") == ("mkfs.xfs", "-f", "-c", f"options={config / 'lts_6.12.conf'}", "/dev/vda3")

    def test_xfs_without_a_config_directory(self, tmp_path):
        runner = StatefulRunner([vda()])
        make_manager(runner, tmp_path, xfs_config_dir=tmp_path / "absent").prepare(erase(filesystem="xfs"))
        assert self.mkfs(runner, "mkfs.xfs") == ("mkfs.xfs", "-f", "/dev/vda3")

    def test_f2fs_root_with_a_separate_boot(self, tmp_path):
        runner = StatefulRunner([vda()])
        manager = make_manager(runner, tmp_path)
        manager.prepare(erase(filesystem="f2fs", separate_boot=True))
        assert self.mkfs(runner, "mkfs.f2fs") == ("mkfs.f2fs", "-f", "/dev/vda4")
        assert ("mount", "/dev/vda4", str(tmp_path / "target")) in runner.commands

    def test_f2fs_atoms_and_manual_f2fs_root(self, tmp_path):
        m = erase(filesystem="f2fs", separate_boot=True)
        assert "sys-fs/f2fs-tools" in FstabMixin.filesystem_atoms(FstabMixin.__new__(FstabMixin), m)
        runner = StatefulRunner(machine())
        rows = (ESP_ROW, Partition("/dev/sdb1", "/", "f2fs", format=True), Partition("/dev/sdb2", "/boot", "ext4", format=True))
        make_manager(runner, tmp_path).prepare(manual(*rows, additional_device_ids=["SER-B"]))
        assert ("mkfs.f2fs", "-f", "/dev/sdb1") in runner.commands

    def test_manual_f2fs_root_with_grub_needs_a_boot_partition(self, tmp_path):
        rows = (ESP_ROW, Partition("/dev/sdb1", "/", "f2fs", format=True))
        runner = StatefulRunner(machine())
        with pytest.raises(StorageError, match="GRUB cannot reliably read an F2FS root"):
            make_manager(runner, tmp_path).prepare(manual(*rows, additional_device_ids=["SER-B"]))

    def test_swap_file_on_f2fs_is_refused(self, tmp_path):
        with pytest.raises(StorageError, match="F2FS"):
            make_manager(StatefulRunner([vda()]), tmp_path).prepare(erase(filesystem="f2fs", separate_boot=True, swap_kind="file"))


# --- STOR-10 / STOR-09: fstab ----------------------------------------------------------------------


GUIDED = [LayoutEntry("/dev/vda1", "/efi", "vfat"), LayoutEntry("/dev/vda3", "/", "ext4"), LayoutEntry("/dev/vda2", "", "swap")]
UUIDS = {"/dev/vda1": "E-1", "/dev/vda3": "R-1", "/dev/vda2": "S-1"}


class TestFstab:
    def test_the_default_is_unchanged(self):
        assert render_fstab(GUIDED, UUIDS) == (
            "# <file system> <mount point> <type> <options> <dump> <pass>\n"
            "UUID=R-1 / ext4 defaults,noatime 0 1\n"
            "UUID=S-1 none swap sw 0 0\n"
            "UUID=E-1 /efi vfat defaults,umask=0077 0 2\n")

    def test_the_fat_clock_is_utc(self):
        assert "UUID=E-1 /efi vfat defaults,umask=0077,tz=UTC 0 2" in render_fstab(GUIDED, UUIDS, tz_utc=True)

    @pytest.mark.parametrize(("atime", "text"), [("noatime", "defaults,noatime"), ("lazytime", "defaults,lazytime"), ("relatime", "defaults,relatime")])
    def test_atime_options(self, atime, text):
        assert f"UUID=R-1 / ext4 {text} 0 1" in render_fstab(GUIDED, UUIDS, atime=atime)

    def test_labels_and_partition_uuids(self):
        sources = {"/dev/vda3": "LABEL=gentoo-root", "/dev/vda1": "PARTUUID=aa-01"}
        text = render_fstab(GUIDED, UUIDS, sources=sources)
        assert "LABEL=gentoo-root / ext4" in text and "PARTUUID=aa-01 /efi vfat" in text and "UUID=S-1 none swap" in text

    def test_the_preview_is_the_same_text_with_placeholders(self):
        m = erase()
        text = layout.fstab_preview(m)
        assert "UUID=<id of root> / ext4 defaults,noatime 0 1" in text
        assert "tz=UTC" in text and "defaults,umask=0077,tz=UTC" in text
        m.system.rtc_local = True
        assert "tz=UTC" not in layout.fstab_preview(m)

    def test_preview_modes(self):
        m = erase(fstab_ident="label", atime="lazytime", separate_boot=True, boot_noauto=True)
        text = layout.fstab_preview(m)
        assert "LABEL=gentoo-root / ext4 defaults,lazytime 0 1" in text
        assert "LABEL=gentoo-boot /boot ext4 defaults,lazytime,noauto 0 2" in text and "LABEL=gentoo-swap none swap" in text
        assert "PARTUUID=<partition id of root> /" in layout.fstab_preview(erase(fstab_ident="partuuid"))


class TestIdentifiers:
    def test_guided_label_mode_labels_the_filesystems(self, tmp_path):
        runner = StatefulRunner([vda()])
        make_manager(runner, tmp_path).prepare(erase(fstab_ident="label", separate_boot=True))
        assert ("mkfs.ext4", "-F", "-L", "gentoo-root", "/dev/vda4") in runner.commands
        assert ("mkfs.ext4", "-F", "-L", "gentoo-boot", "/dev/vda2") in runner.commands
        assert ("mkfs.vfat", "-F", "32", "-n", "GENTOO-EFI", "/dev/vda1") in runner.commands
        assert ("mkswap", "-L", "gentoo-swap", "/dev/vda3") in runner.commands

    def test_uuid_mode_sets_no_label(self, tmp_path):
        runner = StatefulRunner([vda()])
        make_manager(runner, tmp_path).prepare(erase())
        assert not any("-L" in c for c in runner.commands if c[0].startswith("mkfs") or c[0] == "mkswap")

    def test_partuuid_is_refused_on_a_dos_table(self, tmp_path):
        runner = StatefulRunner([vda()])
        with pytest.raises(StorageError, match="GPT"):
            make_manager(runner, tmp_path).prepare(erase(fstab_ident="partuuid", table="dos", firmware="bios"))
        assert not any(c[0] in {"sgdisk", "parted"} for c in runner.commands)


class TestFstabStage:
    """The real storage stage and fstab writer, with blkid answering per tag."""

    def installer(self, tmp_path, runner, **changes):
        from tests.backend.test_backend_fstab import installer_for

        return installer_for(tmp_path, runner, **changes)

    def answer_tags(self, runner):
        original = runner._cmd_blkid

        def blkid(args):
            tag = args[args.index("-s") + 1] if "-s" in args else ""
            device = args[-1]
            if tag == "LABEL":
                return runner.ok(args, {"/dev/vda3": "gentoo-root", "/dev/vda1": "GENTOO-EFI"}.get(device, "") + "\n")
            if tag == "PARTUUID":
                return runner.ok(args, f"pu-{device.rsplit('/', 1)[1]}\n" if not device.startswith("/dev/mapper") else "\n")
            return original(args)

        runner._cmd_blkid = blkid

    def test_label_mode(self, tmp_path):
        from tests.backend.test_storage_support import FakeDisk

        runner = StatefulRunner([FakeDisk("/dev/vda", 80 * GiB, serial="SER", wwn="WWN-1")])
        self.answer_tags(runner)
        installer = self.installer(tmp_path, runner, fstab_ident="label")
        installer._storage()
        installer._write_fstab()
        lines = (tmp_path / "target/etc/fstab").read_text().splitlines()
        assert "LABEL=gentoo-root / ext4 defaults,noatime 0 1" in lines
        assert "LABEL=GENTOO-EFI /efi vfat defaults,umask=0077,tz=UTC 0 2" in lines
        assert any(line.startswith("UUID=") and " none swap " in line for line in lines)  # the swap partition has no label answer: UUID

    def test_partuuid_mode_and_atime(self, tmp_path):
        from tests.backend.test_storage_support import FakeDisk

        runner = StatefulRunner([FakeDisk("/dev/vda", 80 * GiB, serial="SER", wwn="WWN-1")])
        self.answer_tags(runner)
        installer = self.installer(tmp_path, runner, fstab_ident="partuuid", atime="lazytime")
        installer._storage()
        installer._write_fstab()
        lines = (tmp_path / "target/etc/fstab").read_text().splitlines()
        assert "PARTUUID=pu-vda3 / ext4 defaults,lazytime 0 1" in lines

    def test_a_local_time_hardware_clock_leaves_the_fat_clock_alone(self, tmp_path):
        from tests.backend.test_storage_support import FakeDisk

        runner = StatefulRunner([FakeDisk("/dev/vda", 80 * GiB, serial="SER", wwn="WWN-1")])
        installer = self.installer(tmp_path, runner)
        installer.manifest.system.rtc_local = True
        installer._storage()
        installer._write_fstab()
        assert "tz=UTC" not in (tmp_path / "target/etc/fstab").read_text()


class TestManualMounts:
    def rows(self):
        return (ESP_ROW, ROOT_ON_B, Partition("/dev/sdb2", "/usr", "ext4", format=True))

    def test_a_separate_usr_with_dracut(self, tmp_path):
        disks = machine()
        disks[1].parts[1].update(size=30 * GiB, fstype="ext4")
        runner = StatefulRunner(disks)
        make_manager(runner, tmp_path).prepare(manual(*self.rows(), additional_device_ids=["SER-B"]))
        assert ("mount", "/dev/sdb2", str(tmp_path / "target" / "usr")) in runner.commands

    def test_a_separate_usr_without_an_initramfs_is_refused(self, tmp_path):
        m = manual(*self.rows(), additional_device_ids=["SER-B"])
        m.system.initramfs = "none"
        runner = StatefulRunner(machine())
        with pytest.raises(StorageError, match="needs an initramfs"):
            make_manager(runner, tmp_path).prepare(m)
        assert not any(c[0] in {"sgdisk", "wipefs"} or c[0].startswith("mkfs") for c in runner.commands)
