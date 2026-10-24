from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


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
    partitions: list[dict] = field(default_factory=list)

    @property
    def size_text(self) -> str:
        return format_size(self.size)


@dataclass
class SystemInfo:
    architecture: str
    memory_gib: float
    uefi: bool
    disks: list[DiskInfo]
    network: bool
    synchronized_clock: bool
    missing_tools: list[str]


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


def discover_disks() -> list[DiskInfo]:
    try:
        result = subprocess.run(
            ["lsblk", "--json", "--bytes", "--output", "NAME,PATH,MODEL,SERIAL,WWN,SIZE,TYPE,RM,RO,MOUNTPOINTS,PKNAME,FSTYPE,LABEL,PARTTYPENAME"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        raw = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
    disks = []
    for item in raw.get("blockdevices", []):
        if item.get("type") != "disk" or item.get("path", "").startswith("/dev/loop"):
            continue
        children = item.get("children") or []
        mounted = _has_mount(item)
        disks.append(DiskInfo(
            path=item.get("path") or f"/dev/{item.get('name')}",
            model=(item.get("model") or "Block device").strip(),
            size=int(item.get("size") or 0),
            serial=item.get("serial") or "",
            wwid=item.get("wwn") or "",
            removable=bool(item.get("rm")),
            read_only=bool(item.get("ro")),
            mounted=mounted,
            partitions=children,
        ))
    return disks


def _has_mount(item: dict) -> bool:
    mountpoints = item.get("mountpoints") or []
    if isinstance(mountpoints, str):
        mountpoints = [mountpoints]
    return any(isinstance(path, str) and bool(path.strip()) for path in mountpoints) or any(
        _has_mount(child) for child in item.get("children") or []
    )


def check_network(host: str = "distfiles.gentoo.org") -> bool:
    try:
        socket.create_connection((host, 443), timeout=3).close()
        return True
    except OSError:
        return False


def inspect_system(with_network: bool = True) -> SystemInfo:
    memory = 0.0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                memory = int(line.split()[1]) / 1024 / 1024
                break
    except OSError:
        pass
    tools = ["sgdisk", "mkfs.ext4", "tar", "gpgv", "chroot", "grub-install"]
    return SystemInfo(
        architecture=platform.machine(),
        memory_gib=memory,
        uefi=Path("/sys/firmware/efi").exists(),
        disks=discover_disks(),
        network=check_network() if with_network else False,
        synchronized_clock=Path("/run/systemd/timesync/synchronized").exists(),
        missing_tools=[tool for tool in tools if shutil.which(tool) is None],
    )
