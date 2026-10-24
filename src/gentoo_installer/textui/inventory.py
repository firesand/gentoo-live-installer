"""The daemon's ``inspect`` reply, in the shapes the console installer works with.

The reply (docs/protocol.md, contract I2) holds the architecture, memory, firmware, network and
tool facts, the disks as ``BlockDevice.to_dict()`` dictionaries, the hardware profile
(``data.hardware``) and the findings (``data.warnings``). Everything here is a pure function of
that dictionary, so it works the same on a reply from the daemon, from a test or from the local
fallback of ``Host.local_inspection``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from .. import blockdev, system_info
from ..hardware import HardwareProfile
from ..manifest import InstallManifest, Storage

# Reasons (blockdev.TARGET_PROBLEMS keys) for which a device is not worth listing at all.
HIDDEN_PROBLEMS = frozenset({"not_a_disk", "virtual", "firmware_area", "empty"})
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
# BlockDevice.to_dict() uses field names, lsblk uses these keys; BlockDevice.from_lsblk only reads the lsblk ones.
DICT_TO_LSBLK = {"maj_min": "maj:min", "removable": "rm", "transport": "tran", "log_sec": "log-sec", "phy_sec": "phy-sec"}


@dataclass(frozen=True)
class Finding:
    """One thing the hardware probe or the preflight noticed."""

    code: str
    severity: str  # error | warning | info
    message: str
    action: str = ""  # what the graphical installer would offer: sync_time, set_time, reboot_firmware ...

    @property
    def short_code(self) -> str:
        return self.code[3:] if self.code.startswith("hw.") else self.code


def findings_from(raw: Any) -> list[Finding]:
    """Findings from ``data.warnings``; plain strings (older daemons) become warnings."""
    found: list[Finding] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            found.append(Finding("inspect.warning", "warning", item))
        elif isinstance(item, Mapping) and item.get("message"):
            severity = str(item.get("severity") or "warning")
            found.append(Finding(str(item.get("code") or ""), severity if severity in SEVERITY_ORDER else "warning",
                                 str(item["message"]), str(item.get("action") or "")))
    return sorted(found, key=lambda finding: SEVERITY_ORDER[finding.severity])


def lsblk_shape(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A device dictionary with the keys ``BlockDevice.from_lsblk`` reads (``to_dict`` output is renamed, lsblk output is kept)."""
    shaped = {DICT_TO_LSBLK.get(key, key): value for key, value in raw.items() if key != "children"}
    shaped["children"] = [lsblk_shape(child) for child in raw.get("children") or [] if isinstance(child, Mapping)]
    return shaped


def parse_disks(raw: Any) -> list[blockdev.BlockDevice]:
    roots: list[blockdev.BlockDevice] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, Mapping):
            try:
                roots.append(blockdev.BlockDevice.from_lsblk(lsblk_shape(item)))
            except (TypeError, ValueError):
                continue
    return roots


@dataclass(frozen=True)
class DiskEntry:
    """A disk the user may see in the list; ``problem`` says why it cannot be chosen ("" when it can)."""

    disk: blockdev.BlockDevice
    problem: str = ""
    by_path: str = ""
    shared_identity: bool = False  # another disk reports the same serial number or WWN (cheap USB adapters do)
    pool_reasons: tuple[str, ...] = ()  # Btrfs or ZFS pools with a member on this disk and on another one (that takes every disk of the reply to find out)

    @property
    def usable(self) -> bool:
        return not self.problem

    @property
    def has_identity(self) -> bool:
        """Whether a serial number or WWN tells this disk from every other one."""
        return bool(self.disk.serial or self.disk.wwn) and not self.shared_identity

    @property
    def holders(self) -> list[str]:
        """What is alive on the disk: volumes, swap, a volume group, RAID set or Btrfs/ZFS pool it belongs to (the list of the graphical installer)."""
        return list(dict.fromkeys([*system_info.holder_reasons_of(self.disk), *self.pool_reasons]))

    @property
    def mounts(self) -> list[str]:
        """Where the disk or its partitions are mounted. Active swap is not a mount: the service turns it off when it may."""
        return sorted({mount for node in self.disk.walk() for mount in node.mountpoints if mount != system_info.SWAP_MOUNT})


class MachineView:
    """One consistent picture of this computer, taken from an ``inspect`` reply."""

    def __init__(self, reply: Mapping[str, Any] | None = None, *, local_by_path: Mapping[str, str] | None = None) -> None:
        self.reply: dict[str, Any] = dict(reply or {})
        hardware = self.reply.get("hardware")
        self.hardware: dict[str, Any] = dict(hardware) if isinstance(hardware, Mapping) else {}
        self.profile = HardwareProfile.from_dict(self.hardware)
        warnings = self.reply.get("warnings")
        self.findings = findings_from(warnings if warnings is not None else self.hardware.get("warnings"))
        self.roots = parse_disks(self.reply.get("disks"))
        self._local_by_path = {os.path.realpath(path): name for path, name in (local_by_path or {}).items()}
        self._by_path = self._collect_by_path()
        self.live_keys = self._live_keys()
        reported = [value for node in self.roots for value in {node.serial, node.wwn} if value]
        self._shared = {value for value in reported if reported.count(value) > 1}

    # --- plain facts ---------------------------------------------------------------------

    @property
    def known(self) -> bool:
        """Whether the reply says anything at all (an empty view means nothing could be probed)."""
        return bool(self.reply)

    @property
    def memory_gib(self) -> float:
        """Installed memory as the probe measured it (a few percent below what was fitted); 0 when the reply does not say."""
        value = self.reply.get("memory_bytes")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return value / 1024**3
        return self.profile.memory.total_kib / 1024**2

    @property
    def network(self) -> bool | None:
        value = self.reply.get("network")
        return value if isinstance(value, bool) else None

    @property
    def uefi(self) -> bool | None:
        value = self.reply.get("uefi")
        if isinstance(value, bool):
            return value
        return self.profile.firmware.mode == "uefi" if self.hardware else None

    @property
    def tools(self) -> dict[str, bool]:
        tools = self.reply.get("tools")
        return {str(name): bool(present) for name, present in tools.items()} if isinstance(tools, Mapping) else {}

    @property
    def errors(self) -> list[str]:
        errors = self.reply.get("errors")
        return [str(item) for item in errors] if isinstance(errors, list) else []

    @property
    def blocking(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "error"]

    def missing_tools(self, manifest: InstallManifest) -> list[str]:
        """Tools this configuration needs that the daemon reported as missing."""
        tools = self.tools
        return sorted(tool for tool in system_info.tools_for_selection(manifest) if tools.get(tool) is False)

    # --- disks ------------------------------------------------------------------------------------

    def _collect_by_path(self) -> dict[str, str]:
        found: dict[str, str] = {}
        sources: list[Any] = [self.reply.get("disks"), self.hardware.get("disks")]
        for source in sources:
            for item in source if isinstance(source, list) else []:
                if isinstance(item, Mapping) and item.get("path") and isinstance(item.get("by_path"), str) and item["by_path"]:
                    found.setdefault(os.path.realpath(str(item["path"])), item["by_path"])
        return found

    def _live_keys(self) -> set[str]:
        live: set[str] = {os.path.realpath(str(path)) for path in self.reply.get("live_medium") or [] if path}
        for item in self.hardware.get("disks") or []:
            if isinstance(item, Mapping) and item.get("live_medium") and item.get("path"):
                live.add(os.path.realpath(str(item["path"])))
        marked = {node.key for node in blockdev.all_nodes(self.roots) if os.path.realpath(node.path) in live}
        keys = set(marked)
        for node in blockdev.all_nodes(self.roots):
            if node.key in marked:
                keys.update(disk.key for disk in blockdev.parent_disks(self.roots, node))
        return keys

    def by_path_for(self, disk: blockdev.BlockDevice) -> str:
        """The name of a /dev/disk/by-path link that points at the whole disk ("" when there is none)."""
        real = os.path.realpath(disk.path)
        return self._by_path.get(real) or self._local_by_path.get(real, "")

    def disk_entries(self) -> list[DiskEntry]:
        """The disks worth showing: usable ones first, then the ones that are shown only to explain why they are missing."""
        usable: list[DiskEntry] = []
        refused: list[DiskEntry] = []
        for node in self.roots:
            key = blockdev.install_target_problem(node, live_keys=self.live_keys)
            if key in HIDDEN_PROBLEMS:
                continue
            problem = blockdev.TARGET_PROBLEMS[key] if key else (
                f"is smaller than {system_info.format_size(system_info.MIN_DISK_BYTES)}, the least a Gentoo installation needs"
                if node.size < system_info.MIN_DISK_BYTES else "")
            pools = tuple(pool.reason() for pool in blockdev.filesystem_pools(self.roots, [node]))
            entry = DiskEntry(node, problem, self.by_path_for(node), bool({node.serial, node.wwn} & self._shared), pools)
            (refused if problem else usable).append(entry)
        return usable + refused

    def find_disk(self, path: str) -> DiskEntry | None:
        wanted = os.path.realpath(path)
        return next((entry for entry in self.disk_entries() if os.path.realpath(entry.disk.path) == wanted), None)


def apply_disk(storage: Storage, entry: DiskEntry, *, approve_path: bool = False) -> str:
    """Point ``storage`` at the disk and pin its identity the way the graphical installer does.

    A disk with a serial number or WWN is pinned by it. A disk with neither is pinned by its
    hardware path, size and model, which needs ``approve_path`` (the user's explicit approval).
    Returns "serial", "wwn" or "path"; raises ValueError when the disk cannot be identified safely.
    """
    disk = entry.disk
    if entry.has_identity:
        storage.device, storage.device_id, storage.device_id_kind = disk.path, disk.wwn or disk.serial, ""
        return "wwn" if disk.wwn else "serial"
    why = "has the same serial number as another disk" if entry.shared_identity else "has no serial number or WWN"
    if not entry.by_path:
        raise ValueError(f"{disk.path} {why} and no stable hardware path, so the installer cannot make sure it is the same disk later.")
    if not approve_path:
        raise ValueError(f"{disk.path} {why}; it must be approved explicitly.")
    storage.device, storage.device_id, storage.device_id_kind = disk.path, blockdev.path_identity(disk, entry.by_path).value, "path"
    return "path"
