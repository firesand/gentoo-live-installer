"""Turn a chosen system plus its fstab into the list of file systems to mount, in order (docs: fstab-driven, tiered).

The plan is pure data: the same inputs always give the same ``MountPlan`` and digest, which is what lets the two-step
``rescue.mount`` be stateless (the confirming request repeats its parameters and the digest it was shown). Devices come
from the device graph and file system types from the signatures on the devices, never from fstab column 3 (review R16).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from .devices import GPT_ESP, MBR_ESP, GPT_XBOOTLDR, DeviceGraph, DeviceNode
from .errors import RescueError
from .fsprobe import MOUNT_TYPE, RECOVERY_SKIP
from .fstab import CryptEntry, Fstab, Resolved, resolve_all
from .paths import RootFS
from .safety import SAFE_SUBVOL_RE, VALUE_MOUNT_OPTS

TIER_ROOT, TIER_BOOT, TIER_SYSTEM, TIER_DATA = 1, 2, 3, 4
TIER_NAMES = {1: "root", 2: "boot", 3: "system", 4: "data"}
BOOT_MOUNTPOINTS = ("/boot", "/boot/efi", "/efi", "/boot/EFI", "/esp")
SYSTEM_MOUNTPOINTS = ("/usr", "/var", "/etc", "/opt")
# Options that may travel from fstab into a rescue mount; everything else is dropped and reported.
CARRIED_VALUE_OPTIONS = ("subvol", "subvolid", "device", "uid", "gid", "umask", "fmask", "dmask", "iocharset", "codepage", "shortname", "logdev", "rtdev", "journal_path", "journal_dev")
CARRIED_FLAGS = frozenset({"utf8"})
DEVICE_OPTIONS = ("device", "logdev", "rtdev", "journal_path")
PROTECTED_MOUNT_AREAS = ("/proc", "/sys", "/dev", "/run")
MOVABLE_ROOTS = ("/media", "/run/media", "/mnt")


def tier_of(mountpoint: str) -> int:
    if mountpoint == "/":
        return TIER_ROOT
    if mountpoint in BOOT_MOUNTPOINTS or mountpoint.startswith("/boot/"):
        return TIER_BOOT
    if mountpoint in SYSTEM_MOUNTPOINTS or any(mountpoint.startswith(prefix + "/") for prefix in ("/usr", "/var", "/etc", "/opt")):
        return TIER_SYSTEM
    return TIER_DATA


def depth_of(mountpoint: str) -> int:
    return len([part for part in mountpoint.split("/") if part])


@dataclass
class MountItem:
    mountpoint: str
    device: str
    fstype: str
    options: list[str]  # carried fstab options only; ro/rw, recovery and hardening are added when mounting
    tier: int
    source: str = "fstab"  # fstab | user | root
    node_id: str = ""
    selected: bool = True
    needs_mkdir: bool = False
    extra_devices: list[str] = field(default_factory=list)
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mountpoint": self.mountpoint, "device": self.device, "fstype": self.fstype, "options": ",".join(self.options), "tier": self.tier, "tier_name": TIER_NAMES[self.tier],
            "source": self.source, "selected": self.selected, "needs_mkdir": self.needs_mkdir,
        }

    def identity(self) -> dict[str, Any]:
        return {"mountpoint": self.mountpoint, "device": self.device, "fstype": self.fstype, "options": self.options, "tier": self.tier, "selected": self.selected}


@dataclass
class RootSpec:
    device: str
    fstype: str
    uuid: str = ""
    rootflags: str = ""  # "" or "subvolid=N"
    node_id: str = ""
    options: list[str] = field(default_factory=list)  # device options of the root's fstab entry that exist here (logdev=, rtdev=, journal_path=, device=)
    extra_devices: list[str] = field(default_factory=list)  # the other devices of the same file system: btrfs members, external logs (review R7c)

    def identity(self) -> dict[str, Any]:
        return {"device": self.device, "fstype": self.fstype, "uuid": self.uuid, "rootflags": self.rootflags, "options": self.options, "extra_devices": self.extra_devices}

    @property
    def subvolid(self) -> int | None:
        if self.rootflags.startswith("subvolid="):
            return int(self.rootflags.split("=", 1)[1])
        return None


@dataclass
class MountPlan:
    root: RootSpec
    items: list[MountItem]
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    allow_write: bool = True
    ignored_options: list[str] = field(default_factory=list)
    root_node_keys: list[str] = field(default_factory=list)

    @property
    def selected(self) -> list[MountItem]:
        return [item for item in self.items if item.selected]

    @property
    def digest(self) -> str:
        document = {"root": self.root.identity(), "items": [item.identity() for item in self.selected], "allow_write": self.allow_write}
        return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def devices(self) -> list[str]:
        """Every device the repair session may write to: each selected file system with all of its member devices."""
        found = [self.root.device, *self.root.extra_devices]
        for item in self.selected:
            found.extend([item.device, *item.extra_devices])
        return list(dict.fromkeys(found))

    def members_of(self, device: str) -> list[str]:
        """The device and the other devices of its file system (the set that must be made writable together)."""
        if device == self.root.device:
            return list(dict.fromkeys([device, *self.root.extra_devices]))
        for item in self.items:
            if item.device == device:
                return list(dict.fromkeys([device, *item.extra_devices]))
        return [device]

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest, "root": self.root.identity(),
            "items": [item.to_dict() for item in self.items], "unresolved": self.unresolved, "suggestions": self.suggestions, "warnings": self.warnings,
            "allow_write": self.allow_write, "ignored_options": self.ignored_options,
        }


def filter_options(entry_options: Iterable[str], fstype: str) -> tuple[list[str], list[str]]:
    """(carried options, dropped names). Values are validated; a bad ``subvol=`` raises ValueError."""
    carried: list[str] = []
    dropped: list[str] = []
    for option in entry_options:
        key, eq, value = option.partition("=")
        if key in CARRIED_VALUE_OPTIONS and eq:
            pattern = VALUE_MOUNT_OPTS.get(key)
            if key == "subvol":
                if not SAFE_SUBVOL_RE.match(value) or ".." in value.split("/"):
                    raise ValueError(f"The subvolume name {value!r} is unusual and is not mounted automatically")
            elif pattern is not None and not pattern.match(value):
                dropped.append(option)
                continue
            carried.append(option)
        elif key in CARRIED_FLAGS and not eq:
            carried.append(option)
        else:
            dropped.append(key)
    return carried, dropped


def _node_unusable_reason(node: DeviceNode) -> str:
    if node.live_medium:
        return "this is the live medium"
    if node.unsupported_reason:
        return node.unsupported_reason
    if node.role == "swap" or node.fstype == "swap":
        return "swap area"
    if node.fstype not in MOUNT_TYPE and node.fstype not in {"ext3"}:
        return f"file system {node.fstype or '(none)'} cannot be mounted here"
    return ""


def _external_logs(node: DeviceNode, graph: DeviceGraph) -> list[DeviceNode]:
    return graph.external_logs(node)


def _present_device_options(carried: list[str], graph: DeviceGraph, node: DeviceNode, warnings: list[str], where: str) -> list[str]:
    """Device options (``logdev=``) are kept only when the device exists here: names change between the installed system and the live one.

    An XFS external log is found again by the UUID it shares with its file system, so the option is repaired or added when exactly one log matches.
    """
    logs = _external_logs(node, graph)
    kept: list[str] = []
    for option in carried:
        key, _, value = option.partition("=")
        if key in DEVICE_OPTIONS and value.startswith("/dev/"):
            try:
                graph.find(value)
            except LookupError:
                if key == "logdev" and len(logs) == 1:
                    warnings.append(f"{where}: the log device {value} is now {logs[0].path}.")
                    kept.append(f"logdev={logs[0].path}")
                else:
                    warnings.append(f"{where}: the option {option} names a device that is not present here ({value}); it is not used and the mount may fail.")
                continue
        kept.append(option)
    if len(logs) == 1 and not any(option.startswith("logdev=") for option in kept):
        warnings.append(f"{where}: this XFS file system keeps its log on {logs[0].path}; it is mounted with logdev={logs[0].path}.")
        kept.append(f"logdev={logs[0].path}")
    return kept


def _complete_root(root: RootSpec, fstab: Fstab, node: DeviceNode, graph: DeviceGraph, warnings: list[str]) -> RootSpec:
    """The root's own fstab entry can name an external log or other member devices; they are mounted and unprotected together with it."""
    options = list(root.options)
    if not options:
        entry = next((item for item in fstab.entries if _normalise_mountpoint(item.file) == "/"), None)
        if entry is not None:
            try:
                carried, _dropped = filter_options(entry.options, node.fstype)
            except ValueError:
                carried = []
            options = [option for option in carried if option.partition("=")[0] in DEVICE_OPTIONS]
    options = _present_device_options(options, graph, node, warnings, "/")
    extras = list(root.extra_devices) or [path for path in _extra_devices(options, graph, node) if path != node.path]
    return replace(root, options=options, extra_devices=list(dict.fromkeys(extras)))


def _extra_devices(carried: list[str], graph: DeviceGraph, node: DeviceNode) -> list[str]:
    extras: list[str] = []
    for option in carried:
        key, _, value = option.partition("=")
        if key in DEVICE_OPTIONS and value.startswith("/dev/"):
            extras.append(value)
    if node.fstype == "btrfs" and node.uuid:
        extras.extend(member.path for member in graph.by_uuid(node.uuid) if member.fstype == "btrfs" and member.path != node.path)
    extras.extend(log.path for log in _external_logs(node, graph))
    return list(dict.fromkeys(extras))


def plan_mounts(
    root: RootSpec,
    fs: RootFS,
    fstab: Fstab,
    crypttab: list[CryptEntry],
    graph: DeviceGraph,
    *,
    include_tier4: Iterable[str] = (),
    overrides: Iterable[dict[str, str]] = (),
    allow_write: bool = True,
    esp_default_mountpoint: str = "/boot/efi",
    unmount_existing: Iterable[str] = (),
    own_root: str = "",
) -> MountPlan:
    """Plan the file systems below the root; ``fs`` reads the already mounted (read-only) root."""
    chosen_tier4 = set(include_tier4)
    plan = MountPlan(root, [], allow_write=allow_write)
    root_node = graph.find(root.device)
    plan.root = _complete_root(root, fstab, root_node, graph, plan.warnings)
    plan.root_node_keys = [node.key for node in graph.stack_of(root_node)]
    resolved = resolve_all(fstab, graph, crypttab=crypttab)
    claimed = {root_node.key}
    used_mountpoints = {"/"}
    movable = tuple(unmount_existing)
    for position, item in enumerate(resolved):
        entry = item.entry
        mountpoint = _normalise_mountpoint(entry.file)
        if item.status == "skipped" or entry.is_swap or mountpoint in {"none", ""} or entry.vfstype == "swap":
            continue
        if mountpoint == "/":
            continue
        if mountpoint == "/__invalid__":
            plan.unresolved.append({"mountpoint": entry.file, "spec": entry.spec, "status": "unusable", "reason": "the mount point contains '..'", "tier": TIER_DATA})
            continue
        if mountpoint.startswith(PROTECTED_MOUNT_AREAS) or any(mountpoint == area for area in PROTECTED_MOUNT_AREAS) or mountpoint == "/proc":
            plan.warnings.append(f"fstab entry for {mountpoint} is ignored: that area is reserved for the system's virtual file systems")
            continue
        tier = tier_of(mountpoint)
        if mountpoint in used_mountpoints:
            plan.warnings.append(f"fstab lists {mountpoint} more than once; only the first entry is used")
            continue
        used_mountpoints.add(mountpoint)
        if item.status in {"unresolved", "ambiguous", "needs_unlock"}:
            plan.unresolved.append({"mountpoint": mountpoint, "spec": entry.spec, "status": item.status, "reason": item.reason, "tier": tier, **({"container": item.container} if item.container else {})})
            continue
        node = item.device
        assert node is not None
        reason = _node_unusable_reason(node)
        if reason:
            plan.unresolved.append({"mountpoint": mountpoint, "spec": entry.spec, "status": "unusable", "reason": reason, "tier": tier})
            continue
        try:
            carried, dropped = filter_options(entry.options, node.fstype)
        except ValueError as exc:
            plan.unresolved.append({"mountpoint": mountpoint, "spec": entry.spec, "status": "unusable", "reason": str(exc), "tier": tier})
            continue
        plan.ignored_options.extend(f"{mountpoint}: {name}" for name in dropped)
        carried = _present_device_options(carried, graph, node, plan.warnings, mountpoint)
        selected = tier in {TIER_BOOT, TIER_SYSTEM} or mountpoint in chosen_tier4
        plan.items.append(MountItem(mountpoint, node.path, node.fstype, carried, tier, "fstab", node.id, selected, False, _extra_devices(carried, graph, node), position))
        claimed.add(node.key)
    _apply_overrides(plan, overrides, graph, used_mountpoints, chosen_tier4)
    _check_busy(plan, graph, root_node, movable, own_root)
    mounted_below = [item.mountpoint.rstrip("/") + "/" for item in plan.selected]
    for mount in plan.items:
        # Below another mount the directory lives on that file system (/boot/efi on a separate /boot): only the session can tell, once it is mounted.
        mount.needs_mkdir = not any(mount.mountpoint.startswith(prefix) for prefix in mounted_below) and not _directory_exists(fs, mount.mountpoint)
    plan.items.sort(key=lambda it: (depth_of(it.mountpoint), it.order))
    if not fstab.entries or (plan.unresolved and not plan.selected):
        plan.suggestions.extend(suggest_boot_mounts(graph, root_node, esp_default_mountpoint, {item.mountpoint for item in plan.items}))
    if fs.is_dir("/boot") and not any(item.tier == TIER_BOOT for item in plan.selected) and not fs.listdir_or("/boot"):
        plan.warnings.append("/boot is empty on the root file system and no separate /boot was mounted; the kernel may be on a partition that is not listed in fstab")
    return plan


def _normalise_mountpoint(raw: str) -> str:
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if ".." in parts:
        return "/__invalid__"
    return "/" + "/".join(parts)


def _directory_exists(fs: RootFS, mountpoint: str) -> bool:
    try:
        return fs.is_dir(mountpoint)
    except RescueError:
        return False


def _apply_overrides(plan: MountPlan, overrides: Iterable[dict[str, str]], graph: DeviceGraph, used: set[str], tier4: set[str]) -> None:
    for override in overrides:
        mountpoint = _normalise_mountpoint(str(override.get("mountpoint", "")))
        device = str(override.get("device", ""))
        if mountpoint in {"/", "/__invalid__"} or mountpoint.startswith(PROTECTED_MOUNT_AREAS):
            raise RescueError("bad_request", f"{override.get('mountpoint')!r} cannot be used as a mount point", mountpoint=mountpoint)
        try:
            node = graph.find(device)
        except LookupError as exc:
            raise RescueError("not_found", f"Device {device} was not found", device=device) from exc
        reason = _node_unusable_reason(node)
        if reason:
            raise RescueError("unsupported_fs" if node.unsupported_reason else "live_medium" if node.live_medium else "bad_request", f"{device}: {reason}", device=device)
        plan.items = [item for item in plan.items if item.mountpoint != mountpoint]
        plan.unresolved = [entry for entry in plan.unresolved if entry["mountpoint"] != mountpoint]
        tier = tier_of(mountpoint)
        plan.items.append(MountItem(mountpoint, node.path, node.fstype, [], tier, "user", node.id, True, False, _extra_devices([], graph, node), 10_000 + len(plan.items)))
        used.add(mountpoint)


def _check_busy(plan: MountPlan, graph: DeviceGraph, root_node: DeviceNode, movable: tuple[str, ...], own_root: str) -> None:
    """Refuse devices that are mounted elsewhere unless the caller listed those mounts and they are safe to move."""
    nodes = [root_node] + [graph.find(item.device) for item in plan.selected]
    for node in nodes:
        for mountpoint in node.mountpoints:
            if mountpoint == "[SWAP]" or (own_root and (mountpoint == own_root or mountpoint.startswith(own_root + "/"))):
                continue
            if mountpoint in movable and mountpoint.startswith(MOVABLE_ROOTS) and not mountpoint.startswith(("/mnt/gentoo", "/mnt/rescue", "/run/initramfs")):
                continue
            hint = f" ({', '.join(node.fsroots)})" if node.fsroots else ""
            raise RescueError("mounted_elsewhere", f"{node.path}{hint} is mounted at {mountpoint}. Unmount it first (or confirm that rescue may unmount it).", device=node.path, mountpoint=mountpoint)


def suggest_boot_mounts(graph: DeviceGraph, root_node: DeviceNode, esp_mountpoint: str, taken: set[str]) -> list[dict[str, Any]]:
    """When fstab says nothing useful: offer (never auto-mount) EFI/boot partitions on the root's own disks."""
    disks = {disk.key for disk in graph.parent_disks(root_node)}
    found: list[dict[str, Any]] = []
    for node in graph:
        if node.key == root_node.key or node.live_medium or not ({disk.key for disk in graph.parent_disks(node)} & disks):
            continue
        if node.parttype in {GPT_ESP, MBR_ESP} and node.fstype == "vfat" and esp_mountpoint not in taken:
            found.append({"mountpoint": esp_mountpoint, "device": node.path, "reason": "EFI system partition on the same disk"})
        elif node.parttype == GPT_XBOOTLDR and "/boot" not in taken:
            found.append({"mountpoint": "/boot", "device": node.path, "reason": "extended boot loader partition on the same disk"})
    return found
