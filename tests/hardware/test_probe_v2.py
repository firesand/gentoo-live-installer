"""Probe v2 (B01): vendor family, USB records, platform drivers and laptop sysfs."""

from __future__ import annotations

import dataclasses

import pytest

from gentoo_installer import hardware_data as data
from gentoo_installer.hardware import HardwareProfile, is_hybrid

from tests.hardware.helpers import fixture_dict, probe_fixture


def test_the_asus_desktop_is_an_asus_desktop_with_razer_and_aura_devices():
    profile = probe_fixture("asus-desktop-rtx4090")
    assert (profile.machine.vendor_family, profile.machine.form_factor) == ("asus", "desktop")
    roles = {(device.vendor_id, device.product_id): device.role for device in profile.peripherals.usb_devices}
    assert roles[("1532", "0091")] == "razer"       # the Razer Viper
    assert roles[("0b05", "18f3")] == "aura"        # the motherboard's Aura LED controller
    assert roles[("054c", "0ce6")] == "controller"  # the DualSense
    assert roles[("0b05", "9d08")] == ""            # other ASUS devices are not RGB
    assert profile.peripherals.with_role("razer")[0].product_id == "0091"
    assert not profile.platform.asus_nb_wmi and not profile.laptop.charge_threshold


def test_usb_records_carry_no_serial_number():
    for device in probe_fixture("asus-rog-laptop").peripherals.usb_devices:
        assert set(dataclasses.asdict(device)) == {"vendor_id", "product_id", "manufacturer", "product", "classes", "role"}


def test_the_asus_laptop_shows_the_platform_drivers_and_the_battery_limit():
    profile = probe_fixture("asus-rog-laptop")
    assert (profile.machine.vendor_family, profile.machine.oem, profile.machine.form_factor) == ("asus", "rog", "laptop")
    platform = profile.platform
    assert (platform.asus_nb_wmi, platform.platform_profile, platform.kbd_backlight, platform.thinkpad_acpi) == (True, True, True, False)
    assert platform.platform_profiles == ["low-power", "balanced", "performance"]
    assert (profile.laptop.charge_threshold, profile.laptop.charge_end, profile.laptop.mem_sleep, profile.laptop.pm_profile) == (True, 100, "s2idle", 2)
    assert [device.role for device in profile.peripherals.usb_devices] == ["", "aura"]
    assert is_hybrid(profile)


def test_the_razer_blade_is_a_razer_laptop_with_a_razer_usb_device():
    profile = probe_fixture("razer-blade-laptop")
    assert (profile.machine.vendor_family, profile.machine.oem) == ("razer", "blade")
    assert [(d.vendor_id, d.role) for d in profile.peripherals.usb_devices] == [("1532", "razer")]


def test_a_core_ultra_laptop_with_arc_graphics_and_an_nvidia_card_is_hybrid():
    profile = probe_fixture("arc-ultra-convertible")
    igpu, dgpu = profile.gpus
    assert (igpu.integrated, igpu.generation) == (True, "gen8+")
    assert (dgpu.vendor, dgpu.integrated, dgpu.generation) == ("nvidia", False, "ampere_plus")
    assert is_hybrid(profile)
    laptop = profile.laptop
    assert (laptop.convertible, laptop.pen, laptop.ipu_camera, laptop.charge_threshold) == (True, True, True, True)
    assert laptop.iio_sensors == ["accel_3d", "als"]


def test_the_thinkpad_shows_thinkpad_acpi_and_both_suspend_modes():
    profile = probe_fixture("thinkpad-amd-laptop")
    assert (profile.machine.vendor_family, profile.machine.oem) == ("lenovo", "thinkpad")
    assert (profile.platform.thinkpad_acpi, profile.platform.kbd_backlight, profile.platform.asus_nb_wmi) == (True, True, False)
    assert (profile.laptop.mem_sleep, profile.laptop.mem_sleep_options) == ("s2idle", ["s2idle", "deep"])
    assert profile.input.fingerprint_usb == ["06cb:00fc"]


def test_a_profile_saved_before_probe_v2_loads_with_defaults():
    old = fixture_dict("qemu-q35-virtio")
    for key in ("platform", "laptop"):
        old.pop(key)
    old["machine"].pop("vendor_family")
    old["peripherals"].pop("usb_devices")
    loaded = HardwareProfile.from_dict(old)
    assert (loaded.machine.vendor_family, loaded.peripherals.usb_devices, loaded.platform.asus_nb_wmi, loaded.laptop.convertible) == ("", [], False, False)


@pytest.mark.parametrize(("sys_vendor", "board_vendor", "product", "version", "family", "oem"), [
    ("ASUSTeK COMPUTER INC.", "ASUSTeK COMPUTER INC.", "ROG Strix G513", "", "asus", "rog"),
    ("ASUSTeK COMPUTER INC.", "", "TUF Gaming A15 FA507", "", "asus", "tuf"),
    ("System manufacturer", "ASUSTeK COMPUTER INC.", "System Product Name", "", "asus", ""),
    ("LENOVO", "LENOVO", "82JW", "Legion 5 15ACH6", "lenovo", "legion"),
    ("HP", "HP", "Victus by HP Gaming Laptop 16", "", "hp", "victus"),
    ("Hewlett-Packard", "", "HP EliteBook 840", "", "hp", "elitebook"),
    ("Microsoft Corporation", "", "Surface Laptop 4", "", "microsoft", "surface"),
    ("Apple Inc.", "", "MacBookPro16,1", "", "apple", "macbook"),
    ("Framework", "Framework", "Laptop (12th Gen Intel Core)", "", "framework", ""),
    ("To Be Filled By O.E.M.", "To Be Filled By O.E.M.", "To Be Filled By O.E.M.", "", "", ""),
])
def test_vendor_family_and_product_line_from_dmi(sys_vendor, board_vendor, product, version, family, oem):
    found = data.vendor_family(sys_vendor, board_vendor)
    assert (found, data.vendor_oem(found, product, version)) == (family, oem)
