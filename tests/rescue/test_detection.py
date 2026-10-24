"""Detection on layouts of different machines (TD-N4, engine side): what the scan finds, what it must leave alone and what a repair session gets.

Every scenario is a captured or hand-made lsblk document plus fake root trees; nothing here touches a device.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gentoo_installerd.rescue.devices import build_graph
from gentoo_installerd.rescue.scan import ScanOptions

from . import fakeroot as fr
from . import lsblk_fixtures as lf
from .flow import mount_through_the_manager, unmount


def run_scan(lab):
    return lab.scanner().scan(ScanOptions())


def by_device(result) -> dict[str, dict]:
    return {item["device"]: item for item in result.candidates}


# ---- XFS root, separate /boot and ESP ----------------------------------------------------------------------------------------------------------
def xfs_lab(lab_factory):
    lab = lab_factory(lf.xfs_boot_esp())
    fstab = (f"UUID={fr.ROOT_UUID}\t/\txfs\tnoatime\t0 1\nUUID={fr.BOOT_UUID}\t/boot\text4\tnoatime\t0 2\nUUID={fr.ESP_UUID}\t/efi\tvfat\tumask=0077\t0 2\n"
             f"UUID={fr.SWAP_UUID}\tnone\tswap\tsw\t0 0\n")
    tree = lab.add_tree("/dev/sda3", "gentoo", firmware="uefi", separate_boot=True, fstab=fstab)
    (tree / "efi").mkdir(exist_ok=True)
    (tree / "boot").mkdir(exist_ok=True)
    boot = lab.tmp / "trees" / "boot"
    fr.kernel_files(boot, fr.GENTOO_KVER, prefix="vmlinuz-", initrd_prefix="initramfs-", modules=False)
    fr.put(boot, "grub/grub.cfg", fr.grub_cfg(fr.GENTOO_KVER, kernel="vmlinuz").replace("/boot/", "/"))
    lab.system.attach("/dev/sda2", boot)
    lab.system.attach("/dev/sda1", fr.esp_tree(lab.tmp / "trees" / "esp", "gentoo"))
    return lab


def test_xfs_root_is_identified_from_its_own_files(lab_factory) -> None:
    lab = xfs_lab(lab_factory)
    result = run_scan(lab)
    assert list(by_device(result)) == ["/dev/sda3"]  # /boot is ext4 and holds no system; the ESP and swap are not candidates
    found = result.candidates[0]
    assert (found["fstype"], found["os"]["id"], found["os"]["family"], found["init"], found["arch"], found["confidence"]) == ("xfs", "gentoo", "gentoo", "openrc", "x86_64", "high")
    assert found["fstab"] == {"present": True, "entries": 4, "dead": 0, "needs_unlock": 0}
    probe = [argv for argv in lab.system.commands_named("mount") if argv[-2] == "/dev/sda3"][0]
    assert probe[2] == "xfs" and probe[4].split(",")[:2] == ["ro", "norecovery"] and {"nodev", "nosuid", "noexec"} <= set(probe[4].split(","))  # no log replay on a probe


def test_the_separate_boot_and_the_esp_are_mounted_with_the_system(lab_factory) -> None:
    lab = xfs_lab(lab_factory)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/sda3")
    assert [(m["mountpoint"], m["device"]) for m in info["mounts"]] == [("/", "/dev/sda3"), ("/boot", "/dev/sda2"), ("/efi", "/dev/sda1")]
    assert [f for f in info["findings"] if f["severity"] == "error"] == []  # the kernel in /boot and the loader on the ESP are seen
    boot = manager.query({"session_id": sid, "query": "boot.inventory"})["data"]
    assert [kernel["version"] for kernel in boot["kernels"]] == [fr.GENTOO_KVER] and boot["loader"] == "grub" and boot["esp"]["mountpoint"] == "/efi"
    flagged = {entry.data["device"]: entry.data["purpose"] for entry in manager.session(sid).ledger.of_kind("blockro")}
    assert [flagged[device] for device in ("/dev/sda1", "/dev/sda2", "/dev/sda3")] == ["session"] * 3 and flagged["/dev/sda4"] == "protect"  # the swap partition of the same disk is protected, too
    unmount(manager, sid)
    assert not any(lab.system.ro.values())


def test_an_xfs_root_with_an_external_log_is_found_by_naming_the_log(lab_factory) -> None:
    lab = lab_factory(lf.xfs_external_log())
    tree = lab.add_tree("/dev/sda2", "gentoo", firmware="uefi", fstab=f"UUID={fr.ROOT_UUID} / xfs noatime 0 1\n")
    (tree / "efi").mkdir(exist_ok=True)
    lab.system.external_logs["/dev/sda2"] = "/dev/sdb1"
    lab.add_plain_tree("/dev/sdc1", {"data/readme.txt": "files"})
    result = run_scan(lab)
    assert list(by_device(result)) == ["/dev/sda2"]  # the log partition and the XFS share one UUID; only the file system is a system
    probe = [argv for argv in lab.system.commands_named("mount") if argv[-2] == "/dev/sda2"][0]
    assert "logdev=/dev/sdb1" in probe[4].split(",") and not [argv for argv in lab.system.commands_named("mount") if argv[-2] == "/dev/sdb1"]  # the log is not mounted on its own
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/sda2")
    assert [m["mountpoint"] for m in info["mounts"]][:1] == ["/"] and info["state"] == "mounted_ro"
    unmount(manager, sid)
    assert not any(lab.system.ro.values())


# ---- an assembled RAID1 holds the root ----------------------------------------------------------------------------------------------------
def test_root_on_an_assembled_raid_is_found_through_the_array(lab_factory) -> None:
    lab = lab_factory(lf.md_raid1_root())
    fstab = f"UUID={fr.ROOT_UUID}\t/\text4\tnoatime\t0 1\nUUID={fr.ESP_UUID}\t/efi\tvfat\tumask=0077\t0 2\n"
    tree = lab.add_tree("/dev/md127", "gentoo", firmware="uefi", fstab=fstab)
    (tree / "efi").mkdir(exist_ok=True)
    lab.system.attach("/dev/sda1", fr.esp_tree(lab.tmp / "trees" / "esp", "gentoo"))
    result = run_scan(lab)
    found = by_device(result)
    assert list(found) == ["/dev/md127"] and "md:/dev/md127" in found["/dev/md127"]["stack"]  # the members are not probed one by one
    assert not [argv for argv in lab.system.commands_named("mount") if argv[-2] in {"/dev/sda2", "/dev/sdb1"}]
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/md127")
    flagged = {entry.data["device"]: entry.data["purpose"] for entry in manager.session(sid).ledger.of_kind("blockro")}
    assert flagged["/dev/md127"] == "session" and "/dev/sdb" not in flagged and "/dev/sda" not in flagged  # both member disks stay writable for the array
    assert [f for f in info["findings"] if f["severity"] == "error"] == []


# ---- a system installed on a USB stick ----------------------------------------------------------------------------------------------------
def test_a_portable_installation_is_offered_and_the_live_stick_is_hidden(lab_factory) -> None:
    lab = lab_factory(lf.usb_installed_root())
    lab.system.live_mounts.append(("/dev/sdc1", "/run/initramfs/live"))
    lab.add_tree("/dev/sdb2", "debian", firmware="uefi", esp_mount="/boot/efi")
    lab.system.attach("/dev/sdb1", fr.esp_tree(lab.tmp / "trees" / "esp", "debian"))
    result = run_scan(lab)
    assert list(by_device(result)) == ["/dev/sdb2"] and by_device(result)["/dev/sdb2"]["os"]["id"] == "debian"
    assert sorted(result.live_medium) == ["/dev/sdc", "/dev/sdc1", "/dev/sdc2"]
    assert {hint["id"] for hint in result.hints} == {"external_disks"}
    assert not [argv for argv in lab.system.commands if "/dev/sdc" in " ".join(argv) and argv[0] in {"mount", "blockdev"}]  # the live medium is never touched, not even for a probe
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/sdb2")
    flagged = {entry.data["device"]: entry.data["purpose"] for entry in manager.session(sid).ledger.of_kind("blockro")}
    assert flagged["/dev/sdb2"] == "session" and flagged["/dev/sda"] == "protect"  # the internal Windows disk is protected
    assert "/dev/sdc" not in flagged and "/dev/sdb" not in flagged  # hot-plug media stay as they are: the user may save a report there
    assert not lab.system.ro.get("/dev/sdc")


# ---- three systems on one disk --------------------------------------------------------------------------------------------------------------
def multiboot_lab(lab_factory, tmp_path: Path):
    lab = lab_factory(lf.multiboot_three())
    lab.add_tree("/dev/nvme0n1p2", "gentoo", firmware="uefi", esp_mount="/efi", root_uuid=fr.ROOT_UUID)
    lab.add_tree("/dev/nvme0n1p3", "debian", firmware="uefi", root_uuid=fr.HOME_UUID)
    top = tmp_path / "fedora-top"
    fr.make_root(top, "fedora", firmware="uefi", btrfs=True, root_uuid=fr.BOOT_UUID)
    lab.system.attach("/dev/nvme0n1p4", top)
    lab.system.subvolumes["/dev/nvme0n1p4"] = {5: ""}
    lab.system.btrfs_text["/dev/nvme0n1p4"] = ""
    lab.system.btrfs_default["/dev/nvme0n1p4"] = "ID 5 (FS_TREE)"
    esp = fr.esp_tree(lab.tmp / "trees" / "esp", "gentoo")
    fr.esp_tree(esp, "debian")
    fr.esp_tree(esp, "fedora", shim=True)
    lab.system.attach("/dev/nvme0n1p1", esp)
    return lab


def test_every_system_of_a_multiboot_machine_is_listed_with_its_own_family(lab_factory, tmp_path: Path) -> None:
    lab = multiboot_lab(lab_factory, tmp_path)
    result = run_scan(lab)
    found = by_device(result)
    assert {device: (item["os"]["id"], item["fstype"]) for device, item in found.items()} == {
        "/dev/nvme0n1p2": ("gentoo", "ext4"), "/dev/nvme0n1p3": ("debian", "ext4"), "/dev/nvme0n1p4": ("fedora", "btrfs")}
    assert len({item["id"] for item in result.candidates}) == 3  # stable, distinct ids
    assert {item["init"] for item in result.candidates} == {"openrc", "systemd"}
    assert {hint["id"] for hint in result.hints} == set()


@pytest.mark.parametrize("device,esp_mountpoint", [("/dev/nvme0n1p2", "/efi"), ("/dev/nvme0n1p3", "/boot/efi"), ("/dev/nvme0n1p4", "/boot/efi")])
def test_each_system_of_a_multiboot_machine_mounts_the_shared_esp_where_its_own_fstab_says(lab_factory, tmp_path: Path, device: str, esp_mountpoint: str) -> None:
    lab = multiboot_lab(lab_factory, tmp_path)
    manager, sid, info = mount_through_the_manager(lab, candidate_device=device)
    assert [m["mountpoint"] for m in info["mounts"]] == ["/", esp_mountpoint]
    assert [f for f in info["findings"] if f["severity"] == "error"] == []
    others = {entry.data["device"] for entry in manager.session(sid).ledger.of_kind("blockro") if entry.data["purpose"] == "protect"}
    assert device not in others and "/dev/nvme0n1p1" not in others  # the shared ESP is part of the session, the other systems' partitions are protected siblings
    assert {"/dev/nvme0n1p2", "/dev/nvme0n1p3", "/dev/nvme0n1p4"} - {device} <= others
    unmount(manager, sid)
    assert not any(lab.system.ro.values())


# ---- a real capture ---------------------------------------------------------------------------------------------------------------------------
def test_the_captured_desktop_is_understood_without_the_newer_lsblk_columns() -> None:
    graph = build_graph(lf.author_pc(), cmdline="BOOT_IMAGE=/boot/vmlinuz root=UUID=x ro quiet")
    roles = {node.path: node.role for node in graph if node.type == "part"}
    assert roles["/dev/nvme0n1p1"] == "esp" and roles["/dev/nvme0n1p2"] == "swap" and roles["/dev/nvme0n1p3"] == "filesystem"
    assert roles["/dev/sda2"] == "data" and roles["/dev/sda1"] == "other"  # NTFS data is listed, never mounted or probed; a reserved partition has nothing
    assert roles["/dev/sdb1"] == roles["/dev/sdc1"] == roles["/dev/nvme2n1p2"] == "filesystem" and roles["/dev/nvme1n1p1"] == "filesystem"
    root = graph.find("/dev/nvme0n1p3")
    assert root.fstype == "btrfs" and root.mountpoints == ("/var/lib/docker", "/")
    assert [disk.path for disk in graph.parent_disks(root)] == ["/dev/nvme0n1"]  # the parent comes from the nesting: PKNAME was not captured
    assert not any(node.live_medium for node in graph) and graph.find("/dev/nvme0n1p1").mountpoints == ("/boot/efi",)
    assert sorted(disk.path for disk in graph.disks()) == ["/dev/nvme0n1", "/dev/nvme1n1", "/dev/nvme2n1", "/dev/sda", "/dev/sdb", "/dev/sdc"]


def test_scanning_the_running_desktop_reads_the_system_through_its_mounts(lab_factory, tmp_path: Path) -> None:
    data = json.loads(lf.author_pc())
    system, docker = tmp_path / "running-root", tmp_path / "running-docker"
    fr.make_root(system, "gentoo", firmware="uefi", esp_mount="/boot/efi")
    docker.mkdir()
    for disk in data["blockdevices"]:
        for part in disk.get("children", []):
            if part["mountpoints"] == ["/var/lib/docker", "/"]:
                part["mountpoints"] = [str(docker), str(system)]
            else:
                part["mountpoints"] = [m for m in part["mountpoints"] if m == "[SWAP]"]  # the data disks are not mounted in this scenario
    lab = lab_factory(json.dumps(data))
    for device in ("/dev/sdb1", "/dev/sdc1", "/dev/nvme2n1p2", "/dev/nvme1n1p1"):
        lab.add_plain_tree(device, {"data/readme.txt": "just files"})
    result = run_scan(lab)
    found = by_device(result)
    assert list(found) == ["/dev/nvme0n1p3"] and found["/dev/nvme0n1p3"]["os"]["id"] == "gentoo" and found["/dev/nvme0n1p3"]["busy_elsewhere"] == str(system)
    assert "/dev/sda2" not in {argv[-2] for argv in lab.system.commands_named("mount")}  # NTFS is not probed
    assert result.live_medium == []
