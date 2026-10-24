from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from gentoo_installer import __version__
from gentoo_installerd.configure import ESP_SYNC_HOOKS, NVRAM_FAILURE, ConfigurationError, render_esp_sync_hook
from gentoo_installerd.runner import InstallCancelled, RunnerError
from gentoo_installerd.storage import LayoutEntry

from tests.backend.target_helpers import FakePlan, ScriptedRunner, configurer, read, target_manifest

NVRAM_ERROR = "grub-install: error: efibootmgr failed to register the boot entry: Operation not permitted.\nEFI variables are not supported on this system."
UEFI_LAYOUT = [LayoutEntry("/dev/sda3", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat")]


def populate_boot(root: Path) -> None:
    (root / "boot").mkdir(exist_ok=True)
    (root / "boot/vmlinuz-6.12.0").write_bytes(b"kernel")
    (root / "boot/initramfs-6.12.0.img").write_bytes(b"initramfs")


def grub_commands(runner: ScriptedRunner) -> list[str]:
    return [line.split(" ", 2)[2] for line in runner.lines() if " grub-install " in line or " grub-mkconfig " in line]


class GrubUefiTests(unittest.TestCase):
    def install(self, m, runner=None, prepare=None, plan=None):
        runner = runner or ScriptedRunner()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        populate_boot(root)
        if prepare:
            prepare(root)
        c = configurer(root, runner)
        self.notes: list[str] = []
        c.notify = self.notes.append
        c.configure_bootloader(m, "/dev/sda3", "/dev/sda1", plan)
        return root, runner

    def test_grub_gets_a_firmware_entry_and_the_removable_fallback(self) -> None:
        _, runner = self.install(target_manifest())
        self.assertEqual(grub_commands(runner), [
            "grub-install --target=x86_64-efi --efi-directory=/efi --bootloader-id=Gentoo",
            "grub-install --target=x86_64-efi --efi-directory=/efi --removable --no-nvram",
            "grub-mkconfig -o /boot/grub/grub.cfg"])

    def test_a_firmware_that_rejects_boot_entries_still_ends_with_a_bootable_disk(self) -> None:
        runner = ScriptedRunner()
        runner.script("chroot", ("fail", NVRAM_ERROR))
        _, runner = self.install(target_manifest(), runner)
        self.assertEqual(grub_commands(runner)[:3], [
            "grub-install --target=x86_64-efi --efi-directory=/efi --bootloader-id=Gentoo",
            "grub-install --target=x86_64-efi --efi-directory=/efi --bootloader-id=Gentoo --no-nvram",
            "grub-install --target=x86_64-efi --efi-directory=/efi --removable --no-nvram"])
        self.assertTrue(any("firmware refused" in note for note in self.notes))

    def test_other_grub_install_failures_are_not_swallowed(self) -> None:
        runner = ScriptedRunner()
        runner.script("chroot", ("fail", "grub-install: error: cannot find an EFI directory."))
        with self.assertRaises(RunnerError):
            self.install(target_manifest(), runner)
        self.assertEqual(len(grub_commands(runner)), 1)

    def test_nvram_failure_patterns(self) -> None:
        for text in (NVRAM_ERROR, "efibootmgr: not found", "Could not open EFI variables", "NVRAM write failed", "efivars not mounted"):
            self.assertTrue(NVRAM_FAILURE.search(text), text)
        self.assertFalse(NVRAM_FAILURE.search("cannot find an EFI directory"))

    def test_fallback_policy_for_a_shared_esp(self) -> None:
        def with_fallback(root: Path) -> None:
            (root / "efi/EFI/BOOT").mkdir(parents=True)
            (root / "efi/EFI/BOOT/BOOTX64.EFI").write_bytes(b"windows")

        manual = {"storage": {"mode": "manual"}}
        # a preserved ESP keeps another system's fallback file ...
        _, runner = self.install(target_manifest(**manual), prepare=with_fallback)
        self.assertFalse(any("--removable" in command for command in grub_commands(runner)))
        # ... but takes the fallback when none exists ...
        _, runner = self.install(target_manifest(**manual))
        self.assertTrue(any("--removable" in command for command in grub_commands(runner)))
        # ... and an installer-created ESP always gets it.
        _, runner = self.install(target_manifest(), prepare=with_fallback)
        self.assertTrue(any("--removable" in command for command in grub_commands(runner)))

    def test_explicit_fallback_choices(self) -> None:
        _, runner = self.install(target_manifest(hardware={"removable_fallback": "never"}))
        self.assertFalse(any("--removable" in command for command in grub_commands(runner)))
        failing = ScriptedRunner()
        failing.script("chroot", ("fail", NVRAM_ERROR))
        _, runner = self.install(target_manifest(hardware={"removable_fallback": "never"}), failing)
        self.assertTrue(any("--removable" in command for command in grub_commands(runner)), "no entry and no fallback would not boot")
        _, runner = self.install(target_manifest(storage={"mode": "manual"}, hardware={"removable_fallback": "always"}))
        self.assertTrue(any("--removable" in command for command in grub_commands(runner)))

    def test_hardware_plan_decides_the_fallback_when_it_has_an_opinion(self) -> None:
        class Plan(FakePlan):
            removable_fallback = "never"

        _, runner = self.install(target_manifest(), plan=Plan())
        self.assertFalse(any("--removable" in command for command in grub_commands(runner)))

    def test_bios_installs_are_unchanged(self) -> None:
        _, runner = self.install(target_manifest(storage={"firmware": "bios"}))
        self.assertEqual(grub_commands(runner), ["grub-install --target=i386-pc /dev/sda", "grub-mkconfig -o /boot/grub/grub.cfg"])

    def test_dual_boot_turns_os_prober_on_before_the_menu_is_generated(self) -> None:
        root, runner = self.install(target_manifest(system={"dual_boot": True}))
        self.assertIn("GRUB_DISABLE_OS_PROBER=false", read(root, "etc/default/grub"))
        self.assertEqual(grub_commands(runner)[-1], "grub-mkconfig -o /boot/grub/grub.cfg")
        root, _ = self.install(target_manifest())
        self.assertFalse((root / "etc/default/grub").exists())

    def test_dual_boot_replaces_an_existing_setting(self) -> None:
        def prepare(root: Path) -> None:
            (root / "etc/default").mkdir(parents=True)
            (root / "etc/default/grub").write_text("GRUB_DISABLE_OS_PROBER=true\nGRUB_TIMEOUT=2\n")

        root, _ = self.install(target_manifest(system={"dual_boot": True}), prepare=prepare)
        self.assertEqual(read(root, "etc/default/grub"), "GRUB_TIMEOUT=2\nGRUB_DISABLE_OS_PROBER=false\n")

    def test_bootloader_packages_include_os_prober_only_for_dual_boot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            c = configurer(temporary)
            self.assertEqual(c.bootloader_atoms(target_manifest()), ["sys-boot/grub"])
            self.assertEqual(c.bootloader_atoms(target_manifest(system={"dual_boot": True})), ["sys-boot/grub", "sys-boot/os-prober"])
            self.assertEqual(c.bootloader_atoms(target_manifest(system={"bootloader": "systemd-boot", "dual_boot": True})), ["sys-apps/systemd-utils"])


class OtherBootloaderTests(unittest.TestCase):
    def install(self, m, runner=None, root_setup=None):
        runner = runner or ScriptedRunner()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        populate_boot(root)
        if root_setup:
            root_setup(root)
        c = configurer(root, runner)
        self.notes: list[str] = []
        c.notify = self.notes.append
        c.configure_bootloader(m, "/dev/sda3", "/dev/sda1")
        return root, runner

    def test_systemd_boot_retries_without_firmware_variables(self) -> None:
        runner = ScriptedRunner()
        runner.script("chroot", ("fail", "Failed to write EFI variables: Operation not permitted"))
        root, runner = self.install(target_manifest(system={"bootloader": "systemd-boot"}), runner)
        bootctl = [line.split(" ", 2)[2] for line in runner.lines() if " bootctl " in line]
        self.assertEqual(bootctl, ["bootctl --path=/efi install", "bootctl --path=/efi --no-variables install"])
        self.assertTrue((root / "efi/loader/entries/gentoo.conf").is_file())

    def test_systemd_boot_failures_unrelated_to_variables_are_raised(self) -> None:
        runner = ScriptedRunner()
        runner.script("chroot", ("fail", "ESP is not a FAT filesystem"))
        with self.assertRaises(RunnerError):
            self.install(target_manifest(system={"bootloader": "systemd-boot"}), runner)

    def test_efi_stub_with_uki_gets_the_fallback_path(self) -> None:
        def setup(root: Path) -> None:
            (root / "efi/EFI/Linux").mkdir(parents=True)
            (root / "efi/EFI/Linux/gentoo.efi").write_bytes(b"uki")

        runner = ScriptedRunner()
        runner.respond("lsblk --noheadings --output PARTN", "1\n")
        runner.respond("lsblk --noheadings --output PKNAME", "sda\n")
        root, _ = self.install(target_manifest(system={"bootloader": "efi-stub", "uki": True}), runner, setup)
        # the UKI is built by ukify in the chroot (not run here), so the fixture provides it
        self.assertEqual((root / "efi/EFI/BOOT/BOOTX64.EFI").read_bytes(), b"uki")

    def test_efi_stub_with_uki_survives_a_firmware_that_rejects_the_entry(self) -> None:
        def setup(root: Path) -> None:
            (root / "efi/EFI/Linux").mkdir(parents=True)
            (root / "efi/EFI/Linux/gentoo.efi").write_bytes(b"uki")

        runner = ScriptedRunner()
        runner.respond("lsblk --noheadings --output PARTN", "1\n")
        runner.respond("lsblk --noheadings --output PKNAME", "sda\n")
        runner.script("*efibootmgr", ("fail", "Could not create variable: Operation not permitted"))
        root, _ = self.install(target_manifest(system={"bootloader": "efi-stub", "uki": True}), runner, setup)
        self.assertEqual((root / "efi/EFI/BOOT/BOOTX64.EFI").read_bytes(), b"uki")
        self.assertTrue(any("boots from the fallback path" in note for note in self.notes))

    def test_efi_stub_without_uki_cannot_have_a_fallback(self) -> None:
        runner = ScriptedRunner()
        runner.respond("lsblk --noheadings --output PARTN", "1\n")
        runner.respond("lsblk --noheadings --output PKNAME", "sda\n")
        root, _ = self.install(target_manifest(system={"bootloader": "efi-stub"}), runner)
        self.assertFalse((root / "efi/EFI/BOOT/BOOTX64.EFI").exists())
        self.assertTrue(any("no fallback boot path" in note for note in self.notes))
        failing = ScriptedRunner()
        failing.respond("lsblk --noheadings --output PARTN", "1\n")
        failing.respond("lsblk --noheadings --output PKNAME", "sda\n")
        failing.script("chroot", ("fail", "efibootmgr: EFI variables are not supported"))
        with self.assertRaises(RunnerError):
            self.install(target_manifest(system={"bootloader": "efi-stub"}), failing)


class SigningRunner(ScriptedRunner):
    """Records what the signing key directory looks like while sbsign runs."""

    def __init__(self, secret_dir: Path, cancel_on_sign: bool = False) -> None:
        super().__init__()
        self.secret_dir = secret_dir
        self.cancel_on_sign = cancel_on_sign
        self.during_signing: dict[str, object] = {}

    def run(self, argv, **kwargs):
        if len(argv) > 3 and argv[2] == "sbsign":
            self.during_signing = {
                "dir_mode": self.secret_dir.stat().st_mode & 0o777,
                "key_mode": (self.secret_dir / "signing.key").stat().st_mode & 0o777,
                "key": (self.secret_dir / "signing.key").read_text(),
            }
            if self.cancel_on_sign:
                raise InstallCancelled("Installation cancelled")
        return super().run(argv, **kwargs)


class SecureBootTests(unittest.TestCase):
    KEY = "-----BEGIN PRIVATE KEY-----\nSECRET-KEY-MATERIAL\n-----END PRIVATE KEY-----\n"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "target"
        self.root.mkdir()
        populate_boot(self.root)
        self.secrets = self.base / "run-sb"
        self.key = self.base / "sb.key"
        self.cert = self.base / "sb.crt"
        self.key.write_text(self.KEY)
        self.cert.write_text("-----BEGIN CERTIFICATE-----\nCERT\n-----END CERTIFICATE-----\n")
        self.manifest = target_manifest(system={"bootloader": "systemd-boot", "uki": True, "secure_boot": True,
                                                 "secure_boot_key": str(self.key), "secure_boot_certificate": str(self.cert)})
        (self.root / "efi/EFI/Linux").mkdir(parents=True)

    def configure(self, runner):
        c = configurer(self.root, runner, secret_dir=self.secrets)
        self.notes: list[str] = []
        c.notify = self.notes.append
        return c

    def tree_text(self) -> str:
        return "\n".join(path.read_text(errors="ignore") for path in self.root.rglob("*") if path.is_file())

    def test_the_private_key_never_touches_the_target_disk(self) -> None:
        runner = SigningRunner(self.secrets)
        runner.respond("openssl x509 -noout -pubkey", "PUBKEY")
        runner.respond("openssl pkey -pubout", "PUBKEY")
        self.configure(runner).configure_bootloader(self.manifest, "/dev/sda3", "/dev/sda1")
        self.assertNotIn("SECRET-KEY-MATERIAL", self.tree_text())
        self.assertFalse((self.root / "root/secure-boot.key").exists())
        sign = next(command for command in runner.commands if "sbsign" in command)
        self.assertEqual(sign[sign.index("--key") + 1], "/run/gentoo-installer-sb/signing.key")
        # during signing the key lives in a private directory of the live tmpfs, and is gone afterwards
        self.assertEqual(runner.during_signing["dir_mode"], 0o700)
        self.assertEqual(runner.during_signing["key_mode"], 0o600)
        self.assertEqual(runner.during_signing["key"], self.KEY)
        self.assertFalse(self.secrets.exists())

    def test_every_boot_image_is_signed_and_verified(self) -> None:
        runner = SigningRunner(self.secrets)
        runner.respond("openssl x509 -noout -pubkey", "PUBKEY")
        runner.respond("openssl pkey -pubout", "PUBKEY")
        self.configure(runner).configure_bootloader(self.manifest, "/dev/sda3", "/dev/sda1")
        signed = [command[command.index("--output") + 1].removesuffix(".signed") for command in runner.commands if "sbsign" in command]
        self.assertEqual(signed, ["/efi/EFI/Linux/gentoo.efi", "/efi/EFI/systemd/systemd-bootx64.efi", "/efi/EFI/BOOT/BOOTX64.EFI"])
        self.assertEqual(sum("sbverify" in command for command in runner.commands), 3)

    def test_the_fallback_copy_of_an_efi_stub_uki_is_signed_too(self) -> None:
        for mode in ("erase", "manual"):  # a shared ESP only gets the copy when none exists, and it must still be signed
            with self.subTest(mode=mode):
                m = target_manifest(storage={"mode": mode}, system={"bootloader": "efi-stub", "uki": True, "secure_boot": True,
                                                                     "secure_boot_key": str(self.key), "secure_boot_certificate": str(self.cert)})
                if mode == "manual":
                    from gentoo_installer.manifest import Partition

                    m.storage.partitions = [Partition("/dev/sda3", "/", "ext4"), Partition("/dev/sda1", "/efi", "vfat")]
                (self.root / "efi/EFI/Linux/gentoo.efi").write_bytes(b"uki")
                (self.root / "efi/EFI/BOOT").mkdir(parents=True, exist_ok=True)
                (self.root / "efi/EFI/BOOT/BOOTX64.EFI").unlink(missing_ok=True)
                runner = SigningRunner(self.secrets)
                runner.respond("openssl x509 -noout -pubkey", "PUBKEY")
                runner.respond("openssl pkey -pubout", "PUBKEY")
                runner.respond("lsblk --noheadings --output PARTN", "1\n")
                runner.respond("lsblk --noheadings --output PKNAME", "sda\n")
                self.configure(runner).configure_bootloader(m, "/dev/sda3", "/dev/sda1")
                signed = [command[command.index("--output") + 1].removesuffix(".signed") for command in runner.commands if "sbsign" in command]
                self.assertEqual(signed, ["/efi/EFI/Linux/gentoo.efi", "/efi/EFI/BOOT/BOOTX64.EFI"])

    def test_cancelling_during_signing_still_removes_the_key(self) -> None:
        runner = SigningRunner(self.secrets, cancel_on_sign=True)
        runner.respond("openssl x509 -noout -pubkey", "PUBKEY")
        runner.respond("openssl pkey -pubout", "PUBKEY")
        with self.assertRaises(InstallCancelled):
            self.configure(runner).configure_bootloader(self.manifest, "/dev/sda3", "/dev/sda1")
        self.assertFalse(self.secrets.exists())
        self.assertNotIn("SECRET-KEY-MATERIAL", self.tree_text())

    def test_installer_cleanup_removes_staged_secrets(self) -> None:
        c = self.configure(ScriptedRunner())
        c._stage_signing_material(self.manifest.system)
        self.assertTrue((self.secrets / "signing.key").exists())
        c.cleanup_secrets()
        c.cleanup_secrets()  # nothing left: not an error
        self.assertFalse(self.secrets.exists())

    def test_certificate_is_published_for_enrollment(self) -> None:
        runner = SigningRunner(self.secrets)
        runner.respond("openssl x509 -noout -pubkey", "PUBKEY")
        runner.respond("openssl pkey -pubout", "PUBKEY")
        self.configure(runner).configure_bootloader(self.manifest, "/dev/sda3", "/dev/sda1")
        convert = next(command for command in runner.commands if command[:3] == ("openssl", "x509", "-in"))
        self.assertIn("DER", convert)
        self.assertEqual(convert[-1], str(self.root / "efi/EFI/enroll/gentoo-secureboot.cer"))
        self.assertTrue(any("enroll" in note for note in self.notes))

    def test_key_and_certificate_are_checked_before_anything_is_installed(self) -> None:
        runner = ScriptedRunner()
        runner.respond("openssl x509 -noout -pubkey", "PUBKEY-A")
        runner.respond("openssl pkey -pubout", "PUBKEY-B")
        c = self.configure(runner)
        self.assertEqual(c.secure_boot_problems(self.manifest), ["The Secure Boot certificate does not belong to the signing key"])
        with self.assertRaisesRegex(ConfigurationError, "does not belong"):
            c.configure_bootloader(self.manifest, "/dev/sda3", "/dev/sda1")
        self.assertEqual([command for command in runner.commands if "bootctl" in command], [])
        self.key.unlink()
        self.assertIn("signing key is missing", c.secure_boot_problems(self.manifest)[0])
        broken = ScriptedRunner()
        broken.script("openssl", ("fail", "unable to load certificate"))
        self.key.write_text(self.KEY)
        self.assertIn("cannot be parsed", self.configure(broken).secure_boot_problems(self.manifest)[0])

    def test_grub_and_missing_uefi_are_refused(self) -> None:
        for sections, message in (({"system": {"bootloader": "grub"}}, "not supported with GRUB"),
                                  ({"storage": {"firmware": "bios"}}, "requires UEFI")):
            m = self.manifest
            for section, values in sections.items():
                for name, value in values.items():
                    setattr(getattr(m, section), name, value)
            with self.subTest(message=message), self.assertRaisesRegex(ConfigurationError, message):
                configurer(self.root, ScriptedRunner(), dry_run=True).configure_bootloader(m, "/dev/sda3")

    def test_dry_run_signs_with_the_private_directory_path_and_leaves_nothing_behind(self) -> None:
        runner = ScriptedRunner()
        configurer(self.root, runner, dry_run=True, secret_dir=self.secrets).configure_bootloader(self.manifest, "/dev/sda3", "/dev/sda1")
        sign = next(command for command in runner.commands if "sbsign" in command)
        self.assertEqual(sign[sign.index("--key") + 1], "/run/gentoo-installer-sb/signing.key")
        self.assertFalse(self.secrets.exists())


class EspSyncHookTests(unittest.TestCase):
    """Kernel updates must reach the EFI system partition; the hook runs for real, in a temporary root."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "boot").mkdir()
        (self.root / "efi").mkdir()
        (self.root / "etc/kernel").mkdir(parents=True)
        (self.root / "etc/kernel/cmdline").write_text("root=UUID=abc rw\n")
        (self.root / "efi/vmlinuz").write_bytes(b"old kernel")
        (self.root / "efi/initramfs").write_bytes(b"old initramfs")
        self.bin = self.root / "bin"
        self.bin.mkdir()

    def run_hook(self, *args: str, uki: bool = False, fallback: bool = False) -> subprocess.CompletedProcess:
        script = self.root / "hook.sh"
        script.write_text(render_esp_sync_hook("/efi", uki, fallback))
        environment = {**os.environ, "ESP_SYNC_ROOT": str(self.root), "PATH": f"{self.bin}:{os.environ['PATH']}"}
        return subprocess.run(["sh", str(script), *args], env=environment, capture_output=True, text=True)

    def new_kernel(self, version: str = "6.13.1", name: str = "vmlinuz") -> None:
        (self.root / f"boot/{name}-{version}").write_bytes(b"new kernel")
        (self.root / f"boot/initramfs-{version}.img").write_bytes(b"new initramfs")

    def test_installkernel_hook_copies_kernel_and_initramfs_to_the_esp(self) -> None:
        self.new_kernel()
        result = self.run_hook("6.13.1", "/boot/vmlinuz-6.13.1")
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual((self.root / "efi/vmlinuz").read_bytes(), b"new kernel")
        self.assertEqual((self.root / "efi/initramfs").read_bytes(), b"new initramfs")
        self.assertEqual(sorted(path.name for path in (self.root / "efi").iterdir()), ["initramfs", "vmlinuz"])  # no stray .new files

    def test_kernel_install_plugin_arguments_and_the_compat_kernel_name(self) -> None:
        self.new_kernel(name="kernel")
        result = self.run_hook("add", "6.13.1", "/efi/entry", "/usr/src/linux/arch/x86/boot/bzImage")
        self.assertEqual(result.returncode, 0)
        self.assertEqual((self.root / "efi/vmlinuz").read_bytes(), b"new kernel")

    def test_removal_and_unknown_versions_change_nothing_and_never_fail(self) -> None:
        self.new_kernel()
        self.assertEqual(self.run_hook("remove", "6.13.1").returncode, 0)
        self.assertEqual((self.root / "efi/vmlinuz").read_bytes(), b"old kernel")
        result = self.run_hook("9.9.9", "/boot/vmlinuz-9.9.9")
        self.assertEqual(result.returncode, 0)
        self.assertIn("no kernel image for 9.9.9", result.stderr)
        self.assertEqual(self.run_hook().returncode, 0)

    def test_an_unwritable_esp_is_reported_but_does_not_fail_the_kernel_installation(self) -> None:
        self.new_kernel()
        (self.root / "efi").chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup((self.root / "efi").chmod, 0o755)
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        result = self.run_hook("6.13.1", "/boot/vmlinuz-6.13.1")
        self.assertEqual(result.returncode, 0)
        self.assertIn("could not copy the kernel", result.stderr)

    def write_ukify(self, succeed: bool = True) -> None:
        ukify = self.bin / "ukify"
        ukify.write_text("#!/bin/sh\nout=\ncmdline=\n"
                         "while [ $# -gt 0 ]; do case \"$1\" in --output) out=\"$2\"; shift;; --cmdline) cmdline=\"$2\"; shift;; esac; shift; done\n"
                         + ('echo "uki built" > "$out"; echo "$cmdline" > "$out.cmdline"\n' if succeed else "exit 1\n"))
        ukify.chmod(0o755)

    def test_uki_installs_rebuild_the_image_and_refresh_the_fallback_copy(self) -> None:
        self.new_kernel()
        self.write_ukify()
        (self.root / "efi/EFI/Linux").mkdir(parents=True)
        (self.root / "efi/EFI/BOOT").mkdir(parents=True)
        (self.root / "efi/EFI/Linux/gentoo.efi").write_text("old uki")
        result = self.run_hook("6.13.1", "/boot/vmlinuz-6.13.1", uki=True, fallback=True)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual((self.root / "efi/EFI/Linux/gentoo.efi").read_text(), "uki built\n")
        self.assertEqual((self.root / "efi/EFI/BOOT/BOOTX64.EFI").read_text(), "uki built\n")
        self.assertEqual((self.root / "efi/EFI/Linux/gentoo.efi.new.cmdline").read_text(), "root=UUID=abc rw\n")
        self.assertEqual((self.root / "efi/vmlinuz").read_bytes(), b"old kernel")  # UKI installs do not use the loose files

    def test_a_failed_uki_build_keeps_the_working_image(self) -> None:
        self.new_kernel()
        self.write_ukify(succeed=False)
        (self.root / "efi/EFI/Linux").mkdir(parents=True)
        (self.root / "efi/EFI/Linux/gentoo.efi").write_text("old uki")
        result = self.run_hook("6.13.1", "/boot/vmlinuz-6.13.1", uki=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("could not build the Unified Kernel Image", result.stderr)
        self.assertEqual((self.root / "efi/EFI/Linux/gentoo.efi").read_text(), "old uki")
        self.assertFalse((self.root / "efi/EFI/Linux/gentoo.efi.new").exists())

    def test_the_script_is_valid_sh_and_rejects_hostile_mount_points(self) -> None:
        script = self.root / "syntax.sh"
        script.write_text(render_esp_sync_hook("/boot/efi", True, True))
        self.assertEqual(subprocess.run(["sh", "-n", str(script)], capture_output=True).returncode, 0)
        for bad in ('/efi"; rm -rf /; "', "efi", "/ef i", ""):
            with self.assertRaises(ConfigurationError):
                render_esp_sync_hook(bad, False)


class EspSyncInstallTests(unittest.TestCase):
    def install(self, m, fallback=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configurer(root).install_esp_sync_hook(m, fallback)
            return {hook: ((root / hook).read_text(), (root / hook).stat().st_mode & 0o777) for hook in ESP_SYNC_HOOKS if (root / hook).exists()}

    def test_hook_is_installed_for_both_installkernel_flavours(self) -> None:
        installed = self.install(target_manifest(system={"bootloader": "systemd-boot"}))
        self.assertEqual(set(installed), set(ESP_SYNC_HOOKS))
        for text, mode in installed.values():
            self.assertEqual(mode, 0o755)
            self.assertIn('ESP="/efi"', text)
            self.assertIn("UKI=0", text)

    def test_manual_esp_mount_and_uki_are_baked_in(self) -> None:
        from gentoo_installer.manifest import Partition

        m = target_manifest(system={"bootloader": "efi-stub", "uki": True}, storage={"mode": "manual", "partitions": [Partition("/dev/sda3", "/", "ext4"), Partition("/dev/sda1", "/boot/efi", "vfat")]})
        text, _ = next(iter(self.install(m, fallback=True).values()))
        self.assertIn('ESP="/boot/efi"', text)
        self.assertIn("UKI=1", text)
        self.assertIn("FALLBACK_COPY=1", text)

    def test_secure_boot_installs_get_no_hook_because_updates_would_be_unsigned(self) -> None:
        m = target_manifest(system={"bootloader": "systemd-boot", "uki": True, "secure_boot": True})
        self.assertEqual(self.install(m), {})

    def test_the_bootloader_stage_installs_it_for_non_grub_bootloaders_only(self) -> None:
        for bootloader, uki, expected in (("grub", False, False), ("grub", True, True), ("systemd-boot", False, True), ("efi-stub", False, True)):
            with self.subTest(bootloader=bootloader, uki=uki), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                populate_boot(root)
                runner = ScriptedRunner()
                runner.respond("lsblk --noheadings --output PARTN", "1\n")
                runner.respond("lsblk --noheadings --output PKNAME", "sda\n")
                runner.respond("blkid", "1234-ABCD\n")
                m = target_manifest(system={"bootloader": bootloader, "uki": uki})
                configurer(root, runner).configure_bootloader(m, "/dev/sda3", "/dev/sda1")
                self.assertEqual(all((root / hook).exists() for hook in ESP_SYNC_HOOKS), expected)

    def test_kernel_images_named_by_the_systemd_compat_layout_are_found(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "boot").mkdir()
            (root / "boot/kernel-6.12.0").write_bytes(b"compat kernel")
            (root / "boot/initramfs-6.12.0.img").write_bytes(b"initramfs")
            configurer(root).configure_bootloader(target_manifest(system={"bootloader": "systemd-boot"}), "/dev/sda3", "/dev/sda1")
            self.assertEqual((root / "efi/vmlinuz").read_bytes(), b"compat kernel")


class ReceiptTests(unittest.TestCase):
    def test_receipt_follows_the_documented_schema_and_holds_no_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = ScriptedRunner()
            runner.respond("blkid -s UUID -o value /dev/mapper/gentoo-root", "11111111-2222-3333-4444-555555555555\n")
            runner.respond("blkid -s UUID -o value /dev/sda3", "99999999-8888-7777-6666-555555555555\n")
            m = target_manifest(storage={"luks": True, "luks_passphrase": "very-secret-passphrase"}, system={"init": "systemd", "stage_variant": "amd64-systemd", "desktop": "kde"})
            layout = [LayoutEntry("/dev/mapper/gentoo-root", "/", "btrfs"), LayoutEntry("/dev/sda1", "/efi", "vfat"), LayoutEntry("/dev/sda2", "/boot", "ext4")]
            receipt = configurer(root, runner).write_receipt(m, "/dev/mapper/gentoo-root", layout, "/dev/sda3", FakePlan(summary=["Graphics: open drivers", "CPU: Intel"]))
            path = root / "etc/gentoo-live-installer/install.json"
            self.assertEqual(json.loads(path.read_text()), receipt)
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertEqual(set(receipt), {"version", "installer", "created", "firmware", "bootloader", "bootloader_id", "esp", "boot_separate",
                                            "init", "kernel", "initramfs", "uki", "secure_boot", "profile", "root", "cmdline_file", "hardware"})
            self.assertEqual((receipt["version"], receipt["installer"]), (1, __version__))
            self.assertRegex(receipt["created"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            self.assertEqual((receipt["firmware"], receipt["bootloader"], receipt["bootloader_id"], receipt["esp"]), ("uefi", "grub", "Gentoo", "/efi"))
            self.assertEqual((receipt["init"], receipt["kernel"], receipt["initramfs"], receipt["uki"], receipt["secure_boot"]),
                             ("systemd", "gentoo-kernel-bin", "dracut", False, False))
            self.assertTrue(receipt["boot_separate"])
            self.assertEqual(receipt["profile"], "default/linux/amd64/23.0/desktop/plasma/systemd")
            self.assertEqual(receipt["root"], {"fstype": "btrfs", "uuid": "11111111-2222-3333-4444-555555555555",
                                               "luks_uuid": "99999999-8888-7777-6666-555555555555", "lvm": False, "raid": ""})
            self.assertEqual(receipt["cmdline_file"], "/etc/kernel/cmdline")
            self.assertEqual(receipt["hardware"], ["Graphics: open drivers", "CPU: Intel"])
            self.assertNotIn("very-secret-passphrase", path.read_text())

    def test_receipt_without_plan_and_on_bios(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            receipt = configurer(temporary, ScriptedRunner()).write_receipt(
                target_manifest(storage={"firmware": "bios"}), "/dev/sda2", [LayoutEntry("/dev/sda2", "/", "ext4")])
            self.assertEqual((receipt["esp"], receipt["hardware"], receipt["boot_separate"]), ("", [], False))
            self.assertEqual(receipt["root"]["luks_uuid"], "")

    def test_dry_run_writes_no_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configurer(temporary, dry_run=True).write_receipt(target_manifest(), "/dev/sda3")
            self.assertFalse((Path(temporary) / "etc/gentoo-live-installer").exists())


class VerificationTests(unittest.TestCase):
    UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def build(self, root: Path, *, grub_cfg: str | None = None, efi: bool = True) -> None:
        populate_boot(root)
        (root / "etc").mkdir()
        (root / "etc/fstab").write_text(f"UUID={self.UUID} / ext4 defaults,noatime 0 1\n")
        (root / "boot/grub").mkdir()
        (root / "boot/grub/grub.cfg").write_text(grub_cfg if grub_cfg is not None else "menuentry 'Gentoo' {\n\tlinux\t/vmlinuz-6.12.0 root=UUID=x ro\n}\n")
        if efi:
            (root / "efi/EFI/Gentoo").mkdir(parents=True)
            (root / "efi/EFI/Gentoo/grubx64.efi").write_bytes(b"grub")

    def verify(self, m=None, **kwargs):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.build(root, **kwargs)
            return configurer(root).verify_boot_setup(m or target_manifest(), "/dev/sda3", root_uuid=self.UUID)

    def test_a_complete_installation_has_no_problems(self) -> None:
        self.assertEqual(self.verify(), [])

    def test_dry_run_never_inspects_the_target(self) -> None:
        self.assertEqual(configurer("/nonexistent", dry_run=True).verify_boot_setup(target_manifest(), "/dev/sda3"), [])

    def test_missing_pieces_are_named(self) -> None:
        self.assertEqual(self.verify(efi=False), ["No GRUB EFI image was installed on the EFI system partition"])
        self.assertEqual(self.verify(grub_cfg="# empty\n"), ["grub.cfg has no menu entry that loads a kernel"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc").mkdir()
            (root / "etc/fstab").write_text("/dev/other / ext4 defaults 0 1\n")
            problems = configurer(root).verify_boot_setup(target_manifest(system={"bootloader": "systemd-boot", "uki": True}), "/dev/sda3", root_uuid=self.UUID)
            self.assertEqual(problems, ["No kernel image was installed under /boot", "No initramfs was installed under /boot",
                                        "/etc/fstab does not mention the root filesystem", "The systemd-boot entry was not written",
                                        "The Unified Kernel Image is missing from the EFI system partition"])

    def test_the_grub_menu_must_pass_the_arguments_that_find_the_root_filesystem(self) -> None:
        m = target_manifest(storage={"luks": True, "lvm": True, "luks_passphrase": "long-enough-passphrase"})
        problems = self.verify(m)
        self.assertEqual(problems, ["grub.cfg does not pass rd.luks.uuid, so the root filesystem would not be found",
                                    "grub.cfg does not pass rd.lvm.lv, so the root filesystem would not be found"])
        good = "menuentry 'x' {\n\tlinux /vmlinuz-6 root=UUID=x ro rd.luks.uuid=u rd.lvm.lv=gentoo/root\n}\n"
        self.assertEqual(self.verify(m, grub_cfg=good), [])

    def test_the_bios_install_needs_no_efi_image(self) -> None:
        self.assertEqual(self.verify(target_manifest(storage={"firmware": "bios"}), efi=False), [])

    def test_initramfs_modules_come_from_lsinitrd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            populate_boot(root)
            runner = ScriptedRunner()
            runner.respond("chroot", "dracut modules:\nbash\ncrypt\ndm\nlvm\nrootfs-block\n")
            self.assertEqual(configurer(root, runner).initramfs_modules(), {"bash", "crypt", "dm", "lvm", "rootfs-block"})
            self.assertEqual(runner.commands[0][2:], ("lsinitrd", "-m", "/boot/initramfs-6.12.0.img"))
            failing = ScriptedRunner()
            failing.script("chroot", ("fail", "no such file"))
            self.assertIsNone(configurer(root, failing).initramfs_modules())
            self.assertIsNone(configurer(temporary, dry_run=True).initramfs_modules())
            empty = Path(temporary, "empty")
            empty.mkdir()
            self.assertIsNone(configurer(empty, ScriptedRunner()).initramfs_modules())


if __name__ == "__main__":
    unittest.main()
