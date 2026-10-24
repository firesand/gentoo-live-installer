"""The target's fstab: Btrfs subvolumes and the swap file of an encrypted install (they used to be silently dropped).

render_fstab ignored LayoutEntry.options and .source, so a guided Btrfs install stayed flat and a guided
LUKS install got no swap. These tests run the real storage stage and the real fstab writer together.
"""

from __future__ import annotations

import json
from pathlib import Path

from gentoo_installerd.configure import render_fstab
from gentoo_installerd.install import CheckpointStore, Installer
from gentoo_installerd.storage import LayoutEntry

from tests.backend.helpers import FakeInspector, manifest
from tests.backend.test_storage_identity import erase
from tests.backend.test_storage_support import FakeDisk, GiB, StatefulRunner, make_manager

UUIDS = {"/dev/vda3": "root-uuid", "/dev/vda1": "efi-uuid"}


def installer_for(tmp_path: Path, runner: StatefulRunner, **changes) -> Installer:
    m = manifest()
    m.storage.device, m.storage.device_id = "/dev/vda", "SER"
    for name, value in changes.items():
        setattr(m.storage, name, value)
    return Installer(m, runner=runner, storage=make_manager(runner, tmp_path), target_root=tmp_path / "target", state_dir=tmp_path / "state",
                     cache_dir=tmp_path / "cache", inspector=FakeInspector(), emit=lambda event: None)


def fstab_of(tmp_path: Path) -> list[str]:
    return (tmp_path / "target" / "etc/fstab").read_text().splitlines()


class TestRenderFstab:
    def test_mount_options_are_appended_to_the_defaults(self) -> None:
        layout = [LayoutEntry("/dev/vda3", "/", "btrfs", options="subvol=@,compress=zstd:3"),
                  LayoutEntry("/dev/vda3", "/home", "btrfs", format=False, options="subvol=@home,compress=zstd:3")]
        lines = render_fstab(layout, UUIDS).splitlines()
        # Btrfs is not fsck'd at boot (fsck.btrfs is a stub), so its pass number stays 0
        assert "UUID=root-uuid / btrfs defaults,noatime,subvol=@,compress=zstd:3 0 0" in lines
        assert "UUID=root-uuid /home btrfs defaults,noatime,subvol=@home,compress=zstd:3 0 0" in lines

    def test_a_source_replaces_the_uuid_and_the_device(self) -> None:
        layout = [LayoutEntry("/dev/mapper/gentoo-root", "/", "ext4"), LayoutEntry("/mnt/target/swapfile", "", "swap", source="/swapfile")]
        text = render_fstab(layout, {"/dev/mapper/gentoo-root": "root-uuid", "/mnt/target/swapfile": "must-not-be-used"})
        assert "/swapfile none swap sw 0 0" in text.splitlines()
        assert "must-not-be-used" not in text
        assert "/mnt/target" not in text  # the live system's path never reaches the installed system

    def test_entries_without_the_new_fields_are_written_as_before(self) -> None:
        layout = [LayoutEntry("/dev/vda3", "/", "ext4"), LayoutEntry("/dev/vda1", "/efi", "vfat"), LayoutEntry("/dev/vda2", "", "swap")]
        lines = render_fstab(layout, {"/dev/vda3": "r", "/dev/vda1": "e", "/dev/vda2": "s"}).splitlines()
        assert lines[1:] == ["UUID=r / ext4 defaults,noatime 0 1", "UUID=s none swap sw 0 0", "UUID=e /efi vfat defaults,umask=0077 0 2"]

    def test_asynchronous_trim_goes_on_every_btrfs_mount_only(self) -> None:
        layout = [LayoutEntry("/dev/vda3", "/", "btrfs", options="subvol=@"), LayoutEntry("/dev/vda3", "/home", "btrfs", options="subvol=@home"),
                  LayoutEntry("/dev/vda1", "/efi", "vfat")]
        lines = render_fstab(layout, UUIDS, btrfs_discard=True).splitlines()
        assert "UUID=root-uuid / btrfs defaults,noatime,subvol=@,discard=async 0 0" in lines
        assert "UUID=root-uuid /home btrfs defaults,noatime,subvol=@home,discard=async 0 0" in lines
        assert "UUID=efi-uuid /efi vfat defaults,umask=0077 0 2" in lines
        assert "discard" not in render_fstab(layout, UUIDS)


class TestGuidedInstalls:
    def test_guided_btrfs_mounts_the_subvolumes_from_fstab(self, tmp_path: Path) -> None:
        runner = StatefulRunner([FakeDisk("/dev/vda", 80 * GiB, serial="SER", wwn="WWN-1")])
        installer = installer_for(tmp_path, runner, filesystem="btrfs")
        installer._storage()
        installer._write_fstab()
        root_uuid = runner.uuid_of("/dev/vda3")
        lines = fstab_of(tmp_path)
        assert f"UUID={root_uuid} / btrfs defaults,noatime,subvol=@,compress=zstd:3 0 0" in lines
        assert f"UUID={root_uuid} /home btrfs defaults,noatime,subvol=@home,compress=zstd:3 0 0" in lines

    def test_a_guided_encrypted_install_gets_its_swap_file_in_fstab(self, tmp_path: Path) -> None:
        runner = StatefulRunner([FakeDisk("/dev/vda", 80 * GiB, serial="SER", wwn="WWN-1")])
        installer = installer_for(tmp_path, runner, luks=True, luks_passphrase="long enough passphrase")
        installer._storage()
        runner.commands.clear()
        installer._write_fstab()
        assert "/swapfile none swap sw 0 0" in fstab_of(tmp_path)
        # the file is not looked up by UUID: mkswap gave it one, but fstab names it by path
        assert not any(command[0] == "blkid" and command[-1].endswith("swapfile") for command in runner.commands)
        assert "swapfile" not in (tmp_path / "target" / "etc/fstab").read_text().replace("/swapfile none swap", "")

    def test_the_btrfs_swap_file_lives_in_its_own_subvolume(self, tmp_path: Path) -> None:
        runner = StatefulRunner([FakeDisk("/dev/vda", 80 * GiB, serial="SER", wwn="WWN-1")])
        installer = installer_for(tmp_path, runner, filesystem="btrfs", luks=True, luks_passphrase="long enough passphrase")
        installer._storage()
        installer._write_fstab()
        assert "/swap/swapfile none swap sw 0 0" in fstab_of(tmp_path)

    def test_a_plain_guided_install_has_no_swap_file_line(self, tmp_path: Path) -> None:
        runner = StatefulRunner([FakeDisk("/dev/vda", 80 * GiB, serial="SER", wwn="WWN-1")])
        installer = installer_for(tmp_path, runner)
        installer._storage()
        installer._write_fstab()
        assert not any("swapfile" in line for line in fstab_of(tmp_path))
        assert any(line.endswith(" none swap sw 0 0") and line.startswith("UUID=") for line in fstab_of(tmp_path))


class TestResume:
    def test_the_saved_layout_keeps_mount_options_and_the_swap_file_source(self, tmp_path: Path) -> None:
        runner = StatefulRunner([FakeDisk("/dev/vda", 80 * GiB, serial="SER", wwn="WWN-1")])
        installer = installer_for(tmp_path, runner, filesystem="btrfs", luks=True, luks_passphrase="long enough passphrase")
        installer._storage()
        saved = installer.checkpoints.load_layout()
        assert saved == installer.storage.layout
        assert any(entry.options == "subvol=@home,compress=zstd:3" for entry in saved)
        assert any(entry.source == "/swap/swapfile" for entry in saved)

    def test_an_old_layout_file_without_the_new_fields_still_loads(self, tmp_path: Path) -> None:
        store = CheckpointStore(tmp_path / "checkpoints.json", "fingerprint")
        (tmp_path / "layout.json").write_text(json.dumps({"fingerprint": "fingerprint", "receipt": None, "layout": [
            {"device": "/dev/vda3", "mountpoint": "/", "filesystem": "ext4", "format": True}]}))
        assert store.load_layout() == [LayoutEntry("/dev/vda3", "/", "ext4", True)]

    def test_resume_turns_the_swap_file_on_again_and_fstab_still_names_it(self, tmp_path: Path) -> None:
        runner = StatefulRunner([FakeDisk("/dev/vda", 80 * GiB, serial="SER", wwn="WWN-1")])
        first = installer_for(tmp_path, runner, luks=True, luks_passphrase="long enough passphrase")
        first._storage()
        first.storage.cleanup()
        runner.commands.clear()
        (tmp_path / "target" / "swapfile").write_text("")
        resumed = installer_for(tmp_path, runner, luks=True, luks_passphrase="long enough passphrase")
        resumed._verify_storage()
        swap_file = str(tmp_path / "target" / "swapfile")
        assert ("swapon", swap_file) in runner.commands
        resumed._write_fstab()
        assert "/swapfile none swap sw 0 0" in fstab_of(tmp_path)
