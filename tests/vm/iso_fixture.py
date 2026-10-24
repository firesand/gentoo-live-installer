"""A tiny ISO 9660 writer for tests: enough to put directories and files into an image the harness's reader must understand.

It follows ECMA-119 at level 1 (upper-case 8.3 names with ';1'), optionally adds Rock Ridge 'NM' entries with the
real names, and keeps every directory in one sector, which is all a test tree needs.
"""
from __future__ import annotations

import struct
from pathlib import Path

SECTOR = 2048


def both32(value: int) -> bytes:
    return struct.pack("<I", value) + struct.pack(">I", value)


def both16(value: int) -> bytes:
    return struct.pack("<H", value) + struct.pack(">H", value)


def record(identifier: bytes, lba: int, size: int, directory: bool, system_use: bytes = b"") -> bytes:
    padding = b"\0" if len(identifier) % 2 == 0 else b""
    body = b"\0" + both32(lba) + both32(size) + bytes(7) + bytes([2 if directory else 0]) + b"\0\0" + both16(1) + bytes([len(identifier)]) + identifier + padding + system_use
    if (len(body) + 1) % 2:
        body += b"\0"
    return bytes([len(body) + 1]) + body


def iso_name(name: str, directory: bool) -> bytes:
    upper = name.upper()
    if directory:
        return upper.encode()
    return (upper if "." in upper else upper + ".").encode() + b";1"


def build_iso(files: dict[str, bytes], *, rock_ridge: bool = False, label: str = "TEST") -> bytes:
    """An ISO image holding ``files`` ({'/boot/grub/grub.cfg': b'...'})."""
    tree: dict = {}
    for path, data in files.items():
        parts = [part for part in path.split("/") if part]
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = data

    directories: list[dict] = []

    def collect(node: dict) -> None:
        directories.append(node)
        for child in node.values():
            if isinstance(child, dict):
                collect(child)

    collect(tree)
    lba = 18
    placed: dict[int, int] = {}
    for node in directories:
        placed[id(node)] = lba
        lba += 1
    for node in directories:
        for child in node.values():
            if isinstance(child, bytes):
                placed[id(child)] = lba
                lba += max(1, -(-len(child) // SECTOR))
    total = lba

    def directory_extent(node: dict, parent: dict) -> bytes:
        entries = [record(b"\x00", placed[id(node)], SECTOR, True), record(b"\x01", placed[id(parent)], SECTOR, True)]
        for name, child in sorted(node.items()):
            directory = isinstance(child, dict)
            extent = placed[id(child)]
            size = SECTOR if directory else len(child)
            system_use = b"NM" + bytes([5 + len(name), 1, 0]) + name.encode() if rock_ridge else b""
            entries.append(record(iso_name(name, directory), extent, size, directory, system_use))
        data = b"".join(entries)
        assert len(data) <= SECTOR, "test directories must fit in one sector"
        return data.ljust(SECTOR, b"\0")

    image = bytearray(total * SECTOR)
    volume = bytearray(SECTOR)
    volume[0:7] = b"\x01CD001\x01"
    volume[40:72] = label.encode().ljust(32)
    volume[80:88] = both32(total)
    volume[120:124] = both16(1)
    volume[124:128] = both16(1)
    volume[128:132] = both16(SECTOR)
    volume[156:190] = record(b"\x00", placed[id(tree)], SECTOR, True)
    image[16 * SECTOR:17 * SECTOR] = volume
    image[17 * SECTOR:17 * SECTOR + 7] = b"\xffCD001\x01"
    for node in directories:
        parent = next((other for other in directories if any(child is node for child in other.values() if isinstance(child, dict))), node)
        start = placed[id(node)] * SECTOR
        image[start:start + SECTOR] = directory_extent(node, parent)
        for child in node.values():
            if isinstance(child, bytes):
                offset = placed[id(child)] * SECTOR
                image[offset:offset + len(child)] = child
    return bytes(image)


def write_iso(path: Path, files: dict[str, bytes], **options) -> Path:
    path.write_bytes(build_iso(files, **options))
    return path
