"""The installed disk is QEMU's boot device when it boots by itself (bootindex=0), on every bus.

The first Wave 6 rescue-gentoo run: the live boot of the repair (strict boot order, the CD alone) let OVMF drop the installer's
"Gentoo" entry; the next boot of the repaired disk then started the EFI Shell, which was put before the disk's own fallback entry.
"""
from __future__ import annotations

import dataclasses

import pytest

import vm_hw


@pytest.mark.parametrize("bus", ["virtio", "sata", "scsi", "usb", "nvme"])
def test_bootindex_goes_on_the_device_that_holds_the_drive(bus) -> None:
    hardware = dataclasses.replace(vm_hw.VMHardware(), disk_bus=bus)
    devices = [arg for arg in vm_hw.disk_device_arguments(hardware, "target", "SERIAL", 0, bootindex=0) if arg != "-device"]
    holder = [device for device in devices if "drive=target" in device]
    assert len(holder) == 1 and ",bootindex=0" in holder[0], devices
    assert all("bootindex" not in device for device in devices if device not in holder)
    assert all("bootindex" not in arg for arg in vm_hw.disk_device_arguments(hardware, "target", "SERIAL", 0))


def test_nvme_with_4k_sectors_boots_from_its_namespace() -> None:
    hardware = dataclasses.replace(vm_hw.VMHardware(), disk_bus="nvme", sector_size=4096)
    args = vm_hw.disk_device_arguments(hardware, "target", "SERIAL", 0, bootindex=0)
    assert any(arg.startswith("nvme-ns,") and arg.endswith(",bootindex=0") for arg in args)
    assert any(arg.startswith("nvme,") and "bootindex" not in arg for arg in args)
