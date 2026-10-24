"""Block-device graph for rescue, built on the shared ``gentoo_installer.blockdev`` parser.

lsblk prints a device once under every parent it relates to (an md array under each member, a
logical volume under each physical volume), so nodes are merged by MAJ:MIN and the result is a
graph with parent and child sets, not a tree. Roles tell the scanner what each node is; the
live medium and every disk holding it is marked and never offered (docs/contracts.md).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from gentoo_installer.blockdev import LSBLK_COLUMNS, BlockDevice, live_medium_keys, parse_lsblk

LSBLK_EXTRA = ("PKNAME", "PARTN", "FSROOTS")

# GPT partition type GUIDs (lower case) and MBR types used for hints.
GPT_ESP = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
GPT_BIOS_BOOT = "21686148-6449-6e6f-744e-656564726976"
GPT_XBOOTLDR = "bc13c2ff-59e6-4262-a352-b275fd6f7172"
GPT_LINUX_FS = "0fc63daf-8483-4772-8e79-3d69d8477de4"
GPT_LINUX_ROOT_X86_64 = "4f68bce3-e8cd-4db1-96e7-fbcaf984b709"
GPT_LINUX_HOME = "933ac7e1-2eb4-4f13-b844-0e14e2aef915"
GPT_LINUX_SWAP = "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f"
GPT_LINUX_LVM = "e6d6d379-f507-44c2-a23c-238f2a3df928"
GPT_LINUX_RAID = "a19d880f-05fc-4d3b-a006-743f0f84911e"
GPT_LINUX_LUKS = "ca7d7ccb-63ed-4c53-861c-1742536059cc"
GPT_MS_BASIC = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
GPT_MS_RESERVED = "e3c9e316-0b5c-4db8-817d-f92df00215ae"
GPT_WINDOWS_RECOVERY = "de94bba4-06d1-4d40-a16a-bfd50179d6ac"
MBR_ESP = "0xef"

# Filesystems the scanner probe-mounts (review R16: no ntfs/exfat/hfsplus/udf/iso9660 parsing unless asked).
PROBE_FS = frozenset({"ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "jfs"})
MD_MEMBER_FS = frozenset({"linux_raid_member", "isw_raid_member", "ddf_raid_member"})
UNSUPPORTED_FS = {
    "reiserfs": "ReiserFS was removed from the Linux kernel in 6.13; boot an older live image (kernel 6.12 or earlier) to access this filesystem.",
    "reiser4": "Reiser4 is not supported by this live kernel.",
    "bcachefs": "This live kernel has no bcachefs support.",
    "zfs_member": "ZFS needs OpenZFS modules matching the live kernel; they are not available here.",
    "BitLocker": "This is a BitLocker volume (Windows); rescue does not handle it.",
    "ufs": "UFS (BSD) volumes are not supported.",
    "minix": "Minix volumes are not supported.",
}
LIVE_DM_NAMES = frozenset({"live-rw", "live-base", "live-osimg-min"})
PARTN_RE = re.compile(r"(?:p)?(\d+)$")


def rescue_lsblk_argv() -> tuple[str, ...]:
    """One lsblk call with every column rescue needs (the shared ones plus PKNAME, PARTN, FSROOTS)."""
    return ("lsblk", "--json", "--bytes", "--all", "--output", ",".join((*LSBLK_COLUMNS, *LSBLK_EXTRA)))


def extract_json(text: str) -> Any:
    """First JSON value in a tool's output (stderr warnings may be merged in front of it)."""
    start = min((index for index in (text.find("{"), text.find("[")) if index >= 0), default=-1)
    if start < 0:
        raise ValueError("No JSON document in the command output")
    value, _end = json.JSONDecoder().raw_decode(text[start:])
    return value


@dataclass
class DeviceNode:
    key: str
    path: str
    kname: str
    majmin: str
    type: str
    size: int = 0
    ro: bool = False
    rm: bool = False
    hotplug: bool = False
    tran: str = ""
    model: str = ""
    serial: str = ""
    wwn: str = ""
    fstype: str = ""
    fsver: str = ""
    label: str = ""
    uuid: str = ""
    partuuid: str = ""
    partlabel: str = ""
    parttype: str = ""
    partn: int | None = None
    pttype: str = ""
    ptuuid: str = ""
    mountpoints: tuple[str, ...] = ()
    fsroots: tuple[str, ...] = ()
    parents: tuple[str, ...] = ()
    children: tuple[str, ...] = ()
    role: str = "other"
    live_medium: bool = False
    unsupported_reason: str = ""
    probe_error: str = ""

    @property
    def id(self) -> str:
        return "d" + (self.kname or self.path.rsplit("/", 1)[-1])

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "path": self.path, "kname": self.kname, "majmin": self.majmin, "type": self.type, "size": self.size, "ro": self.ro, "removable": self.rm,
            "hotplug": self.hotplug, "tran": self.tran, "model": self.model, "serial": self.serial, "wwn": self.wwn, "fstype": self.fstype, "fsver": self.fsver,
            "label": self.label, "uuid": self.uuid, "partuuid": self.partuuid, "partlabel": self.partlabel, "parttype": self.parttype, "partn": self.partn,
            "pttype": self.pttype, "mountpoints": list(self.mountpoints), "role": self.role, "live_medium": self.live_medium,
            "unsupported_reason": self.unsupported_reason, "probe_error": self.probe_error,
        }


class DeviceGraph:
    def __init__(self, nodes: Iterable[DeviceNode]) -> None:
        self.nodes: dict[str, DeviceNode] = {node.key: node for node in nodes}
        self._by_path: dict[str, DeviceNode] = {}
        for node in self.nodes.values():
            self._by_path.setdefault(node.path, node)
            if node.kname:
                self._by_path.setdefault("/dev/" + node.kname, node)

    def __iter__(self) -> Iterator[DeviceNode]:
        return iter(self.nodes.values())

    def __len__(self) -> int:
        return len(self.nodes)

    def get(self, key: str) -> DeviceNode | None:
        return self.nodes.get(key)

    def find(self, device: str) -> DeviceNode:
        """The node for a /dev path, a node id, or a by-* link (LookupError when absent)."""
        if device in self._by_path:
            return self._by_path[device]
        for node in self.nodes.values():
            if node.id == device or node.key == device:
                return node
        resolved = os.path.realpath(device)  # host-path: a /dev link named by the caller
        if resolved in self._by_path:
            return self._by_path[resolved]
        raise LookupError(f"Block device not found: {device}")

    def parents_of(self, node: DeviceNode) -> list[DeviceNode]:
        return [self.nodes[key] for key in node.parents if key in self.nodes]

    def children_of(self, node: DeviceNode) -> list[DeviceNode]:
        return [self.nodes[key] for key in node.children if key in self.nodes]

    def ancestors(self, node: DeviceNode) -> list[DeviceNode]:
        seen: dict[str, DeviceNode] = {}
        stack = list(self.parents_of(node))
        while stack:
            current = stack.pop()
            if current.key in seen:
                continue
            seen[current.key] = current
            stack.extend(self.parents_of(current))
        return list(seen.values())

    def descendants(self, node: DeviceNode) -> list[DeviceNode]:
        seen: dict[str, DeviceNode] = {}
        stack = list(self.children_of(node))
        while stack:
            current = stack.pop()
            if current.key in seen:
                continue
            seen[current.key] = current
            stack.extend(self.children_of(current))
        return list(seen.values())

    def disks(self) -> list[DeviceNode]:
        return [node for node in self if node.type == "disk"]

    def parent_disks(self, node: DeviceNode) -> list[DeviceNode]:
        """Whole disks below which this node lives (a node can sit on several disks)."""
        return [item for item in ([node] + self.ancestors(node)) if item.type in {"disk", "loop"} and not self.parents_of(item)]

    def by_uuid(self, uuid: str, *, include_logs: bool = False) -> list[DeviceNode]:
        """Devices with this UUID. An XFS external log carries the UUID of the file system it belongs to; it is never what fstab means."""
        wanted = uuid.lower()
        return [node for node in self if node.uuid and node.uuid.lower() == wanted and (include_logs or node.fstype != "xfs_external_log")]

    def by_label(self, label: str) -> list[DeviceNode]:
        return [node for node in self if node.label and node.label == label]

    def by_partuuid(self, partuuid: str) -> list[DeviceNode]:
        wanted = partuuid.lower()
        return [node for node in self if node.partuuid and node.partuuid.lower() == wanted]

    def by_partlabel(self, partlabel: str) -> list[DeviceNode]:
        return [node for node in self if node.partlabel and node.partlabel == partlabel]

    def stack_of(self, node: DeviceNode) -> list[DeviceNode]:
        """The node plus everything it is built on, excluding whole disks (the partitions/mappings the session opens)."""
        return [node] + [item for item in self.ancestors(node) if item.type not in {"disk", "loop"} or self.parents_of(item)]

    def siblings_on_disks(self, nodes: Iterable[DeviceNode]) -> list[DeviceNode]:
        """Partitions that share a disk with the given stack but are not part of it."""
        stack_keys = {node.key for node in nodes}
        found: dict[str, DeviceNode] = {}
        for node in nodes:
            for disk in self.parent_disks(node):
                for child in self.descendants(disk):
                    if child.type == "part" and child.key not in stack_keys:
                        found[child.key] = child
        return list(found.values())

    def to_dict_list(self) -> list[dict[str, Any]]:
        return [node.to_dict() for node in self]


def _walk_raw(items: list[dict[str, Any]], out: dict[str, dict[str, Any]]) -> None:
    for item in items:
        key = str(item.get("maj:min") or item.get("kname") or item.get("name") or "")
        out.setdefault(key, item)
        _walk_raw(item.get("children") or [], out)


def _norm_str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def build_graph(text: str, *, live_sources: Iterable[str] = (), cmdline: str = "") -> DeviceGraph:
    """DeviceGraph from lsblk JSON; ``live_sources`` are the devices backing the live system (findmnt)."""
    document = extract_json(text)
    roots = parse_lsblk(json.dumps(document))
    raw: dict[str, dict[str, Any]] = {}
    _walk_raw(document.get("blockdevices") or [], raw)
    nodes: dict[str, DeviceNode] = {}
    parents: dict[str, list[str]] = {}
    children: dict[str, list[str]] = {}

    def visit(block: BlockDevice, parent: str | None) -> None:
        key = block.key
        extras = raw.get(block.maj_min or block.kname or block.name, {})
        if key not in nodes:
            partn = extras.get("partn")
            if partn in (None, "") and block.type == "part":
                match = PARTN_RE.search(block.name)
                partn = int(match.group(1)) if match else None
            nodes[key] = DeviceNode(
                key=key, path=block.path, kname=block.kname or block.name, majmin=block.maj_min, type=block.type.lower(), size=block.size, ro=block.ro, rm=block.removable,
                hotplug=block.hotplug, tran=block.transport, model=block.model, serial=block.serial, wwn=block.wwn, fstype=block.fstype, fsver=block.fsver, label=block.label,
                uuid=block.uuid, partuuid=block.partuuid, partlabel=block.partlabel, parttype=block.parttype.lower(), partn=int(partn) if partn not in (None, "") else None,
                pttype=block.pttype, ptuuid=block.ptuuid, mountpoints=tuple(m for m in block.mountpoints if m),
                fsroots=tuple(_norm_str(item) for item in (extras.get("fsroots") or []) if item),
            )
            parents[key] = []
            children[key] = []
        if parent is not None and parent not in parents[key]:
            parents[key].append(parent)
            if key not in children[parent]:
                children[parent].append(key)
        for child in block.children:
            visit(child, key)

    for root in roots:
        visit(root, None)
    for key, node in nodes.items():
        node.parents = tuple(parents[key])
        node.children = tuple(children[key])
    graph = DeviceGraph(nodes.values())
    _mark_live(graph, roots, live_sources, cmdline)
    for node in graph:
        _classify(node, graph)
    return graph


def _mark_live(graph: DeviceGraph, roots: list[BlockDevice], sources: Iterable[str], cmdline: str) -> None:
    marked = set(live_medium_keys(roots, sources=list(sources), cmdline=cmdline))
    for node in graph:  # dracut's dmsquash-live plumbing: the loop devices under live-rw/live-base are the medium's files
        if node.name in LIVE_DM_NAMES or node.name.startswith("live-"):
            marked.add(node.key)
            marked.update(parent.key for parent in graph.ancestors(node))
    for key in list(marked):
        node = graph.get(key)
        if node is not None and node.type == "disk":
            marked.update(child.key for child in graph.descendants(node))  # a hybrid USB stick: every partition inherits
    for key in marked:
        node = graph.get(key)
        if node is not None:
            node.live_medium = True


def reclassify(graph: DeviceGraph) -> None:
    """Recompute roles after a node's signature changed (blkid filled in a missing file system type)."""
    for node in graph:
        _classify(node, graph)


EXTENDED_PARTITION_TYPES = frozenset({"0x5", "0xf", "0x85"})


def _classify(node: DeviceNode, graph: DeviceGraph) -> None:
    fstype = node.fstype
    child_types = {child.type for child in graph.children_of(node)}
    node.unsupported_reason = UNSUPPORTED_FS.get(fstype, "")
    if node.live_medium:
        node.role = "live"
    elif node.type == "rom" or fstype in {"iso9660", "udf"} or node.parttype in EXTENDED_PARTITION_TYPES:
        node.role = "other"
    elif fstype == "crypto_LUKS":
        node.role = "luks_unlocked" if "crypt" in child_types else "luks_locked"
    elif fstype == "LVM2_member":
        node.role = "lvm_pv"
    elif fstype in MD_MEMBER_FS:
        node.role = "md_member"
    elif fstype.endswith("_raid_member"):
        node.role = "dmraid_member"
    elif fstype == "swap":
        node.role = "swap"
    elif fstype in PROBE_FS:
        node.role = "filesystem"
    elif node.unsupported_reason:
        node.role = "unsupported"
    elif node.parttype in {GPT_ESP, MBR_ESP} and fstype in {"vfat", ""}:
        node.role = "esp"
    elif node.parttype == GPT_BIOS_BOOT:
        node.role = "bios_boot"
    elif node.parttype == GPT_XBOOTLDR:
        node.role = "xbootldr"
    elif fstype in {"vfat", "ntfs", "exfat", "hfsplus", "ntfs3"} or node.parttype in {GPT_MS_BASIC, GPT_WINDOWS_RECOVERY}:
        node.role = "data"
    elif node.parttype == GPT_MS_RESERVED:
        node.role = "other"
    elif not fstype and node.type in {"part", "lvm", "crypt"} and not node.children:
        node.role = "empty"
    else:
        node.role = "other"
