"""TGT-6: the console font of a dense panel has to reach the initramfs (the passphrase prompt) as well as the installed tty."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gentoo_installerd.storage import LayoutEntry

from tests.backend.target_helpers import ScriptedRunner, configurer, read, target_manifest
from tests.backend.test_w3_target_fakes import UUID, StageMixin

FONT = {"console_font": "ter-132b"}
LUKS = {"luks": True, "luks_passphrase": "long-enough-passphrase"}
GERMAN = {"keymap": "de-latin1-nodeadkeys", "keyboard_layout": "de", "keyboard_variant": "nodeadkeys"}


class InitramfsFontTests(unittest.TestCase):
    LUKS_UUID = "12345678-1234-1234-1234-123456789abc"

    def configure(self, m):
        luks = m.storage.luks
        root_device = "/dev/mapper/gentoo-root" if luks else "/dev/sda3"
        runner = ScriptedRunner()
        runner.respond(f"blkid -s UUID -o value {root_device}", UUID + "\n")
        runner.respond("blkid -s UUID -o value /dev/sda3", self.LUKS_UUID + "\n" if luks else UUID + "\n")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        configurer(root, runner).configure_initramfs(m, root_device, "/dev/sda3" if luks else None, None)
        return root

    def test_the_font_goes_to_the_command_line_the_non_systemd_initramfs_reads(self) -> None:
        root = self.configure(target_manifest(system=FONT))
        self.assertEqual(read(root, "etc/kernel/cmdline"), f"root=UUID={UUID} rw rd.vconsole.font=ter-132b\n")
        self.assertEqual(read(root, "etc/default/grub"), "GRUB_CMDLINE_LINUX=rd.vconsole.font=ter-132b\n")

    def test_the_font_goes_to_vconsole_conf_which_the_systemd_initramfs_reads(self) -> None:
        root = self.configure(target_manifest(system=FONT))
        self.assertEqual(read(root, "etc/vconsole.conf"), "FONT=ter-132b\n")

    def test_the_passphrase_prompt_gets_the_keymap_and_the_font(self) -> None:
        root = self.configure(target_manifest(storage=LUKS, system={**FONT, **GERMAN}))
        cmdline = read(root, "etc/kernel/cmdline")
        self.assertIn("rd.vconsole.keymap=de-latin1-nodeadkeys", cmdline)
        self.assertIn("rd.vconsole.font=ter-132b", cmdline)
        self.assertEqual(read(root, "etc/vconsole.conf"), "KEYMAP=de-latin1-nodeadkeys\nFONT=ter-132b\n")

    def test_without_a_font_nothing_changes(self) -> None:
        root = self.configure(target_manifest())
        self.assertEqual(read(root, "etc/kernel/cmdline"), f"root=UUID={UUID} rw\n")
        self.assertFalse((root / "etc/vconsole.conf").exists())
        root = self.configure(target_manifest(storage=LUKS, system=GERMAN))
        self.assertEqual(read(root, "etc/vconsole.conf"), "KEYMAP=de-latin1-nodeadkeys\n")
        self.assertNotIn("rd.vconsole.font", read(root, "etc/kernel/cmdline"))


class KernelStageFontTests(StageMixin, unittest.TestCase):
    def test_terminus_is_installed_before_the_initramfs_is_built(self) -> None:
        runner = ScriptedRunner()
        runner.respond("blkid -s UUID -o value /dev/sda3", UUID + "\n")
        installer = self.make(target_manifest(system=FONT), runner, layout=[LayoutEntry("/dev/sda3", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat")])
        installer._kernel()
        lines = self.chroot_lines()
        font = next(i for i, line in enumerate(lines) if "media-fonts/terminus-font" in line)
        dracut = next(i for i, line in enumerate(lines) if line.startswith("dracut"))
        self.assertLess(font, dracut)
        self.assertIn("--noreplace", lines[font])

    def test_other_fonts_and_no_font_need_no_package(self) -> None:
        c = configurer(tempfile.gettempdir())
        for font in ("", "lat9w-16", "Lat2-Terminus16"):
            self.assertNotIn("media-fonts/terminus-font", c.kernel_stage_atoms(target_manifest(system={"console_font": font})), font)
        self.assertIn("media-fonts/terminus-font", c.kernel_stage_atoms(target_manifest(system=FONT)))

    def test_the_base_stage_still_lists_the_font_package(self) -> None:
        c = configurer(tempfile.gettempdir())
        self.assertIn("media-fonts/terminus-font", c.essential_atoms(target_manifest(system=FONT)))


if __name__ == "__main__":
    unittest.main()
