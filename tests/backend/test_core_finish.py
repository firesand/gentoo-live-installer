"""CORE-11 (validation warnings) and CORE-08 / B24 (first steps, boot check, FIRST-STEPS.txt)."""

from __future__ import annotations

import stat
import unittest

from gentoo_installer import first_steps
from gentoo_installerd.install import Installer
from gentoo_installerd.runner import DryRunRunner
from gentoo_installerd.stages import finish

from tests.backend.helpers import FakeInspector, manifest
from tests.backend.target_helpers import FakePlan, ScriptedRunner
from tests.backend.test_w3_target_fakes import UUID, StageMixin


class Base(StageMixin, unittest.TestCase):
    def prepare(self, *, init: str = "openrc", plan: object | None = None) -> Installer:
        runner = ScriptedRunner()
        runner.respond("blkid -s UUID -o value /dev/sda3", UUID + "\n")
        installer = self.make(runner=runner, plan=plan)
        installer.manifest.system.init = init
        (self.target / "boot/grub").mkdir(parents=True)
        (self.target / "boot/vmlinuz-6.12.0").write_bytes(b"kernel")
        (self.target / "boot/initramfs-6.12.0.img").write_bytes(b"initramfs")
        (self.target / "etc/portage").mkdir(parents=True)
        (self.target / "etc/portage/make.conf").write_text("x\n")
        (self.target / "etc/fstab").write_text(f"UUID={UUID} / ext4 defaults 0 1\n")
        (self.target / "etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n")
        (self.target / "boot/grub/grub.cfg").write_text("menuentry 'Gentoo' {\n linux /vmlinuz-6.12.0 root=UUID=x ro\n}\n")
        (self.target / "efi/EFI/Gentoo").mkdir(parents=True)
        (self.target / "efi/EFI/Gentoo/grubx64.efi").write_bytes(b"grub")
        runner.respond("chroot " + str(self.target) + " getcap", "/bin/ping cap_net_raw=ep\n")
        return installer


class ValidationWarningTests(Base):
    def test_a_healthy_system_adds_no_warning(self) -> None:
        installer = self.prepare()
        installer._validate()
        self.assertEqual(installer.warnings, [])

    def test_an_empty_machine_id_warns_on_openrc_but_not_on_systemd(self) -> None:
        installer = self.prepare()
        (self.target / "etc/machine-id").write_text("")
        installer._validate()
        self.assertTrue(any("machine ID" in w for w in installer.warnings))
        installer = self.prepare(init="systemd")
        (self.target / "etc/machine-id").write_text("")
        installer._validate()
        self.assertEqual(installer.warnings, [])

    def test_a_pc_that_will_refuse_the_system_says_so_from_the_bootloader_on(self) -> None:
        installer = self.prepare(plan=FakePlan(scalars={"secure_boot_refuses": True}))
        installer.hardware_plan = installer._plan()
        installer._stage_done("stage3")
        self.assertEqual(installer.warnings, [])
        installer._stage_done("bootloader")
        installer._stage_done("bootloader")  # a resume reports it again
        self.assertEqual(installer.warnings, [finish.SECURE_BOOT_FIRST_START])

    def test_a_pc_that_starts_the_system_hears_nothing_about_secure_boot(self) -> None:
        installer = self.prepare(plan=FakePlan())
        installer.hardware_plan = installer._plan()
        installer._stage_done("bootloader")
        self.assertEqual(installer.warnings, [])

    def test_lost_file_capabilities_warn(self) -> None:
        installer = self.prepare()
        self.runner.outputs.clear()
        self.runner.respond("blkid -s UUID -o value /dev/sda3", UUID + "\n")
        self.runner.respond("chroot " + str(self.target) + " stat", "755\n")
        installer._validate()
        self.assertTrue(any("file capabilities" in w for w in installer.warnings))

    def test_a_setuid_ping_is_fine(self) -> None:
        installer = self.prepare()
        self.runner.outputs.clear()
        self.runner.respond("blkid -s UUID -o value /dev/sda3", UUID + "\n")
        self.runner.respond("chroot " + str(self.target) + " stat", "4755\n")
        installer._validate()
        self.assertEqual(installer.warnings, [])

    def test_missing_early_microcode_warns_only_when_microcode_was_planned(self) -> None:
        plan = FakePlan()
        plan.scalars = {"microcode": "intel"}
        installer = self.prepare(plan=plan)
        self.runner.respond("chroot " + str(self.target) + " lsinitrd /boot", "Image: x\nArguments: -f\n")
        installer._validate()
        self.assertTrue(any("early CPU microcode" in w for w in installer.warnings))

    def test_early_microcode_present_is_fine(self) -> None:
        plan = FakePlan()
        plan.scalars = {"microcode": "intel"}
        installer = self.prepare(plan=plan)
        self.runner.respond("chroot " + str(self.target) + " lsinitrd /boot", "Early CPIO image\nkernel/x86/microcode/GenuineIntel.bin\n")
        installer._validate()
        self.assertEqual(installer.warnings, [])

    def test_no_microcode_planned_means_no_microcode_check(self) -> None:
        plan = FakePlan()
        plan.scalars = {"microcode": "none"}
        installer = self.prepare(plan=plan)
        self.runner.respond("chroot " + str(self.target) + " lsinitrd /boot", "nothing useful\n")
        installer._validate()
        self.assertEqual(installer.warnings, [])


class FirstStepsFileTests(Base):
    def test_the_file_is_written_into_the_home_with_the_right_owner_and_mode(self) -> None:
        installer = self.prepare()
        user = installer.manifest.accounts.username
        (self.target / "home" / user).mkdir(parents=True)
        self.runner.respond("chroot " + str(self.target) + " eselect news count new", "3\n")
        installer._validate()
        path = self.target / "home" / user / "FIRST-STEPS.txt"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.assertIn(f"chroot {self.target} chown {user}: /home/{user}/FIRST-STEPS.txt", self.runner.lines())
        text = path.read_text()
        for needle in ("emerge --sync", "--update --deep --newuse @world", "eclean-dist -d", "@module-rebuild", "eclean-kernel", "grub-mkconfig",
                       "make.conf", "package.use", "Libera.Chat", "Boot check passed: GRUB found the kernel and initramfs", "3 unread Gentoo news"):
            self.assertIn(needle, text)
        self.assertEqual(installer.news_unread, 3)

    def test_hardware_first_steps_are_added_and_limited_to_six(self) -> None:
        plan = FakePlan()
        plan.first_steps = [f"Line {n}" for n in range(9)]
        installer = self.prepare(plan=plan)
        (self.target / "home" / installer.manifest.accounts.username).mkdir(parents=True)
        installer._validate()
        text = (self.target / "home" / installer.manifest.accounts.username / "FIRST-STEPS.txt").read_text()
        self.assertIn("- Line 5", text)
        self.assertNotIn("Line 6", text)

    def test_unknown_news_and_a_missing_home_hide_only_their_lines(self) -> None:
        installer = self.prepare()
        installer._validate()  # no /home/<user>: nothing written, no failure
        self.assertEqual(installer.warnings, [])
        self.assertIsNone(installer.news_unread)
        self.assertEqual(installer._finish_data(True)["boot_check"], installer.boot_check)
        self.assertNotIn("news_unread", installer._finish_data(True))

    def test_the_completed_event_carries_the_finish_data(self) -> None:
        installer = self.prepare()
        installer.boot_check = "Boot check passed: GRUB found the kernel"
        installer.news_unread = 0
        data = installer._finish_data(True)
        login = data.pop("login")
        self.assertEqual(data, {"first_steps": [], "boot_check": "Boot check passed: GRUB found the kernel", "news_unread": 0})
        self.assertEqual(login["username"], installer.manifest.accounts.username)
        self.assertEqual(login["hostname"], installer.manifest.accounts.hostname)
        self.assertEqual(set(login), {"username", "hostname", "privilege", "ssh", "minimal", "bootloader"})
        self.assertEqual(installer._finish_data(False), {})


class TextBuilderTests(unittest.TestCase):
    def test_every_command_and_the_ssh_and_wifi_lines(self) -> None:
        steps = first_steps.FirstSteps(username="ana", hostname="box", ssh=True, minimal=True, privilege="doas")
        text = first_steps.first_steps_text(steps)
        self.assertIn("ssh ana@box", text)
        self.assertIn("doas nmtui", text)
        self.assertIn("doas eclean-dist -d", text)
        self.assertNotIn("Boot check", text)
        self.assertNotIn("news", text)

    def test_dry_run_installer_has_no_finish_side_effects(self) -> None:
        installer = Installer(manifest(), runner=DryRunRunner(), dry_run=True, inspector=FakeInspector(), emit=lambda e: None)
        self.assertEqual(finish.run_checks(installer), [])
