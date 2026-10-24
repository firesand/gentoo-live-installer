"""Read-only inventory of the live system: disks, tools, network, memory.

Disk parsing lives in gentoo_installer.blockdev (one lsblk parser for the GUI, the
daemon, hardware detection and rescue). Disk, parse_lsblk and find_disk stay here as
thin compatibility names so older callers keep working.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from gentoo_installer import blockdev
from gentoo_installer.blockdev import BlockDevice

from .runner import DryRunRunner, Runner, RunnerError, SubprocessRunner, format_command

# The daemon's historical name for a node of the lsblk tree.
Disk = BlockDevice

BY_PATH_DIR = Path("/dev/disk/by-path")
PROC_CMDLINE = Path("/proc/cmdline")
# Where dracut's live and iso-scan modes mount the installer medium (or the device holding its image).
LIVE_MOUNTPOINTS = ("/run/initramfs/live", "/run/initramfs/isoscan")
DEFAULT_MIRROR = "https://distfiles.gentoo.org"
# Stacked devices (LVM, dm-crypt, md, multipath, ...) are everything lsblk lists that is not one of these.
STRUCTURAL_TYPES = frozenset({"disk", "part", "loop", "rom"})
MD_MEMBER_TYPES = frozenset({"linux_raid_member", "isw_raid_member", "ddf_raid_member"})
LVM_MEMBER_TYPES = frozenset({"LVM2_member"})

REQUIRED_TOOLS = ("lsblk", "findmnt", "blkid", "sgdisk", "parted", "partprobe", "udevadm", "wipefs", "mkfs.ext4", "mkswap", "mount", "umount", "tar", "gpg", "gpgv")
OPTIONAL_TOOLS = ("swapon", "swapoff", "mkfs.xfs", "mkfs.btrfs", "mkfs.vfat", "btrfs", "cryptsetup", "pvcreate", "vgcreate", "lvcreate", "pvs", "vgchange", "mdadm", "dmsetup", "lsof", "sbsign", "sbverify", "efibootmgr")


@dataclass
class Inspection:
    architecture: str
    memory_bytes: int
    uefi: bool
    network: bool
    tools: dict[str, bool]
    disks: list[Disk]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # [{code, severity: "error"|"warning", message}] behind errors/warnings; clients branch on the code.
    findings: list[dict[str, str]] = field(default_factory=list)
    # Device paths of the live installer medium and of the disks that hold it.
    live_medium: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["disks"] = [disk.to_dict() for disk in self.disks]
        return result

    def blocking_errors(self) -> list[str]:
        """Messages that must stop a real installation.

        Structured findings decide when present; an inspector that only fills the
        legacy errors list (older callers, tests) keeps every entry blocking.
        """
        if not self.findings:
            return list(self.errors)
        listed = {finding["message"] for finding in self.findings}
        legacy = [error for error in self.errors if error not in listed]
        return [finding["message"] for finding in self.findings if finding.get("severity") == "error"] + legacy


# --- compatibility wrappers over gentoo_installer.blockdev ------------------------------


def parse_lsblk(text: str) -> list[Disk]:
    raw = json.loads(text)
    if not isinstance(raw, dict) or not isinstance(raw.get("blockdevices"), list):
        raise ValueError("lsblk returned an invalid disk list")
    return [Disk.from_lsblk(item) for item in raw["blockdevices"]]


def find_disk(disks: Sequence[Disk], device: str) -> Disk:
    """The single lsblk node for ``device``.

    lsblk repeats a device under every parent it relates to (an md array under each
    member, an LV under each PV); blockdev.find merges those by MAJ:MIN, so only two
    genuinely different devices sharing a path are ambiguous.
    """
    try:
        return blockdev.find(disks, device)
    except LookupError:
        raise ValueError(f"Target device is absent: {device}") from None
    except ValueError:
        raise ValueError(f"Target device is ambiguous: {device}") from None


def parent_disks(disks: Sequence[Disk], device: str | Disk) -> list[Disk]:
    """Top-level devices (whole disks and loop devices) whose tree contains ``device``.

    An md array or an LV that spans several disks returns every one of them.
    """
    wanted = _key_of(disks, device)
    found: dict[str, Disk] = {}
    for root in disks:
        if root.type in {"disk", "loop"} and any(node.key == wanted for node in root.walk()):
            found.setdefault(root.key, root)
    return list(found.values())


def _key_of(disks: Sequence[Disk], device: str | Disk) -> str:
    if isinstance(device, BlockDevice):
        return device.key
    try:
        return blockdev.find(disks, device).key
    except (LookupError, ValueError):
        return os.path.realpath(device)


def disk_identities(disk: Disk) -> set[str]:
    """Strings that pin ``disk`` in a manifest: its serial and its WWN.

    A kernel name is never an identity for a real disk, it changes between boots.
    Loop devices have no serial and are pinned by their path (tests, CI, disk images).
    Disks with neither a serial nor a WWN need the user-approved by-path identity
    (storage.device_id_kind == "path"), which StorageManager checks separately.
    """
    identities = {value for value in (disk.serial, disk.wwn) if value}
    if disk.type == "loop":
        identities.add(disk.path)
    return identities


def holders(node: Disk) -> list[Disk]:
    """Stacked devices (LVM, dm-crypt, md, device-mapper, multipath) below ``node``.

    Listed children-first so they can be torn down in order; a device lsblk repeats
    under several parents appears once. Mounted ones are refused elsewhere, this finds
    the active-but-unmounted ones.
    """
    seen: set[str] = set()
    found: list[Disk] = []
    for child in reversed(node.walk()[1:]):
        if child.type not in STRUCTURAL_TYPES and child.key not in seen:
            seen.add(child.key)
            found.append(child)
    return found


# --- by-path links (stable identity for disks without serial/WWN) -----------------------


def resolve_by_path(name: str, directory: Path | str = BY_PATH_DIR) -> str:
    """Kernel path a by-path link name points to ("" when it does not exist)."""
    if not name or "/" in name or name in {".", ".."}:
        return ""
    link = Path(directory) / name
    return os.path.realpath(link) if link.is_symlink() else ""


def by_path_names(device: str, directory: Path | str = BY_PATH_DIR) -> list[str]:
    """by-path link names of a whole disk, most stable first.

    Partition links (``*-part1``) are skipped. A ``pci-...``/``platform-...`` name is
    preferred over the ``virtio-pci-...`` duplicate udev creates for virtio disks.
    """
    wanted = os.path.realpath(device)
    try:
        names = [entry.name for entry in os.scandir(directory) if entry.is_symlink()]
    except OSError:
        return []
    matching = [name for name in names if "-part" not in name and os.path.realpath(Path(directory) / name) == wanted]
    return sorted(matching, key=lambda name: (name.startswith("virtio-"), len(name), name))


# --- read-only command execution ------------------------------------------------------


class ReadOnlyRunner:
    """Runs only inventory commands; every other command is refused.

    A dry-run preview uses it so the plan is made against the real disks without any
    chance of touching them.
    """

    ALLOWED = (("lsblk",), ("findmnt",), ("blkid",), ("pvs",), ("mdadm", "--examine"))

    def __init__(self, log: Callable[[str], None] | None = None) -> None:
        self._log = log or (lambda _line: None)
        self._inner = SubprocessRunner()  # silent: the lsblk JSON would flood a preview's log

    def run(self, argv: Sequence[str], **kwargs: Any) -> Any:
        args = tuple(argv)
        if not any(args[: len(prefix)] == prefix for prefix in self.ALLOWED):
            raise RunnerError(args or ("",), 126, "refused: only read-only inventory commands run in a preview")
        self._log("$ [read-only] " + format_command(args))
        return self._inner.run(args, **kwargs)


# --- discovery ------------------------------------------------------------------------


class Discovery:
    """Inventory of disks, tools, memory and network.

    ``mirror`` and ``proxy`` (both optional) make the network probe test the host the
    installer will actually download from, through the proxy the user configured.
    """

    def __init__(
        self,
        runner: Runner,
        *,
        mirror: str = "",
        proxy: str = "",
        cmdline_path: Path | str = PROC_CMDLINE,
        by_path_dir: Path | str = BY_PATH_DIR,
    ) -> None:
        self.runner = runner
        # A preview runs with a DryRunRunner, which returns empty output for lsblk; reading the
        # real disks (read-only) keeps it realistic without any risk.
        self.reader: Runner = ReadOnlyRunner(runner.log) if isinstance(runner, DryRunRunner) else runner
        self.mirror = mirror
        self.proxy = proxy
        self.cmdline_path = Path(cmdline_path)
        self.by_path_dir = Path(by_path_dir)

    def configure(self, *, mirror: str | None = None, proxy: str | None = None) -> None:
        """Set the network probe target after construction (the installer knows the manifest later)."""
        if mirror is not None:
            self.mirror = mirror
        if proxy is not None:
            self.proxy = proxy

    def disks(self) -> list[Disk]:
        return parse_lsblk(self.reader.run(blockdev.lsblk_argv()).stdout)

    def live_medium_keys(self, disks: Sequence[Disk]) -> set[str]:
        """MAJ:MIN keys of the live installer medium and of every disk that holds it.

        Two rules, both applied: what is mounted at dracut's live mount points, and the
        root=live:CDLABEL= label on the kernel command line (a medium copied to RAM is no
        longer mounted but keeps its label).
        """
        sources: list[str] = []
        for mountpoint in LIVE_MOUNTPOINTS:
            try:
                output = self.reader.run(("findmnt", "--noheadings", "--output", "SOURCE", "--mountpoint", mountpoint)).stdout
            except (RunnerError, OSError):
                continue
            # findmnt prints /dev/sdb1[/subvolume] for btrfs mounts; only the device matters here.
            source = output.strip().splitlines()[0].strip().split("[", 1)[0] if output.strip() else ""
            if source.startswith("/dev/"):
                sources.append(source)
        try:
            cmdline = self.cmdline_path.read_text(errors="replace")
        except OSError:
            cmdline = ""
        return blockdev.live_medium_keys(disks, sources=sources, cmdline=cmdline)

    def inspect(self) -> Inspection:
        findings: list[dict[str, str]] = []

        def note(code: str, severity: str, message: str) -> None:
            findings.append({"code": code, "severity": severity, "message": message})

        live: list[str] = []
        try:
            disks = self.disks()
            keys = self.live_medium_keys(disks)
            live = sorted(node.path for node in blockdev.all_nodes(disks) if node.key in keys)
        except (RunnerError, ValueError, OSError) as exc:  # includes json.JSONDecodeError and a missing lsblk
            disks = []
            note("preflight.disks", "error", f"Disk discovery failed: {exc}")
        memory = 0
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    memory = int(line.split()[1]) * 1024
                    break
        except (OSError, ValueError, IndexError):
            note("preflight.memory_unknown", "error", "Memory size could not be determined")
        host, port = mirror_endpoint(self.mirror)
        network = _network_ready(host, port, self.proxy)
        if not network:
            via = f" through the proxy {_proxy_label(self.proxy)}" if self.proxy else ""
            note("preflight.network", "error", f"No network connection to the stage3 mirror {host}:{port}{via} was detected")
        architecture = platform.machine()
        if architecture not in {"x86_64", "amd64"}:
            note("preflight.architecture", "error", f"Gentoo AMD64 requires x86_64, not {architecture}")
        if memory and memory < 2 * 1024**3:
            note("preflight.memory_low", "warning", "At least 2 GiB of memory is recommended")
        tools = {tool: shutil.which(tool) is not None for tool in (*REQUIRED_TOOLS, *OPTIONAL_TOOLS)}
        missing = [tool for tool in REQUIRED_TOOLS if not tools[tool]]
        if missing:
            note("preflight.tools", "error", "Missing required tools: " + ", ".join(missing))
        errors = [item["message"] for item in findings if item["severity"] == "error"]
        warnings = [item["message"] for item in findings if item["severity"] == "warning"]
        return Inspection(architecture, memory, Path("/sys/firmware/efi").exists(), network, tools, disks, errors, warnings, findings, live)


# --- network probe --------------------------------------------------------------------


def mirror_endpoint(mirror: str = "") -> tuple[str, int]:
    """Host and TCP port of the HTTPS stage3 mirror (the official one by default)."""
    default_host = urllib.parse.urlsplit(DEFAULT_MIRROR).hostname or ""
    try:
        parsed = urllib.parse.urlsplit(mirror or DEFAULT_MIRROR)
        port = parsed.port
    except ValueError:  # an unparsable URL: validation reports it, the probe falls back to the default
        return default_host, 443
    return parsed.hostname or default_host, port or 443


def _proxy_label(proxy: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        return f"{parsed.hostname or proxy}:{parsed.port}" if parsed.port else (parsed.hostname or proxy)
    except ValueError:
        return proxy


def _network_ready(host: str = "distfiles.gentoo.org", port: int = 443, proxy: str = "", timeout: float = 5.0) -> bool:
    """TCP reachability of host:port, or an HTTP CONNECT through the proxy when one is set.

    Any failure (unreachable, refused, 403/407, garbage) is a False answer, never an exception.
    """
    try:
        if not proxy:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        parsed = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        if not parsed.hostname:
            return False
        with socket.create_connection((parsed.hostname, parsed.port or 3128), timeout=timeout) as connection:
            if parsed.scheme not in {"http", "https"}:
                return True  # SOCKS and friends: the proxy answering is all a probe can check
            connection.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
            connection.settimeout(timeout)
            status = connection.recv(256).split(b"\r\n", 1)[0].split()
            return len(status) >= 2 and status[1] == b"200"
    except (OSError, ValueError):
        return False


# --- LVM and md membership (explicit checks: live auto-activation is switched off) ---------


@dataclass(frozen=True)
class VolumeSet:
    """An LVM volume group or md array that has members on the inspected disk."""

    kind: str  # "lvm" | "md"
    name: str  # VG name, or the md array UUID ("" when it could not be read)
    inside: tuple[str, ...]  # member devices on the inspected disk
    outside: tuple[str, ...] = ()  # members elsewhere: erasing the inspected disk damages those too

    @property
    def spans_other_disks(self) -> bool:
        return bool(self.outside)

    def describe(self) -> str:
        label = {"lvm": "LVM volume group", "md": "RAID array"}.get(self.kind, self.kind)
        name = f" {self.name}" if self.name else ""
        text = f"{label}{name} (members here: {', '.join(self.inside)}"
        return text + (f"; also on {', '.join(self.outside)})" if self.outside else ")")


_PV_LINE = re.compile(r"^\s*(/dev/\S+?):([^:\s]*)")
_MD_UUID = re.compile(r"^\s*(?:Array )?UUID\s*:\s*(\S+)", re.MULTILINE)


def _probe_type(runner: Runner, device: str) -> str:
    """Filesystem/RAID signature found by reading the device itself ("" when none)."""
    try:
        return runner.run(("blkid", "--probe", "--output", "value", "--match-tag", "TYPE", device)).stdout.strip()
    except (RunnerError, OSError):
        return ""  # exit status 2: nothing recognisable


def find_volume_sets(runner: Runner, disks: Sequence[Disk], nodes: Sequence[Disk]) -> list[VolumeSet]:
    """LVM VGs and md arrays that have a member inside ``nodes`` (whole disks or partitions).

    The live system no longer assembles arrays or activates volume groups by itself,
    so lsblk shows nothing active even when the disk is a member of a set that spans
    other disks. Signatures are read explicitly (lsblk's udev data, then blkid on
    leaves without one), and pvs / mdadm --examine name the set and find its other members.
    """
    inside_keys = {sub.key for node in nodes for sub in node.walk()}
    lvm_inside: list[str] = []
    md_inside: list[str] = []
    for node in nodes:
        for sub in node.walk():
            signature = sub.fstype
            if not signature and not sub.children and sub.type in {"disk", "part"} and sub.size >= 1024 * 1024:
                signature = _probe_type(runner, sub.path)
            if signature in LVM_MEMBER_TYPES:
                lvm_inside.append(sub.path)
            elif signature in MD_MEMBER_TYPES:
                md_inside.append(sub.path)
    sets: list[VolumeSet] = []
    if lvm_inside:
        groups: dict[str, list[str]] = {}
        outside_groups: dict[str, list[str]] = {}
        try:
            listing = runner.run(("pvs", "--noheadings", "--separator", ":", "-o", "pv_name,vg_name")).stdout
        except (RunnerError, OSError):
            listing = ""
        inside_real = {os.path.realpath(device) for device in lvm_inside}
        for line in listing.splitlines():
            match = _PV_LINE.match(line)
            if not match or not match.group(2):
                continue
            device, group = match.group(1), match.group(2)
            if os.path.realpath(device) in inside_real:
                groups.setdefault(group, []).append(device)
            else:
                outside_groups.setdefault(group, []).append(device)
        named = {device for members in groups.values() for device in members}
        for group, members in groups.items():
            sets.append(VolumeSet("lvm", group, tuple(members), tuple(outside_groups.get(group, ()))))
        unnamed = [device for device in lvm_inside if device not in named]
        if unnamed:
            sets.append(VolumeSet("lvm", "", tuple(unnamed)))
    if md_inside:
        uuids: dict[str, str] = {}
        for device in md_inside:
            uuids[device] = _md_uuid(runner, device)
        outside = [
            sub
            for root in disks
            for sub in root.walk()
            if sub.fstype in MD_MEMBER_TYPES and sub.key not in inside_keys and sub.type in {"disk", "part"}
        ]
        outside_uuid = {sub.path: _md_uuid(runner, sub.path) for sub in outside} if any(uuids.values()) else {}
        for uuid in dict.fromkeys(uuids.values()):
            members = tuple(device for device, value in uuids.items() if value == uuid)
            others = tuple(path for path, value in outside_uuid.items() if uuid and value == uuid)
            sets.append(VolumeSet("md", uuid, members, others))
    return sets


def _md_uuid(runner: Runner, device: str) -> str:
    try:
        output = runner.run(("mdadm", "--examine", device)).stdout
    except (RunnerError, OSError):
        return ""
    match = _MD_UUID.search(output)
    return match.group(1) if match else ""
