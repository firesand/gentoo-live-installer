"""Wave 5: the live desktop's look (Arc-Dark, Papirus-Dark, Fira Sans, JetBrains Mono, one 44 px bottom panel)."""
from __future__ import annotations

import configparser
import json
from pathlib import Path
import re
from xml.etree import ElementTree

import pytest

from .conftest import ROOT, load_script

verify = load_script("scripts/verify-iso.py", "verify_iso")
OVERLAY = ROOT / "iso/root_overlay"
XFCONF = "etc/xdg/xfce4/xfconf/xfce-perchannel-xml/"
CONTRACT = json.loads((ROOT / "iso/image-contract.json").read_text())

# Plugin types that exist in Xfce 4.20 (xfce4-panel 4.20.8 and the plugins of the image).
PLUGIN_TYPES = {
    "applicationsmenu", "actions", "clock", "launcher", "pager", "separator", "showdesktop", "systray", "tasklist",
    "directorymenu", "windowmenu",  # xfce4-panel itself
    "whiskermenu", "xkb", "pulseaudio",  # xfce4-whiskermenu-plugin, xfce4-xkb-plugin, xfce4-pulseaudio-plugin
}
# launcher file -> the package of the image that installs it (or the overlay)
LAUNCHERS = {
    "gentoo-installer.desktop": "app-admin/gentoo-live-installer",
    "gentoo-rescue.desktop": "app-admin/gentoo-live-installer",
    "xfce4-terminal.desktop": "x11-terms/xfce4-terminal",
    "thunar.desktop": "xfce-base/xfce4-meta",  # xfce-base/thunar, a dependency of the meta package
    "gparted.desktop": "sys-block/gparted",
    "gentoo-handbook.desktop": None,  # iso/root_overlay/usr/share/applications
}


def xml(relative: str) -> ElementTree.Element:
    return ElementTree.fromstring((OVERLAY / relative).read_text(encoding="utf-8"))


def xfconf_value(channel: ElementTree.Element, path: str) -> str | None:
    node = channel
    for name in path.strip("/").split("/"):
        node = next((c for c in node.findall("property") if c.get("name") == name), None)
        if node is None:
            return None
    return node.get("value")


def stage1() -> set[str]:
    spec = verify.parse_spec((ROOT / "iso/specs/livecd-stage1.spec").read_text())
    return {re.sub(r"^=", "", a) for a in verify.spec_list(spec, "livecd/packages")}


def greeter() -> dict[str, str]:
    parser = configparser.ConfigParser(delimiters=("=",), interpolation=None)
    parser.read_string((OVERLAY / "etc/lightdm/lightdm-gtk-greeter.conf").read_text())
    return dict(parser["greeter"])


# --- the xfconf channels ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["xsettings", "xfwm4", "xfce4-desktop", "xfce4-notifyd", "xfce4-panel"])
def test_every_channel_parses_and_is_named_like_its_file(name: str) -> None:
    path = f"{XFCONF}{name}.xml" if name != "xfce4-panel" else "etc/xdg/xfce4/panel/default.xml"
    channel = xml(path)
    assert channel.tag == "channel" and channel.get("name") == name and channel.get("version") == "1.0"
    for node in channel.iter("property"):
        assert node.get("type") in {"empty", "string", "int", "uint", "bool", "double", "array"}, (name, node.get("name"))
        if node.get("type") == "empty":
            assert node.get("value") is None


def test_the_gtk_window_and_icon_themes_agree_everywhere() -> None:
    settings = xml(XFCONF + "xsettings.xml")
    gtk = xfconf_value(settings, "Net/ThemeName")
    icons = xfconf_value(settings, "Net/IconThemeName")
    assert (gtk, icons) == ("Arc-Dark", "Papirus-Dark")
    assert xfconf_value(xml(XFCONF + "xfwm4.xml"), "general/theme") == gtk, "the frames use the dark Arc variant too"
    assert (greeter()["theme-name"], greeter()["icon-theme-name"]) == (gtk, icons)


def test_fonts_are_fira_sans_and_jetbrains_mono_with_the_greeter_and_terminal_in_step() -> None:
    settings = xml(XFCONF + "xsettings.xml")
    assert xfconf_value(settings, "Gtk/FontName") == "Fira Sans 10"
    assert xfconf_value(settings, "Gtk/MonospaceFontName") == "JetBrains Mono 11"
    assert greeter()["font-name"].startswith("Fira Sans ")
    assert xfconf_value(xml(XFCONF + "xfwm4.xml"), "general/title_font").startswith("Fira Sans ")
    terminal = configparser.ConfigParser(interpolation=None)
    terminal.optionxform = str
    terminal.read_string((OVERLAY / "etc/xdg/xfce4/terminal/terminalrc").read_text())
    config = terminal["Configuration"]
    assert config["FontName"] == "JetBrains Mono 11" and config["MiscMenubarDefault"] == "FALSE"
    assert re.fullmatch(r"#[0-9a-f]{6}", config["ColorBackground"]) and int(config["ColorBackground"][1:3], 16) < 0x60, "a dark terminal"
    assert len(config["ColorPalette"].split(";")) == 16
    # DejaVu stays installed as the fallback; fontconfig prefers the two families
    assert "media-fonts/dejavu" in stage1()
    text = (OVERLAY / "etc/fonts/conf.d/61-gentoo-live-fonts.conf").read_text()
    assert "<family>Fira Sans</family>" in text and "<family>JetBrains Mono</family>" in text


def test_the_compositor_has_shadows_and_works_without_3d() -> None:
    xfwm = xml(XFCONF + "xfwm4.xml")
    assert xfconf_value(xfwm, "general/use_compositing") == "true"
    assert xfconf_value(xfwm, "general/show_frame_shadow") == "true" and xfconf_value(xfwm, "general/show_popup_shadow") == "true"
    assert xfconf_value(xfwm, "general/vblank_mode") == "auto", "auto, so that a VM without 3D still composites"


def test_notifications_come_from_xfce4_notifyd() -> None:
    assert "xfce-extra/xfce4-notifyd" in stage1() and "gnome-extra/nm-applet" in stage1()
    assert xfconf_value(xml(XFCONF + "xfce4-notifyd.xml"), "theme") == "Default"


# --- the panel ------------------------------------------------------------------------------------


def panel() -> ElementTree.Element:
    return xml("etc/xdg/xfce4/panel/default.xml")


def plugins() -> dict[str, ElementTree.Element]:
    return {p.get("name"): p for p in panel().find("property[@name='plugins']").findall("property")}


def test_there_is_one_44_pixel_dark_locked_bottom_panel() -> None:
    root = panel()
    panels = root.find("property[@name='panels']")
    assert [v.get("value") for v in panels.findall("value")] == ["1"] and panels.find("property[@name='dark-mode']").get("value") == "true"
    assert [p.get("name") for p in panels.findall("property") if re.fullmatch(r"panel-\d+", p.get("name"))] == ["panel-1"]
    config = panels.find("property[@name='panel-1']")
    value = {p.get("name"): p.get("value") for p in config.findall("property")}
    assert value["size"] == "44" and value["length"] == "100" and value["position-locked"] == "true"
    assert value["position"].startswith("p=10;"), "p=10 is the bottom edge, as in Xfce's own bottom dock"
    assert value["background-style"] == "1", "a solid dark background, not the GTK theme's light default"
    channel_ids = [v.get("value") for v in config.find("property[@name='plugin-ids']").findall("value")]
    assert sorted(channel_ids, key=int) == sorted((n.split("-")[1] for n in plugins()), key=int), "every plugin is on the panel, none is missing"
    assert len(set(channel_ids)) == len(channel_ids)


def test_every_plugin_type_exists_in_xfce_4_20() -> None:
    types = {p.get("value") for p in plugins().values()}
    assert types <= PLUGIN_TYPES, types - PLUGIN_TYPES
    packages = {"whiskermenu": "xfce-extra/xfce4-whiskermenu-plugin", "xkb": "xfce-extra/xfce4-xkb-plugin", "pulseaudio": "xfce-extra/xfce4-pulseaudio-plugin"}
    for kind, package in packages.items():
        assert kind in types and package in stage1(), (kind, package)


def test_the_panel_order_is_menu_launchers_windows_status_clock_logout() -> None:
    root = panel()
    ids = [v.get("value") for v in root.find("property[@name='panels']/property[@name='panel-1']/property[@name='plugin-ids']").findall("value")]
    by_id = {name.split("-")[1]: p for name, p in plugins().items()}
    kinds = [by_id[i].get("value") for i in ids]
    launched = [v.get("value") for i in ids for v in by_id[i].iter("value") if (v.get("value") or "").endswith(".desktop")]
    assert kinds[0] == "whiskermenu" and kinds[-1] == "actions"
    assert launched == ["gentoo-installer.desktop", "gentoo-rescue.desktop", "xfce4-terminal.desktop", "thunar.desktop", "gparted.desktop", "gentoo-handbook.desktop"]
    order = [k for k in kinds if k != "separator" and k != "launcher"]
    assert order == ["whiskermenu", "tasklist", "systray", "pulseaudio", "xkb", "clock", "actions"]
    assert kinds.index("tasklist") > max(i for i, k in enumerate(kinds) if k == "launcher"), "the window list follows the launchers"


def test_the_menu_button_is_neutral_not_a_distribution_logo() -> None:
    icon = plugins()["plugin-1"].find("property[@name='button-icon']").get("value")
    assert not re.search(r"gentoo|start-here|distributor|logo", icon, re.I), icon


def test_the_clock_and_keyboard_plugins_show_what_the_plan_says() -> None:
    clock = {p.get("name"): p.get("value") for p in next(p for p in plugins().values() if p.get("value") == "clock").findall("property")}
    assert clock["digital-time-format"] == "%a %d %b  %H:%M"
    xkb = {p.get("name"): p.get("value") for p in next(p for p in plugins().values() if p.get("value") == "xkb").findall("property")}
    assert xkb["display-type"] == "1", "the layout is shown as text"


def desktop_file(relative: str) -> dict[str, str]:
    parser = configparser.ConfigParser(delimiters=("=",), interpolation=None, strict=True)
    parser.optionxform = str
    parser.read_string((OVERLAY / relative).read_text())
    return dict(parser["Desktop Entry"])


def test_every_launcher_is_installed_by_a_package_of_the_image() -> None:
    for name, package in LAUNCHERS.items():
        if package is None:
            entry = desktop_file(f"usr/share/applications/{name}")
            assert entry["Type"] == "Application" and entry["Exec"] and entry["Icon"]
        else:
            assert package in stage1(), (name, package)
    # the two project launchers come from the live package: the installer's from files/, the repair tool's from data/
    package_dir = ROOT / "packaging/gentoo-overlay/app-admin/gentoo-live-installer"
    assert (package_dir / "files/gentoo-installer.desktop").is_file() and (ROOT / "data/gentoo-rescue.desktop").is_file()
    ebuild = next(package_dir.glob("*.ebuild")).read_text()
    assert 'domenu "${FILESDIR}/gentoo-installer.desktop"' in ebuild and 'domenu "${S}/data/gentoo-rescue.desktop"' in ebuild
    # and the contract demands the files the panel points at
    required = {item["path"] for item in CONTRACT["squashfs"]["required"]}
    for name in LAUNCHERS:
        assert f"usr/share/applications/{name}" in required, name


def test_the_handbook_launcher_opens_the_amd64_handbook_in_the_browser_of_the_image() -> None:
    entry = desktop_file("usr/share/applications/gentoo-handbook.desktop")
    assert entry["Exec"] == "exo-open --launch WebBrowser https://wiki.gentoo.org/wiki/Handbook:AMD64"
    assert entry["Terminal"] == "false"
    helper = desktop_file("usr/share/xfce4/helpers/gentoo-live-browser.desktop")
    assert helper["Type"] == "X-XFCE-Helper" and helper["X-XFCE-Category"] == "WebBrowser"
    assert helper["X-XFCE-Binaries"] == "firefox-bin;" and helper["X-XFCE-CommandsWithParameter"] == '%B "%s"'
    rc = dict(l.split("=", 1) for l in (OVERLAY / "etc/xfce4/helpers.rc").read_text().splitlines() if "=" in l)
    assert rc["WebBrowser"] == "gentoo-live-browser" == "gentoo-live-browser.desktop".removesuffix(".desktop")
    assert (OVERLAY / "usr/share/xfce4/helpers/gentoo-live-browser.desktop").is_file()
    assert "www-client/firefox-bin" in stage1() and "www-client/links" in stage1(), "Firefox for the desktop, links for the text console"
    # the handbook URL is the one the installer's own help links use
    help_source = (ROOT / "src/gentoo_installer/pages/help.py").read_text()
    assert 'HANDBOOK = "https://wiki.gentoo.org/wiki/Handbook:AMD64/"' in help_source


# --- the desktop background -----------------------------------------------------------------------


def test_the_default_wallpaper_is_indigo_silk_through_the_file_xfdesktop_names() -> None:
    fsscript = (ROOT / "iso/fsscript-stage2.sh").read_text()
    assert 'default_backdrop=/usr/share/backgrounds/xfce/xfce-x.svg' in fsscript
    # the build stops when xfdesktop no longer names the file, before the link replaces it
    check = fsscript.index('grep -aqF "${default_backdrop}" /usr/bin/xfdesktop')
    link = fsscript.index('ln -sfn ../gentoo-live/indigo-silk.svg "${default_backdrop}"')
    assert check < link and "|| fail" in fsscript[check:link]
    # the relative link resolves to the picture the overlay ships
    resolved = (Path("/usr/share/backgrounds/xfce") / "../gentoo-live/indigo-silk.svg").resolve()
    assert str(resolved) == "/usr/share/backgrounds/gentoo-live/indigo-silk.svg"
    assert (OVERLAY / "usr/share/backgrounds/gentoo-live/indigo-silk.svg").is_file()
    desktop = xml(XFCONF + "xfce4-desktop.xml")
    assert xfconf_value(desktop, "backdrop/screen0/monitorVirtual-1/workspace0/last-image") == "/usr/share/backgrounds/gentoo-live/indigo-silk.svg"
    assert xfconf_value(desktop, "backdrop/screen0/monitorVirtual-1/workspace0/image-style") == "5"
    assert xfconf_value(desktop, "backdrop/single-workspace-mode") == "true"
    assert greeter()["background"] == "/usr/share/backgrounds/gentoo-live/indigo-silk.svg"
    assert "xfce-x.svg" in {Path(i["path"]).name for i in CONTRACT["squashfs"]["required"]}


def test_there_are_no_desktop_icons() -> None:
    desktop = xml(XFCONF + "xfce4-desktop.xml")
    assert xfconf_value(desktop, "desktop-icons/style") == "0"
    for name in ("filesystem", "home", "trash", "removable"):
        assert xfconf_value(desktop, f"desktop-icons/file-icons/show-{name}") == "false"


def test_the_gentoo_logo_is_not_added_anywhere_new() -> None:
    """The Gentoo name and logo guidelines do not allow the logo on an unofficial product."""
    new_files = [p for p in OVERLAY.rglob("*") if p.is_file() and p.suffix in {".svg", ".png", ".xml", ".conf", ".desktop", ".rc"}]
    for path in new_files:
        if path.name == "indigo-silk.svg":
            continue
        assert not re.search(r"gentoo[-_]?(logo|signet)", path.read_text(errors="replace"), re.I), path


# --- the image: packages, flags, the build check ----------------------------------------------------


def test_the_look_packages_are_in_stage1_and_the_contract() -> None:
    wanted = {
        "x11-themes/arc-theme", "x11-themes/papirus-icon-theme", "media-fonts/fira-sans", "media-fonts/jetbrains-mono",
        "xfce-extra/xfce4-notifyd", "xfce-extra/xfce4-pulseaudio-plugin", "www-client/firefox-bin",
    }
    assert wanted <= stage1()
    assert wanted <= set(CONTRACT["squashfs"]["required_packages"])
    assert "media-fonts/noto" not in stage1(), "noto is a 1.1 GB download"


def test_arc_is_built_for_gtk3_gtk4_and_xfwm4_without_the_gtk2_theme() -> None:
    use = {l.split()[0]: l.split()[1:] for l in (ROOT / "iso/portage/package.use/live-installer").read_text().splitlines() if l.strip() and not l.startswith("#")}
    assert use["x11-themes/arc-theme"] == ["xfce", "-gtk2"]


def test_the_fsscript_stops_the_build_when_a_theme_icon_or_font_is_missing() -> None:
    fsscript = (ROOT / "iso/fsscript-stage2.sh").read_text()
    for needle in ("/usr/share/themes/Arc-Dark/gtk-3.0/gtk.css", "/usr/share/themes/Arc-Dark/xfwm4/themerc", "/usr/share/icons/Papirus-Dark/index.theme",
                   "/usr/bin/firefox-bin", '"Fira Sans" "JetBrains Mono"'):
        assert needle in fsscript
    for path in re.findall(r"^\t(/usr/share/(?:themes|icons)/\S+) \\$", fsscript, re.M):
        assert path.lstrip("/") in {i["path"] for i in CONTRACT["squashfs"]["required"]}, f"the contract does not require {path}"


def test_the_names_in_the_configuration_are_what_the_packages_install(pinned_tree: Path) -> None:
    arc = (pinned_tree / "x11-themes/arc-theme").glob("*.ebuild")
    text = next(iter(sorted(arc))).read_text()
    assert "usev xfce xfwm" in text
    assert re.search(r'IUSE=".*\bxfce\b', text) and re.search(r'IUSE=".*\+gtk3\b', text)
    papirus = sorted((pinned_tree / "x11-themes/papirus-icon-theme").glob("*.ebuild"))
    assert papirus and "Free and open source SVG icon theme" in papirus[0].read_text()
    firefox = sorted((pinned_tree / "www-client/firefox-bin").glob("*.ebuild"))[0].read_text()
    assert 'newbin "${FILESDIR}/${PN}-r1.sh" ${PN}' in firefox and "firefox-bin.desktop" in firefox or 'desktop_filename="${PN}.desktop"' in firefox


def test_every_package_is_stable_on_amd64_and_its_licence_is_free(pinned_tree: Path) -> None:
    groups = (pinned_tree / "profiles/license_groups").read_text()
    free = set()

    def expand(name: str) -> None:
        line = next(l for l in groups.splitlines() if l.startswith(name + " "))
        for word in line.split()[1:]:
            if word.startswith("@"):
                expand(word[1:])
            else:
                free.add(word)

    expand("FREE")
    for atom in ("x11-themes/arc-theme", "x11-themes/papirus-icon-theme", "media-fonts/fira-sans", "media-fonts/jetbrains-mono",
                 "xfce-extra/xfce4-notifyd", "xfce-extra/xfce4-pulseaudio-plugin", "www-client/firefox-bin"):
        ebuilds = sorted((pinned_tree / atom).glob("*.ebuild"))
        assert ebuilds, atom
        stable = [e for e in ebuilds if re.search(r'^KEYWORDS=".*(?<![~-])\bamd64\b', e.read_text(), re.M)]
        assert stable, f"{atom} has no version that is stable on amd64"
        licence = re.search(r'^LICENSE="([^"]*)"', stable[-1].read_text(), re.M).group(1)
        assert set(re.findall(r"[A-Za-z][\w.+-]*", re.sub(r"[()|]", " ", licence))) - {"||"} <= free, (atom, licence)
