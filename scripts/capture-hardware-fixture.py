#!/usr/bin/env python3
"""Copy exactly the files the hardware probe reads into a redacted fixture tree.

    scripts/capture-hardware-fixture.py DEST [--root /] [--force] [--no-redact]

The probe (gentoo_installer.hardware) is run with a recorder, so the fixture
contains precisely what it touched and nothing else. Only read-only tools are
run (lsblk, lscpu, cpuid2cpuflags, efibootmgr -v, chronyc tracking). The result
is a directory that ``probe(DEST)`` or ``python -m gentoo_installer.hardware
--root DEST`` can replay on any machine, and a bug report that lets a developer
reproduce what the installer decided.

Conventions (shared with tests/hardware/synthetic.py):

* No real symlinks. A symlink becomes an empty directory placeholder and its
  link text is stored in ``.links.json`` (path -> text); hardware.Fs.readlink
  consults that file for entries that are not real symlinks.
* Command output lives in ``.commands/`` (see hardware.command_key).
* ``usr/share/hwdata/pci.ids`` is trimmed to the devices present.
* Redaction (on by default): disk serials, WWNs and UUIDs, EDID serial numbers,
  MAC addresses (also inside names), Bluetooth unique ids, USB/DMI serial numbers and the product UUID if a probe ever read them, mounts, command line
  tokens that are not hardware switches, and the NTP server address are removed.
  Nothing the probe reads contains a hostname, DMI serial or product UUID.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from gentoo_installer import hardware  # noqa: E402

TOOL_VERSION = 1
MAC_RE = re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}:){5}[0-9a-f]{2}(?![0-9a-f])")
ENX_RE = re.compile(r"(?i)\benx[0-9a-f]{12}\b")
SAFE_CMDLINE = re.compile(
    r"^(?:root=live:\S+|rd\.live\.\S+|cdroot|nodhcp|secureconsole|efi=\S+|nomodeset|quiet|splash|ro|rw|ignore_loglevel|loglevel=\d|"
    r"console=\S+|nox|dokeymap|dospeakup|rd\.debug|pcie_aspm=\S+|nvme_core\.\S+|usbcore\.\S+|acpi\S*|noapic|\w+_iommu=\S+|iommu=\S+|"
    r"mitigations=\S+|video=\S+|BOOT_IMAGE=\S+|initrd=\S+)$")


class FixtureWriter:
    """Writes a probe fixture tree: real files and directories, symlinks as sidecar entries."""

    def __init__(self, dest: Path, mac_map: dict[str, str] | None = None) -> None:
        self.dest = dest
        self.links: dict[str, str] = {}
        self.commands: dict[str, str] = {}
        self.mac_map = mac_map if mac_map is not None else {}

    def fake_mac(self, mac: str) -> str:
        key = mac.lower()
        if key not in self.mac_map:
            self.mac_map[key] = "aa:bb:cc:dd:ee:%02x" % (len(self.mac_map) + 1)
        return self.mac_map[key]

    def scrub_name(self, text: str) -> str:
        """Replace MAC addresses (and enx<MAC> interface names) with stable fakes."""
        text = MAC_RE.sub(lambda m: self.fake_mac(m.group(0)), text)

        def enx(match: re.Match[str]) -> str:
            digits = match.group(0)[3:].lower()
            return "enx" + self.fake_mac(":".join(digits[i:i + 2] for i in range(0, 12, 2))).replace(":", "")

        return ENX_RE.sub(enx, text)

    def _real(self, path: str) -> Path:
        return self.dest / path.lstrip("/")

    def mkdir(self, path: str) -> None:
        self._real(path).mkdir(parents=True, exist_ok=True)

    def write_file(self, path: str, content: bytes | str) -> None:
        target = self._real(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode()
        target.write_bytes(content)

    def link(self, path: str, target: str) -> None:
        """A symlink: placeholder directory plus the link text in the sidecar."""
        self.links[path.rstrip("/")] = target
        if not self._real(path).exists():
            self.mkdir(path)

    def command(self, key: str, output: str) -> None:
        self.commands[key] = output

    def keep_empty_directories(self) -> None:
        """git does not store empty directories, so mark them (hardware.Fs.listdir hides the marker)."""
        for directory, children, files in os.walk(self.dest):
            if not children and not files and Path(directory) != self.dest:
                (Path(directory) / hardware.KEEP_MARKER).write_text("")

    def finish(self, meta: dict[str, Any] | None = None) -> None:
        self.keep_empty_directories()
        if self.links:
            (self.dest / ".links.json").write_text(json.dumps(dict(sorted(self.links.items())), indent=1) + "\n")
        if self.commands:
            (self.dest / ".commands").mkdir(exist_ok=True)
            for key, output in self.commands.items():
                (self.dest / ".commands" / key).write_text(output)
        if meta is not None:
            (self.dest / ".capture.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")


# -- redactors ------------------------------------------------------------------

def redact_edid(blob: bytes) -> bytes:
    """Zero the ID serial number and drop serial-number descriptors, then fix the checksum."""
    if len(blob) < 128 or blob[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return blob
    data = bytearray(blob)
    data[12:16] = b"\x00\x00\x00\x00"
    for offset in (54, 72, 90, 108):
        if data[offset:offset + 3] == b"\x00\x00\x00" and data[offset + 3] in (0xFF, 0xFE):
            data[offset + 3] = 0xFE  # "unspecified text" instead of a serial number
            data[offset + 5:offset + 18] = b"redacted\n     "
    data[127] = (-sum(data[:127])) & 0xFF
    return bytes(data)


def redact_lsblk(text: str) -> str:
    """Remove serials, WWNs and every UUID from lsblk --json output; keep fs types and well-known labels."""
    try:
        raw = json.loads(text)
    except ValueError:
        return text
    counter = {"n": 0}

    def clean(node: dict[str, Any]) -> None:
        for key in ("serial", "wwn"):
            if node.get(key):
                counter["n"] += 1
                node[key] = f"REDACTED{counter['n']}"
        for key in ("uuid", "partuuid", "ptuuid"):
            if node.get(key):
                node[key] = "00000000-0000-0000-0000-000000000000"
        label = node.get("label")
        if label and not re.fullmatch(r"GENTOO_INSTALLER_\w+|EFI|ESP|boot|BOOT|root|swap|home|Gentoo|ISOIMAGE|Windows|Recovery|System Reserved|SYSTEM|WINRE_DRV", label):
            node["label"] = "LABEL"
        if node.get("partlabel"):
            node["partlabel"] = "PARTLABEL"
        if node.get("mountpoints"):
            node["mountpoints"] = [mp if mp is None or mp in ("/", "/boot", "/efi", "/boot/efi", "/run/initramfs/live", "[SWAP]") else "/mnt/redacted"
                                   for mp in node["mountpoints"]]
        for child in node.get("children") or []:
            clean(child)

    for device in raw.get("blockdevices") or []:
        clean(device)
    return json.dumps(raw, indent=2) + "\n"


def trim_cpuinfo(text: str) -> str:
    """Keep the first logical CPU in full and only a ``processor`` stub for the others."""
    blocks = [block for block in text.strip().split("\n\n") if block.strip()]
    if not blocks:
        return text
    stubs = [f"processor\t: {index}" for index in range(1, len(blocks))]
    return "\n\n".join([blocks[0], *stubs]) + "\n"


_INPUT_NAMES = (
    (re.compile(r"Lid Switch", re.I), "Lid Switch"),
    (re.compile(r"Power Button", re.I), "Power Button"),
    (re.compile(r"Sleep Button", re.I), "Sleep Button"),
    (re.compile(r"Video Bus", re.I), "Video Bus"),
    (re.compile(r"controller|dualsense|dualshock|gamepad|joystick|x-box|xbox|8bitdo", re.I), "Game Controller"),
    (re.compile(r"\bpen\b|stylus", re.I), "Stylus Pen"),
    (re.compile(r"tablet.?mode", re.I), "Tablet Mode Switch"),
    (re.compile(r"touch\s*pad|trackpad|synaptics", re.I), "Touchpad"),
    (re.compile(r"touch\s*screen|goodix|ilitek|egalax", re.I), "Touchscreen"),
    (re.compile(r"keyboard", re.I), "Keyboard"),
)


def redact_input_devices(text: str) -> str:
    """Drop unique ids and physical paths; reduce device names to the class the probe recognises."""
    out: list[str] = []
    for block in text.strip().split("\n\n"):
        lines = []
        for line in block.splitlines():
            if line.startswith(("P:", "U:", "S:")):
                continue
            if line.startswith("N:"):
                name = re.search(r'Name="(.*)"', line)
                value = name.group(1) if name else ""
                label = next((canonical for pattern, canonical in _INPUT_NAMES if pattern.search(value)), "Input device")
                if label == "Game Controller" and re.search(r"touch\s*pad", value, re.I):
                    label = "Game Controller Touchpad"
                line = f'N: Name="{label}"'
            lines.append(line)
        out.append("\n".join(lines))
    return "\n\n".join(out) + "\n"


def redact_cmdline(text: str) -> str:
    return " ".join(token for token in text.split() if SAFE_CMDLINE.match(token)) + "\n"


def redact_mounts(text: str) -> str:
    keep = []
    for line in text.splitlines():
        parts = line.split()
        if "/run/initramfs/live" in parts or "efivarfs" in parts:
            keep.append(line)
    return "\n".join(keep) + ("\n" if keep else "")


def redact_route(text: str) -> str:
    """Keep interface, flags and metric; replace every address with a documentation-style fake."""
    lines = text.splitlines()
    out = lines[:1]
    for line in lines[1:]:
        parts = line.split()
        if len(parts) >= 8:
            default = parts[1] == "00000000"
            parts[1] = "00000000" if default else "0000FEA9"
            parts[2] = "00000000" if parts[2] == "00000000" else "0101FEA9"
            parts[7] = "00000000" if default else "0000FFFF"
        out.append("\t".join(parts))
    return "\n".join(out) + "\n"


def redact_efibootmgr(text: str) -> str:
    lines = []
    for line in text.splitlines():
        match = re.match(r"(Boot[0-9A-Fa-f]{4}\*?)\s+(.*?)(?:\t|$)", line)
        if match:
            lines.append(f"{match.group(1)} {match.group(2)}")
    return "\n".join(lines) + ("\n" if lines else "")


def redact_chrony(text: str) -> str:
    parts = text.strip().split(",")
    if len(parts) >= 14:
        parts[0], parts[1] = "7F7F0101", "127.127.1.1"
    return ",".join(parts) + "\n"


def trim_lscpu(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if line.startswith(("Architecture:", "Model name:", "Hypervisor vendor:", "Virtualization type:"))) + "\n"


# -- capture ---------------------------------------------------------------------

def _sanitize_command(key: str, output: str) -> str:
    return {
        "lsblk.json": redact_lsblk,
        "lscpu.txt": trim_lscpu,
        "efibootmgr.txt": redact_efibootmgr,
        "chronyc-tracking.txt": redact_chrony,
    }.get(key, lambda value: value)(output)


class _RecordingRunner:
    def __init__(self, inner: Any, sink: dict[str, str]) -> None:
        self.inner, self.sink = inner, sink

    def run(self, argv, **kwargs):  # noqa: ANN001
        result = self.inner.run(argv, **kwargs)
        if not getattr(result, "returncode", 0):
            self.sink[hardware.command_key(argv)] = result.stdout
        return result


def capture(root: Path, dest: Path, *, redact: bool = True, now: float | None = None) -> dict[str, Any]:
    """Probe ``root`` and write the fixture to ``dest``; returns the capture metadata."""
    records: list[tuple[str, str]] = []
    commands: dict[str, str] = {}
    base = hardware.default_root(root)
    inner = hardware.LocalRunner() if str(base) == "/" else hardware.FixtureRunner(base)
    moment = time.time() if now is None else now
    profile = hardware.probe(base, _RecordingRunner(inner, commands), now=moment, recorder=lambda kind, path: records.append((kind, path)))
    writer = FixtureWriter(dest)
    source_fs = hardware.Fs(base)  # sees real symlinks and, for a fixture source, its .links.json sidecar

    def source(path: str) -> str:
        return os.path.join(str(base), path.lstrip("/"))

    def scrub(value: str) -> str:
        return writer.scrub_name(value) if redact else value

    def link_text(path: str) -> str | None:
        return source_fs.readlink(path)

    def place_link(path: str) -> None:
        """A symlink in the source: placeholder directory + sidecar entry."""
        writer.link(scrub(path), scrub(link_text(path) or ""))

    def materialise(path: str, final: str) -> None:
        """Create the parents of ``path``. final: 'dir' | 'file' | 'link' says what the last component is."""
        parts = [part for part in path.split("/") if part]
        current = ""
        for index, part in enumerate(parts):
            current += "/" + part
            last = index == len(parts) - 1
            if last and final == "file":
                return
            if link_text(current) is not None:
                place_link(current)
            elif not last or final == "dir":
                writer.mkdir(scrub(current))

    done: set[tuple[str, str]] = set()
    pci_ids_read = False
    for kind, path in records:
        if (kind, path) in done:
            continue
        done.add((kind, path))
        if path in hardware.PCI_IDS_PATHS:
            pci_ids_read = True
            continue
        src = source(path)
        if kind == "list":
            materialise(path, "dir")
            try:
                names = sorted(os.listdir(src))
            except OSError:
                continue
            for name in names:
                child = f"{path.rstrip('/')}/{name}"
                if link_text(child) is not None:
                    place_link(child)
                elif os.path.isdir(source(child)):
                    writer.mkdir(scrub(child))
        elif kind == "link":
            materialise(path, "link")
        elif kind == "stat":
            if not os.path.lexists(src):
                continue
            if link_text(path) is not None or os.path.isdir(src):
                materialise(path, "dir")
            else:
                materialise(path, "file")
                if not writer._real(scrub(path)).exists():
                    writer.write_file(scrub(path), b"")
        elif kind == "read":
            materialise(path, "file")
            try:
                content = Path(src).read_bytes()
            except OSError:
                continue
            writer.write_file(scrub(path), _process_file(path, content, redact))
    for key, output in commands.items():
        writer.command(key, _sanitize_command(key, output) if redact else output)
    if pci_ids_read:
        _write_trimmed_pci_ids(base, dest, writer)
    meta = {"tool": "capture-hardware-fixture", "tool_version": TOOL_VERSION, "schema": hardware.SCHEMA, "now": int(moment),
            "redacted": redact, "summary": profile.summary()}
    writer.finish(meta)
    return meta


SERIAL_FILES = ("serial", "device_serial", "product_serial", "board_serial", "chassis_serial", "product_uuid", "uniq")


def is_serial_file(path: str) -> bool:
    """Files that identify one unit: USB and DMI serial numbers, the product UUID, an input device's unique id. The probe never asks for them;
    if a newer probe or a hand-made recorder does, they are blanked rather than committed."""
    return path.rsplit("/", 1)[-1] in SERIAL_FILES


def _process_file(path: str, content: bytes, redact: bool) -> bytes:
    if not redact:
        return content
    text = lambda: content.decode("utf-8", "replace")  # noqa: E731
    if path == "/proc/cpuinfo":
        return trim_cpuinfo(text()).encode()
    if path == "/proc/bus/input/devices":
        return redact_input_devices(text()).encode()
    if path == "/proc/cmdline":
        return redact_cmdline(text()).encode()
    if path in ("/proc/mounts", "/proc/self/mountinfo"):
        return redact_mounts(text()).encode()
    if path == "/proc/net/route":
        return redact_route(text()).encode()
    if path.endswith("/edid"):
        return redact_edid(content)
    if is_serial_file(path):
        return b"REDACTED\n"
    return content


def _write_trimmed_pci_ids(base: Path, dest: Path, writer: FixtureWriter) -> None:
    """A pci.ids that holds only the vendors and devices present in the captured sysfs."""
    wanted: set[tuple[str, str]] = set()
    devices = dest / "sys/bus/pci/devices"
    if devices.is_dir():
        for slot in devices.iterdir():
            try:
                vendor = (slot / "vendor").read_text().strip().removeprefix("0x")
                device = (slot / "device").read_text().strip().removeprefix("0x")
            except OSError:
                continue
            wanted.add((vendor.lower(), device.lower()))
    for candidate in hardware.PCI_IDS_PATHS:
        path = Path(os.path.join(str(base), candidate.lstrip("/")))
        if path.is_file():
            vendors, names = hardware.parse_pci_ids(path.read_text(errors="replace"), wanted)
            lines = []
            for vendor in sorted(vendors):
                lines.append(f"{vendor}  {vendors[vendor]}")
                lines.extend(f"\t{device}  {name}" for (v, device), name in sorted(names.items()) if v == vendor)
            writer.write_file(candidate, "\n".join(lines) + "\n")
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture this machine's hardware probe inputs as a redacted fixture tree.")
    parser.add_argument("dest", type=Path, help="directory to create (must not exist unless --force)")
    parser.add_argument("--root", type=Path, default=Path("/"), help="capture from another tree instead of this machine")
    parser.add_argument("--force", action="store_true", help="replace an existing destination")
    parser.add_argument("--no-redact", action="store_true", help="keep serials, UUIDs and MAC addresses (never commit such a fixture)")
    args = parser.parse_args(argv)
    if args.dest.exists():
        if not args.force:
            print(f"{args.dest} exists; use --force to replace it", file=sys.stderr)
            return 1
        shutil.rmtree(args.dest)
    args.dest.mkdir(parents=True)
    meta = capture(args.root, args.dest, redact=not args.no_redact)
    print(f"Captured: {meta['summary']}")
    print(f"Fixture written to {args.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
