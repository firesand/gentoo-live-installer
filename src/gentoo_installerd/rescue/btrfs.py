"""btrfs subvolume layouts: which subvolume is the system, and which are snapshots (review R10).

The default subvolume is what the installed system itself boots (openSUSE depends on this), so
it is the root candidate and stays writable unless the filesystem says it is read-only. Only
other subvolumes that match a snapshot pattern are snapshot candidates.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .devices import extract_json
from .errors import RescueError
from .osrelease import read_os_info
from .paths import RootFS
from .runner import RescueRunner

SNAPSHOT_RE = re.compile(r"(?:^|/)\.snapshots/\d+/snapshot$|(?:^|/)timeshift-btrfs/snapshots/[^/]+/[^/]+$")
NESTED_SKIP = ("var/lib/docker/", "var/lib/containers/", "var/lib/machines/", "var/lib/portables/")
LIST_LINE_RE = re.compile(r"^ID (\d+) gen \d+(?: cgen \d+)?(?: parent \d+)? top level (\d+)(?:.*?)? path (.+)$")
DEFAULT_RE = re.compile(r"^ID (\d+)(?: gen \d+ top level \d+ path (.+)|\s+\(FS_TREE\).*)$")
FS_TREE = "<FS_TREE>/"
MAX_TESTS = 300
MAX_SNAPSHOT_TESTS = 20
MAX_DEPTH = 3


@dataclass
class Subvolume:
    id: int
    path: str
    top_level: int = 5

    @property
    def depth(self) -> int:
        return self.path.count("/") + 1 if self.path else 0

    @property
    def is_snapshot(self) -> bool:
        return bool(SNAPSHOT_RE.search(self.path))


@dataclass
class BtrfsRoot:
    subvolume: Subvolume
    kind: str  # "root" | "snapshot"
    default: bool = False
    read_only: bool | None = None

    @property
    def rootflags(self) -> str:
        return f"subvolid={self.subvolume.id}"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.subvolume.id, "path": self.subvolume.path or "/", "default": self.default, "kind": self.kind, "read_only": self.read_only}


def parse_list_text(text: str) -> list[Subvolume]:
    result = []
    for line in text.splitlines():
        match = LIST_LINE_RE.match(line.strip())
        if match:
            path = match.group(3)
            result.append(Subvolume(int(match.group(1)), path[len(FS_TREE):] if path.startswith(FS_TREE) else path, int(match.group(2))))
    return result


def parse_list_json(text: str) -> list[Subvolume]:
    document = extract_json(text)
    items: list[Any] = []
    if isinstance(document, dict):
        for key in ("subvolume-list", "subvolumes", "subvolume_list"):
            if isinstance(document.get(key), list):
                items = document[key]
    elif isinstance(document, list):
        items = document
    result = []
    for item in items:
        if isinstance(item, dict) and "id" in item and "path" in item:
            path = str(item["path"])
            result.append(Subvolume(int(item["id"]), path[len(FS_TREE):] if path.startswith(FS_TREE) else path, int(item.get("top_level") or item.get("parent") or 5)))
    return result


def parse_default(text: str) -> int:
    match = DEFAULT_RE.match(text.strip())
    return int(match.group(1)) if match else 5


def list_subvolumes(runner: RescueRunner, mountpoint: str) -> tuple[list[Subvolume], int]:
    """(subvolumes, default subvolume id); JSON first, the text format when this btrfs-progs cannot do JSON."""
    subvolumes: list[Subvolume] = []
    result = runner.execute(("btrfs", "--format", "json", "subvolume", "list", "-a", mountpoint), timeout=30)
    if result.returncode == 0:
        try:
            subvolumes = parse_list_json(result.stdout)
        except (ValueError, json.JSONDecodeError):
            subvolumes = []
    if not subvolumes:
        text = runner.execute(("btrfs", "subvolume", "list", "-a", mountpoint), timeout=30)
        if text.returncode == 0:
            subvolumes = parse_list_text(text.stdout)
    default = 5
    result = runner.execute(("btrfs", "subvolume", "get-default", mountpoint), timeout=15)
    if result.returncode == 0:
        default = parse_default(result.stdout)
    return subvolumes, default


def looks_like_root(fs: RootFS) -> bool:
    """A system root has an os-release (or a legacy release file) or an fstab beside /bin."""
    return read_os_info(fs) is not None


def _is_nested_storage(path: str) -> bool:
    return any(path.startswith(prefix) or ("/" + prefix) in "/" + path for prefix in NESTED_SKIP)


def find_roots(runner: RescueRunner, top: RootFS, mountpoint: str, subvolumes: list[Subvolume], default_id: int) -> list[BtrfsRoot]:
    """System roots in a btrfs file system whose top level is mounted at ``mountpoint`` (``top`` reads it)."""
    by_id = {sv.id: sv for sv in subvolumes}
    order: list[Subvolume] = []
    if default_id != 5 and default_id in by_id:
        order.append(by_id[default_id])
    order.append(Subvolume(5, ""))
    plain = [sv for sv in subvolumes if sv.id != default_id and sv.depth <= MAX_DEPTH and not sv.is_snapshot and not _is_nested_storage(sv.path)]
    snapshots = sorted((sv for sv in subvolumes if sv.id != default_id and sv.is_snapshot), key=lambda sv: -sv.id)[:MAX_SNAPSHOT_TESTS]
    order.extend(plain[: MAX_TESTS - len(order)])
    order.extend(snapshots)
    roots: list[BtrfsRoot] = []
    for sv in order:
        try:
            view = top.sub(sv.path or "/")
        except (OSError, RescueError):
            continue
        with view:
            if not looks_like_root(view):
                continue
        is_default = sv.id == default_id or (default_id == 5 and sv.id == 5)
        kind = "snapshot" if sv.is_snapshot and not is_default else "root"
        roots.append(BtrfsRoot(sv, kind, is_default, _read_only(runner, top, sv) if is_default or kind == "snapshot" else None))
    return roots


def _read_only(runner: RescueRunner, top: RootFS, sv: Subvolume) -> bool | None:
    try:
        path = top.host_path(sv.path or "/")
    except (OSError, RescueError):
        return None
    result = runner.execute(("btrfs", "property", "get", "-ts", path, "ro"), timeout=15)
    if result.returncode != 0:
        return None
    return "ro=true" in result.stdout
