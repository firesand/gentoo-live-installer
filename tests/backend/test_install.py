from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_installer.manifest import Partition
from gentoo_installerd.install import CheckpointStore, InstallError, Installer
from gentoo_installerd.runner import DryRunRunner, InstallCancelled
from gentoo_installerd.storage import StorageManager

from tests.backend.helpers import FakeInspector, FakeRunner, manifest


class CheckpointTests(unittest.TestCase):
    def test_checkpoint_round_trip_requires_artifact_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "checkpoints.json")
            store = CheckpointStore(path, "fingerprint")
            store.complete("stage3")
            self.assertTrue(store.is_complete("stage3", lambda: True))
            self.assertFalse(store.is_complete("stage3"))
            self.assertFalse(store.is_complete("stage3", lambda: False))
            loaded = CheckpointStore(path, "fingerprint")
            loaded.load()
            self.assertIn("stage3", loaded.completed)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_mismatched_checkpoint_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "checkpoints.json")
            CheckpointStore(path, "one").complete("storage")
            with self.assertRaises(InstallError):
                CheckpointStore(path, "two").load()


class InstallerFlowTests(unittest.TestCase):
    def test_full_dry_run_emits_every_stage_and_mutates_no_paths(self) -> None:
        m = manifest()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target, state, cache = base / "target", base / "state", base / "cache"
            events = []
            runner = DryRunRunner()
            installer = Installer(m, runner=runner, target_root=target, state_dir=state, cache_dir=cache, dry_run=True, inspector=FakeInspector(), emit=events.append)
            self.assertTrue(installer.run())
            stages = {event.stage for event in events if event.type == "stage" and event.data.get("status") == "completed"}
            self.assertEqual(stages, {"preflight", "storage", "stage3", "mounts", "repository", "portage", "kernel", "target", "base", "bootloader", "desktop", "validation", "cleanup"})
            self.assertEqual([event.data.get("success") for event in events if event.type == "completed"], [True])
            self.assertFalse(target.exists())
            self.assertFalse(state.exists())
            self.assertFalse(cache.exists())
            self.assertIn(("sgdisk", "--zap-all", "/dev/sda"), runner.commands)
            self.assertTrue(any(command[:1] == ("chroot",) for command in runner.commands))

    def test_dry_run_contains_no_password_or_luks_secrets_in_events(self) -> None:
        m = manifest()
        m.storage.luks = True
        m.storage.luks_passphrase = "luks-secret-value"
        m.network.wifi_ssid = "network"
        m.network.wifi_password = "wifi-secret-value"
        events = []
        with tempfile.TemporaryDirectory() as temporary:
            installer = Installer(m, target_root=Path(temporary, "target"), state_dir=Path(temporary, "state"), dry_run=True, inspector=FakeInspector(), emit=events.append)
            self.assertTrue(installer.run())
        rendered = "\n".join(event.to_json() for event in events)
        for secret in (m.accounts.password, m.accounts.root_password, m.storage.luks_passphrase, m.network.wifi_password):
            self.assertNotIn(secret, rendered)

    def test_step_failure_never_emits_success(self) -> None:
        runner = FakeRunner()
        runner.failures["chroot"] = 1
        events = []
        with tempfile.TemporaryDirectory() as temporary:
            installer = Installer(manifest(), runner=runner, target_root=Path(temporary, "target"), state_dir=Path(temporary, "state"), dry_run=True, inspector=FakeInspector(), emit=events.append)
            self.assertFalse(installer.run())
        self.assertTrue(any(event.type == "error" for event in events))
        self.assertEqual([event.data.get("success") for event in events if event.type == "completed"], [False])

    def test_a_cleanup_failure_after_a_finished_install_is_a_warning_not_a_failure(self) -> None:
        runner = FakeRunner()
        runner.failures["umount"] = 1
        storage = StorageManager(runner)
        storage.owned_mounts = [Path("/mnt/gentoo")]
        events = []
        with tempfile.TemporaryDirectory() as temporary:
            installer = Installer(manifest(), runner=runner, storage=storage, target_root=Path(temporary, "target"), state_dir=Path(temporary, "state"), dry_run=True, inspector=FakeInspector(), emit=events.append)
            self.assertTrue(installer.run())
        completed = [event for event in events if event.type == "completed"]
        self.assertEqual(len(completed), 1)
        self.assertTrue(completed[0].data["success"])
        self.assertFalse([event for event in events if event.type == "error"])
        warning = next(event for event in events if event.type == "status" and event.data.get("warning"))
        self.assertEqual(warning.stage, "cleanup")
        self.assertIn("The installation itself is complete", warning.message)
        self.assertIn("umount -R", warning.message)
        self.assertEqual(completed[0].data["warnings"], [warning.message])

    def test_a_cleanup_failure_after_a_failed_install_is_reported_as_an_error(self) -> None:
        runner = FakeRunner()
        runner.failures["umount"] = 1
        runner.failures["chroot"] = 1
        storage = StorageManager(runner)
        storage.owned_mounts = [Path("/mnt/gentoo")]
        events = []
        with tempfile.TemporaryDirectory() as temporary:
            installer = Installer(manifest(), runner=runner, storage=storage, target_root=Path(temporary, "target"), state_dir=Path(temporary, "state"), dry_run=True, inspector=FakeInspector(), emit=events.append)
            self.assertFalse(installer.run())
        errors = [event for event in events if event.type == "error"]
        self.assertEqual([event.data["code"] for event in errors], ["install.failed", "install.cleanup_failed"])
        self.assertEqual(errors[-1].stage, "cleanup")
        completed = [event for event in events if event.type == "completed"]
        self.assertFalse(completed[0].data["success"])
        self.assertEqual(completed[0].data["warnings"], [])

    def test_cancelled_before_start_is_not_success(self) -> None:
        cancel = threading.Event()
        cancel.set()
        events = []
        with tempfile.TemporaryDirectory() as temporary:
            installer = Installer(manifest(), target_root=Path(temporary, "target"), state_dir=Path(temporary, "state"), dry_run=True, inspector=FakeInspector(), cancel=cancel, emit=events.append)
            self.assertFalse(installer.run())
        self.assertTrue(any(event.error == "cancelled" for event in events))

    def test_manual_kernel_stops_with_actionable_error(self) -> None:
        m = manifest()
        m.system.kernel = "manual"
        events = []
        with tempfile.TemporaryDirectory() as temporary:
            installer = Installer(m, target_root=Path(temporary, "target"), state_dir=Path(temporary, "state"), dry_run=True, inspector=FakeInspector(), emit=events.append)
            self.assertFalse(installer.run())
        self.assertIn("chroot", next(event.error for event in events if event.type == "error"))

    def test_storage_utilities_are_installed_before_initramfs_generation(self) -> None:
        from gentoo_installerd.storage import LayoutEntry
        for option, package in (("luks", "sys-fs/cryptsetup"), ("lvm", "sys-fs/lvm2"),
                                ("raid_level", "sys-fs/mdadm")):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as temporary:
                m = manifest()
                setattr(m.storage, option, "1" if option == "raid_level" else True)
                if option == "raid_level":
                    # The installer does not create arrays: it installs onto an assembled /dev/md device in manual mode.
                    m.storage.mode, m.storage.device, m.storage.firmware = "manual", "/dev/md0", "bios"
                    m.storage.partitions = [Partition("/dev/md0", "/", "ext4")]
                if option == "luks":
                    m.storage.luks_passphrase = "test-only-passphrase"
                runner = DryRunRunner()
                installer = Installer(m, runner=runner, target_root=Path(temporary, "target"),
                                      state_dir=Path(temporary, "state"), dry_run=True,
                                      inspector=FakeInspector())
                installer.storage.layout = [LayoutEntry("/dev/mapper/gentoo-root", "/", "ext4")]
                installer.storage._stack["luks_device"] = "/dev/sda3"
                installer._kernel()
                tool_index = next(i for i, c in enumerate(runner.commands) if package in c)
                kernel_index = next(i for i, c in enumerate(runner.commands) if "sys-kernel/installkernel" in c)
                initramfs_index = next(i for i, c in enumerate(runner.commands) if "--regenerate-all" in c)
                self.assertLess(tool_index, kernel_index)
                self.assertLess(tool_index, initramfs_index)

    def test_encrypted_systemd_updates_cryptsetup_helper_before_initramfs(self) -> None:
        from gentoo_installerd.storage import LayoutEntry
        for init, luks in (("systemd", True), ("systemd", False), ("openrc", True)):
            with self.subTest(init=init, luks=luks), tempfile.TemporaryDirectory() as temporary:
                m = manifest()
                m.system.init = init
                if init == "systemd":
                    m.system.stage_variant = "amd64-systemd"
                    m.system.profile = "default/linux/amd64/23.0/systemd"
                m.storage.luks = luks
                if luks:
                    m.storage.luks_passphrase = "test-only-passphrase"
                runner = DryRunRunner()
                installer = Installer(m, runner=runner, target_root=Path(temporary, "target"),
                                      state_dir=Path(temporary, "state"), dry_run=True,
                                      inspector=FakeInspector())
                installer.storage.layout = [LayoutEntry("/dev/mapper/gentoo-root" if luks else "/dev/sda3", "/", "ext4")]
                if luks:
                    installer.storage._stack["luks_device"] = "/dev/sda3"
                installer._kernel()
                update = ("chroot", str(installer.target_root), "emerge", "--update", "--newuse",
                          "--oneshot", "sys-apps/systemd")
                self.assertEqual(update in runner.commands, init == "systemd" and luks)
                if update in runner.commands:
                    self.assertLess(runner.commands.index(update),
                                    next(i for i, command in enumerate(runner.commands) if "sys-kernel/installkernel" in command))
                    self.assertLess(runner.commands.index(update),
                                    next(i for i, command in enumerate(runner.commands) if "--regenerate-all" in command))

    def test_encrypted_systemd_rejects_missing_cryptsetup_helper(self) -> None:
        from gentoo_installerd.storage import LayoutEntry
        m = manifest()
        m.system.init = "systemd"
        m.system.stage_variant = "amd64-systemd"
        m.system.profile = "default/linux/amd64/23.0/systemd"
        m.storage.luks = True
        m.storage.luks_passphrase = "test-only-passphrase"
        with tempfile.TemporaryDirectory() as temporary:
            installer = Installer(m, runner=DryRunRunner(), target_root=Path(temporary, "target"),
                                  state_dir=Path(temporary, "state"), dry_run=True,
                                  inspector=FakeInspector())
            installer.storage.layout = [LayoutEntry("/dev/mapper/gentoo-root", "/", "ext4")]
            installer.storage._stack["luks_device"] = "/dev/sda3"
            installer.dry_run = False
            with patch.object(installer.configurer, "configure_initramfs"):
                with self.assertRaisesRegex(InstallError, "systemd-cryptsetup is missing"):
                    installer._kernel()
                helper = installer.target_root / "usr/lib/systemd/systemd-cryptsetup"
                helper.parent.mkdir(parents=True)
                helper.touch()
                installer._kernel()

    def test_persistent_manifest_is_redacted_and_private(self) -> None:
        m = manifest()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary, "state")
            installer = Installer(m, runner=FakeRunner(), target_root=Path(temporary, "target"), state_dir=state, inspector=FakeInspector())
            installer._log_file.close()
            text = (state / "manifest.json").read_text()
            self.assertNotIn("user-password", text)
            self.assertNotIn("root-password", text)
            self.assertIn("<redacted>", text)
            self.assertEqual((state / "manifest.json").stat().st_mode & 0o777, 0o600)

    def test_microcode_precedes_kernel_hooks_and_explicit_initramfs(self) -> None:
        from gentoo_installerd.storage import LayoutEntry
        for firmware, microcode, expected in (
            (False, True, {"sys-kernel/linux-firmware", "sys-firmware/intel-microcode"}),
            (True, False, {"sys-kernel/linux-firmware"}),
            (False, False, set()),
        ):
            with self.subTest(firmware=firmware, microcode=microcode), tempfile.TemporaryDirectory() as temporary:
                m = manifest()
                m.options.firmware, m.options.microcode = firmware, microcode
                runner = DryRunRunner()
                installer = Installer(m, runner=runner, target_root=Path(temporary, "target"),
                                      state_dir=Path(temporary, "state"), dry_run=True,
                                      inspector=FakeInspector())
                installer.storage.layout = [LayoutEntry("/dev/sda3", "/", "ext4")]
                installer._kernel()
                kernel_index = next(i for i, c in enumerate(runner.commands) if "sys-kernel/installkernel" in c)
                initramfs_index = next(i for i, c in enumerate(runner.commands) if "--regenerate-all" in c)
                for atom in {"sys-kernel/linux-firmware", "sys-firmware/intel-microcode"}:
                    indices = [i for i, command in enumerate(runner.commands) if atom in command]
                    self.assertEqual(len(indices), int(atom in expected))
                    if indices:
                        # With the distribution kernel, in its own emerge: earlier, their virtual/dist-kernel pulls gentoo-kernel from source.
                        self.assertEqual(indices[0], kernel_index)
                        self.assertIn("sys-kernel/gentoo-kernel-bin", runner.commands[indices[0]])
                        self.assertLess(indices[0], initramfs_index)


class ResumeRegressionTests(unittest.TestCase):
    def test_missing_storage_checkpoint_never_repartitions(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = FakeRunner()
            installer = Installer(manifest(), runner=runner, target_root=root / "target", state_dir=root / "state", resume=True, inspector=FakeInspector())
            with patch.object(installer, "_preflight"), patch.object(installer.storage, "prepare") as prepare:
                self.assertFalse(installer.run())
                prepare.assert_not_called()

    def test_resume_failure_never_falls_back_to_prepare(self):
        from unittest.mock import patch
        from gentoo_installerd.storage import StorageError
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer = Installer(manifest(), runner=FakeRunner(), target_root=root / "target", state_dir=root / "state", resume=True, inspector=FakeInspector())
            installer.checkpoints.complete("storage")
            with patch.object(installer, "_preflight"), patch.object(installer, "_verify_storage", side_effect=StorageError("UUID mismatch")), patch.object(installer.storage, "prepare") as prepare:
                self.assertFalse(installer.run())
                prepare.assert_not_called()

    def test_cleanup_can_be_retried_after_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner = FakeRunner()
            runner.failures["umount"] = 1
            installer = Installer(manifest(), runner=runner, state_dir=Path(temporary), dry_run=True, inspector=FakeInspector())
            installer.storage.owned_mounts = [Path("/fake-target")]
            with self.assertRaises(Exception):
                installer._cleanup()
            self.assertFalse(installer._cleanup_done)
            runner.failures.clear()
            installer._cleanup()
            self.assertTrue(installer._cleanup_done)
            self.assertEqual(installer.storage.owned_mounts, [])


class ResumeIntegrationTests(unittest.TestCase):
    def test_resume_after_unmount_restores_without_preparing_storage(self):
        from unittest.mock import patch
        from tests.backend.helpers import lsblk
        from gentoo_installerd.storage import LayoutEntry
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = FakeRunner()
            runner.respond("lsblk", lsblk())
            runner.respond("blkid -s UUID -o value /dev/sda1", "saved-root")
            installer = Installer(manifest(), runner=runner, target_root=root / "target", state_dir=root / "state", resume=True, inspector=FakeInspector())
            installer.checkpoints.save_layout([LayoutEntry("/dev/sda1", "/", "ext4")], {"signatures": {"/dev/sda": ["disk-serial", "wwn-disk", 100000000000]}, "uuids": {"/dev/sda1": "saved-root"}, "stack": {}})
            installer.checkpoints.complete("storage")
            with patch.object(installer.storage, "prepare") as prepare, patch.object(installer, "_stage3", side_effect=InstallError("stop after restore")):
                self.assertFalse(installer.run())
                prepare.assert_not_called()
            self.assertIn(("mount", "/dev/sda1", str(root / "target")), runner.commands)
            self.assertIn(("umount", "-R", str(root / "target")), runner.commands)
            self.assertFalse(any(c[0].startswith("mkfs") or c[0] == "sgdisk" for c in runner.commands))

    def test_manual_kernel_can_resume_after_user_installs_artifacts(self):
        from gentoo_installerd.storage import LayoutEntry
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "boot").mkdir()
            (root / "boot/vmlinuz-test").write_bytes(b"kernel")
            (root / "boot/initramfs-test.img").write_bytes(b"initramfs")
            m = manifest()
            m.system.kernel = "manual"
            installer = Installer(m, runner=FakeRunner(), target_root=root, dry_run=True, resume=True, inspector=FakeInspector())
            installer.storage.layout = [LayoutEntry("/dev/sda3", "/", "ext4")]
            installer._kernel()
            (root / "boot/initramfs-test.img").unlink()
            with self.assertRaises(InstallError):
                installer._kernel()

if __name__ == "__main__":
    unittest.main()
