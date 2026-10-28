"""The desktop set of the daemon: browser, Flatpak and Flathub, portals, the new desktops and their session files."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gentoo_installer import desktops

from tests.backend.target_helpers import ScriptedRunner, configurer, mark_installed, read, target_manifest

QUIET = {"bluetooth": False, "pipewire": False, "printing": False, "install_hwmonux": False, "install_portagehub": False,
         "install_gentoo_settings": False, "install_kernelsmith": False}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.runner = ScriptedRunner()
        self.c = configurer(self.root, self.runner)
        self.notes: list[str] = []
        self.c.notify = self.notes.append

    def emerges(self) -> list[str]:
        return [" ".join(command) for command in self.runner.commands if "emerge" in command]


class AtomTests(Base):
    def atoms(self, **sections):
        return self.c.desktop_atoms(target_manifest(**sections))

    def test_firefox_is_on_every_desktop_by_default(self) -> None:
        for key in desktops.DESKTOPS:
            with self.subTest(desktop=key):
                atoms = self.atoms(system={"desktop": key}, options=QUIET)
                self.assertEqual(desktops.FIREFOX in atoms.optional, key != "minimal")

    def test_the_browser_none_installs_no_browser(self) -> None:
        atoms = self.atoms(system={"desktop": "kde"}, options={**QUIET, "browser": "none"})
        self.assertNotIn(desktops.FIREFOX, atoms.everything())
        self.assertNotIn(desktops.FIREFOX_BIN, atoms.everything())

    def test_flatpak_flathub_and_the_portals_of_the_desktop(self) -> None:
        plain = self.atoms(system={"desktop": "cinnamon"}, options=QUIET)
        self.assertNotIn(desktops.FLATPAK, plain.optional)
        self.assertNotIn("sys-apps/xdg-desktop-portal-gtk", plain.optional)
        flat = self.atoms(system={"desktop": "cinnamon"}, options={**QUIET, "flatpak": True})
        self.assertIn(desktops.FLATPAK, flat.optional)
        self.assertIn("sys-apps/xdg-desktop-portal-gtk", flat.optional)
        self.assertIn("sys-apps/xdg-desktop-portal-xapp", flat.optional)
        hub = self.atoms(system={"desktop": "xfce"}, options={**QUIET, "flathub": True})  # the remote needs the program
        self.assertIn(desktops.FLATPAK, hub.optional)
        self.assertIn("sys-apps/xdg-desktop-portal-gtk", hub.optional)
        minimal = self.atoms(system={"desktop": "minimal"}, options={**QUIET, "flatpak": True})
        self.assertEqual(minimal.optional.count(desktops.FLATPAK), 1)
        self.assertFalse([atom for atom in minimal.optional if "portal" in atom])

    def test_the_package_use_of_plasma_depends_on_flatpak(self) -> None:
        without = self.c.package_use_table(target_manifest(system={"desktop": "kde"}))
        self.assertNotIn("kde-plasma/plasma-meta", without)
        with_flatpak = self.c.package_use_table(target_manifest(system={"desktop": "kde"}, options={"flatpak": True}))
        self.assertEqual(with_flatpak["kde-plasma/plasma-meta"], ["discover", "flatpak"])
        flathub = self.c.package_use_table(target_manifest(system={"desktop": "kde"}, options={"flathub": True}))
        self.assertEqual(flathub["kde-plasma/plasma-meta"], ["discover", "flatpak"])

    def test_every_desktop_installs_its_registry_set_and_nm_applet_follows_the_network_manager(self) -> None:
        for key in desktops.DESKTOPS:
            for init in ("openrc", "systemd"):
                with self.subTest(desktop=key, init=init):
                    m = target_manifest(system={"desktop": key, "init": init, "stage_variant": f"amd64-{init}"}, options=QUIET)
                    required = self.c.desktop_atoms(m).required
                    self.assertEqual(required[:len(desktops.required_atoms(key, init))] if key != "minimal" else [], desktops.required_atoms(key, init)
                                     if key != "minimal" else [])
                    if key != "minimal":
                        self.assertEqual("gui-libs/display-manager-init" in required, init == "openrc")
                        m.network.manager = "dhcpcd"
                        self.assertNotIn(desktops.NM_APPLET, self.c.desktop_atoms(m).required)


class InstallTests(Base):
    def manifest(self, desktop="kde", binhost=True, **options):
        m = target_manifest(system={"desktop": desktop}, options={**QUIET, **options}, portage={"binhost": binhost})
        return m

    def test_firefox_from_the_binary_host_when_it_has_the_package(self) -> None:
        self.c.install_desktop(self.manifest())
        self.assertTrue(any("--pretend" in e and "--usepkgonly" in e and desktops.FIREFOX in e for e in self.emerges()))
        optional = [e for e in self.emerges() if "--keep-going" in e and desktops.FIREFOX in e]
        self.assertTrue(optional)
        self.assertFalse(any(desktops.FIREFOX_BIN in e for e in self.emerges()))

    def test_firefox_bin_when_the_host_has_no_matching_package(self) -> None:
        self.runner.script("chroot " + str(self.root) + " emerge --pretend", ("fail", "no binary package"))
        self.c.install_desktop(self.manifest())
        self.assertTrue(any(desktops.FIREFOX_BIN in e and "--keep-going" in e for e in self.emerges()))
        self.assertFalse(any(e.endswith(desktops.FIREFOX) and "--keep-going" in e for e in self.emerges()))
        self.assertTrue(any("no matching Firefox" in note for note in self.notes))

    def test_firefox_bin_for_a_source_installation_without_asking_the_host(self) -> None:
        self.c.install_desktop(self.manifest(binhost=False))
        self.assertFalse(any("--pretend" in e for e in self.emerges()))
        self.assertTrue(any(desktops.FIREFOX_BIN in e and "--keep-going" in e for e in self.emerges()))

    def test_nothing_about_firefox_without_the_browser(self) -> None:
        self.c.install_desktop(self.manifest(browser="none"))
        self.assertFalse(any("firefox" in e for e in self.emerges()))

    def test_a_firefox_that_fails_to_build_is_replaced_by_the_binary_build(self) -> None:
        mark_installed(self.root, *desktops.required_atoms("kde", "openrc"), "gui-libs/display-manager-init")
        failure = ("fail", " * 1) www-client/firefox-1::gentoo (/log)\n!!! Fetch failed for firefox.tar.xz")
        self.runner.script("*--keep-going", failure, failure, failure)  # the desktop set (all installed), then both attempts at the optional set
        result = self.c.install_desktop(self.manifest())
        self.assertTrue(any(e.endswith(desktops.FIREFOX_BIN) for e in self.emerges()))
        self.assertTrue(any("firefox-bin" in note for note in self.notes))
        self.assertNotIn(desktops.FIREFOX, result.failed)

    def test_the_session_adds_the_flathub_remote_once_per_install(self) -> None:
        self.c.configure_desktop_session(self.manifest(flathub=True))
        remote = [c for c in self.runner.commands if "remote-add" in c]
        self.assertEqual(len(remote), 1)
        self.assertEqual(remote[0][-5:], ("remote-add", "--system", "--if-not-exists", "flathub", "https://dl.flathub.org/repo/flathub.flatpakrepo"))

    def test_no_flathub_remote_unless_asked_for(self) -> None:
        self.c.configure_desktop_session(self.manifest(flatpak=True))
        self.assertFalse([c for c in self.runner.commands if "remote-add" in c])

    def test_a_failing_flathub_remote_tells_how_to_add_it_later(self) -> None:
        self.runner.script("*remote-add", ("fail", "no network"))
        self.c.configure_desktop_session(self.manifest(flathub=True))
        self.assertTrue(any("flatpak remote-add --system --if-not-exists flathub" in note for note in self.notes))


class SessionFileTests(Base):
    def test_sway_gets_its_snippet(self) -> None:
        m = target_manifest(system={"desktop": "sway"}, options={"pipewire": True})
        self.c.configure_desktop_session(m)
        text = read(self.root, "etc/sway/config.d/50-gentoo-installer.conf")
        self.assertIn("exec /usr/libexec/polkit-mate-authentication-agent-1", text)
        self.assertIn("exec nm-applet --indicator", text)
        self.assertIn("swaybar_command waybar", text)
        self.assertIn("exec gentoo-pipewire-launcher restart", text)
        self.assertNotIn("xkb_layout", text)

    def test_sway_follows_the_keyboard_layout_and_the_network_manager(self) -> None:
        m = target_manifest(system={"desktop": "sway", "keyboard_layout": "de", "keyboard_variant": "nodeadkeys", "init": "systemd", "stage_variant": "amd64-systemd"},
                            network={"manager": "dhcpcd"})
        self.c.configure_desktop_session(m)
        text = read(self.root, "etc/sway/config.d/50-gentoo-installer.conf")
        self.assertIn("xkb_layout de", text)
        self.assertIn("xkb_variant nodeadkeys", text)
        self.assertNotIn("nm-applet", text)
        self.assertNotIn("pipewire-launcher", text)

    def test_i3_config_is_extended_once(self) -> None:
        (self.root / "etc/i3").mkdir(parents=True)
        (self.root / "etc/i3/config").write_text("set $mod Mod4\n")
        m = target_manifest(system={"desktop": "i3"}, options={"pipewire": True})
        self.c.configure_desktop_session(m)
        self.c.configure_desktop_session(m)
        text = read(self.root, "etc/i3/config")
        self.assertTrue(text.startswith("set $mod Mod4\n"))
        self.assertEqual(text.count("# --- Gentoo installer"), 1)
        self.assertIn("exec --no-startup-id nm-applet", text)
        self.assertIn("gentoo-pipewire-launcher restart", text)

    def test_a_missing_i3_config_is_reported(self) -> None:
        self.c.configure_desktop_session(target_manifest(system={"desktop": "i3"}))
        self.assertTrue(any("/etc/i3/config" in note for note in self.notes))

    def test_xfce_keeps_its_polkit_agent_and_the_others_write_no_session_file(self) -> None:
        for key in ("cinnamon", "mate", "lxqt", "gnome-light", "kde"):
            self.c.configure_desktop_session(target_manifest(system={"desktop": key}))
        self.assertFalse((self.root / "etc/sway").exists() or (self.root / "etc/i3").exists())
        self.assertFalse((self.root / "etc/xdg/autostart/gentoo-installer-polkit-agent.desktop").exists())

    def test_the_dconf_keyboard_follows_the_registry_for_gnome_light(self) -> None:
        m = target_manifest(system={"desktop": "gnome-light", "keyboard_layout": "de"})
        self.c.configure_desktop_session(m)
        self.assertIn(("chroot", str(self.root), "dconf", "update"), self.runner.commands)
        files = {path for path, _, _ in self.c.keyboard_files(m)}
        self.assertIn("etc/dconf/db/local.d/00-gentoo-installer-keyboard", files)
        self.assertIn("etc/X11/xorg.conf.d/00-keyboard.conf", files)
        sway = {path for path, _, _ in self.c.keyboard_files(target_manifest(system={"desktop": "cinnamon", "keyboard_layout": "de"}))}
        self.assertEqual(sway, {"etc/X11/xorg.conf.d/00-keyboard.conf"})


class ServiceTests(Base):
    def specs(self, desktop, init):
        m = target_manifest(system={"desktop": desktop, "init": init, "stage_variant": f"amd64-{init}"})
        return self.c.service_specs(m)

    def test_display_manager_and_settings_daemon_per_desktop(self) -> None:
        self.assertIn(("display-manager", "default", True), self.specs("cinnamon", "openrc"))
        self.assertIn(("openrc-settingsd", "default", True), self.specs("cinnamon", "openrc"))
        self.assertIn(("openrc-settingsd", "default", True), self.specs("mate", "openrc"))
        self.assertNotIn(("openrc-settingsd", "default", True), self.specs("mate", "systemd"))
        self.assertIn(("lightdm", "default", True), self.specs("i3", "systemd"))
        self.assertIn(("gdm", "default", True), self.specs("gnome-light", "systemd"))
        self.assertIn(("sddm", "default", True), self.specs("sway", "systemd"))
        self.assertIn(("sddm", "default", True), self.specs("lxqt", "systemd"))

    def test_the_openrc_display_manager_name_is_written_for_every_desktop(self) -> None:
        for key, name in (("lxqt", "sddm"), ("mate", "lightdm"), ("gnome-light", "gdm"), ("sway", "sddm"), ("i3", "lightdm"), ("cinnamon", "lightdm")):
            with self.subTest(desktop=key):
                root = self.root / key
                (root / "etc/init.d").mkdir(parents=True)
                (root / "etc/init.d/display-manager").write_text("#!/sbin/openrc-run\n")
                configurer(root, ScriptedRunner()).configure_services(target_manifest(system={"desktop": key}), phase="desktop")
                self.assertEqual(read(root, "etc/conf.d/display-manager"), f'DISPLAYMANAGER="{name}"\n')


if __name__ == "__main__":
    unittest.main()
