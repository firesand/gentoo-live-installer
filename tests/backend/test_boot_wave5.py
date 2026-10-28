"""Wave 5 boot items: the safe-graphics entry (B05), kernel files and tools (BOOT-03/04), serial console and no bootloader (BOOT-11)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from gentoo_installerd.configure import ESP_SYNC_HOOKS, render_esp_sync_hook
from gentoo_installerd.configure_boot_extra import SAFE_GRAPHICS_TOKENS, safe_tokens

from tests.backend.target_helpers import FakePlan, ScriptedRunner, configurer, read, target_manifest
from tests.backend.test_target_boot import populate_boot

SAFE = "nomodeset module_blacklist=nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm,nouveau"


class NvidiaPlan(FakePlan):
    def __init__(self, driver: str = "nvidia") -> None:
        super().__init__()
        self.scalars = {"gpu_driver": driver}
        self.kernel_cmdline = ["nvidia-drm.modeset=1", "nvidia-drm.fbdev=1"]


def boot_root(prepare=None) -> tempfile.TemporaryDirectory:
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    populate_boot(root)
    (root / "etc/kernel").mkdir(parents=True)
    (root / "etc/kernel/cmdline").write_text("root=UUID=abc rw nvidia-drm.modeset=1 nvidia-drm.fbdev=1 quiet\n")
    if prepare:
        prepare(root)
    return temporary


def runner_for_efi() -> ScriptedRunner:
    runner = ScriptedRunner()
    runner.respond("lsblk --noheadings --output PARTN", "1\n")
    runner.respond("lsblk --noheadings --output PKNAME", "sda\n")
    runner.respond("blkid", "1234-ABCD\n")
    return runner


class SafeGraphicsTests(unittest.TestCase):
    def configure(self, plan=None, **system):
        temporary = boot_root()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        runner = runner_for_efi()
        m = target_manifest(system=system)
        plan = NvidiaPlan() if plan is None else plan
        configurer(root, runner).configure_bootloader(m, "/dev/sda3", "/dev/sda1", plan)
        return root, runner, m

    def test_the_tokens_drop_the_mode_setting_switches_and_add_the_safe_ones(self) -> None:
        self.assertEqual(" ".join(safe_tokens("root=/dev/sda3 rw nvidia-drm.modeset=1 quiet")), "root=/dev/sda3 rw quiet " + SAFE)
        self.assertEqual(SAFE_GRAPHICS_TOKENS[0], "nomodeset")

    def test_grub_gets_a_submenu_script_with_the_safe_arguments(self) -> None:
        root, runner, _ = self.configure(bootloader="grub")
        script = read(root, "etc/grub.d/42_gentoo_safe_graphics")
        self.assertTrue((root / "etc/grub.d/42_gentoo_safe_graphics").stat().st_mode & 0o111)
        self.assertIn("submenu 'Gentoo Linux, safe graphics", script)
        self.assertIn("GRUB_CMDLINE_LINUX='quiet " + SAFE + "'", script)
        self.assertNotIn("nvidia-drm", script)
        self.assertIn("/etc/grub.d/10_linux", script)
        self.assertEqual(subprocess.run(["sh", "-n", str(root / "etc/grub.d/42_gentoo_safe_graphics")]).returncode, 0)
        self.assertTrue(any("grub-mkconfig" in line for line in runner.lines()))

    def test_systemd_boot_gets_a_second_entry_over_the_same_kernel_files(self) -> None:
        root, _, _ = self.configure(bootloader="systemd-boot")
        main, safe = read(root, "efi/loader/entries/gentoo.conf"), read(root, "efi/loader/entries/gentoo-safe.conf")
        self.assertIn("nvidia-drm.modeset=1", main)
        self.assertEqual(safe, "title Gentoo Linux (safe graphics)\nlinux /vmlinuz\ninitrd /initramfs\noptions root=UUID=abc rw quiet " + SAFE + "\n")
        self.assertIn("default gentoo.conf", read(root, "efi/loader/loader.conf"))

    def test_a_uki_install_builds_a_second_image_for_every_loader(self) -> None:
        for bootloader in ("systemd-boot", "efi-stub", "grub"):
            with self.subTest(bootloader=bootloader):
                root, runner, _ = self.configure(bootloader=bootloader, uki=True)
                builds = [line for line in runner.lines() if " ukify build" in line]
                self.assertEqual(len(builds), 2)
                self.assertIn("--output /efi/EFI/Linux/gentoo-safe.efi", builds[1])
                self.assertIn(SAFE, builds[1])
                self.assertNotIn("nvidia-drm", builds[1])
                self.assertIn("nvidia-drm.modeset=1", builds[0])
                if bootloader == "grub":
                    self.assertIn("gentoo-safe.efi", read(root, "etc/grub.d/40_custom"))
                if bootloader == "systemd-boot":
                    self.assertIn("linux /EFI/Linux/gentoo-safe.efi", read(root, "efi/loader/entries/gentoo-safe.conf"))

    def test_the_efi_stub_makes_the_safe_entry_first_so_gentoo_stays_the_default(self) -> None:
        _, runner, _ = self.configure(bootloader="efi-stub")
        entries = [line for line in runner.lines() if "efibootmgr --create" in line]
        self.assertEqual(len(entries), 2)
        self.assertIn("--label Gentoo (safe graphics)", entries[0])
        self.assertIn(SAFE, entries[0])
        self.assertIn("--label Gentoo --loader", entries[1])
        self.assertNotIn("nomodeset", entries[1])

    def test_the_hook_keeps_the_safe_image_current(self) -> None:
        root, _, _ = self.configure(bootloader="systemd-boot", uki=True)
        self.assertIn("SAFE=1", read(root, ESP_SYNC_HOOKS[0]))
        self.assertEqual(read(root, "etc/kernel/cmdline-safe").strip(), "root=UUID=abc rw quiet " + SAFE)

    def test_no_entry_without_the_proprietary_driver_or_when_switched_off(self) -> None:
        for plan, switch in ((NvidiaPlan("open"), True), (None, True), (NvidiaPlan(), False)):
            with self.subTest(driver=getattr(plan, "scalars", None), switch=switch):
                temporary = boot_root()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name)
                m = target_manifest(system={"bootloader": "systemd-boot"})
                m.hardware.safe_graphics_entry = switch
                configurer(root, runner_for_efi()).configure_bootloader(m, "/dev/sda3", "/dev/sda1", plan if plan is not None else FakePlan())
                self.assertFalse((root / "efi/loader/entries/gentoo-safe.conf").exists())
                self.assertFalse((root / "etc/kernel/cmdline-safe").exists())

    def test_the_hook_builds_and_signs_both_images_in_a_real_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for path in ("boot", "efi/EFI/Linux", "etc/kernel", "etc/secureboot", "bin"):
                (root / path).mkdir(parents=True)
            (root / "boot/vmlinuz-6.13.1").write_bytes(b"kernel")
            (root / "boot/initramfs-6.13.1.img").write_bytes(b"initramfs")
            (root / "etc/kernel/cmdline").write_text("root=UUID=abc rw nvidia-drm.modeset=1\n")
            (root / "etc/kernel/cmdline-safe").write_text("root=UUID=abc rw " + SAFE + "\n")
            tools = {
                "ukify": '#!/bin/sh\nwhile [ $# -gt 0 ]; do case "$1" in --output) out="$2"; shift;; --cmdline) c="$2"; shift;; esac; shift; done\necho "uki: $c" > "$out"\n',
                "sbverify": '#!/bin/sh\ngrep -q SIGNED "$3" 2>/dev/null\n',
                "sbsign": '#!/bin/sh\nwhile [ $# -gt 0 ]; do case "$1" in --output) out="$2"; shift;; esac; shift; done\n'
                          'src="${out%.signed}"; { cat "$src"; echo SIGNED; } > "$out"\n',
            }
            for name, body in tools.items():
                (root / "bin" / name).write_text(body)
                (root / "bin" / name).chmod(0o755)
            script = root / "hook.sh"
            script.write_text(render_esp_sync_hook("/efi", True, False, safe=True, sign=True))
            environment = {**os.environ, "ESP_SYNC_ROOT": str(root), "PATH": f"{root / 'bin'}:{os.environ['PATH']}"}
            result = subprocess.run(["sh", str(script), "6.13.1", "/boot/vmlinuz-6.13.1"], env=environment, capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            main, safe = (root / "efi/EFI/Linux/gentoo.efi").read_text(), (root / "efi/EFI/Linux/gentoo-safe.efi").read_text()
            self.assertIn("nvidia-drm.modeset=1", main)
            self.assertIn("SIGNED", main)
            self.assertIn(SAFE, safe)
            self.assertIn("SIGNED", safe)
            self.assertFalse(list((root / "efi/EFI/Linux").glob("*.new")))


class KernelFilesTests(unittest.TestCase):
    def prepare(self, **system):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "etc/portage").mkdir(parents=True)
        (root / "etc/portage/make.conf").write_text('COMMON_FLAGS="-O2"\n')
        runner = ScriptedRunner()
        m = target_manifest(system=system)
        c = configurer(root, runner)
        c.write_boot_portage_files(m)
        return root, m, c

    def test_distribution_kernels_get_dist_kernel_use_and_a_readme(self) -> None:
        for kernel in ("gentoo-kernel-bin", "gentoo-kernel"):
            with self.subTest(kernel=kernel):
                root, m, c = self.prepare(kernel=kernel)
                self.assertIn("*/* dist-kernel", read(root, "etc/portage/package.use/installer-boot"))
                self.assertIn(f"sys-kernel/{kernel}", read(root, "etc/kernel/README"))
                self.assertEqual(c.kernel_extra_atoms(m), ["sys-block/io-scheduler-udev-rules"])

    def test_nothing_is_written_for_a_manual_or_custom_kernel(self) -> None:
        for kernel in ("manual", "custom"):
            with self.subTest(kernel=kernel):
                root, m, c = self.prepare(kernel=kernel, kernel_config_snippet="CONFIG_X=y")
                self.assertFalse((root / "etc/portage/package.use/installer-boot").exists())
                self.assertFalse((root / "etc/kernel/README").exists())
                self.assertFalse((root / "etc/kernel/config.d/00-installer.config").exists())
                self.assertEqual(c.kernel_extra_atoms(m), [])

    def test_the_newest_kernel_series_accepts_the_testing_kernel_and_the_long_term_one_does_not(self) -> None:
        """The user's choice of 2026-10-28: the newest Gentoo kernel by default, so an update brings 7.3 as soon as Gentoo ships it."""
        for kernel in ("gentoo-kernel-bin", "gentoo-kernel"):
            with self.subTest(kernel=kernel):
                root, m, _ = self.prepare(kernel=kernel)
                self.assertEqual(m.system.kernel_series, "newest")
                keywords = read(root, "etc/portage/package.accept_keywords/installer-kernel")
                self.assertEqual([line for line in keywords.splitlines() if not line.startswith("#")],
                                 ["sys-kernel/gentoo-kernel-bin ~amd64", "sys-kernel/gentoo-kernel ~amd64", "virtual/dist-kernel ~amd64"])
                self.assertIn("newest Gentoo kernel", read(root, "etc/kernel/README"))
                root, _, _ = self.prepare(kernel=kernel, kernel_series="longterm")
                self.assertFalse((root / "etc/portage/package.accept_keywords/installer-kernel").exists())
                self.assertIn("long-term kernel", read(root, "etc/kernel/README"))
        root, _, _ = self.prepare(kernel="manual")
        self.assertFalse((root / "etc/portage/package.accept_keywords/installer-kernel").exists())

    def test_eclean_kernel_comes_with_its_option(self) -> None:
        m = target_manifest()
        m.options.eclean_kernel = True
        self.assertEqual(configurer(Path("/nonexistent")).kernel_extra_atoms(m), ["sys-block/io-scheduler-udev-rules", "app-admin/eclean-kernel"])
        self.assertIn("app-admin/eclean-kernel", configurer(Path("/nonexistent")).kernel_atoms(m))

    def test_the_config_snippet_goes_to_the_source_kernel_only(self) -> None:
        snippet = "CONFIG_HZ_1000=y\n# CONFIG_DEBUG_INFO is not set"
        root, _, _ = self.prepare(kernel="gentoo-kernel", kernel_config_snippet=snippet)
        self.assertIn(snippet, read(root, "etc/kernel/config.d/00-installer.config"))
        self.assertIn("00-installer.config", read(root, "etc/kernel/README"))
        root, _, _ = self.prepare(kernel="gentoo-kernel-bin", kernel_config_snippet=snippet)
        self.assertFalse((root / "etc/kernel/config.d/00-installer.config").exists())
        root, _, _ = self.prepare(kernel="gentoo-kernel")  # an empty box writes no file
        self.assertFalse((root / "etc/kernel/config.d/00-installer.config").exists())

    def test_secure_boot_is_persisted_for_updates(self) -> None:
        root, _, _ = self.prepare(kernel="gentoo-kernel", bootloader="systemd-boot", uki=True, secure_boot=True, secure_boot_mode="create")
        make_conf = read(root, "etc/portage/make.conf")
        for line in ('SECUREBOOT_SIGN_KEY=/etc/secureboot/gentoo.key', 'SECUREBOOT_SIGN_CERT=/etc/secureboot/gentoo.crt', "MODULES_SIGN_KEY=", "MODULES_SIGN_HASH="):
            self.assertIn(line, make_conf)
        self.assertIn("sys-kernel/gentoo-kernel secureboot modules-sign", read(root, "etc/portage/package.use/installer-boot"))
        self.assertEqual(read(root, "etc/kernel/uki.conf"),
                         "[UKI]\nSecureBootPrivateKey=/etc/secureboot/gentoo.key\nSecureBootCertificate=/etc/secureboot/gentoo.crt\n")
        root, _, _ = self.prepare(kernel="gentoo-kernel-bin", bootloader="systemd-boot", uki=True, secure_boot=True, secure_boot_mode="create")
        self.assertNotIn("MODULES_SIGN", read(root, "etc/portage/make.conf"))  # the binary kernel's modules are signed by Gentoo
        self.assertNotIn("modules-sign", read(root, "etc/portage/package.use/installer-boot"))


class CommandLineTests(unittest.TestCase):
    def initramfs(self, **system):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        runner = ScriptedRunner()
        runner.respond("blkid", "12345678-1234-1234-1234-123456789abc\n")
        m = target_manifest(system=system)
        c = configurer(root, runner)
        c.configure_initramfs(m, "/dev/sda3")
        return root, runner, m

    def test_extra_options_reach_the_kernel_command_line_and_grub(self) -> None:
        root, _, _ = self.initramfs(bootloader="grub", kernel_cmdline_extra="quiet nomodeset")
        self.assertTrue(read(root, "etc/kernel/cmdline").strip().endswith("quiet nomodeset"))
        self.assertIn("quiet nomodeset", read(root, "etc/default/grub"))

    def test_serial_console_adds_console_arguments_with_the_serial_port_last(self) -> None:
        root, _, _ = self.initramfs(bootloader="grub", serial_console="ttyS0,115200", kernel_cmdline_extra="quiet")
        line = read(root, "etc/kernel/cmdline").strip()
        self.assertIn("console=tty0 console=ttyS0,115200 quiet", line)
        self.assertIn("console=ttyS0,115200", read(root, "etc/default/grub"))

    def test_serial_console_gets_a_login_prompt(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "etc").mkdir()
        (root / "etc/inittab").write_text("c1:12345:respawn:/sbin/agetty 38400 tty1 linux\n#s0:12345:respawn:/sbin/agetty -L 9600 ttyS0 vt100\n")
        m = target_manifest(system={"serial_console": "ttyS1,57600"})
        configurer(root, ScriptedRunner()).configure_serial_getty(m)
        inittab = read(root, "etc/inittab")
        self.assertIn("s0:12345:respawn:/sbin/agetty -L 57600 ttyS1 vt100", inittab)
        self.assertNotIn("#s0:", inittab)
        runner = ScriptedRunner()
        configurer(root, runner).configure_serial_getty(target_manifest(system={"init": "systemd", "stage_variant": "amd64-systemd", "serial_console": "ttyS0"}))
        self.assertTrue(any("systemctl enable serial-getty@ttyS0.service" in line for line in runner.lines()))
        runner = ScriptedRunner()
        configurer(root, runner).configure_serial_getty(target_manifest())  # no serial console: nothing happens
        self.assertEqual(runner.commands, [])

    def test_systemd_boot_links_the_command_line_for_dracut(self) -> None:
        root, _, _ = self.initramfs(bootloader="systemd-boot")
        link = root / "etc/cmdline.d/00-installkernel.conf"
        self.assertTrue(link.is_symlink() and os.readlink(link) == "../kernel/cmdline")
        root, _, _ = self.initramfs(bootloader="grub")
        self.assertFalse((root / "etc/cmdline.d").exists())


class NoBootloaderTests(unittest.TestCase):
    def test_no_bootloader_installs_nothing_and_says_what_to_do(self) -> None:
        temporary = boot_root()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        runner = ScriptedRunner()
        c = configurer(root, runner)
        notes: list[str] = []
        c.notify = notes.append
        m = target_manifest(system={"bootloader": "none"})
        self.assertEqual(c.bootloader_atoms(m), [])
        c.configure_bootloader(m, "/dev/sda3", "/dev/sda1")
        self.assertEqual(runner.commands, [])
        self.assertTrue(any("No boot loader" in note for note in notes))
        self.assertFalse(any((root / hook).exists() for hook in ESP_SYNC_HOOKS))
        (root / "etc").mkdir(exist_ok=True)
        (root / "etc/fstab").write_text("UUID=abc / ext4 defaults 0 1\n")
        self.assertEqual(c.verify_boot_setup(m, "/dev/sda3", root_uuid="abc"), [])  # nothing is expected of a boot loader the user installs
