"""A fabricated guest for the disk guard: lsblk trees and sysfs facts that match (or deliberately
do not match) a GuardSpec, so the harness's guard handling is tested without a VM."""
from __future__ import annotations

import copy
import json
import shlex

import vm_guard

MUTATIONS = ("wrong_size", "extra_disk", "wrong_serial", "not_blank", "no_nonce", "second_same_size", "wrong_model",
             "wrong_vendor", "read_only", "mounted")


def device_name(bus: str, index: int) -> str:
    if bus == "virtio":
        return f"vd{chr(ord('a') + index)}"
    if bus == "nvme":
        return f"nvme{index}n1"
    return f"sd{chr(ord('a') + index)}"


def lsblk_tree(spec: vm_guard.GuardSpec, *, bus: str = "virtio", installed: bool = False, sector: int | None = None) -> list[dict]:
    """The ``blockdevices`` list of a guest holding exactly the disks of ``spec`` plus a CD-ROM."""
    nodes: list[dict] = []
    for index, expected in enumerate(spec.disks):
        name = device_name(bus, index)
        node = {"name": name, "kname": name, "path": f"/dev/{name}", "type": "disk", "size": expected.size,
                "serial": expected.serial or None, "model": expected.model or None, "tran": expected.tran or None,
                "rm": 1 if expected.removable else 0, "ro": 0, "log-sec": sector or expected.log_sec or 512,
                "phy-sec": sector or expected.log_sec or 512, "pttype": None, "fstype": None, "mountpoints": [None],
                "children": []}
        if installed and expected.role == "target":
            sep = "p" if name[-1].isdigit() else ""
            node["pttype"] = "gpt"
            node["children"] = [
                {"name": f"{name}{sep}1", "kname": f"{name}{sep}1", "path": f"/dev/{name}{sep}1", "type": "part",
                 "size": 512 << 20, "fstype": "vfat", "mountpoints": [None]},
                {"name": f"{name}{sep}2", "kname": f"{name}{sep}2", "path": f"/dev/{name}{sep}2", "type": "part",
                 "size": expected.size - (1 << 30), "fstype": "ext4", "mountpoints": [None]}]
        nodes.append(node)
    nodes.append({"name": "sr0", "kname": "sr0", "path": "/dev/sr0", "type": "rom", "size": 2 << 30, "mountpoints": ["/run/initramfs/live"]})
    nodes.append({"name": "loop0", "kname": "loop0", "path": "/dev/loop0", "type": "loop", "size": 3 << 30, "mountpoints": ["/run/initramfs/squashfs"]})
    return nodes


def fabricated_report(spec: vm_guard.GuardSpec, *, bus: str = "virtio", installed: bool | None = None,
                      mutate: str = "", sector: int | None = None) -> dict:
    """What the guest's guard would report for ``spec`` (the real ``vm_guard.evaluate`` judges it)."""
    if installed is None:  # the harness expects an installation exactly when it asks for a non-blank disk
        installed = bool(spec.disks and spec.disks[0].blank is False)
    devices = lsblk_tree(spec, bus=bus, installed=installed, sector=sector)
    dmi: list[str] | None = [f"{vm_guard.NONCE_KEY}={spec.nonce}"] if spec.nonce else []
    disks = [node for node in devices if node["type"] == "disk"]
    if mutate == "wrong_size":
        disks[0]["size"] += 1
    elif mutate == "extra_disk":
        extra = copy.deepcopy(disks[0])
        extra.update(name="vdz", kname="vdz", path="/dev/vdz", size=disks[0]["size"] + (7 << 20), serial="REAL-DISK")
        devices.append(extra)
    elif mutate == "second_same_size":
        extra = copy.deepcopy(disks[0])
        extra.update(name="vdz", kname="vdz", path="/dev/vdz", serial="OTHER")
        devices.append(extra)
    elif mutate == "wrong_serial":
        disks[0]["serial"] = "SOME-REAL-DISK"
    elif mutate == "wrong_model":
        disks[0]["model"] = "Samsung SSD 990 PRO"
    elif mutate == "not_blank":
        disks[0]["pttype"] = "gpt"
    elif mutate == "no_nonce":
        dmi = [f"{vm_guard.NONCE_KEY}=0000"]
    elif mutate == "read_only":
        disks[0]["ro"] = 1
    elif mutate == "mounted":
        disks[0]["mountpoints"] = ["/mnt/gentoo"]
    by_path = {node["kname"]: [f"pci-0000:00:{7 + index:02x}.0", f"virtio-pci-0000:00:{7 + index:02x}.0"]
               for index, node in enumerate(disks)}
    vendors = {node["kname"]: (vm_guard.BUS_FACTS[bus]["pci_vendors"][0] if mutate != "wrong_vendor" else "0x8086")
               for node in disks}
    return vm_guard.evaluate(spec, devices, by_path=by_path, pci_vendor=vendors, dmi_strings=dmi)


def answer_guard(remote: str, **options) -> str:
    """The guard's output for the ``python3 -c <source> <spec>`` command line the harness sent."""
    spec = vm_guard.GuardSpec.from_json(shlex.split(remote)[-1])
    return json.dumps(fabricated_report(spec, **options), sort_keys=True) + "\n"
