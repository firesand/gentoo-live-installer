from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gentoo_installer.manifest import Partition
from gentoo_installerd.storage import StorageError, StorageManager

from tests.backend.helpers import FakeRunner, lsblk, manifest


class StorageCommandTests(unittest.TestCase):
    def test_erase_layout_and_mkfs_commands(self) -> None:
        m = manifest()
        runner = FakeRunner()
        manager = StorageManager(runner, "/tmp/unused-target")
        layout = manager.prepare(m, dry_run=True)
        self.assertEqual([(entry.device, entry.mountpoint) for entry in layout], [("/dev/sda1", "/efi"), ("/dev/sda3", "/"), ("/dev/sda2", "")])
        self.assertEqual(runner.commands[0], ("sgdisk", "--zap-all", "/dev/sda"))
        self.assertIn(("mkfs.vfat", "-F", "32", "/dev/sda1"), runner.commands)
        self.assertIn(("mkfs.ext4", "-F", "/dev/sda3"), runner.commands)
        self.assertIn(("mkswap", "/dev/sda2"), runner.commands)
        self.assertFalse(any(command[0] == "mount" for command in runner.commands))

    def test_dos_partitions_have_monotonic_offsets_and_esp_flag(self) -> None:
        m = manifest()
        m.storage.table = "dos"
        runner = FakeRunner()
        StorageManager(runner).prepare(m, dry_run=True)
        partitions = [command for command in runner.commands if command[:3] == ("parted", "--script", "/dev/sda") and "mkpart" in command]
        self.assertEqual(partitions[0][-2:], ("1MiB", "1025MiB"))
        self.assertEqual(partitions[1][-2:], ("1025MiB", "5121MiB"))
        self.assertEqual(partitions[2][-2:], ("5121MiB", "100%"))
        self.assertIn(("parted", "--script", "/dev/sda", "set", "1", "esp", "on"), runner.commands)

    def test_nvme_partition_names(self) -> None:
        m = manifest()
        m.storage.device = "/dev/nvme0n1"
        m.storage.device_id = ""
        manager = StorageManager(FakeRunner())
        layout = manager.prepare(m, dry_run=True)
        self.assertEqual(layout[0].device, "/dev/nvme0n1p1")
        self.assertIn("/dev/nvme0n1p3", [entry.device for entry in layout])

    def test_luks_and_lvm_use_secret_stdin(self) -> None:
        m = manifest()
        m.storage.luks = True
        m.storage.luks_passphrase = "correct horse battery"
        m.storage.lvm = True
        runner = FakeRunner()
        layout = StorageManager(runner).prepare(m, dry_run=True)
        self.assertEqual(next(entry.device for entry in layout if entry.mountpoint == "/"), "/dev/gentoo/root")
        crypt = [index for index, command in enumerate(runner.commands) if command[0] == "cryptsetup"]
        self.assertEqual(len(crypt), 2)
        self.assertTrue(all(runner.secrets[index] for index in crypt))
        self.assertTrue(all(runner.inputs[index] == m.storage.luks_passphrase for index in crypt))
        self.assertFalse(any(m.storage.luks_passphrase in arg for command in runner.commands for arg in command))

    def test_unsupported_raid_erase_fails_before_commands(self) -> None:
        m = manifest()
        m.storage.raid_level = "1"
        runner = FakeRunner()
        with self.assertRaises(StorageError):
            StorageManager(runner).prepare(m, dry_run=True)
        self.assertEqual(runner.commands, [])


class StorageGuardTests(unittest.TestCase):
    def test_mounted_or_live_disk_is_rejected_before_erase(self) -> None:
        for mountpoint in ("/", "/home"):
            m = manifest()
            runner = FakeRunner()
            runner.respond("lsblk", lsblk(mountpoint=mountpoint))
            with self.subTest(mountpoint=mountpoint), self.assertRaises(StorageError):
                StorageManager(runner, tempfile.mkdtemp()).prepare(m)
            self.assertFalse(any(command[0] in {"sgdisk", "parted", "mkfs.ext4"} for command in runner.commands))

    def test_identity_mismatch_is_rejected(self) -> None:
        m = manifest()
        runner = FakeRunner()
        runner.respond("lsblk", lsblk(serial="another-disk"))
        with self.assertRaisesRegex(StorageError, "identity"):
            StorageManager(runner).prepare(m)
        self.assertFalse(any(command[0] == "sgdisk" for command in runner.commands))

    def test_disk_without_stable_identity_is_rejected(self) -> None:
        m = manifest()
        m.storage.device_id = ""
        runner = FakeRunner()
        raw = json.loads(lsblk(serial=""))
        raw["blockdevices"][0]["wwn"] = ""
        runner.respond("lsblk", json.dumps(raw))
        with self.assertRaisesRegex(StorageError, "stable"):
            StorageManager(runner).prepare(m)

    def test_manual_layout_formats_and_mounts_only_selected_root(self) -> None:
        m = manifest()
        m.storage.mode = "manual"
        m.storage.device_id = ""
        m.storage.firmware = "bios"
        m.storage.swap_mib = 0
        m.storage.partitions = [Partition(device="/dev/sda1", mountpoint="/", filesystem="ext4")]
        raw = json.loads(lsblk())
        raw["blockdevices"][0]["mountpoints"] = []
        raw["blockdevices"][0]["children"][0]["mountpoints"] = []
        runner = FakeRunner()
        runner.respond("lsblk", json.dumps(raw))
        with tempfile.TemporaryDirectory() as root:
            manager = StorageManager(runner, root)
            manager.prepare(m)
            self.assertIn(("mkfs.ext4", "-F", "/dev/sda1"), runner.commands)
            self.assertIn(("mount", "/dev/sda1", root), runner.commands)
            manager.cleanup()
            self.assertEqual(runner.commands[-1], ("umount", "-R", root))

    def test_duplicate_manual_devices_are_rejected(self) -> None:
        m = manifest()
        m.storage.mode = "manual"
        m.storage.partitions = [Partition(device="/dev/sda1", mountpoint="/"), Partition(device="/dev/sda1", mountpoint="/home")]
        with self.assertRaisesRegex(StorageError, "duplicate"):
            StorageManager(FakeRunner()).prepare(m, dry_run=True)

    def test_cleanup_uses_reverse_order_and_ignores_unowned_paths(self) -> None:
        runner = FakeRunner()
        manager = StorageManager(runner)
        manager.owned_mounts = [Path("/mnt/gentoo"), Path("/mnt/gentoo/home"), Path("/mnt/gentoo/dev")]
        manager.cleanup()
        self.assertEqual(runner.commands, [("umount", "-R", "/mnt/gentoo/dev"), ("umount", "-R", "/mnt/gentoo/home"), ("umount", "-R", "/mnt/gentoo")])


class StorageResumeSafetyTests(unittest.TestCase):
    def fixture(self, root):
        runner = FakeRunner()
        runner.respond("lsblk", lsblk())
        runner.respond("blkid -s UUID -o value /dev/sda1", "root-uuid\n")
        manager = StorageManager(runner, root)
        from gentoo_installerd.storage import LayoutEntry
        layout = [LayoutEntry("/dev/sda1", "/", "ext4")]
        receipt = {"signatures": {"/dev/sda": ["disk-serial", "wwn-disk", 100000000000]}, "uuids": {"/dev/sda1": "root-uuid"}, "stack": {}}
        return runner, manager, layout, receipt

    def test_resume_remounts_without_reformatting(self):
        with tempfile.TemporaryDirectory() as root:
            runner, manager, layout, receipt = self.fixture(root)
            manager.restore(manifest(), layout, receipt)
            self.assertIn(("mount", "/dev/sda1", root), runner.commands)
            self.assertEqual(manager.layout, layout)
            self.assertFalse(any(c[0].startswith("mkfs") or c[0] in {"sgdisk", "parted", "pvcreate", "mkswap"} for c in runner.commands))

    def test_resume_rejects_changed_filesystem_before_mount(self):
        with tempfile.TemporaryDirectory() as root:
            runner, manager, layout, receipt = self.fixture(root)
            receipt["uuids"]["/dev/sda1"] = "previous-filesystem"
            with self.assertRaisesRegex(StorageError, "filesystem identity"):
                manager.restore(manifest(), layout, receipt)
            self.assertFalse(any(c[0] == "mount" for c in runner.commands))
            self.assertEqual(manager.owned_mounts, [])

    def test_resume_rejects_changed_disk_before_mount(self):
        with tempfile.TemporaryDirectory() as root:
            runner, manager, layout, receipt = self.fixture(root)
            receipt["signatures"]["/dev/sda"][0] = "different-disk"
            with self.assertRaisesRegex(StorageError, "disk identity"):
                manager.restore(manifest(), layout, receipt)
            self.assertFalse(any(c[0] == "mount" for c in runner.commands))

    def test_resume_rejects_unrelated_mount_without_claiming_it(self):
        with tempfile.TemporaryDirectory() as root:
            runner, manager, layout, receipt = self.fixture(root)
            runner.respond("findmnt", "/dev/unrelated\n")
            with self.assertRaisesRegex(StorageError, "Unrelated filesystem"):
                manager.restore(manifest(), layout, receipt)
            manager.cleanup()
            self.assertFalse(any(c[0] in {"mount", "umount"} for c in runner.commands))

    def test_resume_rejects_missing_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            runner, manager, layout, receipt = self.fixture(root)
            with self.assertRaisesRegex(StorageError, "saved disk"):
                manager.restore(manifest(), layout, {})
            self.assertEqual(runner.commands, [])

    def test_no_format_stack_creation_is_rejected_before_commands(self):
        for kind in ("luks", "lvm"):
            m = manifest()
            m.storage.mode = "manual"
            m.storage.partitions = [Partition(device="/dev/sda1", mountpoint="/", format=False), Partition(device="/dev/sda2", mountpoint="/boot")]
            setattr(m.storage, kind, True)
            runner = FakeRunner()
            with self.subTest(kind=kind), self.assertRaisesRegex(StorageError, "Preserved root"):
                StorageManager(runner).prepare(m, dry_run=True)
            self.assertEqual(runner.commands, [])

    def test_cleanup_retains_failed_resources_for_retry(self):
        runner = FakeRunner()
        runner.failures["umount"] = 1
        manager = StorageManager(runner)
        manager.owned_mounts = [Path("/fake-target")]
        with self.assertRaises(StorageError):
            manager.cleanup()
        self.assertEqual(manager.owned_mounts, [Path("/fake-target")])
        runner.failures.clear()
        manager.cleanup()
        self.assertEqual(manager.owned_mounts, [])

    def test_cancelled_cleanup_uses_uncancelled_executor(self):
        import threading
        from unittest.mock import patch
        from gentoo_installerd.runner import CommandResult, SubprocessRunner
        cancel = threading.Event()
        cancel.set()
        runner = SubprocessRunner(cancel=cancel)
        manager = StorageManager(runner)
        manager.owned_mounts = [Path("/fake-target")]
        with patch.object(runner, "run_cleanup", return_value=CommandResult((), 0, "")) as cleanup:
            manager.cleanup()
        cleanup.assert_called_once_with(("umount", "-R", "/fake-target"))
        self.assertTrue(cancel.is_set())
        self.assertEqual(manager.owned_mounts, [])

    def test_resume_reopens_luks_by_saved_identity_without_formatting(self):
        from gentoo_installerd.storage import LayoutEntry
        with tempfile.TemporaryDirectory() as root:
            runner, manager, _, receipt = self.fixture(root)
            raw = json.loads(lsblk())
            raw["blockdevices"][0]["children"][0]["children"] = [{"name": "gentoo-root", "path": "/dev/mapper/gentoo-root", "type": "crypt", "size": 900000, "mountpoints": []}]
            runner.responses = []
            runner.respond("lsblk", json.dumps(raw))
            runner.respond("blkid -s UUID -o value /dev/sda1", "luks-uuid\n")
            runner.respond("blkid -s UUID -o value /dev/mapper/gentoo-root", "root-uuid\n")
            runner.failures["cryptsetup status"] = 4
            receipt["uuids"] = {"/dev/mapper/gentoo-root": "root-uuid"}
            receipt["stack"] = {"luks_device": "/dev/sda1", "luks_uuid": "luks-uuid", "mapper": "gentoo-root"}
            m = manifest()
            m.storage.luks = True
            m.storage.luks_passphrase = "passphrase"
            manager.restore(m, [LayoutEntry("/dev/mapper/gentoo-root", "/", "ext4")], receipt)
            self.assertIn(("cryptsetup", "open", "--key-file=-", "/dev/sda1", "gentoo-root"), runner.commands)
            self.assertFalse(any("luksFormat" in c for c in runner.commands))
            self.assertEqual(manager.owned_maps, ["gentoo-root"])

    def test_resume_refuses_changed_volume_group_before_activation(self):
        with tempfile.TemporaryDirectory() as root:
            runner, manager, layout, receipt = self.fixture(root)
            receipt["stack"] = {"vg": "gentoo", "vg_uuid": "original-vg", "pv_device": "/dev/sda1"}
            runner.respond("pvs", "unrelated-vg\n")
            with self.assertRaisesRegex(StorageError, "volume group identity"):
                manager.restore(manifest(), layout, receipt)
            self.assertFalse(any(c[0] in {"vgchange", "mount", "pvcreate"} for c in runner.commands))

    def test_guided_luks_has_separate_unencrypted_boot(self):
        m = manifest()
        m.storage.luks = True
        m.storage.luks_passphrase = "passphrase"
        runner = FakeRunner()
        layout = StorageManager(runner).prepare(m, dry_run=True)
        boot = next(e for e in layout if e.mountpoint == "/boot")
        self.assertEqual(boot.device, "/dev/sda2")
        self.assertEqual(boot.filesystem, "ext4")
        self.assertIn(("mkfs.ext4", "-F", "/dev/sda2"), runner.commands)
        self.assertFalse(any("luksFormat" in c and c[-1] == boot.device for c in runner.commands))

    def test_symlink_root_and_mountpoints_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "root-link").symlink_to("/", target_is_directory=True)
            runner = FakeRunner()
            with self.assertRaisesRegex(StorageError, "non-root"):
                StorageManager(runner, base / "root-link").prepare(manifest(), dry_run=True)
            self.assertEqual(runner.commands, [])
            target = base / "target"
            target.mkdir()
            (target / "boot-real").mkdir()
            (target / "boot").symlink_to("boot-real", target_is_directory=True)
            with self.assertRaisesRegex(StorageError, "symlink"):
                StorageManager(runner, target).target_path("/boot")


if __name__ == "__main__":
    unittest.main()
