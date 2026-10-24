"""What the GUI knows about the machine it runs on.

Everything here works without root: the disk list comes from lsblk through the
shared blockdev parser, the rest from /proc and /sys. When the installer daemon
is reachable the GUI prefers its view for readiness (tools, network, warnings);
this module only merges that reply into a SystemInfo (merge_inspection).
"""

from __future__ import annotations

import email.utils
import http.client
import os
import platform
import re
import shutil
import ssl
import subprocess
import threading
import time
from base64 import b64encode
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import urlparse

from . import blockdev, layout
from .daemon_client import DaemonState
from .hardware import HardwareProfile

# Absolute floor for a disk the wizard offers (the smallest desktop needs more: see minimum_disk_bytes).
MIN_DISK_BYTES = 16 * 1024**3
# Filesystem signatures of a disk (or partition) that belongs to a volume group or a RAID set.
MEMBER_SIGNATURES = {
    "LVM2_member": "an LVM volume group",
    "linux_raid_member": "a software RAID array",
    "isw_raid_member": "an Intel firmware RAID set",
    "ddf_raid_member": "a firmware RAID set",
}
SWAP_MOUNT = "[SWAP]"
DEFAULT_MIRROR_HOST = "distfiles.gentoo.org"
BY_PATH_DIR = Path("/dev/disk/by-path")
# Seconds of clock error that are fine / worth a warning (beyond that TLS and
# GPG verification of the stage3 start to fail).
CLOCK_OK_SECONDS = 120.0
CLOCK_WARN_SECONDS = 24 * 3600.0

# What the daemon's preflight needs (gentoo_installerd.inspect.REQUIRED_TOOLS)
# plus the tools the GUI always relied on. Used only when the daemon cannot be
# asked; a test keeps this a superset of the daemon list.
REQUIRED_TOOLS = (
    "lsblk", "findmnt", "blkid", "sgdisk", "parted", "partprobe", "udevadm", "wipefs", "mkfs.ext4",
    "mkswap", "mount", "umount", "tar", "gpg", "gpgv", "chroot", "grub-install",
)

ESP_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
BIOS_BOOT_GUID = "21686148-6449-6e6f-744e-656564454649"
SWAP_GUID = "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f"
WINDOWS_GUIDS = {
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7",  # Microsoft basic data
    "e3c9e316-0b5c-4db8-817d-f92df00215ae",  # Microsoft reserved
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac",  # Windows recovery environment
}
PARTTYPE_NAMES = {
    ESP_GUID: "EFI System",
    BIOS_BOOT_GUID: "BIOS boot",
    SWAP_GUID: "Linux swap",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Linux filesystem",
    "933ac7e1-2eb4-4f13-b844-0e14e2aef915": "Linux home",
    "4f68bce3-e8cd-4db1-96e7-fbcaf984b709": "Linux root",
    "a19d880f-05fc-4d3b-a006-743f0f84911e": "Linux RAID",
    "e6d6d379-f507-44c2-a23c-238f2a3df928": "Linux LVM",
    "ca7d7ccb-63ed-4c53-861c-1742536059cc": "Linux LUKS",
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Microsoft basic data",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "Microsoft reserved",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Windows recovery",
}
EFI_LABELS = {"efi", "esp", "efi system partition", "efi system"}


def smallest_disk_bytes(manifest: Any) -> int:
    """Smallest disk the installer can use at all: the minimal system (no desktop) with a bare guided layout."""
    storage = manifest.storage
    boot = layout.ESP_MIB if storage.firmware == "uefi" else (layout.BIOS_BOOT_MIB if storage.table == "gpt" else 0)
    extra = 0 if manifest.portage.binhost else layout.SOURCE_BUILD_EXTRA_MIB["minimal"]
    return (layout.MIN_ROOT_MIB["minimal"] + extra + boot + layout.RESERVED_MIB) * layout.MIB


def desktop_disk_bytes(manifest: Any) -> int:
    """Smallest disk for the chosen desktop with a bare guided layout (no swap, no separate /home)."""
    storage = manifest.storage
    boot = layout.ESP_MIB if storage.firmware == "uefi" else (layout.BIOS_BOOT_MIB if storage.table == "gpt" else 0)
    return (layout.minimum_root_mib(manifest) + boot + layout.RESERVED_MIB) * layout.MIB


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


def _tail(value: str, keep: int = 6) -> str:
    return value if len(value) <= keep else "…" + value[-keep:]


@dataclass
class PartitionInfo:
    """A partition or stacked volume (LUKS mapping, LVM volume, md array) on a disk."""

    path: str
    size: int = 0
    fstype: str = ""
    label: str = ""
    partlabel: str = ""
    parttype: str = ""
    uuid: str = ""
    partuuid: str = ""
    mountpoints: tuple[str, ...] = ()
    type: str = "part"
    parent: str = ""  # path of the whole disk this node was found under

    @property
    def size_text(self) -> str:
        return format_size(self.size)

    @property
    def real_mountpoints(self) -> tuple[str, ...]:
        """Where the partition is mounted; active swap is not a mount (the daemon turns it off on request)."""
        return tuple(point for point in self.mountpoints if point != SWAP_MOUNT)

    @property
    def mounted(self) -> bool:
        return bool(self.real_mountpoints)

    @property
    def swap_active(self) -> bool:
        return SWAP_MOUNT in self.mountpoints

    @property
    def member_of(self) -> str:
        """'an LVM volume group' when the partition belongs to one ('' otherwise)."""
        return MEMBER_SIGNATURES.get(self.fstype, "")

    @property
    def is_esp(self) -> bool:
        if self.parttype.lower() in {ESP_GUID, "0xef"}:
            return True
        return self.fstype in {"vfat", "fat32", "fat16"} and (self.partlabel or self.label).lower() in EFI_LABELS

    @property
    def is_bios_boot(self) -> bool:
        return self.parttype.lower() == BIOS_BOOT_GUID

    @property
    def is_swap(self) -> bool:
        return self.fstype == "swap" or self.parttype.lower() in {SWAP_GUID, "0x82"}

    @property
    def is_windows(self) -> bool:
        return self.fstype in {"ntfs", "bitlocker"} or self.parttype.lower() in WINDOWS_GUIDS

    @property
    def parttype_name(self) -> str:
        return PARTTYPE_NAMES.get(self.parttype.lower(), "")

    def describe(self) -> str:
        """'ntfs 450.0 GiB “Windows”' for the review and the disk cards."""
        name = self.label or self.partlabel
        text = f"{self.fstype or self.parttype_name or 'unformatted'} {self.size_text}"
        return f"{text} “{name}”" if name else text

    def choice_text(self) -> str:
        """One line for a partition picker."""
        pieces = [self.path, self.size_text, self.fstype or "unformatted"]
        if self.label or self.partlabel:
            pieces.append(f"“{self.label or self.partlabel}”")
        if self.parttype_name and self.parttype_name != "Linux filesystem":
            pieces.append(self.parttype_name)
        return " · ".join(pieces)


@dataclass
class DiskInfo:
    path: str
    model: str
    size: int
    serial: str = ""
    wwid: str = ""
    removable: bool = False
    read_only: bool = False
    mounted: bool = False
    partitions: list[PartitionInfo] = field(default_factory=list)
    transport: str = ""
    by_path: str = ""  # name of the /dev/disk/by-path link, "" when there is none
    live_medium: bool = False
    model_raw: str | None = None  # lsblk's MODEL ("" when empty); None = same as model
    pttype: str = ""  # "gpt" | "dos" | ""
    rotational: bool | None = None
    fstype: str = ""  # a filesystem directly on the whole disk
    active_volumes: list[str] = field(default_factory=list)
    log_sec: int = 512  # logical sector size (the DOS table limit depends on it)
    problem: str = ""  # a key of blockdev.TARGET_PROBLEMS when the disk cannot be a target
    swaps: list[str] = field(default_factory=list)  # devices on this disk that are in use as swap
    signatures: list[str] = field(default_factory=list)  # LVM / RAID member signatures on the disk or its partitions
    identity_shared: bool = False  # another disk reports the same serial number or WWN

    @property
    def size_text(self) -> str:
        return format_size(self.size)

    @property
    def has_identity(self) -> bool:
        """A serial number or WWN that identifies this disk alone."""
        return bool(self.serial or self.wwid) and not self.identity_shared

    @property
    def unavailable_reason(self) -> str:
        """Why this disk cannot be chosen, as a sentence ('' when it can)."""
        if self.live_medium:
            return "This disk holds the live installer medium this system was started from."
        if self.read_only:
            return "This disk is read-only (a write-protect switch or a broken drive)."
        if self.problem:
            return f"This device {blockdev.TARGET_PROBLEMS.get(self.problem, 'cannot be used')}."
        return ""

    @property
    def holders(self) -> list[str]:
        """What is alive on the disk and would have to be switched off: volumes, swap, volume-group and RAID membership."""
        found = [f"active volume {volume}" for volume in self.active_volumes]
        found += [f"swap in use on {path}" for path in self.swaps]
        found += [f"{signature}" for signature in self.member_text]
        return found

    @property
    def member_text(self) -> list[str]:
        """'part of an LVM volume group' style statements for every member signature."""
        return [f"part of {MEMBER_SIGNATURES[name]}" for name in dict.fromkeys(self.signatures) if name in MEMBER_SIGNATURES]

    @property
    def stable_id(self) -> str:
        """The value stored in Storage.device_id for a serial/WWN identity."""
        return self.wwid or self.serial

    @property
    def selectable(self) -> bool:
        return not self.live_medium and not self.read_only and not self.problem and self.size > 0

    @property
    def identity_text(self) -> str:
        if self.identity_shared:
            return "serial/WWN shared with another disk"
        if self.serial:
            return f"serial {_tail(self.serial)}"
        if self.wwid:
            return f"WWN {_tail(self.wwid)}"
        return "no serial/WWN"

    @property
    def bus_text(self) -> str:
        names = {"nvme": "NVMe", "sata": "SATA", "ata": "SATA", "usb": "USB", "sas": "SAS", "scsi": "SCSI", "virtio": "virtio"}
        return names.get(self.transport.lower(), self.transport.upper())

    @property
    def contents_text(self) -> str:
        """What is on the disk now, for the cards, the review and the erase dialog."""
        parts = [part for part in self.partitions if part.type == "part"]
        if not parts:
            if self.fstype:
                return f"whole disk holds {self.fstype}"
            return "no partitions" if self.pttype else "blank (no partition table)"
        shown = ", ".join(part.describe() for part in parts[:4])
        more = f" and {len(parts) - 4} more" if len(parts) > 4 else ""
        noun = "partition" if len(parts) == 1 else "partitions"
        return f"{len(parts)} {noun}: {shown}{more}"

    def path_identity(self) -> str:
        """Storage.device_id for the 'path' identity kind ("" when the disk has no by-path link)."""
        if not self.by_path:
            return ""
        node = blockdev.BlockDevice(
            name=os.path.basename(self.path), path=self.path, type="disk", size=self.size,
            model=self.model if self.model_raw is None else self.model_raw,
        )
        return blockdev.path_identity(node, self.by_path).value

    def describe(self) -> str:
        """'/dev/sda · Samsung SSD 870 · 500.0 GiB · serial …123456'."""
        return f"{self.path} · {self.model} · {self.size_text} · {self.identity_text}"


@dataclass(frozen=True)
class ExcludedDevice:
    """A device that was found but cannot receive an installation (shown in the empty state of the disk list)."""

    path: str
    model: str
    size: int
    problem: str  # a key of blockdev.TARGET_PROBLEMS

    @property
    def reason(self) -> str:
        return blockdev.TARGET_PROBLEMS.get(self.problem, "cannot be used")


@dataclass
class SystemInfo:
    architecture: str
    memory_gib: float
    uefi: bool
    disks: list[DiskInfo]
    network: bool | None  # None while the check is still running
    synchronized_clock: bool
    missing_tools: list[str]
    inspected: bool = True
    source: str = "local"  # "pending" | "local" | "daemon"
    errors: list[str] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)  # hardware findings from the daemon
    hardware: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, bool] = field(default_factory=dict)
    clock_skew: float | None = None  # mirror time minus local time in seconds (> 0: this clock is behind)
    tls_ok: bool | None = None
    network_error: str = ""
    daemon: DaemonState | None = None
    missing_for_selection: list[str] = field(default_factory=list)
    roots: list[blockdev.BlockDevice] = field(default_factory=list)  # the lsblk tree behind `disks`
    excluded: list[ExcludedDevice] = field(default_factory=list)  # real devices that cannot be a target, with the reason

    @classmethod
    def pending(cls) -> SystemInfo:
        """Placeholder shown while the real inspection runs in the background.
        Only instant facts (architecture, RAM, firmware) are filled in."""
        return cls(
            architecture=platform.machine(), memory_gib=_memory_gib(), uefi=_is_uefi(), disks=[],
            network=None, synchronized_clock=False, missing_tools=[], inspected=False, source="pending",
        )

    @property
    def usable_disks(self) -> list[DiskInfo]:
        return [disk for disk in self.disks if disk.selectable and disk.size >= MIN_DISK_BYTES]

    @property
    def profile(self) -> HardwareProfile:
        """The daemon's hardware profile as an object (an empty one when nothing was detected)."""
        return profile_of(self.hardware)

    def block_devices(self) -> list[blockdev.BlockDevice]:
        """The lsblk tree for layout.plan_storage: the real one, or one rebuilt from `disks`."""
        return self.roots or [device_from_disk(disk) for disk in self.disks]

    def find_disk(self, path: str) -> DiskInfo | None:
        wanted = os.path.realpath(path)
        return next((disk for disk in self.disks if os.path.realpath(disk.path) == wanted), None)

    def find_partition(self, path: str) -> PartitionInfo | None:
        wanted = os.path.realpath(path)
        for disk in self.disks:
            for partition in disk.partitions:
                if os.path.realpath(partition.path) == wanted:
                    return partition
        return None

    def clock_status(self) -> tuple[str, str]:
        """(ok|warning|error|unknown, message) for the System clock row."""
        if self.tls_ok is False and self.clock_skew is None:
            return "error", "The mirror's certificate was rejected, which usually means the clock is wrong. Set the date and time, then check again."
        if self.clock_skew is None:
            if self.network is False:
                return "unknown", "Cannot compare the clock with the network while offline."
            return ("ok", "Synchronized") if self.synchronized_clock else ("unknown", "Not compared with the network yet.")
        skew = abs(self.clock_skew)
        direction = "behind" if self.clock_skew > 0 else "ahead of"
        if skew <= CLOCK_OK_SECONDS:
            return "ok", "The clock matches the network time."
        if skew <= CLOCK_WARN_SECONDS and self.tls_ok is not False:
            return "warning", f"The clock is {_duration(skew)} {direction} the network time. Set it before installing if downloads fail verification."
        return "error", (
            f"The clock is {_duration(skew)} {direction} the network time, so downloads cannot be verified. "
            "Set the date and time (for example with: sudo date -s '2026-01-31 12:00', or enable network time), then check again."
        )


def _duration(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds < 90:
        return f"{int(seconds)} seconds"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} minutes"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} hours"
    return f"{round(seconds / 86400)} days"


# --- instant facts -----------------------------------------------------------


def _memory_gib() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 / 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def _is_uefi() -> bool:
    return Path("/sys/firmware/efi").exists()


# --- disks --------------------------------------------------------------------


def _lsblk_roots() -> list[blockdev.BlockDevice]:
    try:
        result = subprocess.run(list(blockdev.lsblk_argv()), capture_output=True, text=True, check=True, timeout=10)
        return blockdev.parse_lsblk(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def _mount_sources() -> list[str]:
    """Block devices the live system itself was started from."""
    sources: list[str] = []
    for target in ("/run/initramfs/live", "/run/initramfs/isoscan"):
        try:
            result = subprocess.run(
                ["findmnt", "--noheadings", "--output", "SOURCE", "--target", target],
                capture_output=True, text=True, check=False, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for line in (result.stdout or "").splitlines():
            line = line.strip().split("[", 1)[0]
            if line.startswith("/dev/") and line not in sources:
                sources.append(line)
    return sources


def _kernel_cmdline() -> str:
    try:
        return Path("/proc/cmdline").read_text()
    except OSError:
        return ""


def by_path_names(directory: Path | None = None) -> dict[str, str]:
    """Real path of every whole disk -> name of its (alphabetically first)
    /dev/disk/by-path link. Partition links ('-part') are skipped."""
    directory = directory or BY_PATH_DIR
    names: dict[str, str] = {}
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return names
    for name in entries:
        if "-part" in name:
            continue
        names.setdefault(os.path.realpath(directory / name), name)
    return names


def _partition_info(node: blockdev.BlockDevice, parent: str) -> PartitionInfo:
    return PartitionInfo(
        path=node.path, size=node.size, fstype=node.fstype, label=node.label, partlabel=node.partlabel,
        parttype=node.parttype, uuid=node.uuid, partuuid=node.partuuid, mountpoints=node.mountpoints,
        type=node.type, parent=parent,
    )


# Problems that make a device uninteresting: it is not a disk the user could mean (RAM disks, loop devices, a CD drive,
# hidden eMMC areas, an empty card-reader slot). The others (live medium, read-only) are shown as disabled cards.
HIDDEN_PROBLEMS = {"virtual", "firmware_area", "not_a_disk", "empty"}


def _sector_size(node: blockdev.BlockDevice) -> int:
    return node.log_sec or 512


def _descendants(node: blockdev.BlockDevice) -> list[blockdev.BlockDevice]:
    """Every device below a disk once (md arrays and LVs repeat under each parent)."""
    return list({child.key: child for child in node.walk()[1:]}.values())


def _non_swap_mount(node: blockdev.BlockDevice) -> bool:
    return any(point != SWAP_MOUNT for sub in node.walk() for point in sub.mountpoints)


def disks_from_roots(roots: list[blockdev.BlockDevice], *, live_keys: set[str] | None = None, by_path: dict[str, str] | None = None,
                     excluded: list[ExcludedDevice] | None = None) -> list[DiskInfo]:
    live_keys = live_keys or set()
    by_path = by_path or {}
    disks: list[DiskInfo] = []
    for node in roots:
        problem = blockdev.install_target_problem(node, live_keys=live_keys)
        if problem in HIDDEN_PROBLEMS:
            if excluded is not None and problem == "empty" and node.type == "disk":
                excluded.append(ExcludedDevice(node.path, node.model or "Block device", node.size, problem))
            continue
        descendants = _descendants(node)
        partitions = [_partition_info(child, node.path) for child in descendants]
        volumes = [f"{child.type} {child.path}" for child in descendants if child.type not in blockdev.STRUCTURAL_TYPES]
        signatures = [sub.fstype for sub in (node, *descendants) if sub.fstype in MEMBER_SIGNATURES]
        disks.append(DiskInfo(
            path=node.path,
            model=node.model or "Block device",
            size=node.size,
            serial=node.serial,
            wwid=node.wwn,
            removable=node.removable or node.transport == "usb",
            read_only=node.ro,
            mounted=_non_swap_mount(node),
            partitions=partitions,
            transport=node.transport,
            by_path=by_path.get(os.path.realpath(node.path), ""),
            live_medium=node.key in live_keys,
            model_raw=node.model,
            pttype=node.pttype,
            fstype=node.fstype,
            active_volumes=volumes,
            log_sec=_sector_size(node),
            problem=problem if problem not in {"live_medium", "read_only"} else "",
            swaps=blockdev.active_swaps(node),
            signatures=signatures,
        ))
    mark_shared_identities(disks)
    return disks


def mark_shared_identities(disks: list[DiskInfo]) -> None:
    """Two disks that report the same serial number or WWN cannot be told apart by it: both need the by-path approval."""
    seen: dict[str, int] = {}
    for disk in disks:
        for value in {disk.serial, disk.wwid} - {""}:
            seen[value] = seen.get(value, 0) + 1
    for disk in disks:
        disk.identity_shared = any(seen.get(value, 0) > 1 for value in (disk.serial, disk.wwid) if value)


def device_from_disk(disk: DiskInfo) -> blockdev.BlockDevice:
    """A BlockDevice tree for a DiskInfo that did not come from lsblk (tests, older callers)."""
    children = tuple(
        blockdev.BlockDevice(
            name=os.path.basename(part.path), path=part.path, type=part.type, size=part.size, fstype=part.fstype, label=part.label,
            partlabel=part.partlabel, parttype=part.parttype, uuid=part.uuid, partuuid=part.partuuid, mountpoints=part.mountpoints,
        )
        for part in disk.partitions
    )
    return blockdev.BlockDevice(
        name=os.path.basename(disk.path), path=disk.path, type="disk", size=disk.size, ro=disk.read_only, removable=disk.removable,
        transport=disk.transport, model=disk.model if disk.model_raw is None else disk.model_raw, serial=disk.serial, wwn=disk.wwid,
        fstype=disk.fstype, pttype=disk.pttype, log_sec=disk.log_sec, children=children,
    )


def profile_of(hardware: dict[str, Any] | None) -> HardwareProfile:
    """The hardware profile of an inspect reply; unknown keys are ignored and missing ones keep their defaults."""
    return HardwareProfile.from_dict(hardware if isinstance(hardware, dict) else None)


def discover(roots: list[blockdev.BlockDevice] | None = None) -> tuple[list[blockdev.BlockDevice], list[DiskInfo], list[ExcludedDevice]]:
    """(lsblk tree, offered disks, devices that were found but cannot be used)."""
    roots = _lsblk_roots() if roots is None else roots
    if not roots:
        return [], [], []
    try:
        live_keys = blockdev.live_medium_keys(roots, sources=_mount_sources(), cmdline=_kernel_cmdline())
    except (OSError, ValueError):
        live_keys = set()
    excluded: list[ExcludedDevice] = []
    return roots, disks_from_roots(roots, live_keys=live_keys, by_path=by_path_names(), excluded=excluded), excluded


def discover_disks() -> list[DiskInfo]:
    return discover()[1]


# --- network and clock -----------------------------------------------------------


@dataclass(frozen=True)
class MirrorProbe:
    reachable: bool
    tls_ok: bool | None = None
    skew_seconds: float | None = None
    error: str = ""


T = TypeVar("T")


def run_with_deadline(function: Callable[[], T], seconds: float, default: T) -> T:
    """Run function in a daemon thread and give up after `seconds`.

    getaddrinfo() and friends ignore socket timeouts; a hung resolver must not
    freeze the readiness check or keep the process alive at exit."""
    box: list[T] = []

    def target() -> None:
        try:
            box.append(function())
        except Exception:  # noqa: BLE001 - a failing probe is just "unknown"
            pass

    thread = threading.Thread(target=target, name="gentoo-installer-probe", daemon=True)
    thread.start()
    thread.join(seconds)
    return box[0] if box else default


def _proxy_parts(proxy: str) -> tuple[str, int, dict[str, str]] | None:
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}") if proxy else None
    if not parsed or not parsed.hostname or parsed.scheme not in {"http", "https", ""}:
        return None
    headers: dict[str, str] = {}
    if parsed.username:
        token = b64encode(f"{parsed.username}:{parsed.password or ''}".encode()).decode()
        headers["Proxy-Authorization"] = f"Basic {token}"
    return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 3128), headers


def _head_date(host: str, proxy: str, timeout: float, context: ssl.SSLContext) -> float:
    """HEAD https://host/ and return (server time - local time) in seconds."""
    proxy_parts = _proxy_parts(proxy)
    if proxy_parts:
        connection = http.client.HTTPSConnection(proxy_parts[0], proxy_parts[1], timeout=timeout, context=context)
        connection.set_tunnel(host, 443, headers=proxy_parts[2])
    else:
        connection = http.client.HTTPSConnection(host, 443, timeout=timeout, context=context)
    try:
        started = time.time()
        connection.request("HEAD", "/", headers={"User-Agent": "gentoo-live-installer", "Connection": "close"})
        response = connection.getresponse()
        finished = time.time()
        header = response.getheader("Date")
        if not header:
            raise OSError("The mirror did not send a Date header")
        server = email.utils.parsedate_to_datetime(header).timestamp()
        return server - (started + finished) / 2
    finally:
        connection.close()


def probe_mirror(host: str = DEFAULT_MIRROR_HOST, proxy: str = "", timeout: float = 4.0, deadline: float = 8.0) -> MirrorProbe:
    """Reach the mirror over HTTPS (through the proxy when given) and compare
    its Date header with the local clock. Bounded by `deadline` seconds."""

    def probe() -> MirrorProbe:
        try:
            skew = _head_date(host, proxy, timeout, ssl.create_default_context())
            return MirrorProbe(True, True, skew)
        except ssl.SSLCertVerificationError as exc:
            message = str(getattr(exc, "verify_message", "") or exc)
            if "not yet valid" in message or "expired" in message:
                try:  # read the Date header only, to tell the user how far off the clock is
                    skew = _head_date(host, proxy, timeout, ssl._create_unverified_context())  # noqa: S323
                    return MirrorProbe(True, False, skew, f"The mirror's certificate is {message}: the system clock is wrong.")
                except (OSError, ssl.SSLError, ValueError, http.client.HTTPException):
                    pass
                return MirrorProbe(True, False, None, f"The mirror's certificate is {message}: the system clock is wrong.")
            return MirrorProbe(True, False, None, f"The mirror's certificate could not be verified: {message}")
        except (OSError, ssl.SSLError, ValueError, http.client.HTTPException) as exc:
            return MirrorProbe(False, None, None, str(exc))

    return run_with_deadline(probe, deadline, MirrorProbe(False, None, None, "The network check timed out"))


PROBE_TARGET: dict[str, str] = {"host": DEFAULT_MIRROR_HOST, "proxy": ""}


def set_probe_target(mirror_url: str, proxy: str = "") -> None:
    """Tell inspect_system() which mirror (and proxy) the install will use."""
    host = urlparse(mirror_url).hostname if mirror_url else None
    PROBE_TARGET["host"] = host or DEFAULT_MIRROR_HOST
    PROBE_TARGET["proxy"] = proxy.strip()


def check_network(host: str | None = None, proxy: str | None = None) -> bool:
    return probe_mirror(host or PROBE_TARGET["host"], PROBE_TARGET["proxy"] if proxy is None else proxy).reachable


# --- whole-system inspection -----------------------------------------------------


def inspect_fast() -> SystemInfo:
    """Architecture, RAM, firmware, disks and tools: no network."""
    tools = {tool: shutil.which(tool) is not None for tool in REQUIRED_TOOLS}
    roots, disks, excluded = discover()
    return SystemInfo(
        architecture=platform.machine(),
        memory_gib=_memory_gib(),
        uefi=_is_uefi(),
        disks=disks,
        network=None,
        synchronized_clock=False,
        missing_tools=[tool for tool, present in tools.items() if not present],
        tools=tools,
        roots=roots,
        excluded=excluded,
    )


def inspect_system(with_network: bool = True) -> SystemInfo:
    info = inspect_fast()
    if with_network:
        apply_probe(info, probe_mirror(PROBE_TARGET["host"], PROBE_TARGET["proxy"]))
    else:
        info.network = False
    return info


def apply_probe(info: SystemInfo, probe: MirrorProbe) -> None:
    info.network = probe.reachable
    info.network_error = probe.error
    info.tls_ok = probe.tls_ok
    info.clock_skew = probe.skew_seconds
    info.synchronized_clock = probe.skew_seconds is not None and abs(probe.skew_seconds) <= CLOCK_OK_SECONDS


def tools_for_selection(manifest: Any) -> list[str]:
    """Tools the chosen configuration needs beyond the always-required ones
    (the same rules as the daemon's preflight)."""
    storage, system = manifest.storage, manifest.system
    needed: set[str] = set()
    if storage.firmware == "uefi":
        needed.add("mkfs.vfat")
    if storage.filesystem in {"xfs", "btrfs"}:
        needed.add("mkfs." + storage.filesystem)
    for partition in storage.partitions:
        if partition.format and partition.filesystem in {"ext4", "xfs", "btrfs", "vfat"}:
            needed.add("mkfs." + partition.filesystem)
    if storage.swap_mib or any(p.filesystem == "swap" for p in storage.partitions):
        needed.update(("swapon", "swapoff"))
    if storage.luks:
        needed.add("cryptsetup")
    if storage.lvm:
        needed.update(("pvcreate", "vgcreate", "lvcreate", "pvs", "vgchange"))
    if storage.raid_level:
        needed.add("mdadm")
    if system.secure_boot:
        needed.update(("sbsign", "sbverify"))
    if system.bootloader == "efi-stub":
        needed.add("efibootmgr")
    return sorted(needed)


def missing_for_selection(manifest: Any, tools: dict[str, bool] | None = None) -> list[str]:
    tools = tools or {}
    return [tool for tool in tools_for_selection(manifest) if tools.get(tool) is False or (tool not in tools and shutil.which(tool) is None)]


def roots_from_inspection(raw_disks: list[Any]) -> list[blockdev.BlockDevice]:
    """The lsblk tree from the daemon's `inspect` reply (lsblk-shaped dictionaries)."""
    roots = []
    for item in raw_disks:
        if isinstance(item, dict):
            try:
                roots.append(blockdev.BlockDevice.from_lsblk(item))
            except (TypeError, ValueError):
                continue
    return roots


def disks_from_inspection(raw_disks: list[Any]) -> list[DiskInfo]:
    """Disk list from the daemon's `inspect` reply (lsblk-shaped dictionaries)."""
    return disks_from_roots(roots_from_inspection(raw_disks), by_path=by_path_names())


def merge_inspection(info: SystemInfo, data: dict[str, Any], state: DaemonState | None = None) -> SystemInfo:
    """Overlay the daemon's `inspect` reply on a locally gathered SystemInfo.

    The daemon is authoritative for what the installer will see in its
    preflight (tools, network, firmware, hardware findings). Disks stay local
    unless the local scan found none (lsblk is readable without root)."""
    if not isinstance(data, dict):
        return info
    info.source = "daemon"
    info.daemon = state
    if isinstance(data.get("architecture"), str):
        info.architecture = data["architecture"]
    memory = data.get("memory_bytes")
    if isinstance(memory, (int, float)) and memory > 0:
        info.memory_gib = memory / 1024**3
    if isinstance(data.get("uefi"), bool):
        info.uefi = data["uefi"]
    if isinstance(data.get("network"), bool):
        info.network = data["network"]
    tools = data.get("tools")
    if isinstance(tools, dict):
        info.tools = {str(name): bool(present) for name, present in tools.items()}
        info.missing_tools = [tool for tool in REQUIRED_TOOLS if info.tools.get(tool) is False]
    if isinstance(data.get("errors"), list):
        info.errors = [str(item) for item in data["errors"]]
    if isinstance(data.get("warnings"), list):
        info.warnings = [item for item in data["warnings"] if isinstance(item, dict)]
    if isinstance(data.get("hardware"), dict):
        info.hardware = data["hardware"]
    if isinstance(data.get("missing_for_selection"), list):
        info.missing_for_selection = [str(item) for item in data["missing_for_selection"]]
    clock = data.get("clock")
    if isinstance(clock, dict):
        skew = clock.get("skew_seconds")
        if isinstance(skew, (int, float)):
            info.clock_skew = float(skew)
            info.synchronized_clock = abs(skew) <= CLOCK_OK_SECONDS
        if isinstance(clock.get("tls_ok"), bool):
            info.tls_ok = clock["tls_ok"]
    elif isinstance(info.hardware.get("clock"), dict) and isinstance(info.hardware["clock"].get("ntp_synced"), bool):
        info.synchronized_clock = info.hardware["clock"]["ntp_synced"]
    if not info.disks and isinstance(data.get("disks"), list):
        info.roots = roots_from_inspection(data["disks"])
        info.disks = disks_from_roots(info.roots, by_path=by_path_names())
    apply_daemon_disk_facts(info, data)
    info.inspected = True
    return info


def apply_daemon_disk_facts(info: SystemInfo, data: dict[str, Any]) -> None:
    """The daemon sees the disks as root and decides the by-path identity it will
    later verify, so its answer wins over the GUI's own guess."""
    by_path: dict[str, str] = {}
    live: set[str] = set()
    sources: list[Any] = [data.get("disks")]
    hardware = data.get("hardware")
    if isinstance(hardware, dict):
        sources.append(hardware.get("disks"))
    for source in sources:
        for item in source if isinstance(source, list) else []:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            path = os.path.realpath(str(item["path"]))
            if isinstance(item.get("by_path"), str) and item["by_path"]:
                by_path[path] = item["by_path"]
            if item.get("live_medium") is True:
                live.add(path)
    for disk in info.disks:
        path = os.path.realpath(disk.path)
        if path in by_path:
            disk.by_path = by_path[path]
        if path in live:
            disk.live_medium = True


def inspect_with_daemon(
    socket_path: str,
    *,
    local: Callable[[], SystemInfo] | None = None,
    dev_mode: bool | None = None,
    request_payload: dict[str, Any] | None = None,
) -> SystemInfo:
    """The GUI's inspection: local facts, then the daemon's `inspect` on top.

    Without a reachable daemon the local result is returned with `daemon` set
    to the failure (the Readiness page explains it). GENTOO_INSTALLER_DEV=1
    skips the daemon altogether (development outside the live image)."""
    from . import daemon_client  # late: keeps this module importable without sockets

    info = (local or inspect_system)()
    if dev_mode is None:
        dev_mode = os.environ.get("GENTOO_INSTALLER_DEV") == "1"
    if dev_mode:
        info.daemon = DaemonState("dev", "Development mode: the installer service is not used.")
        info.inspected = True
        return info
    state = daemon_client.probe(socket_path)
    info.daemon = state
    if state.reachable:
        # A daemon that does not know the optional manifest (or rejects it) is asked again without it.
        for payload in ([request_payload, None] if request_payload else [None]):
            try:
                events = daemon_client.request(socket_path, "inspect", payload, timeout=15.0, max_seconds=40.0)
            except OSError:
                break
            reply = next((e["data"] for e in events if e.get("type") == "status" and isinstance(e.get("data"), dict) and "architecture" in e["data"]), None)
            if reply is not None:
                merge_inspection(info, reply, state)
                break
    info.inspected = True
    return info
