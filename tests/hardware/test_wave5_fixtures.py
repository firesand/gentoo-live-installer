"""The Wave 5 fixtures (H01): honest provenance, no personal data, and the machine each one is meant to be."""

from __future__ import annotations

import json
import re

import pytest

from tests.hardware import synthetic
from tests.hardware.helpers import DERIVED, FIXTURE_NAMES, fixture_dict, fixture_path

REAL = {"asus-desktop-rtx4090", "thinkpad-intel-nvidia-hybrid-never-real"}
WAVE5 = (
    "asus-desktop-rtx4090-secureboot", "pascal-desktop-gtx1060", "kepler-desktop-gtx680", "blackwell-desktop-rtx5090",
    "asus-rog-zephyrus-intel-nvidia-mux", "asus-rog-zephyrus-eco", "razer-blade-intel-nvidia", "lenovo-legion-amd-nvidia", "amd-laptop-nvidia",
    "intel-arc-discrete-desktop", "intel-core-ultra-laptop-arc-igpu",
)
MAC = re.compile(r"(?i)\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b")


def meta(name: str) -> dict:
    return json.loads((fixture_path(name) / ".capture.json").read_text())


def gpu_ids(name: str) -> list[str]:
    return [f"{gpu['vendor_id']}:{gpu['device_id']}" for gpu in fixture_dict(name)["gpus"]]


@pytest.mark.parametrize("name", WAVE5)
def test_wave5_fixtures_are_registered_and_marked_synthesized(name):
    assert name in FIXTURE_NAMES
    data = meta(name)
    assert data["synthetic"] is True, "a machine nobody captured must say so"
    assert data["note"], "the note says what is invented"


@pytest.mark.parametrize("name", [n for n in WAVE5 if n not in DERIVED])
def test_unverified_details_are_listed_for_the_machines_with_invented_vendor_strings(name):
    data = meta(name)
    if any(word in name for word in ("razer", "legion", "rog", "amd-laptop-nvidia")):
        assert "dmi strings" in data["unverified"]


def test_the_derived_secure_boot_machine_is_the_real_capture_with_two_invented_files():
    real = fixture_dict("asus-desktop-rtx4090")
    derived = fixture_dict("asus-desktop-rtx4090-secureboot")
    assert real["firmware"]["secure_boot"] == "disabled"
    assert derived["firmware"]["secure_boot"] == "enabled"
    assert derived["firmware"]["lockdown"] == "integrity"
    assert [g["device_id"] for g in derived["gpus"]] == [g["device_id"] for g in real["gpus"]]
    assert "asus-desktop-rtx4090-secureboot" in DERIVED
    assert not (synthetic.FIXTURES / "asus-desktop-rtx4090-secureboot").exists(), "derived at test time, never committed as a second copy"


@pytest.mark.parametrize(("name", "ids"), [
    ("pascal-desktop-gtx1060", ["10de:1c03"]),
    ("kepler-desktop-gtx680", ["10de:1180"]),
    ("blackwell-desktop-rtx5090", ["10de:2b85"]),
    ("intel-arc-discrete-desktop", ["8086:56a0"]),
    ("intel-core-ultra-laptop-arc-igpu", ["8086:7d55"]),
    ("asus-rog-zephyrus-intel-nvidia-mux", ["8086:46a6", "10de:24a0"]),
    ("asus-rog-zephyrus-eco", ["8086:46a6"]),
    ("razer-blade-intel-nvidia", ["8086:9bc4", "10de:2191"]),
    ("lenovo-legion-amd-nvidia", ["1002:1638", "10de:2520"]),
    ("amd-laptop-nvidia", ["1002:1638", "10de:25a0"]),
])
def test_each_machine_has_the_graphics_it_is_named_for(name, ids):
    assert sorted(gpu_ids(name)) == sorted(ids)


def test_the_nvidia_generations_the_planner_has_to_tell_apart_are_all_present():
    generations = {name: [g["generation"] for g in fixture_dict(name)["gpus"] if g["vendor"] == "nvidia"]
                   for name in ("pascal-desktop-gtx1060", "kepler-desktop-gtx680", "blackwell-desktop-rtx5090", "asus-desktop-rtx4090")}
    # The classifier tells Blackwell (only the 595 open modules) and Ampere and newer apart from the older generations (B02).
    assert generations == {"pascal-desktop-gtx1060": ["maxwell-volta"], "kepler-desktop-gtx680": ["kepler"], "blackwell-desktop-rtx5090": ["blackwell"],
                           "asus-desktop-rtx4090": ["ampere_plus"]}
    assert all(not g["generation_guess"] for name in generations for g in fixture_dict(name)["gpus"]), "every id must be named in the trimmed pci.ids"


@pytest.mark.parametrize("name", WAVE5)
def test_no_serial_numbers_or_mac_addresses_leak_into_a_fixture(name):
    root = fixture_path(name)
    for path in root.rglob("*"):
        if path.is_file() and not path.name.endswith(".bin"):
            if path.name in ("serial", "device_serial", "product_serial", "board_serial", "chassis_serial", "product_uuid"):
                pytest.fail(f"{path} is a serial or UUID file")
            text = path.read_text(errors="ignore")
            for found in MAC.findall(text):
                assert found.lower().startswith("aa:bb:cc"), f"{path} holds a MAC address {found}"


def test_the_vendor_machines_carry_the_sysfs_the_new_probe_reads():
    rog = fixture_path("asus-rog-zephyrus-intel-nvidia-mux")
    eco = fixture_path("asus-rog-zephyrus-eco")
    assert (rog / "sys/devices/platform/asus-nb-wmi/gpu_mux_mode").read_text() == "1\n"
    assert (rog / "sys/devices/platform/asus-nb-wmi/dgpu_disable").read_text() == "0\n"
    assert (eco / "sys/devices/platform/asus-nb-wmi/dgpu_disable").read_text() == "1\n"
    assert (rog / "sys/class/firmware-attributes/asus-armoury/attributes/gpu_mux_mode/current_value").read_text() == "1\n"
    assert (rog / "sys/firmware/acpi/platform_profile").read_text() == "balanced\n"
    assert (rog / "sys/firmware/acpi/platform_profile_choices").read_text() == "quiet balanced performance\n"
    assert (rog / "sys/class/leds/asus::kbd_backlight/max_brightness").exists()
    assert (rog / "sys/class/power_supply/BAT0/charge_control_end_threshold").read_text() == "100\n"
    assert (rog / "sys/bus/pci/devices/0000:00:02.0/subsystem_vendor").read_text() == "0x1043\n"
    assert (rog / "sys/bus/usb/devices/1-5/manufacturer").read_text() == "ASUSTeK Computer Inc.\n"
    assert (rog / "sys/bus/usb/devices/1-5/product").read_text() == "N-KEY Device\n"
    razer = fixture_path("razer-blade-intel-nvidia")
    assert (razer / "sys/bus/usb/devices/1-9/idVendor").read_text() == "1532\n"
    assert (razer / "sys/class/dmi/id/sys_vendor").read_text() == "Razer\n"
    legion = fixture_path("lenovo-legion-amd-nvidia")
    assert (legion / "sys/class/dmi/id/product_version").read_text() == "Legion 5 15ACH6H\n"


def test_an_eco_mode_laptop_shows_no_nvidia_device_at_all():
    assert [g["vendor"] for g in fixture_dict("asus-rog-zephyrus-eco")["gpus"]] == ["intel"]


def test_builder_helpers_never_write_serial_files(tmp_path, monkeypatch):
    monkeypatch.setattr(synthetic, "FIXTURES", tmp_path)
    builder = synthetic.Builder("probe")
    builder.usb("1-1", "1532", "0091", "00", ("03",), manufacturer="Razer", product_name="Razer Viper")
    assert (tmp_path / "probe/sys/bus/usb/devices/1-1/product").read_text() == "Razer Viper\n"
    assert not (tmp_path / "probe/sys/bus/usb/devices/1-1/serial").exists()


def test_the_capture_tool_blanks_serial_files_a_probe_might_read(tmp_path):
    import capture_hardware_fixture as capture_tool  # registered by tests.hardware.synthetic

    for path in ("/sys/bus/usb/devices/1-1/serial", "/sys/class/dmi/id/product_serial", "/sys/class/dmi/id/product_uuid", "/sys/class/dmi/id/board_serial"):
        assert capture_tool._process_file(path, b"ABC123\n", True) == b"REDACTED\n"
    assert capture_tool._process_file("/sys/bus/usb/devices/1-1/product", b"Razer Viper\n", True) == b"Razer Viper\n"
    assert capture_tool._process_file("/sys/bus/usb/devices/1-1/serial", b"ABC123\n", False) == b"ABC123\n"
