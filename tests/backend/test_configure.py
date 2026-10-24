from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from gentoo_installer.manifest import Partition
from gentoo_installerd.configure import ConfigurationError, Configurer, ninja_opts_from_make_opts, render_fstab, render_hosts, render_make_conf, render_netifrc, render_package_use, render_systemd_network
from gentoo_installerd.hwplan import plan as plan_hardware
from gentoo_installerd.storage import LayoutEntry

from tests.backend.helpers import FakeRunner, manifest


class RenderingTests(unittest.TestCase):
    def test_make_conf_quotes_values_and_enables_verified_binhost(self) -> None:
        m = manifest()
        m.portage.cflags = "-O2 -pipe; not-a-command"
        m.portage.features = ["collision-protect"]
        text = render_make_conf(m)
        self.assertIn("COMMON_FLAGS='-O2 -pipe; not-a-command'", text)
        self.assertIn("CFLAGS='${COMMON_FLAGS}'", text)
        self.assertIn("getbinpkg", text)
        self.assertIn("binpkg-request-signature", text)
        self.assertEqual(text.count("FEATURES="), 1)
        self.assertIn("PORTAGE_BINHOST=", text)

    def test_make_conf_keeps_the_stage3_compiler_and_locale_settings(self) -> None:
        text = render_make_conf(manifest())
        for line in ("FCFLAGS='${COMMON_FLAGS}'", "FFLAGS='${COMMON_FLAGS}'", "LC_MESSAGES=C.UTF-8", "CXXFLAGS='${CFLAGS}'"):
            self.assertIn(line, text.splitlines())

    def test_empty_values_never_override_profile_defaults(self) -> None:
        # An empty CPU_FLAGS_X86 made opencv's REQUIRED_USE fail in the default KDE install.
        m = manifest()
        self.assertEqual((m.portage.cpu_flags, m.portage.video_cards, m.portage.use_flags, m.portage.rustflags), ("", "", [], ""))
        text = render_make_conf(m)
        for key in ("CPU_FLAGS_X86", "VIDEO_CARDS", "USE", "RUSTFLAGS", "MAKEOPTS", "NINJAOPTS"):
            self.assertNotIn(f"{key}=", text)
        m.portage.cpu_flags = "auto"
        m.portage.video_cards = "auto"
        self.assertNotIn("CPU_FLAGS_X86", render_make_conf(m))
        self.assertNotIn("VIDEO_CARDS", render_make_conf(m))
        m.portage.cpu_flags = "${CPU_FLAGS_X86} aes avx"
        m.portage.video_cards = "intel"
        text = render_make_conf(m)
        self.assertIn("CPU_FLAGS_X86='${CPU_FLAGS_X86} aes avx'", text)
        self.assertIn("VIDEO_CARDS=intel", text)

    def test_make_opts_given_by_the_caller_are_used_verbatim(self) -> None:
        m = manifest()
        self.assertIn("MAKEOPTS='-j6 -l8'", render_make_conf(m, make_opts="-j6 -l8"))
        m.portage.make_opts = "-j3"
        self.assertIn("MAKEOPTS=-j3", render_make_conf(m))
        self.assertNotIn("NINJAOPTS", render_make_conf(m))

    def test_ninja_parallelism_tracks_bounded_make_jobs(self) -> None:
        # NINJAOPTS is no longer persisted, but the helper still bounds ninja for explicit use.
        for make_opts, expected in (("-j3 -l2.5", "-j3 -l2.5"), ("--jobs=5 --load-average=4", "-j5 -l4"),
                                    ("-j 1 -l 2.5", "-j1 -l2.5"), ("--jobs 1 --load-average 2.5", "-j1 -l2.5"),
                                    ("-j4 --jobs 1 -l3 --load-average 2.5", "-j1 -l2.5"), ("-j", "-j2"), ("-j9 -j0", "-j2")):
            with self.subTest(make_opts=make_opts):
                self.assertEqual(ninja_opts_from_make_opts(make_opts), expected)

    def test_package_use_is_sorted_and_validated(self) -> None:
        self.assertEqual(render_package_use({"app/two": ["-X"], "app/one": ["ssl"]}), "app/one ssl\napp/two -X\n")
        with self.assertRaises(ConfigurationError):
            render_package_use({"invalid;rm": ["X"]})
        with self.assertRaises(ConfigurationError):
            render_package_use({"app/valid": ["X;touch"]})

    def test_fstab_uses_uuids_and_correct_pass_numbers(self) -> None:
        layout = [LayoutEntry("/dev/sda2", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat"), LayoutEntry("/dev/sda3", "", "swap")]
        text = render_fstab(layout, {"/dev/sda2": "root-id", "/dev/sda1": "efi-id", "/dev/sda3": "swap-id"})
        self.assertLess(text.index("root-id"), text.index("efi-id"))
        self.assertIn("UUID=root-id / ext4 defaults,noatime 0 1", text)
        self.assertIn("umask=0077", text)
        self.assertIn("UUID=swap-id none swap sw 0 0", text)

    def test_network_rendering(self) -> None:
        m = manifest()
        m.network.dhcp = False
        m.network.address = "192.0.2.10/24"
        m.network.gateway = "192.0.2.1"
        m.network.dns = ["1.1.1.1", "2606:4700:4700::1111"]
        m.network.interface = "enp1s0"
        self.assertIn("Address=192.0.2.10/24", render_systemd_network(m))
        self.assertIn('routes_enp1s0="default via 192.0.2.1"', render_netifrc(m))
        self.assertIn(m.accounts.hostname, render_hosts(m.accounts.hostname))


class PathAndWriteTests(unittest.TestCase):
    def test_paths_are_confined(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            configurer = Configurer(FakeRunner(), root)
            for path in ("../outside", "/../../outside", "etc/../../outside", "bad\x00path"):
                with self.subTest(path=path), self.assertRaises(ConfigurationError):
                    configurer.path(path)
            configurer.write("etc/example", "ok\n")
            self.assertEqual((Path(root) / "etc/example").read_text(), "ok\n")

    def test_existing_symlink_cannot_escape_root(self) -> None:
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            Path(root, "etc").symlink_to(outside, target_is_directory=True)
            configurer = Configurer(FakeRunner(), root)
            with self.assertRaises(ConfigurationError):
                configurer.write("etc/passwd", "bad")
            self.assertFalse(Path(outside, "passwd").exists())

    def test_sensitive_files_have_restrictive_modes(self) -> None:
        m = manifest()
        m.network.wifi_ssid = "test-network"
        m.network.wifi_password = "wireless-secret"
        with tempfile.TemporaryDirectory() as root:
            configurer = Configurer(FakeRunner(), root)
            configurer.configure_network(m)
            path = Path(root, "etc/NetworkManager/system-connections/installer-wifi.nmconnection")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("wireless-secret", path.read_text())

    def test_networkmanager_static_wired_profile_and_dhcp_default(self) -> None:
        m = manifest()
        with tempfile.TemporaryDirectory() as root:
            configurer = Configurer(FakeRunner(), root)
            configurer.configure_network(m)
            profile = Path(root, "etc/NetworkManager/system-connections/installer-wired.nmconnection")
            self.assertFalse(profile.exists())

            m.network.dhcp = False
            m.network.address = "192.0.2.10/24"
            m.network.gateway = "192.0.2.1"
            m.network.dns = ["1.1.1.1", "2606:4700:4700::1111"]
            configurer.configure_network(m)
            text = profile.read_text()
            self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
            self.assertIn("type=ethernet\nautoconnect=true\n", text)
            self.assertIn("uuid=", text)
            self.assertIn("\n[ethernet]\n", text)
            self.assertNotIn("interface-name=", text)
            self.assertIn("[ipv4]\nmethod=manual\naddress1=192.0.2.10/24\ngateway=192.0.2.1\ndns=1.1.1.1;", text)
            self.assertIn("[ipv6]\nmethod=auto\ndns=2606:4700:4700::1111;\nignore-auto-dns=true", text)
            self.assertEqual(Path(root, "etc/resolv.conf").read_text(),
                             "nameserver 1.1.1.1\nnameserver 2606:4700:4700::1111\n")

    def test_networkmanager_static_wired_interface_and_family_are_checked(self) -> None:
        m = manifest()
        m.network.dhcp = False
        m.network.address = "2001:db8::10/64"
        m.network.gateway = "2001:db8::1"
        m.network.dns = ["2001:4860:4860::8888"]
        m.network.interface = "enp1s0"
        with tempfile.TemporaryDirectory() as root:
            configurer = Configurer(FakeRunner(), root)
            configurer.configure_network(m)
            profile = Path(root, "etc/NetworkManager/system-connections/installer-wired.nmconnection")
            self.assertIn("interface-name=enp1s0", profile.read_text())
            self.assertIn("[ipv6]\nmethod=manual\naddress1=2001:db8::10/64\ngateway=2001:db8::1",
                          profile.read_text())
            self.assertIn("[ipv4]\nmethod=disabled", profile.read_text())
            m.network.interface = "enp1s0\n[evil]"
            with self.assertRaises(ConfigurationError):
                configurer.configure_network(m)
            m.network.interface = "enp1s0"
            m.network.gateway = "192.0.2.1"
            with self.assertRaises(ConfigurationError):
                configurer.configure_network(m)

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            configurer = Configurer(FakeRunner(), root, dry_run=True)
            configurer.write("etc/file", "content")
            self.assertEqual(list(Path(root).iterdir()), [])


class AccountAndCommandTests(unittest.TestCase):
    def test_passwords_only_enter_chpasswd_stdin(self) -> None:
        m = manifest()
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as root:
            configurer = Configurer(runner, root, dry_run=True)
            configurer.configure_accounts(m)
        index = next(index for index, command in enumerate(runner.commands) if command[-1] == "chpasswd")
        self.assertTrue(runner.secrets[index])
        self.assertIn("user-password", runner.inputs[index] or "")
        self.assertIn("root-password", runner.inputs[index] or "")
        self.assertFalse(any("password" in arg for command in runner.commands for arg in command))

    def test_existing_account_is_not_created_again(self) -> None:
        runner = FakeRunner()
        Configurer(runner, "/mnt/gentoo").configure_accounts(manifest())
        self.assertFalse(any("useradd" in command for command in runner.commands))
        self.assertTrue(any(command[-1] == "chpasswd" for command in runner.commands))

    def test_all_target_commands_use_chroot_argv(self) -> None:
        runner = FakeRunner()
        configurer = Configurer(runner, "/mnt/gentoo", dry_run=True)
        configurer.chroot(("emerge", "--noreplace", "app-admin/sudo"))
        self.assertEqual(runner.commands, [("chroot", "/mnt/gentoo", "emerge", "--noreplace", "app-admin/sudo")])


class PackageAndServiceTests(unittest.TestCase):
    def test_selected_network_and_companion_atoms_match_repository_categories(self) -> None:
        m = manifest()
        m.options.install_hwmonux = True
        m.options.install_portagehub = True
        atoms = Configurer(FakeRunner(), "/mnt/gentoo", dry_run=True).package_atoms(m)
        self.assertIn("net-misc/networkmanager", atoms)
        self.assertIn("app-misc/hwmonux", atoms)
        self.assertIn("app-portage/portagehub", atoms)
        self.assertNotIn("gui-apps/hwmonux", atoms)
        self.assertIn("gui-libs/display-manager-init", atoms)

    def test_display_manager_init_is_only_needed_for_openrc_desktops(self) -> None:
        m = manifest()
        configurer = Configurer(FakeRunner(), "/mnt/gentoo", dry_run=True)
        m.system.desktop = "xfce"
        self.assertIn("gui-libs/display-manager-init", configurer.package_atoms(m))
        m.system.init = "systemd"
        self.assertNotIn("gui-libs/display-manager-init", configurer.package_atoms(m))
        m.system.init = "openrc"
        m.system.desktop = "minimal"
        self.assertNotIn("gui-libs/display-manager-init", configurer.package_atoms(m))

    def test_copies_companion_repository_and_distfiles_before_emerge(self) -> None:
        m = manifest()
        m.options.install_hwmonux = True
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, bundle = base / "target", base / "bundle"
            (bundle / "overlay/profiles").mkdir(parents=True)
            (bundle / "overlay/profiles/repo_name").write_text("gentoo-live-installer\n")
            (bundle / "distfiles").mkdir()
            (bundle / "distfiles/hwmonux-1.0.0.tar.zst").write_bytes(b"archive")
            runner = FakeRunner()
            Configurer(runner, root, companion_bundle=bundle).install_packages(m)
            self.assertEqual((root / "var/cache/distfiles/hwmonux-1.0.0.tar.zst").read_bytes(), b"archive")
            self.assertEqual((root / "var/db/repos/gentoo-live-installer/profiles/repo_name").read_text(), "gentoo-live-installer\n")
            self.assertEqual((root / "etc/portage/package.accept_keywords/gentoo-installer-companions").read_text(),
                             "app-misc/liquidctl ~amd64\ndev-python/hidapi ~amd64\n")
            self.assertIn("app-misc/hwmonux", runner.commands[-1])

    def test_netifrc_enables_selected_interface(self) -> None:
        m = manifest()
        m.network.manager = "netifrc"
        m.network.interface = "enp1s0"
        m.system.desktop = "minimal"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc/init.d").mkdir(parents=True)
            (root / "etc/init.d/net.lo").write_text("stub")
            runner = FakeRunner()
            Configurer(runner, root).configure_services(m)
            self.assertEqual((root / "etc/init.d/net.enp1s0").readlink().as_posix(), "net.lo")
            self.assertIn(("chroot", str(root), "rc-update", "add", "net.enp1s0", "default"), runner.commands)

    def test_encrypted_root_configures_dracut_and_boot_arguments(self) -> None:
        m = manifest()
        m.storage.luks = True
        m.storage.lvm = True
        runner = FakeRunner()
        uuid = "12345678-1234-1234-1234-123456789abc"
        runner.respond("blkid -s UUID -o value /dev/sda3", uuid + "\n")
        runner.respond("blkid -s UUID -o value /dev/gentoo/root", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Configurer(runner, root).configure_initramfs(m, "/dev/gentoo/root", "/dev/sda3")
            self.assertIn(f"rd.luks.uuid={uuid}", (root / "etc/kernel/cmdline").read_text())
            self.assertIn(f"rd.luks.name={uuid}=gentoo-root", (root / "etc/kernel/cmdline").read_text())
            self.assertIn(f"rd.luks.uuid={uuid}", (root / "etc/default/grub").read_text())
            self.assertIn("rd.lvm.lv=gentoo/root", (root / "etc/default/grub").read_text())
            self.assertIn("crypt lvm", (root / "etc/dracut.conf.d/gentoo-installer.conf").read_text())
            self.assertIn("root=UUID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", (root / "etc/kernel/cmdline").read_text())

    def test_plain_root_uses_generic_initramfs_before_target_fstab_exists(self) -> None:
        m = manifest()
        runner = FakeRunner()
        runner.respond("blkid -s UUID -o value /dev/sda3", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Configurer(runner, root).configure_initramfs(m, "/dev/sda3")
            self.assertEqual((root / "etc/dracut.conf.d/gentoo-installer.conf").read_text(), 'hostonly="no"\n')
            self.assertFalse((root / "etc/fstab").exists())
            self.assertEqual((root / "etc/kernel/cmdline").read_text(),
                             "root=UUID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee rw\n")

    def test_grub_uki_searches_efi_partition(self) -> None:
        m = manifest()
        m.system.uki = True
        runner = FakeRunner()
        runner.respond("blkid -s UUID -o value /dev/sda1", "1234-ABCD\n")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "boot").mkdir()
            (root / "boot/vmlinuz-6.12.0").write_bytes(b"kernel")
            (root / "boot/initramfs-6.12.0.img").write_bytes(b"initramfs")
            Configurer(runner, root).configure_bootloader(m, "/dev/sda3", "/dev/sda1")
            script = (root / "etc/grub.d/40_custom").read_text()
            self.assertIn("search --no-floppy --fs-uuid --set=root 1234-ABCD", script)
            self.assertIn("chainloader /EFI/Linux/gentoo.efi", script)

    def test_openrc_uki_enables_required_systemd_utils_flags(self) -> None:
        m = manifest()
        m.system.uki = True
        m.system.bootloader = "systemd-boot"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configurer = Configurer(FakeRunner(), root)
            configurer.configure_portage(m)
            self.assertIn("sys-apps/systemd-utils boot kernel-install ukify",
                          (root / "etc/portage/package.use/installer").read_text())
            self.assertIn("sys-apps/systemd-utils", configurer.package_atoms(m))
            self.assertIn("sync-uri = https://", (root / "etc/portage/binrepos.conf/gentoo.conf").read_text())

    def test_hwmonux_enables_liquidctl_i2c_python_dependency(self) -> None:
        m = manifest()
        m.options.install_hwmonux = True
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Configurer(FakeRunner(), root).configure_portage(m)
            self.assertIn("sys-apps/i2c-tools python", (root / "etc/portage/package.use/installer").read_text())

    def test_dracut_installkernel_hook_is_enabled_before_kernel_emerge(self) -> None:
        for initramfs in ("dracut", "genkernel", "none"):
            with self.subTest(initramfs=initramfs), tempfile.TemporaryDirectory() as temporary:
                m = manifest()
                m.system.initramfs = initramfs
                m.portage.package_use = {"sys-kernel/installkernel": ["-dracut", "grub"]}
                root = Path(temporary)
                Configurer(FakeRunner(), root).configure_portage(m)
                lines = (root / "etc/portage/package.use/installer").read_text().splitlines()
                expected = "sys-kernel/installkernel grub dracut" if initramfs == "dracut" else "sys-kernel/installkernel -dracut grub"
                self.assertIn(expected, lines)

    def test_minimal_hwmonux_satisfies_qt_use_dependencies(self) -> None:
        m = manifest()
        m.system.desktop = "minimal"
        m.options.install_hwmonux = True
        m.portage.package_use = {"dev-qt/qtbase": ["-opengl"], "dev-qt/qttools": ["-opengl"],
                                 "media-libs/libglvnd": ["-X"], "x11-libs/libxkbcommon": ["-X"]}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Configurer(FakeRunner(), root).configure_portage(m)
            lines = (root / "etc/portage/package.use/installer").read_text().splitlines()
            self.assertIn("dev-qt/qtbase opengl", lines)
            self.assertIn("dev-qt/qttools opengl", lines)
            self.assertIn("media-libs/libglvnd X", lines)
            self.assertIn("x11-libs/libxkbcommon X", lines)

    def test_portagehub_without_hwmonux_satisfies_mesa_x_dependency(self) -> None:
        m = manifest()
        m.system.desktop = "minimal"
        m.options.install_hwmonux = False
        m.options.install_portagehub = True
        m.portage.package_use = {"media-libs/libglvnd": ["-X"]}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Configurer(FakeRunner(), root).configure_portage(m)
            lines = (root / "etc/portage/package.use/installer").read_text().splitlines()
            self.assertIn("media-libs/libglvnd X", lines)

    def test_networkmanager_and_lvm_enable_required_runtime_flags(self) -> None:
        m = manifest()
        m.storage.lvm = True
        m.portage.package_use = {"net-wireless/wpa_supplicant": ["-dbus"], "sys-fs/lvm2": ["-lvm"]}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Configurer(FakeRunner(), root).configure_portage(m)
            lines = (root / "etc/portage/package.use/installer").read_text().splitlines()
            self.assertIn("net-wireless/wpa_supplicant dbus", lines)
            self.assertIn("sys-fs/lvm2 lvm", lines)

    def test_minimal_systemd_portagehub_enables_polkit_dependency(self) -> None:
        m = manifest()
        m.system.init = "systemd"
        m.system.desktop = "minimal"
        m.options.install_portagehub = True
        m.portage.package_use = {"sys-apps/systemd": ["-policykit"]}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Configurer(FakeRunner(), root).configure_portage(m)
            self.assertIn("sys-apps/systemd policykit",
                          (root / "etc/portage/package.use/installer").read_text().splitlines())

    def test_encrypted_systemd_enables_cryptsetup_helper_only_when_needed(self) -> None:
        for init, luks, expected in (("systemd", True, True), ("systemd", False, False),
                                     ("openrc", True, False)):
            with self.subTest(init=init, luks=luks), tempfile.TemporaryDirectory() as temporary:
                m = manifest()
                m.system.init = init
                m.storage.luks = luks
                m.portage.package_use = {"sys-apps/systemd": ["-cryptsetup"]}
                root = Path(temporary)
                Configurer(FakeRunner(), root).configure_portage(m)
                lines = (root / "etc/portage/package.use/installer").read_text().splitlines()
                self.assertIn("sys-apps/systemd cryptsetup" if expected else "sys-apps/systemd -cryptsetup", lines)

    def test_desktop_breaks_libwebp_tiff_binary_dependency_cycle(self) -> None:
        m = manifest()
        m.system.desktop = "xfce"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Configurer(FakeRunner(), root).configure_portage(m)
            self.assertIn("media-libs/libwebp -tiff", (root / "etc/portage/package.use/installer").read_text())

    def test_firmware_and_microcode_come_from_the_hardware_plan_with_narrow_licenses(self) -> None:
        # configure.py names no firmware or microcode package itself: the plan (here the generic one) decides.
        for firmware, microcode, expected_atoms, expected_licenses, expected_use in (
            (False, False, [], "", ""),
            (True, False, ["sys-kernel/linux-firmware"],
             "sys-kernel/linux-firmware linux-fw-redistributable\n", "sys-kernel/linux-firmware redistributable\n"),
            (False, True, ["sys-firmware/intel-microcode", "sys-kernel/linux-firmware"],
             "sys-firmware/intel-microcode intel-ucode\nsys-kernel/linux-firmware linux-fw-redistributable\n",
             "sys-firmware/intel-microcode initramfs\nsys-kernel/linux-firmware initramfs redistributable\n"),
            (True, True, ["sys-firmware/intel-microcode", "sys-kernel/linux-firmware"],
             "sys-firmware/intel-microcode intel-ucode\nsys-kernel/linux-firmware linux-fw-redistributable\n",
             "sys-firmware/intel-microcode initramfs\nsys-kernel/linux-firmware initramfs redistributable\n"),
        ):
            with self.subTest(firmware=firmware, microcode=microcode), tempfile.TemporaryDirectory() as temporary:
                m = manifest()
                m.options.firmware = firmware
                m.options.microcode = microcode
                plan = plan_hardware(m, None)
                configurer = Configurer(FakeRunner(), temporary)
                configurer.configure_portage(m, plan)
                self.assertEqual(configurer.early_boot_atoms(m, plan), expected_atoms)
                atoms = configurer.package_atoms(m, plan)
                self.assertEqual([atom for atom in atoms if atom in expected_atoms], expected_atoms)
                self.assertNotIn("sys-firmware/amd-ucode", atoms)
                root = Path(temporary)
                license_file = root / "etc/portage/package.license/installer-hardware"
                self.assertEqual(license_file.read_text() if license_file.exists() else "", expected_licenses)
                self.assertFalse((root / "etc/portage/package.license/installer").exists())  # nothing but the plan names a licence
                self.assertIn("ACCEPT_LICENSE=@FREE", (root / "etc/portage/make.conf").read_text())
                use = (root / "etc/portage/package.use/installer-hardware").read_text()
                self.assertEqual("".join(line + "\n" for line in use.splitlines() if "pipewire" not in line), expected_use)
                package_use = (root / "etc/portage/package.use/installer").read_text()
                self.assertNotIn("linux-firmware", package_use)
                self.assertNotIn("intel-microcode", package_use)

    def test_without_a_plan_configure_decides_nothing_about_hardware(self) -> None:
        m = manifest()
        m.options.firmware = m.options.microcode = True
        with tempfile.TemporaryDirectory() as temporary:
            configurer = Configurer(FakeRunner(), temporary)
            configurer.configure_portage(m)
            self.assertEqual(configurer.early_boot_atoms(m), [])
            self.assertFalse((Path(temporary) / "etc/portage/package.license").exists())
            self.assertNotIn("firmware", "".join(configurer.package_atoms(m)))

    def test_a_flag_the_user_set_explicitly_beats_the_plans_flag(self) -> None:
        m = manifest()
        m.options.firmware, m.options.microcode = False, True
        m.portage.package_use = {"sys-kernel/linux-firmware": ["-initramfs"]}
        plan = plan_hardware(m, None)
        self.assertEqual(plan.package_use["sys-kernel/linux-firmware"], ["redistributable"])
        self.assertTrue(any(issue.code == "hw.package_use_conflict" for issue in plan.warnings))
        with tempfile.TemporaryDirectory() as temporary:
            Configurer(FakeRunner(), temporary).configure_portage(m, plan)
            self.assertIn("sys-kernel/linux-firmware -initramfs", (Path(temporary) / "etc/portage/package.use/installer").read_text())


class BootloaderTests(unittest.TestCase):
    def test_manual_boot_efi_mountpoint_is_used_for_systemd_boot(self) -> None:
        m = manifest()
        m.storage.mode = "manual"
        m.storage.partitions = [Partition("/dev/sda3", "/", "ext4"), Partition("/dev/sda1", "/boot/efi", "vfat")]
        m.system.bootloader = "systemd-boot"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "boot/vmlinuz-6.12.0").parent.mkdir()
            (root / "boot/vmlinuz-6.12.0").write_bytes(b"kernel")
            (root / "boot/initramfs-6.12.0.img").write_bytes(b"initramfs")
            runner = FakeRunner()
            Configurer(runner, root).configure_bootloader(m, "/dev/sda3", "/dev/sda1")
            self.assertEqual((root / "boot/efi/vmlinuz").read_bytes(), b"kernel")
            self.assertTrue((root / "boot/efi/loader/entries/gentoo.conf").is_file())
            self.assertIn(("chroot", str(root), "bootctl", "--path=/boot/efi", "install"), runner.commands)

    def test_manual_boot_efi_mountpoint_is_used_for_grub_uki(self) -> None:
        m = manifest()
        m.storage.mode = "manual"
        m.storage.partitions = [Partition("/dev/sda3", "/", "ext4"), Partition("/dev/sda1", "/boot/efi", "vfat")]
        m.system.uki = True
        runner = FakeRunner()
        runner.respond("blkid -s UUID -o value /dev/sda1", "1234-ABCD\n")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "boot/vmlinuz-6.12.0").parent.mkdir()
            (root / "boot/vmlinuz-6.12.0").write_bytes(b"kernel")
            (root / "boot/initramfs-6.12.0.img").write_bytes(b"initramfs")
            Configurer(runner, root).configure_bootloader(m, "/dev/sda3", "/dev/sda1")
            self.assertIn("/boot/efi/EFI/Linux/gentoo.efi", next(command for command in runner.commands if "ukify" in command))
            self.assertIn("--efi-directory=/boot/efi", next(command for command in runner.commands if "grub-install" in command))

    def test_efi_stub_derives_esp_parent_and_part_number(self) -> None:
        m = manifest()
        m.system.bootloader = "efi-stub"
        runner = FakeRunner()
        runner.respond("lsblk --noheadings --output PARTN /dev/sda2", "2\n")
        runner.respond("lsblk --noheadings --output PKNAME /dev/sda2", "sda\n")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "boot").mkdir()
            (root / "boot/vmlinuz-6.12.0").write_bytes(b"kernel")
            (root / "boot/initramfs-6.12.0.img").write_bytes(b"initramfs")
            Configurer(runner, root).configure_bootloader(m, "/dev/sda3", "/dev/sda2")
        command = next(command for command in runner.commands if "efibootmgr" in command)
        self.assertIn("--disk", command)
        self.assertEqual(command[command.index("--disk") + 1], "/dev/sda")
        self.assertEqual(command[command.index("--part") + 1], "2")

    def test_systemd_boot_copies_real_kernel_and_initramfs(self) -> None:
        m = manifest()
        m.system.bootloader = "systemd-boot"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "boot").mkdir()
            (root / "boot/vmlinuz-6.12.0").write_bytes(b"kernel")
            (root / "boot/initramfs-6.12.0.img").write_bytes(b"initramfs")
            Configurer(FakeRunner(), root).configure_bootloader(m, "/dev/sda3", "/dev/sda1")
            self.assertEqual((root / "efi/vmlinuz").read_bytes(), b"kernel")
            self.assertEqual((root / "efi/initramfs").read_bytes(), b"initramfs")
            self.assertIn("options root=/dev/sda3 rw", (root / "efi/loader/entries/gentoo.conf").read_text())

    def test_uki_is_built_with_ukify_and_embedded_root(self) -> None:
        m = manifest()
        m.system.uki = True
        runner = FakeRunner()
        Configurer(runner, "/mnt/gentoo", dry_run=True).configure_bootloader(m, "/dev/sda3", "/dev/sda1")
        command = next(command for command in runner.commands if "ukify" in command)
        self.assertIn("/efi/EFI/Linux/gentoo.efi", command)
        self.assertIn("root=/dev/sda3 rw", command)

    def test_secure_systemd_boot_requires_uki(self) -> None:
        m = manifest()
        m.system.bootloader = "systemd-boot"
        m.system.secure_boot = True
        m.system.secure_boot_key = "/root/key"
        m.system.secure_boot_certificate = "/root/cert"
        with self.assertRaisesRegex(ConfigurationError, "requires a signed UKI"):
            Configurer(FakeRunner(), "/mnt/gentoo", dry_run=True).configure_bootloader(m, "/dev/sda3")


if __name__ == "__main__":
    unittest.main()
