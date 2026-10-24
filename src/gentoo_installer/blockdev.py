"""Shared block-device discovery for the GUI, the daemon, hardware detection and rescue.

One lsblk parser instead of several: lsblk prints a device once under every
parent it relates to (an md array under each member partition, an LV under
each PV), so lookups go through MAJ:MIN and treat repeated nodes as one device.
Pure functions only; callers run lsblk/findmnt through their own runner.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence

LSBLK_COLUMNS = (
    "NAME", "KNAME", "PATH", "MAJ:MIN", "TYPE", "SIZE", "RO", "RM", "HOTPLUG", "TRAN",
    "MODEL", "SERIAL", "WWN", "FSTYPE", "FSVER", "LABEL", "UUID", "PARTTYPE", "PARTLABEL",
    "PARTUUID", "PTTYPE", "PTUUID", "LOG-SEC", "PHY-SEC", "MOUNTPOINTS",
)
LIVE_LABEL_RE = re.compile(r"(?:^|\s)root=live:(?:CD)?LABEL=(\S+)")


def lsblk_argv(*, all_devices: bool = False) -> tuple[str, ...]:
    """Command that produces the JSON parse_lsblk() expects."""
    argv = ("lsblk", "--json", "--bytes", "--output", ",".join(LSBLK_COLUMNS))
    return argv + ("--all",) if all_devices else argv


def _text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    return "" if value is None else str(value).strip()


def _flag(raw: dict[str, Any], key: str) -> bool:
    value = raw.get(key)
    return value in (True, 1, "1", "true")


def _int(raw: dict[str, Any], key: str) -> int:
    try:
        return int(raw.get(key) or 0)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class Identity:
    """A disk identity the user approved: serial, WWN, or by-path fallback."""

    kind: str  # "serial" | "wwn" | "path"
    value: str


@dataclass(frozen=True)
class BlockDevice:
    name: str
    path: str
    type: str
    size: int = 0
    kname: str = ""
    maj_min: str = ""
    ro: bool = False
    removable: bool = False
    hotplug: bool = False
    transport: str = ""
    model: str = ""
    serial: str = ""
    wwn: str = ""
    fstype: str = ""
    fsver: str = ""
    label: str = ""
    uuid: str = ""
    parttype: str = ""
    partlabel: str = ""
    partuuid: str = ""
    pttype: str = ""
    ptuuid: str = ""
    log_sec: int = 0
    phy_sec: int = 0
    mountpoints: tuple[str, ...] = ()
    children: tuple[BlockDevice, ...] = ()

    @classmethod
    def from_lsblk(cls, raw: dict[str, Any]) -> BlockDevice:
        name = _text(raw, "name")
        mountpoints = raw.get("mountpoints")
        if mountpoints is None and raw.get("mountpoint"):
            mountpoints = [raw.get("mountpoint")]
        return cls(
            name=name,
            path=_text(raw, "path") or "/dev/" + name,
            type=_text(raw, "type"),
            size=_int(raw, "size"),
            kname=_text(raw, "kname"),
            maj_min=_text(raw, "maj:min"),
            ro=_flag(raw, "ro"),
            removable=_flag(raw, "rm"),
            hotplug=_flag(raw, "hotplug"),
            transport=_text(raw, "tran"),
            model=_text(raw, "model"),
            serial=_text(raw, "serial"),
            wwn=_text(raw, "wwn"),
            fstype=_text(raw, "fstype"),
            fsver=_text(raw, "fsver"),
            label=_text(raw, "label"),
            uuid=_text(raw, "uuid"),
            parttype=_text(raw, "parttype"),
            partlabel=_text(raw, "partlabel"),
            partuuid=_text(raw, "partuuid"),
            pttype=_text(raw, "pttype"),
            ptuuid=_text(raw, "ptuuid"),
            log_sec=_int(raw, "log-sec"),
            phy_sec=_int(raw, "phy-sec"),
            # util-linux <= 2.40 prints [null] for an unmounted device.
            mountpoints=tuple(str(item) for item in (mountpoints or []) if item),
            children=tuple(cls.from_lsblk(child) for child in raw.get("children") or []),
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BlockDevice:
        """Compatibility name for from_lsblk() (the daemon's old Disk.from_dict)."""
        return cls.from_lsblk(raw)

    def walk(self) -> tuple[BlockDevice, ...]:
        return (self,) + tuple(node for child in self.children for node in child.walk())

    @property
    def key(self) -> str:
        """Stable identity of the kernel device (MAJ:MIN, or the path)."""
        return self.maj_min or os.path.realpath(self.path)

    @property
    def mounted(self) -> bool:
        return any(node.mountpoints for node in self.walk())

    @property
    def identity(self) -> Identity | None:
        if self.serial:
            return Identity("serial", self.serial)
        if self.wwn:
            return Identity("wwn", self.wwn)
        return None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_lsblk(text: str) -> list[BlockDevice]:
    raw = json.loads(text)
    return [BlockDevice.from_lsblk(item) for item in raw.get("blockdevices") or []]


def all_nodes(roots: Sequence[BlockDevice]) -> list[BlockDevice]:
    """Every node once per kernel device (repeated multi-parent nodes merged)."""
    seen: dict[str, BlockDevice] = {}
    for root in roots:
        for node in root.walk():
            seen.setdefault(node.key, node)
    return list(seen.values())


def find(roots: Sequence[BlockDevice], device: str) -> BlockDevice:
    """Find one kernel device by path (symlinks resolved).

    Raises LookupError when absent and ValueError when the path names several
    different kernel devices. A node lsblk repeats under several parents is one
    device, not an ambiguity.
    """
    wanted = os.path.realpath(device)
    matches: dict[str, BlockDevice] = {}
    for root in roots:
        for node in root.walk():
            if os.path.realpath(node.path) == wanted:
                matches.setdefault(node.key, node)
    if not matches:
        raise LookupError(f"Block device not found: {device}")
    if len(matches) > 1:
        raise ValueError(f"Block device path is ambiguous: {device}")
    return next(iter(matches.values()))


def parent_disks(roots: Sequence[BlockDevice], device: BlockDevice) -> list[BlockDevice]:
    """Whole disks (type disk) whose subtree contains the device."""
    found: dict[str, BlockDevice] = {}
    for root in roots:
        if root.type == "disk" and any(node.key == device.key for node in root.walk()):
            found.setdefault(root.key, root)
    return list(found.values())


def path_identity(disk: BlockDevice, by_path_name: str) -> Identity:
    """Fallback identity for a disk without serial/WWN (needs user approval)."""
    if not by_path_name or "/" in by_path_name or "|" in by_path_name:
        raise ValueError("A by-path link name is required for a path identity")
    return Identity("path", f"{by_path_name}|{disk.size}|{disk.model.replace('|', ' ')}")


def identity_matches(disk: BlockDevice, kind: str, value: str, by_path_name: str = "") -> bool:
    """Whether the disk still matches an identity recorded earlier."""
    if not value:
        return False
    if kind in ("", "serial", "wwn"):
        return value in {disk.serial, disk.wwn} - {""}
    if kind == "path":
        return bool(by_path_name) and path_identity(disk, by_path_name).value == value
    return False


def live_medium_label(cmdline: str) -> str:
    match = LIVE_LABEL_RE.search(cmdline)
    return match.group(1) if match else ""


def live_medium_keys(roots: Sequence[BlockDevice], *, sources: Iterable[str] = (), cmdline: str = "") -> set[str]:
    """Keys of the live medium and every disk holding it.

    sources are the devices backing the live system (for example the SOURCE of
    /run/initramfs/live from findmnt); the kernel command line contributes the
    dracut root=live:CDLABEL=... label. Both rules are applied because a medium
    copied to RAM is no longer mounted but still carries its label.
    """
    label = live_medium_label(cmdline)
    marked: set[str] = set()
    for source in sources:
        try:
            marked.add(find(roots, source).key)
        except (LookupError, ValueError):
            continue
    for node in all_nodes(roots):
        if label and node.label == label:
            marked.add(node.key)
    for node in all_nodes(roots):
        if node.key in marked:
            marked.update(disk.key for disk in parent_disks(roots, node))
    return marked


# --- which devices may be offered as an installation target -------------------

# Kernel placeholders that lsblk reports as "disk" but never hold an installation.
VIRTUAL_DEVICE_RE = re.compile(r"^(?:loop|ram|zram|nbd|sr|fd)\d*$")
# eMMC exposes its boot areas and the replay-protected memory block as extra read-only disks.
HIDDEN_MMC_RE = re.compile(r"^mmcblk\d+(?:boot\d+|rpmb)$")

TARGET_PROBLEMS = {
    "not_a_disk": "is not a whole disk",
    "virtual": "is a virtual or placeholder device (RAM disk, zram, loop, network block or floppy)",
    "firmware_area": "is a hidden eMMC boot or replay-protected area",
    "empty": "has no media (an empty card-reader slot reports 0 bytes)",
    "read_only": "is read-only",
    "live_medium": "holds the live installer medium this system was started from",
}


def install_target_problem(node: BlockDevice, *, live_keys: Iterable[str] = (), allow_loop: bool = False) -> str:
    """Why a top-level lsblk node cannot be an installation target ("" when it can).

    The result is a key of TARGET_PROBLEMS. live_keys comes from live_medium_keys().
    allow_loop admits loop devices (hand-written manifests and tests; never the GUI).
    """
    name = os.path.basename(node.path) or node.name
    if node.type == "loop":
        if not allow_loop:
            return "virtual"
    elif VIRTUAL_DEVICE_RE.match(name):
        return "virtual"
    elif HIDDEN_MMC_RE.match(name):
        return "firmware_area"
    elif node.type != "disk":
        return "not_a_disk"
    if node.size <= 0:
        return "empty"
    if node.ro:
        return "read_only"
    if node.key in set(live_keys):
        return "live_medium"
    return ""


def install_target_disks(roots: Sequence[BlockDevice], live_keys: Iterable[str] = (), *, allow_loop: bool = False) -> list[BlockDevice]:
    """The top-level devices that may be offered as installation targets."""
    keys = set(live_keys)
    return [node for node in roots if not install_target_problem(node, live_keys=keys, allow_loop=allow_loop)]


# --- what is alive on a disk ------------------------------------------------------

# Everything lsblk lists below a disk that is not one of these is a stacked device (LVM, dm-crypt, md, multipath).
STRUCTURAL_TYPES = frozenset({"disk", "part", "loop", "rom"})


def stacked_devices(node: BlockDevice) -> list[BlockDevice]:
    """Active LVM / dm-crypt / md / device-mapper devices below ``node``, children first (so they can be stopped in order).

    A device lsblk repeats under several parents is listed once.
    """
    seen: set[str] = set()
    found: list[BlockDevice] = []
    for child in reversed(node.walk()[1:]):
        if child.type not in STRUCTURAL_TYPES and child.key not in seen:
            seen.add(child.key)
            found.append(child)
    return found


# Filesystems that can spread over several devices. lsblk shows the filesystem's own UUID (the Btrfs fsid, the ZFS pool
# GUID, the bcachefs UUID) on every member, so the same type and UUID on another device means the same filesystem.
POOL_FILESYSTEMS = {"btrfs": "Btrfs filesystem", "zfs_member": "ZFS pool", "bcachefs": "bcachefs filesystem"}


@dataclass(frozen=True)
class FilesystemPool:
    """A multi-device filesystem that has members inside the inspected devices and on other devices too."""

    fstype: str
    uuid: str
    inside: tuple[str, ...]  # member devices inside the inspected devices
    outside: tuple[str, ...]  # members elsewhere: erasing the inspected devices damages those too

    @property
    def spans_other_disks(self) -> bool:
        return bool(self.outside)

    def describe(self) -> str:
        label = POOL_FILESYSTEMS.get(self.fstype, self.fstype)
        return f"{label} {self.uuid} (members here: {', '.join(self.inside)}; also on {', '.join(self.outside)})"

    def reason(self) -> str:
        """One line for a disk card: why this disk is not free to erase (the wording of the other 'is part of ...' reasons)."""
        label = POOL_FILESYSTEMS.get(self.fstype, self.fstype)
        return f"{', '.join(self.inside)} is part of a {label} that also uses {', '.join(self.outside)}"


def filesystem_pools(roots: Sequence[BlockDevice], nodes: Sequence[BlockDevice]) -> list[FilesystemPool]:
    """The multi-device filesystems (Btrfs, ZFS, bcachefs) that have a member inside ``nodes`` and another outside.

    Erasing ``nodes`` destroys the member inside and with it data that the devices outside hold too. A filesystem
    on a single device is never reported, and neither is a pool whose members all lie inside ``nodes``.
    A copy made with dd shares the UUID as well: that only asks one question more.
    """
    inside = {sub.key: sub for node in nodes for sub in node.walk()}
    outside = [sub for sub in all_nodes(roots) if sub.key not in inside]
    found: list[FilesystemPool] = []
    for fstype in POOL_FILESYSTEMS:
        for uuid in dict.fromkeys(sub.uuid for sub in inside.values() if sub.fstype == fstype and sub.uuid):
            here = [sub.path for sub in inside.values() if sub.fstype == fstype and sub.uuid == uuid]
            elsewhere = [sub.path for sub in outside if sub.fstype == fstype and sub.uuid == uuid]
            if elsewhere:
                found.append(FilesystemPool(fstype, uuid, tuple(here), tuple(elsewhere)))
    return found


def active_swaps(node: BlockDevice) -> list[str]:
    """Paths of the devices at or below ``node`` that are in use as swap."""
    return [sub.path for sub in node.walk() if "[SWAP]" in sub.mountpoints]
