"""No Wave 5 hardware choice is accepted and then silently ignored: each one changes the plan or draws a finding (refusal or note)."""

from __future__ import annotations

import dataclasses

import pytest

from gentoo_installer.manifest import HARDWARE_DEVICE_KEYS, Hardware
from gentoo_installerd import hwplan

from tests.hardware.builders import gpu, manifest, profile
from tests.hardware.helpers import fixture_dict


def observe(m, machine):
    """Everything a user could see of a choice: the plan and the cross-checks."""
    result = hwplan.plan(m, machine)
    issues = hwplan.validate_plan(m, machine)
    return (result.to_dict(), sorted((i.code, i.severity) for i in issues))


def accepted(**hardware):
    return manifest(hardware={"nvidia_license_accepted": True, **hardware}, system={"kernel": "gentoo-kernel-bin"})


NVIDIA_DESKTOP = profile(gpus=(gpu("nvidia", "ampere_plus", name="AD102 [GeForce RTX 4090]"),))
TURING_LAPTOP = profile(form="laptop", gpus=(gpu("intel", "gen8+", integrated=True, boot=True), gpu("nvidia", "turing", outputs=0)))

UNTICKED = {"nvidia_license_accepted": False}      # Automatic without the licence: nouveau, the neutral state to compare the NVIDIA choices with
PASCAL = profile(gpus=(gpu("nvidia", "maxwell-volta"),))
BOLT_LAPTOP = {**profile(form="laptop", gpus=(gpu("amd", "gcn3+", boot=True, integrated=True),)), "peripherals": {"thunderbolt": True}}

# (field, value, machine, settings before, settings after): the plan or the findings must differ between the two.
CASES = [
    ("gpu_driver", "nouveau", NVIDIA_DESKTOP, {}, {"gpu_driver": "nouveau"}),
    ("gpu_driver", "basic", NVIDIA_DESKTOP, {}, {"gpu_driver": "basic"}),
    ("gpu_driver", "nvidia", NVIDIA_DESKTOP, UNTICKED, {"gpu_driver": "nvidia"}),
    ("nvidia_modules", "closed", TURING_LAPTOP, {}, {"nvidia_modules": "closed"}),
    ("nvidia_modules", "open", PASCAL, {"gpu_driver": "nvidia"}, {"gpu_driver": "nvidia", "nvidia_modules": "open"}),
    ("nvk", "on", NVIDIA_DESKTOP, UNTICKED, {**UNTICKED, "nvk": "on"}),
    ("safe_graphics_entry", False, NVIDIA_DESKTOP, {}, {"safe_graphics_entry": False}),
    ("nvidia_rtd3", "on", TURING_LAPTOP, {}, {"nvidia_rtd3": "on"}),
    ("nvidia_rtd3", "off", TURING_LAPTOP, {"nvidia_modules": "closed"}, {"nvidia_modules": "closed", "nvidia_rtd3": "off"}),
    ("nvidia_extras", ["nvtop"], NVIDIA_DESKTOP, {}, {"nvidia_extras": ["nvtop"]}),
    ("nvidia_extras", ["vaapi"], NVIDIA_DESKTOP, {}, {"nvidia_extras": ["vaapi"]}),
    ("nvidia_extras", ["persistenced"], NVIDIA_DESKTOP, {}, {"nvidia_extras": ["persistenced"]}),
    ("nvidia_extras", ["cuda"], NVIDIA_DESKTOP, {}, {"nvidia_extras": ["cuda"], "nvidia_cuda_license_accepted": True}),
    ("signed_modules", "auto", NVIDIA_DESKTOP, {}, {"signed_modules": "auto"}),
    ("signed_modules", "kernel_key", NVIDIA_DESKTOP, {}, {"signed_modules": "kernel_key"}),
    ("battery_charge_limit", 80, fixture_dict("thinkpad-amd-laptop"), {}, {"battery_charge_limit": 80}),
    ("battery_charge_limit", 60, fixture_dict("thinkpad-amd-laptop"), {}, {"battery_charge_limit": 60}),
    ("lid_action", "lock", fixture_dict("thinkpad-amd-laptop"), {}, {"lid_action": "lock"}),
    ("lid_action", "ignore", fixture_dict("thinkpad-amd-laptop"), {}, {"lid_action": "ignore"}),
    ("vendor_workarounds", True, fixture_dict("razer-blade-laptop"), {}, {"vendor_workarounds": True}),
    ("vendor_workarounds", True, fixture_dict("thinkpad-amd-laptop"), {}, {"vendor_workarounds": True}),
]
CASES += [("devices", key, machine, {"devices": {key: False}}, {"devices": {key: True}}) for key, machine in (
    ("openrazer", fixture_dict("asus-desktop-rtx4090")), ("razer_frontend", fixture_dict("asus-desktop-rtx4090")), ("openrgb", fixture_dict("asus-desktop-rtx4090")),
    ("game_rules", fixture_dict("asus-desktop-rtx4090")), ("solaar", fixture_dict("asus-desktop-rtx4090")), ("iio_sensors", fixture_dict("arc-ultra-convertible")),
    ("fingerprint", fixture_dict("thinkpad-amd-laptop")), ("asus_tools", fixture_dict("asus-rog-laptop")), ("thinkfan", fixture_dict("thinkpad-amd-laptop")),
    ("bolt", BOLT_LAPTOP), ("wacom", fixture_dict("arc-ultra-convertible")))]


@pytest.mark.parametrize(("name", "value", "machine", "before", "after"), CASES, ids=lambda case: str(case)[:30])
def test_a_wave5_choice_either_changes_the_plan_or_says_why_not(name, value, machine, before, after):
    assert observe(accepted(**after), machine) != observe(accepted(**before), machine), f"{name}={value!r} is accepted and does nothing"


def test_the_cases_cover_every_wave5_hardware_field_and_every_device_key():
    covered = {name for name, *_rest in CASES}
    wave5 = {"gpu_driver", "nvidia_modules", "nvk", "safe_graphics_entry", "nvidia_rtd3", "nvidia_extras", "signed_modules", "devices", "battery_charge_limit",
             "lid_action", "vendor_workarounds"}
    assert wave5 <= covered
    assert {value for name, value, *_rest in CASES if name == "devices"} == set(HARDWARE_DEVICE_KEYS)
    assert {field.name for field in dataclasses.fields(Hardware)} >= wave5


def test_nvk_off_matters_in_a_source_build_where_automatic_would_add_it():
    machine = profile(gpus=(gpu("nvidia", "ampere_plus"),))
    on = hwplan.plan(manifest(hardware=UNTICKED, portage={"binhost": False}), machine)
    off = hwplan.plan(manifest(hardware={**UNTICKED, "nvk": "off"}, portage={"binhost": False}), machine)
    assert on.make_conf["VIDEO_CARDS"] == "nouveau nvk" and off.make_conf["VIDEO_CARDS"] == "nouveau"
