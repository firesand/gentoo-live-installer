"""Vendor tools and laptop extras of the plan: Razer (B10), RGB and controllers (B11), vendor warnings (B12), ASUS ROG and TUF (B13),
the laptop stage (B09), PipeWire (B16) and the desktop-coupled rules (B20)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from gentoo_installer import devices
from gentoo_installer import hardware as hw
from gentoo_installer.hardware import HardwareProfile
from gentoo_installerd import hwplan

from tests.hardware.builders import gpu, manifest, profile, systemd
from tests.hardware.helpers import fixture_dict

RAZER = "sys-apps/openrazer"
EDORP = "edorp git https://github.com/firesand/edorp-overlay.git"
GURU = "guru git https://github.com/gentoo/guru.git"


def plan_for(name: str, m=None, **sections):
    machine = fixture_dict(name)
    return hwplan.plan(m or manifest(**sections), machine)


def files_of(result):
    return {path: content for path, content, _mode in result.files}


# -- B10: Razer -----------------------------------------------------------------------------------------------

def test_the_users_viper_is_found_and_openrazer_is_preticked_in_guided():
    machine = HardwareProfile.from_dict(fixture_dict("asus-desktop-rtx4090"))
    offers = {offer.key: offer for offer in devices.device_offers(machine)}
    assert offers["openrazer"].default is True and "1532:0091" in offers["openrazer"].found
    assert offers["openrazer"].built_here and not offers["openrazer"].overlay
    assert devices.wanted(manifest(), list(offers.values()))["openrazer"] is True
    assert devices.wanted(manifest(hardware={"devices": {"openrazer": False}}), list(offers.values()))["openrazer"] is False


def test_openrazer_on_openrc_links_the_autostart_file_because_the_ebuild_does_not_start_the_daemon():
    result = plan_for("asus-desktop-rtx4090")
    assert RAZER in result.optional_atoms and "plugdev" in result.groups
    assert result.package_use[RAZER] == ["dist-kernel"]
    link = next(step for step in result.post_steps if "openrazer-daemon.desktop" in step[2])
    assert "/usr/share/openrazer/openrazer-daemon.desktop" in link[2] and "/etc/xdg/autostart/openrazer-daemon.desktop" in link[2]
    assert link[2].rstrip().endswith("exit 0")                       # a failed build leaves nothing to link and must not fail the install
    assert result.user_services == []                               # OpenRC has no user units


def test_openrazer_on_systemd_also_enables_the_user_unit_globally():
    machine = fixture_dict("asus-desktop-rtx4090")
    result = hwplan.plan(systemd(), machine)
    assert ("openrazer-daemon.service", "service") in result.user_services
    assert any("openrazer-daemon.desktop" in step[2] for step in result.post_steps)


def test_the_front_end_follows_the_desktop_and_a_blade_without_a_usb_device_still_gets_openrazer():
    assert "sys-apps/razergenie" in plan_for("asus-desktop-rtx4090").optional_atoms
    xfce = plan_for("asus-desktop-rtx4090", system={"desktop": "xfce"})
    assert "sys-apps/polychromatic" in xfce.optional_atoms and "sys-apps/razergenie" not in xfce.optional_atoms
    off = plan_for("asus-desktop-rtx4090", hardware={"devices": {"razer_frontend": False}})
    assert RAZER in off.optional_atoms and not {"sys-apps/razergenie", "sys-apps/polychromatic"} & set(off.optional_atoms)
    blade = plan_for("razer-blade-laptop")
    assert RAZER in blade.optional_atoms and "plugdev" in blade.groups


def test_a_plain_virtual_machine_offers_and_plans_no_device_tools():
    machine = HardwareProfile.from_dict(fixture_dict("qemu-q35-virtio"))
    assert devices.device_offers(machine) == []
    result = plan_for("qemu-q35-virtio")
    assert not result.groups and not result.package_keywords and not result.repos and RAZER not in result.optional_atoms


def test_the_unticked_razer_choice_and_secure_boot():
    off = plan_for("asus-desktop-rtx4090", hardware={"devices": {"openrazer": False}})
    assert RAZER not in off.optional_atoms and "plugdev" not in off.groups
    signing = manifest(system={"secure_boot": True, "secure_boot_key": "/k", "secure_boot_certificate": "/c"})
    machine = fixture_dict("asus-desktop-rtx4090")
    offers = devices.device_offers(HardwareProfile.from_dict(machine), signing)
    razer = next(offer for offer in offers if offer.key == "openrazer")
    assert razer.blocked and "unsigned" in razer.blocked
    assert RAZER not in hwplan.plan(signing, machine).optional_atoms            # the default is off while signing is on ...
    explicit = manifest(hardware={"devices": {"openrazer": True}}, system={"secure_boot": True, "secure_boot_key": "/k", "secure_boot_certificate": "/c"})
    assert "hw.device_secure_boot" in {i.code for i in hwplan.validate_plan(explicit, machine) if i.severity == "error"}


# -- B11: RGB, controllers, Logitech ------------------------------------------------------------------------------

def test_the_users_pc_offers_openrgb_game_rules_and_razer_and_builds_nothing_silently():
    machine = HardwareProfile.from_dict(fixture_dict("asus-desktop-rtx4090"))
    offers = {offer.key: offer for offer in devices.device_offers(machine)}
    assert {"openrazer", "openrgb", "game_rules"} <= set(offers)
    assert offers["openrgb"].default is False and offers["game_rules"].default is False and offers["game_rules"].testing
    result = plan_for("asus-desktop-rtx4090")
    assert "app-misc/openrgb" not in result.optional_atoms and "games-util/game-device-udev-rules" not in result.optional_atoms
    assert "i2c" not in result.groups and "/etc/modules-load.d/installer-i2c-dev.conf" not in files_of(result) and not result.package_keywords


def test_openrgb_adds_the_i2c_group_and_the_module_only_when_chosen():
    result = plan_for("asus-desktop-rtx4090", hardware={"devices": {"openrgb": True}})
    assert "app-misc/openrgb" in result.optional_atoms and "i2c" in result.groups
    assert "i2c-dev" in files_of(result)["/etc/modules-load.d/installer-i2c-dev.conf"]


@pytest.mark.parametrize(("init", "flag"), [("openrc", "elogind"), ("systemd", "systemd")])
def test_game_rules_are_one_exact_testing_version_with_acl(init, flag):
    m = (systemd if init == "systemd" else manifest)(hardware={"devices": {"game_rules": True}})
    result = hwplan.plan(m, fixture_dict("asus-desktop-rtx4090"))
    assert result.package_keywords == {"=games-util/game-device-udev-rules-20260625": ["~amd64"]}
    assert result.package_use["games-util/game-device-udev-rules"] == ["acl", flag]
    assert "games-util/game-device-udev-rules" in result.optional_atoms


def test_no_keyword_is_ever_global_or_unversioned():
    for name in ("asus-desktop-rtx4090", "asus-rog-laptop", "arc-ultra-convertible"):
        for choice in ({"game_rules": True, "openrgb": True, "asus_tools": True, "iio_sensors": True},):
            result = plan_for(name, hardware={"devices": choice})
            for atom, words in result.package_keywords.items():
                assert re.fullmatch(r"=[a-z0-9-]+/[A-Za-z0-9_+-]+-[0-9][A-Za-z0-9_.+-]*", atom), atom
                assert words == ["~amd64"]
            assert "*/*" not in result.package_keywords


# -- B12: vendor warnings -----------------------------------------------------------------------------------------

def warned(**machine_fields):
    profile_ = HardwareProfile.from_dict(profile(form="laptop", gpus=(gpu("intel", "gen8+", integrated=True, boot=True), gpu("nvidia", "ampere_plus"))))
    for name, value in machine_fields.items():
        setattr(profile_.machine, name, value)
    return {warning.code: warning for warning in hw.derive_warnings(profile_)}


def test_surface_and_apple_t2_are_warned_about_with_may():
    surface = warned(vendor_family="microsoft", oem="surface")["hw.vendor_surface"]
    assert surface.severity == "warning" and "may" in surface.message and "unsupported" not in surface.message.lower()
    t2 = HardwareProfile.from_dict(profile())
    t2.machine.vendor_family = "apple"
    t2.storage_controllers = [hw.StorageController(vendor_id="106b", device_id="2005", kind="nvme")]
    assert hw.derive_warnings(t2)[[w.code for w in hw.derive_warnings(t2)].index("hw.vendor_apple_t2")].severity == "warning"
    plain_mac = HardwareProfile.from_dict(profile())
    plain_mac.machine.vendor_family = "apple"
    assert "hw.vendor_apple_t2" not in {w.code for w in hw.derive_warnings(plain_mac)}


def test_victus_hybrid_and_legion_get_information_only():
    found = warned(vendor_family="hp", oem="victus")
    assert found["hw.vendor_victus"].severity == "info"
    legion = warned(vendor_family="lenovo", oem="legion")["hw.vendor_legion"]
    assert legion.severity == "info" and "LenovoLegionLinux" in legion.message
    assert "hw.vendor_victus" not in warned(vendor_family="hp", oem="omen")
    assert not {code for code in warned() if code.startswith("hw.vendor_")}


# -- B13: ASUS ROG and TUF laptops -------------------------------------------------------------------------------

ASUS = "sys-power/asusctl"
SUPERGFX = "sys-power/supergfxctl"


def asus_plan(init="openrc", fixture="asus-rog-laptop", **hardware):
    maker = systemd if init == "systemd" else manifest
    return hwplan.plan(maker(hardware={"devices": {"asus_tools": True}, **hardware}), fixture_dict(fixture))


@pytest.mark.parametrize(("init", "flag"), [("openrc", "-systemd"), ("systemd", "systemd")])
def test_asus_tools_on_a_hybrid_rog_laptop(init, flag):
    result = asus_plan(init)
    assert result.repos == [GURU, EDORP]                                    # edorp lists GURU as its master
    assert {ASUS, SUPERGFX} <= set(result.optional_atoms)
    assert result.package_keywords == {f"={ASUS}-6.5.0": ["~amd64"], f"={SUPERGFX}-5.2.7": ["~amd64"]}
    assert result.package_use[ASUS] == ["gui", flag] and result.package_use[SUPERGFX] == [flag]
    assert [name for name, _level in result.optional_services if name in ("asusd", "asus-shutdown", "supergfxd")] == ["asusd", "asus-shutdown", "supergfxd"]
    assert result.scalars["asus_tools"] == "asusctl+supergfxctl" and result.scalars["power"] == "none"
    assert not {"sys-power/tlp", "sys-power/power-profiles-daemon"} & set(result.all_atoms())
    assert any("unofficial" in note and "GURU" in note and "power-profiles-daemon" in note for note in result.notes)


def test_supergfxctl_only_on_hybrid_laptops():
    from tests.hardware.builders import profile as make

    single = make(form="laptop", gpus=(gpu("amd", "gcn3+", boot=True, integrated=True),))
    single["machine"].update(sys_vendor="ASUSTeK COMPUTER INC.", vendor_family="asus", oem="tuf")
    single["platform"] = {"asus_nb_wmi": True}
    result = hwplan.plan(manifest(hardware={"devices": {"asus_tools": True}}), single)
    assert ASUS in result.optional_atoms and SUPERGFX not in result.optional_atoms and result.scalars["asus_tools"] == "asusctl"
    assert "supergfxd" not in [name for name, _ in result.optional_services]


def test_asus_tools_are_never_planned_on_an_asus_desktop_or_another_make():
    for fixture in ("asus-desktop-rtx4090", "razer-blade-laptop", "thinkpad-amd-laptop"):
        result = asus_plan(fixture=fixture)
        assert ASUS not in result.optional_atoms and not result.repos
        assert any(w.code == "hw.asus_tools_skipped" for w in result.warnings), fixture
    assert "asus_tools" not in {offer.key for offer in devices.device_offers(HardwareProfile.from_dict(fixture_dict("asus-desktop-rtx4090")))}
    assert "asus_tools" in {offer.key for offer in devices.device_offers(HardwareProfile.from_dict(fixture_dict("asus-rog-laptop")))}


def test_the_asus_tools_are_off_until_chosen_and_say_what_they_are():
    offer = next(o for o in devices.device_offers(HardwareProfile.from_dict(fixture_dict("asus-rog-laptop"))) if o.key == "asus_tools")
    assert offer.default is False and offer.overlay and offer.built_here and "GURU" in offer.note and "unofficial" in offer.note
    plain = hwplan.plan(manifest(), fixture_dict("asus-rog-laptop"))
    assert ASUS not in plain.optional_atoms and not plain.repos and plain.scalars["power"] == "ppd"


@pytest.mark.parametrize(("hardware", "fragment"), [({"power": "tlp"}, "TLP"), ({"power": "ppd"}, "power-profiles-daemon"), ({"battery_charge_limit": 80}, "battery limit")])
def test_asus_tools_conflict_with_tlp_and_ppd_and_the_message_says_why(hardware, fragment):
    m = manifest(hardware={"devices": {"asus_tools": True}, **hardware})
    errors = [i for i in hwplan.validate_plan(m, fixture_dict("asus-rog-laptop")) if i.code == "hw.asus_power_conflict"]
    assert errors and "conflict" in errors[0].message


def test_asus_tools_conflict_with_other_gpu_switchers_and_with_extra_power_packages():
    m = manifest(hardware={"devices": {"asus_tools": True}}, options={"extra_packages": ["sys-power/envycontrol", "sys-power/tlp"]})
    codes = {i.code for i in hwplan.validate_plan(m, fixture_dict("asus-rog-laptop")) if i.severity == "error"}
    assert {"hw.asus_power_conflict", "hw.asus_gpu_conflict"} <= codes


# -- B09: the laptop stage ---------------------------------------------------------------------------------------

def test_the_core_ultra_convertible_gets_only_what_the_probe_proved():
    result = plan_for("arc-ultra-convertible")
    use = result.package_use
    assert "virtualkeyboard" in use["kde-plasma/plasma-meta"] and "wacom" in use["kde-plasma/plasma-meta"] and "thunderbolt" not in use["kde-plasma/plasma-meta"]
    assert any(w.code == "hw.camera_ipu" and w.severity == "info" for w in result.warnings)
    assert any(w.code == "hw.sleep_s2idle" for w in result.warnings)
    assert "gnome-extra/iio-sensor-proxy" not in result.optional_atoms        # offered, not planned: from an unofficial repository


def test_each_laptop_item_needs_its_own_evidence():
    from_thinkpad = plan_for("thinkpad-amd-laptop")
    assert "kde-plasma/plasma-meta" not in from_thinkpad.package_use or not {"wacom", "virtualkeyboard", "thunderbolt"} & set(from_thinkpad.package_use["kde-plasma/plasma-meta"])
    assert not [w for w in from_thinkpad.warnings if w.code in ("hw.camera_ipu",)]
    desktop = plan_for("asus-desktop-rtx4090")
    assert not [w for w in desktop.warnings if w.code in ("hw.sleep_s2idle", "hw.camera_ipu")]


def test_thunderbolt_on_a_laptop_adds_bolt_for_kde_through_plasma_meta_and_otherwise_on_its_own():
    machine = profile(form="laptop", gpus=(gpu("amd", "gcn3+", boot=True, integrated=True),))
    machine["peripherals"] = {"thunderbolt": True}
    kde = hwplan.plan(manifest(), machine)
    assert "thunderbolt" in kde.package_use["kde-plasma/plasma-meta"] and "sys-apps/bolt" not in kde.optional_atoms
    gnome = hwplan.plan(manifest(system={"desktop": "gnome"}), machine)
    assert "sys-apps/bolt" in gnome.optional_atoms and ("boltd", "default") in gnome.optional_services
    off = hwplan.plan(manifest(hardware={"devices": {"bolt": False}}), machine)
    assert "sys-apps/bolt" not in off.optional_atoms and "kde-plasma/plasma-meta" not in off.package_use


def test_a_fingerprint_reader_adds_fprint_to_gdm_and_nothing_to_kde():
    machine = profile(form="laptop", gpus=(gpu("amd", "gcn3+", boot=True, integrated=True),))
    gnome = hwplan.plan(manifest(system={"desktop": "gnome"}, hardware={"fingerprint": True}), machine)
    assert "fprint" in gnome.package_use["gnome-base/gdm"] and "sys-auth/fprintd" in gnome.optional_atoms
    kde = hwplan.plan(manifest(hardware={"fingerprint": True}), machine)
    assert "sys-auth/fprintd" in kde.optional_atoms and "gnome-base/gdm" not in kde.package_use
    via_device = hwplan.plan(manifest(hardware={"devices": {"fingerprint": True}}), machine)
    assert "sys-auth/fprintd" in via_device.optional_atoms


def test_bluetooth_is_switched_on_automatically_only_where_there_is_bluetooth():
    laptop = plan_for("thinkpad-amd-laptop")
    assert "AutoEnable=true" in files_of(laptop)["/etc/bluetooth/main.conf.d/10-installer.conf"]
    assert "/etc/bluetooth/main.conf.d/10-installer.conf" not in files_of(plan_for("asus-desktop-rtx4090"))
    assert "/etc/bluetooth/main.conf.d/10-installer.conf" not in files_of(plan_for("thinkpad-amd-laptop", options={"bluetooth": False}))


def test_fwupd_gets_its_openrc_service_and_is_on_by_default_for_the_makers_that_use_lvfs():
    from gentoo_installer import recommend

    openrc = plan_for("thinkpad-amd-laptop", hardware={"fwupd": True})
    assert ("fwupd", "default") in openrc.optional_services and "sys-apps/fwupd" in openrc.optional_atoms
    assert ("fwupd", "default") not in hwplan.plan(systemd(hardware={"fwupd": True}), fixture_dict("thinkpad-amd-laptop")).optional_services
    seeded = manifest()
    recommend.apply_defaults(seeded, fixture_dict("thinkpad-amd-laptop"))
    assert seeded.hardware.fwupd is True                                 # Lenovo publishes on the LVFS
    asus = manifest()
    recommend.apply_defaults(asus, fixture_dict("asus-rog-laptop"))
    assert asus.hardware.fwupd is False
    vm = manifest()
    recommend.apply_defaults(vm, fixture_dict("qemu-q35-virtio"))
    assert vm.hardware.fwupd is False


def test_the_battery_limit_forces_tlp_and_writes_its_file():
    result = plan_for("thinkpad-amd-laptop", hardware={"battery_charge_limit": 80})
    assert result.scalars["power"] == "tlp" and "sys-power/tlp" in result.optional_atoms
    conf = files_of(result)["/etc/tlp.d/90-installer-battery.conf"]
    assert "STOP_CHARGE_THRESH_BAT0=80" in conf and "START_CHARGE_THRESH_BAT0=75" in conf
    assert "/etc/tlp.d/90-installer-battery.conf" not in files_of(plan_for("thinkpad-amd-laptop"))
    errors = hwplan.validate_plan(manifest(hardware={"battery_charge_limit": 60, "power": "ppd"}), fixture_dict("thinkpad-amd-laptop"))
    assert "hw.power_conflict" in {i.code for i in errors if i.severity == "error"}
    desktop = hwplan.validate_plan(manifest(hardware={"battery_charge_limit": 60}), fixture_dict("asus-desktop-rtx4090"))
    assert "hw.battery_limit_unsupported" in {i.code for i in desktop if i.severity == "error"}


@pytest.mark.parametrize(("init", "path"), [("openrc", "/etc/elogind/logind.conf.d/10-installer-lid.conf"), ("systemd", "/etc/systemd/logind.conf.d/10-installer-lid.conf")])
def test_the_lid_action_is_a_logind_drop_in(init, path):
    maker = systemd if init == "systemd" else manifest
    result = hwplan.plan(maker(hardware={"lid_action": "ignore"}), fixture_dict("thinkpad-amd-laptop"))
    assert "HandleLidSwitch=ignore" in files_of(result)[path]
    assert not [f for f in files_of(hwplan.plan(maker(), fixture_dict("thinkpad-amd-laptop"))) if "installer-lid" in f]
    desktop = hwplan.plan(maker(hardware={"lid_action": "lock"}), fixture_dict("asus-desktop-rtx4090"))
    assert any(w.code == "hw.lid_action_no_lid" for w in desktop.warnings) and not [f for f in files_of(desktop) if "installer-lid" in f]


def test_vendor_workarounds_only_where_one_is_known_and_never_silently():
    blade = plan_for("razer-blade-laptop", hardware={"vendor_workarounds": True})
    assert "button.lid_init_state=open" in blade.kernel_cmdline
    other = plan_for("thinkpad-amd-laptop", hardware={"vendor_workarounds": True})
    assert "button.lid_init_state=open" not in other.kernel_cmdline and any(w.code == "hw.vendor_workarounds_none" for w in other.warnings)
    assert "button.lid_init_state=open" not in plan_for("razer-blade-laptop").kernel_cmdline


def test_thinkfan_is_advanced_and_only_installs_with_a_hint():
    offers = devices.device_offers(HardwareProfile.from_dict(fixture_dict("thinkpad-amd-laptop")))
    assert next(o for o in offers if o.key == "thinkfan").advanced
    result = plan_for("thinkpad-amd-laptop", hardware={"devices": {"thinkfan": True}})
    assert "app-laptop/thinkfan" in result.optional_atoms and "thinkfan" not in [name for name, _ in result.optional_services + result.services]
    assert any("thinkfan.yaml" in line for line in result.first_steps)


# -- B16: PipeWire ---------------------------------------------------------------------------------------------

def test_pipewire_user_services_are_enabled_on_systemd_and_not_on_openrc():
    on = hwplan.plan(systemd(), fixture_dict("qemu-q35-virtio"))
    assert [name for name, _kind in on.user_services] == ["pipewire.socket", "pipewire-pulse.socket", "wireplumber.service"]
    assert hwplan.plan(manifest(), fixture_dict("qemu-q35-virtio")).user_services == []
    assert hwplan.plan(systemd(options={"pipewire": False}), fixture_dict("qemu-q35-virtio")).user_services == []


def test_a_window_manager_on_openrc_starts_pipewire_from_its_own_configuration():
    sway = hwplan.plan(manifest(system={"desktop": "sway"}), fixture_dict("qemu-q35-virtio"))
    assert "exec gentoo-pipewire-launcher restart" in files_of(sway)["/etc/sway/config.d/10-installer-pipewire.conf"]
    i3 = hwplan.plan(manifest(system={"desktop": "i3"}), fixture_dict("qemu-q35-virtio"))
    assert any("gentoo-pipewire-launcher restart" in line for line in i3.first_steps)
    assert not [f for f in files_of(hwplan.plan(manifest(), fixture_dict("qemu-q35-virtio"))) if "pipewire" in f]


# -- B20: the desktop-coupled rules come from one table -----------------------------------------------------------

def test_no_literal_desktop_name_is_compared_in_the_planner_modules():
    source = Path(hwplan.__file__).parent
    for name in ("hwplan.py", "hwplan_devices.py", "hwplan_nvidia_plan.py"):
        text = (source / name).read_text()
        found = re.findall(r"""desktop\s*(?:==|!=|in|not in)\s*[(\["']\s*["'](?:kde|gnome|gnome-light|xfce|minimal|sway|i3|lxqt|mate|cinnamon)["']""", text)
        # three explicit exceptions, each with a reason in the code: GNOME's rebuild note, the Wayland-only Sway check, and the i3 configuration hint
        allowed = {"hwplan_nvidia_plan.py": 4, "hwplan_devices.py": 2, "hwplan.py": 1}[name]
        assert len(found) <= allowed, (name, found)


def test_hidpi_is_set_for_every_desktop_that_does_not_scale_itself():
    machine = profile(gpus=(gpu("amd", "gcn3+", boot=True),), displays=(hw.DisplayInfo(internal=True, dpi=192, width=3840, height=2400),))
    for desktop, path, text in (("xfce", "/etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml", "192"), ("i3", "/etc/skel/.Xresources", "Xft.dpi: 192"),
                                ("lxqt", "/etc/skel/.Xresources", "Xft.dpi: 192"), ("sway", "/etc/sway/config.d/20-installer-hidpi.conf", "scale 2")):
        result = hwplan.plan(manifest(system={"desktop": desktop}), machine)
        assert text in files_of(result)[path], desktop
    for desktop in ("kde", "gnome", "gnome-light", "cinnamon"):
        assert not [f for f in files_of(hwplan.plan(manifest(system={"desktop": desktop}), machine)) if "hidpi" in f or f.endswith(".Xresources") or "xsettings" in f], desktop
