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


@pytest.mark.parametrize("device", ["/dev/md0", "/dev/md127", "/dev/loop0", "/dev/nbd1"])
def test_an_array_or_a_disk_image_is_not_reported_as_a_missing_disk(device):
    # lsblk lists them, the probe deliberately does not: the install target of a RAID or a disk image must pass preflight
    chosen = manifest(storage={"device": device, "mode": "manual", "firmware": "bios", "partitions": [__import__("gentoo_installer.manifest", fromlist=["Partition"]).Partition(device, "/", "ext4")]})
    assert "hw.st_disk_missing" not in {issue.code for issue in hwplan.validate_plan(chosen, profile())}


def test_a_name_that_is_simply_not_there_still_is():
    assert "hw.st_disk_missing" in {issue.code for issue in hwplan.validate_plan(manifest(storage={"device": "/dev/sdz"}), profile())}


@pytest.mark.parametrize("desktop", ["minimal", "xfce", "gnome", "kde"])
@pytest.mark.parametrize("binhost", [True, False])
def test_the_planner_and_the_storage_stage_use_the_same_root_sizes(desktop, binhost):
    # The planner blocks in preflight and the storage stage refuses later: they must never disagree about how big is big enough.
    from gentoo_installer import layout, recommend

    chosen = manifest(system={"desktop": desktop}, portage={"binhost": binhost})
    assert recommend.minimum_root_gib(desktop, binhost) == (layout.minimum_root_mib(chosen) // 1024, layout.recommended_root_mib(chosen) // 1024)
