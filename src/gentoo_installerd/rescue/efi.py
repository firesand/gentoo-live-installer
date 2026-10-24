"""UEFI boot entries (NVRAM) and the firmware matrix: what can be repaired from this live session.

efibootmgr is a host tool and the only thing that touches NVRAM. A BIOS-booted live session has no efivarfs, so entries
can only be written when the live system itself runs in UEFI mode (docs: firmware matrix).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .hostenv import Environment

ENTRY_RE = re.compile(r"^Boot([0-9A-Fa-f]{4})(\*?)\s+(.*?)(?:\t(.*))?$")
HD_RE = re.compile(r"HD\((\d+),GPT,([0-9a-fA-F-]{36}),")
HD_MBR_RE = re.compile(r"HD\((\d+),MBR,0x([0-9a-fA-F]{1,8}),")
FILE_RE = re.compile(r"File\(([^)]*)\)")  # efibootmgr 17 and older: HD(...)/File(\EFI\x\y.efi)
FILE_TAIL_RE = re.compile(r"\)/(\\.*)$")  # efibootmgr 18: HD(...)/\EFI\x\y.efi, optional data glued to the end of it
EFI_EXTENSION_RE = re.compile(r"\.efi", re.IGNORECASE)
LABEL_CLEAN_RE = re.compile(r"[^A-Za-z0-9 ._-]")


@dataclass
class BootEntry:
    number: str
    active: bool
    label: str
    device_path: str = ""
    partuuid: str = ""
    partition: int = 0
    loader: str = ""  # \EFI\dir\file.efi as the firmware stores it

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "active": self.active, "label": self.label, "partuuid": self.partuuid, "loader": self.loader, "device_path": self.device_path[:120]}


@dataclass
class EfiState:
    usable: bool = True
    error: str = ""
    current: str = ""
    next: str = ""
    order: list[str] = field(default_factory=list)
    timeout: str = ""
    entries: list[BootEntry] = field(default_factory=list)

    def entry(self, number: str) -> BootEntry | None:
        wanted = number.upper()
        for item in self.entries:
            if item.number.upper() == wanted:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"usable": self.usable, "error": self.error, "current": self.current, "next": self.next, "order": self.order, "timeout": self.timeout,
                "entries": [entry.to_dict() for entry in self.entries]}


def loader_of(device_path: str) -> str:
    """The file a boot entry starts (``\\EFI\\dir\\file.efi``), from either spelling of the device path.

    efibootmgr 18 prints the optional data of an entry directly after the path: as text when it is printable, else as
    hex digits (``...BOOTX64.EFI0000424f``). The file name ends at the ``.efi`` after which only hex digits remain, or
    at the first ``.efi`` when nothing fits.
    """
    old = FILE_RE.search(device_path)
    if old:
        return old.group(1)
    tail = FILE_TAIL_RE.search(device_path)
    if not tail:
        return ""
    text = tail.group(1)
    ends = [match.end() for match in EFI_EXTENSION_RE.finditer(text)]
    for end in ends:
        if re.fullmatch(r"[0-9a-fA-F]*", text[end:]):
            return text[:end]
    return text[:ends[0]] if ends else text


def partition_of(device_path: str) -> tuple[str, int]:
    """(PARTUUID as lsblk prints it, partition number) of the hard disk node of a device path, or ('', 0)."""
    gpt = HD_RE.search(device_path)
    if gpt:
        return gpt.group(2).lower(), int(gpt.group(1))
    mbr = HD_MBR_RE.search(device_path)
    if mbr:
        return f"{int(mbr.group(2), 16):08x}-{int(mbr.group(1)):02x}", int(mbr.group(1))  # Linux names a DOS partition <disk signature>-<number>
    return "", 0


def parse_efibootmgr(text: str, returncode: int = 0) -> EfiState:
    """Parse ``efibootmgr -v`` output; an error message (no efivarfs, no UEFI) becomes ``usable=False``."""
    state = EfiState()
    if returncode != 0 or "EFI variables are not supported" in text:
        state.usable, state.error = False, text.strip().splitlines()[0] if text.strip() else "efibootmgr failed"
        return state
    for line in text.splitlines():
        if line.startswith("BootCurrent:"):
            state.current = line.split(":", 1)[1].strip()
        elif line.startswith("BootNext:"):
            state.next = line.split(":", 1)[1].strip()
        elif line.startswith("BootOrder:"):
            state.order = [item for item in line.split(":", 1)[1].strip().split(",") if item]
        elif line.startswith("Timeout:"):
            state.timeout = line.split(":", 1)[1].strip()
        else:
            match = ENTRY_RE.match(line)
            if match:
                path = match.group(4) or ""
                partuuid, partition = partition_of(path)
                state.entries.append(BootEntry(match.group(1).upper(), bool(match.group(2)), match.group(3).strip(), path, partuuid, partition, loader_of(path)))
    return state


def esp_file_exists(loader: str, listdir: Callable[[str], list[str]], esp_mountpoint: str) -> bool:
    """FAT is case-insensitive: look a firmware path (\\EFI\\dir\\file.efi) up component by component, ignoring case."""
    current = esp_mountpoint.rstrip("/")
    parts = [part for part in loader.replace("\\", "/").split("/") if part]
    if not parts:
        return False
    for index, part in enumerate(parts):
        names = listdir(current or "/")
        match = next((name for name in names if name.lower() == part.lower()), None)
        if match is None:
            return False
        current = f"{current}/{match}"
        if index == len(parts) - 1:
            return True
    return False


def entries_for_esp(state: EfiState, partuuid: str) -> list[BootEntry]:
    return [entry for entry in state.entries if entry.partuuid and entry.partuuid == partuuid.lower()]


def firmware_message(live_uefi: bool, target_uefi: bool | None, target_bios: bool | None) -> tuple[str, str]:
    """(code, text) of the firmware matrix row that applies, or ('', '') when everything matches."""
    if not live_uefi and target_uefi:
        return ("live_bios_target_uefi", "This live session started in legacy BIOS mode but the installed system boots with UEFI. Boot entries cannot be written from here; the fallback boot path "
                "(EFI/BOOT/BOOTX64.EFI) can be created instead. For a complete repair, start this medium in UEFI mode.")
    if live_uefi and target_bios and not target_uefi:
        return ("live_uefi_target_bios", "The installed system boots in legacy BIOS mode. After the repair, start the computer in BIOS/CSM mode.")
    return "", ""


def sanitize_label(label: str, default: str = "Linux") -> str:
    cleaned = LABEL_CLEAN_RE.sub("", label).strip()[:64]
    return cleaned or default


def loader_for_entry(path: str) -> str:
    """Normalize a user/adapter supplied loader (\\EFI\\dir\\file.efi or /EFI/dir/file.efi) to the firmware form."""
    cleaned = "\\" + "\\".join(part for part in path.replace("/", "\\").split("\\") if part)
    return cleaned


def efi_usable(env: Environment) -> tuple[bool, str]:
    if not env.uefi:
        return False, "This live session was started in legacy BIOS mode; boot entries cannot be listed or changed. Restart it in UEFI mode to repair boot entries."
    if env.efivars == "ro":
        return False, "The firmware variables are mounted read-only, so boot entries cannot be changed."
    return True, ""
