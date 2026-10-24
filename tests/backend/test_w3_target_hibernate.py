"""TGT-2: 'Allow hibernation' reserves a swap partition, so the installed system has to look for the image there."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gentoo_installerd import hwplan
from gentoo_installerd.install import InstallError
from gentoo_installerd.storage import LayoutEntry

from tests.backend.target_helpers import ScriptedRunner, configurer, read, target_manifest
from tests.backend.test_w3_target_fakes import SWAP_UUID, UUID, StageMixin

HIBERNATE = {"hibernate": True}
LAYOUT = [LayoutEntry("/dev/sda3", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat"), LayoutEntry("/dev/sda2", "", "swap")]


class ResumeArgumentTests(unittest.TestCase):
    def configure(self, m, resume_device="/dev/sda2", plan=None):
        runner = ScriptedRunner()
        runner.respond("blkid -s UUID -o value /dev/sda3", UUID + "\n")
        runner.respond("blkid -s UUID -o value /dev/sda2", SWAP_UUID + "\n")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        configurer(root, runner).configure_initramfs(m, "/dev/sda3", None, plan, resume_device=resume_device)
        return root, runner

    def test_the_swap_uuid_goes_to_the_boot_configuration(self) -> None:
        root, _ = self.configure(target_manifest(hardware=HIBERNATE))
        self.assertEqual(read(root, "etc/kernel/cmdline"), f"root=UUID={UUID} rw resume=UUID={SWAP_UUID}\n")
        self.assertIn(f"GRUB_CMDLINE_LINUX=resume=UUID={SWAP_UUID}", read(root, "etc/default/grub"))

    def test_the_resume_module_is_asked_for_explicitly(self) -> None:
        root, _ = self.configure(target_manifest(hardware=HIBERNATE))
        self.assertIn('add_dracutmodules+=" resume "', read(root, "etc/dracut.conf.d/gentoo-installer.conf"))

    def test_resume_comes_before_the_plan_and_the_extra_arguments(self) -> None:
        from tests.backend.target_helpers import FakePlan

        root, _ = self.configure(target_manifest(hardware=HIBERNATE, system={"kernel_cmdline_extra": "quiet"}), plan=FakePlan(kernel_cmdline=["nvidia-drm.modeset=1"]))
        self.assertEqual(read(root, "etc/kernel/cmdline"), f"root=UUID={UUID} rw resume=UUID={SWAP_UUID} nvidia-drm.modeset=1 quiet\n")

    def test_without_a_resume_device_nothing_changes(self) -> None:
        root, runner = self.configure(target_manifest(), resume_device=None)
        self.assertEqual(read(root, "etc/kernel/cmdline"), f"root=UUID={UUID} rw\n")
        self.assertNotIn("resume", read(root, "etc/dracut.conf.d/gentoo-installer.conf"))
        self.assertFalse(any("/dev/sda2" in line for line in runner.lines()))

    def test_a_preview_names_the_device_because_it_has_no_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configurer(temporary, dry_run=True).configure_initramfs(target_manifest(hardware=HIBERNATE), "/dev/sda3", None, None, resume_device="/dev/sda2")
            # nothing is written in a preview; the call must simply not fail
            self.assertFalse(Path(temporary, "etc/kernel/cmdline").exists())

    def test_a_swap_partition_without_a_uuid_is_an_error(self) -> None:
        from gentoo_installerd.configure import ConfigurationError

        runner = ScriptedRunner()
        runner.respond("blkid -s UUID -o value /dev/sda3", UUID + "\n")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ConfigurationError, "swap partition has no valid UUID"):
                configurer(temporary, runner).configure_initramfs(target_manifest(hardware=HIBERNATE), "/dev/sda3", None, None, resume_device="/dev/sda2")


class KernelStageResumeTests(StageMixin, unittest.TestCase):
    def prepare(self, m=None, layout=None):
        runner = ScriptedRunner()
        runner.respond("blkid -s UUID -o value /dev/sda3", UUID + "\n")
        runner.respond("blkid -s UUID -o value /dev/sda2", SWAP_UUID + "\n")
        return self.make(m or target_manifest(hardware=HIBERNATE), runner, layout=list(layout if layout is not None else LAYOUT))

    def test_the_kernel_stage_finds_the_swap_partition_the_plan_reserved(self) -> None:
        installer = self.prepare()
        installer._kernel()
        self.assertEqual(read(self.target, "etc/kernel/cmdline"), f"root=UUID={UUID} rw resume=UUID={SWAP_UUID}\n")

    def test_a_system_that_does_not_hibernate_gets_no_resume_argument(self) -> None:
        installer = self.prepare(target_manifest())
        installer._kernel()
        self.assertEqual(read(self.target, "etc/kernel/cmdline"), f"root=UUID={UUID} rw\n")

    def test_hibernation_without_a_swap_partition_stops_at_the_kernel_stage_with_a_clear_message(self) -> None:
        installer = self.prepare(layout=[LayoutEntry("/dev/sda3", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat")])
        with self.assertRaisesRegex(InstallError, "Hibernation is on, but this installation has no swap partition"):
            installer._kernel()
        self.assertFalse(any("dracut" in line for line in self.chroot_lines()))

    def test_a_swap_file_cannot_be_resumed_from(self) -> None:
        layout = [LayoutEntry("/dev/sda3", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat"), LayoutEntry("/mnt/swapfile", "", "swap", True, source="/swapfile")]
        installer = self.prepare(layout=layout)
        with self.assertRaisesRegex(InstallError, "no swap partition"):
            installer._kernel()

    def test_the_first_swap_partition_of_a_manual_layout_is_used(self) -> None:
        layout = [*LAYOUT, LayoutEntry("/dev/sdb1", "", "swap")]
        installer = self.prepare(layout=layout)
        installer._kernel()
        self.assertIn(f"resume=UUID={SWAP_UUID}", read(self.target, "etc/kernel/cmdline"))


def codes(m, machine=None, severity=None):
    return {issue.code for issue in hwplan.validate_plan(m, machine) if severity is None or issue.severity == severity}


class HibernationValidationTests(unittest.TestCase):
    """The preflight must refuse a hibernation request that no swap partition can serve, before any disk is touched."""

    def machine(self, ram_gib=16, partitions=()):
        from gentoo_installer.hardware import PartitionInfo
        from tests.hardware.builders import disk, profile

        listed = [PartitionInfo(name=name, size=size, fstype="swap") for name, size in partitions]
        return profile(ram_gib=ram_gib, disks=(disk("/dev/sda", partitions=listed),))

    def test_unknown_memory_does_not_hide_a_missing_swap_partition(self) -> None:
        m = target_manifest(hardware=HIBERNATE, storage={"swap_mib": 0})
        self.assertIn("hw.hibernate_swap_small", codes(m, {}, "error"))

    def test_manual_layout_without_a_swap_row_cannot_hibernate(self) -> None:
        from gentoo_installer.manifest import Partition

        m = target_manifest(hardware=HIBERNATE, storage={"mode": "manual", "swap_mib": 20480, "partitions": [Partition("/dev/sda3", "/", "ext4", format=True), Partition("/dev/sda1", "/efi", "vfat", format=True)]})
        found = [issue for issue in hwplan.validate_plan(m, None) if issue.code == "hw.hibernate_swap_small"]
        self.assertEqual(len(found), 1)
        self.assertIn("no swap partition", found[0].message)

    def test_manual_layout_with_a_swap_row_is_checked_against_its_real_size(self) -> None:
        from gentoo_installer.manifest import Partition

        rows = [Partition("/dev/sda3", "/", "ext4", format=True), Partition("/dev/sda2", "", "swap", format=True)]
        m = target_manifest(hardware=HIBERNATE, storage={"mode": "manual", "partitions": rows})
        small = self.machine(16, [("sda2", 4 * 1024**3)])
        big = self.machine(16, [("sda2", 24 * 1024**3)])
        self.assertIn("hw.hibernate_swap_small", codes(m, small, "error"))
        self.assertNotIn("hw.hibernate_swap_small", codes(m, big, "error"))

    def test_manual_swap_on_a_volume_the_probe_does_not_list_is_not_second_guessed(self) -> None:
        from gentoo_installer.manifest import Partition

        rows = [Partition("/dev/sda3", "/", "ext4", format=True), Partition("/dev/mapper/vg0-swap", "", "swap", format=True)]
        m = target_manifest(hardware=HIBERNATE, storage={"mode": "manual", "partitions": rows})
        self.assertNotIn("hw.hibernate_swap_small", codes(m, self.machine(16), "error"))

    def test_guided_installs_keep_their_rules(self) -> None:
        ok = target_manifest(hardware=HIBERNATE, storage={"swap_mib": 20 * 1024})
        self.assertNotIn("hw.hibernate_swap_small", codes(ok, self.machine(16), "error"))
        small = target_manifest(hardware=HIBERNATE, storage={"swap_mib": 8 * 1024})
        self.assertIn("hw.hibernate_swap_small", codes(small, self.machine(16), "error"))


if __name__ == "__main__":
    unittest.main()
