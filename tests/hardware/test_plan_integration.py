"""Planner rules found while the plan was connected to a real install (Wave 2)."""

from __future__ import annotations

import pytest

from gentoo_installerd import hwplan

from tests.hardware.builders import gpu, manifest, profile

VMTOOLS = "app-emulation/open-vm-tools"


@pytest.mark.parametrize(("desktop", "flags"), [("minimal", ["-resolutionkms"]), ("kde", []), ("xfce", []), ("gnome", [])])
def test_vmware_tools_do_not_pull_an_x_server_into_a_console_only_system(desktop, flags):
    # open-vm-tools[resolutionkms] (on by default) depends on x11-base/xorg-server, which does not resolve on the minimal profile
    result = hwplan.plan(manifest(system={"desktop": desktop}), profile(hypervisor="vmware", gpus=(gpu("vmware"),)))
    assert VMTOOLS in result.optional_atoms
    assert result.package_use.get(VMTOOLS, []) == flags


def test_a_probe_that_found_nothing_cannot_claim_the_nvidia_card_is_missing():
    chosen = manifest(hardware={"gpu_driver": "nvidia", "nvidia_license_accepted": True})
    assert "hw.nvidia_no_gpu" not in {issue.code for issue in hwplan.validate_plan(chosen, None)}
    assert hwplan.plan(chosen, None).driver_atoms == ["x11-drivers/nvidia-drivers"]


def test_a_machine_that_was_probed_and_has_no_nvidia_card_still_gets_the_error():
    chosen = manifest(hardware={"gpu_driver": "nvidia", "nvidia_license_accepted": True})
    machine = profile(gpus=(gpu("amd", "gcn3+", boot=True),))
    assert "hw.nvidia_no_gpu" in {issue.code for issue in hwplan.validate_plan(chosen, machine)}
