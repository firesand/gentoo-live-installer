from __future__ import annotations

import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from gentoo_installerd.install import RECEIPT_PATH, InstallError, Installer
from gentoo_installerd.storage import LayoutEntry

from tests.backend.helpers import FakeInspector
from tests.backend.target_helpers import FakePlan, ScriptedRunner, mark_installed, read, target_manifest
from tests.backend.test_target_packages import EmergeDiagnosisTests

UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
LAYOUT = [LayoutEntry("/dev/sda3", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat")]


class StageHarness(unittest.TestCase):
    """An Installer on temporary directories whose runner is scripted; stages are called directly."""

    dry_run = False

    def make(self, m=None, runner=None, plan=None, **kwargs) -> Installer:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.runner = runner or ScriptedRunner()
        self.events: list = []
        installer = Installer(m or target_manifest(), runner=self.runner, target_root=self.base / "target", state_dir=self.base / "state",
                              cache_dir=self.base / "cache", dry_run=self.dry_run, inspector=FakeInspector(), emit=self.events.append, **kwargs)
        self.addCleanup(lambda: installer._log_file and installer._log_file.close())
        installer.storage.layout = list(LAYOUT)
        installer.configurer.sleep = lambda seconds: None
        if plan is not None:
            installer.hardware_plan = plan
        (self.base / "target").mkdir(exist_ok=True)
        self.target = self.base / "target"
        return installer

    def chroot_lines(self) -> list[str]:
        prefix = f"chroot {self.target} "
        return [line.removeprefix(prefix) for line in self.runner.lines() if line.startswith(prefix)]

    def logs(self) -> list[str]:
        return [event.message for event in self.events if event.type == "log"]


class PortageStageTests(StageHarness):
    def test_the_package_set_is_resolved_before_any_hour_long_work_starts(self) -> None:
        installer = self.make()
        installer._portage()
        lines = self.chroot_lines()
        self.assertEqual(lines[0], "eselect profile set default/linux/amd64/23.0/desktop/plasma")
        pretend = next(line for line in lines if " --pretend " in f" {line} ")
        for atom in ("sys-kernel/installkernel", "sys-kernel/gentoo-kernel-bin", "sys-kernel/dracut", "app-admin/sudo",
                     "net-misc/networkmanager", "sys-boot/grub", "kde-plasma/plasma-meta", "gui-apps/sddm", "sys-fs/dosfstools"):
            self.assertIn(atom, pretend)
        for flag in ("--pretend", "--noreplace"):
            self.assertIn(flag, pretend)
        self.assertNotIn("media-video/pipewire", pretend)  # optional packages are not part of the gate
        self.assertEqual((self.target / "etc/portage/make.conf").read_text().count("MAKEOPTS"), 1)

    def test_an_unresolvable_choice_stops_here_and_names_the_flag(self) -> None:
        runner = ScriptedRunner()
        runner.script("*--pretend", ("fail", EmergeDiagnosisTests.USE_CHANGES))
        installer = self.make(runner=runner)
        with self.assertRaisesRegex(InstallError, "cannot resolve.*qtpositioning.*geoclue"):
            installer._portage()

    def test_the_profile_is_derived_when_the_manifest_names_none(self) -> None:
        installer = self.make(target_manifest(system={"init": "systemd", "stage_variant": "amd64-systemd", "desktop": "minimal"}))
        installer._portage()
        self.assertEqual(self.chroot_lines()[0], "eselect profile set default/linux/amd64/23.0/systemd")

    def test_hardware_plan_flags_reach_portage_before_the_resolve(self) -> None:
        installer = self.make(plan=FakePlan(package_use={"media-libs/mesa": ["-vulkan"]}, make_conf={"CPU_FLAGS_X86": "${CPU_FLAGS_X86} avx2"}))
        installer._portage()
        self.assertEqual(read(self.target, "etc/portage/package.use/installer-hardware"), "media-libs/mesa -vulkan\n")
        self.assertIn("CPU_FLAGS_X86='${CPU_FLAGS_X86} avx2'", read(self.target, "etc/portage/make.conf"))


class RepositoryStageTests(StageHarness):
    def test_mirrors_and_proxy_are_configured_before_the_tree_is_downloaded(self) -> None:
        class Checking(ScriptedRunner):
            def run(inner, argv, **kwargs):
                if "emerge-webrsync" in argv:
                    inner.make_conf_at_sync = (self.target / "etc/portage/make.conf").read_text()
                return super().run(argv, **kwargs)

        runner = Checking()
        installer = self.make(target_manifest(portage={"mirror": "https://mirror.example/gentoo"}, network={"proxy": "http://proxy.example:3128"}), runner)
        installer._repository()
        self.assertIn("GENTOO_MIRRORS=https://mirror.example/gentoo", runner.make_conf_at_sync)
        sync = next(i for i, command in enumerate(runner.commands) if "emerge-webrsync" in command)
        self.assertEqual(runner.envs[sync]["https_proxy"], "http://proxy.example:3128")
        self.assertEqual(runner.envs[sync]["RSYNC_PROXY"], "proxy.example:3128")

    def test_rsync_mirrors_use_emaint(self) -> None:
        installer = self.make(target_manifest(portage={"rsync_mirror": "rsync://rsync.example/gentoo-portage"}))
        installer._repository()
        self.assertIn("emaint sync --auto", self.chroot_lines())
        self.assertIn("rsync://rsync.example/gentoo-portage", read(self.target, "etc/portage/repos.conf/gentoo.conf"))

    def test_plain_installs_pass_no_environment(self) -> None:
        installer = self.make()
        installer._repository()
        sync = next(i for i, command in enumerate(self.runner.commands) if "emerge-webrsync" in command)
        self.assertFalse(self.runner.envs[sync])


class KernelStageTests(StageHarness):
    def prepare(self, **kwargs) -> Installer:
        runner = ScriptedRunner()
        runner.respond("blkid -s UUID -o value /dev/sda3", UUID + "\n")
        installer = self.make(runner=runner, **kwargs)
        return installer

    def test_filesystem_tools_and_drivers_come_before_the_initramfs_is_generated(self) -> None:
        plan = FakePlan(driver_atoms=["x11-drivers/nvidia-drivers"], files=[("etc/modprobe.d/nvidia.conf", "blacklist nouveau\n", 0o644)],
                        early_atoms=["sys-firmware/sof-firmware"])
        installer = self.prepare(plan=plan)
        installer.storage.layout = [LayoutEntry("/dev/sda3", "/", "xfs"), LayoutEntry("/dev/sda1", "/efi", "vfat")]
        installer.manifest.storage.filesystem = "xfs"
        installer._kernel()
        lines = self.chroot_lines()
        index = {key: next(i for i, line in enumerate(lines) if key in line) for key in
                 ("sys-fs/xfsprogs", "sys-firmware/sof-firmware", "sys-kernel/installkernel", "nvidia-drivers", "--regenerate-all")}
        self.assertLess(index["sys-fs/xfsprogs"], index["sys-kernel/installkernel"])
        self.assertLess(index["sys-firmware/sof-firmware"], index["sys-kernel/installkernel"])
        self.assertLess(index["sys-kernel/installkernel"], index["nvidia-drivers"])
        self.assertLess(index["nvidia-drivers"], index["--regenerate-all"])
        self.assertEqual(read(self.target, "etc/modprobe.d/nvidia.conf"), "blacklist nouveau\n")
        self.assertIn("sys-fs/dosfstools", lines[index["sys-fs/xfsprogs"]])

    def test_a_driver_that_fails_to_build_degrades_instead_of_failing(self) -> None:
        installer = self.prepare(plan=FakePlan(driver_atoms=["x11-drivers/nvidia-drivers"]))
        self.runner.script("*nvidia-drivers", ("fail", "build failed"))
        installer._kernel()
        self.assertTrue(any("nvidia-drivers" in item for item in installer.degraded))
        self.assertTrue(any(line.startswith("dracut --force") for line in self.chroot_lines()))

    def test_manual_kernel_is_a_command_line_feature_with_clear_instructions(self) -> None:
        installer = self.prepare(m=target_manifest(system={"kernel": "manual"}))
        with self.assertRaisesRegex(InstallError, "chroot into that directory"):
            installer._kernel()

    def test_only_the_dist_kernel_flow_remains(self) -> None:
        installer = self.prepare()
        installer._kernel()
        self.assertFalse(any("genkernel" in line for line in self.chroot_lines()))
        self.assertTrue(any("--regenerate-all" in line for line in self.chroot_lines()))

    def test_extra_kernel_arguments_end_up_in_the_boot_configuration(self) -> None:
        installer = self.prepare(m=target_manifest(system={"kernel_cmdline_extra": "console=ttyS0,115200"}))
        installer._kernel()
        self.assertEqual(read(self.target, "etc/kernel/cmdline"), f"root=UUID={UUID} rw console=ttyS0,115200\n")


class BaseAndDesktopStageTests(StageHarness):
    def test_base_installs_the_essentials_and_their_services_only(self) -> None:
        installer = self.make(target_manifest(system={"desktop": "kde"}, options={"ssh": True}))
        installer._base()
        lines = self.chroot_lines()
        emerge = next(line for line in lines if line.startswith("emerge"))
        self.assertIn("net-misc/networkmanager", emerge)
        self.assertIn("app-admin/sudo", emerge)
        self.assertNotIn("kde-plasma/plasma-meta", emerge)
        self.assertNotIn("--keep-going", emerge)
        self.assertIn("rc-update add NetworkManager default", lines)
        self.assertIn("rc-update add sshd default", lines)
        self.assertFalse(any("display-manager" in line or "sddm" in line for line in lines))

    def test_a_failing_base_stage_fails_the_install_with_the_reason(self) -> None:
        runner = ScriptedRunner()
        runner.script("*emerge", ("fail", EmergeDiagnosisTests.USE_CHANGES))
        installer = self.make(runner=runner)
        with self.assertRaisesRegex(InstallError, "base system packages.*qtpositioning"):
            installer._base()

    def test_desktop_installs_the_environment_then_enables_its_services(self) -> None:
        installer = self.make(target_manifest(system={"desktop": "xfce"}, options={"bluetooth": False, "printing": False, "pipewire": False}))
        for name in ("dbus", "elogind", "display-manager"):
            (self.target / "etc/init.d").mkdir(parents=True, exist_ok=True)
            (self.target / "etc/init.d" / name).write_text("x")
        installer._desktop()
        lines = self.chroot_lines()
        emerge = next(i for i, line in enumerate(lines) if "xfce-base/xfce4-meta" in line)
        self.assertIn("--keep-going", lines[emerge])
        for service in ("dbus default", "elogind boot", "display-manager default"):
            self.assertGreater(lines.index(f"rc-update add {service}"), emerge)
        self.assertEqual(read(self.target, "etc/conf.d/display-manager"), 'DISPLAYMANAGER="lightdm"\n')
        self.assertIn("OnlyShowIn=XFCE;", read(self.target, "etc/xdg/autostart/gentoo-installer-polkit-agent.desktop"))
        self.assertEqual(installer.degraded, [])

    def test_a_desktop_that_cannot_be_built_leaves_a_bootable_system_and_says_so(self) -> None:
        runner = ScriptedRunner()
        for _ in range(3):
            runner.script("*emerge", ("fail", " * 1) gnome-base/gdm-48::gentoo (/log)"))
        installer = self.make(target_manifest(system={"desktop": "gnome"}), runner)
        with self.assertRaises(InstallError) as context:
            installer._desktop()
        self.assertTrue(context.exception.bootable)
        self.assertIn("bootable", str(context.exception))
        self.assertTrue(any("only the desktop failed" in message for message in self.logs()))

    def test_optional_failures_are_reported_as_warnings_and_the_stage_succeeds(self) -> None:
        runner = ScriptedRunner()
        # the optional emerge (pipewire) fails once in the group, once alone
        runner.script("*pipewire", ("fail", "!!! Fetch failed for pipewire"), ("fail", "!!! Fetch failed for pipewire"),
                      ("fail", "!!! Fetch failed for pipewire"))
        installer = self.make(target_manifest(system={"desktop": "minimal"}, options={"pipewire": True, "bluetooth": False, "printing": False}), runner)
        installer._desktop()
        self.assertEqual(len(installer.degraded), 1)
        self.assertIn("media-video/pipewire could not be installed", installer.degraded[0])
        progress = [event for event in self.events if event.type == "progress" and event.stage == "desktop"]
        self.assertEqual(progress[-1].data["bootable"], True)
        self.assertEqual(progress[-1].data["degraded"], installer.degraded)
        self.assertTrue(any(message.startswith("WARNING: media-video/pipewire") for message in self.logs()))

    def test_plan_steps_that_fail_are_warnings(self) -> None:
        runner = ScriptedRunner()
        runner.script("*usermod", ("fail", "group pipewire does not exist"))
        plan = FakePlan(post_steps=[["usermod", "-aG", "pipewire", "gentoo"]])
        installer = self.make(target_manifest(system={"desktop": "minimal"}, options={"pipewire": False, "bluetooth": False, "printing": False}), runner, plan)
        installer._desktop()
        self.assertTrue(any("usermod -aG pipewire gentoo failed" in item for item in installer.degraded))


class BootloaderAndValidationTests(StageHarness):
    def prepare(self, m=None, **kwargs) -> Installer:
        runner = ScriptedRunner()
        runner.respond("blkid -s UUID -o value /dev/sda3", UUID + "\n")
        installer = self.make(m, runner, **kwargs)
        (self.target / "boot").mkdir()
        (self.target / "boot/vmlinuz-6.12.0").write_bytes(b"kernel")
        (self.target / "boot/initramfs-6.12.0.img").write_bytes(b"initramfs")
        return installer

    def finish_install(self, installer: Installer) -> None:
        """What the target looks like after the earlier stages succeeded."""
        (self.target / "etc").mkdir(exist_ok=True)
        (self.target / "etc/fstab").write_text(f"UUID={UUID} / ext4 defaults,noatime 0 1\n")
        (self.target / "etc/portage").mkdir(parents=True, exist_ok=True)
        (self.target / "etc/portage/make.conf").write_text("x\n")
        (self.target / "boot/grub").mkdir(exist_ok=True)
        (self.target / "boot/grub/grub.cfg").write_text("menuentry 'Gentoo' {\n linux /vmlinuz-6.12.0 root=UUID=x ro\n}\n")
        (self.target / "efi/EFI/Gentoo").mkdir(parents=True, exist_ok=True)
        (self.target / "efi/EFI/Gentoo/grubx64.efi").write_bytes(b"grub")

    def test_the_bootloader_stage_installs_grub_and_records_what_it_did(self) -> None:
        installer = self.prepare()
        installer._bootloader()
        lines = self.chroot_lines()
        self.assertEqual(lines[0], "emerge --noreplace sys-boot/grub")
        self.assertTrue(any(line.startswith("grub-install --target=x86_64-efi") for line in lines))
        receipt = (self.target / RECEIPT_PATH).read_text()
        self.assertIn(f'"uuid": "{UUID}"', receipt)

    def test_dual_boot_installs_os_prober_first(self) -> None:
        installer = self.prepare(target_manifest(system={"dual_boot": True}))
        installer._bootloader()
        self.assertEqual(self.chroot_lines()[0], "emerge --noreplace sys-boot/grub sys-boot/os-prober")

    def test_the_stage_is_only_complete_with_a_receipt_and_an_efi_image(self) -> None:
        installer = self.prepare()
        self.finish_install(installer)
        self.assertFalse(installer._verify_bootloader())
        installer.configurer.write_receipt(installer.manifest, "/dev/sda3", LAYOUT)
        self.assertTrue(installer._verify_bootloader())
        (self.target / "efi/EFI/Gentoo/grubx64.efi").unlink()
        self.assertFalse(installer._verify_bootloader())
        fallback = self.target / "efi/EFI/BOOT"
        fallback.mkdir()
        (fallback / "BOOTX64.EFI").write_bytes(b"grub")
        self.assertTrue(installer._verify_bootloader())

    def test_validation_passes_keeps_the_log_and_hands_resolv_conf_to_resolved(self) -> None:
        m = target_manifest(system={"init": "systemd", "stage_variant": "amd64-systemd", "desktop": "minimal"}, network={"manager": "systemd-networkd"})
        installer = self.prepare(m)
        self.finish_install(installer)
        (self.target / "etc/resolv.conf").write_text("nameserver 10.0.0.1\n")
        installer._validate()
        self.assertEqual(os.readlink(self.target / "etc/resolv.conf"), "../run/systemd/resolve/stub-resolv.conf")
        kept = self.target / "var/log/gentoo-installer"
        self.assertEqual(sorted(path.name for path in kept.iterdir()), ["install.log", "manifest.json"])
        self.assertEqual((kept / "install.log").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("user-password", (kept / "manifest.json").read_text())

    def test_validation_does_not_touch_resolv_conf_when_it_fails(self) -> None:
        m = target_manifest(system={"init": "systemd", "stage_variant": "amd64-systemd", "desktop": "minimal"}, network={"manager": "systemd-networkd"})
        installer = self.prepare(m)
        self.finish_install(installer)
        (self.target / "boot/vmlinuz-6.12.0").unlink()
        (self.target / "etc/resolv.conf").write_text("nameserver 10.0.0.1\n")
        with self.assertRaisesRegex(InstallError, "No kernel image was installed"):
            installer._validate()
        self.assertFalse((self.target / "etc/resolv.conf").is_symlink())

    def test_validation_lists_every_problem_it_found(self) -> None:
        installer = self.prepare(target_manifest(storage={"luks": True, "luks_passphrase": "long-enough-passphrase"}))
        installer.storage.layout = [LayoutEntry("/dev/mapper/gentoo-root", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat")]
        self.finish_install(installer)
        (self.target / "etc/fstab").write_text("/dev/other / ext4 defaults 0 1\n")
        with self.assertRaises(InstallError) as context:
            installer._validate()
        message = str(context.exception)
        self.assertIn("failed its final checks", message)
        self.assertIn("/etc/fstab does not mention the root filesystem", message)
        self.assertIn("grub.cfg does not pass rd.luks.uuid", message)

    def test_an_initramfs_without_the_crypt_module_fails_validation(self) -> None:
        installer = self.prepare(target_manifest(storage={"luks": True, "luks_passphrase": "long-enough-passphrase"}))
        installer.storage.layout = [LayoutEntry("/dev/mapper/gentoo-root", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat")]
        self.finish_install(installer)
        (self.target / "boot/grub/grub.cfg").write_text("menuentry 'x' {\n linux /vmlinuz-6 root=UUID=x ro rd.luks.uuid=u\n}\n")
        (self.target / "etc/fstab").write_text(f"/dev/mapper/gentoo-root / ext4 defaults 0 1\n")
        self.runner.respond("chroot", "dracut modules:\nbash\nrootfs-block\n")
        self.runner.respond("blkid -s UUID -o value /dev/mapper/gentoo-root", UUID + "\n")
        (self.target / "etc/fstab").write_text(f"UUID={UUID} / ext4 defaults 0 1\n")
        with self.assertRaisesRegex(InstallError, "lacks the crypt module"):
            installer._validate()


class Stage3StageTests(StageHarness):
    dry_run = False

    def test_proxy_mirrors_and_mount_points_are_passed_to_the_downloader(self) -> None:
        class Capturing:
            notify = None

            def install(self, *args, **kwargs):
                self.args, self.kwargs = args, kwargs

        capture = Capturing()
        installer = self.make(target_manifest(network={"proxy": "http://proxy.example:3128"},
                                              portage={"mirror_fallbacks": ["https://mirror.example/gentoo"]}), stage3=capture)
        installer._stage3()
        self.assertEqual(capture.args[:3], ("https://distfiles.gentoo.org", "amd64-openrc", installer.target_root))
        self.assertEqual(capture.kwargs["proxy"], "http://proxy.example:3128")
        self.assertEqual(capture.kwargs["fallback_mirrors"], ("https://mirror.example/gentoo",))
        self.assertEqual(capture.kwargs["allowed_existing"], ["/", "/efi"])
        self.assertEqual(capture.notify, installer.log)

    def test_plain_installs_pass_only_the_mount_points(self) -> None:
        class Capturing:
            def install(self, *args, **kwargs):
                self.kwargs = kwargs

        capture = Capturing()
        installer = self.make(stage3=capture)
        installer._stage3()
        self.assertEqual(set(capture.kwargs), {"allowed_existing"})


class PlanSeamTests(unittest.TestCase):
    def installer(self, temporary: str) -> Installer:
        return Installer(target_manifest(), runner=ScriptedRunner(), target_root=Path(temporary) / "t", state_dir=Path(temporary) / "s",
                         dry_run=True, inspector=FakeInspector())

    def test_installer_without_a_plan_behaves_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer = self.installer(temporary)
            self.assertIsNone(installer._plan())
            installer.hardware_plan = FakePlan()
            self.assertIsNotNone(installer._plan())

    def test_the_plan_is_made_from_the_probe_when_the_hardware_module_exists(self) -> None:
        import sys
        import types

        calls: list = []
        module = types.ModuleType("gentoo_installerd.hwplan")
        module.plan = lambda manifest, hardware: (calls.append((manifest, hardware)), FakePlan(summary=["Graphics: open drivers"]))[1]
        with tempfile.TemporaryDirectory() as temporary:
            installer = self.installer(temporary)
            installer.inspection = types.SimpleNamespace(hardware={"cpu": {"vendor": "intel"}})
            with unittest.mock.patch.dict(sys.modules, {"gentoo_installerd.hwplan": module}):
                with unittest.mock.patch("gentoo_installerd.hwplan", module, create=True):
                    plan = installer._plan()
                    self.assertEqual(plan.summary, ["Graphics: open drivers"])
                    self.assertIs(installer._plan(), plan)  # made once
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], {"cpu": {"vendor": "intel"}})

    def test_a_planning_bug_falls_back_to_the_generic_setup(self) -> None:
        import sys
        import types

        module = types.ModuleType("gentoo_installerd.hwplan")

        def broken(manifest, hardware):
            raise RuntimeError("table lookup failed")

        module.plan = broken
        with tempfile.TemporaryDirectory() as temporary:
            installer = self.installer(temporary)
            logs: list[str] = []
            installer.log = logs.append  # type: ignore[method-assign]
            installer.inspection = types.SimpleNamespace(hardware={"cpu": {}})
            with unittest.mock.patch.dict(sys.modules, {"gentoo_installerd.hwplan": module}):
                with unittest.mock.patch("gentoo_installerd.hwplan", module, create=True):
                    self.assertIsNone(installer._plan())
            self.assertTrue(any("could not be planned (table lookup failed)" in line for line in logs))

    def test_no_probe_data_means_no_plan(self) -> None:
        import types

        with tempfile.TemporaryDirectory() as temporary:
            installer = self.installer(temporary)
            installer.inspection = types.SimpleNamespace(hardware={})
            self.assertIsNone(installer._plan())


if __name__ == "__main__":
    unittest.main()
