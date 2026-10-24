"""Read-only discovery of installed systems (docs: nothing is written; probe mounts are ro, nodev, nosuid, noexec).

The scan lists block devices, inventories LUKS/LVM/md, probe-mounts the file systems that can hold a Linux root and
identifies what is on them from their own files. File systems rescue cannot or should not parse (ntfs, exfat, hfsplus,
iso9660, udf; review R16) are listed, never mounted. A device that fails to mount is reported and the scan goes on.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .bootinfo import collect_boot
from .btrfs import BtrfsRoot, find_roots, list_subvolumes
from .devices import DeviceGraph, DeviceNode, build_graph, reclassify, rescue_lsblk_argv
from .errors import RescueError
from .fstab import Fstab, parse_crypttab, resolve_all
from .fsprobe import mount_readonly
from .hostenv import Environment, Host, collect_environment
from .osrelease import NOT_GENTOO, SystemFacts, identify
from .paths import RootFS
from .runner import RescueRunner
from .safety import PolicyContext
from .stack import StackManager

LIVE_MOUNT_TARGETS = ("/run/initramfs/live", "/run/live/medium", "/mnt/cdrom", "/media/cdrom")
Emit = Callable[..., None]


@dataclass
class ScanOptions:
    probe: bool = True
    include_loop: bool = False
    network: bool = False


@dataclass
class ScanResult:
    scan_id: str
    graph: DeviceGraph
    environment: Environment
    stacks: dict[str, list[dict[str, Any]]]
    candidates: list[dict[str, Any]]
    hints: list[dict[str, str]]
    warnings: list[str]
    live_medium: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scan_id": self.scan_id, "environment": self.environment.to_dict(), "devices": self.graph.to_dict_list(), "stacks": self.stacks, "candidates": self.candidates,
            "live_medium": self.live_medium, "hints": self.hints, "warnings": self.warnings,
        }

    def candidate(self, candidate_id: str) -> dict[str, Any]:
        for item in self.candidates:
            if item["id"] == candidate_id:
                return item
        raise KeyError(candidate_id)


def candidate_id(uuid: str, rootflags: str, device: str) -> str:
    return "c" + hashlib.sha1(f"{uuid}|{rootflags}|{device}".encode()).hexdigest()[:8]


def strip_source(source: str) -> str:
    """findmnt prints '/dev/sda2[/@home]' for a btrfs subvolume mount."""
    return source.split("[", 1)[0]


def live_sources(runner: RescueRunner) -> list[str]:
    result = runner.execute(("findmnt", "-n", "-l", "-o", "SOURCE,TARGET"), timeout=15)  # -l: the default tree view draws branches in front of the target
    sources: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip() in LIVE_MOUNT_TARGETS and parts[0].startswith("/dev/"):
            sources.append(strip_source(parts[0]))
    return sources


def parse_blkid_export(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, eq, value = line.partition("=")
        if eq:
            fields[key.strip()] = value.strip()
    return fields


def load_graph(runner: RescueRunner, ctx: PolicyContext, host: Host) -> DeviceGraph:
    """One lsblk call merged into a device graph; the live medium is marked in the policy so it can never be touched."""
    runner.execute(("udevadm", "settle", "--timeout=10"), timeout=20)
    output = runner.run(rescue_lsblk_argv(), timeout=30).stdout
    graph = build_graph(output, live_sources=live_sources(runner), cmdline=host.cmdline())
    for node in graph:
        if node.live_medium:
            ctx.devices.mark_live(node.path)
    return graph


@contextlib.contextmanager
def probe_mounted(runner: RescueRunner, host: Host, probe_root: Path, node: DeviceNode, *, subvolid: int | None = None, sleep: Callable[[float], None] = time.sleep,
                  graph: DeviceGraph | None = None) -> Iterator[tuple[str, int]]:
    """Mount a device read-only (nodev, nosuid, noexec, no journal replay) in a private directory and always undo it.

    The kernel read-only flag is set around the mount and restored afterwards; yields (directory, ladder rung). An XFS file system
    with an external log cannot be mounted without ``logdev=``, so the log found in ``graph`` is named.
    """
    was_ro = runner.execute(("blockdev", "--getro", node.path), timeout=10).stdout.strip() == "1"
    if not was_ro:
        runner.execute(("blockdev", "--setro", node.path), timeout=10)
    target = probe_root / (node.kname or node.name).replace("/", "_")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    mounted = False
    try:
        logs = graph.external_logs(node) if graph is not None else []
        rung = mount_readonly(runner, node.path, node.fstype, str(target), subvolid=subvolid if subvolid is not None else (5 if node.fstype == "btrfs" else None),
                              extra=[f"logdev={logs[0].path}"] if len(logs) == 1 else [], available=host.filesystems() or None)
        mounted = True
        yield str(target), rung
    finally:
        if mounted:
            for attempt in range(3):
                if runner.execute(("umount", str(target)), timeout=15).returncode == 0:
                    break
                sleep(0.2 * (attempt + 1))
            else:
                runner.execute(("umount", "-l", str(target)), timeout=15)  # a stuck probe must not outlive the operation
        shutil.rmtree(target, ignore_errors=True)
        if not was_ro:
            runner.execute(("blockdev", "--setrw", node.path), timeout=10)


class Scanner:
    def __init__(self, runner: RescueRunner, ctx: PolicyContext, host: Host, probe_root: str | os.PathLike[str], *, emit: Emit | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.runner = runner
        self.ctx = ctx
        self.host = host
        self.probe_root = Path(probe_root)
        self.emit: Emit = emit or (lambda *_a, **_k: None)
        self.clock = clock
        self.sleep = sleep
        self.warnings: list[str] = []
        self.graph: DeviceGraph | None = None

    # ---- graph ---------------------------------------------------------------------------------
    def load_graph(self) -> DeviceGraph:
        self.graph = load_graph(self.runner, self.ctx, self.host)
        return self.graph

    def _fill_signatures(self, graph: DeviceGraph) -> None:
        """blkid -p for nodes udev left without a signature (stale udev data is the usual cause)."""
        changed = False
        for node in graph:
            if node.fstype or node.live_medium or node.size <= 1 << 20 or node.children or node.type not in {"part", "lvm", "crypt", "disk"}:
                continue
            if node.type == "disk" and node.pttype:
                continue
            result = self.runner.execute(("blkid", "-p", "-o", "export", node.path), timeout=15)
            if result.returncode not in (0, 2):
                node.probe_error = "unresponsive" if result.returncode not in (0, 2) else ""
                continue
            fields = parse_blkid_export(result.stdout)
            if fields.get("TYPE"):
                node.fstype, node.uuid, node.label = fields["TYPE"], fields.get("UUID", node.uuid), fields.get("LABEL", node.label)
                changed = True
        if changed:
            reclassify(graph)

    # ---- the scan ---------------------------------------------------------------------------------
    def scan(self, options: ScanOptions | None = None) -> ScanResult:
        options = options or ScanOptions()
        self.warnings = []
        self.emit("stage", "devices", stage="devices")
        graph = self.load_graph()
        self._fill_signatures(graph)
        self.emit("stage", "Looking at encryption, LVM and RAID", stage="stacks")
        self.runner.execute(("btrfs", "device", "scan"), timeout=30)
        stacks = self._stacks(graph)
        candidates: list[dict[str, Any]] = []
        if options.probe:
            nodes = [node for node in graph if node.role == "filesystem" and not node.live_medium and (options.include_loop or node.type != "loop")]
            for index, node in enumerate(nodes, 1):
                self.emit("progress", f"Reading {node.path}", stage="probe", device=node.path, index=index, total=len(nodes), progress=index * 100.0 / max(len(nodes), 1))
                candidates.extend(self._probe_node(node, graph))
        self.emit("stage", "Checking the firmware and network", stage="environment")
        environment = collect_environment(self.host, self.runner, network=options.network)
        hints = make_hints(graph, candidates, environment, stacks)
        live = sorted(node.path for node in graph if node.live_medium)
        return ScanResult("s" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.clock())), graph, environment, stacks, candidates, hints, list(self.warnings), live)

    # ---- stacks ---------------------------------------------------------------------------------------
    def _stacks(self, graph: DeviceGraph) -> dict[str, list[dict[str, Any]]]:
        from .ledger import Ledger  # a throw-away ledger: inventory never acquires anything

        manager = StackManager(self.runner, self.ctx, Ledger(), lambda: graph, self.host)
        luks: list[dict[str, Any]] = []
        for node in graph:
            if node.role not in {"luks_locked", "luks_unlocked"} or node.live_medium:
                continue
            entry: dict[str, Any] = {"device": node.path, "state": "locked" if node.role == "luks_locked" else "unlocked", "uuid": node.uuid, "version": int(node.fsver) if node.fsver.isdigit() else 1}
            try:
                entry.update({key: value for key, value in manager.luks_info(node).to_dict().items() if key in {"tokens", "pbkdf", "memory_kib", "hints"}})
            except RescueError:
                pass
            luks.append(entry)
        vgs: list[dict[str, Any]] = []
        arrays: list[dict[str, Any]] = []
        if any(node.role == "lvm_pv" and not node.live_medium for node in graph):
            vgs = self._inventory("LVM volume groups", lambda: [group.to_dict() for group in manager.lvm_inventory()])
        if any(node.role == "md_member" and not node.live_medium for node in graph):
            arrays = self._inventory("RAID arrays", lambda: [array.to_dict() for array in manager.md_inventory(graph)])
        return {"luks": luks, "vgs": vgs, "arrays": arrays}

    def _inventory(self, what: str, read: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """A tool that prints something unexpected costs that part of the list, never the whole scan."""
        try:
            return read()
        except (RescueError, ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
            self.emit("log", f"{what}: {type(exc).__name__}: {exc}")
            self.warnings.append(f"The {what} could not be read, so they are not listed.")
            return []

    # ---- probing ---------------------------------------------------------------------------------------
    def _probe_node(self, node: DeviceNode, graph: DeviceGraph) -> list[dict[str, Any]]:
        busy = [mp for mp in node.mountpoints if not mp.startswith(("/run/initramfs", "[SWAP]")) and "/mnt/rescue" not in mp]
        if busy:
            return self._probe_mounted_elsewhere(node, graph, busy)
        try:
            with probe_mounted(self.runner, self.host, self.probe_root, node, sleep=self.sleep, graph=graph) as (target, rung):
                return self._identify_mounted(node, graph, target, rung)
        except RescueError as exc:
            node.probe_error = exc.short
            self.warnings.append(f"{node.path}: {exc.message}")
            return []

    def _probe_mounted_elsewhere(self, node: DeviceNode, graph: DeviceGraph, mountpoints: list[str]) -> list[dict[str, Any]]:
        """Read a device that is already mounted through the mount point that shows a system.

        A btrfs file system is often mounted several times (``/``, ``/var/lib/docker``, ``/home`` ...), and lsblk lists
        the subvolume mounts in any order, so every mount point is tried, the shallowest first.
        """
        for mountpoint in sorted(mountpoints, key=lambda path: (path.count("/"), path)):
            try:
                with RootFS(mountpoint) as fs:
                    found = self._candidate_for(node, graph, fs, rootflags="", subvol=None, rung=0)
            except OSError:
                continue
            if found:
                found["busy_elsewhere"] = mountpoint
                if found["supported"]:
                    found["warnings"].append(f"{node.path} is mounted at {mountpoint}; rescue will ask you to unmount it before repairing")
                return [found]
        return []

    def _identify_mounted(self, node: DeviceNode, graph: DeviceGraph, target: str, rung: int) -> list[dict[str, Any]]:
        with RootFS(target) as fs:
            if node.fstype != "btrfs":
                found = self._candidate_for(node, graph, fs, rootflags="", subvol=None, rung=rung)
                return [found] if found else []
            subvolumes, default_id = list_subvolumes(self.runner, target)
            candidates = []
            for root in find_roots(self.runner, fs, target, subvolumes, default_id):
                view = fs.sub(root.subvolume.path or "/")
                with view:
                    found = self._candidate_for(node, graph, view, rootflags=root.rootflags if root.subvolume.id != 5 else "", subvol=root, rung=rung)
                if found:
                    candidates.append(found)
            return candidates

    def _candidate_for(self, node: DeviceNode, graph: DeviceGraph, fs: RootFS, *, rootflags: str, subvol: BtrfsRoot | None, rung: int) -> dict[str, Any] | None:
        facts = identify(fs)
        if facts is None:
            return None
        boot = collect_boot(fs)
        fstab_text = fs.read_text_or("/etc/fstab")
        fstab = Fstab.parse(fstab_text)
        crypt = parse_crypttab(fs.read_text_or("/etc/crypttab"))
        resolved = resolve_all(fstab, graph, crypttab=crypt)
        supported = facts.os.family == "gentoo"
        warnings: list[str] = []
        if not supported:
            warnings.append(NOT_GENTOO)  # the only thing worth saying about a system this tool does not open
        else:
            if rung:
                warnings.append("The file system needed recovery options to mount; some data may be unreadable")
            if facts.arch and facts.arch != "x86_64" and facts.arch != "i386":
                warnings.append(f"This is a {facts.arch} system; programs in it cannot run here, so only file-level repairs are possible")
            if subvol and subvol.kind == "snapshot":
                warnings.append("Snapshots are read-only history; repair the active system instead")
            if subvol and subvol.read_only:
                warnings.append("This subvolume is read-only")
        identifier = candidate_id(node.uuid, rootflags, node.path)
        return {
            "id": identifier, "device": node.path, "node": node.id, "stack": stack_labels(graph, node), "fstype": node.fstype, "uuid": node.uuid, "label": node.label,
            "rootflags": rootflags, "subvol": subvol.to_dict() if subvol else None, "kind": subvol.kind if subvol else "root",
            "os": facts.os.to_dict(), "supported": supported, "init": facts.init, "arch": facts.arch, "extra": facts.extra, "boot": boot.to_dict(),
            "fstab": {"present": bool(fstab_text.strip()), "entries": len(fstab.entries), "dead": sum(1 for item in resolved if item.status in {"unresolved", "ambiguous"}),
                      "needs_unlock": sum(1 for item in resolved if item.status == "needs_unlock")},
            "confidence": "high" if facts.os.source in {"/etc/os-release", "/usr/lib/os-release"} else "medium" if facts.os.id != "linux" else "low",
            "degraded_view": bool(rung), "warnings": warnings, "busy_elsewhere": "",
        }


def stack_labels(graph: DeviceGraph, node: DeviceNode) -> list[str]:
    """['/dev/nvme0n1p3', 'luks:/dev/mapper/rescue-1f2e..', 'lvm:vg0/root'] from the bottom layer up."""
    labels = []
    for item in reversed(graph.stack_of(node)):
        if item.type == "crypt":
            labels.append(f"luks:{item.path}")
        elif item.type == "lvm":
            labels.append(f"lvm:{item.name}")
        elif item.type.startswith("raid"):
            labels.append(f"md:{item.path}")
        else:
            labels.append(item.path)
    return labels


def _hint(identifier: str, severity: str, text: str) -> dict[str, str]:
    return {"id": identifier, "severity": severity, "text": text}


def make_hints(graph: DeviceGraph, candidates: list[dict[str, Any]], env: Environment, stacks: dict[str, list[dict[str, Any]]]) -> list[dict[str, str]]:
    """Plain-language checklist for a scan that found nothing, or found something unexpected."""
    hints: list[dict[str, str]] = []
    gentoo = [item for item in candidates if item.get("supported")]
    disks = [node for node in graph if node.type == "disk" and not node.live_medium]
    if not disks:
        hints.append(_hint("no_disks", "warn", "No disks were detected. If this laptop's firmware is set to 'RAID On' or 'Intel RST', switch SATA mode to AHCI. NVMe disks behind Intel VMD are supported by this kernel; "
                           "check that they are not hidden by the firmware setting."))
    if candidates and not gentoo:
        hints.append(_hint("no_gentoo", "info", "No Gentoo system was found. The systems listed are other Linux distributions, and this tool only repairs Gentoo."))
    if disks and not gentoo:
        if any(node.role == "luks_locked" for node in graph):
            hints.append(_hint("locked_volumes", "info", "Encrypted volumes were found. Unlock them to look inside."))
        if any(node.role == "lvm_pv" for node in graph) and not any(group.get("active") for group in stacks.get("vgs", [])):
            hints.append(_hint("inactive_vg", "info", "Volume groups were found but are not active. Activate the one you need."))
        if any(node.role == "md_member" for node in graph) and not any(array.get("state") != "not_assembled" for array in stacks.get("arrays", [])):
            hints.append(_hint("md_members", "info", "RAID members were found. Assemble the array first."))
        unsupported = [node for node in graph if node.role == "unsupported" and not node.live_medium]
        for node in unsupported:
            hints.append(_hint("unsupported_fs", "warn", f"{node.path}: {node.unsupported_reason}"))
        if graph and all(node.role in {"data", "esp", "other", "empty", "swap", "live", "bios_boot"} or node.type in {"disk", "loop", "rom"} for node in graph) and any(node.role == "data" for node in graph):
            hints.append(_hint("only_windows", "info", "Only Windows-style volumes were found."))
    if any(node.hotplug and not node.live_medium for node in disks):
        hints.append(_hint("external_disks", "info", "External or USB disks are listed. If your system is on one, make sure it is connected before scanning."))
    has_esp = any(node.role == "esp" and not node.live_medium for node in graph)
    if not env.uefi and has_esp and any(node.pttype == "gpt" and not node.live_medium for node in disks):
        hints.append(_hint("fw_mismatch", "warn", "This live session started in legacy BIOS mode but the disk looks UEFI-installed. Boot entries cannot be written from here; file-level repairs with the fallback "
                           "boot path still work. For a full repair, start this medium in UEFI mode."))
    if env.uefi and any(node.pttype == "dos" and not node.live_medium for node in disks) and not has_esp:
        hints.append(_hint("fw_mismatch_bios", "info", "The disk looks like a legacy-BIOS install. After the repair, start the computer in BIOS/CSM mode."))
    return hints
