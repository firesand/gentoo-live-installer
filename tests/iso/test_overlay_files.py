"""The files that make the live session safe on somebody else's PC (root_overlay)."""
from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from xml.etree import ElementTree

import pytest

from .conftest import ROOT, load_script

verify = load_script("scripts/verify-iso.py", "verify_iso")
OVERLAY = ROOT / "iso/root_overlay"
CONTRACT = json.loads((ROOT / "iso/image-contract.json").read_text())

# Property names that exist in the xfce4-power-manager 4.20 binary of the r19 image
# (found with strings(1); the xfconf channel cannot be queried without a session).
XFPM_PROPERTIES = {
    "presentation-mode", "lid-action-on-ac", "lid-action-on-battery", "inactivity-on-ac",
    "inactivity-on-battery", "dpms-enabled", "dpms-on-ac-sleep", "dpms-on-ac-off",
    "dpms-on-battery-sleep", "dpms-on-battery-off", "critical-power-action",
    "lock-screen-suspend-hibernate", "inactivity-sleep-mode-on-ac", "inactivity-sleep-mode-on-battery",
    "power-button-action", "sleep-button-action", "hibernate-button-action", "battery-button-action",
}
THUNAR_VOLMAN_PROPERTIES = {
    "automount-drives", "automount-media", "autobrowse", "autoopen", "autorun", "autoburn", "autoipod",
    "autokeyboard", "automouse", "autophoto", "autoprinter", "autotablet",
}
AUTOCONFIG_KEYS = {
    "ACPI", "ALSA", "APM", "BRLTTY", "COLDPLUG", "DETECT", "DHCP", "DMRAID", "FIREWIRE", "GPM", "IDEDMA", "LVM",
    "MDADM", "NET", "NFS", "PASSWD", "PATA", "PCMCIA", "SATA", "SCSI", "SPEAKUP", "SSHD", "USB", "X11",
}


def read(relative: str) -> str:
    return (OVERLAY / relative).read_text(encoding="utf-8")


def test_lvm_does_not_autoactivate_volume_groups() -> None:
    text = read("etc/lvm/lvmlocal.conf")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert re.search(r"global\s*\{[^}]*event_activation\s*=\s*0", code)
    assert re.search(r"activation\s*\{[^}]*auto_activation_volume_list\s*=\s*\[\s*\]", code)


def test_mdadm_does_not_assemble_arrays_by_itself() -> None:
    lines = [l for l in read("etc/mdadm.conf").splitlines() if l.strip() and not l.startswith("#")]
    assert lines == ["AUTO -all"]


def test_logind_ignores_lid_and_idle_but_keeps_the_power_key() -> None:
    parser = configparser.ConfigParser(delimiters=("=",), interpolation=None)
    parser.optionxform = str  # keys are case sensitive
    parser.read_string(read("etc/elogind/logind.conf.d/50-gentoo-live.conf"))
    login = dict(parser["Login"])
    for key in ("HandleLidSwitch", "HandleLidSwitchExternalPower", "HandleLidSwitchDocked", "IdleAction"):
        assert login[key] == "ignore"
    assert not [key for key in login if key.startswith("HandlePowerKey")]


def properties(xml: str) -> dict[str, str]:
    """Flatten an xfconf channel into {path: value}."""
    channel = ElementTree.fromstring(xml)
    found: dict[str, str] = {}

    def walk(node: ElementTree.Element, prefix: str) -> None:
        for child in node.findall("property"):
            path = f"{prefix}/{child.get('name')}"
            if child.get("value") is not None:
                found[path] = child.get("value")
            walk(child, path)

    walk(channel, "")
    return found


def test_power_manager_never_suspends_or_blanks() -> None:
    xml = read("etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml")
    assert ElementTree.fromstring(xml).get("name") == "xfce4-power-manager"
    found = properties(xml)
    names = {path.rsplit("/", 1)[1] for path in found}
    assert names <= XFPM_PROPERTIES, names - XFPM_PROPERTIES
    prefix = "/xfce4-power-manager/"
    assert found[prefix + "presentation-mode"] == "true"
    assert found[prefix + "lid-action-on-ac"] == "0" and found[prefix + "lid-action-on-battery"] == "0"
    assert found[prefix + "dpms-enabled"] == "false"
    assert found[prefix + "critical-power-action"] == "0"
    assert found[prefix + "inactivity-on-ac"] == "14" and found[prefix + "inactivity-on-battery"] == "14"


def test_thunar_volman_does_not_automount() -> None:
    found = properties(read("etc/xdg/xfce4/xfconf/xfce-perchannel-xml/thunar-volman.xml"))
    assert {p.split("/")[1] for p in found} <= THUNAR_VOLMAN_PROPERTIES
    for name in ("automount-drives", "automount-media", "autobrowse", "autoopen", "autorun"):
        assert found[f"/{name}/enabled"] == "false"


def desktop_entry(relative: str) -> dict[str, str]:
    parser = configparser.ConfigParser(delimiters=("=",), interpolation=None, strict=True)
    parser.optionxform = str
    parser.read_string(read(relative))
    return dict(parser["Desktop Entry"])


def test_screen_saver_is_hidden_and_x_blanking_is_off() -> None:
    assert desktop_entry("etc/xdg/autostart/xfce4-screensaver.desktop")["Hidden"] == "true"
    entry = desktop_entry("etc/xdg/autostart/gentoo-live-noblank.desktop")
    arguments = shlex.split(entry["Exec"])
    assert arguments[:2] == ["sh", "-c"] and "xset s off" in arguments[2] and "xset -dpms" in arguments[2]
    assert entry["OnlyShowIn"] == "XFCE;"


def test_acpid_cannot_power_off_the_pc() -> None:
    script = OVERLAY / "etc/acpi/actions/powerbtn.sh"
    assert os.access(script, os.X_OK)
    code = [l for l in script.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]
    assert not any(re.search(r"shutdown|poweroff|halt|reboot", l) for l in code)
    result = subprocess.run(["sh", str(script)], capture_output=True, check=False)
    assert result.returncode == 0
    autoconfig = read("etc/conf.d/autoconfig")
    values = dict(re.findall(r'^([A-Z0-9]+)="([^"]*)"$', autoconfig, re.M))
    assert set(values) == AUTOCONFIG_KEYS
    assert values["ACPI"] == "no"
    # Everything but ACPI keeps livecd-tools' defaults.
    assert values["X11"] == "yes" and values["PASSWD"] == "yes" and values["LVM"] == "no"


def test_chrony_corrects_the_clock_but_never_writes_the_rtc() -> None:
    code = [l.strip() for l in read("etc/chrony/chrony.conf").splitlines() if l.strip() and not l.startswith("#")]
    assert any(l.startswith("pool ") for l in code)
    assert "makestep 1.0 -1" in code
    assert not [l for l in code if l.startswith(("rtcsync", "rtcfile"))]


def test_greeting_texts() -> None:
    motd, issue = read("etc/motd"), read("etc/issue")
    for text in (motd, issue):
        assert "gentoo-rescue" in text and "gentoo-installer" in text
        assert text.isascii(), "the Linux console does not show UTF-8 until the keymap is loaded"
    assert max(len(l) for l in motd.splitlines()) <= 80, "the console is 80 columns wide"
    assert "links" in motd, "the motd names the text browser; helpers.rc and the package list provide it"
    for text in (motd, issue):
        assert "Repair a Gentoo system that does not start" in text and "broken system" not in text, "the repair tool is for Gentoo, and the greeting says so"


def test_console_hint_is_printed_on_text_consoles_only() -> None:
    script = OVERLAY / "etc/profile.d/10-gentoo-rescue-hint.sh"
    base = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    console = subprocess.run(["bash", "--norc", "-i", "-c", f". {script}"], capture_output=True, text=True, env=base, check=False)
    assert "Repair a Gentoo system that does not start: sudo gentoo-rescue" in console.stdout
    assert "Install Gentoo (desktop): gentoo-installer" in console.stdout and "sudo gentoo-installer-text" in console.stdout
    assert max(len(line) for line in console.stdout.splitlines()) <= 80, "the console is 80 columns wide"
    desktop = subprocess.run(["bash", "--norc", "-i", "-c", f". {script}"], capture_output=True, text=True, env={**base, "DISPLAY": ":0"}, check=False)
    assert "gentoo-rescue" not in desktop.stdout
    script_mode = subprocess.run(["bash", "--norc", "-c", f". {script}"], capture_output=True, text=True, env=base, check=False)
    assert script_mode.stdout == "", "scripts and scp must not see the hint"


def test_the_live_session_starts_a_polkit_agent_without_gnome_only_conditions() -> None:
    """GParted and other pkexec programs need a password dialog. The packaged autostart file of polkit-gnome carries
    AutostartCondition=GNOME3 ... and NotShowIn=MATE;KDE;, so the overlay replaces it (same file name) with one for Xfce."""
    entry = desktop_entry("etc/xdg/autostart/polkit-gnome-authentication-agent-1.desktop")
    assert entry["Exec"] == "/usr/libexec/polkit-gnome-authentication-agent-1"
    assert entry["OnlyShowIn"] == "XFCE;" and entry["Type"] == "Application" and entry["NoDisplay"] == "true"
    assert not {"AutostartCondition", "NotShowIn", "Hidden"} & set(entry)
    assert "gnome-extra/polkit-gnome" in stage1_packages(), "the agent program comes from this package"
    assert "polkit-gnome-authentication-agent-1" in read("etc/xdg/autostart/polkit-gnome-authentication-agent-1.desktop")


def test_the_polkit_agent_file_has_the_name_the_package_uses(pinned_tree: Path) -> None:
    """The overlay only replaces the packaged file when the names are equal; otherwise two agents would start."""
    ebuild = sorted((pinned_tree / "gnome-extra/polkit-gnome").glob("*.ebuild"))[-1].read_text()
    assert "polkit-gnome-authentication-agent-1.desktop" in ebuild
    assert "Exec=/usr/libexec/polkit-gnome-authentication-agent-1" in ebuild
    assert "insinto /etc/xdg/autostart" in ebuild


def stage1_packages() -> set[str]:
    spec = verify.parse_spec((ROOT / "iso/specs/livecd-stage1.spec").read_text())
    return {re.sub(r"^=", "", a) for a in verify.spec_list(spec, "livecd/packages")}


def test_panel_launchers_have_programs_and_a_browser() -> None:
    """The launcher files and the browser helper are checked in detail by test_desktop_look.py."""
    helpers = dict(l.split("=", 1) for l in read("etc/xfce4/helpers.rc").splitlines() if "=" in l)
    assert helpers["WebBrowser"] == "gentoo-live-browser", "an empty WebBrowser opens the Preferred Applications dialog"
    assert "www-client/firefox-bin" in stage1_packages() and "www-client/links" in stage1_packages(), "links stays for the text console"


def test_panel_has_the_keyboard_layout_plugin() -> None:
    panel = ElementTree.fromstring(read("etc/xdg/xfce4/panel/default.xml"))
    plugin_ids = [v.get("value") for v in panel.find("property[@name='panels']/property[@name='panel-1']/property[@name='plugin-ids']").findall("value")]
    xkb = [p.get("name") for p in panel.find("property[@name='plugins']").findall("property") if p.get("value") == "xkb"]
    assert xkb and xkb[0].split("-")[1] in plugin_ids
    assert "xfce-extra/xfce4-xkb-plugin" in stage1_packages()


def test_existing_overlay_files_keep_their_behaviour() -> None:
    assert "need display-manager" in read("etc/init.d/xdm"), "the xdm shim stays; nox is honoured by display-manager-init (H3)"
    assert "rc-manager=file" in read("etc/NetworkManager/NetworkManager.conf")
    assert "autologin-user=gentoo" in read("etc/lightdm/lightdm.conf")


def test_every_overlay_file_is_covered_by_the_image_contract() -> None:
    listed = {item["path"] for item in CONTRACT["squashfs"]["required"]}
    ignored = {"etc/lightdm/lightdm.conf"}  # listed by name
    present = {
        str(p.relative_to(OVERLAY)) for p in OVERLAY.rglob("*")
        if p.is_file() and "usr/local/share" not in str(p)
    }
    assert present <= listed | ignored | {"etc/init.d/xdm"} | listed, sorted(present - listed)
    # ...and every overlay path the contract names exists in the overlay or comes from a package.
    package_provided = {
        "etc/init.d/gentoo-installerd", "etc/init.d/chronyd", "usr/bin/gentoo-installer", "usr/bin/gentoo-installerd",
        "usr/bin/hwmonux", "usr/bin/portagehub", "usr/libexec/portagehub-helper", "usr/bin/gentoo-rescue",
        "usr/bin/gentoo-settings", "usr/libexec/gentoo-settings-helper", "usr/share/applications/gentoo-settings.desktop",
        "usr/share/polkit-1/actions/org.gentoosettings.helper.policy", "usr/bin/kernelsmith", "usr/share/applications/kernelsmith.desktop",
        "usr/share/polkit-1/actions/org.kernelsmith.policy",
        "usr/bin/gentoo-rescue-gui", "usr/share/applications/gentoo-installer.desktop", "usr/share/applications/gentoo-rescue.desktop",
        "etc/modules-load.d/hwmonux.conf", "boot/memtest86plus/memtest64.bios", "boot/memtest86plus/memtest.efi64",
        "mnt/gentoo", "mnt/rescue", "home/gentoo", "usr/lib/modules", "usr/lib/firmware",
        "usr/bin/gentoo-installer-text", "usr/bin/gentoo-chroot", "usr/libexec/polkit-gnome-authentication-agent-1",
        "usr/lib/grub/i386-pc", "usr/lib/grub/x86_64-efi", "usr/bin/gentoo-remote-access", "usr/bin/gentoo-live-report",
        # wave 6: the approver service and the account come from the installer package and the fsscript; the AI rescue files
        # and console scripts from the gentoo_rescue_ai package (tests/iso/test_ai_rescue.py keeps them in step)
        "etc/init.d/gentoo-rescue-approver", "etc/conf.d/gentoo-rescue-approver", "etc/passwd", "etc/group", "home/rescue-ai",
        "etc/pam.d/su", "etc/gentoo-rescue/AGENTS.md", "etc/claude-code/CLAUDE.md", "etc/claude-code/managed-settings.json", "etc/skel-rescue-ai",
        "usr/bin/gentoo-rescue-agents", "usr/bin/gentoo-rescue-login", "usr/bin/gentoo-rescue-ai", "usr/bin/gentoo-rescue-do",
        "usr/bin/gentoo-rescue-approver", "usr/bin/gentoo-rescue-mount", "usr/bin/gentoo-rescue-share",
        # the desktop look: files of the packages arc-theme, xfwm4 (the HiDPI frames), papirus-icon-theme, firefox-bin, xfdesktop,
        # thunar, gparted and xfce4-terminal (test_desktop_look.py checks them against the tree)
        "usr/share/backgrounds/xfce/xfce-x.svg", "usr/share/themes/Arc-Dark/gtk-3.0/gtk.css", "usr/share/themes/Arc-Dark/xfwm4/themerc",
        "usr/share/themes/Default-xhdpi/xfwm4/themerc",
        "usr/share/icons/Papirus-Dark/index.theme", "usr/bin/firefox-bin", "usr/share/applications/xfce4-terminal.desktop",
        "usr/share/applications/thunar.desktop", "usr/share/applications/gparted.desktop",
    }
    for path in listed - package_provided:
        assert (OVERLAY / path).exists(), f"{path} is in the contract but neither in the overlay nor provided by a package"


def test_overlay_has_no_setuid_files_or_odd_modes() -> None:
    for path in OVERLAY.rglob("*"):
        if path.is_file():
            mode = path.stat().st_mode
            assert not mode & 0o7000, path
            assert not mode & 0o022, f"{path} is writable by group or others"


@pytest.mark.skipif(shutil.which("desktop-file-validate") is None, reason="desktop-file-validate is not installed")
def test_desktop_entries_pass_the_freedesktop_validator() -> None:
    files = sorted((OVERLAY / "etc/xdg/autostart").glob("*.desktop")) + [
        ROOT / "data/gentoo-rescue.desktop",
        ROOT / "packaging/gentoo-overlay/app-admin/gentoo-live-installer/files/gentoo-installer.desktop",
        ROOT / "apps/hwmonux/hwmonux.desktop",
    ]
    for path in files:
        result = subprocess.run(["desktop-file-validate", str(path)], capture_output=True, text=True, check=False)
        assert result.returncode == 0 and "error" not in result.stdout.lower(), f"{path.name}: {result.stdout}{result.stderr}"


@pytest.mark.skipif(shutil.which("xmllint") is None, reason="xmllint is not installed")
def test_xml_files_are_well_formed() -> None:
    files = [*OVERLAY.rglob("*.xml"), *(ROOT / "packaging/gentoo-overlay").rglob("metadata.xml")]
    assert len(files) >= 7
    for path in files:
        result = subprocess.run(["xmllint", "--noout", str(path)], capture_output=True, text=True, check=False)
        assert result.returncode == 0, f"{path}: {result.stderr}"


def test_the_greeting_does_not_name_a_command_the_package_lacks() -> None:
    """Integration tripwire: once the repair tool is in the tree, pyproject.toml must define `gentoo-rescue`."""
    if not (ROOT / "src/gentoo_installerd/rescue/__init__.py").exists():
        pytest.skip("the repair tool is not part of this tree")
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert re.search(r'^gentoo-rescue\s*=', pyproject, re.M), (
        "iso/root_overlay/etc/motd, etc/issue, the profile hint and iso/README-live.txt send users to `gentoo-rescue`; "
        "add it to [project.scripts] in pyproject.toml (for example gentoo-rescue = \"gentoo_installerd.rescue.cli:main\")"
    )
