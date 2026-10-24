from __future__ import annotations

import json
import os
import platform
import shutil
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .runner import Runner, RunnerError


@dataclass(frozen=True)
class Disk:
    name: str
    path: str
    type: str
    size: int
    model: str = ""
    serial: str = ""
    wwn: str = ""
    fstype: str = ""
    mountpoints: tuple[str, ...] = ()
    children: tuple[Disk, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Disk:
        return cls(
            name=str(raw.get("name", "")),
            path=str(raw.get("path") or ("/dev/" + str(raw.get("name", "")))),
            type=str(raw.get("type", "")),
            size=int(raw.get("size") or 0),
            model=str(raw.get("model") or "").strip(),
            serial=str(raw.get("serial") or "").strip(),
            wwn=str(raw.get("wwn") or "").strip(),
            fstype=str(raw.get("fstype") or ""),
            mountpoints=tuple(str(item) for item in (raw.get("mountpoints") or []) if item),
            children=tuple(cls.from_dict(child) for child in raw.get("children") or []),
        )

    def walk(self) -> tuple[Disk, ...]:
        return (self,) + tuple(child for node in self.children for child in node.walk())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Inspection:
    architecture: str
    memory_bytes: int
    uefi: bool
    network: bool
    tools: dict[str, bool]
    disks: list[Disk]
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["disks"] = [disk.to_dict() for disk in self.disks]
        return result


REQUIRED_TOOLS = ("lsblk", "findmnt", "blkid", "sgdisk", "parted", "partprobe", "udevadm", "mkfs.ext4", "mkswap", "mount", "umount", "tar", "gpg", "gpgv")
OPTIONAL_TOOLS = ("swapon", "swapoff", "mkfs.xfs", "mkfs.btrfs", "mkfs.vfat", "cryptsetup", "pvcreate", "vgcreate", "lvcreate", "pvs", "vgchange", "mdadm", "sbsign", "sbverify", "efibootmgr")


def parse_lsblk(text: str) -> list[Disk]:
    raw = json.loads(text)
    if not isinstance(raw, dict) or not isinstance(raw.get("blockdevices"), list):
        raise ValueError("lsblk returned an invalid disk list")
    return [Disk.from_dict(item) for item in raw["blockdevices"]]


class Discovery:
    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def disks(self) -> list[Disk]:
        result = self.runner.run(("lsblk", "--json", "--bytes", "--output", "NAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,FSTYPE,MOUNTPOINTS"))
        return parse_lsblk(result.stdout)

    def inspect(self) -> Inspection:
        errors: list[str] = []
        try:
            disks = self.disks()
        except (RunnerError, ValueError, json.JSONDecodeError) as exc:
            disks = []
            errors.append(f"Disk discovery failed: {exc}")
        memory = 0
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    memory = int(line.split()[1]) * 1024
                    break
        except (OSError, ValueError, IndexError):
            errors.append("Memory size could not be determined")
        network = _network_ready()
        if not network:
            errors.append("No network connection was detected")
        architecture = platform.machine()
        if architecture not in {"x86_64", "amd64"}:
            errors.append(f"Gentoo AMD64 requires x86_64, not {architecture}")
        if memory and memory < 2 * 1024**3:
            errors.append("At least 2 GiB of memory is recommended")
        tools = {tool: shutil.which(tool) is not None for tool in (*REQUIRED_TOOLS, *OPTIONAL_TOOLS)}
        missing = [tool for tool in REQUIRED_TOOLS if not tools[tool]]
        if missing:
            errors.append("Missing required tools: " + ", ".join(missing))
        return Inspection(architecture, memory, Path("/sys/firmware/efi").exists(), network, tools, disks, errors)


def _network_ready() -> bool:
    try:
        with socket.create_connection(("distfiles.gentoo.org", 443), timeout=2):
            return True
    except OSError:
        return False


def find_disk(disks: Sequence[Disk], device: str) -> Disk:
    canonical = os.path.realpath(device)
    matches = [node for disk in disks for node in disk.walk() if os.path.realpath(node.path) == canonical]
    if len(matches) != 1:
        raise ValueError(f"Target device is absent or ambiguous: {device}")
    return matches[0]
