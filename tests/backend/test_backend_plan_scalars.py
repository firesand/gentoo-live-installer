"""Choices the hardware plan resolves (HardwarePlan.scalars, optional_services) must reach the installed system."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gentoo_installerd.configure import GRUB_EFI_FILES, grub_efi_target
from gentoo_installerd.hwplan import plan as plan_hardware
from gentoo_installerd.storage import LayoutEntry

from tests.backend.target_helpers import FakePlan, ScriptedRunner, configurer, read, target_manifest
from tests.backend.test_target_boot import UEFI_LAYOUT, grub_commands, populate_boot
from tests.hardware.builders import profile


def scalars(**values) -> FakePlan:
    plan = FakePlan()
    plan.scalars = values  # type: ignore[attr-defined]
    return plan


class RemovableFallbackFromThePlan(unittest.TestCase):
    def install(self, m, plan, prepare=None):
        runner = ScriptedRunner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            populate_boot(root)
            if prepare:
                prepare(root)
            c = configurer(root, runner)
            c.notify = lambda message: None
            c.configure_bootloader(m, "/dev/sda3", "/dev/sda1", plan)
        return grub_commands(runner)

    def test_the_resolved_choice_in_the_plans_scalars_is_used(self) -> None:
        for choice, expected in (("never", False), ("always", True), ("if-missing", True)):
            with self.subTest(choice=choice):
                commands = self.install(target_manifest(), scalars(removable_fallback=choice))
                self.assertEqual(any("--removable" in command for command in commands), expected)

    def test_if_missing_keeps_a_fallback_file_that_is_already_there(self) -> None:
        def windows(root: Path) -> None:
            (root / "efi/EFI/BOOT").mkdir(parents=True)
            (root / "efi/EFI/BOOT/BOOTX64.EFI").write_bytes(b"windows")

        self.assertFalse(any("--removable" in c for c in self.install(target_manifest(), scalars(removable_fallback="if-missing"), windows)))

    def test_a_32_bit_uefi_looks_for_its_own_fallback_file(self) -> None:
        def other_system(root: Path) -> None:
            (root / "efi/EFI/BOOT").mkdir(parents=True)
            (root / "efi/EFI/BOOT/BOOTIA32.EFI").write_bytes(b"other")

        plan = scalars(removable_fallback="if-missing", grub_target="i386-efi")
        self.assertFalse(any("--removable" in c for c in self.install(target_manifest(), plan, other_system)))
        # a 64-bit file on a 32-bit machine is not its fallback
        def wrong_file(root: Path) -> None:
            (root / "efi/EFI/BOOT").mkdir(parents=True)
            (root / "efi/EFI/BOOT/BOOTX64.EFI").write_bytes(b"x64")

        self.assertTrue(any("--removable" in c for c in self.install(target_manifest(), plan, wrong_file)))

    def test_the_plan_of_a_bios_booted_session_asks_for_the_fallback_even_on_a_shared_esp(self) -> None:
        machine = profile(firmware="bios")
        m = target_manifest(storage={"mode": "manual", "partitions": []}, hardware={"removable_fallback": "auto"})
        # booted in BIOS mode the firmware cannot register an entry, so the fallback path is the only way to boot
        self.assertEqual(plan_hardware(m, machine).scalars["removable_fallback"], "always")


class GrubTargetFromThePlan(unittest.TestCase):
    def test_32_bit_uefi_machines_get_an_i386_efi_grub(self) -> None:
        runner = ScriptedRunner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            populate_boot(root)
            c = configurer(root, runner)
            c.configure_bootloader(target_manifest(), "/dev/sda3", "/dev/sda1", scalars(grub_target="i386-efi", removable_fallback="always"))
        self.assertEqual(grub_commands(runner), [
            "grub-install --target=i386-efi --efi-directory=/efi --bootloader-id=Gentoo",
            "grub-install --target=i386-efi --efi-directory=/efi --removable --no-nvram",
            "grub-mkconfig -o /boot/grub/grub.cfg"])

    def test_the_target_comes_from_the_machine(self) -> None:
        sixty_four = plan_hardware(target_manifest(), profile())
        self.assertEqual(sixty_four.scalars["grub_target"], "x86_64-efi")
        self.assertEqual(grub_efi_target(sixty_four), "x86_64-efi")

    def test_anything_unknown_falls_back_to_64_bit_and_bios_is_untouched(self) -> None:
        self.assertEqual(grub_efi_target(None), "x86_64-efi")
        self.assertEqual(grub_efi_target(scalars(grub_target="i386-pc")), "x86_64-efi")
        runner = ScriptedRunner()
        with tempfile.TemporaryDirectory() as temporary:
            c = configurer(Path(temporary), runner)
            populate_boot(Path(temporary))
            c.configure_bootloader(target_manifest(storage={"firmware": "bios"}), "/dev/sda2", None, scalars(grub_target="i386-pc"))
        self.assertEqual(grub_commands(runner), ["grub-install --target=i386-pc /dev/sda", "grub-mkconfig -o /boot/grub/grub.cfg"])

    def test_the_finished_system_is_checked_for_the_files_of_its_own_target(self) -> None:
        for target, installed, removable in (("x86_64-efi", "grubx64.efi", "BOOTX64.EFI"), ("i386-efi", "grubia32.efi", "BOOTIA32.EFI")):
            for place in (f"EFI/Gentoo/{installed}", f"EFI/BOOT/{removable}"):
                with self.subTest(target=target, place=place), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    populate_boot(root)
                    (root / "boot/grub").mkdir()
                    (root / "boot/grub/grub.cfg").write_text("linux /vmlinuz root=UUID=1234\n")
                    (root / "etc").mkdir()
                    (root / "etc/fstab").write_text("UUID=1234 / ext4 defaults 0 1\n")
                    c = configurer(root)
                    plan = scalars(grub_target=target)
                    self.assertIn("No GRUB EFI image", " ".join(c.verify_boot_setup(target_manifest(), "/dev/sda3", root_uuid="1234", plan=plan)))
                    (root / "efi" / place).parent.mkdir(parents=True, exist_ok=True)
                    (root / "efi" / place).write_bytes(b"grub")
                    self.assertEqual(c.verify_boot_setup(target_manifest(), "/dev/sda3", root_uuid="1234", plan=plan), [])

    def test_the_two_file_tables_agree_with_the_targets(self) -> None:
        self.assertEqual(set(GRUB_EFI_FILES), {"x86_64-efi", "i386-efi"})


class DiscardFromThePlan(unittest.TestCase):
    def initramfs(self, m, plan) -> tuple[str, str]:
        runner = ScriptedRunner()
        runner.respond("blkid -s UUID -o value /dev/mapper/gentoo-root", "11111111-2222-3333-4444-555555555555\n")
        runner.respond("blkid -s UUID -o value /dev/sda3", "66666666-7777-8888-9999-000000000000\n")
        with tempfile.TemporaryDirectory() as temporary:
            c = configurer(temporary, runner)
            c.configure_initramfs(m, "/dev/mapper/gentoo-root", "/dev/sda3", plan)
            return read(Path(temporary), "etc/kernel/cmdline"), read(Path(temporary), "etc/default/grub") if (Path(temporary) / "etc/default/grub").exists() else ""

    def encrypted(self, **system):
        return target_manifest(storage={"luks": True, "luks_passphrase": "long enough passphrase"}, system=system)

    def test_trim_through_the_encryption_is_only_switched_on_when_the_plan_says_so(self) -> None:
        on, _ = self.initramfs(self.encrypted(), scalars(luks_allow_discards=True))
        self.assertIn("rd.luks.allow-discards=66666666-7777-8888-9999-000000000000", on)
        off, _ = self.initramfs(self.encrypted(), scalars(luks_allow_discards=False))
        self.assertNotIn("discard", off)
        none, _ = self.initramfs(self.encrypted(), None)
        self.assertNotIn("discard", none)

    def test_a_systemd_initramfs_takes_the_option_through_rd_luks_options(self) -> None:
        line, grub = self.initramfs(self.encrypted(init="systemd", stage_variant="amd64-systemd"), scalars(luks_allow_discards=True))
        self.assertIn("rd.luks.options=66666666-7777-8888-9999-000000000000=discard", line)
        self.assertNotIn("allow-discards", line)
        self.assertIn("rd.luks.options=66666666-7777-8888-9999-000000000000=discard", grub)

    def test_the_real_plan_asks_for_it_only_with_the_users_consent(self) -> None:
        m = self.encrypted()
        self.assertFalse(plan_hardware(m, profile()).scalars["luks_allow_discards"])
        m.hardware.luks_discard = True
        self.assertTrue(plan_hardware(m, profile()).scalars["luks_allow_discards"])

    def test_btrfs_gets_asynchronous_trim_on_a_solid_state_disk_through_the_installer(self) -> None:
        from gentoo_installerd.install import Installer
        from tests.backend.helpers import FakeInspector
        from tests.backend.target_helpers import ScriptedRunner as Scripted

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            runner = Scripted()
            runner.respond("blkid -s UUID -o value", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n")
            m = target_manifest(storage={"filesystem": "btrfs"})
            installer = Installer(m, runner=runner, target_root=base / "target", state_dir=base / "state", inspector=FakeInspector(), emit=lambda event: None)
            installer.storage.layout = [LayoutEntry("/dev/sda3", "/", "btrfs", options="subvol=@"), LayoutEntry("/dev/sda1", "/efi", "vfat")]
            for discard in (True, False):
                installer.hardware_plan = scalars(btrfs_discard_async=discard)
                installer._write_fstab()
                text = (base / "target/etc/fstab").read_text()
                self.assertEqual("subvol=@,discard=async" in text, discard, text)
                self.assertNotIn("umask=0077,discard", text)
            installer._log_file.close()


class ServicesFromThePlan(unittest.TestCase):
    def test_services_of_best_effort_packages_are_enabled_when_the_package_is_there(self) -> None:
        plan = FakePlan(services=[("cronie", "default")])
        plan.optional_services = [("tlp", "default"), ("thermald", "default")]  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc/init.d").mkdir(parents=True)
            for name in ("cronie", "tlp"):
                (root / "etc/init.d" / name).write_text("")
            runner = ScriptedRunner()
            c = configurer(root, runner)
            c.notify = lambda message: None
            warnings = c.configure_services(target_manifest(system={"desktop": "minimal"}, options={"cron": False, "logger": False, "chrony": False, "bluetooth": False, "printing": False}),
                                            plan, phase="desktop")
        lines = runner.lines()
        self.assertTrue(any(line.endswith("rc-update add cronie default") for line in lines))
        self.assertTrue(any(line.endswith("rc-update add tlp default") for line in lines))
        self.assertFalse(any("thermald" in line for line in lines))  # its package is missing: skipped, not an error
        self.assertEqual(warnings, ["Service thermald was not enabled because its package is not installed"])

    def test_systemd_timers_and_sockets_are_found_by_their_own_name(self) -> None:
        plan = FakePlan(services=[("fstrim.timer", "default")])
        plan.optional_services = [("spice-vdagentd.socket", "default"), ("vmtoolsd", "default"), ("missing.timer", "default")]  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "usr/lib/systemd/system").mkdir(parents=True)
            for unit in ("fstrim.timer", "spice-vdagentd.socket", "vmtoolsd.service"):
                (root / "usr/lib/systemd/system" / unit).write_text("")
            runner = ScriptedRunner()
            c = configurer(root, runner)
            c.notify = lambda message: None
            warnings = c.configure_services(target_manifest(system={"init": "systemd", "stage_variant": "amd64-systemd", "desktop": "minimal"},
                                                            options={"cron": False, "logger": False, "chrony": False, "bluetooth": False, "printing": False}), plan, phase="desktop")
        enabled = [line.rsplit(" ", 1)[-1] for line in runner.lines() if "systemctl enable" in line]
        self.assertEqual(enabled, ["fstrim.timer", "spice-vdagentd.socket", "vmtoolsd"])
        self.assertEqual(warnings, ["Service missing.timer was not enabled because its package is not installed"])


if __name__ == "__main__":
    unittest.main()
