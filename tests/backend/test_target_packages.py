from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gentoo_installerd.configure import (
    DESKTOP_USE,
    ConfigurationError,
    PackageInstallError,
    Resources,
    auto_emerge_jobs,
    auto_make_opts,
    explain_emerge_failure,
    machine_resources,
    render_make_conf,
    render_package_use,
)
from gentoo_installerd.storage import LayoutEntry

from tests.backend.target_helpers import FakePlan, ScriptedRunner, configurer, mark_installed, read, target_manifest


class MachineSizedBuildTests(unittest.TestCase):
    def test_make_opts_follow_cpu_count_and_memory(self) -> None:
        gib = 1024**3
        for cpus, available, swap, expected in (
            (1, 1 * gib, 0, "-j1 -l1"),
            (2, 1.2 * gib, 4096, "-j1 -l2"),
            (4, 2.6 * gib, 4096, "-j2 -l4"),
            (8, 6.4 * gib, 8192, "-j5 -l8"),
            (16, 14 * gib, 8192, "-j9 -l16"),
            (32, 44.5 * gib, 8192, "-j24 -l32"),
            (64, 250 * gib, 8192, "-j32 -l64"),
        ):
            with self.subTest(cpus=cpus):
                self.assertEqual(auto_make_opts(cpus, int(available), swap), expected)

    def test_parallel_emerge_only_on_roomy_machines(self) -> None:
        gib = 1024**3
        self.assertEqual([auto_emerge_jobs(2, 2 * gib), auto_emerge_jobs(4, 8 * gib), auto_emerge_jobs(8, 15 * gib),
                          auto_emerge_jobs(16, 62 * gib), auto_emerge_jobs(32, 46 * gib)], [1, 1, 2, 3, 2])

    def test_unreadable_meminfo_degrades_to_a_small_machine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            resources = machine_resources(Path(temporary, "missing"))
            self.assertEqual((resources.mem_total, resources.mem_available), (0, 0))
            self.assertEqual(auto_make_opts(resources.cpus, resources.mem_available), f"-j1 -l{resources.cpus}")
            meminfo = Path(temporary, "meminfo")
            meminfo.write_text("MemTotal:       16384000 kB\nMemFree: 1 kB\nMemAvailable:   8192000 kB\n")
            resources = machine_resources(meminfo)
            self.assertEqual((resources.mem_total, resources.mem_available), (16384000 * 1024, 8192000 * 1024))

    def test_explicit_make_opts_win_over_the_plan_and_the_machine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            c = configurer(temporary)
            m = target_manifest()
            self.assertEqual(c.effective_make_opts(m), "-j7 -l8")  # 12 GiB available + half the swap, 8 CPUs
            plan = FakePlan(make_conf={"MAKEOPTS": "-j3 -l4"})
            self.assertEqual(c.effective_make_opts(m, plan), "-j3 -l4")
            m.portage.make_opts = "-j2"
            self.assertEqual(c.effective_make_opts(m, plan), "-j2")
            c.configure_portage(m, plan)
            self.assertIn("MAKEOPTS=-j2\n", read(Path(temporary), "etc/portage/make.conf"))

    def test_emerge_environment_carries_proxy_and_parallel_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            c = configurer(temporary)
            m = target_manifest(network={"proxy": "http://user:secret@proxy.example:3128"})
            c.resources = lambda: Resources(16, 32 * 1024**3, 30 * 1024**3)
            environment = c.emerge_environment(m)
            self.assertEqual(environment["https_proxy"], "http://user:secret@proxy.example:3128")
            self.assertEqual(environment["EMERGE_DEFAULT_OPTS"], "--ask=n --verbose=y --jobs=2 --load-average=16")
            m.portage.emerge_opts = "--ask=n --jobs=1"
            self.assertNotIn("EMERGE_DEFAULT_OPTS", c.emerge_environment(m))
            c.resources = lambda: Resources(2, 4 * 1024**3, 3 * 1024**3)
            m.portage.emerge_opts = "--ask=n"
            self.assertEqual(c.emerge_environment(target_manifest()), {})


class MakeConfPlanTests(unittest.TestCase):
    def test_plan_values_fill_empty_keys_without_widening_licences(self) -> None:
        m = target_manifest()
        plan = FakePlan(make_conf={"CPU_FLAGS_X86": "${CPU_FLAGS_X86} aes avx2", "VIDEO_CARDS": "${VIDEO_CARDS} nvidia",
                                   "ACCEPT_LICENSE": "*", "L10N": "de", "EMERGE_DEFAULT_OPTS": "--jobs=2", "EMPTY": "",
                                   "bad key": "x"})
        text = render_make_conf(m, plan, make_opts="-j4 -l8")
        self.assertIn("CPU_FLAGS_X86='${CPU_FLAGS_X86} aes avx2'", text)
        self.assertIn("VIDEO_CARDS='${VIDEO_CARDS} nvidia'", text)
        self.assertIn("L10N=de", text)
        self.assertIn("ACCEPT_LICENSE=@FREE", text)
        self.assertNotIn("ACCEPT_LICENSE=*", text)
        self.assertIn("EMERGE_DEFAULT_OPTS='--ask=n --verbose=y --jobs=2'", text)
        self.assertNotIn("EMPTY", text)
        self.assertNotIn("bad key", text)

    def test_proxy_without_credentials_is_kept_in_make_conf_but_credentials_are_not(self) -> None:
        plain = render_make_conf(target_manifest(network={"proxy": "http://proxy.example:3128"}))
        self.assertIn("http_proxy=http://proxy.example:3128", plain)
        secret = render_make_conf(target_manifest(network={"proxy": "http://user:secret@proxy.example:3128"}))
        self.assertNotIn("secret", secret)
        self.assertNotIn("proxy", secret)

    def test_package_use_accepts_use_expand_lines(self) -> None:
        text = render_package_use({"*/*": ["VIDEO_CARDS:", "virgl", "-nouveau"], "app/one": ["ssl"]})
        self.assertEqual(text, "*/* VIDEO_CARDS: virgl -nouveau\napp/one ssl\n")
        for bad in ({"*/foo": ["x"]}, {"a/b": ["video_cards:"]}, {"a/b": ["x y"]}):
            with self.subTest(bad=bad), self.assertRaises(ConfigurationError):
                render_package_use(bad)


class PackageUseTableTests(unittest.TestCase):
    def table(self, **sections):
        with tempfile.TemporaryDirectory() as temporary:
            return configurer(temporary).package_use_table(target_manifest(**sections))

    def test_desktop_use_table_applied(self) -> None:
        for desktop, entries in DESKTOP_USE.items():
            with self.subTest(desktop=desktop):
                table = self.table(system={"desktop": desktop})
                for atom, flags in entries.items():
                    self.assertEqual(table[atom], list(flags))
        minimal = self.table(system={"desktop": "minimal"})
        for entries in DESKTOP_USE.values():
            for atom in entries:
                self.assertNotIn(atom, minimal)
        self.assertEqual(DESKTOP_USE["kde"], {"dev-qt/qtpositioning": ("geoclue",)})
        self.assertEqual(DESKTOP_USE["gnome"], {"net-libs/ngtcp2": ("gnutls",)})
        self.assertEqual(DESKTOP_USE["xfce"], {"dev-libs/libdbusmenu": ("gtk3",)})

    def test_user_flags_cannot_remove_what_the_selected_packages_need(self) -> None:
        table = self.table(system={"desktop": "kde"}, portage={"package_use": {"dev-qt/qtpositioning": ["-geoclue", "gps"]}})
        self.assertEqual(table["dev-qt/qtpositioning"], ["gps", "geoclue"])

    def test_doas_is_built_with_the_persist_keyword_it_is_configured_with(self) -> None:
        self.assertEqual(self.table(accounts={"privilege": "doas"})["app-admin/doas"], ["persist"])
        self.assertNotIn("app-admin/doas", self.table(accounts={"privilege": "sudo"}))

    def test_grub_installkernel_regenerates_the_boot_menu_on_kernel_updates(self) -> None:
        self.assertEqual(self.table()["sys-kernel/installkernel"], ["dracut", "grub"])
        self.assertEqual(self.table(system={"bootloader": "systemd-boot"})["sys-kernel/installkernel"], ["dracut"])
        self.assertEqual(self.table(system={"bootloader": "efi-stub"})["sys-kernel/installkernel"], ["dracut"])

    def test_dual_boot_lets_grub_read_other_filesystems(self) -> None:
        self.assertEqual(self.table(system={"dual_boot": True})["sys-boot/grub"], ["mount"])
        self.assertNotIn("sys-boot/grub", self.table())
        self.assertNotIn("sys-boot/grub", self.table(system={"dual_boot": True, "bootloader": "systemd-boot"}))

    def test_hardware_plan_flags_go_into_their_own_file_after_the_installer_file(self) -> None:
        plan = FakePlan(package_use={"*/*": ["VIDEO_CARDS:", "virgl"], "media-libs/mesa": ["-vulkan"]},
                        package_license={"x11-drivers/nvidia-drivers": ["NVIDIA-2025"]}, package_mask=[">=x11-drivers/nvidia-drivers-581"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configurer(root).configure_portage(target_manifest(), plan)
            self.assertEqual(read(root, "etc/portage/package.use/installer-hardware"), "*/* VIDEO_CARDS: virgl\nmedia-libs/mesa -vulkan\n")
            self.assertEqual(read(root, "etc/portage/package.license/installer-hardware"), "x11-drivers/nvidia-drivers NVIDIA-2025\n")
            self.assertEqual(read(root, "etc/portage/package.mask/installer-hardware"), ">=x11-drivers/nvidia-drivers-581\n")
            self.assertGreater("installer-hardware", "installer")  # Portage reads the files in this order
            without = Path(temporary, "without")
            configurer(without).configure_portage(target_manifest())
            self.assertFalse((without / "etc/portage/package.use/installer-hardware").exists())


class PackageSetTests(unittest.TestCase):
    def sets(self, **sections):
        with tempfile.TemporaryDirectory() as temporary:
            c = configurer(temporary)
            m = target_manifest(**sections)
            return c.essential_atoms(m), c.desktop_atoms(m), c.package_atoms(m)

    def test_stage_sets_partition_the_package_list(self) -> None:
        for desktop in ("kde", "gnome", "xfce", "minimal"):
            for init in ("openrc", "systemd"):
                with self.subTest(desktop=desktop, init=init):
                    essential, desktop_atoms, everything = self.sets(
                        system={"desktop": desktop, "init": init, "stage_variant": f"amd64-{init}"},
                        options={"install_hwmonux": True, "install_portagehub": True, "ssh": True, "flatpak": True,
                                 "extra_packages": ["app-editors/vim"]})
                    self.assertTrue(set(essential) <= set(everything))
                    self.assertTrue(set(desktop_atoms.everything()) <= set(everything))
                    self.assertFalse(set(essential) & set(desktop_atoms.everything()))
                    self.assertIn("app-admin/sudo", essential)
                    self.assertIn("net-misc/networkmanager", essential)
                    self.assertIn("app-misc/hwmonux", desktop_atoms.optional)
                    self.assertIn("app-portage/portagehub", desktop_atoms.optional)
                    self.assertEqual(desktop_atoms.extras, ["app-editors/vim"])
                    self.assertEqual(bool(desktop_atoms.required), desktop != "minimal")

    def test_everything_the_installer_used_to_install_is_still_installed(self) -> None:
        # The union of the new sets covers the previous single package list (CRITIC-2).
        _, _, kde = self.sets(system={"desktop": "kde"}, options={"kde_apps": "full"})
        for atom in ("net-misc/networkmanager", "sys-kernel/linux-firmware", "sys-firmware/intel-microcode",
                     "kde-plasma/plasma-meta", "kde-apps/kde-apps-meta", "gui-apps/sddm", "x11-base/xorg-server",
                     "sys-power/power-profiles-daemon", "gui-libs/display-manager-init", "media-video/pipewire",
                     "net-wireless/bluez", "net-wireless/blueman", "net-print/cups", "net-misc/chrony",
                     "sys-process/cronie", "app-shells/bash-completion", "app-admin/syslog-ng", "app-admin/sudo"):
            self.assertIn(atom, kde)

    def test_kde_applications_default_to_the_core_set(self) -> None:
        _, core, _ = self.sets(system={"desktop": "kde"})
        self.assertIn("kde-apps/kdecore-meta", core.required)
        self.assertNotIn("kde-apps/kde-apps-meta", core.required)
        _, full, _ = self.sets(system={"desktop": "kde"}, options={"kde_apps": "full"})
        self.assertIn("kde-apps/kde-apps-meta", full.required)
        self.assertNotIn("kde-apps/kdecore-meta", full.required)

    def test_graphical_only_packages_are_not_installed_on_minimal_systems(self) -> None:
        _, minimal, _ = self.sets(system={"desktop": "minimal"}, options={"bluetooth": True})
        self.assertIn("net-wireless/bluez", minimal.optional)
        self.assertNotIn("net-wireless/blueman", minimal.optional)
        self.assertNotIn("sys-fs/ntfs3g", minimal.optional)
        _, kde, _ = self.sets(system={"desktop": "kde"}, options={"bluetooth": True})
        self.assertIn("net-wireless/blueman", kde.optional)
        self.assertIn("sys-fs/ntfs3g", kde.optional)

    def test_openrc_logger_brings_a_log_rotator_and_a_cron_daemon(self) -> None:
        essential, _, _ = self.sets(options={"logger": True, "cron": False})
        self.assertIn("app-admin/syslog-ng", essential)
        self.assertIn("app-admin/logrotate", essential)
        self.assertIn("sys-process/cronie", essential)
        essential, _, _ = self.sets(system={"init": "systemd", "stage_variant": "amd64-systemd"}, options={"logger": True, "cron": False})
        self.assertNotIn("app-admin/logrotate", essential)
        self.assertNotIn("sys-process/cronie", essential)
        essential, _, _ = self.sets(options={"logger": False, "cron": False})
        self.assertNotIn("sys-process/cronie", essential)
        self.assertNotIn("app-admin/syslog-ng", essential)

    def test_xfce_always_gets_a_polkit_agent(self) -> None:
        for init in ("openrc", "systemd"):
            for multilib in (True, False):
                _, xfce, _ = self.sets(system={"desktop": "xfce", "init": init, "stage_variant": f"amd64-{init}", "multilib": multilib})
                self.assertIn("gnome-extra/polkit-gnome", xfce.required)
        _, kde, _ = self.sets(system={"desktop": "kde"})
        self.assertNotIn("gnome-extra/polkit-gnome", kde.required)

    def test_filesystem_userland_matches_the_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            c = configurer(temporary)
            m = target_manifest(storage={"filesystem": "btrfs"})
            layout = [LayoutEntry("/dev/sda3", "/", "btrfs"), LayoutEntry("/dev/sda1", "/efi", "vfat")]
            self.assertEqual(c.filesystem_atoms(m, layout), ["sys-fs/dosfstools", "sys-fs/btrfs-progs"])
            m = target_manifest(storage={"filesystem": "xfs", "firmware": "bios"})
            self.assertEqual(c.filesystem_atoms(m, [LayoutEntry("/dev/sda2", "/", "xfs")]), ["sys-fs/xfsprogs"])
            m = target_manifest(storage={"firmware": "bios"})
            self.assertEqual(c.filesystem_atoms(m, [LayoutEntry("/dev/sda2", "/", "ext4")]), [])
            self.assertEqual(c.filesystem_atoms(target_manifest(), [LayoutEntry("/dev/sda3", "/", "ext4")]), ["sys-fs/dosfstools"])

    def test_kernel_stage_batch_has_storage_firmware_and_filesystem_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            c = configurer(temporary)
            m = target_manifest(storage={"luks": True, "lvm": True, "luks_passphrase": "long-enough-passphrase"})
            plan = FakePlan(early_atoms=["sys-firmware/sof-firmware"])
            batch = c.kernel_stage_atoms(m, [LayoutEntry("/dev/mapper/gentoo-root", "/", "ext4")], plan)
            self.assertEqual(batch[:2], ["sys-fs/cryptsetup", "sys-fs/lvm2"])
            for atom in ("sys-kernel/linux-firmware", "sys-firmware/sof-firmware", "sys-fs/dosfstools"):
                self.assertIn(atom, batch)

    def test_hardware_plan_atoms_join_the_stage_sets(self) -> None:
        plan = FakePlan(early_atoms=["sys-firmware/sof-firmware"], required_atoms=["sys-power/tlp"],
                        optional_atoms=[("Guest tools", ["app-emulation/qemu-guest-agent"]), "sys-power/thermald"])
        with tempfile.TemporaryDirectory() as temporary:
            c = configurer(temporary)
            m = target_manifest(system={"desktop": "xfce"})
            self.assertIn("sys-firmware/sof-firmware", c.early_boot_atoms(m, plan))
            self.assertIn("sys-power/tlp", c.desktop_atoms(m, plan).required)
            groups = dict(c.desktop_atoms(m, plan).groups)
            self.assertEqual(groups["Guest tools"], ["app-emulation/qemu-guest-agent"])
            self.assertEqual(groups["sys-power/thermald"], ["sys-power/thermald"])
            self.assertIn("app-emulation/qemu-guest-agent", c.package_atoms(m, plan))

    def test_companion_archives_are_found_by_pattern_not_by_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary, "bundle")
            (bundle / "distfiles").mkdir(parents=True)
            c = configurer(temporary, companion_bundle=bundle)
            with self.assertRaisesRegex(ConfigurationError, "found: none"):
                c.companion_archive("hwmonux")
            (bundle / "distfiles/hwmonux-2.3.4.tar.zst").write_bytes(b"x")
            self.assertEqual(c.companion_archive("hwmonux").name, "hwmonux-2.3.4.tar.zst")
            (bundle / "distfiles/hwmonux-2.3.5.tar.zst").write_bytes(b"x")
            with self.assertRaisesRegex(ConfigurationError, "hwmonux-2.3.4.tar.zst, hwmonux-2.3.5.tar.zst"):
                c.companion_archive("hwmonux")


class EmergeDiagnosisTests(unittest.TestCase):
    USE_CHANGES = """
Calculating dependencies... done!
The following USE changes are necessary to proceed:
 (see "package.use" in the portage(5) man page for more details)
# required by kde-misc/kweather-26.04.3::gentoo
# required by kde-apps/kdeutils-meta-26.04.3::gentoo
# required by kde-apps/kde-apps-meta (argument)
>=dev-qt/qtpositioning-6.11.2 geoclue

"""
    REQUIRED_USE = """
!!! The ebuild selected to satisfy ">=media-libs/opencv-2.3.0:0/4.12.0=[contrib]" has unmet requirements.
- media-libs/opencv-4.12.0-r1::gentoo USE="avif contrib" ABI_X86="(64)"

  The following REQUIRED_USE flag constraints are unsatisfied:
    amd64? ( cpu_flags_x86_sse cpu_flags_x86_sse2 )

(dependency required by "media-plugins/frei0r-plugins-2.4.0::gentoo" [ebuild])
"""

    def test_use_changes_name_the_flag_and_the_package_that_needs_it(self) -> None:
        diagnosis = explain_emerge_failure(self.USE_CHANGES)
        self.assertTrue(diagnosis.resolution)
        self.assertIn("USE change needed: >=dev-qt/qtpositioning-6.11.2 geoclue (required by kde-misc/kweather-26.04.3)", diagnosis.summary)

    def test_required_use_failures_name_the_package_and_the_constraint(self) -> None:
        diagnosis = explain_emerge_failure(self.REQUIRED_USE)
        self.assertTrue(diagnosis.resolution)
        self.assertIn("media-libs/opencv-4.12.0-r1::gentoo has unsatisfiable REQUIRED_USE: amd64? ( cpu_flags_x86_sse", diagnosis.summary)

    def test_masked_missing_blocked_and_failed_packages_are_recognised(self) -> None:
        masked = explain_emerge_failure('!!! All ebuilds that could satisfy "dev-qt/qtfoo" have been masked.\n'
                                        '!!! One of the following masked packages is required to complete your request:\n'
                                        '- dev-qt/qtfoo-6.0::gentoo (masked by: ~amd64 keyword)\n')
        self.assertTrue(masked.resolution)
        self.assertIn('dev-qt/qtfoo is masked (~amd64 keyword)', masked.summary)
        self.assertIn("no package matches app-misc/nonexistent", explain_emerge_failure('emerge: there are no ebuilds to satisfy "app-misc/nonexistent".').summary)
        self.assertTrue(explain_emerge_failure("[blocks B      ] sys-apps/a (\"sys-apps/a\" is soft blocking sys-apps/b-1)\nError: The above package list contains packages which cannot be installed at the same time on the same system.").resolution)
        failed = explain_emerge_failure(" * The following 2 packages failed to build, install, or execute postinst:\n"
                                        " * 1) dev-qt/qtwebengine-6.11.2::gentoo (/var/tmp/portage/x/log)\n * 2) www-client/falkon-26.04.3::gentoo (/var/tmp/portage/y)\n")
        self.assertFalse(failed.resolution)
        self.assertIn("failed to build: dev-qt/qtwebengine-6.11.2, www-client/falkon-26.04.3", failed.summary)
        fetch = explain_emerge_failure("!!! Fetch failed for foo-1.tar.gz, continuing...")
        self.assertFalse(fetch.resolution)
        self.assertIn("download failed for foo-1.tar.gz", fetch.summary)

    def test_unrecognised_output_falls_back_to_its_last_lines(self) -> None:
        diagnosis = explain_emerge_failure("a\nb\n\nsomething odd happened\nreally odd\n")
        self.assertFalse(diagnosis.resolution)
        self.assertEqual(diagnosis.summary, "b | something odd happened | really odd")


class PretendTests(unittest.TestCase):
    def test_a_resolution_failure_stops_the_install_with_the_reason(self) -> None:
        runner = ScriptedRunner()
        runner.script("chroot", ("fail", EmergeDiagnosisTests.USE_CHANGES))
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(PackageInstallError) as context:
                configurer(temporary, runner).pretend(target_manifest(), ["kde-apps/kde-apps-meta"])
            self.assertTrue(context.exception.resolution)
            self.assertIn("qtpositioning", str(context.exception))
        command = runner.commands[0]
        self.assertEqual(command[:3], ("chroot", temporary, "emerge"))
        for flag in ("--pretend", "--noreplace"):
            self.assertIn(flag, command)
        self.assertNotIn("--usepkg=n", command)  # predicts the real install, binary packages included
        self.assertEqual(command[-1], "kde-apps/kde-apps-meta")

    def test_other_failures_are_only_warnings(self) -> None:
        runner = ScriptedRunner()
        runner.script("chroot", ("fail", "Temporary failure in name resolution"))
        notes: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            c = configurer(temporary, runner)
            c.notify = notes.append
            c.pretend(target_manifest(), ["app-admin/sudo"])
        self.assertTrue(any("could not run" in note for note in notes))

    def test_nothing_to_resolve_runs_nothing(self) -> None:
        runner = ScriptedRunner()
        with tempfile.TemporaryDirectory() as temporary:
            configurer(temporary, runner).pretend(target_manifest(), [])
        self.assertEqual(runner.commands, [])


class StageInstallTests(unittest.TestCase):
    """The base stage must succeed; the desktop stage degrades instead of failing the install."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runner = ScriptedRunner()
        self.c = configurer(self.root, self.runner)
        self.notes: list[str] = []
        self.c.notify = self.notes.append

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def emerges(self) -> list[str]:
        return [line.removeprefix(f"chroot {self.root} emerge ") for line in self.runner.lines() if line.startswith(f"chroot {self.root} emerge")]

    def test_essential_packages_are_one_plain_emerge_and_failures_are_fatal(self) -> None:
        m = target_manifest(options={"ssh": True})
        self.c.install_essential(m)
        self.assertEqual(len(self.emerges()), 1)
        self.assertTrue(self.emerges()[0].startswith("--noreplace "))
        self.assertNotIn("--keep-going", self.emerges()[0])
        self.assertIn("net-misc/openssh", self.emerges()[0])
        self.runner.script("chroot", ("fail", EmergeDiagnosisTests.USE_CHANGES))
        with self.assertRaisesRegex(PackageInstallError, "base system packages could not be installed.*qtpositioning"):
            self.c.install_essential(m)

    def test_essential_packages_survive_a_dropped_download(self) -> None:
        self.runner.script("chroot", ("fail", "!!! Fetch failed for sudo-1.9.tar.gz, continuing..."))
        self.c.install_essential(target_manifest())
        self.assertEqual(len(self.emerges()), 2)
        self.assertTrue(any("trying again (2/3)" in note for note in self.notes))

    def test_essential_packages_are_not_rebuilt_after_a_compile_error(self) -> None:
        self.runner.script("chroot", ("fail", " * The following 1 packages failed to build:\n * 1) app-admin/sudo-1.9::gentoo (/log)\n"))
        with self.assertRaisesRegex(PackageInstallError, "failed to build: app-admin/sudo-1.9"):
            self.c.install_essential(target_manifest())
        self.assertEqual(len(self.emerges()), 1)

    def test_essential_packages_give_up_after_three_attempts(self) -> None:
        for _ in range(3):
            self.runner.script("chroot", ("fail", "Temporary failure in name resolution"))
        with self.assertRaises(PackageInstallError):
            self.c.install_essential(target_manifest())
        self.assertEqual(len(self.emerges()), 3)

    def test_desktop_retries_what_is_still_missing_and_then_succeeds(self) -> None:
        m = target_manifest(system={"desktop": "xfce"}, options={"bluetooth": False, "pipewire": False, "printing": False})
        self.runner.script("chroot", ("fail", "!!! Fetch failed for lightdm-1.32.tar.xz, continuing..."))
        mark_installed(self.root, "xfce-base/xfce4-meta", "x11-base/xorg-server")
        result = self.c.install_desktop(m)
        required = [e for e in self.emerges() if "xfce4-meta" in e or "lightdm" in e]
        self.assertEqual(len(required), 2)
        self.assertIn("--keep-going", required[0])
        self.assertNotIn("xfce-base/xfce4-meta", required[1])  # only what is still missing is retried
        self.assertIn("x11-misc/lightdm", required[1])
        self.assertEqual(result.degraded, [])
        self.assertTrue(any("retrying (2/3)" in note for note in self.notes))

    def test_desktop_gives_up_after_three_downloads_fail(self) -> None:
        m = target_manifest(system={"desktop": "gnome"})
        for _ in range(3):
            self.runner.script("chroot", ("fail", "!!! Fetch failed for gdm-48.tar.xz, continuing..."))
        with self.assertRaises(PackageInstallError) as context:
            self.c.install_desktop(m)
        self.assertIn("download failed for gdm-48.tar.xz", str(context.exception))
        self.assertIn("gnome-base/gnome", context.exception.failed)
        self.assertEqual(sum("--keep-going" in e for e in self.emerges()), 3)

    def test_a_package_that_fails_to_build_is_not_built_again(self) -> None:
        # a deterministic compile error would be repeated after hours; the user gets the reason at once
        m = target_manifest(system={"desktop": "gnome"})
        self.runner.script("chroot", ("fail", " * The following 1 packages failed to build:\n * 1) gnome-base/gdm-48::gentoo (/log)\n"))
        with self.assertRaisesRegex(PackageInstallError, "failed to build: gnome-base/gdm-48"):
            self.c.install_desktop(m)
        self.assertEqual(sum("--keep-going" in e for e in self.emerges()), 1)

    def test_a_build_that_runs_out_of_memory_is_retried_with_one_compile_job(self) -> None:
        m = target_manifest(system={"desktop": "xfce"}, options={"bluetooth": False, "pipewire": False, "printing": False})
        self.runner.script("chroot", ("fail", "c++: fatal error: Killed signal terminated program cc1plus\n * 1) xfce-base/xfce4-meta-4.20::gentoo (/log)"))
        mark_installed(self.root, "x11-base/xorg-server")
        self.c.install_desktop(m)
        first, second = [i for i, e in enumerate(self.emerges()) if "--keep-going" in e][:2]
        envs = [env or {} for env, command in zip(self.runner.envs, self.runner.commands) if "emerge" in command]
        self.assertNotIn("MAKEOPTS", envs[first])
        self.assertEqual(envs[second]["MAKEOPTS"], "-j1")
        self.assertTrue(any("single compile job" in note for note in self.notes))

    def test_unrecognised_failures_are_retried(self) -> None:
        self.runner.script("chroot", ("fail", "something strange happened"))
        self.c.install_desktop(target_manifest(system={"desktop": "gnome"}, options={"bluetooth": False, "pipewire": False, "printing": False}))
        self.assertEqual(sum("--keep-going" in e for e in self.emerges()), 3)  # the failed attempt, its retry, and the optional packages

    def test_desktop_does_not_retry_an_unresolvable_set(self) -> None:
        self.runner.script("chroot", ("fail", EmergeDiagnosisTests.USE_CHANGES))
        with self.assertRaises(PackageInstallError) as context:
            self.c.install_desktop(target_manifest(system={"desktop": "kde"}))
        self.assertIn("qtpositioning", str(context.exception))
        self.assertEqual(sum("--keep-going" in e for e in self.emerges()), 1)

    def test_a_dependency_failure_is_not_an_error_when_everything_requested_is_installed(self) -> None:
        mark_installed(self.root, "xfce-base/xfce4-meta", "x11-base/xorg-server", "x11-misc/lightdm",
                       "x11-misc/lightdm-gtk-greeter", "gnome-extra/polkit-gnome", "gui-libs/display-manager-init")
        self.runner.script("chroot", ("fail", " * 1) app-misc/unrelated-1::gentoo (/log)"))
        result = self.c.install_desktop(target_manifest(system={"desktop": "xfce"}, options={"bluetooth": False, "pipewire": False, "printing": False}))
        self.assertEqual(result.degraded, [])
        self.assertTrue(any("everything requested is installed" in note for note in self.notes))

    def test_optional_packages_that_cannot_be_built_degrade_with_the_reason(self) -> None:
        m = target_manifest(system={"desktop": "minimal"}, options={"pipewire": True, "bluetooth": True, "printing": False,
                                                                       "extra_packages": ["app-misc/typo", "app-editors/vim"]})
        mark_installed(self.root, "net-wireless/bluez")  # bluez installs, pipewire does not
        self.runner.script("chroot", ("fail", "!!! Fetch failed for pipewire.tar.xz"), ("fail", "!!! Fetch failed for pipewire.tar.xz"))
        result = self.c.install_desktop(m)
        self.assertEqual(result.failed, ["media-video/pipewire"])
        self.assertEqual(len(result.degraded), 1)
        self.assertIn("media-video/pipewire could not be installed: download failed for pipewire.tar.xz", result.degraded[0])
        # the packages the user typed are separate emerges, so a typo cannot take the others down
        self.assertTrue(any(e.endswith("app-misc/typo") for e in self.emerges()))
        self.assertTrue(any(e.endswith("app-editors/vim") for e in self.emerges()))

    def test_an_unresolvable_optional_group_is_tried_package_by_package(self) -> None:
        m = target_manifest(system={"desktop": "minimal"}, options={"pipewire": True, "bluetooth": True, "printing": False})
        self.runner.script("chroot", ("fail", 'emerge: there are no ebuilds to satisfy "net-wireless/bluez".'))
        mark_installed(self.root, "media-video/pipewire")
        result = self.c.install_desktop(m)
        singles = [e for e in self.emerges() if e.endswith("media-video/pipewire") or e.endswith("net-wireless/bluez")]
        self.assertEqual(len(singles), 2)  # the group emerge that resolved nothing, then bluez on its own
        self.assertEqual(result.degraded, [])

    def test_world_update_is_best_effort(self) -> None:
        m = target_manifest(system={"desktop": "minimal", "world_update": True}, options={"pipewire": False, "bluetooth": False, "printing": False})
        self.runner.script("chroot", ("fail", "slot conflict"))
        result = self.c.install_desktop(m)
        self.assertTrue(result.degraded and "@world" in result.degraded[0])
        self.assertIn("--update --deep --newuse --keep-going @world", self.emerges()[0])

    def test_hardware_plan_groups_are_installed_best_effort(self) -> None:
        plan = FakePlan(optional_atoms=[("TLP", ["sys-power/tlp"])], required_atoms=["sys-power/thermald"])
        m = target_manifest(system={"desktop": "minimal"}, options={"pipewire": False, "bluetooth": False, "printing": False})
        result = self.c.install_desktop(m, plan)
        emerges = self.emerges()
        self.assertTrue(any("sys-power/thermald" in e for e in emerges))
        self.assertTrue(any("sys-power/tlp" in e for e in emerges))
        self.assertEqual(result.degraded, [])

    def test_missing_companion_applications_degrade_instead_of_failing(self) -> None:
        m = target_manifest(system={"desktop": "minimal"}, options={"pipewire": False, "bluetooth": False, "printing": False,
                                                                       "install_hwmonux": True, "install_portagehub": True})
        c = configurer(self.root, self.runner, companion_bundle=self.root / "no-bundle")
        result = c.install_desktop(m)
        self.assertEqual(len(result.degraded), 1)
        self.assertIn("companion applications were skipped", result.degraded[0])
        self.assertEqual([e for e in self.emerges() if "hwmonux" in e or "portagehub" in e], [])

    def test_apply_hardware_writes_the_files_and_runs_the_steps(self) -> None:
        plan = FakePlan(files=[("etc/modprobe.d/blacklist-nouveau.conf", "blacklist nouveau\n", 0o644)], post_steps=[["usermod", "-aG", "video", "gentoo"]])
        self.runner.script("*usermod", ("fail", "no such group"))
        warnings = self.c.apply_hardware(plan)
        self.assertEqual(read(self.root, "etc/modprobe.d/blacklist-nouveau.conf"), "blacklist nouveau\n")
        self.assertEqual(len(warnings), 1)
        self.assertIn("usermod -aG video gentoo failed", warnings[0])

    def test_chroot_commands_run_as_root_when_the_runner_supports_it(self) -> None:
        self.c.chroot(("true",))
        self.assertEqual(self.runner.root_identity, [True])


if __name__ == "__main__":
    unittest.main()
