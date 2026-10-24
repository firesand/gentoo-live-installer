"""Layout details: manual swap rows (S9), preserved partitions (S10), labels, encrypted swap (S6, NEW-4),
Btrfs subvolumes (CRITIC-7) and installing over an existing system (STOR-NEW-7)."""

from __future__ import annotations

import json

import pytest

from gentoo_installer.manifest import InstallManifest, Partition
from gentoo_installerd import configure
from gentoo_installerd import storage as storage_module
from gentoo_installerd.storage import LayoutEntry, StorageError

from tests.backend.test_storage_identity import ESP_ROW, ROOT_ON_B, erase, machine, manual, no_writes, vda
from tests.backend.test_storage_support import (
    BIOS_BOOT,
    ESP,
    FakeDisk,
    GiB,
    MiB,
    StatefulRunner,
    commands_text,
    first_write,
    make_manager,
)


def supports(monkeypatch, options: bool = True, source: bool = True) -> None:
    monkeypatch.setattr(storage_module, "fstab_supports_layout_fields", lambda: (options, source))


def render_with_layout_fields(layout, uuids):
    """The proposed render_fstab: LayoutEntry.source replaces UUID=/device, LayoutEntry.options is appended."""
    lines = ["# <file system> <mount point> <type> <options> <dump> <pass>"]
    for entry in sorted(layout, key=lambda item: (item.mountpoint != "/", item.mountpoint)):
        uuid = uuids.get(entry.device, "")
        source = entry.source or (f"UUID={uuid}" if uuid else entry.device)
        extra = f",{entry.options}" if entry.options else ""
        if entry.filesystem == "swap":
            lines.append(f"{source} none swap sw 0 0")
        elif entry.mountpoint:
            options = ("defaults,noatime" if entry.filesystem != "vfat" else "defaults,umask=0077") + extra
            lines.append(f"{source} {entry.mountpoint} {entry.filesystem} {options} 0 {1 if entry.mountpoint == '/' else 2}")
    return "\n".join(lines) + "\n"


class TestManualSwap:
    def rows(self, swap_format=True):
        return (ESP_ROW, ROOT_ON_B, Partition("/dev/sdb2", "", "swap", format=swap_format))

    def test_the_manifest_accepts_a_swap_row_without_a_mountpoint(self):
        assert manual(*self.rows()).validate() == []

    def test_engine_formats_and_activates_swap_and_never_mounts_it(self, tmp_path):
        runner = StatefulRunner(machine())
        make_manager(runner, tmp_path).prepare(manual(*self.rows(), additional_device_ids=["SER-B"]))
        assert ("mkswap", "/dev/sdb2") in runner.commands
        assert ("swapon", "/dev/sdb2") in runner.commands
        assert not any(c[0] == "mount" and "/dev/sdb2" in c for c in runner.commands)

    def test_a_kept_swap_row_must_really_be_swap(self, tmp_path):
        runner = StatefulRunner(machine())
        with pytest.raises(StorageError, match="/dev/sdb2 is kept \\(format=false\\) and declared swap, but it contains ntfs"):
            make_manager(runner, tmp_path).prepare(manual(*self.rows(swap_format=False), additional_device_ids=["SER-B"]))
        no_writes(runner)


class TestPreservedPartitions:
    def test_declared_ext4_on_an_ntfs_partition_is_refused_before_any_command(self, tmp_path):
        runner = StatefulRunner(machine())
        rows = (ESP_ROW, ROOT_ON_B, Partition("/dev/sdb2", "/data", "ext4", format=False))
        with pytest.raises(StorageError, match="declared ext4, but it contains ntfs; format it or correct the declaration") as caught:
            make_manager(runner, tmp_path).prepare(manual(*rows, additional_device_ids=["SER-B"]))
        assert caught.value.code == "storage.preserved"
        no_writes(runner)

    def test_ext3_may_be_kept_as_ext4_and_matching_types_pass(self, tmp_path):
        disks = machine()
        disks[1].parts[1]["fstype"] = "ext3"
        make_manager(StatefulRunner(disks), tmp_path).prepare(manual(ESP_ROW, ROOT_ON_B, Partition("/dev/sdb2", "/data", "ext4", format=False), additional_device_ids=["SER-B"]))

    def test_a_kept_partition_without_a_filesystem_is_refused(self, tmp_path):
        disks = machine()
        disks[1].parts[1]["fstype"] = None
        runner = StatefulRunner(disks)
        with pytest.raises(StorageError, match="contains no recognisable filesystem"):
            make_manager(runner, tmp_path).prepare(manual(ESP_ROW, ROOT_ON_B, Partition("/dev/sdb2", "/data", "ext4", format=False), additional_device_ids=["SER-B"]))
        no_writes(runner)

    def test_legacy_bios_from_a_gpt_disk_needs_a_bios_boot_partition(self, tmp_path):
        rows = (Partition("/dev/sda2", "/", "ext4", format=True),)
        disks = machine()
        disks[0].parts[1]["size"] = 100 * GiB
        runner = StatefulRunner(disks)
        with pytest.raises(StorageError, match="needs a \"BIOS boot\" partition \\(type EF02.*GParted") as caught:
            make_manager(runner, tmp_path).prepare(manual(*rows, firmware="bios"))
        assert caught.value.code == "storage.bios_boot"
        no_writes(runner)
        disks = machine()
        disks[0].parts[1]["size"] = 100 * GiB
        disks[0].parts.append({"num": 3, "size": 1 * MiB, "fstype": None, "parttype": BIOS_BOOT})
        make_manager(StatefulRunner(disks), tmp_path).prepare(manual(*rows, firmware="bios"))

    def test_legacy_bios_from_a_dos_disk_needs_none(self, tmp_path):
        disks = machine()
        disks[0].pttype = "dos"
        disks[0].parts[1]["size"] = 100 * GiB
        make_manager(StatefulRunner(disks), tmp_path).prepare(manual(Partition("/dev/sda2", "/", "ext4", format=True), firmware="bios"))

    def test_the_efi_partition_must_be_vfat_and_large_enough(self, tmp_path):
        disks = machine()
        with pytest.raises(StorageError, match="must be vfat"):
            make_manager(StatefulRunner(disks), tmp_path).prepare(manual(Partition("/dev/sda1", "/efi", "ext4", format=True), ROOT_ON_B, additional_device_ids=["SER-B"]))
        disks = machine()
        disks[0].parts[0]["size"] = 32 * MiB
        with pytest.raises(StorageError, match="EFI System Partition /dev/sda1 is only 32.0 MiB"):
            make_manager(StatefulRunner(disks), tmp_path).prepare(manual(ESP_ROW, ROOT_ON_B, additional_device_ids=["SER-B"]))

    def test_a_root_too_small_for_the_desktop_is_refused_before_it_is_formatted(self, tmp_path):
        disks = machine()
        disks[1].parts[0]["size"] = 12 * GiB
        runner = StatefulRunner(disks)
        with pytest.raises(StorageError, match="root partition /dev/sdb1 is 12.0 GiB, but a kde installation needs at least 24.0 GiB") as caught:
            make_manager(runner, tmp_path).prepare(manual(ESP_ROW, ROOT_ON_B, additional_device_ids=["SER-B"]))
        assert caught.value.code == "storage.capacity"
        no_writes(runner)

    def test_a_tiny_boot_partition_is_refused(self, tmp_path):
        disks = machine()
        disks[1].parts[1]["size"] = 100 * MiB
        with pytest.raises(StorageError, match="too small for /boot"):
            make_manager(StatefulRunner(disks), tmp_path).prepare(manual(ESP_ROW, ROOT_ON_B, Partition("/dev/sdb2", "/boot", "ext4", format=True), additional_device_ids=["SER-B"]))

    def test_a_root_on_something_that_is_not_a_filesystem_we_install_to_is_refused(self, tmp_path):
        with pytest.raises(StorageError, match="must be ext4, XFS or Btrfs"):
            make_manager(StatefulRunner(machine()), tmp_path).prepare(manual(ESP_ROW, Partition("/dev/sdb1", "/", "vfat", format=True), additional_device_ids=["SER-B"]))


class TestLabels:
    def run(self, tmp_path, filesystem, label, monkeypatch=None):
        runner = StatefulRunner(machine())
        rows = (ESP_ROW, ROOT_ON_B, Partition("/dev/sdb2", "/data" if filesystem != "swap" else "", filesystem, format=True, label=label))
        make_manager(runner, tmp_path).prepare(manual(*rows, additional_device_ids=["SER-B"]))
        return runner

    @pytest.mark.parametrize(
        ("filesystem", "expected"),
        [
            ("ext4", ("mkfs.ext4", "-F", "-L", "data", "/dev/sdb2")),
            ("xfs", ("mkfs.xfs", "-f", "-L", "data", "/dev/sdb2")),
            ("btrfs", ("mkfs.btrfs", "-f", "-L", "data", "/dev/sdb2")),
            ("vfat", ("mkfs.vfat", "-F", "32", "-n", "data", "/dev/sdb2")),
            ("swap", ("mkswap", "-L", "data", "/dev/sdb2")),
        ],
    )
    def test_label_reaches_the_mkfs_command(self, tmp_path, filesystem, expected):
        assert expected in self.run(tmp_path, filesystem, "data").commands

    def test_rows_without_a_label_keep_the_old_commands(self, tmp_path):
        runner = self.run(tmp_path, "ext4", "")
        assert ("mkfs.ext4", "-F", "/dev/sdb2") in runner.commands
        assert ("mkfs.ext4", "-F", "/dev/sdb1") in runner.commands

    def test_the_label_of_an_encrypted_root_follows_the_mapped_device(self, tmp_path):
        runner = StatefulRunner(machine())
        rows = (ESP_ROW, Partition("/dev/sdb1", "/", "ext4", format=True, label="gentoo"), Partition("/dev/sdb2", "/boot", "ext4", format=True))
        m = manual(*rows, additional_device_ids=["SER-B"], luks=True, luks_passphrase="long enough passphrase")
        make_manager(runner, tmp_path).prepare(m)
        assert ("mkfs.ext4", "-F", "-L", "gentoo", "/dev/mapper/gentoo-root") in runner.commands


def encrypted(**changes) -> InstallManifest:
    return erase(luks=True, luks_passphrase="long enough passphrase", **changes)


class TestEncryptedSwap:
    """An encrypted install must not leave swap in the clear: it lives in a file inside the encrypted root."""

    def prepare(self, tmp_path, monkeypatch, **changes):
        supports(monkeypatch)
        runner = StatefulRunner([FakeDisk("/dev/vda", 80 * GiB, serial="SER", wwn="WWN-1")])
        manager = make_manager(runner, tmp_path)
        manager.prepare(encrypted(**changes))
        return runner, manager

    def test_guided_luks_has_no_plain_swap_partition_and_no_plain_mkswap(self, tmp_path, monkeypatch):
        runner, manager = self.prepare(tmp_path, monkeypatch)
        assert [name for name in commands_text(runner) if name.startswith("sgdisk --new")] == [
            "sgdisk --new=1:0:+1024MiB --typecode=1:EF00 --change-name=1:EFI /dev/vda",
            "sgdisk --new=2:0:+1024MiB --typecode=2:8300 --change-name=2:boot /dev/vda",
            "sgdisk --new=3:0:0 --typecode=3:8300 --change-name=3:root /dev/vda",
        ]
        assert all(c[-1] != "/dev/vda2" for c in runner.commands if c[0] == "mkswap")
        assert not any(c[0] == "mkswap" and not c[1:2] == ("--file",) for c in runner.commands)

    def test_the_swap_file_is_created_on_the_mounted_encrypted_root_and_used(self, tmp_path, monkeypatch):
        runner, manager = self.prepare(tmp_path, monkeypatch)
        swap_file = str(tmp_path / "target" / "swapfile")
        text = commands_text(runner)
        assert f"mkswap --file --size 4096MiB {swap_file}" in text
        assert text.index("mount /dev/mapper/gentoo-root " + str(tmp_path / "target")) < text.index(f"mkswap --file --size 4096MiB {swap_file}") < text.index(f"swapon {swap_file}")
        entry = next(e for e in manager.layout if e.source)
        assert entry == LayoutEntry(swap_file, "", "swap", True, source="/swapfile")
        assert swap_file in manager.owned_swaps

    def test_btrfs_keeps_the_swap_file_in_its_own_subvolume(self, tmp_path, monkeypatch):
        runner, manager = self.prepare(tmp_path, monkeypatch, filesystem="btrfs")
        target = str(tmp_path / "target")
        text = commands_text(runner)
        assert f"btrfs subvolume create {target}/swap" in text
        assert f"btrfs filesystem mkswapfile --size 4096m {target}/swap/swapfile" in text
        assert next(e for e in manager.layout if e.source).source == "/swap/swapfile"

    def test_without_swap_there_is_no_file(self, tmp_path, monkeypatch):
        runner, manager = self.prepare(tmp_path, monkeypatch, swap_mib=0)
        assert not any("swapfile" in " ".join(c) or c[0] == "swapon" for c in runner.commands)

    def test_an_fstab_writer_that_cannot_describe_a_swap_file_gets_no_swap_file(self, tmp_path, monkeypatch):
        supports(monkeypatch, options=True, source=False)
        runner = StatefulRunner([FakeDisk("/dev/vda", 80 * GiB, serial="SER", wwn="WWN-1")])
        manager = make_manager(runner, tmp_path)
        manager.prepare(encrypted())
        assert not any(c[0] in {"swapon", "mkswap"} for c in runner.commands)
        assert any("Swap was skipped" in note for note in manager.notes)

    def test_the_swap_file_counts_against_the_disk(self, tmp_path, monkeypatch):
        supports(monkeypatch)
        runner = StatefulRunner([FakeDisk("/dev/vda", 36 * GiB, serial="SER", wwn="WWN-1")])
        with pytest.raises(StorageError, match="swap file 16.0 GiB inside the encrypted root"):
            make_manager(runner, tmp_path).prepare(encrypted(swap_mib=16384))
        no_writes(runner)

    def test_a_swap_file_is_released_before_the_root_is_unmounted(self, tmp_path, monkeypatch):
        runner, manager = self.prepare(tmp_path, monkeypatch)
        runner.commands.clear()
        manager.cleanup()
        target = str(tmp_path / "target")
        swap_file = target + "/swapfile"
        text = commands_text(runner)
        assert text.index(f"swapoff {swap_file}") < text.index(f"umount -R {target}")
        assert manager.owned_swaps == [] and manager.owned_mounts == []

    def test_resume_reactivates_the_swap_file_without_creating_anything(self, tmp_path, monkeypatch):
        runner, manager = self.prepare(tmp_path, monkeypatch)
        receipt = json.loads(json.dumps(manager.snapshot()))
        assert receipt["version"] == 2 and "uuids" in receipt and not any(device.endswith("swapfile") for device in receipt["uuids"])
        manager.cleanup()
        runner.commands.clear()
        # layout.json only keeps four fields: the swap file's source comes back from the receipt
        brief = [LayoutEntry(e.device, e.mountpoint, e.filesystem, e.format) for e in manager.layout]
        (tmp_path / "target" / "swapfile").write_text("")
        again = make_manager(runner, tmp_path)
        again.restore(encrypted(), brief, receipt)
        swap_file = str(tmp_path / "target" / "swapfile")
        assert ("swapon", swap_file) in runner.commands
        assert any(e.source == "/swapfile" for e in again.layout)
        assert first_write(runner.commands) is None, runner.commands

    def test_summary_text_matches_what_is_built(self, tmp_path, monkeypatch):
        runner, manager = self.prepare(tmp_path, monkeypatch)
        text = manager.manifest.storage.encryption_summary()
        assert "swap file (inside the encrypted root)" in text


class TestBtrfsSubvolumes:
    def prepare(self, tmp_path, monkeypatch, *, options=True, **changes):
        supports(monkeypatch, options=options)
        runner = StatefulRunner([FakeDisk("/dev/vda", 80 * GiB, serial="SER", wwn="WWN-1")])
        manager = make_manager(runner, tmp_path)
        manager.prepare(erase(filesystem="btrfs", **changes))
        return runner, manager

    def test_subvolumes_are_created_before_the_final_mounts(self, tmp_path, monkeypatch):
        runner, manager = self.prepare(tmp_path, monkeypatch)
        target = str(tmp_path / "target")
        text = commands_text(runner)
        order = [
            text.index("mkfs.btrfs -f /dev/vda3"),
            text.index(f"mount /dev/vda3 {target}"),
            text.index(f"btrfs subvolume create {target}/@"),
            text.index(f"btrfs subvolume create {target}/@home"),
            text.index(f"btrfs subvolume set-default {target}/@"),
            text.index(f"umount {target}"),
            text.index(f"mount -o subvol=@,compress=zstd:3 /dev/vda3 {target}"),
            text.index(f"mount -o subvol=@home,compress=zstd:3 /dev/vda3 {target}/home"),
        ]
        assert order == sorted(order), text
        assert text.count("mkfs.btrfs -f /dev/vda3") == 1
        assert [(e.mountpoint, e.options) for e in manager.layout if e.device == "/dev/vda3"] == [("/", "subvol=@,compress=zstd:3"), ("/home", "subvol=@home,compress=zstd:3")]
        assert [e.format for e in manager.layout if e.device == "/dev/vda3"] == [True, False]
        assert manager.owned_mounts.count(tmp_path / "target") == 1

    def test_a_separate_home_partition_leaves_only_the_root_subvolume(self, tmp_path, monkeypatch):
        runner, manager = self.prepare(tmp_path, monkeypatch, separate_home=True, home_mib=10 * 1024)
        text = commands_text(runner)
        assert not any(line.endswith("@home") for line in text)
        assert [e.mountpoint for e in manager.layout if e.options] == ["/"]

    def test_the_fstab_writer_decides_whether_subvolumes_are_used(self, tmp_path, monkeypatch):
        runner, manager = self.prepare(tmp_path, monkeypatch, options=False)
        assert not any(c[0] == "btrfs" for c in runner.commands)
        assert all(not e.options for e in manager.layout)
        assert ("mount", "/dev/vda3", str(tmp_path / "target")) in runner.commands

    def test_the_proposed_fstab_renderer_writes_the_subvolume_options(self, tmp_path, monkeypatch):
        runner, manager = self.prepare(tmp_path, monkeypatch)
        uuids = {e.device: manager._uuid(e.device) for e in manager.layout if not e.source}
        text = render_with_layout_fields(manager.layout, uuids)
        assert f"UUID={uuids['/dev/vda3']} / btrfs defaults,noatime,subvol=@,compress=zstd:3 0 1" in text
        assert f"UUID={uuids['/dev/vda3']} /home btrfs defaults,noatime,subvol=@home,compress=zstd:3 0 2" in text

    def test_the_capability_probe_asks_the_real_fstab_writer(self, monkeypatch):
        monkeypatch.setattr(configure, "render_fstab", render_with_layout_fields)
        assert storage_module.fstab_supports_layout_fields() == (True, True)
        monkeypatch.setattr(configure, "render_fstab", lambda layout, uuids: "")
        assert storage_module.fstab_supports_layout_fields() == (False, False)

        def explode(layout, uuids):
            raise RuntimeError("boom")

        monkeypatch.setattr(configure, "render_fstab", explode)
        assert storage_module.fstab_supports_layout_fields() == (False, False)

    def test_resume_mounts_every_subvolume_with_its_options(self, tmp_path, monkeypatch):
        runner, manager = self.prepare(tmp_path, monkeypatch)
        receipt = json.loads(json.dumps(manager.snapshot()))
        manager.cleanup()
        runner.commands.clear()
        brief = [LayoutEntry(e.device, e.mountpoint, e.filesystem, e.format) for e in manager.layout]
        again = make_manager(runner, tmp_path)
        again.restore(erase(filesystem="btrfs"), brief, receipt)
        target = str(tmp_path / "target")
        assert ("mount", "-o", "subvol=@,compress=zstd:3", "/dev/vda3", target) in runner.commands
        assert ("mount", "-o", "subvol=@home,compress=zstd:3", "/dev/vda3", target + "/home") in runner.commands
        assert not any(c[0].startswith("mkfs") or c[0] in {"btrfs", "sgdisk", "wipefs"} for c in runner.commands)

    def test_resume_accepts_a_filesystem_mounted_twice_at_the_expected_places(self, tmp_path, monkeypatch):
        runner, manager = self.prepare(tmp_path, monkeypatch)
        receipt = json.loads(json.dumps(manager.snapshot()))
        runner.commands.clear()
        again = make_manager(runner, tmp_path)
        # the volume is still mounted at / and /home by the first run: nothing is remounted, nothing complains
        again.restore(erase(filesystem="btrfs"), [LayoutEntry(e.device, e.mountpoint, e.filesystem, e.format) for e in manager.layout], receipt)
        assert not any(c[0] == "mount" for c in runner.commands)


class TestExistingSystem:
    def prepare(self, tmp_path, *, consent=False, populate=True, format_root=False):
        runner = StatefulRunner(machine())
        manager = make_manager(runner, tmp_path)
        target = tmp_path / "target"
        if populate:
            (target / "etc").mkdir(parents=True, exist_ok=True)
            (target / "etc" / "passwd").write_text("root:x:0:0::/root:/bin/sh\n")
            (target / "lost+found").mkdir(exist_ok=True)
            (target / "root" / ".ssh").mkdir(parents=True, exist_ok=True)
            (target / "home" / "alice" / ".ssh").mkdir(parents=True, exist_ok=True)
        m = manual(ESP_ROW, Partition("/dev/sdb1", "/", "ext4", format=format_root), additional_device_ids=["SER-B"], overwrite_existing_system=consent)
        return runner, manager, m

    def test_a_kept_root_that_holds_a_system_is_refused_without_consent(self, tmp_path):
        runner, manager, m = self.prepare(tmp_path)
        with pytest.raises(StorageError, match="already contains files \\(etc, home, root\\).*replacing its /etc, accounts and boot files") as caught:
            manager.prepare(m)
        assert caught.value.code == "storage.existing_system"
        assert not any(c[0] == "tar" for c in runner.commands)
        assert not any(c[:1] == ("mount",) and c[-1].endswith("/efi") for c in runner.commands)  # refused before the other mount points exist
        assert (tmp_path / "target" / "etc" / "passwd").read_text().startswith("root:")

    def test_with_consent_etc_and_ssh_keys_are_saved_first(self, tmp_path):
        runner, manager, m = self.prepare(tmp_path, consent=True)
        manager.prepare(m)
        tars = [c for c in runner.commands if c[0] == "tar"]
        assert len(tars) == 1
        argv = tars[0]
        assert argv[:3] == ("tar", "--zstd", "-cpf")
        assert argv[3].startswith(str(tmp_path / "target" / "root" / "pre-install-backup-")) and argv[3].endswith(".tar.zst")
        assert argv[4:] == ("-C", str(tmp_path / "target"), "etc", "home/alice/.ssh", "root/.ssh")
        assert any("saved to /root/pre-install-backup-" in note for note in manager.notes)
        assert [i for i, c in enumerate(runner.commands) if c[0] == "tar"][0] < [i for i, c in enumerate(runner.commands) if c[:1] == ("mount",) and c[-1].endswith("/efi")][0]

    def test_a_failed_backup_stops_the_stage(self, tmp_path):
        runner, manager, m = self.prepare(tmp_path, consent=True)
        runner.fail["tar"] = (2, "No space left on device")
        with pytest.raises(StorageError, match="safety backup of the existing system could not be written"):
            manager.prepare(m)

    @pytest.mark.parametrize("names", [[], ["lost+found"]])
    def test_an_empty_root_needs_no_consent_and_no_backup(self, tmp_path, names):
        runner, manager, m = self.prepare(tmp_path, populate=False)
        for name in names:
            (tmp_path / "target" / name).mkdir(parents=True, exist_ok=True)
        manager.prepare(m)
        assert not any(c[0] == "tar" for c in runner.commands)

    def test_a_formatted_root_is_never_checked(self, tmp_path):
        runner, manager, m = self.prepare(tmp_path, format_root=True)
        manager.prepare(m)
        assert not any(c[0] == "tar" for c in runner.commands)

    def test_resume_never_runs_the_guard(self, tmp_path):
        runner, manager, m = self.prepare(tmp_path, consent=True)
        manager.prepare(m)
        receipt = json.loads(json.dumps(manager.snapshot()))
        manager.cleanup()
        runner.commands.clear()
        # the root now holds the freshly extracted new system: resume must not treat that as an old one
        make_manager(runner, tmp_path).restore(m, manager.layout, receipt)
        assert not any(c[0] == "tar" for c in runner.commands)


class TestRaidMessages:
    def test_erase_mode_raid_says_how_to_do_it(self, tmp_path):
        runner = StatefulRunner([vda()])
        with pytest.raises(StorageError, match="not created by the installer: assemble the array first") as caught:
            make_manager(runner, tmp_path).prepare(erase(raid_level="1"), dry_run=True)
        assert caught.value.code == "storage.raid"
        assert runner.commands == []

    def test_manual_raid_needs_the_md_device(self, tmp_path):
        with pytest.raises(StorageError, match="need the assembled /dev/md device"):
            make_manager(StatefulRunner([vda()]), tmp_path).prepare(manual(ESP_ROW, ROOT_ON_B, raid_level="1"), dry_run=True)


def test_layout_entries_keep_their_old_positional_shape():
    entry = LayoutEntry("/dev/sda1", "/", "ext4")
    assert (entry.format, entry.options, entry.source) == (True, "", "")
    assert LayoutEntry("/dev/sda1", "/", "ext4", False).format is False
