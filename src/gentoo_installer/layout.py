"""Guided-layout arithmetic and the storage plan description, shared by the GUI and the daemon.

Pure functions: no Qt, no subprocess. StorageManager._erase_layout creates exactly the
partitions these functions describe, so the estimate the GUI shows cannot drift from the real
layout, and the daemon repeats the same capacity check before it touches a disk.

* guided_plan() / capacity_problems(): how much room the root filesystem gets and whether
  that is enough for the chosen desktop (S5).
* plan_storage(): what will happen to every disk the install touches, for the Review page
  and the install log (S2).
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from . import blockdev
from .manifest import InstallManifest, Storage

MIB = 1024 * 1024
GIB = 1024 * MIB
ESP_MIB = 1024
BIOS_BOOT_MIB = 3
BOOT_MIB = 1024
# 1 MiB of alignment gap in front plus room for the backup GPT and rounding at the end.
RESERVED_MIB = 2
# An MBR partition entry holds 32-bit sector numbers: 2^32 - 1 sectors of the logical sector size.
DOS_MAX_SECTORS = 2**32 - 1
DOS_MAX_BYTES = DOS_MAX_SECTORS * 512
# mkfs.vfat -F 32 needs at least 65525 clusters of 512 bytes.
MIN_FAT32_MIB = 33

# Root filesystem sizes for a binary-package (binhost) install. They are estimates: the
# daemon logs `df` after the validation stage so they can be calibrated from real runs.
# Below MIN_ROOT_MIB the install is refused before any disk is touched; below
# RECOMMENDED_ROOT_MIB the user is warned.
MIN_ROOT_MIB = {"minimal": 8 * 1024, "xfce": 14 * 1024, "gnome": 18 * 1024, "kde": 24 * 1024}
RECOMMENDED_ROOT_MIB = {"minimal": 15 * 1024, "xfce": 22 * 1024, "gnome": 28 * 1024, "kde": 40 * 1024}
# Extra room when packages are compiled instead of downloaded (portage.binhost off).
SOURCE_BUILD_EXTRA_MIB = {"minimal": 10 * 1024, "xfce": 12 * 1024, "gnome": 14 * 1024, "kde": 16 * 1024}


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def minimum_root_mib(manifest: InstallManifest) -> int:
    """Smallest root filesystem (MiB) the chosen desktop and package source need."""
    desktop = manifest.system.desktop if manifest.system.desktop in MIN_ROOT_MIB else "kde"
    extra = 0 if manifest.portage.binhost else SOURCE_BUILD_EXTRA_MIB[desktop]
    return MIN_ROOT_MIB[desktop] + extra


def recommended_root_mib(manifest: InstallManifest) -> int:
    desktop = manifest.system.desktop if manifest.system.desktop in RECOMMENDED_ROOT_MIB else "kde"
    extra = 0 if manifest.portage.binhost else SOURCE_BUILD_EXTRA_MIB[desktop]
    return RECOMMENDED_ROOT_MIB[desktop] + extra


def swap_in_root(storage: Storage) -> bool:
    """Whether the guided layout keeps swap as a file inside the (encrypted) root.

    An encrypted install must not leave swap in the clear, and a second LUKS container would
    need a second passphrase prompt, so the swap lives inside the encrypted root filesystem.
    """
    return bool(storage.luks and storage.swap_mib)


@dataclass(frozen=True)
class GuidedPlan:
    disk_mib: int
    fixed: tuple[tuple[str, int], ...]  # partitions created before the root, in order
    root_mib: int  # what is left for the root partition (may be <= 0)
    swap_file_mib: int = 0  # swap kept as a file inside the root filesystem

    @property
    def usable_root_mib(self) -> int:
        """Root space left for the system once a swap file has been created."""
        return self.root_mib - self.swap_file_mib


def guided_plan(storage: Storage, disk_bytes: int) -> GuidedPlan:
    """Partition sizes StorageManager._erase_layout creates on a disk of ``disk_bytes``."""
    fixed: list[tuple[str, int]] = []
    if storage.firmware == "uefi":
        fixed.append(("EFI system partition", ESP_MIB))
    elif storage.table == "gpt":
        fixed.append(("BIOS boot partition", BIOS_BOOT_MIB))
    if storage.luks:
        fixed.append(("/boot", BOOT_MIB))
    if storage.swap_mib and not swap_in_root(storage):
        fixed.append(("swap", storage.swap_mib))
    if storage.separate_home:
        fixed.append(("/home", storage.home_mib))
    disk_mib = disk_bytes // MIB
    return GuidedPlan(disk_mib, tuple(fixed), disk_mib - RESERVED_MIB - sum(size for _, size in fixed), storage.swap_mib if swap_in_root(storage) else 0)


def capacity_problems(manifest: InstallManifest, disk_bytes: int, log_sec: int = 512) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a guided erase of a disk of ``disk_bytes``; ([], []) for other modes.

    ``disk_bytes <= 0`` means "unknown" (a preview without an inventory) and is not an error.
    """
    storage = manifest.storage
    if storage.mode != "erase" or disk_bytes <= 0:
        return [], []
    errors: list[str] = []
    warnings: list[str] = []
    if storage.table == "dos" and disk_bytes > DOS_MAX_SECTORS * max(log_sec, 512):
        limit = DOS_MAX_SECTORS * max(log_sec, 512) / 1024**4
        errors.append(
            f"A DOS/MBR partition table cannot address more than {limit:.0f} TiB, but the disk has {disk_bytes / 1024**4:.1f} TiB: "
            "choose GPT (legacy BIOS boot then uses a BIOS boot partition)"
        )
    plan = guided_plan(storage, disk_bytes)
    desktop = manifest.system.desktop
    minimum = minimum_root_mib(manifest)
    recommended = recommended_root_mib(manifest)
    layout = ", ".join(f"{name} {size / 1024:.1f} GiB" for name, size in plan.fixed) or "no other partitions"
    if plan.swap_file_mib:
        layout += f", swap file {plan.swap_file_mib / 1024:.1f} GiB inside the encrypted root"
    source = "" if manifest.portage.binhost else " built from source"
    if plan.usable_root_mib < minimum:
        errors.append(
            f"The disk is too small for this layout: {disk_bytes / GIB:.1f} GiB minus {layout} leaves "
            f"{max(plan.usable_root_mib, 0) / 1024:.1f} GiB for the system, but a {desktop} installation{source} needs at least {minimum / 1024:.0f} GiB. "
            "Choose a larger disk, reduce swap or /home, or pick a lighter desktop."
        )
    elif plan.usable_root_mib < recommended:
        warnings.append(
            f"The system will get only {plan.usable_root_mib / 1024:.1f} GiB; {recommended / 1024:.0f} GiB or more is recommended for {desktop}{source} "
            "so updates and builds have room."
        )
    return errors, warnings


# --- plan description -------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedVolume:
    role: str  # efi | bios_boot | boot | swap | swapfile | root | home | data
    mountpoint: str
    filesystem: str
    size_mib: int  # 0 = whatever is left
    action: str  # create | format | keep
    device: str = ""  # partition path when it exists already (manual rows)
    encrypted: bool = False
    note: str = ""  # what it holds now, or a caveat

    def describe(self) -> str:
        size = "the rest" if not self.size_mib else human_size(self.size_mib * MIB)
        where = f" → {self.mountpoint}" if self.mountpoint else ""
        target = self.device or {"efi": "EFI system partition", "bios_boot": "BIOS boot partition", "swapfile": "swap file"}.get(self.role, self.role)
        encrypted = " (encrypted)" if self.encrypted else ""
        note = f" — {self.note}" if self.note else ""
        verb = {"create": "CREATE", "format": "FORMAT", "keep": "KEEP  "}[self.action]
        return f"{verb}  {target}{where} ({self.filesystem}, {size}){encrypted}{note}"


@dataclass(frozen=True)
class PlannedDisk:
    path: str
    action: str  # erase | use
    description: str  # model, size and identity
    table: str = ""  # partition table the disk gets
    current: str = ""  # what is on it today
    volumes: tuple[PlannedVolume, ...] = ()


@dataclass(frozen=True)
class StoragePlan:
    disks: tuple[PlannedDisk, ...]
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def lines(self) -> list[str]:
        """The plan as plain text: every disk touched, then what is created, formatted and kept."""
        lines: list[str] = []
        for disk in self.disks:
            if disk.action == "erase":
                lines.append(f"ERASE  {disk.description}: all existing data on it will be destroyed" + (f" (today: {disk.current})" if disk.current else ""))
                lines.append(f"       new {disk.table.upper()} partition table")
            else:
                lines.append(f"USE    {disk.description}: only the partitions listed below are touched" + (f" (today: {disk.current})" if disk.current else ""))
            lines.extend("       " + volume.describe() for volume in disk.volumes)
        lines.extend(self.notes)
        lines.extend("Warning: " + warning for warning in self.warnings)
        lines.extend("Problem: " + error for error in self.errors)
        return lines


def identity_text(disk: blockdev.BlockDevice) -> str:
    """Short, recognisable identity of a disk: the tail of its serial or WWN."""
    if disk.serial:
        return f"serial …{disk.serial[-6:]}"
    if disk.wwn:
        return f"WWN …{disk.wwn[-6:]}"
    return "no serial number"


def disk_description(disk: blockdev.BlockDevice) -> str:
    model = disk.model or "disk"
    return f"{disk.path} ({model}, {human_size(disk.size)}, {identity_text(disk)})"


def contents_text(disk: blockdev.BlockDevice) -> str:
    """One-line summary of what a disk (or partition) holds today."""
    parts = [node for node in disk.walk()[1:] if node.type == "part"]
    if not parts:
        return f"{disk.fstype} {human_size(disk.size)}" if disk.fstype else "no partitions"
    shown = [f"{node.fstype or 'no filesystem'} {human_size(node.size)}" + (f" “{node.label}”" if node.label else "") for node in parts[:4]]
    more = f", +{len(parts) - 4} more" if len(parts) > 4 else ""
    return f"{len(parts)} partition{'s' if len(parts) != 1 else ''}: " + ", ".join(shown) + more


def _node_text(node: blockdev.BlockDevice) -> str:
    return f"now {node.fstype or 'no filesystem'} {human_size(node.size)}" + (f" “{node.label}”" if node.label else "")


def plan_storage(
    manifest: InstallManifest,
    inventory: Sequence[blockdev.BlockDevice] | None = None,
    *,
    btrfs_subvolumes: bool = True,
    swap_file: bool = True,
) -> StoragePlan:
    """What the install will do to storage.

    ``inventory`` (lsblk roots from blockdev.parse_lsblk) adds model, size, identity and the
    current contents of every disk and partition the plan touches; without it the plan still
    lists the partitions. ``btrfs_subvolumes`` says whether the guided Btrfs layout uses the
    @ / @home subvolumes and ``swap_file`` whether an encrypted install gets its swap file (the
    daemon reports what it will really do).
    """
    storage = manifest.storage
    roots = list(inventory or [])
    try:
        selected = blockdev.find(roots, storage.device) if roots else None
    except (LookupError, ValueError):
        selected = None
    if storage.mode == "erase":
        return _erase_plan(manifest, selected, btrfs_subvolumes, swap_file)
    return _manual_plan(manifest, roots, selected)


def _erase_plan(manifest: InstallManifest, selected: blockdev.BlockDevice | None, btrfs_subvolumes: bool, swap_file: bool) -> StoragePlan:
    storage = manifest.storage
    volumes: list[PlannedVolume] = []
    notes: list[str] = []
    if storage.firmware == "uefi":
        volumes.append(PlannedVolume("efi", "/efi", "vfat", ESP_MIB, "create"))
    elif storage.table == "gpt":
        volumes.append(PlannedVolume("bios_boot", "", "none", BIOS_BOOT_MIB, "create"))
    if storage.luks:
        volumes.append(PlannedVolume("boot", "/boot", "ext4", BOOT_MIB, "create", note="not encrypted: the bootloader must read it"))
    if storage.swap_mib and not swap_in_root(storage):
        volumes.append(PlannedVolume("swap", "", "swap", storage.swap_mib, "create"))
    if storage.separate_home:
        volumes.append(PlannedVolume("home", "/home", storage.filesystem, storage.home_mib, "create"))
    root_note = ""
    if storage.filesystem == "btrfs" and btrfs_subvolumes:
        root_note = "Btrfs subvolumes @ (/)" + ("" if storage.separate_home else " and @home (/home)")
    root_mib = guided_plan(storage, selected.size).root_mib if selected is not None else 0
    volumes.append(PlannedVolume("root", "/", storage.filesystem, max(root_mib, 0), "create", encrypted=storage.luks, note=root_note))
    if swap_in_root(storage) and swap_file:
        volumes.append(PlannedVolume("swapfile", "", "swap", storage.swap_mib, "create", encrypted=True, note="a file inside the encrypted root, so swapped-out memory is encrypted too"))
    elif swap_in_root(storage):
        notes.append("No swap is set up: swap outside the encrypted root would be stored unencrypted.")
    if storage.luks and storage.lvm:
        notes.append("LVM is created inside the encrypted container.")
    errors: list[str] = []
    warnings: list[str] = []
    if selected is not None:
        errors, warnings = capacity_problems(manifest, selected.size, selected.log_sec or 512)
        description, current = disk_description(selected), contents_text(selected)
        extra = blockdev.stacked_devices(selected)
        if extra:
            warnings.append("Active volumes on this disk (" + ", ".join(f"{node.type} {node.path}" for node in extra) + ") are part of what is erased.")
    else:
        description, current = storage.device or "the selected disk", ""
        plan = guided_plan(storage, 0)
        if plan.fixed:
            notes.append("Sizes of the remaining space depend on the disk and are checked before anything is written.")
    notes.append("Other disks will not be modified.")
    return StoragePlan((PlannedDisk(storage.device, "erase", description, storage.table, current, tuple(volumes)),), tuple(notes), tuple(warnings), tuple(errors))


def _manual_plan(manifest: InstallManifest, roots: Sequence[blockdev.BlockDevice], selected: blockdev.BlockDevice | None) -> StoragePlan:
    storage = manifest.storage
    grouped: dict[str, list[PlannedVolume]] = {}
    described: dict[str, blockdev.BlockDevice | None] = {}
    notes: list[str] = []
    warnings: list[str] = []
    for row in storage.partitions:
        try:
            node = blockdev.find(roots, row.device) if roots else None
        except (LookupError, ValueError):
            node = None
        parents = blockdev.parent_disks(roots, node) if node is not None else []
        owner = parents[0] if parents else None
        key = owner.path if owner else _guess_parent(row.device)
        described.setdefault(key, owner)
        action = "format" if row.format and row.filesystem != "none" else "keep"
        role = "swap" if row.filesystem == "swap" else "root" if row.mountpoint == "/" else "data"
        note = _node_text(node) if node is not None else ""
        if len(parents) > 1:
            note += (", " if note else "") + "also uses " + ", ".join(p.path for p in parents[1:])
        if row.mountpoint == "/" and action == "keep":
            note = (note + "; " if note else "") + "the new system is installed INTO this partition and replaces its /etc, accounts and boot files"
            warnings.append(f"{row.device} is kept but the new system is installed into it: its /etc, accounts and boot files are replaced (a backup of /etc is made first).")
        if row.label and action == "format":
            note += (", " if note else "") + f"label “{row.label}”"
        grouped.setdefault(key, []).append(PlannedVolume(role, row.mountpoint, row.filesystem, 0, action, row.device, encrypted=False, note=note))
    if storage.luks:
        notes.append(storage.encryption_summary())
    if storage.firmware == "bios":
        notes.append(f"Legacy BIOS: the bootloader is written to the start of {storage.device}.")
    disks = []
    for key, volumes in grouped.items():
        owner = described.get(key)
        disks.append(PlannedDisk(key, "use", disk_description(owner) if owner is not None else key, "", contents_text(owner) if owner is not None else "", tuple(volumes)))
    notes.append("Disks and partitions that are not listed will not be modified.")
    return StoragePlan(tuple(disks), tuple(notes), tuple(warnings))


def _guess_parent(device: str) -> str:
    """Parent disk of a partition path when there is no inventory (/dev/sdb1 -> /dev/sdb)."""
    name = os.path.basename(device)
    if name.startswith(("nvme", "mmcblk", "loop")) and "p" in name:
        return os.path.join(os.path.dirname(device), name.rsplit("p", 1)[0])
    stripped = name.rstrip("0123456789")
    return os.path.join(os.path.dirname(device), stripped) if stripped and stripped != name and not device.startswith("/dev/mapper/") else device
