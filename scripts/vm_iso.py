"""The live ISO's boot menu, read without any host tool: an ISO 9660 reader and a grub.cfg parser.

``test-vm.py --boot-entry`` boots one entry of the ISO's GRUB menu (text console, serial console,
rescue console, ...). The menu is drawn by ``gfxterm`` only, so a harness cannot drive it over the
serial port, and keystrokes sent to a ten second countdown would be a guess. Instead the harness
reads the menu from the ISO itself, takes the kernel, the initramfs and the arguments of the chosen
entry and starts QEMU with exactly those (``-kernel``, ``-initrd``, ``-append``) with the ISO still
attached as the CD-ROM, which is what the entry's ``root=live:CDLABEL=...`` needs. GRUB itself is
therefore not exercised by such a run (``scripts/verify-iso.py`` checks the menu file); every
argument that decides how the live system behaves is.

Everything here is pure or reads a file; nothing starts a process.
"""
from __future__ import annotations

import re
import shlex
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

SECTOR = 2048
FIRST_DESCRIPTOR = 16
MAX_TEXT = 1 << 20
GRUB_CFG = "/boot/grub/grub.cfg"


class IsoError(Exception):
    """The image cannot be read, or a menu entry cannot be booted this way."""


@dataclass(frozen=True)
class IsoNode:
    name: str
    lba: int
    size: int
    directory: bool


def _le32(record: bytes, offset: int) -> int:
    return struct.unpack_from("<I", record, offset)[0]


def _rock_ridge_name(system_use: bytes) -> str:
    """The long file name of a Rock Ridge 'NM' entry (continuation areas are not followed)."""
    name = b""
    offset = 0
    while offset + 4 <= len(system_use):
        signature, length = system_use[offset:offset + 2], system_use[offset + 2]
        if length < 4:
            break
        if signature == b"NM":
            flags = system_use[offset + 4]
            if not flags & 0b110:  # not the current or the parent directory
                name += system_use[offset + 5:offset + length]
        offset += length
    return name.decode(errors="replace")


def _iso_name(identifier: bytes) -> str:
    """'GRUB.CFG;1' -> 'grub.cfg', 'GENTOO.' -> 'gentoo'."""
    text = identifier.decode(errors="replace").split(";", 1)[0]
    return text[:-1] if text.endswith(".") and len(text) > 1 else text


def parse_directory(data: bytes) -> Iterator[IsoNode]:
    """The records of one directory extent, without '.' and '..'."""
    offset = 0
    while offset < len(data):
        length = data[offset]
        if length == 0:  # records never straddle a sector: the rest of this one is padding
            offset = (offset // SECTOR + 1) * SECTOR
            continue
        record = data[offset:offset + length]
        offset += length
        if len(record) < 34:
            raise IsoError("a directory record is truncated")
        identifier_length = record[32]
        identifier = record[33:33 + identifier_length]
        if identifier in (b"\x00", b"\x01"):
            continue
        system_use = record[33 + identifier_length + (1 - identifier_length % 2):]
        name = _rock_ridge_name(system_use) or _iso_name(identifier)
        yield IsoNode(name, _le32(record, 2), _le32(record, 10), bool(record[25] & 2))


class IsoImage:
    """Read-only access to the primary tree of an ISO 9660 image (Rock Ridge names are used when present)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        try:
            with self.path.open("rb") as stream:
                self.root = self._root_record(stream)
        except OSError as exc:
            raise IsoError(f"cannot read {self.path}: {exc}") from exc

    @staticmethod
    def _root_record(stream) -> IsoNode:
        sector = FIRST_DESCRIPTOR
        while True:
            stream.seek(sector * SECTOR)
            descriptor = stream.read(SECTOR)
            if len(descriptor) < SECTOR or descriptor[1:6] != b"CD001":
                raise IsoError("this is not an ISO 9660 image (no volume descriptor)")
            if descriptor[0] == 1:
                record = descriptor[156:190]
                return IsoNode("", _le32(record, 2), _le32(record, 10), True)
            if descriptor[0] == 255:
                raise IsoError("the image has no primary volume descriptor")
            sector += 1

    def _read(self, node: IsoNode, limit: int | None = None) -> bytes:
        size = node.size if limit is None else min(node.size, limit)
        with self.path.open("rb") as stream:
            stream.seek(node.lba * SECTOR)
            data = stream.read(size)
        if len(data) < size:
            raise IsoError(f"{node.name or '/'} ends beyond the end of the image")
        return data

    def listdir(self, node: IsoNode) -> list[IsoNode]:
        if not node.directory:
            raise IsoError(f"{node.name} is not a directory")
        return list(parse_directory(self._read(node)))

    def find(self, path: str) -> IsoNode:
        node = self.root
        for part in [item for item in path.split("/") if item]:
            wanted = part.lower()
            match = next((child for child in self.listdir(node) if child.name.lower() == wanted), None)
            if match is None:
                raise IsoError(f"{path} is not in the image")
            node = match
        return node

    def read_text(self, path: str) -> str:
        node = self.find(path)
        if node.directory or node.size > MAX_TEXT:
            raise IsoError(f"{path} is not a small text file")
        return self._read(node).decode(errors="replace")

    def extract(self, path: str, destination: Path) -> int:
        """Copy a file out of the image (streaming); returns its size."""
        node = self.find(path)
        if node.directory:
            raise IsoError(f"{path} is a directory")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("rb") as source, destination.open("wb") as target:
            source.seek(node.lba * SECTOR)
            remaining = node.size
            while remaining:
                chunk = source.read(min(remaining, 4 << 20))
                if not chunk:
                    raise IsoError(f"{path} ends beyond the end of the image")
                target.write(chunk)
                remaining -= len(chunk)
        return node.size


# --- the boot menu ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class BootEntry:
    title: str
    submenu: tuple[str, ...] = ()
    kernel: str = ""
    initrd: str = ""
    arguments: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return " > ".join((*self.submenu, self.title))

    @property
    def bootable(self) -> bool:
        return bool(self.kernel)


@dataclass
class _Block:
    kind: str  # menuentry | submenu | other
    title: str = ""
    kernel: str = ""
    initrd: str = ""
    arguments: list[str] = field(default_factory=list)


BLOCK_START_RE = re.compile(r"^\s*(menuentry|submenu)\s+(.*?)\{\s*$")
OPEN_BLOCK_RE = re.compile(r"^\s*(?:function\s+\S+|\S+\s*\(\s*\))\s*\{\s*$")


def _words(text: str) -> list[str]:
    try:
        return shlex.split(text, comments=False)
    except ValueError:
        return text.split()


def parse_boot_menu(text: str) -> list[BootEntry]:
    """Every menu entry of a grub.cfg, in file order, with the kernel, initrd and arguments of its linux line.

    Entries inside ``if`` blocks (Memtest86+, firmware setup) are listed like the others; GRUB decides at
    boot time whether they are shown. Only the constructs GRUB's menu files use are understood.
    """
    entries: list[BootEntry] = []
    stack: list[_Block] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        start = BLOCK_START_RE.match(raw)
        if start:
            words = _words(start.group(2))
            stack.append(_Block(start.group(1), words[0] if words else ""))
            continue
        if line == "}":
            if not stack:
                raise IsoError("grub.cfg has a closing brace without a block")
            block = stack.pop()
            if block.kind == "menuentry":
                titles = tuple(item.title for item in stack if item.kind == "submenu")
                entries.append(BootEntry(block.title, titles, block.kernel, block.initrd, tuple(block.arguments)))
            continue
        if OPEN_BLOCK_RE.match(raw) or line.endswith("{"):
            stack.append(_Block("other"))
            continue
        current = stack[-1] if stack and stack[-1].kind == "menuentry" else None
        if current is None:
            continue
        words = _words(line)
        if not words:
            continue
        if words[0] in ("linux", "linuxefi", "linux16") and len(words) >= 2:
            current.kernel, current.arguments = words[1], words[2:]
        elif words[0] in ("initrd", "initrdefi", "initrd16") and len(words) >= 2:
            current.initrd = words[1]
    if stack:
        raise IsoError("grub.cfg ends inside a block")
    return entries


ALIASES = {
    "default": r"",
    "text": r"^text console only",
    "text-ram": r"^text console in ram",
    "text-installer": r"text installer|text-mode installer|installer.*\btext\b",
    "serial": r"serial console",
    "rescue": r"^rescue|rescue console|repair console",
    "desktop": r"^gentoo installer",
    "verbose": r"verbose boot",
    "compat": r"compatibility mode",
}


def select_entry(entries: Sequence[BootEntry], query: str) -> BootEntry:
    """The entry ``query`` names: an alias (default, text, serial, rescue ...), a title, or a regular expression.

    "default" is the first entry that boots Linux. When several entries match, the one that is not inside a
    submenu wins; otherwise the query is ambiguous and the error lists the candidates.
    """
    linux = [entry for entry in entries if entry.bootable]
    if not linux:
        raise IsoError("the boot menu has no entry that boots Linux")
    key = query.strip().lower()
    if key == "default":
        return linux[0]
    titled = [entry for entry in linux if entry.title.lower() == key]
    if len(titled) == 1:  # a title typed exactly beats an alias of the same word
        return titled[0]
    pattern = ALIASES.get(key, query)
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise IsoError(f"--boot-entry {query!r} is not a menu entry name or a regular expression: {exc}") from exc
    matches = [entry for entry in linux if regex.search(entry.title) or regex.search(entry.label)]
    if len(matches) > 1:
        top_level = [entry for entry in matches if not entry.submenu]
        if len(top_level) == 1:
            return top_level[0]
    if len(matches) == 1:
        return matches[0]
    available = "; ".join(entry.label for entry in linux)
    if not matches:
        raise IsoError(f"no boot menu entry matches {query!r}. The entries that boot Linux are: {available}")
    raise IsoError(f"{query!r} matches several boot menu entries ({'; '.join(entry.label for entry in matches)}): name one of them more exactly")


@dataclass(frozen=True)
class DirectBoot:
    """A menu entry resolved to files on the host: what QEMU boots with -kernel, -initrd and -append."""

    entry: BootEntry
    kernel: Path
    initrd: Path | None
    cmdline: str

    def qemu_arguments(self) -> list[str]:
        args = ["-kernel", str(self.kernel)]
        if self.initrd is not None:
            args += ["-initrd", str(self.initrd)]
        return args + ["-append", self.cmdline]


def load_menu(iso: Path) -> list[BootEntry]:
    return parse_boot_menu(IsoImage(iso).read_text(GRUB_CFG))


def prepare_direct_boot(iso: Path, query: str, directory: Path) -> DirectBoot:
    """Read the ISO's menu, choose the entry, and copy its kernel and initramfs into ``directory``."""
    image = IsoImage(iso)
    entry = select_entry(parse_boot_menu(image.read_text(GRUB_CFG)), query)
    if any("$" in argument for argument in entry.arguments):
        raise IsoError(f"the entry {entry.label!r} uses GRUB variables in its arguments, so it cannot be started without GRUB")
    directory.mkdir(parents=True, exist_ok=True)
    kernel = directory / "kernel"
    image.extract(entry.kernel, kernel)
    initrd: Path | None = None
    if entry.initrd:
        initrd = directory / "initrd"
        image.extract(entry.initrd, initrd)
    return DirectBoot(entry, kernel, initrd, " ".join(entry.arguments))


def cmdline_problems(entry: BootEntry, live_cmdline: str) -> list[str]:
    """Arguments of the chosen entry that the running live system does not have: it booted another entry (or ignored them)."""
    present = set(live_cmdline.split())
    return [argument for argument in entry.arguments if argument not in present]
