"""Screenshots of the live desktop and of every installer page at 1366x768 and 1920x1080 (the harness's side).

``scripts/test-vm.py --scenario screenshots`` boots the live ISO once per size on a VM whose display has exactly that size
(``VMHardware.resolution``: the virtual graphics card announces it in its EDID, so the live X server starts in it), waits for the XFCE
session, takes a picture of the whole screen through QMP (what a person sees when the ISO has booted: the desktop with the installer
window) and runs ``scripts/vm-gui-shots.py`` as the live user (a picture of each installer page, Guided and Advanced). This module
holds what can be tested without a VM: the PPM to PNG conversion, the safe unpacking of what the guest sends back, and the judgement of
the result.
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import re
import shlex
import struct
import tarfile
import zlib
from pathlib import Path
from typing import Any, Mapping, Sequence

SIZES = ("1366x768", "1920x1080")
SIZE_RE = re.compile(r"([1-9][0-9]{2,3})x([1-9][0-9]{2,3})")
GUEST_DIR = "/tmp/gentoo-vm-shots"
GUEST_SCRIPT = "/run/vm-gui-shots.py"
MEMBER_RE = re.compile(r"(?:\./)?[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:png|json)")
MAX_MEMBER = 32 << 20
MAX_TOTAL = 256 << 20
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ShotsError(ValueError):
    """The guest's answer cannot be used (not a PNG, an unsafe archive, a size nobody asked for)."""


def parse_sizes(text: str) -> list[tuple[int, int]]:
    """'1366x768,1920x1080' -> [(1366, 768), (1920, 1080)]; anything else is an error naming the culprit."""
    sizes = []
    for part in (item.strip() for item in text.split(",") if item.strip()):
        match = SIZE_RE.fullmatch(part)
        if match is None:
            raise ShotsError(f"{part!r} is not a screen size like 1366x768")
        size = (int(match.group(1)), int(match.group(2)))
        if size not in sizes:
            sizes.append(size)
    if not sizes:
        raise ShotsError("no screen size given")
    return sizes


def size_name(size: tuple[int, int]) -> str:
    return f"{size[0]}x{size[1]}"


def ppm_header(data: bytes) -> tuple[int, int, int]:
    """(width, height, offset of the pixels) of a binary PPM (P6, maxval 255), comments allowed."""
    fields: list[bytes] = []
    index = 0
    length = len(data)
    while len(fields) < 4 and index < length:
        while index < length and data[index:index + 1].isspace():
            index += 1
        if data[index:index + 1] == b"#":
            while index < length and data[index:index + 1] != b"\n":
                index += 1
            continue
        start = index
        while index < length and not data[index:index + 1].isspace():
            index += 1
        fields.append(data[start:index])
    if len(fields) < 4 or fields[0] != b"P6":
        raise ShotsError("the screendump is not a binary PPM (P6)")
    try:
        width, height, maxval = int(fields[1]), int(fields[2]), int(fields[3])
    except ValueError as exc:
        raise ShotsError("the screendump header is not numbers") from exc
    if maxval != 255 or not 0 < width <= 16384 or not 0 < height <= 16384:
        raise ShotsError(f"the screendump is {width}x{height} with maxval {maxval}")
    offset = index + 1  # one whitespace byte follows maxval
    if len(data) - offset < width * height * 3:
        raise ShotsError("the screendump is shorter than its header says")
    return width, height, offset


def ppm_to_png(data: bytes) -> bytes:
    """The PNG (8-bit RGB) of a PPM screendump; QEMU writes PPM, a person wants PNG."""
    width, height, offset = ppm_header(data)
    stride = width * 3
    raw = b"".join(b"\x00" + data[offset + row * stride:offset + (row + 1) * stride] for row in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def png_size(data: bytes) -> tuple[int, int]:
    if not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise ShotsError("not a PNG image")
    return struct.unpack(">II", data[16:24])


def shots_command(environment: Mapping[str, str], settle_ms: int = 600) -> str:
    """The command that photographs the installer pages as the live user inside the live X session."""
    env_args = [f"{key}={value}" for key, value in environment.items()]
    return shlex.join(("runuser", "-u", "gentoo", "--", "env", *env_args, "python3", GUEST_SCRIPT, GUEST_DIR, "--settle", str(settle_ms)))


def fetch_command() -> str:
    """One file with every picture: a tar archive, base64 encoded (the SSH channel carries text)."""
    return f"tar -C {GUEST_DIR} -cf - . | base64 -w0"


def unpack(payload: str, destination: Path) -> list[str]:
    """Write the pictures and shots.json of the guest's archive below ``destination``; refuse anything but plain, small, well-named files."""
    try:
        raw = base64.b64decode(payload.strip(), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ShotsError("the pictures did not arrive as base64") from exc
    destination.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for member in archive:
                if member.isdir() and member.name in (".", "./"):
                    continue
                if not member.isfile() or MEMBER_RE.fullmatch(member.name) is None:
                    raise ShotsError(f"the archive holds {member.name!r}, which is not a picture or an index")
                if member.size > MAX_MEMBER or (total := total + member.size) > MAX_TOTAL:
                    raise ShotsError("the archive is larger than any set of pictures should be")
                data = archive.extractfile(member).read()  # type: ignore[union-attr]
                name = member.name.removeprefix("./")
                if name.endswith(".png"):
                    png_size(data)
                (destination / name).write_bytes(data)
                written.append(name)
    except tarfile.TarError as exc:
        raise ShotsError(f"the pictures did not arrive as a tar archive: {exc}") from exc
    return sorted(written)


def judge(size: tuple[int, int], index: Mapping[str, Any], desktop: tuple[int, int] | None, files: Sequence[str],
          expected_modes: Sequence[str] = ("guided", "advanced")) -> list[str]:
    """What is wrong with one size's pictures: the display was not the size asked for, pages are missing in a mode, the guest reported a problem."""
    problems: list[str] = []
    wanted = list(size)
    if desktop is not None and desktop != size:
        problems.append(f"the screendump of the desktop is {desktop[0]}x{desktop[1]}, not {size_name(size)}")
    if list(index.get("screen") or []) != wanted:
        problems.append(f"the live system's screen is {'x'.join(map(str, index.get('screen') or [])) or 'unknown'}, not {size_name(size)}: the graphics card did not offer the size")
    shown = [page for page in index.get("pages", []) if "full_page_of" not in page]
    if not shown:
        problems.append("the guest took no picture of any installer page")
    for mode in expected_modes:
        if not any(page.get("mode") == mode for page in shown):
            problems.append(f"no installer page was photographed in {mode} mode")
    names = {page.get("page") for page in shown}
    for page in shown:
        if page.get("file") not in files:
            problems.append(f"{page.get('file')} is listed but did not arrive")
    if len(names) < 3:
        problems.append(f"only {len(names)} different pages were photographed")
    problems += [f"installer: {problem}" for problem in index.get("problems", [])]
    return problems


def summary(size: tuple[int, int], files: Sequence[str], problems: Sequence[str]) -> dict[str, Any]:
    return {"size": size_name(size), "pictures": len(files), "desktop": "live-desktop.png" in files, "problems": list(problems)}


def write_index(directory: Path, results: Sequence[Mapping[str, Any]]) -> Path:
    path = directory / "index.json"
    path.write_text(json.dumps({"sizes": list(results)}, indent=2) + "\n")
    return path
