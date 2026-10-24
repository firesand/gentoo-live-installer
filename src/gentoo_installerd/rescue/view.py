"""SessionView: the one door adapters and diagnosis use to look at a mounted system (review R1, R9).

It never hands out a ``Path`` of the target. Every read goes through ``RootFS`` (dir_fd walks with chroot semantics) and
nothing here runs a program from the target; package databases are read as files.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import cmdline as cmdline_mod
from .bootinfo import BootInfo, KernelInfo, collect_boot, first_text_cmdline
from .devices import DeviceGraph, DeviceNode
from .errors import RescueError
from .fstab import CryptEntry, Fstab, Resolved, parse_crypttab, resolve_all
from .hostenv import Environment
from .osrelease import OsInfo, SystemFacts, identify
from .paths import RootFS
from .session import RescueSession

ESP_MOUNTPOINTS = ("/efi", "/boot/efi", "/boot/EFI", "/esp", "/boot")


@dataclass
class EspInfo:
    mountpoint: str  # inside the system, for example /efi
    device: str
    disk: str
    partn: int | None
    files: dict[str, list[str]] = field(default_factory=dict)  # EFI/<dir> -> files


class SessionView:
    def __init__(self, session: RescueSession, graph_provider: Callable[[], DeviceGraph], environment: Environment | None = None, efi_text: str = "") -> None:
        self._session = session
        self._graph_provider = graph_provider
        self.environment = environment or Environment()
        self.efi_text = efi_text
        self._cache_key = -1
        self._cache: dict[str, Any] = {}
        self.degraded_view = any(entry.data.get("rung") for entry in session.ledger.of_kind("mount"))

    # ---- identity ----------------------------------------------------------------------------------------------------
    @property
    def sid(self) -> str:
        return self._session.sid

    @property
    def mode(self) -> str:
        return self._session.mode

    @property
    def allow_write(self) -> bool:
        return self._session.allow_write

    @property
    def generation(self) -> int:
        return self._session.generation

    def _cached(self, key: str, make: Callable[[], Any]) -> Any:
        if self._cache_key != self._session.generation:
            self._cache, self._cache_key = {}, self._session.generation
        if key not in self._cache:
            self._cache[key] = make()
        return self._cache[key]

    def _fs(self) -> RootFS:
        return RootFS(self._session.root)

    @property
    def facts(self) -> SystemFacts:
        def make() -> SystemFacts:
            with self._fs() as fs:
                facts = identify(fs)
            if facts is None:
                raise RescueError("unsupported_system", "The mounted file system does not look like a Linux system")
            return facts

        return self._cached("facts", make)

    @property
    def os(self) -> OsInfo:
        return self.facts.os

    @property
    def family(self) -> str:
        return self.os.family

    @property
    def init(self) -> str:
        return self.facts.init

    @property
    def arch(self) -> str:
        return self.facts.arch

    @property
    def immutable(self) -> str:
        return self.facts.immutable

    @property
    def can_run_programs(self) -> bool:
        """Programs of the system can run here: x86 family, not an image-based system, executable mount."""
        return self.arch in {"x86_64", "i386", ""} and not self.immutable and self._session.allow_write

    @property
    def graph(self) -> DeviceGraph:
        return self._graph_provider()

    @property
    def root_device(self) -> str:
        return self._session.plan.root.device

    @property
    def root_uuid(self) -> str:
        return self._session.plan.root.uuid

    @property
    def root_node(self) -> DeviceNode | None:
        try:
            return self.graph.find(self.root_device)
        except LookupError:
            return None

    # ---- reading (always chroot semantics) ----------------------------------------------------------------------------------
    def exists(self, path: str) -> bool:
        with self._fs() as fs:
            return fs.exists(path)

    def is_file(self, path: str) -> bool:
        with self._fs() as fs:
            return fs.is_file(path)

    def is_dir(self, path: str) -> bool:
        with self._fs() as fs:
            return fs.is_dir(path)

    def read(self, path: str, limit: int = 1 << 20, default: str = "") -> str:
        with self._fs() as fs:
            return fs.read_text_or(path, default, limit)

    def sha256(self, path: str) -> str:
        with self._fs() as fs:
            return fs.sha256(path)

    def size(self, path: str) -> int:
        with self._fs() as fs:
            try:
                return fs.stat(path).st_size
            except (OSError, RescueError):
                return 0

    def listdir(self, path: str) -> list[str]:
        with self._fs() as fs:
            return fs.listdir_or(path)

    def which(self, name: str) -> str | None:
        with self._fs() as fs:
            return fs.which(name)

    def free_bytes(self, path: str = "/") -> int:
        with self._fs() as fs:
            try:
                return fs.free_bytes(path)
            except (OSError, RescueError):
                return 0

    def readlink(self, path: str) -> str:
        with self._fs() as fs:
            return fs.readlink(path)

    # ---- mounts --------------------------------------------------------------------------------------------------------------
    @property
    def mounts(self) -> dict[str, dict[str, Any]]:
        return {entry.data.get("mountpoint", "/"): dict(entry.data) for entry in self._session.ledger.entries
                if entry.kind == "mount" and not entry.data.get("virtual")}

    def is_mounted(self, mountpoint: str) -> bool:
        return mountpoint in self.mounts

    # ---- boot facts --------------------------------------------------------------------------------------------------------------
    def esp(self) -> EspInfo | None:
        def make() -> EspInfo | None:
            for mountpoint in ESP_MOUNTPOINTS:
                info = self.mounts.get(mountpoint)
                if not info or info.get("fs") != "vfat":
                    continue
                try:
                    node = self.graph.find(info["device"])
                except LookupError:
                    continue
                disks = self.graph.parent_disks(node)
                with self._fs() as fs:
                    try:
                        esp_fs = fs.sub(mountpoint)
                    except (OSError, RescueError):
                        return EspInfo(mountpoint, node.path, disks[0].path if disks else "", node.partn)
                    with esp_fs:
                        files = {name: esp_fs.listdir_or(f"/EFI/{name}") for name in esp_fs.listdir_or("/EFI")}
                return EspInfo(mountpoint, node.path, disks[0].path if disks else "", node.partn, files)
            return None

        return self._cached("esp", make)

    def boot(self) -> BootInfo:
        def make() -> BootInfo:
            with self._fs() as fs:
                esp = self.esp()
                if esp is not None:
                    try:
                        with fs.sub(esp.mountpoint) as esp_fs:
                            return collect_boot(fs, esp_fs)
                    except (OSError, RescueError):
                        pass
                return collect_boot(fs)

        return self._cached("boot", make)

    def kernels(self) -> list[KernelInfo]:
        return self.boot().kernels

    def fstab(self) -> Fstab:
        return self._cached("fstab", lambda: Fstab.parse(self.read("/etc/fstab")))

    def crypttab(self) -> list[CryptEntry]:
        return self._cached("crypttab", lambda: parse_crypttab(self.read("/etc/crypttab")))

    def resolved_fstab(self) -> list[Resolved]:
        return self._cached("resolved", lambda: resolve_all(self.fstab(), self.graph, crypttab=self.crypttab()))

    def selinux(self) -> tuple[bool, str]:
        text = self.read("/etc/selinux/config", limit=4096)
        mode = re.search(r"^SELINUX=(\w+)", text, re.M)
        kind = re.search(r"^SELINUXTYPE=(\w+)", text, re.M)
        enabled = bool(mode and mode.group(1) in {"enforcing", "permissive"} and kind and self.is_dir(f"/etc/selinux/{kind.group(1)}"))
        return enabled, kind.group(1) if kind else ""

    def kernel_cmdline_source(self) -> tuple[str, str]:
        with self._fs() as fs:
            return first_text_cmdline(fs)

    def installkernel_guard(self, use: str) -> cmdline_mod.GuardInfo:
        with self._fs() as fs:
            return cmdline_mod.installkernel_guard(fs, use)

    def grub_binary_mentions(self, name: str, needle: bytes) -> bool:
        """Whether a program of the system contains a string, to learn which options it knows without running it."""
        path = self.which(name)
        if not path:
            return False
        with self._fs() as fs:
            try:
                return needle in fs.read_bytes(path, 4 << 20)
            except (OSError, RescueError):
                return False

    # ---- package databases, read as files (review R9: no target program is run to query them) ---------------------------------------
    def installed_packages(self) -> dict[str, str]:
        """name -> version for the packages this view can list without running anything."""
        def make() -> dict[str, str]:
            family = self.family
            if family == "gentoo":
                return self._portage_packages()
            if family == "debian":
                return self._dpkg_packages()
            if family == "arch":
                return self._pacman_packages()
            return {}

        return self._cached("packages", make)

    def _portage_packages(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for category in self.listdir("/var/db/pkg"):
            for entry in self.listdir(f"/var/db/pkg/{category}"):
                match = re.match(r"^(.+?)-(\d[\w.]*(?:_[a-z]+\d*)*(?:-r\d+)?)$", entry)
                if match:
                    found[f"{category}/{match.group(1)}"] = match.group(2)
        return found

    def _dpkg_packages(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for block in self.read("/var/lib/dpkg/status", 16 << 20).split("\n\n"):
            fields = dict(re.findall(r"^([A-Za-z-]+): (.*)$", block, re.M))
            if fields.get("Package") and "install ok installed" in fields.get("Status", ""):
                found[fields["Package"]] = fields.get("Version", "")
        return found

    def _pacman_packages(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for entry in self.listdir("/var/lib/pacman/local"):
            match = re.match(r"^(.+)-([^-]+-[^-]+)$", entry)
            if match:
                found[match.group(1)] = match.group(2)
        return found

    def dpkg_half_configured(self) -> list[str]:
        names = []
        for block in self.read("/var/lib/dpkg/status", 16 << 20).split("\n\n"):
            fields = dict(re.findall(r"^([A-Za-z-]+): (.*)$", block, re.M))
            status = fields.get("Status", "")
            if fields.get("Package") and status and not status.endswith("installed") and not status.endswith("not-installed") and not status.endswith("config-files"):
                names.append(fields["Package"])
        return names

    def rpm_names(self) -> set[str] | None:
        """Installed rpm package names from rpmdb.sqlite (Fedora 33+, RHEL 9+); None when the database is not readable here."""
        data = self._rpmdb_blobs()
        if data is None:
            return None
        return {name for name in (_rpm_header_name(blob) for blob in data) if name}

    def _rpmdb_blobs(self) -> list[bytes] | None:
        for path in ("/var/lib/rpm/rpmdb.sqlite", "/usr/lib/sysimage/rpm/rpmdb.sqlite"):
            if not self.is_file(path):
                continue
            payload = None
            with self._fs() as fs:
                try:
                    payload = fs.read_bytes(path, 256 << 20)
                except (OSError, RescueError):
                    continue
            if not payload:
                continue
            with tempfile.TemporaryDirectory(prefix="rescue-rpmdb-") as scratch:
                copy = Path(scratch) / "rpmdb.sqlite"
                copy.write_bytes(payload)
                try:
                    with sqlite3.connect(f"file:{copy}?mode=ro", uri=True) as db:
                        return [row[0] for row in db.execute("SELECT blob FROM Packages")]
                except sqlite3.DatabaseError:
                    return None
        return None

    def portage_resume_pending(self) -> bool:
        text = self.read("/var/cache/edb/mtimedb", 4 << 20)
        try:
            return bool(json.loads(text).get("resume", {}).get("mergelist"))
        except (ValueError, AttributeError):
            return False

    # ---- accounts (read from passwd; hashes never leave the daemon) --------------------------------------------------------------------
    def accounts(self) -> list[dict[str, Any]]:
        uid_min = 1000
        match = re.search(r"^UID_MIN\s+(\d+)", self.read("/etc/login.defs", 65536), re.M)
        if match:
            uid_min = int(match.group(1))
        shadow: dict[str, str] = {}
        for line in self.read("/etc/shadow", 1 << 20).splitlines():
            parts = line.split(":")
            if len(parts) > 1:
                shadow[parts[0]] = parts[1]
        users = []
        for line in self.read("/etc/passwd", 1 << 20).splitlines():
            parts = line.split(":")
            if len(parts) < 7 or not parts[2].isdigit():
                continue
            uid = int(parts[2])
            shell = parts[6]
            if uid != 0 and (uid < uid_min or uid == 65534 or shell.endswith(("nologin", "false"))):
                continue
            hashed = shadow.get(parts[0], "")
            state = "empty" if hashed == "" and parts[0] in shadow else "locked" if hashed[:1] in {"!", "*"} else "set" if hashed else "unknown"
            users.append({"name": parts[0], "uid": uid, "home": parts[5], "shell": shell, "password": state})
        return users


_RPM_TAG_NAME = 1000


def _rpm_header_name(blob: bytes) -> str:
    """The NAME tag of an rpm header blob (index count, data length, 16-byte index entries, data store)."""
    if len(blob) < 8:
        return ""
    count = int.from_bytes(blob[0:4], "big")
    size = int.from_bytes(blob[4:8], "big")
    store = 8 + 16 * count
    if count > 4096 or store + size > len(blob):
        return ""
    for index in range(count):
        entry = blob[8 + 16 * index: 8 + 16 * (index + 1)]
        tag, kind, offset = (int.from_bytes(entry[0:4], "big"), int.from_bytes(entry[4:8], "big"), int.from_bytes(entry[8:12], "big"))
        if tag == _RPM_TAG_NAME and kind in (6, 9):
            end = blob.find(b"\x00", store + offset)
            return blob[store + offset:end].decode("utf-8", "replace") if end > 0 else ""
    return ""
