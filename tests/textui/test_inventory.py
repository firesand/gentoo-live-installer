"""Inventory: the daemon's inspect reply as the console installer uses it."""

from __future__ import annotations

import copy
import json

import pytest

from gentoo_installer import blockdev
from gentoo_installer.manifest import InstallManifest, Storage
from gentoo_installer.textui.inventory import DiskEntry, Finding, MachineView, apply_disk, findings_from, lsblk_shape, parse_disks

from tests.textui.support import device_number, inspect_reply

GIB = 1024**3
LSBLK_JSON = json.dumps({"blockdevices": [{
    "name": "sda", "kname": "sda", "path": "/dev/sda", "maj:min": "8:0", "type": "disk", "size": 64 * GIB, "ro": False, "rm": True, "hotplug": True,
    "tran": "usb", "model": "Test Disk", "serial": "S1", "wwn": None, "log-sec": 4096, "phy-sec": 4096, "mountpoints": [None],
    "children": [{"name": "sda1", "path": "/dev/sda1", "maj:min": "8:1", "type": "part", "size": GIB, "fstype": "ext4", "mountpoints": ["/mnt"]}],
}]})


def node(name: str, size: int = 64 * GIB, **kwargs) -> blockdev.BlockDevice:
    return blockdev.BlockDevice(name=name, path="/dev/" + name, type=kwargs.pop("type", "disk"), size=size, maj_min=kwargs.pop("maj_min", device_number(name)), **kwargs)


def view_of(*roots: blockdev.BlockDevice, **reply) -> MachineView:
    return MachineView({"disks": [root.to_dict() for root in roots], **reply})


# --- the two shapes of a disk ------------------------------------------------------------------------------------------


def test_a_to_dict_disk_keeps_everything_that_lsblk_would_have_told() -> None:
    original = blockdev.parse_lsblk(LSBLK_JSON)[0]
    via_dict = parse_disks([original.to_dict()])[0]
    for name in ("maj_min", "transport", "removable", "hotplug", "log_sec", "phy_sec", "serial", "model", "size", "kname"):
        assert getattr(via_dict, name) == getattr(original, name), name
    assert via_dict.children[0].maj_min == "8:1" and via_dict.children[0].mountpoints == ("/mnt",)
    assert via_dict.key == "8:0"


def test_a_raw_lsblk_disk_is_read_as_well() -> None:
    raw = json.loads(LSBLK_JSON)["blockdevices"]
    disk = parse_disks(raw)[0]
    assert (disk.maj_min, disk.transport, disk.removable, disk.log_sec) == ("8:0", "usb", True, 4096)


def test_garbage_in_the_disk_list_is_skipped() -> None:
    assert parse_disks(["sda", None, 7, {"name": "sdb", "type": "disk", "size": 1}])[0].name == "sdb"
    assert parse_disks(None) == []
    assert lsblk_shape({"name": "a", "children": ["not a device"]})["children"] == []


# --- which disks are offered ---------------------------------------------------------------------------------------------


def test_the_live_medium_is_listed_but_not_usable_and_virtual_devices_are_hidden() -> None:
    reply = inspect_reply("qemu-q35-virtio", serials={"/dev/vda": "VDA-1"})
    reply["disks"] += [node("loop0", 2 * GIB, type="loop").to_dict(), node("sr0", 1 * GIB, type="rom").to_dict(), node("zram0", 4 * GIB).to_dict(), node("sdz", 0).to_dict()]
    entries = MachineView(reply).disk_entries()
    assert [entry.disk.path for entry in entries] == ["/dev/vda", "/dev/sdb"]
    assert entries[0].usable and not entries[1].usable
    assert "installer medium" in entries[1].problem


def test_a_read_only_and_a_small_disk_are_explained() -> None:
    view = view_of(node("sda", ro=True), node("sdb", 8 * GIB), node("sdc", 100 * GIB))
    by_name = {entry.disk.name: entry for entry in view.disk_entries()}
    assert "read-only" in by_name["sda"].problem
    assert "smaller than 16.0 GiB" in by_name["sdb"].problem
    assert by_name["sdc"].usable
    assert [entry.disk.name for entry in view.disk_entries()] == ["sdc", "sda", "sdb"]  # usable first


def test_a_partition_of_the_live_medium_marks_its_disk() -> None:
    usb = blockdev.BlockDevice(name="sdb", path="/dev/sdb", type="disk", size=16 * GIB, maj_min="8:16",
                               children=(blockdev.BlockDevice(name="sdb1", path="/dev/sdb1", type="part", size=GIB, maj_min="8:17"),))
    view = MachineView({"disks": [usb.to_dict(), node("sda").to_dict()], "live_medium": ["/dev/sdb1"]})
    by_name = {entry.disk.name: entry for entry in view.disk_entries()}
    assert not by_name["sdb"].usable and by_name["sda"].usable


def test_the_hardware_profile_can_name_the_live_medium_too() -> None:
    reply = inspect_reply("old-bios-sata-radeon")
    reply.pop("live_medium")
    entries = {entry.disk.path: entry for entry in MachineView(reply).disk_entries()}
    assert entries["/dev/sda"].usable and not entries["/dev/sdb"].usable


def test_find_disk_by_path() -> None:
    view = MachineView(inspect_reply("qemu-q35-virtio"))
    assert view.find_disk("/dev/vda") is not None and view.find_disk("/dev/nope") is None


# --- by-path names --------------------------------------------------------------------------------------------------------


def test_the_by_path_name_comes_from_the_service_then_from_the_local_links() -> None:
    reply = inspect_reply("qemu-q35-virtio")
    entry = MachineView(reply).find_disk("/dev/vda")
    assert entry is not None and entry.by_path == "pci-0000:00:03.0"
    for item in reply["hardware"]["disks"]:
        item["by_path"] = ""
    assert MachineView(reply).find_disk("/dev/vda").by_path == ""  # type: ignore[union-attr]
    local = MachineView(reply, local_by_path={"/dev/vda": "pci-0000:00:03.0-local"})
    assert local.find_disk("/dev/vda").by_path == "pci-0000:00:03.0-local"  # type: ignore[union-attr]


def test_a_by_path_name_sent_with_the_lsblk_disk_is_used() -> None:
    reply = {"disks": [{**node("vda").to_dict(), "by_path": "pci-0000:00:05.0"}]}
    assert MachineView(reply).find_disk("/dev/vda").by_path == "pci-0000:00:05.0"  # type: ignore[union-attr]


# --- pinning the identity ---------------------------------------------------------------------------------------------------


def test_a_disk_with_a_serial_or_wwn_is_pinned_without_asking() -> None:
    storage = Storage()
    assert apply_disk(storage, DiskEntry(node("sda", serial="SER1"))) == "serial"
    assert (storage.device, storage.device_id, storage.device_id_kind) == ("/dev/sda", "SER1", "")
    assert apply_disk(storage, DiskEntry(node("sdb", serial="SER2", wwn="0x5000"))) == "wwn"
    assert (storage.device, storage.device_id, storage.device_id_kind) == ("/dev/sdb", "0x5000", "")


def test_a_disk_without_identity_needs_approval_and_a_hardware_path() -> None:
    storage = Storage()
    entry = DiskEntry(node("vda", model="QEMU HARDDISK"), by_path="pci-0000:00:03.0")
    with pytest.raises(ValueError, match="approved explicitly"):
        apply_disk(storage, entry)
    assert apply_disk(storage, entry, approve_path=True) == "path"
    assert storage.device_id_kind == "path"
    assert storage.device_id == f"pci-0000:00:03.0|{64 * GIB}|QEMU HARDDISK"
    with pytest.raises(ValueError, match="no stable hardware path"):
        apply_disk(Storage(), DiskEntry(node("vdb")), approve_path=True)


def test_the_path_identity_is_exactly_what_the_daemon_verifies() -> None:
    entry = DiskEntry(node("vda", model="A|B"), by_path="pci-1")
    storage = Storage()
    apply_disk(storage, entry, approve_path=True)
    assert blockdev.identity_matches(entry.disk, "path", storage.device_id, "pci-1")
    manifest = InstallManifest()
    manifest.storage = storage
    assert not [error for error in manifest.validate_sections()["storage"] if "identity" in error]


# --- the rest of the reply ---------------------------------------------------------------------------------------------------


def test_findings_are_sorted_by_severity_and_plain_strings_are_warnings() -> None:
    found = findings_from([{"code": "hw.a", "severity": "info", "message": "i"}, "old style", {"code": "hw.b", "severity": "error", "message": "e", "action": "sync_time"}, {"severity": "weird", "message": "w"}, {"message": ""}])
    assert [(f.severity, f.message) for f in found] == [("error", "e"), ("warning", "old style"), ("warning", "w"), ("info", "i")]
    assert found[0].action == "sync_time" and found[0].short_code == "b"


def test_the_facts_of_the_reply() -> None:
    view = MachineView(inspect_reply("qemu-q35-virtio", network=False))
    assert view.network is False and view.uefi is True and view.known
    assert view.profile.machine.hypervisor == "qemu"
    assert MachineView(inspect_reply("qemu-q35-virtio", network=None)).network is None
    assert {finding.code for finding in view.findings} == {"hw.fw_sb_setup", "hw.vm_detected"}


def test_missing_tools_for_a_choice() -> None:
    tools = {"mkfs.xfs": False, "cryptsetup": False, "mkfs.btrfs": True}
    view = MachineView(inspect_reply("qemu-q35-virtio", tools=tools))
    manifest = InstallManifest()
    manifest.storage.filesystem, manifest.storage.luks = "xfs", True
    assert view.missing_tools(manifest) == ["cryptsetup", "mkfs.xfs"]
    manifest.storage.filesystem, manifest.storage.luks = "btrfs", False
    assert view.missing_tools(manifest) == []


def test_an_empty_reply_is_an_empty_view() -> None:
    view = MachineView()
    assert not view.known and view.disk_entries() == [] and view.network is None and view.uefi is None and view.tools == {}
    assert isinstance(Finding("a", "info", "b"), Finding)


def test_the_uefi_flag_falls_back_to_the_hardware_profile() -> None:
    reply = inspect_reply("old-bios-sata-radeon")
    reply.pop("uefi")
    assert MachineView(reply).uefi is False
    assert MachineView(copy.deepcopy(inspect_reply("qemu-q35-virtio"))).uefi is True


def test_two_disks_with_the_same_serial_number_are_told_apart_by_their_hardware_path() -> None:
    reply = {"disks": [node("sda", serial="0000000000").to_dict(), node("sdb", serial="0000000000").to_dict(), node("sdc", serial="UNIQUE-1").to_dict()]}
    by_name = {entry.disk.name: entry for entry in MachineView(reply).disk_entries()}
    assert by_name["sda"].shared_identity and by_name["sdb"].shared_identity and not by_name["sdc"].shared_identity
    assert not by_name["sda"].has_identity and by_name["sdc"].has_identity
    storage = Storage()
    with pytest.raises(ValueError, match="same serial number as another disk"):
        apply_disk(storage, DiskEntry(by_name["sda"].disk, by_path="pci-1", shared_identity=True))
    assert apply_disk(storage, DiskEntry(by_name["sda"].disk, by_path="pci-1", shared_identity=True), approve_path=True) == "path"
    assert apply_disk(Storage(), by_name["sdc"]) == "serial"
    with pytest.raises(ValueError, match="same serial number as another disk and no stable hardware path"):
        apply_disk(Storage(), DiskEntry(by_name["sda"].disk, shared_identity=True), approve_path=True)
