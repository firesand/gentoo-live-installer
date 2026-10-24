"""HWMonux's USE flags on the target: USB sensors on, NVIDIA's NVML off unless the proprietary driver is installed."""

from __future__ import annotations

from pathlib import Path

from gentoo_installerd.configure import Configurer, render_package_use
from gentoo_installerd.hwplan import plan as plan_hardware

from tests.backend.helpers import FakeRunner
from tests.backend.target_helpers import target_manifest


def use_table(**sections):
    m = target_manifest(**sections)
    return Configurer(FakeRunner(), "/nonexistent").package_use_table(m)


def test_hwmonux_gets_usb_sensors_but_not_nvml_by_default():
    # dev-python/nvidia-ml-py needs the licence-masked nvidia-drivers, so a default-on nvidia flag
    # (the ebuild's IUSE) would make HWMonux uninstallable on every machine that did not accept that licence
    flags = use_table(options={"install_hwmonux": True})["app-misc/hwmonux"]
    assert flags == ["-nvidia", "liquidctl"]
    assert "app-misc/hwmonux -nvidia liquidctl" in render_package_use({"app-misc/hwmonux": flags})


def test_no_hwmonux_flags_without_hwmonux():
    assert "app-misc/hwmonux" not in use_table(options={"install_hwmonux": False})


def test_a_flag_the_user_chose_is_not_overruled():
    table = use_table(options={"install_hwmonux": True}, portage={"package_use": {"app-misc/hwmonux": ["nvidia", "-liquidctl"]}})
    assert table["app-misc/hwmonux"] == ["nvidia", "-liquidctl"]


def test_the_nvidia_step_of_the_plan_switches_nvml_on_after_the_driver_is_installed():
    # The plan's step writes this file after nvidia-drivers installed; Portage reads package.use files in
    # name order and the later line wins, so it overrides the -nvidia that the installer file carries.
    m = target_manifest(options={"install_hwmonux": True}, hardware={"gpu_driver": "nvidia", "nvidia_license_accepted": True})
    plan = plan_hardware(m, None)
    script = next(step[-1] for step in plan.post_steps if "installer-hwmonux-nvidia" in step[-1])
    assert "/etc/portage/package.use/installer-hwmonux-nvidia" in script and "app-misc/hwmonux nvidia" in script
    assert "installer-hwmonux-nvidia" > "installer"
    assert "installer-hwmonux-nvidia" > "installer-hardware"
