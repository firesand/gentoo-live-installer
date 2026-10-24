"""Facts about the live system (firmware, memory, clock), readable from a fixture tree in tests."""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

SECURE_BOOT_VAR = "SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
TOOLS_WANTED = ("lsblk", "blkid", "findmnt", "mount", "umount", "blockdev", "cryptsetup", "pvs", "vgchange", "mdadm", "btrfs", "efibootmgr", "e2fsck", "xfs_repair", "grub-install",
                "smartctl", "lsinitrd", "chroot", "script", "fuser", "timeout")


class Host:
    """Reads /proc and /sys of the running system, or of a fixture tree (``GENTOO_HW_ROOT`` style)."""

    def __init__(self, root: str | os.PathLike[str] = "/", *, which: Callable[[str], str | None] | None = None, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self._which = which or shutil.which
        self.clock = clock

    def path(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")

    def read(self, absolute: str, default: str = "") -> str:
        try:
            return self.path(absolute).read_text(errors="replace")
        except OSError:
            return default

    def read_bytes(self, absolute: str) -> bytes:
        try:
            return self.path(absolute).read_bytes()
        except OSError:
            return b""

    def exists(self, absolute: str) -> bool:
        return self.path(absolute).exists()

    def cmdline(self) -> str:
        return self.read("/proc/cmdline").strip()

    def filesystems(self) -> set[str]:
        names: set[str] = set()
        for line in self.read("/proc/filesystems").splitlines():
            parts = line.split()
            if parts:
                names.add(parts[-1])
        return names

    def fs_available(self, fstype: str) -> bool:
        """Built in, or a module file exists (``.ko`` or ``.ko.xz``)."""
        if fstype in self.filesystems():
            return True
        release = platform.release() if self.root == Path("/") else self.read("/proc/sys/kernel/osrelease").strip()
        base = self.path(f"/lib/modules/{release}/kernel/fs")
        if release and base.is_dir():
            return any(base.rglob(f"{fstype}.ko*"))
        return False

    def uefi(self) -> bool:
        return self.exists("/sys/firmware/efi")

    def fw_platform_size(self) -> int:
        text = self.read("/sys/firmware/efi/fw_platform_size").strip()
        return int(text) if text.isdigit() else (64 if self.uefi() else 0)

    def secure_boot(self) -> bool | None:
        data = self.read_bytes(f"/sys/firmware/efi/efivars/{SECURE_BOOT_VAR}")
        return bool(data[-1]) if data else None

    def meminfo(self) -> tuple[int, int]:
        total = available = 0
        for line in self.read("/proc/meminfo").splitlines():
            match = re.match(r"^(MemTotal|MemAvailable):\s+(\d+) kB", line)
            if match:
                value = int(match.group(2)) * 1024
                if match.group(1) == "MemTotal":
                    total = value
                else:
                    available = value
        return total, available

    def machine(self) -> str:
        return platform.machine()

    def which(self, name: str) -> str | None:
        return self._which(name)


@dataclass
class Environment:
    uefi: bool = False
    fw_platform_size: int = 0
    efivars: str = ""  # "" (not mounted) | "ro" | "rw"
    secure_boot: bool | None = None
    memory_bytes: int = 0
    free_bytes: int = 0
    clock_ok: bool = True
    machine: str = ""
    tools: dict[str, bool] = field(default_factory=dict)
    network: dict[str, bool] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def firmware(self) -> str:
        return "uefi" if self.uefi else "bios"


def default_connect(address: tuple[str, int]) -> bool:
    try:
        with socket.create_connection(address, timeout=2):
            return True
    except OSError:
        return False


def check_network(connect: Callable[[tuple[str, int]], bool] = default_connect, resolve: Callable[[str], bool] | None = None) -> dict[str, bool]:
    """Separate 'no route' from 'no DNS': three well-known addresses and one name lookup."""
    route = any(connect(addr) for addr in (("1.1.1.1", 443), ("9.9.9.9", 443), ("8.8.8.8", 443)))

    def lookup(name: str) -> bool:
        try:
            socket.getaddrinfo(name, 443)
            return True
        except OSError:
            return False

    dns = (resolve or lookup)("deb.debian.org")
    return {"ip": route, "dns": dns}


def collect_environment(host: Host, runner: Any = None, *, network: bool = False, tools: Iterable[str] = TOOLS_WANTED,
                        connect: Callable[[tuple[str, int]], bool] = default_connect, resolve: Callable[[str], bool] | None = None) -> Environment:
    total, available = host.meminfo()
    env = Environment(
        uefi=host.uefi(), fw_platform_size=host.fw_platform_size(), secure_boot=host.secure_boot() if host.uefi() else None, memory_bytes=total, free_bytes=available,
        clock_ok=time.gmtime(host.clock()).tm_year >= 2025, machine=host.machine(), tools={name: host.which(name) is not None for name in tools},
    )
    if env.uefi and runner is not None:
        result = runner.execute(("findmnt", "-n", "-o", "OPTIONS", "/sys/firmware/efi/efivars"))
        options = result.stdout.strip().split(",") if result.returncode == 0 and result.stdout.strip() else []
        env.efivars = "rw" if "rw" in options else "ro" if "ro" in options else ""
    if network:
        env.network = check_network(connect, resolve)
    return env
