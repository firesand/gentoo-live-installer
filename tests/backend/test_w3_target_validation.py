"""BOOT-1: a failed final check must not be reported as a disk that boots."""

from __future__ import annotations

import unittest
from pathlib import Path

from gentoo_installerd.install import InstallError, Installer
from gentoo_installerd.runner import DryRunRunner, RunnerError

from tests.backend.helpers import FakeInspector, manifest
from tests.backend.target_helpers import ScriptedRunner
from tests.backend.test_w3_target_fakes import UUID, StageMixin


class ValidationClearsTheBootableFlagTests(StageMixin, unittest.TestCase):
    def prepare(self) -> Installer:
        runner = ScriptedRunner()
        runner.respond("blkid -s UUID -o value /dev/sda3", UUID + "\n")
        installer = self.make(runner=runner)
        installer._bootable = True  # what the finished bootloader stage sets
        (self.target / "boot/grub").mkdir(parents=True)
        (self.target / "boot/vmlinuz-6.12.0").write_bytes(b"kernel")
        (self.target / "boot/initramfs-6.12.0.img").write_bytes(b"initramfs")
        (self.target / "etc/portage").mkdir(parents=True)
        (self.target / "etc/portage/make.conf").write_text("x\n")
        (self.target / "etc/fstab").write_text(f"UUID={UUID} / ext4 defaults 0 1\n")
        (self.target / "boot/grub/grub.cfg").write_text("menuentry 'Gentoo' {\n linux /vmlinuz-6.12.0 root=UUID=x ro\n}\n")
        (self.target / "efi/EFI/Gentoo").mkdir(parents=True)
        (self.target / "efi/EFI/Gentoo/grubx64.efi").write_bytes(b"grub")
        return installer

    def test_a_system_that_passes_stays_bootable(self) -> None:
        installer = self.prepare()
        installer._validate()
        self.assertTrue(installer._bootable)

    def test_a_missing_kernel_means_the_disk_does_not_boot(self) -> None:
        installer = self.prepare()
        (self.target / "boot/vmlinuz-6.12.0").unlink()
        with self.assertRaisesRegex(InstallError, "No kernel image was installed"):
            installer._validate()
        self.assertFalse(installer._bootable)

    def test_a_grub_menu_without_a_kernel_entry_means_the_disk_does_not_boot(self) -> None:
        installer = self.prepare()
        (self.target / "boot/grub/grub.cfg").write_text("set timeout=5\n")
        with self.assertRaisesRegex(InstallError, "grub.cfg has no menu entry"):
            installer._validate()
        self.assertFalse(installer._bootable)

    def test_an_initramfs_without_the_crypt_module_means_the_disk_does_not_boot(self) -> None:
        installer = self.prepare()
        installer.manifest.storage.luks = True
        (self.target / "boot/grub/grub.cfg").write_text("menuentry 'x' {\n linux /vmlinuz-6 root=UUID=x ro rd.luks.uuid=u\n}\n")
        self.runner.respond("chroot", "dracut modules:\nbash\nrootfs-block\n")
        with self.assertRaisesRegex(InstallError, "lacks the crypt module"):
            installer._validate()
        self.assertFalse(installer._bootable)

    def test_a_failing_chroot_check_is_not_reported_as_bootable_either(self) -> None:
        for command in ("test -s /etc/fstab", "test -s /etc/portage/make.conf", "getent passwd gentoo"):
            with self.subTest(command=command):
                installer = self.prepare()
                self.runner.script(f"chroot {self.target} {command}", ("fail", "", 1))
                with self.assertRaises(RunnerError):
                    installer._validate()
                self.assertFalse(installer._bootable)


class ValidationEventsTests(unittest.TestCase):
    """The error and completed events a client acts on (the GUI hides 'Start over' while bootable is true)."""

    def run_installer(self, tmp_path: Path):
        events: list = []
        installer = Installer(manifest(), runner=DryRunRunner(), target_root=tmp_path / "target", state_dir=tmp_path / "state", dry_run=True,
                              inspector=FakeInspector(), emit=events.append)
        installer.configurer.verify_boot_setup = lambda *args, **kwargs: ["grub.cfg has no menu entry that loads a kernel"]  # type: ignore[method-assign]
        self.assertFalse(installer.run())
        return events

    def test_both_events_say_the_disk_does_not_boot_when_the_final_check_fails(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            events = self.run_installer(Path(temporary))
        error = next(event for event in events if event.type == "error")
        completed = next(event for event in events if event.type == "completed")
        self.assertEqual(error.stage, "validation")
        self.assertIn("failed its final checks", error.error)
        self.assertIs(error.data["bootable"], False)
        self.assertIs(completed.data["bootable"], False)

    def test_a_failing_desktop_still_leaves_a_bootable_disk(self) -> None:
        import tempfile

        events: list = []
        with tempfile.TemporaryDirectory() as temporary:
            installer = Installer(manifest(), runner=DryRunRunner(), target_root=Path(temporary, "target"), state_dir=Path(temporary, "state"),
                                  dry_run=True, inspector=FakeInspector(), emit=events.append)

            def desktop() -> None:
                raise InstallError("the desktop did not build")

            installer._desktop = desktop  # type: ignore[method-assign]
            self.assertFalse(installer.run())
        self.assertIs(next(event for event in events if event.type == "completed").data["bootable"], True)


if __name__ == "__main__":
    unittest.main()
