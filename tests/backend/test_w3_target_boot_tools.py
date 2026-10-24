"""TGT-1: systemd-boot and UKI need USE flags on a package the stage3 already has.

``emerge --noreplace`` skips an installed package, so the flags the installer writes to package.use
(boot, kernel-install, ukify) never applied. The fake runner (PortageRunner) models that: only a
rebuild installs bootctl, the systemd-boot image and ukify, and running a missing tool fails with 127.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from gentoo_installerd.hwplan import HardwarePlan
from gentoo_installerd.install import InstallError, Installer
from gentoo_installerd.runner import RunnerError

from tests.backend.target_helpers import ScriptedRunner, target_manifest
from tests.backend.test_w3_target_fakes import UUID, PortageRunner, StageMixin

SYSTEMD = {"init": "systemd", "stage_variant": "amd64-systemd", "profile": "default/linux/amd64/23.0/systemd"}


class BootToolsTests(StageMixin, unittest.TestCase):
    def prepare(self, m, *, installed=("sys-apps/systemd-utils",), files=None, plan=None) -> Installer:
        installer = self.make(m, runner_factory=lambda target: PortageRunner(target, installed, files), plan=plan)
        self.runner.respond("blkid -s UUID -o value /dev/sda3", UUID + "\n")
        self.runner.respond("blkid -s UUID -o value /dev/sda1", "1234-ABCD\n")
        if "sys-apps/systemd" in installed:
            (self.target / "usr/bin").mkdir(parents=True)
            (self.target / "usr/bin/bootctl").write_text("x")  # systemd always installs bootctl, but not the boot images
        # the earlier stages: make.conf and package.use as the installer writes them
        installer.configurer.configure_portage(installer.manifest, installer._plan())
        return installer

    def run_stages(self, installer: Installer) -> None:
        installer._kernel()
        (self.target / "boot").mkdir(exist_ok=True)
        (self.target / "boot/vmlinuz-6.12.0").write_bytes(b"kernel")
        (self.target / "boot/initramfs-6.12.0.img").write_bytes(b"initramfs")
        installer._bootloader()

    def rebuilds(self) -> list[str]:
        return [line for line in self.chroot_lines() if line.startswith("emerge") and "--newuse" in line]

    def test_systemd_boot_on_openrc_finds_bootctl_when_the_bootloader_stage_runs(self) -> None:
        installer = self.prepare(target_manifest(system={"bootloader": "systemd-boot"}))
        self.run_stages(installer)
        self.assertEqual(self.rebuilds(), ["emerge --update --newuse --oneshot sys-apps/systemd-utils"])
        self.assertIn("bootctl --path=/efi install", self.chroot_lines())
        self.assertTrue((self.target / "efi/loader/entries/gentoo.conf").is_file())

    def test_the_rebuild_comes_before_the_kernel_and_the_initramfs(self) -> None:
        installer = self.prepare(target_manifest(system={"bootloader": "systemd-boot"}))
        installer._kernel()
        lines = self.chroot_lines()
        rebuild = lines.index("emerge --update --newuse --oneshot sys-apps/systemd-utils")
        kernel = next(i for i, line in enumerate(lines) if "sys-kernel/installkernel" in line)
        dracut = next(i for i, line in enumerate(lines) if line.startswith("dracut"))
        self.assertLess(rebuild, kernel)
        self.assertLess(rebuild, dracut)

    def test_a_unified_kernel_image_finds_ukify_with_any_bootloader(self) -> None:
        for bootloader in ("grub", "efi-stub", "systemd-boot"):
            with self.subTest(bootloader=bootloader):
                installer = self.prepare(target_manifest(system={"bootloader": bootloader, "uki": True}))
                self.runner.respond("lsblk --noheadings --output PARTN", "1\n")
                self.runner.respond("lsblk --noheadings --output PKNAME", "sda\n")
                self.run_stages(installer)
                self.assertEqual(self.rebuilds(), ["emerge --update --newuse --oneshot sys-apps/systemd-utils"])
                self.assertTrue(any(line.startswith("ukify build") for line in self.chroot_lines()))

    def test_systemd_init_rebuilds_systemd_itself(self) -> None:
        installer = self.prepare(target_manifest(system={**SYSTEMD, "bootloader": "systemd-boot"}), installed=("sys-apps/systemd",))
        self.run_stages(installer)
        self.assertEqual(self.rebuilds(), ["emerge --update --newuse --oneshot sys-apps/systemd"])
        self.assertTrue((self.target / "usr/lib/systemd/boot/efi/systemd-bootx64.efi").is_file())
        self.assertIn("bootctl --path=/efi install", self.chroot_lines())

    def test_encryption_and_systemd_boot_on_systemd_rebuild_it_only_once(self) -> None:
        m = target_manifest(system={**SYSTEMD, "bootloader": "systemd-boot"}, storage={"luks": True, "luks_passphrase": "long-enough-passphrase"})
        installer = self.prepare(m, installed=("sys-apps/systemd",))
        installer.storage.layout[0] = type(installer.storage.layout[0])("/dev/mapper/gentoo-root", "/", "ext4")
        installer.storage._stack["luks_device"] = "/dev/sda3"
        self.runner.respond("blkid -s UUID -o value /dev/mapper/gentoo-root", UUID + "\n")
        installer._kernel()
        self.assertEqual(self.rebuilds(), ["emerge --update --newuse --oneshot sys-apps/systemd"])
        for name in ("usr/lib/systemd/systemd-cryptsetup", "usr/lib/systemd/boot/efi/systemd-bootx64.efi"):
            self.assertTrue((self.target / name).is_file(), name)

    def test_a_plain_grub_install_rebuilds_nothing(self) -> None:
        installer = self.prepare(target_manifest())
        self.run_stages(installer)
        self.assertEqual(self.rebuilds(), [])
        self.assertEqual(self.runner.rebuilds, [])

    def test_openrc_with_encryption_and_grub_rebuilds_nothing(self) -> None:
        installer = self.prepare(target_manifest(storage={"luks": True, "luks_passphrase": "long-enough-passphrase"}))
        installer.storage._stack["luks_device"] = "/dev/sda3"
        self.runner.respond("blkid -s UUID -o value /dev/sda3", "12345678-1234-1234-1234-123456789abc\n")
        installer._kernel()
        self.assertEqual(self.rebuilds(), [])

    def test_a_rebuild_that_leaves_the_tool_out_stops_the_install_before_the_long_stages(self) -> None:
        installer = self.prepare(target_manifest(system={"bootloader": "systemd-boot"}), files={})
        with self.assertRaisesRegex(InstallError, "bootctl is missing after rebuilding sys-apps/systemd-utils"):
            installer._kernel()
        lines = self.chroot_lines()
        self.assertFalse(any("sys-kernel/installkernel" in line for line in lines))
        self.assertFalse(any(line.startswith("dracut") for line in lines))

    def test_a_missing_systemd_boot_image_is_named(self) -> None:
        installer = self.prepare(target_manifest(system={**SYSTEMD, "bootloader": "systemd-boot"}), installed=("sys-apps/systemd",), files={"boot": ["usr/bin/bootctl"]})
        with self.assertRaisesRegex(InstallError, "systemd-bootx64.efi"):
            installer._kernel()

    def test_a_missing_ukify_is_named(self) -> None:
        installer = self.prepare(target_manifest(system={"bootloader": "efi-stub", "uki": True}), files={"boot": ["usr/bin/bootctl"]})
        with self.assertRaisesRegex(InstallError, "ukify is missing after rebuilding sys-apps/systemd-utils"):
            installer._kernel()

    def test_the_old_behaviour_is_what_the_model_calls_a_failure(self) -> None:
        """Without the rebuild the model behaves like the real target: bootctl is not found (exit 127)."""
        installer = self.prepare(target_manifest(system={"bootloader": "systemd-boot"}))
        with self.assertRaises(RunnerError) as context:
            installer.configurer.chroot(("bootctl", "--path=/efi", "install"))
        self.assertEqual(context.exception.returncode, 127)

    def test_32_bit_firmware_needs_the_ia32_image(self) -> None:
        plan = HardwarePlan(scalars={"grub_target": "i386-efi"})
        installer = self.prepare(target_manifest(system={"bootloader": "systemd-boot"}), files={"boot": ["usr/bin/bootctl", "usr/lib/systemd/boot/efi/systemd-bootx64.efi"]}, plan=plan)
        with self.assertRaisesRegex(InstallError, "systemd-bootia32.efi"):
            installer._kernel()

    def test_the_existing_cryptsetup_message_is_kept(self) -> None:
        m = target_manifest(system=SYSTEMD, storage={"luks": True, "luks_passphrase": "long-enough-passphrase"})
        installer = self.prepare(m, installed=("sys-apps/systemd",), files={})
        installer.storage._stack["luks_device"] = "/dev/sda3"
        self.runner.respond("blkid -s UUID -o value /dev/sda3", "12345678-1234-1234-1234-123456789abc\n")
        with self.assertRaisesRegex(InstallError, "systemd-cryptsetup is missing after enabling systemd.cryptsetup."):
            installer._kernel()


class ResolveGateTests(StageMixin, unittest.TestCase):
    """The pretend run in the portage stage must see the rebuild too: --noreplace never looks at an installed package."""

    def test_the_gate_resolves_the_rebuild_of_the_boot_tools_package(self) -> None:
        installer = self.make(target_manifest(system={"bootloader": "systemd-boot", "uki": True}))
        installer._portage()
        pretends = [line for line in self.chroot_lines() if line.startswith("emerge") and "--pretend" in line]
        self.assertEqual(len(pretends), 2)
        self.assertIn("--noreplace", pretends[0])
        self.assertEqual(pretends[1], "emerge --pretend --oneshot --update --newuse --verbose=n sys-apps/systemd-utils")

    def test_a_rebuild_that_cannot_be_resolved_stops_the_install_in_the_portage_stage(self) -> None:
        from tests.backend.test_target_packages import EmergeDiagnosisTests

        runner = ScriptedRunner()
        runner.script("*--newuse", ("fail", EmergeDiagnosisTests.USE_CHANGES))
        installer = self.make(target_manifest(system={"bootloader": "systemd-boot"}), runner)
        with self.assertRaisesRegex(InstallError, "cannot resolve.*qtpositioning"):
            installer._portage()

    def test_a_plain_install_has_one_pretend_run(self) -> None:
        installer = self.make(target_manifest())
        installer._portage()
        pretends = [line for line in self.chroot_lines() if line.startswith("emerge") and "--pretend" in line]
        self.assertEqual(len(pretends), 1)


class DryRunTests(unittest.TestCase):
    def test_a_preview_names_the_rebuild_and_checks_nothing(self) -> None:
        import tempfile

        from gentoo_installerd.runner import DryRunRunner
        from tests.backend.helpers import FakeInspector

        with tempfile.TemporaryDirectory() as temporary:
            runner = DryRunRunner()
            installer = Installer(target_manifest(system={"bootloader": "systemd-boot"}), runner=runner, target_root=Path(temporary, "t"), state_dir=Path(temporary, "s"),
                                  dry_run=True, inspector=FakeInspector())
            from gentoo_installerd.storage import LayoutEntry

            installer.storage.layout = [LayoutEntry("/dev/sda3", "/", "ext4")]
            installer._kernel()
            self.assertIn(("chroot", str(installer.target_root), "emerge", "--update", "--newuse", "--oneshot", "sys-apps/systemd-utils"), runner.commands)


if __name__ == "__main__":
    unittest.main()
