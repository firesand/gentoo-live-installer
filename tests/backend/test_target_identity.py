from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_installer import keymaps
from gentoo_installerd.configure import (
    ConfigurationError,
    locale_gen_line,
    render_dconf_keyboard,
    render_kxkbrc,
    render_nm_wifi,
    render_nm_wired,
    render_xorg_keyboard,
    rewrite_keyfile,
)
from gentoo_installerd.runner import RunnerError

from tests.backend.target_helpers import FakePlan, ScriptedRunner, configurer, read, target_manifest


def keyboard(**system):
    return target_manifest(system={"keymap": "de-latin1-nodeadkeys", "keyboard_layout": "de", "keyboard_variant": "nodeadkeys", **system})


class KeymapTableTests(unittest.TestCase):
    def test_every_preset_names_a_real_console_keymap(self) -> None:
        available = keymaps.list_console_keymaps()
        if not available:
            self.skipTest("kbd keymaps are not installed on this machine")
        missing = sorted({preset.console for preset in keymaps.PRESETS} - available)
        self.assertEqual(missing, [])

    def test_xkb_names_exist_in_xkeyboard_config(self) -> None:
        rules = Path("/usr/share/X11/xkb/rules/evdev.xml")
        if not rules.is_file():
            self.skipTest("xkeyboard-config is not installed on this machine")
        import xml.etree.ElementTree as ET

        layouts = {}
        for layout in ET.parse(rules).getroot().iter("layout"):
            layouts[layout.find("configItem/name").text] = {v.find("configItem/name").text for v in layout.iter("variant")}
        for preset in keymaps.PRESETS:
            self.assertIn(preset.layout, layouts, preset)
            if preset.variant:
                self.assertIn(preset.variant, layouts[preset.layout], preset)

    def test_the_old_gui_presets_that_did_not_exist_are_replaced(self) -> None:
        consoles = {console for console, _ in keymaps.console_choices()}
        self.assertNotIn("gb", consoles)
        self.assertNotIn("pt", consoles)
        self.assertLessEqual({"uk", "pt-latin9", "de-latin1", "us"}, consoles)
        self.assertEqual(len(consoles), len(keymaps.console_choices()))

    def test_layout_to_console_mapping(self) -> None:
        self.assertEqual(keymaps.console_keymap_for("gb"), "uk")
        self.assertEqual(keymaps.console_keymap_for("de", "nodeadkeys"), "de-latin1-nodeadkeys")
        self.assertEqual(keymaps.console_keymap_for("de"), "de-latin1")
        self.assertEqual(keymaps.console_keymap_for("ie"), "uk")
        self.assertEqual(keymaps.console_keymap_for("ru"), "us")  # the text console cannot show Cyrillic
        self.assertEqual(keymaps.console_keymap_for("us,ru"), "us")
        self.assertEqual(keymaps.console_keymap_for("zz"), "us")
        self.assertEqual(keymaps.xkb_for_console("uk"), ("gb", ""))
        self.assertEqual(keymaps.xkb_for_console("nonsense"), ("us", ""))
        self.assertEqual(keymaps.near_matches("de-latin", ["de-latin1", "fr", "us"]), ["de-latin1"])


class KeyboardFileTests(unittest.TestCase):
    def files(self, m):
        with tempfile.TemporaryDirectory() as temporary:
            c = configurer(temporary)
            return {path: content for path, content, _ in c.keyboard_files(m)}

    def test_us_keyboard_and_minimal_systems_write_no_desktop_files(self) -> None:
        self.assertEqual(self.files(target_manifest()), {})
        self.assertEqual(self.files(keyboard(desktop="minimal")), {})

    def test_xfce_and_sddm_read_the_xorg_keyboard_section(self) -> None:
        files = self.files(keyboard(desktop="xfce", keyboard_options="grp:alt_shift_toggle"))
        self.assertEqual(list(files), ["etc/X11/xorg.conf.d/00-keyboard.conf"])
        text = files["etc/X11/xorg.conf.d/00-keyboard.conf"]
        for line in ('Option "XkbLayout" "de"', 'Option "XkbModel" "pc105"', 'Option "XkbVariant" "nodeadkeys"',
                     'Option "XkbOptions" "grp:alt_shift_toggle"', 'MatchIsKeyboard "on"'):
            self.assertIn(line, text)

    def test_plasma_gets_kxkbrc_and_gnome_gets_a_dconf_database(self) -> None:
        kde = self.files(keyboard(desktop="kde"))
        self.assertEqual(sorted(kde), ["etc/X11/xorg.conf.d/00-keyboard.conf", "etc/xdg/kxkbrc"])
        self.assertEqual(kde["etc/xdg/kxkbrc"], "[Layout]\nLayoutList=de\nUse=true\nModel=pc105\nVariantList=nodeadkeys\n")
        gnome = self.files(keyboard(desktop="gnome", keyboard_layout="us,ru", keyboard_variant=",", keyboard_options="grp:alt_shift_toggle"))
        self.assertEqual(gnome["etc/dconf/profile/user"], "user-db:user\nsystem-db:local\n")
        self.assertEqual(gnome["etc/dconf/db/local.d/00-gentoo-installer-keyboard"],
                         "[org/gnome/desktop/input-sources]\nsources=[('xkb', 'us'), ('xkb', 'ru')]\nxkb-options=['grp:alt_shift_toggle']\n")

    def test_renderers_omit_empty_options(self) -> None:
        system = target_manifest(system={"keyboard_layout": "fr"}).system
        self.assertNotIn("XkbVariant", render_xorg_keyboard(system))
        self.assertNotIn("Options", render_kxkbrc(system))
        self.assertEqual(render_dconf_keyboard(system), "[org/gnome/desktop/input-sources]\nsources=[('xkb', 'fr')]\n")


class IdentityTests(unittest.TestCase):
    def configure(self, m, **kwargs):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        zone = root / "usr/share/zoneinfo/UTC"
        zone.parent.mkdir(parents=True)
        zone.write_text("tz")
        runner = ScriptedRunner()
        c = configurer(root, runner, **kwargs)
        notes: list[str] = []
        c.notify = notes.append
        c.configure_identity(m)
        return root, runner, notes

    def test_defaults_write_the_console_locale_and_clock_files(self) -> None:
        root, runner, _ = self.configure(target_manifest())
        self.assertEqual(read(root, "etc/conf.d/keymaps"), 'keymap="us"\n')
        self.assertEqual(read(root, "etc/vconsole.conf"), "KEYMAP=us\n")
        self.assertEqual(read(root, "etc/locale.conf"), "LANG=en_US.UTF-8\n")
        self.assertEqual(read(root, "etc/env.d/02locale"), "LANG=en_US.UTF-8\n")
        self.assertEqual(read(root, "etc/locale.gen"), "en_US.UTF-8 UTF-8\n")
        self.assertFalse((root / "etc/adjtime").exists())
        self.assertFalse((root / "etc/X11").exists())
        self.assertIn(("chroot", str(root), "locale-gen"), runner.commands)

    def test_locale_is_added_to_the_base_locale_and_stays_quoted(self) -> None:
        root, _, _ = self.configure(target_manifest(system={"locale": "de_DE.UTF-8"}))
        self.assertEqual(read(root, "etc/locale.gen"), "de_DE.UTF-8 UTF-8\nen_US.UTF-8 UTF-8\n")
        self.assertEqual(read(root, "etc/locale.conf"), "LANG=de_DE.UTF-8\n")
        root, _, _ = self.configure(target_manifest(system={"locale": "C.UTF-8"}))
        self.assertEqual(read(root, "etc/locale.gen"), "")

    def test_locale_gen_uses_the_charmap_glibc_lists_for_the_name(self) -> None:
        supported = "en_US.UTF-8 UTF-8\nde_DE@euro ISO-8859-15\nsr_RS@latin UTF-8\n"
        self.assertEqual(locale_gen_line("de_DE@euro", supported), "de_DE@euro ISO-8859-15")
        self.assertEqual(locale_gen_line("sr_RS@latin", supported), "sr_RS@latin UTF-8")
        self.assertEqual(locale_gen_line("en_US.UTF-8", supported), "en_US.UTF-8 UTF-8")
        self.assertEqual(locale_gen_line("zh_CN.GB18030"), "zh_CN.GB18030 GB18030")
        self.assertEqual(locale_gen_line("de_DE@euro"), "de_DE@euro UTF-8")  # not listed: the old behaviour
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "usr/share/i18n").mkdir(parents=True)
            (root / "usr/share/i18n/SUPPORTED").write_text(supported)
            (root / "usr/share/zoneinfo").mkdir(parents=True)
            (root / "usr/share/zoneinfo/UTC").write_text("tz")
            configurer(root).configure_identity(target_manifest(system={"locale": "de_DE@euro"}))
            self.assertEqual(read(root, "etc/locale.gen"), "de_DE@euro ISO-8859-15\nen_US.UTF-8 UTF-8\n")

    def test_empty_console_keymap_is_derived_from_the_keyboard_layout(self) -> None:
        root, _, _ = self.configure(keyboard(keymap=""))
        self.assertEqual(read(root, "etc/conf.d/keymaps"), 'keymap="de-latin1-nodeadkeys"\n')
        self.assertEqual(read(root, "etc/vconsole.conf"), "KEYMAP=de-latin1-nodeadkeys\n")

    def test_missing_console_keymap_falls_back_to_us_with_a_warning(self) -> None:
        def prepare(root: Path) -> None:
            (root / "usr/share/keymaps/i386/qwertz").mkdir(parents=True)
            (root / "usr/share/keymaps/i386/qwertz/de-latin1.map.gz").write_bytes(b"x")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare(root)
            (root / "usr/share/zoneinfo").mkdir(parents=True)
            (root / "usr/share/zoneinfo/UTC").write_text("tz")
            c = configurer(root)
            notes: list[str] = []
            c.notify = notes.append
            c.configure_identity(keyboard(keymap="de-latin"))
            self.assertEqual(read(root, "etc/conf.d/keymaps"), 'keymap="us"\n')
            self.assertTrue(any("'de-latin' does not exist" in note and "de-latin1" in note for note in notes))
            c.configure_identity(keyboard(keymap="de-latin1"))
            self.assertEqual(read(root, "etc/conf.d/keymaps"), 'keymap="de-latin1"\n')

    def test_desktop_keyboard_files_are_written_for_graphical_systems(self) -> None:
        root, _, _ = self.configure(keyboard(desktop="kde"))
        self.assertIn('"XkbLayout" "de"', read(root, "etc/X11/xorg.conf.d/00-keyboard.conf"))
        self.assertIn("LayoutList=de", read(root, "etc/xdg/kxkbrc"))

    def test_hardware_clock_in_local_time_for_windows_dual_boot(self) -> None:
        root, _, _ = self.configure(target_manifest(system={"rtc_local": True}))
        self.assertEqual(read(root, "etc/adjtime"), "0.0 0 0.0\n0\nLOCAL\n")
        self.assertEqual(read(root, "etc/conf.d/hwclock"), 'clock="local"\n')

    def test_console_font_goes_to_both_init_systems(self) -> None:
        root, _, _ = self.configure(target_manifest(system={"console_font": "ter-132b"}))
        self.assertEqual(read(root, "etc/vconsole.conf"), "KEYMAP=us\nFONT=ter-132b\n")
        self.assertEqual(read(root, "etc/conf.d/consolefont"), 'consolefont="ter-132b"\n')

    def test_missing_timezone_is_still_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ConfigurationError, "Timezone is not installed"):
                configurer(temporary).configure_identity(target_manifest())


class InitramfsArgumentTests(unittest.TestCase):
    UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    LUKS = "12345678-1234-1234-1234-123456789abc"

    def configure(self, m, plan=None, root_device=None):
        """Run configure_initramfs; with LUKS the root is the opened mapper and /dev/sda3 the encrypted partition."""
        root_device = root_device or ("/dev/mapper/gentoo-root" if m.storage.luks else "/dev/sda3")
        runner = ScriptedRunner()
        runner.respond(f"blkid -s UUID -o value {root_device}", self.UUID + "\n")
        runner.respond("blkid -s UUID -o value /dev/sda3", self.LUKS + "\n" if m.storage.luks else self.UUID + "\n")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        configurer(root, runner).configure_initramfs(m, root_device, "/dev/sda3" if m.storage.luks else None, plan)
        return root

    def test_lvm_only_systems_get_the_lvm_argument_in_the_grub_configuration_too(self) -> None:
        root = self.configure(target_manifest(storage={"lvm": True}), root_device="/dev/gentoo/root")
        self.assertIn("rd.lvm.lv=gentoo/root", read(root, "etc/kernel/cmdline"))
        self.assertEqual(read(root, "etc/default/grub"), "GRUB_CMDLINE_LINUX=rd.lvm.lv=gentoo/root\n")

    def test_plain_systems_leave_the_grub_configuration_alone(self) -> None:
        root = self.configure(target_manifest())
        self.assertFalse((root / "etc/default/grub").exists())

    def test_an_existing_grub_cmdline_is_replaced_not_duplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc/default").mkdir(parents=True)
            (root / "etc/default/grub").write_text('GRUB_TIMEOUT=3\nGRUB_CMDLINE_LINUX="old quiet"\n# GRUB_CMDLINE_LINUX="commented"\n')
            runner = ScriptedRunner()
            runner.respond("blkid", self.UUID + "\n")
            configurer(root, runner).configure_initramfs(target_manifest(system={"kernel_cmdline_extra": "quiet nvme_core.default_ps_max_latency_us=0"}), "/dev/sda3")
            text = read(root, "etc/default/grub")
            self.assertEqual(text.count("GRUB_CMDLINE_LINUX="), 2)  # the active one and the commented one
            self.assertIn("GRUB_TIMEOUT=3", text)
            self.assertIn("GRUB_CMDLINE_LINUX='quiet nvme_core.default_ps_max_latency_us=0'", text)
            self.assertNotIn("old quiet", text)

    def test_extra_arguments_and_plan_arguments_come_last_in_order(self) -> None:
        plan = FakePlan(kernel_cmdline=["resume=UUID=" + self.UUID, "bad token;"])
        root = self.configure(target_manifest(system={"kernel_cmdline_extra": "console=ttyS0,115200"}), plan)
        self.assertEqual(read(root, "etc/kernel/cmdline"), f"root=UUID={self.UUID} rw resume=UUID={self.UUID} console=ttyS0,115200\n")

    def test_luks_prompt_uses_the_console_keymap_only_when_the_layout_was_chosen_with_it(self) -> None:
        luks = {"luks": True, "luks_passphrase": "long-enough-passphrase"}
        root = self.configure(target_manifest(storage=luks, system={"keymap": "de-latin1-nodeadkeys", "keyboard_layout": "de", "keyboard_variant": "nodeadkeys"}))
        self.assertIn("rd.vconsole.keymap=de-latin1-nodeadkeys", read(root, "etc/kernel/cmdline"))
        self.assertIn("rd.vconsole.keymap=de-latin1-nodeadkeys", read(root, "etc/default/grub"))
        self.assertEqual(read(root, "etc/vconsole.conf"), "KEYMAP=de-latin1-nodeadkeys\n")
        # a keymap picked on its own (the layout is still the US default): the passphrase was typed on a US layout
        root = self.configure(target_manifest(storage=luks, system={"keymap": "de-latin1-nodeadkeys"}))
        self.assertNotIn("rd.vconsole.keymap", read(root, "etc/kernel/cmdline"))
        root = self.configure(target_manifest(storage=luks))
        self.assertNotIn("rd.vconsole.keymap", read(root, "etc/kernel/cmdline"))
        # without encryption the prompt does not exist
        root = self.configure(target_manifest(system={"keymap": "de-latin1-nodeadkeys", "keyboard_layout": "de", "keyboard_variant": "nodeadkeys"}))
        self.assertNotIn("rd.vconsole.keymap", read(root, "etc/kernel/cmdline"))


class AccountTests(unittest.TestCase):
    def useradd(self, m):
        runner = ScriptedRunner()
        runner.script("chroot /mnt/gentoo id -u", ("fail", "no such user"))
        configurer("/mnt/gentoo", runner, dry_run=True).configure_accounts(m)
        return next(command for command in runner.commands if "useradd" in command)

    def test_portagehub_users_join_the_portage_group_when_it_exists(self) -> None:
        command = self.useradd(target_manifest(options={"install_portagehub": True}))
        self.assertEqual(command[command.index("--groups") + 1], "audio,video,wheel,portage")
        command = self.useradd(target_manifest(options={"install_portagehub": False}))
        self.assertEqual(command[command.index("--groups") + 1], "audio,video,wheel")

    def test_missing_portage_group_is_skipped(self) -> None:
        runner = ScriptedRunner()
        runner.script("chroot /mnt/gentoo id -u", ("fail", "no such user"))
        runner.script("chroot /mnt/gentoo getent group portage", ("fail", ""))
        configurer("/mnt/gentoo", runner, dry_run=True).configure_accounts(target_manifest(options={"install_portagehub": True}))
        command = next(command for command in runner.commands if "useradd" in command)
        self.assertEqual(command[command.index("--groups") + 1], "audio,video,wheel")

    def test_ssh_keys_are_private_to_the_user(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = ScriptedRunner()
            m = target_manifest(accounts={"ssh_authorized_keys": ["ssh-ed25519 AAAA test"]})
            configurer(temporary, runner).configure_accounts(m)
            self.assertEqual((Path(temporary) / "home/gentoo/.ssh/authorized_keys").stat().st_mode & 0o777, 0o600)
            self.assertIn(("chroot", temporary, "chmod", "700", "/home/gentoo/.ssh"), runner.commands)

    def test_privilege_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            c = configurer(temporary)
            c.configure_privileges(target_manifest())
            self.assertEqual((Path(temporary) / "etc/sudoers.d/wheel").stat().st_mode & 0o777, 0o440)
            c.configure_privileges(target_manifest(accounts={"privilege": "doas"}))
            self.assertEqual(read(Path(temporary), "etc/doas.conf"), "permit persist :wheel\n")


class NetworkTests(unittest.TestCase):
    def test_wifi_profile_has_identity_and_security(self) -> None:
        text = render_nm_wifi(target_manifest(network={"wifi_ssid": "Home Wi-Fi", "wifi_password": "correct horse"}).network,
                              connection_uuid="11111111-2222-3333-4444-555555555555")
        self.assertEqual(text.splitlines()[:5], ["[connection]", "id=Home Wi-Fi", "uuid=11111111-2222-3333-4444-555555555555", "type=wifi", "autoconnect=true"])
        self.assertIn("[wifi]\nmode=infrastructure\nssid=Home Wi-Fi\n", text)
        self.assertIn("[wifi-security]\nkey-mgmt=wpa-psk\npsk=correct horse\n", text)
        self.assertIn("[ipv4]\nmethod=auto", text)

    def test_open_network_has_no_security_section_and_special_characters_are_escaped(self) -> None:
        open_network = render_nm_wifi(target_manifest(network={"wifi_ssid": "Cafe"}).network)
        self.assertNotIn("wifi-security", open_network)
        tricky = render_nm_wifi(target_manifest(network={"wifi_ssid": " odd\\name", "wifi_password": "pa\\ss word!"}).network)
        self.assertIn("ssid=\\sodd\\\\name", tricky)
        self.assertIn("psk=pa\\\\ss word!", tricky)

    def test_wifi_values_cannot_inject_keyfile_lines(self) -> None:
        for ssid, password in (("evil\n[ipv4]", "password1"), ("x" * 33, ""), ("ok", "pass\nword!!")):
            with self.subTest(ssid=ssid[:10]), self.assertRaises(ConfigurationError):
                render_nm_wifi(target_manifest(network={"wifi_ssid": ssid, "wifi_password": password}).network)

    def test_static_settings_belong_to_the_wired_link_unless_a_wireless_interface_is_named(self) -> None:
        static = {"dhcp": False, "address": "192.0.2.10/24", "gateway": "192.0.2.1", "dns": ["192.0.2.53"]}
        with tempfile.TemporaryDirectory() as temporary:
            c = configurer(temporary)
            c.configure_network(target_manifest(network={"wifi_ssid": "Home", "wifi_password": "correct horse", **static}))
            profiles = Path(temporary, "etc/NetworkManager/system-connections")
            self.assertTrue((profiles / "installer-wired.nmconnection").is_file())
            self.assertIn("[ipv4]\nmethod=auto", (profiles / "installer-wifi.nmconnection").read_text())
            self.assertEqual(profiles.stat().st_mode & 0o777, 0o700)
            self.assertEqual((profiles / "installer-wifi.nmconnection").stat().st_mode & 0o777, 0o600)
        with tempfile.TemporaryDirectory() as temporary:
            configurer(temporary).configure_network(target_manifest(network={"wifi_ssid": "Home", "wifi_password": "correct horse", "interface": "wlp3s0", **static}))
            profiles = Path(temporary, "etc/NetworkManager/system-connections")
            self.assertFalse((profiles / "installer-wired.nmconnection").exists())
            self.assertIn("method=manual\naddress1=192.0.2.10/24", (profiles / "installer-wifi.nmconnection").read_text())

    def test_wired_profile_checks_the_interface_name(self) -> None:
        with self.assertRaises(ConfigurationError):
            render_nm_wired(target_manifest(network={"dhcp": False, "address": "192.0.2.10/24", "gateway": "192.0.2.1", "interface": "bad name"}).network)

    def test_dhcpcd_static_binds_to_the_named_interface_and_extends_the_stock_file(self) -> None:
        static = {"manager": "dhcpcd", "dhcp": False, "address": "192.0.2.10/24", "gateway": "192.0.2.1",
                  "dns": ["192.0.2.53", "192.0.2.54"], "interface": "enp1s0"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            c = configurer(root)
            m = target_manifest(network=static)
            c.configure_network(m)
            self.assertFalse((root / "etc/dhcpcd.conf").exists())  # the package has not installed its own yet
            (root / "etc").mkdir(exist_ok=True)
            (root / "etc/dhcpcd.conf").write_text("# stock\nhostname\n")
            c.configure_network_late(m)
            c.configure_network_late(m)
            text = read(root, "etc/dhcpcd.conf")
            self.assertTrue(text.startswith("# stock\nhostname\n"))
            self.assertEqual(text.count("interface enp1s0"), 1)
            self.assertIn("static ip_address=192.0.2.10/24\nstatic routers=192.0.2.1\nstatic domain_name_servers=192.0.2.53 192.0.2.54\n", text)
            self.assertNotIn("eth0", text)

    def test_dhcpcd_with_dhcp_never_touches_the_stock_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc").mkdir()
            (root / "etc/dhcpcd.conf").write_text("# stock\n")
            c = configurer(root)
            m = target_manifest(network={"manager": "dhcpcd"})
            c.configure_network(m)
            c.configure_network_late(m)
            self.assertEqual(read(root, "etc/dhcpcd.conf"), "# stock\n")

    def test_dhcpcd_static_without_an_interface_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            m = target_manifest(network={"manager": "dhcpcd", "dhcp": False, "address": "192.0.2.10/24", "gateway": "192.0.2.1"})
            with self.assertRaisesRegex(ConfigurationError, "interface"):
                configurer(temporary).configure_network(m)

    def test_systemd_networkd_hands_resolv_conf_to_resolved_at_the_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc").mkdir()
            (root / "etc/resolv.conf").write_text("nameserver 10.0.0.1\n")
            c = configurer(root)
            networkd = target_manifest(system={"init": "systemd", "stage_variant": "amd64-systemd"}, network={"manager": "systemd-networkd"})
            c.finalize_network(target_manifest())  # other managers keep the copied file
            self.assertFalse((root / "etc/resolv.conf").is_symlink())
            c.finalize_network(networkd)
            self.assertEqual(os.readlink(root / "etc/resolv.conf"), "../run/systemd/resolve/stub-resolv.conf")
            c.finalize_network(networkd)  # idempotent
            self.assertTrue((root / "etc/resolv.conf").is_symlink())


class LiveProfileImportTests(unittest.TestCase):
    WIFI = ("[connection]\nid=Office\nuuid=aaaa\ntype=wifi\npermissions=user:gentoo:;\n\n[wifi]\nssid=Office\n\n"
            "[wifi-security]\nkey-mgmt=wpa-psk\npsk=secret-secret\n")
    ENTERPRISE = ("[connection]\nid=Uni\ntype=wifi\npermissions=user:gentoo:;\n\n[802-1x]\neap=peap;\nca-cert={ca}\n"
                  "identity=me\n")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.live = self.base / "live"
        self.live.mkdir()
        self.root = self.base / "target"
        self.root.mkdir()

    def importer(self):
        c = configurer(self.root, live_network_dir=self.live)
        self.notes: list[str] = []
        c.notify = self.notes.append
        return c

    def test_rewrite_drops_permissions_and_skips_tunnels(self) -> None:
        profile = rewrite_keyfile(self.WIFI)
        self.assertNotIn("permissions", profile.text)
        self.assertIn("psk=secret-secret", profile.text)
        for kind in ("bridge", "bond", "vlan", "tun", "loopback", "dummy", "team"):
            self.assertIsNone(rewrite_keyfile(f"[connection]\nid=x\ntype={kind}\n"))
        self.assertIsNone(rewrite_keyfile("[wifi]\nssid=no type\n"))

    def test_portable_installs_forget_ethernet_interface_names(self) -> None:
        text = "[connection]\nid=Wired\ntype=ethernet\ninterface-name=enp3s0\n"
        self.assertIn("interface-name=enp3s0", rewrite_keyfile(text).text)
        self.assertNotIn("interface-name", rewrite_keyfile(text, portable=True).text)
        wifi = "[connection]\nid=W\ntype=wifi\ninterface-name=wlp2s0\n"
        self.assertIn("interface-name=wlp2s0", rewrite_keyfile(wifi, portable=True).text)

    def test_connections_are_copied_private_and_system_wide(self) -> None:
        (self.live / "Office.nmconnection").write_text(self.WIFI)
        (self.live / "installer-wifi.nmconnection").write_text(self.WIFI)
        (self.live / "bridge.nmconnection").write_text("[connection]\nid=br\ntype=bridge\n")
        (self.live / "notes.txt").write_text("not a profile")
        imported = self.importer().import_live_network_profiles(target_manifest(network={"import_live_profiles": True}))
        self.assertEqual(imported, ["Office"])
        target = self.root / "etc/NetworkManager/system-connections"
        self.assertEqual(sorted(path.name for path in target.iterdir()), ["Office.nmconnection"])
        self.assertEqual((target / "Office.nmconnection").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("permissions", (target / "Office.nmconnection").read_text())
        self.assertTrue(any("Office" in note for note in self.notes))

    def test_certificates_are_copied_and_paths_rewritten(self) -> None:
        ca = self.live / "ca.pem"
        ca.write_text("CERT")
        (self.live / "Uni.nmconnection").write_text(self.ENTERPRISE.format(ca=ca))
        with patch("gentoo_installerd.configure.CERTIFICATE_ROOTS", (str(self.base) + "/",)):
            self.importer().import_live_network_profiles(target_manifest(network={"import_live_profiles": True}))
        profile = (self.root / "etc/NetworkManager/system-connections/Uni.nmconnection").read_text()
        copied = [line.partition("=")[2] for line in profile.splitlines() if line.startswith("ca-cert=")][0]
        self.assertTrue(copied.startswith("/etc/NetworkManager/certs/"))
        self.assertEqual((self.root / copied.lstrip("/")).read_text(), "CERT")
        self.assertEqual((self.root / copied.lstrip("/")).stat().st_mode & 0o777, 0o600)

    def test_profiles_with_missing_or_foreign_certificates_are_skipped(self) -> None:
        (self.live / "Uni.nmconnection").write_text(self.ENTERPRISE.format(ca="/etc/shadow"))
        (self.live / "Gone.nmconnection").write_text(self.ENTERPRISE.format(ca=self.live / "missing.pem"))
        imported = self.importer().import_live_network_profiles(target_manifest(network={"import_live_profiles": True}))
        self.assertEqual(imported, [])
        self.assertEqual(sum("certificate that is missing" in note for note in self.notes), 2)

    def test_import_is_only_done_when_asked_for(self) -> None:
        (self.live / "Office.nmconnection").write_text(self.WIFI)
        c = self.importer()
        c.configure_network(target_manifest())
        self.assertFalse((self.root / "etc/NetworkManager/system-connections/Office.nmconnection").exists())
        c.configure_network(target_manifest(network={"import_live_profiles": True}))
        self.assertTrue((self.root / "etc/NetworkManager/system-connections/Office.nmconnection").exists())


class ServiceTests(unittest.TestCase):
    def enabled(self, m, plan=None, phase="all", installed=()):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in installed:
                (root / "etc/init.d").mkdir(parents=True, exist_ok=True)
                (root / "etc/init.d" / name).write_text("#!/sbin/openrc-run\n")
            runner = ScriptedRunner()
            c = configurer(root, runner)
            notes: list[str] = []
            c.notify = notes.append
            warnings = c.configure_services(m, plan, phase)
            adds = [command[4:] for command in runner.commands if command[2:4] == ("rc-update", "add")]
            return [tuple(add) for add in adds], warnings, notes

    ALL_INSTALLED = ("dbus", "elogind", "bluetooth", "cupsd", "display-manager", "consolefont")

    def test_openrc_desktops_start_dbus_and_elogind_even_without_networkmanager(self) -> None:
        for manager, extra in (("dhcpcd", {}), ("netifrc", {"interface": "enp1s0"})):
            with self.subTest(manager=manager):
                m = target_manifest(system={"desktop": "xfce"}, network={"manager": manager, **extra},
                                    options={"bluetooth": False, "printing": False})
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (root / "etc/init.d").mkdir(parents=True)
                    for name in ("net.lo", "dbus", "elogind", "display-manager"):
                        (root / "etc/init.d" / name).write_text("x")
                    runner = ScriptedRunner()
                    configurer(root, runner).configure_services(m)
                    adds = [command[4:] for command in runner.commands if command[2:4] == ("rc-update", "add")]
                    self.assertIn(("dbus", "default"), [tuple(a) for a in adds])
                    self.assertIn(("elogind", "boot"), [tuple(a) for a in adds])
                    self.assertLess(adds.index(("dbus", "default")), adds.index(("display-manager", "default")))

    def test_systemd_needs_no_runlevels_and_no_bus_service(self) -> None:
        m = target_manifest(system={"desktop": "xfce", "init": "systemd", "stage_variant": "amd64-systemd"})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit_dir = root / "usr/lib/systemd/system"
            unit_dir.mkdir(parents=True)
            for unit in ("NetworkManager", "bluetooth", "cups", "lightdm"):
                (unit_dir / f"{unit}.service").write_text("[Unit]\n")
            runner = ScriptedRunner()
            configurer(root, runner).configure_services(m)
            enabled = [command[4] for command in runner.commands if command[2:4] == ("systemctl", "enable")]
            self.assertIn("lightdm", enabled)
            self.assertNotIn("dbus", enabled)
            self.assertNotIn("elogind", enabled)
            self.assertFalse(any(command[2] == "rc-update" for command in runner.commands))

    def test_minimal_openrc_without_bluetooth_has_no_desktop_services(self) -> None:
        adds, _, _ = self.enabled(target_manifest(system={"desktop": "minimal"}, options={"bluetooth": False, "printing": False, "chrony": False, "cron": False, "logger": False}))
        self.assertEqual(adds, [("NetworkManager", "default")])

    def test_phases_split_base_and_desktop_services(self) -> None:
        m = target_manifest(system={"desktop": "kde"}, options={"ssh": True})
        base, _, _ = self.enabled(m, phase="base")
        desktop, _, _ = self.enabled(m, phase="desktop", installed=self.ALL_INSTALLED)
        self.assertIn(("NetworkManager", "default"), base)
        self.assertIn(("sshd", "default"), base)
        self.assertIn(("cronie", "default"), base)
        self.assertIn(("syslog-ng", "default"), base)
        self.assertFalse({"display-manager", "dbus", "bluetooth"} & {name for name, _ in base})
        self.assertEqual([name for name, _ in desktop], ["dbus", "elogind", "bluetooth", "cupsd", "display-manager"])

    def test_cron_is_enabled_for_the_logger_even_when_not_ticked(self) -> None:
        base, _, _ = self.enabled(target_manifest(options={"cron": False, "logger": True}), phase="base")
        self.assertIn(("cronie", "default"), base)

    def test_optional_services_without_a_package_are_skipped_with_a_warning(self) -> None:
        m = target_manifest(system={"desktop": "gnome"})
        adds, warnings, notes = self.enabled(m, phase="desktop", installed=("dbus", "display-manager", "elogind"))
        names = [name for name, _ in adds]
        self.assertEqual(names, ["dbus", "elogind", "display-manager"])
        self.assertEqual(sorted(w.split()[1] for w in warnings), ["bluetooth", "cupsd"])
        self.assertEqual(len(notes), 2)

    def test_display_manager_configuration_names_the_selected_manager(self) -> None:
        for desktop, manager in (("kde", "sddm"), ("gnome", "gdm"), ("xfce", "lightdm")):
            with self.subTest(desktop=desktop), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "etc/init.d").mkdir(parents=True)
                (root / "etc/init.d/display-manager").write_text("x")
                configurer(root).configure_services(target_manifest(system={"desktop": desktop}), phase="desktop")
                self.assertEqual(read(root, "etc/conf.d/display-manager"), f'DISPLAYMANAGER="{manager}"\n')

    def test_plan_services_and_console_font_are_enabled_when_present(self) -> None:
        plan = FakePlan(services=[("tlp", "default"), "thermald"])
        m = target_manifest(system={"desktop": "minimal", "console_font": "ter-132b"}, options={"bluetooth": False, "printing": False})
        adds, warnings, _ = self.enabled(m, plan, phase="desktop", installed=("tlp", "consolefont"))
        self.assertEqual(adds, [("consolefont", "boot"), ("tlp", "default")])
        self.assertEqual(warnings, ["Service thermald was not enabled because its package is not installed"])

    def test_essential_services_are_not_pre_checked_so_real_problems_stay_loud(self) -> None:
        runner = ScriptedRunner()
        runner.script("chroot /mnt/gentoo rc-update add NetworkManager", ("fail", "service `NetworkManager' does not exist"))
        with self.assertRaises(RunnerError):
            configurer("/mnt/gentoo", runner, dry_run=True).configure_services(target_manifest(), phase="base")

    def test_netifrc_link_is_created_in_the_base_phase(self) -> None:
        m = target_manifest(system={"desktop": "minimal"}, network={"manager": "netifrc", "interface": "enp1s0"})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc/init.d").mkdir(parents=True)
            (root / "etc/init.d/net.lo").write_text("stub")
            configurer(root).configure_services(m, phase="base")
            self.assertEqual(os.readlink(root / "etc/init.d/net.enp1s0"), "net.lo")

    def test_desktop_session_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = ScriptedRunner()
            c = configurer(root, runner)
            c.configure_desktop_session(target_manifest(system={"desktop": "xfce"}))
            text = read(root, "etc/xdg/autostart/gentoo-installer-polkit-agent.desktop")
            self.assertIn("Exec=/usr/libexec/polkit-gnome-authentication-agent-1", text)
            self.assertIn("OnlyShowIn=XFCE;", text)
            self.assertNotIn("AutostartCondition", text)
            c.configure_desktop_session(target_manifest(system={"desktop": "kde"}))
            self.assertEqual([c for c in runner.commands if "dconf" in c], [])
            c.configure_desktop_session(keyboard(desktop="gnome"))
            self.assertIn(("chroot", temporary, "dconf", "update"), runner.commands)


class WriteBehaviourTests(unittest.TestCase):
    def test_files_and_new_directories_belong_to_root_and_are_synced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            chowned: list[tuple[str, int, int]] = []
            fchowned: list[tuple[int, int]] = []
            synced: list[int] = []
            real_fsync = os.fsync
            with patch("os.geteuid", return_value=0), \
                    patch("os.chown", lambda path, uid, gid: chowned.append((str(path), uid, gid))), \
                    patch("os.fchown", lambda fd, uid, gid: fchowned.append((fd, gid))), \
                    patch("os.fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1]):
                configurer(temporary).write("etc/portage/package.use/installer", "x\n")
            created = [path for path, uid, gid in chowned]
            self.assertEqual([Path(path).relative_to(temporary).as_posix() for path in created],
                             ["etc", "etc/portage", "etc/portage/package.use"])
            self.assertTrue(all(uid == 0 and gid == 0 for _, uid, gid in chowned))
            self.assertEqual([gid for _, gid in fchowned], [0])
            self.assertGreaterEqual(len(synced), 2)  # the file and its directory
            self.assertEqual((Path(temporary) / "etc").stat().st_mode & 0o777, 0o755)

    def test_unprivileged_runs_do_not_try_to_chown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch("os.geteuid", return_value=1000), patch("os.chown", side_effect=AssertionError), patch("os.fchown", side_effect=AssertionError):
                configurer(temporary).write("etc/file", "x", 0o600)
            self.assertEqual((Path(temporary) / "etc/file").stat().st_mode & 0o777, 0o600)

    def test_a_failed_write_leaves_no_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            c = configurer(temporary)
            c.write("etc/file", "old")
            with patch("os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    c.write("etc/file", "new")
            self.assertEqual(sorted(path.name for path in (Path(temporary) / "etc").iterdir()), ["file"])
            self.assertEqual(read(Path(temporary), "etc/file"), "old")


if __name__ == "__main__":
    unittest.main()
