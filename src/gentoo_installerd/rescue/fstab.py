"""fstab and crypttab: a line-preserving parser, device resolution and safe repair proposals.

Nothing here touches the disk. ``Fstab.render()`` reproduces comments and untouched entries
byte for byte so a repair is a minimal diff. Proposals follow review R13: only the root entry,
an exact swap signature and a single EFI-typed partition may be preselected; every other dead
entry lists its candidates unselected and defaults to ``nofail``.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .devices import GPT_ESP, MBR_ESP, DeviceGraph, DeviceNode

PSEUDO_FS = frozenset({
    "proc", "sysfs", "devpts", "tmpfs", "devtmpfs", "cgroup", "cgroup2", "securityfs", "debugfs", "tracefs", "mqueue", "hugetlbfs", "ramfs", "efivarfs", "bpf",
    "fusectl", "configfs", "pstore", "overlay", "autofs", "binfmt_misc", "rpc_pipefs", "nsfs", "selinuxfs", "squashfs",
})
NETWORK_FS = frozenset({"nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "ceph", "glusterfs", "afs", "ncpfs", "davfs"})
EXT_FAMILY = frozenset({"ext2", "ext3", "ext4"})
UNSTABLE_NAME_RE = re.compile(r"^/dev/(?:sd[a-z]+\d*|hd[a-z]+\d*|vd[a-z]+\d*|xvd[a-z]+\d*|nvme\d+n\d+(?:p\d+)?|mmcblk\d+(?:p\d+)?)$")
OCTAL_RE = re.compile(r"\\([0-7]{3})")
UUID_SPEC_RE = re.compile(r"^UUID=([A-Za-z0-9:-]{4,64})$")
FIELD_RE = re.compile(r"\S+")


def decode_octal(token: str) -> str:
    return OCTAL_RE.sub(lambda m: chr(int(m.group(1), 8)), token)


def encode_octal(token: str) -> str:
    return token.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011").replace("\n", "\\012")


@dataclass
class Entry:
    spec: str
    file: str
    vfstype: str
    opts: str
    dump: str
    passno: str
    lineno: int
    raw: str

    @property
    def options(self) -> list[str]:
        return [item for item in self.opts.split(",") if item]

    def option_value(self, name: str) -> str | None:
        for item in self.options:
            key, eq, value = item.partition("=")
            if key == name:
                return value if eq else ""
        return None

    def has_option(self, name: str) -> bool:
        return self.option_value(name) is not None

    @property
    def is_swap(self) -> bool:
        return self.vfstype == "swap"


@dataclass
class FstabLine:
    raw: str
    entry: Entry | None = None


class Fstab:
    def __init__(self, lines: list[FstabLine], trailing_newline: bool = True) -> None:
        self.lines = lines
        self.trailing_newline = trailing_newline

    @classmethod
    def parse(cls, text: str) -> Fstab:
        trailing = text.endswith("\n") or not text
        physical = text.split("\n")
        if physical and physical[-1] == "":
            physical.pop()  # the terminating newline is remembered, not a line
        lines: list[FstabLine] = []
        for number, raw in enumerate(physical, 1):
            stripped = raw.strip()
            parts = FIELD_RE.findall(raw)
            if not stripped or stripped.startswith("#") or len(parts) < 3:
                lines.append(FstabLine(raw))
                continue
            fields = parts[:6] + ["defaults", "0", "0"][len(parts) - 3:] if len(parts) < 6 else parts[:6]
            entry = Entry(decode_octal(fields[0]), decode_octal(fields[1]), fields[2], fields[3], fields[4], fields[5], number, raw)
            lines.append(FstabLine(raw, entry))
        return cls(lines, trailing)

    @property
    def entries(self) -> list[Entry]:
        return [line.entry for line in self.lines if line.entry]

    def entry_at(self, lineno: int) -> Entry:
        for entry in self.entries:
            if entry.lineno == lineno:
                return entry
        raise KeyError(lineno)

    def render(self) -> str:
        text = "\n".join(line.raw for line in self.lines)
        return text + "\n" if self.trailing_newline and self.lines else text

    # ---- minimal, layout-preserving edits ---------------------------------------------------------
    def _line(self, lineno: int) -> FstabLine:
        for line in self.lines:
            if line.entry and line.entry.lineno == lineno:
                return line
        raise KeyError(lineno)

    def replace_spec(self, lineno: int, new_spec: str) -> None:
        line = self._line(lineno)
        spans = [m.span() for m in FIELD_RE.finditer(line.raw)]
        start, end = spans[0]
        line.raw = line.raw[:start] + encode_octal(new_spec) + line.raw[end:]
        assert line.entry is not None
        line.entry.spec = new_spec
        line.entry.raw = line.raw

    def add_option(self, lineno: int, option: str) -> None:
        line = self._line(lineno)
        assert line.entry is not None
        if line.entry.has_option(option.split("=", 1)[0]):
            return
        spans = [m.span() for m in FIELD_RE.finditer(line.raw)]
        if len(spans) >= 4:
            start, end = spans[3]
            value = f"{line.raw[start:end]},{option}"
            line.raw = line.raw[:start] + value + line.raw[end:]
        else:  # the options column was omitted
            value = f"defaults,{option}"
            line.raw = line.raw.rstrip() + ("\t" if "\t" in line.raw else " ") + value
        line.entry.opts = value
        line.entry.raw = line.raw

    def comment_out(self, lineno: int, note: str = "disabled by gentoo-rescue: device not found") -> None:
        line = self._line(lineno)
        line.raw = f"# {note}\n# {line.raw}"
        line.entry = None
        # Embedded newline: render() keeps the pair as two physical lines.


def unified_diff(old: str, new: str, name: str = "etc/fstab") -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=f"a/{name}", tofile=f"b/{name}"))


# ---- crypttab ---------------------------------------------------------------------------------------------
@dataclass
class CryptEntry:
    name: str
    source: str
    key: str
    options: str
    lineno: int

    @property
    def keyfile(self) -> str:
        return "" if self.key in {"", "none", "-"} else self.key


def parse_crypttab(text: str) -> list[CryptEntry]:
    result = []
    for number, raw in enumerate(text.splitlines(), 1):
        parts = raw.split()
        if not parts or parts[0].startswith("#") or len(parts) < 2:
            continue
        parts += [""] * (4 - len(parts))
        result.append(CryptEntry(parts[0], decode_octal(parts[1]), parts[2], parts[3], number))
    return result


# ---- resolution ---------------------------------------------------------------------------------------------
@dataclass
class Resolved:
    entry: Entry
    status: str  # resolved | unresolved | ambiguous | needs_unlock | skipped
    kind: str = ""
    devices: list[DeviceNode] = field(default_factory=list)
    reason: str = ""
    unstable_name: bool = False
    type_mismatch: str = ""
    container: str = ""  # LUKS container behind a needs_unlock entry

    @property
    def device(self) -> DeviceNode | None:
        return self.devices[0] if self.devices else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "line": self.entry.lineno, "spec": self.entry.spec, "mountpoint": self.entry.file, "fstype": self.entry.vfstype, "status": self.status, "kind": self.kind,
            "devices": [node.path for node in self.devices], "reason": self.reason, "unstable_name": self.unstable_name, "type_mismatch": self.type_mismatch,
            "container": self.container,
        }


DevLinkResolver = Callable[[str], "str | None"]


def default_dev_link_resolver(path: str) -> str | None:
    resolved = os.path.realpath(path)  # host-path: a /dev/disk link of the live system
    return resolved if resolved != path or os.path.exists(path) else None


def _unescape_label(text: str) -> str:
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), text)


def _unquote(value: str) -> str:
    """fstab(5) allows TAG="value" (blkid prints it that way): one pair of matching quotes is not part of the value."""
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" else value


def _fs_compatible(entry_type: str, node: DeviceNode) -> bool:
    if entry_type in {"auto", "", "none"} or not node.fstype:
        return True
    if entry_type in EXT_FAMILY and node.fstype in EXT_FAMILY:
        return True
    if entry_type in {"vfat", "msdos", "fat", "fat32"} and node.fstype == "vfat":
        return True
    if entry_type in {"ntfs", "ntfs3", "ntfs-3g", "fuseblk"} and node.fstype == "ntfs":
        return True
    return entry_type == node.fstype


def _mapper_name(vg: str, lv: str) -> str:
    return vg.replace("-", "--") + "-" + lv.replace("-", "--")


def resolve_entry(entry: Entry, graph: DeviceGraph, *, crypttab: Iterable[CryptEntry] = (), dev_links: DevLinkResolver | None = None) -> Resolved:
    spec = entry.spec
    if entry.vfstype in PSEUDO_FS or spec in {"none", "proc", "sysfs", "tmpfs", "devpts"} and entry.vfstype != "swap":
        return Resolved(entry, "skipped", "pseudo", reason="virtual filesystem")
    if entry.vfstype in NETWORK_FS or re.match(r"^[\w.-]+:/", spec) or spec.startswith("//"):
        return Resolved(entry, "skipped", "network", reason="network filesystem")
    if entry.has_option("bind") or entry.has_option("rbind"):
        return Resolved(entry, "skipped", "bind", reason="bind mount")
    links = dev_links or default_dev_link_resolver
    kind, devices, reason = "other", [], ""
    unstable = False
    lowered = spec.lower()
    value = _unquote(spec.split("=", 1)[1]) if "=" in spec else ""
    if lowered.startswith("uuid="):
        kind, devices = "uuid", graph.by_uuid(value)
        reason = "no device has this UUID"
    elif lowered.startswith("label="):
        kind, devices = "label", graph.by_label(value)
        reason = "no device has this label"
    elif lowered.startswith("partuuid="):
        kind, devices = "partuuid", graph.by_partuuid(value)
        reason = "no partition has this PARTUUID"
    elif lowered.startswith("partlabel="):
        kind, devices = "partlabel", graph.by_partlabel(value)
        reason = "no partition has this PARTLABEL"
    elif spec.startswith("/dev/disk/by-"):
        kind, devices, reason = _resolve_by_link(spec, graph, links)
    elif spec.startswith("/dev/mapper/"):
        kind, devices, reason, container = _resolve_mapper(spec, graph, crypttab, links)
        if container:
            return Resolved(entry, "needs_unlock", kind, [], f"{spec} needs the encrypted volume {container} to be unlocked", container=container)
    elif re.match(r"^/dev/[^/]+/[^/]+$", spec) and not spec.startswith(("/dev/md", "/dev/mapper")) and not UNSTABLE_NAME_RE.match(spec):
        vg, lv = spec[5:].split("/", 1)
        kind = "lv"
        devices = [node for node in graph if node.path == "/dev/mapper/" + _mapper_name(vg, lv)]
        reason = f"logical volume {vg}/{lv} is not active"
    elif spec.startswith("/dev/"):
        kind = "md" if spec.startswith("/dev/md") else "kernel"
        devices = [node for node in graph if node.path == spec or "/dev/" + node.kname == spec]
        unstable = bool(UNSTABLE_NAME_RE.match(spec))
        reason = "no such device"
    if not devices:
        return Resolved(entry, "unresolved", kind, [], reason or "unrecognised device specification", unstable_name=unstable)
    unique = {node.key: node for node in devices}
    if len(unique) > 1:
        if len({node.fstype for node in unique.values()}) == 1 and next(iter(unique.values())).fstype == "btrfs":
            members = list(unique.values())  # members of one multi-device btrfs share a UUID legitimately
            return Resolved(entry, "resolved", kind, members, "", unstable_name=unstable)
        return Resolved(entry, "ambiguous", kind, list(unique.values()), "several devices match (cloned disks?)", unstable_name=unstable)
    node = next(iter(unique.values()))
    mismatch = "" if _fs_compatible(entry.vfstype, node) else f"fstab says {entry.vfstype}, the device holds {node.fstype}"
    return Resolved(entry, "resolved", kind, [node], "", unstable_name=unstable, type_mismatch=mismatch)


def _resolve_by_link(spec: str, graph: DeviceGraph, links: DevLinkResolver) -> tuple[str, list[DeviceNode], str]:
    group, _, name = spec[len("/dev/disk/by-"):].partition("/")
    name = _unescape_label(name)
    if group == "uuid":
        return "uuid", graph.by_uuid(name), "no device has this UUID"
    if group == "label":
        return "label", graph.by_label(name), "no device has this label"
    if group == "partuuid":
        return "partuuid", graph.by_partuuid(name), "no partition has this PARTUUID"
    if group == "partlabel":
        return "partlabel", graph.by_partlabel(name), "no partition has this PARTLABEL"
    target = links(spec)
    if target:
        try:
            return "by_" + group, [graph.find(target)], ""
        except LookupError:
            pass
    return "by_" + group, [], f"no device is linked as {spec} on this computer"


def _resolve_mapper(spec: str, graph: DeviceGraph, crypttab: Iterable[CryptEntry], links: DevLinkResolver) -> tuple[str, list[DeviceNode], str, str]:
    exact = [node for node in graph if node.path == spec]
    if exact:
        return "mapper", exact, "", ""
    name = spec.rsplit("/", 1)[-1]
    for crypt in crypttab:
        if crypt.name != name:
            continue
        container = _resolve_crypt_source(crypt.source, graph, links)
        if container is None:
            return "mapper", [], f"the encrypted volume for {name} ({crypt.source}) is not present", ""
        mappings = [child for child in graph.children_of(container) if child.type == "crypt"]
        if mappings:
            return "mapper", [mappings[0]], "", ""
        return "mapper", [], "", container.path
    return "mapper", [], f"{spec} does not exist (LVM volume not active, or encrypted volume not unlocked)", ""


def _resolve_crypt_source(source: str, graph: DeviceGraph, links: DevLinkResolver) -> DeviceNode | None:
    lowered = source.lower()
    value = _unquote(source.split("=", 1)[1]) if "=" in source else ""
    candidates: list[DeviceNode] = []
    if lowered.startswith("uuid="):
        candidates = graph.by_uuid(value)
    elif lowered.startswith("partuuid="):
        candidates = graph.by_partuuid(value)
    elif lowered.startswith("label="):
        candidates = graph.by_label(value)
    elif source.startswith("/dev/disk/by-"):
        _kind, candidates, _reason = _resolve_by_link(source, graph, links)
    elif source.startswith("/dev/"):
        try:
            candidates = [graph.find(source)]
        except LookupError:
            candidates = []
    return candidates[0] if len(candidates) == 1 else None


def resolve_all(fstab: Fstab, graph: DeviceGraph, *, crypttab: Iterable[CryptEntry] = (), dev_links: DevLinkResolver | None = None) -> list[Resolved]:
    crypt = list(crypttab)
    return [resolve_entry(entry, graph, crypttab=crypt, dev_links=dev_links) for entry in fstab.entries]


# ---- repair proposals (review R13) --------------------------------------------------------------------------------
@dataclass
class Candidate:
    device: str
    node_id: str
    spec: str
    score: int
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"device": self.device, "node": self.node_id, "spec": self.spec, "score": self.score, "reasons": self.reasons}


@dataclass
class Proposal:
    lineno: int
    mountpoint: str
    old_spec: str
    status: str
    reason: str
    candidates: list[Candidate] = field(default_factory=list)
    selected: str = ""  # a UUID= spec the tool may preselect (root, exact swap, single ESP) or ""
    selected_why: str = ""
    default_action: str = "nofail"

    def to_dict(self) -> dict[str, Any]:
        return {
            "line": self.lineno, "mountpoint": self.mountpoint, "old_spec": self.old_spec, "status": self.status, "reason": self.reason,
            "candidates": [item.to_dict() for item in self.candidates], "selected": self.selected, "selected_why": self.selected_why, "default_action": self.default_action,
        }


def uuid_spec(node: DeviceNode) -> str:
    return f"UUID={node.uuid}" if node.uuid else (f"PARTUUID={node.partuuid}" if node.partuuid else "")


ESP_MOUNTPOINTS = frozenset({"/efi", "/boot/efi", "/boot/EFI", "/esp"})
SIZE_RANGES = {"esp": (32 << 20, 2 << 30), "boot": (100 << 20, 4 << 30), "swap": (16 << 20, 1 << 50)}


def _score(entry: Entry, node: DeviceNode, graph: DeviceGraph, root_disks: set[str], esp_ok: bool) -> Candidate:
    score = 0
    reasons: list[str] = []
    mountpoint = entry.file
    label_hint = os.path.basename(mountpoint.rstrip("/")).lower()
    if entry.vfstype in {"auto", ""} or _fs_compatible(entry.vfstype, node):
        score += 50
        reasons.append(f"same filesystem type ({node.fstype})")
    if root_disks & {disk.key for disk in graph.parent_disks(node)}:
        score += 20
        reasons.append("on the same disk as the root")
    fits = (entry.is_swap and node.fstype == "swap") or (mountpoint in ESP_MOUNTPOINTS and esp_ok) or (mountpoint == "/boot" and node.fstype in {"ext2", "ext3", "ext4", "xfs", "vfat"})
    if fits:
        score += 15
        reasons.append("looks like the right kind of partition")
    spec_value = entry.spec.split("=", 1)[1] if entry.spec.upper().startswith("LABEL=") else ""
    if spec_value and node.label and node.label.lower().replace(" ", "") == spec_value.lower().replace(" ", ""):
        score += 40
        reasons.append("same label apart from case")
    elif label_hint and node.label and label_hint in node.label.lower():
        score += 10
        reasons.append(f"label mentions {label_hint}")
    low_high = SIZE_RANGES.get("esp" if mountpoint in ESP_MOUNTPOINTS else "swap" if entry.is_swap else "boot" if mountpoint == "/boot" else "", None)
    if low_high and low_high[0] <= node.size <= low_high[1]:
        score += 5
        reasons.append("plausible size")
    return Candidate(node.path, node.id, uuid_spec(node), score, reasons)


def propose_fixes(
    fstab: Fstab,
    resolved: Iterable[Resolved],
    graph: DeviceGraph,
    *,
    root_node: DeviceNode | None,
    esp_has_efi_dir: dict[str, bool] | None = None,
) -> list[Proposal]:
    """One proposal per entry that cannot be mounted; see the module docstring for the selection rules."""
    resolved = list(resolved)
    used = {node.key for item in resolved if item.status == "resolved" for node in item.devices}
    if root_node is not None:
        used.add(root_node.key)
    root_disks = {disk.key for disk in graph.parent_disks(root_node)} if root_node is not None else set()
    probe = esp_has_efi_dir or {}
    proposals: list[Proposal] = []
    for item in resolved:
        entry = item.entry
        if item.status in {"resolved", "skipped"} and not item.type_mismatch:
            continue
        if item.status == "needs_unlock":
            continue
        if item.status == "resolved" and item.type_mismatch:
            proposals.append(Proposal(entry.lineno, entry.file, entry.spec, "type_mismatch", item.type_mismatch, default_action="keep"))
            continue
        proposal = Proposal(entry.lineno, entry.file, entry.spec, item.status, item.reason)
        if entry.file == "/" and root_node is not None and uuid_spec(root_node):
            proposal.selected = uuid_spec(root_node)
            proposal.selected_why = "this is the filesystem you mounted as the root"
            proposal.default_action = "replace"
            proposals.append(proposal)
            continue
        pool = [
            node for node in graph
            if node.key not in used and not node.live_medium and node.role in {"filesystem", "esp", "swap", "data"} and node.fstype
            and (node.fstype == "swap" if entry.is_swap else entry.vfstype in {"auto", ""} or _fs_compatible(entry.vfstype, node))
        ]
        candidates = [_score(entry, node, graph, root_disks, node.role == "esp") for node in pool if uuid_spec(node)]
        candidates.sort(key=lambda c: (-c.score, c.device))
        proposal.candidates = candidates[:8]
        _maybe_select(proposal, entry, pool, graph, root_disks, probe)
        if not proposal.selected:  # a drive marked nofail or noauto is just not plugged in: nothing needs to change, and its automount is not ours to switch off
            proposal.default_action = "keep" if entry.has_option("nofail") or entry.has_option("noauto") else "nofail"
        proposals.append(proposal)
    return proposals


def _maybe_select(proposal: Proposal, entry: Entry, pool: list[DeviceNode], graph: DeviceGraph, root_disks: set[str], probe: dict[str, bool]) -> None:
    if entry.is_swap:
        swaps = [node for node in pool if node.fstype == "swap" and uuid_spec(node)]
        if len(swaps) == 1:
            proposal.selected = uuid_spec(swaps[0])
            proposal.selected_why = "the only swap area that no other entry uses (exact signature)"
            proposal.default_action = "replace"
        return
    if entry.file in ESP_MOUNTPOINTS or (entry.file == "/boot" and entry.vfstype == "vfat"):
        typed = [node for node in pool if node.parttype in {GPT_ESP, MBR_ESP} and node.fstype == "vfat" and uuid_spec(node)]
        typed = [node for node in typed if not root_disks or root_disks & {d.key for d in graph.parent_disks(node)}]
        typed = [node for node in typed if probe.get(node.key, True)]
        if len(typed) == 1:
            proposal.selected = uuid_spec(typed[0])
            proposal.selected_why = "the only EFI system partition on the disk of this system"
            proposal.default_action = "replace"


def apply_choices(fstab: Fstab, choices: dict[int, dict[str, str]], graph: DeviceGraph) -> Fstab:
    """A copy of ``fstab`` with the user's per-line choices applied.

    ``{"action": "replace", "spec": "UUID=..."}`` is accepted only when that UUID belongs to a device
    in the graph; ``nofail`` adds the option, ``comment`` disables the line, ``keep`` changes nothing.
    """
    updated = Fstab.parse(fstab.render())
    for lineno, choice in sorted(choices.items()):
        action = choice.get("action", "keep")
        if action == "keep":
            continue
        try:
            entry = updated.entry_at(lineno)
        except KeyError as exc:
            raise ValueError(f"fstab line {lineno} does not exist") from exc
        if action == "replace":
            spec = choice.get("spec", "")
            match = UUID_SPEC_RE.match(spec)
            if not match or not graph.by_uuid(match.group(1)):
                raise ValueError(f"{spec!r} is not the UUID of a device on this computer")
            updated.replace_spec(lineno, spec)
        elif action == "nofail":
            updated.add_option(lineno, "nofail")
        elif action == "comment":
            updated.comment_out(lineno)
        elif action == "unstable_to_uuid":
            node = graph.find(entry.spec)
            if not uuid_spec(node):
                raise ValueError(f"{entry.spec} has no UUID")
            updated.replace_spec(lineno, uuid_spec(node))
        else:
            raise ValueError(f"Unknown fstab action {action!r}")
    return updated
