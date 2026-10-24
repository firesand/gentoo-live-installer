"""lsblk JSON in the shape util-linux 2.42 prints (``--json --bytes --all`` with the rescue columns)."""

from __future__ import annotations

import copy
import json
import zlib
from typing import Any

from . import fakeroot as fr

ESP = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
BIOS_BOOT = "21686148-6449-6e6f-744e-656564726976"
LINUX_FS = "0fc63daf-8483-4772-8e79-3d69d8477de4"
LINUX_ROOT = "4f68bce3-e8cd-4db1-96e7-fbcaf984b709"
LINUX_SWAP = "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f"
LINUX_LVM = "e6d6d379-f507-44c2-a23c-238f2a3df928"
LINUX_RAID = "a19d880f-05fc-4d3b-a006-743f0f84911e"
LINUX_LUKS = "ca7d7ccb-63ed-4c53-861c-1742536059cc"
MS_BASIC = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
MS_RESERVED = "e3c9e316-0b5c-4db8-817d-f92df00215ae"
WIN_RECOVERY = "de94bba4-06d1-4d40-a16a-bfd50179d6ac"

GiB = 1 << 30
MiB = 1 << 20


def node(name: str, type: str, size: int, *, maj_min: str = "", pkname: str | None = None, fstype: str | None = None, label: str | None = None, uuid: str | None = None,
         partuuid: str | None = None, partlabel: str | None = None, parttype: str | None = None, partn: int | None = None, pttype: str | None = None, ptuuid: str | None = None,
         mountpoints: list[str | None] | None = None, fsroots: list[str] | None = None, children: list[dict[str, Any]] | None = None, ro: bool = False, rm: bool = False,
         hotplug: bool = False, tran: str | None = None, model: str | None = None, serial: str | None = None, wwn: str | None = None, fsver: str | None = None,
         path: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name, "kname": name, "pkname": pkname, "path": path or f"/dev/{name}", "maj:min": maj_min or f"240:{zlib.crc32(name.encode()) % 1000000}",
        "type": type, "size": size, "ro": ro, "rm": rm, "hotplug": hotplug, "tran": tran, "fstype": fstype, "fsver": fsver, "label": label, "uuid": uuid,
        "partuuid": partuuid, "partlabel": partlabel, "parttype": parttype, "partn": partn, "pttype": pttype, "ptuuid": ptuuid, "model": model, "serial": serial,
        "wwn": wwn, "log-sec": 512, "phy-sec": 512, "mountpoints": mountpoints if mountpoints is not None else [], "fsroots": fsroots if fsroots is not None else [],
    }
    if children:
        result["children"] = children
    return result


def lsblk(*roots: dict[str, Any]) -> str:
    return json.dumps({"blockdevices": list(roots)}, indent=3)


def _minor(major: int, index: int, partn: int = 0) -> str:
    return f"{major}:{index * 16 + partn}"


def sata_disk(letter: str, index: int, size: int, children: list[dict[str, Any]], *, model: str = "Samsung SSD 870", pttype: str = "gpt", serial: str | None = None,
              hotplug: bool = False, tran: str = "sata") -> dict[str, Any]:
    name = f"sd{letter}"
    return node(name, "disk", size, maj_min=_minor(8, index), tran=tran, model=model, serial=serial or f"SERIAL{letter.upper()}0001", wwn=f"0x5002538f7110000{index}",
                pttype=pttype, ptuuid=f"{index:08x}-0000-4000-8000-000000000001" if pttype == "gpt" else f"{index:08x}", hotplug=hotplug, rm=hotplug, children=children)


def part(parent: str, partn: int, size: int, major: int, index: int, *, sep: str = "", **kwargs: Any) -> dict[str, Any]:
    name = f"{parent}{sep}{partn}"
    kwargs.setdefault("partuuid", f"{index:08x}-0000-4000-8000-{partn:012x}")
    return node(name, "part", size, maj_min=_minor(major, index, partn), pkname=parent, partn=partn, **kwargs)


# ---- scenarios ------------------------------------------------------------------------------------
def plain_sata_mbr() -> str:
    """DOS table: sda1 ext4 root, an extended container holding swap and ext4 /home."""
    kids = [
        part("sda", 1, 40 * GiB, 8, 0, fstype="ext4", uuid=fr.ROOT_UUID, label="root", parttype="0x83", pttype="dos", mountpoints=[]),
        part("sda", 2, 1024, 8, 0, parttype="0x5", pttype="dos"),
        part("sda", 5, 4 * GiB, 8, 0, fstype="swap", uuid=fr.SWAP_UUID, parttype="0x82", pttype="dos"),
        part("sda", 6, 200 * GiB, 8, 0, fstype="ext4", uuid=fr.HOME_UUID, label="home", parttype="0x83", pttype="dos"),
    ]
    return lsblk(sata_disk("a", 0, 250 * GiB, kids, pttype="dos"))


def nvme_dualboot() -> str:
    """Windows and Linux on one NVMe disk (GPT) sharing the ESP."""
    kids = [
        part("nvme0n1", 1, 512 * MiB, 259, 0, sep="p", fstype="vfat", uuid=fr.ESP_UUID, label="SYSTEM", parttype=ESP, pttype="gpt"),
        part("nvme0n1", 2, 16 * MiB, 259, 0, sep="p", parttype=MS_RESERVED, pttype="gpt"),
        part("nvme0n1", 3, 400 * GiB, 259, 0, sep="p", fstype="ntfs", uuid="AEF8A308F8A2CDC1", label="Windows", parttype=MS_BASIC, pttype="gpt"),
        part("nvme0n1", 4, 1 * GiB, 259, 0, sep="p", fstype="ntfs", uuid="1234567890ABCDEF", label="Recovery", parttype=WIN_RECOVERY, pttype="gpt"),
        part("nvme0n1", 5, 100 * GiB, 259, 0, sep="p", fstype="ext4", uuid=fr.ROOT_UUID, parttype=LINUX_ROOT, pttype="gpt"),
        part("nvme0n1", 6, 16 * GiB, 259, 0, sep="p", fstype="swap", uuid=fr.SWAP_UUID, parttype=LINUX_SWAP, pttype="gpt"),
    ]
    disk = node("nvme0n1", "disk", 1000 * GiB, maj_min="259:0", tran="nvme", model="Samsung SSD 990 PRO 1TB", serial="S7NZ0000000001", wwn="eui.0025385b01000000", pttype="gpt",
                ptuuid="aaaaaaaa-0000-4000-8000-000000000001", children=kids)
    return lsblk(disk)


def debian_cloud() -> str:
    """Debian genericcloud: 1 root, 14 bios_boot, 15 ESP."""
    kids = [
        part("vda", 1, 2 * GiB, 254, 0, fstype="ext4", uuid=fr.ROOT_UUID, label="debian-root", parttype=LINUX_ROOT, pttype="gpt"),
        part("vda", 14, 3 * MiB, 254, 0, parttype=BIOS_BOOT, pttype="gpt"),
        part("vda", 15, 124 * MiB, 254, 0, fstype="vfat", uuid=fr.ESP_UUID, label="UEFI", parttype=ESP, pttype="gpt"),
    ]
    return lsblk(node("vda", "disk", 3 * GiB, maj_min="254:0", pttype="gpt", ptuuid="bbbbbbbb-0000-4000-8000-000000000001", serial="GENTOO-VM-DISPOSABLE", children=kids))


def fedora_cloud_btrfs() -> str:
    kids = [
        part("vda", 1, 2 * MiB, 254, 0, parttype=BIOS_BOOT, pttype="gpt"),
        part("vda", 2, 100 * MiB, 254, 0, fstype="vfat", uuid=fr.ESP_UUID, label="EFI-SYSTEM", parttype=ESP, pttype="gpt"),
        part("vda", 3, 1 * GiB, 254, 0, fstype="ext4", uuid=fr.BOOT_UUID, label="boot", parttype=LINUX_FS, pttype="gpt"),
        part("vda", 4, 4 * GiB, 254, 0, fstype="btrfs", uuid=fr.ROOT_UUID, label="fedora", parttype=LINUX_FS, pttype="gpt", fsroots=[]),
    ]
    return lsblk(node("vda", "disk", 5 * GiB, maj_min="254:0", pttype="gpt", ptuuid="cccccccc-0000-4000-8000-000000000001", serial="GENTOO-VM-DISPOSABLE", children=kids))


def ubuntu_lvm() -> str:
    lv_root = node("ubuntu--vg-ubuntu--lv", "lvm", 50 * GiB, maj_min="253:0", fstype="ext4", uuid=fr.ROOT_UUID, path="/dev/mapper/ubuntu--vg-ubuntu--lv")
    kids = [
        part("sda", 1, 1 * GiB, 8, 0, fstype="vfat", uuid=fr.ESP_UUID, parttype=ESP, pttype="gpt"),
        part("sda", 2, 2 * GiB, 8, 0, fstype="ext4", uuid=fr.BOOT_UUID, parttype=LINUX_FS, pttype="gpt"),
        part("sda", 3, 60 * GiB, 8, 0, fstype="LVM2_member", uuid="Xy9Zab-1234-abcd-efgh-ijkl-mnop-qrstuv", parttype=LINUX_LVM, pttype="gpt", children=[lv_root]),
    ]
    return lsblk(sata_disk("a", 0, 64 * GiB, kids))


def luks_lvm_ext4(*, unlocked: bool = False, activated: bool = False, mapper: str = "rescue-1f2e3d4c5b6a") -> str:
    """nvme0n1: ESP, ext4 /boot and a LUKS container (optionally unlocked, optionally with its VG active)."""
    luks_children: list[dict[str, Any]] = []
    if unlocked:
        lvs = []
        if activated:
            lvs = [
                node("vg0-root", "lvm", 80 * GiB, maj_min="253:2", fstype="ext4", uuid=fr.ROOT_UUID, path="/dev/mapper/vg0-root"),
                node("vg0-swap", "lvm", 8 * GiB, maj_min="253:3", fstype="swap", uuid=fr.SWAP_UUID, path="/dev/mapper/vg0-swap"),
            ]
        pv = node(mapper, "crypt", 99 * GiB, maj_min="253:1", fstype="LVM2_member", uuid="PVuuid-1111-2222-3333-4444-5555-666666", path=f"/dev/mapper/{mapper}", children=lvs)
        luks_children = [pv]
    kids = [
        part("nvme0n1", 1, 512 * MiB, 259, 0, sep="p", fstype="vfat", uuid=fr.ESP_UUID, parttype=ESP, pttype="gpt"),
        part("nvme0n1", 2, 1 * GiB, 259, 0, sep="p", fstype="ext4", uuid=fr.BOOT_UUID, parttype=LINUX_FS, pttype="gpt"),
        part("nvme0n1", 3, 99 * GiB, 259, 0, sep="p", fstype="crypto_LUKS", fsver="2", uuid=fr.LUKS_UUID, parttype=LINUX_LUKS, pttype="gpt", children=luks_children),
    ]
    return lsblk(node("nvme0n1", "disk", 100 * GiB, maj_min="259:0", tran="nvme", model="WD Blue SN570", serial="WDSN0001", wwn="eui.e8238fa6bf530001", pttype="gpt",
                      ptuuid="dddddddd-0000-4000-8000-000000000001", children=kids))


def luks_btrfs(*, unlocked: bool = False, mapper: str = "rescue-1f2e3d4c5b6a") -> str:
    children = [node(mapper, "crypt", 99 * GiB, maj_min="253:1", fstype="btrfs", uuid=fr.ROOT_UUID, label="arch", path=f"/dev/mapper/{mapper}")] if unlocked else []
    kids = [
        part("sda", 1, 512 * MiB, 8, 0, fstype="vfat", uuid=fr.ESP_UUID, parttype=ESP, pttype="gpt"),
        part("sda", 2, 99 * GiB, 8, 0, fstype="crypto_LUKS", fsver="2", uuid=fr.LUKS_UUID, parttype=LINUX_LUKS, pttype="gpt", children=children),
    ]
    return lsblk(sata_disk("a", 0, 100 * GiB, kids))


def md_raid1_under_lvm() -> str:
    """The md array and the LV on it are printed under BOTH member partitions (review S7)."""
    lv = node("vgdata-data", "lvm", 100 * GiB, maj_min="253:0", fstype="ext4", uuid=fr.HOME_UUID, path="/dev/mapper/vgdata-data")
    md = node("md127", "raid1", 100 * GiB, maj_min="9:127", fstype="LVM2_member", uuid="PVmd-1111-2222-3333-4444-5555-666666", children=[copy.deepcopy(lv)])
    members = [
        sata_disk("b", 1, 100 * GiB, [part("sdb", 1, 100 * GiB, 8, 1, fstype="linux_raid_member", uuid="aaaa1111-bbbb-2222-cccc-333344445555", label="host:data",
                                            parttype=LINUX_RAID, pttype="gpt", children=[copy.deepcopy(md)])]),
        sata_disk("c", 2, 100 * GiB, [part("sdc", 1, 100 * GiB, 8, 2, fstype="linux_raid_member", uuid="aaaa1111-bbbb-2222-cccc-333344445555", label="host:data",
                                            parttype=LINUX_RAID, pttype="gpt", children=[copy.deepcopy(md)])]),
    ]
    return lsblk(*members)


def md_raid1_unassembled() -> str:
    kids = lambda letter, index: [part(f"sd{letter}", 1, 100 * GiB, 8, index, fstype="linux_raid_member", uuid="aaaa1111-bbbb-2222-cccc-333344445555", label="host:root",  # noqa: E731
                                        parttype=LINUX_RAID, pttype="gpt")]
    return lsblk(sata_disk("b", 1, 100 * GiB, kids("b", 1)), sata_disk("c", 2, 100 * GiB, kids("c", 2)))


def lvm_inactive() -> str:
    kids = [part("sda", 1, 512 * MiB, 8, 0, fstype="vfat", uuid=fr.ESP_UUID, parttype=ESP, pttype="gpt"),
            part("sda", 2, 60 * GiB, 8, 0, fstype="LVM2_member", uuid="Xy9Zab-1234-abcd-efgh-ijkl-mnop-qrstuv", parttype=LINUX_LVM, pttype="gpt")]
    return lsblk(sata_disk("a", 0, 64 * GiB, kids))


def btrfs_multi_device() -> str:
    uuid = fr.ROOT_UUID
    return lsblk(
        sata_disk("a", 0, 100 * GiB, [part("sda", 1, 100 * GiB, 8, 0, fstype="btrfs", uuid=uuid, label="pool", parttype=LINUX_FS, pttype="gpt")]),
        sata_disk("b", 1, 100 * GiB, [part("sdb", 1, 100 * GiB, 8, 1, fstype="btrfs", uuid=uuid, label="pool", parttype=LINUX_FS, pttype="gpt")]),
    )


def xfs_external_log() -> str:
    """XFS root whose log lives on a partition of a second disk (it shares the file system's UUID), plus an unrelated third disk."""
    root_disk = sata_disk("a", 0, 100 * GiB, [
        part("sda", 1, 512 * MiB, 8, 0, fstype="vfat", uuid=fr.ESP_UUID, parttype=ESP, pttype="gpt"),
        part("sda", 2, 90 * GiB, 8, 0, fstype="xfs", uuid=fr.ROOT_UUID, parttype=LINUX_ROOT, pttype="gpt"),
    ])
    log_disk = sata_disk("b", 1, 8 * GiB, [part("sdb", 1, 1 * GiB, 8, 1, fstype="xfs_external_log", uuid=fr.ROOT_UUID, parttype=LINUX_FS, pttype="gpt")])
    other = sata_disk("c", 2, 500 * GiB, [part("sdc", 1, 500 * GiB, 8, 2, fstype="ext4", uuid=fr.HOME_UUID, parttype=LINUX_FS, pttype="gpt")])
    return lsblk(root_disk, log_disk, other)


def usb_live_hybrid() -> str:
    """The live USB stick (iso9660 + appended EFI partition) next to an internal disk with a Linux root."""
    stick = node("sdb", "disk", 16 * GiB, maj_min="8:16", tran="usb", model="Cruzer", serial="USB0001", hotplug=True, rm=True, pttype="dos", ptuuid="12345678", children=[
        part("sdb", 1, 3 * GiB, 8, 1, fstype="iso9660", label="GENTOO_INSTALLER_AMD64", uuid="2026-10-24-08-00-00-00", parttype="0x0", pttype="dos",
             mountpoints=["/run/initramfs/live"]),
        part("sdb", 2, 8 * MiB, 8, 1, fstype="vfat", uuid="BEEF-0001", label="ISOEFI", parttype="0xef", pttype="dos"),
    ])
    internal = sata_disk("a", 0, 500 * GiB, [
        part("sda", 1, 512 * MiB, 8, 0, fstype="vfat", uuid=fr.ESP_UUID, parttype=ESP, pttype="gpt"),
        part("sda", 2, 400 * GiB, 8, 0, fstype="ext4", uuid=fr.ROOT_UUID, parttype=LINUX_ROOT, pttype="gpt"),
    ])
    loop = node("loop0", "loop", 2800 * MiB, maj_min="7:0", fstype="squashfs", ro=True)
    return lsblk(loop, internal, stick)


def ventoy_live() -> str:
    """Ventoy style: the live medium is a device-mapper device built on the stick's second partition."""
    dm = node("ventoy", "dm", 3 * GiB, maj_min="253:0", fstype="iso9660", label="GENTOO_INSTALLER_AMD64", path="/dev/mapper/ventoy", mountpoints=["/run/initramfs/live"], ro=True)
    stick = node("sdb", "disk", 32 * GiB, maj_min="8:16", tran="usb", model="Ventoy", serial="VENTOY1", hotplug=True, rm=True, pttype="gpt", ptuuid="eeeeeeee-0000-4000-8000-000000000001", children=[
        part("sdb", 1, 30 * GiB, 8, 1, fstype="exfat", label="Ventoy", uuid="1111-2222", parttype=MS_BASIC, pttype="gpt"),
        part("sdb", 2, 32 * MiB, 8, 1, fstype="vfat", label="VTOYEFI", uuid="3333-4444", parttype=ESP, pttype="gpt", children=[dm]),
    ])
    internal = sata_disk("a", 0, 500 * GiB, [part("sda", 1, 400 * GiB, 8, 0, fstype="ext4", uuid=fr.ROOT_UUID, parttype=LINUX_ROOT, pttype="gpt")])
    return lsblk(internal, stick)


def desktop_automount() -> str:
    kids = [part("sda", 1, 40 * GiB, 8, 0, fstype="ext4", uuid=fr.ROOT_UUID, parttype=LINUX_ROOT, pttype="gpt", mountpoints=["/run/media/gentoo/root"]),
            part("sda", 2, 100 * GiB, 8, 0, fstype="btrfs", uuid=fr.HOME_UUID, parttype=LINUX_FS, pttype="gpt", mountpoints=["/run/media/gentoo/home"], fsroots=["/@home"])]
    return lsblk(sata_disk("a", 0, 150 * GiB, kids))


def cloned_uuids() -> str:
    def disk(letter: str, index: int) -> dict[str, Any]:
        return sata_disk(letter, index, 50 * GiB, [part(f"sd{letter}", 1, 50 * GiB, 8, index, fstype="ext4", uuid=fr.ROOT_UUID, parttype=LINUX_ROOT, pttype="gpt")])
    return lsblk(disk("a", 0), disk("b", 1))


def legacy_null() -> str:
    """util-linux 2.38 style: ``[null]`` mount points and string booleans."""
    data = json.loads(plain_sata_mbr())
    for item in data["blockdevices"]:
        for entry in [item, *item.get("children", [])]:
            entry["mountpoints"] = [None]
            entry["ro"] = "0"
            entry["rm"] = "0"
            entry["hotplug"] = "0"
            entry.pop("fsroots", None)
            entry.pop("partn", None)
    return json.dumps(data)


def windows_only() -> str:
    kids = [part("sda", 1, 500 * MiB, 8, 0, fstype="ntfs", uuid="AAAA1111BBBB2222", label="Recovery", parttype=WIN_RECOVERY, pttype="gpt"),
            part("sda", 2, 100 * MiB, 8, 0, fstype="vfat", uuid=fr.ESP_UUID, parttype=ESP, pttype="gpt"),
            part("sda", 3, 200 * GiB, 8, 0, fstype="ntfs", uuid="CCCC3333DDDD4444", label="OS", parttype=MS_BASIC, pttype="gpt")]
    return lsblk(sata_disk("a", 0, 200 * GiB, kids))


def exotic_filesystems() -> str:
    kids = [part("sda", 1, 10 * GiB, 8, 0, fstype="reiserfs", uuid="r1", parttype=LINUX_FS, pttype="gpt"),
            part("sda", 2, 10 * GiB, 8, 0, fstype="zfs_member", uuid="1234567890", parttype=LINUX_FS, pttype="gpt"),
            part("sda", 3, 10 * GiB, 8, 0, fstype="bcachefs", uuid="b1", parttype=LINUX_FS, pttype="gpt"),
            part("sda", 4, 10 * GiB, 8, 0, fstype="BitLocker", parttype=MS_BASIC, pttype="gpt")]
    return lsblk(sata_disk("a", 0, 50 * GiB, kids))


SCENARIOS = {
    "plain_sata_mbr": plain_sata_mbr, "nvme_dualboot": nvme_dualboot, "debian_cloud": debian_cloud, "fedora_cloud_btrfs": fedora_cloud_btrfs, "ubuntu_lvm": ubuntu_lvm,
    "luks_lvm_ext4": luks_lvm_ext4, "luks_btrfs": luks_btrfs, "md_raid1_under_lvm": md_raid1_under_lvm, "md_raid1_unassembled": md_raid1_unassembled,
    "lvm_inactive": lvm_inactive, "btrfs_multi_device": btrfs_multi_device, "xfs_external_log": xfs_external_log, "usb_live_hybrid": usb_live_hybrid, "ventoy_live": ventoy_live,
    "desktop_automount": desktop_automount, "cloned_uuids": cloned_uuids, "legacy_null": legacy_null, "windows_only": windows_only, "exotic_filesystems": exotic_filesystems,
}
