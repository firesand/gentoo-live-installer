"""The disk model of the GUI: reasons a device is not offered, things alive on a disk, shared identities, size thresholds."""

from __future__ import annotations

import json

from gentoo_installer import blockdev, system_info
from gentoo_installer.diagnostics import describe_system_info
from gentoo_installer.manifest import InstallManifest
from gentoo_installer.system_info import DiskInfo, PartitionInfo, SystemInfo, desktop_disk_bytes, smallest_disk_bytes
from tests.hardware.helpers import fixture_dict

GIB = 1024**3
MINORS: dict[str, int] = {}


def node(name, type_="disk", size=0, **extra):
    minor = MINORS.setdefault(name, len(MINORS) + 1)
    record = {"name": name, "kname": name, "path": f"/dev/{name}", "maj:min": f"8:{minor}", "type": type_, "size": size,
              "ro": False, "rm": False, "hotplug": False, "mountpoints": [None]}
    record.update(extra)
    return record


def roots_of(*items):
    return blockdev.parse_lsblk(json.dumps({"blockdevices": list(items)}))


# --- why a device is not offered ---------------------------------------------------------------------------------------------


def test_devices_that_cannot_be_targets_are_classified_by_the_shared_rules():
    roots = roots_of(
        node("sda", size=500 * GIB, model="SSD", serial="A1"),
        node("sdb", size=16 * GIB, model="Stick", serial="U1", ro=True),
        node("sdc", size=0, model="Card reader"),
        node("zram0", size=4 * GIB),
        node("sr0", "rom", GIB, model="DVD"),
        node("mmcblk0boot0", size=4 * 1024**2, ro=True),
    )
    excluded: list[system_info.ExcludedDevice] = []
    disks = system_info.disks_from_roots(roots, excluded=excluded)
    assert [d.path for d in disks] == ["/dev/sda", "/dev/sdb"]
    assert disks[1].read_only and not disks[1].selectable and "read-only" in disks[1].unavailable_reason
    assert [(e.path, e.problem) for e in excluded] == [("/dev/sdc", "empty")] and "no media" in excluded[0].reason


def test_the_live_medium_is_a_disabled_disk_with_its_reason():
    roots = roots_of(node("sdd", size=16 * GIB, model="USB", serial="U", children=[node("sdd1", "part", 16 * GIB, fstype="iso9660", label="GENTOO")]))
    live = blockdev.live_medium_keys(roots, cmdline="root=live:CDLABEL=GENTOO")
    disk = system_info.disks_from_roots(roots, live_keys=live)[0]
    assert disk.live_medium and not disk.selectable and "live installer medium" in disk.unavailable_reason


def test_a_disk_in_use_lists_what_would_have_to_be_switched_off():
    roots = roots_of(node("sda", size=500 * GIB, model="SSD", serial="A1", children=[
        node("sda1", "part", 100 * GIB, fstype="LVM2_member", children=[node("vg-root", "lvm", 90 * GIB, fstype="ext4", mountpoints=["/mnt/x"])]),
        node("sda2", "part", 8 * GIB, fstype="swap", mountpoints=["[SWAP]"]),
        node("sda3", "part", 50 * GIB, fstype="linux_raid_member"),
    ]))
    disk = system_info.disks_from_roots(roots)[0]
    assert disk.active_volumes == ["lvm /dev/vg-root"] and disk.swaps == ["/dev/sda2"]
    assert disk.signatures == ["LVM2_member", "linux_raid_member"]
    text = " | ".join(disk.holders)
    assert "active volume lvm /dev/vg-root" in text and "swap in use on /dev/sda2" in text
    assert "part of an LVM volume group" in text and "part of a software RAID array" in text
    assert disk.mounted  # the volume is mounted for real


def test_active_swap_alone_is_not_a_mount():
    roots = roots_of(node("sda", size=100 * GIB, model="SSD", serial="A1", children=[node("sda2", "part", 8 * GIB, fstype="swap", mountpoints=["[SWAP]"])]))
    disk = system_info.disks_from_roots(roots)[0]
    assert not disk.mounted and disk.swaps == ["/dev/sda2"] and not disk.partitions[0].mounted and disk.partitions[0].swap_active


def test_two_disks_with_the_same_serial_both_need_the_path_approval():
    roots = roots_of(node("vda", size=64 * GIB, model="QEMU", serial="SAME"), node("vdb", size=64 * GIB, model="QEMU", serial="SAME"),
                     node("vdc", size=64 * GIB, model="QEMU", serial="OWN"))
    a, b, c = system_info.disks_from_roots(roots)
    assert a.identity_shared and b.identity_shared and not a.has_identity and "shared" in a.identity_text
    assert c.has_identity and not c.identity_shared


def test_the_sector_size_and_the_lsblk_tree_are_kept():
    roots = roots_of(node("nvme0n1", size=500 * GIB, model="NVMe", serial="N1", **{"log-sec": 4096}))
    disk = system_info.disks_from_roots(roots)[0]
    assert disk.log_sec == 4096
    info = SystemInfo("x86_64", 8, True, [disk], True, True, [], roots=roots)
    assert info.block_devices() is roots
    rebuilt = SystemInfo("x86_64", 8, True, [DiskInfo("/dev/sda", "Disk", 100 * GIB, serial="S", partitions=[PartitionInfo("/dev/sda1", GIB, "ext4", parent="/dev/sda")])], True, True, [])
    tree = rebuilt.block_devices()
    assert tree[0].path == "/dev/sda" and tree[0].children[0].path == "/dev/sda1" and tree[0].serial == "S"


def test_an_inspect_reply_provides_the_tree_when_the_local_scan_found_nothing():
    reply = {"architecture": "x86_64", "disks": [node("sda", size=100 * GIB, model="SSD", serial="A1")]}
    info = SystemInfo.pending()
    system_info.merge_inspection(info, reply)
    assert [d.path for d in info.disks] == ["/dev/sda"] and [r.path for r in info.roots] == ["/dev/sda"]


# --- size thresholds -------------------------------------------------------------------------------------------------------------------------


def test_the_smallest_disk_follows_the_layout_module_not_a_constant():
    manifest = InstallManifest()
    assert smallest_disk_bytes(manifest) == (8 * 1024 + 1024 + 2) * 1024**2  # minimal root + ESP + reserved
    assert desktop_disk_bytes(manifest) == (24 * 1024 + 1024 + 2) * 1024**2  # KDE
    manifest.system.desktop = "xfce"
    assert desktop_disk_bytes(manifest) == (14 * 1024 + 1024 + 2) * 1024**2
    manifest.storage.firmware = "bios"
    assert desktop_disk_bytes(manifest) == (14 * 1024 + 3 + 2) * 1024**2  # a BIOS boot partition instead of the ESP
    manifest.portage.binhost = False
    assert smallest_disk_bytes(manifest) == (18 * 1024 + 3 + 2) * 1024**2  # building from source needs more room


# --- the diagnostics bundle -------------------------------------------------------------------------------------------------------------------


def test_the_diagnostics_hide_serial_numbers_the_lsblk_tree_and_connection_names():
    hardware = fixture_dict("thinkpad-intel-nvidia-hybrid")
    hardware["network_profiles"] = ["Home Wi-Fi"]
    hardware["disks"][0]["serial"] = "S5Y1NX0T123456"
    roots = roots_of(node("sda", size=100 * GIB, model="SSD", serial="S5Y1NX0T123456"))
    info = SystemInfo("x86_64", 8, True, system_info.disks_from_roots(roots), True, True, [], hardware=hardware, roots=roots)
    text = json.dumps(describe_system_info(info), ensure_ascii=False)
    assert "S5Y1NX0T123456" not in text and "Home Wi-Fi" not in text and "roots" not in json.loads(text)
    assert "…123456" in text
