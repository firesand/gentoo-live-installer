#!/usr/bin/env python3
"""Static acceptance checks for a finished Gentoo installer and rescue ISO.

    scripts/verify-iso.py ISO [--flavor release|testkey] [--workspace DIR] [--json FILE]

Catalyst exits 0 even when every `rc-update add` failed, when the overlay is missing and
when the boot menu is Catalyst's own, so a built ISO proves nothing by existing. This
script opens the ISO read-only and checks what a user's PC will depend on:

  * El Torito: exactly one BIOS and one UEFI boot image, a hybrid MBR/GPT for USB sticks,
    the volume label that grub.cfg searches for, the GRUB loaders for 64-bit and 32-bit UEFI;
  * boot/grub/grub.cfg: every `linux` line carries root=live:CDLABEL=<volume id> and the
    boot arguments of iso/pins.env, the default entry has no `dokeymap`, there are text-console,
    nomodeset and Memtest86+ entries, no entry promises a "safe graphics" desktop;
  * the initramfs: dmsquash-live and the storage modules a live medium needs;
  * image.squashfs: the required files and commands, every service in livecd/rcadd as a
    runlevel link plus init script, no builder-owned files (uid/gid 1000), a locked root
    account, no builder make.conf settings, and in a release no test-access artifacts;
  * the installed package list, written to --packages-out.

The rules live in iso/image-contract.json. The ISO is read with xorriso/isoinfo and
rdsquashfs (squashfs-tools-ng); nothing is mounted. Exit status: 0 all checks passed,
1 a check failed, 2 the ISO could not be read or the arguments are wrong.
"""
from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import io
import json
import lzma
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence

SCHEMA_VERSION = 1
FLAVORS = ("release", "testkey")
TESTKEY_VOLID_SUFFIX = "_TESTKEY"
LIVE_USER_ID = 1000


class VerifyError(Exception):
    """The ISO or a tool could not be read (as opposed to a failed check)."""


# ---------------------------------------------------------------------------
# results


@dataclasses.dataclass
class Check:
    id: str
    ok: bool
    detail: str = ""
    severity: str = "error"  # a failed "warning" does not fail the run


class Report:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def passed(self, check_id: str, detail: str = "") -> None:
        self.checks.append(Check(check_id, True, detail))

    def failed(self, check_id: str, detail: str, severity: str = "error") -> None:
        self.checks.append(Check(check_id, False, detail, severity))

    def expect(self, condition: bool, check_id: str, ok_detail: str, fail_detail: str, severity: str = "error") -> bool:
        if condition:
            self.passed(check_id, ok_detail)
        else:
            self.failed(check_id, fail_detail, severity)
        return condition

    @property
    def errors(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.severity == "error"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self, flavor: str) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "flavor": flavor,
            "ok": self.ok,
            "checks": [dataclasses.asdict(c) for c in self.checks],
            "errors": [c.id + ": " + c.detail for c in self.errors],
            "warnings": [c.id + ": " + c.detail for c in self.warnings],
        }


# ---------------------------------------------------------------------------
# naming (mirrors iso/lib/naming.sh; tests keep the two in step)


def flavor_volume_id(volume_id: str, flavor: str) -> str:
    if flavor == "testkey" and not volume_id.endswith(TESTKEY_VOLID_SUFFIX):
        stem = volume_id[: -len("_AMD64")] if volume_id.endswith("_AMD64") else volume_id
        return stem + TESTKEY_VOLID_SUFFIX
    return volume_id


def load_pins(path: Path) -> dict[str, str]:
    """Read a pins.env file (KEY=value lines, optionally quoted)."""
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        pins[key.strip()] = value
    return pins


# ---------------------------------------------------------------------------
# Catalyst spec


def parse_spec(text: str) -> dict[str, Any]:
    """Parse a Catalyst spec the way catalyst.config.SpecParser does.

    "key: value" starts a key (the separator is the first colon of ANY line, which is
    why a continuation line cannot contain one), further lines add words. One word is
    a str, several are a list.
    """
    values: dict[str, Any] = {}
    current: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"\s*#.*$", "", raw.strip())
        if not line:
            continue
        if ":" in line:
            key, _, rest = line.partition(":")
            current = [key]
            rest = rest.strip().strip('"')
            if rest:
                current += rest.split()
        else:
            current += line.split()
        values[current[0]] = current[1] if len(current) == 2 else current[1:]
    return values


def spec_list(spec: Mapping[str, Any], key: str) -> list[str]:
    value = spec.get(key, [])
    return [value] if isinstance(value, str) else list(value)


# ---------------------------------------------------------------------------
# external tools


class Tools:
    """The commands that read an ISO. Tests replace this with canned answers."""

    def __init__(self, iso: Path, workdir: Path) -> None:
        self.iso = iso
        self.workdir = workdir

    def _run(self, argv: Sequence[str]) -> bytes:
        try:
            proc = subprocess.run(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        except FileNotFoundError as exc:
            raise VerifyError(f"required tool is missing: {argv[0]}") from exc
        if proc.returncode != 0:
            raise VerifyError(f"{' '.join(argv)} failed: {proc.stderr.decode(errors='replace').strip()[-400:]}")
        return proc.stdout

    def el_torito_report(self) -> str:
        return self._run([
            "xorriso", "-indev", str(self.iso), "-pvd_info",
            "-report_system_area", "plain", "-report_el_torito", "plain",
        ]).decode(errors="replace")

    def iso_files(self) -> list[str]:
        out = self._run(["isoinfo", "-R", "-f", "-i", str(self.iso)]).decode(errors="replace")
        return [line for line in out.splitlines() if line.strip()]

    def extract(self, path: str, destination: Path) -> Path:
        """Copy one file out of the ISO. (isoinfo -x silently writes nothing for files over 2 GiB.)"""
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.unlink(missing_ok=True)
        self._run(["xorriso", "-osirrox", "on", "-indev", str(self.iso), "-extract", path, str(destination)])
        if not destination.is_file() or destination.stat().st_size == 0:
            raise VerifyError(f"cannot extract {path} from the ISO")
        return destination

    def iso_read(self, path: str) -> bytes:
        target = self.workdir / ("extract-" + path.strip("/").replace("/", "_"))
        try:
            return self.extract(path, target).read_bytes()
        finally:
            target.unlink(missing_ok=True)

    def squashfs(self) -> Path:
        target = self.workdir / "image.squashfs"
        if not target.exists():
            self.extract("/image.squashfs", target)
        return target

    def squashfs_listing(self) -> str:
        return self._run(["rdsquashfs", "-d", str(self.squashfs())]).decode(errors="replace")

    def squashfs_cat(self, path: str) -> bytes:
        return self._run(["rdsquashfs", "-c", path, str(self.squashfs())])

    def squashfs_root_stat(self) -> str:
        return self._run(["rdsquashfs", "-s", "/", str(self.squashfs())]).decode(errors="replace")


# ---------------------------------------------------------------------------
# El Torito and ISO contents


@dataclasses.dataclass
class ElTorito:
    volume_id: str
    boot_record: str
    images: list[tuple[str, str]]  # (platform, path)


def parse_el_torito(text: str) -> ElTorito:
    """Parse the output of `xorriso -indev ISO -pvd_info -report_system_area plain -report_el_torito plain`."""
    volume_id = ""
    boot_record = ""
    platforms: dict[str, str] = {}
    paths: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        match = re.match(r"Volume [Ii]d\s*:\s*(?:'(.*)'|(\S*))\s*$", line)
        if match:
            volume_id = match.group(1) if match.group(1) is not None else match.group(2)
        match = re.match(r"(?:System area summary|Boot record)\s*:\s*(.*)", line)
        if match:
            boot_record = match.group(1)
        match = re.match(r"El Torito boot img\s*:\s*(\d+)\s+(\S+)\s", line)
        if match:
            platforms[match.group(1)] = match.group(2)
        match = re.match(r"El Torito img path\s*:\s*(\d+)\s+(\S+)", line)
        if match:
            paths[match.group(1)] = match.group(2)
    images = [(platforms[n], paths.get(n, "")) for n in sorted(platforms, key=int)]
    return ElTorito(volume_id, boot_record, images)


def check_iso(report: Report, iso_files: Iterable[str], boot: ElTorito, contract: Mapping[str, Any], expected_volid: str) -> set[str]:
    """Checks that need only the file list and the El Torito report. Returns the file set."""
    files = {name.strip("/") for name in iso_files if name.strip("/")}
    files = {name.split(";")[0] for name in files}  # isoinfo may print ";1" version suffixes
    report.expect(
        boot.volume_id == expected_volid,
        "iso.volume_id",
        f"volume id {boot.volume_id}",
        f"the volume id is '{boot.volume_id}' but grub.cfg and the build expect '{expected_volid}'",
    )
    report.expect(len(boot.volume_id) <= 32, "iso.volume_id_length", "", "the volume id is longer than 32 characters")
    bios = [path for platform, path in boot.images if platform.upper() == "BIOS"]
    uefi = [path for platform, path in boot.images if platform.upper() == "UEFI"]
    report.expect(
        len(bios) == 1 and len(uefi) == 1 and len(boot.images) == 2,
        "iso.el_torito",
        f"BIOS {bios[0] if bios else ''}, UEFI {uefi[0] if uefi else ''}",
        f"expected exactly one BIOS and one UEFI El Torito image, found {boot.images}",
    )
    record = boot.boot_record.upper()
    report.expect(
        "MBR" in record and "GPT" in record,
        "iso.hybrid",
        boot.boot_record,
        f"the ISO is not a hybrid MBR/GPT image (boot record: '{boot.boot_record}'), so it will not boot from a USB stick",
    )
    for image in bios + uefi:
        if image:  # an image taken from an appended partition has no path
            report.expect(image.strip("/") in files, "iso.boot_image_present", image, f"the El Torito image {image} is not in the ISO")
    required = [name for name in contract.get("required_files", [])]
    missing = [name for name in required if name not in files]
    report.expect(not missing, "iso.required_files", f"{len(required)} files present", "missing from the ISO: " + ", ".join(missing))
    required_dirs = [name for name in contract.get("required_dirs", [])]
    missing_dirs = [name for name in required_dirs if not any(f == name or f.startswith(name + "/") for f in files)]
    report.expect(not missing_dirs, "iso.required_dirs", "", "missing directories in the ISO: " + ", ".join(missing_dirs))
    forbidden = [name for name in contract.get("forbidden_files", []) if any(fnmatch.fnmatch(f, name) for f in files)]
    report.expect(not forbidden, "iso.forbidden_files", "", "must not be in the ISO: " + ", ".join(forbidden))
    return files


# ---------------------------------------------------------------------------
# grub.cfg


@dataclasses.dataclass
class MenuEntry:
    title: str
    lines: list[str]
    submenu: bool = False

    @property
    def linux(self) -> list[str]:
        """The `linux` lines that boot a live system (Memtest86+ is loaded the same way)."""
        return [line for line in self.lines if line.startswith("linux ") and "memtest" not in line.split()[1]]


def parse_grub_cfg(text: str) -> tuple[list[MenuEntry], dict[str, str]]:
    """Return the menu entries (submenus flattened) and the `set` variables outside entries."""
    entries: list[MenuEntry] = []
    variables: dict[str, str] = {}
    stack: list[MenuEntry | None] = []  # open blocks; None is a submenu
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"(menuentry|submenu)\s+(?:'([^']*)'|\"([^\"]*)\")", line)
        if match:
            title = match.group(2) if match.group(2) is not None else match.group(3)
            if match.group(1) == "submenu":
                stack.append(None)
            else:
                entry = MenuEntry(title, [])
                entries.append(entry)
                stack.append(entry)
            continue
        if line == "}":
            if stack:
                stack.pop()
            continue
        current = stack[-1] if stack else None
        if current is not None:
            current.lines.append(line)
            continue
        match = re.match(r"set\s+(\w+)=(.*)", line)
        if match:
            variables[match.group(1)] = match.group(2).strip().strip("\"'")
    return entries, variables


def check_grub(
    report: Report,
    text: str,
    volid: str,
    bootargs: Sequence[str],
    iso_files: set[str],
    contract: Mapping[str, Any],
) -> None:
    entries, variables = parse_grub_cfg(text)
    rules = contract.get("grub", {})
    report.expect(
        len(entries) >= int(rules.get("min_entries", 5)),
        "grub.entries",
        f"{len(entries)} entries",
        f"grub.cfg has only {len(entries)} menu entries (is Catalyst's own menu still in place?)",
    )
    report.expect(variables.get("default") == "0", "grub.default", "", "grub.cfg must start the first entry by default (set default=0)")
    report.expect(
        variables.get("timeout", "").isdigit() and int(variables["timeout"]) > 0,
        "grub.timeout",
        f"timeout {variables.get('timeout')}",
        "grub.cfg needs a positive timeout so that the default entry starts by itself",
    )
    linux_entries = [e for e in entries if e.linux]
    kernel = rules.get("kernel", "/boot/gentoo")
    initrd = rules.get("initrd", "/boot/gentoo.igz")
    wanted_root = f"root=live:CDLABEL={volid}"
    dracut_tail = [wanted_root, "rd.live.dir=/", "rd.live.squashimg=image.squashfs", "cdroot"]
    problems: list[str] = []
    for entry in linux_entries:
        for line in entry.linux:
            words = line.split()
            if len(words) < 2 or words[1] != kernel:
                problems.append(f"'{entry.title}': kernel is not {kernel}")
                continue
            args = words[2:]
            for needed in list(bootargs) + dracut_tail:
                if needed not in args:
                    problems.append(f"'{entry.title}': missing {needed}")
            roots = [a for a in args if a.startswith("root=live:CDLABEL=")]
            if roots and roots != [wanted_root]:
                problems.append(f"'{entry.title}': {roots[0]} does not match the volume id {volid}")
        if not any(line.split()[:2] == ["initrd", initrd] for line in entry.lines):
            problems.append(f"'{entry.title}': no 'initrd {initrd}'")
        if not any(line.startswith("search ") and f"-l {volid}" in line for line in entry.lines):
            problems.append(f"'{entry.title}': no 'search ... -l {volid}'")
    report.expect(bool(linux_entries) and not problems, "grub.linux_lines", f"{len(linux_entries)} Linux entries", "; ".join(problems) or "no Linux entry")
    for needed in (kernel, initrd):
        report.expect(needed.strip("/") in iso_files, "grub.kernel_files", needed, f"{needed} is referenced by grub.cfg but not in the ISO")
    if entries:
        first = entries[0]
        report.expect(
            bool(first.linux) and not any("dokeymap" in line.split() for line in first.linux),
            "grub.default_entry",
            first.title,
            "the default (first) entry must be a Linux entry without dokeymap, which would stop every boot on a keymap prompt",
        )
        report.expect(
            not any("nox" in line.split() for line in first.linux),
            "grub.default_gui",
            "",
            "the default entry must start the desktop (no 'nox')",
        )
    for entry in linux_entries:
        if any("dokeymap" in line.split() for line in entry.linux) and not any("nox" in line.split() for line in entry.linux):
            report.failed("grub.dokeymap", f"'{entry.title}' has dokeymap but starts the desktop; only the text console entry may ask for a keymap")
    needed_args = rules.get("required_arguments", [])
    for argument in needed_args:
        report.expect(
            any(argument in line.split() for e in linux_entries for line in e.linux),
            f"grub.has_{argument}",
            "",
            f"no entry passes '{argument}'",
        )
    for argument in rules.get("required_argument_groups", []):
        words = argument.split()
        report.expect(
            any(set(words) <= set(line.split()) for e in linux_entries for line in e.linux),
            "grub.argument_group",
            argument,
            f"no entry passes all of: {argument}",
        )
    titles = [e.title.lower() for e in entries]
    for phrase in rules.get("forbidden_titles", []):
        bad = [t for t in titles if phrase.lower() in t]
        report.expect(not bad, "grub.forbidden_titles", phrase, f"menu entry '{bad[0] if bad else ''}' promises something this kernel cannot do ({phrase}); see iso/image-contract.json")
    report.expect(len(set(titles)) == len(titles), "grub.unique_titles", "", "menu entries with the same title (a memtest block emitted twice?)")
    memtest_titles = [t for t in titles if "memtest" in t]
    memtest_files = {name for name in iso_files if name.startswith("memtest")}
    if memtest_files:
        report.expect(bool(memtest_titles), "grub.memtest", ", ".join(sorted(memtest_files)), "memtest images are in the ISO but grub.cfg has no Memtest86+ entry")
    for entry in entries:
        for line in entry.lines:
            words = line.split()
            if words and words[0] in ("linux", "chainloader") and len(words) > 1 and "memtest" in words[1]:
                path = words[1].strip('"').lstrip("/")
                report.expect(path in iso_files, "grub.memtest_files", path, f"'{entry.title}' loads /{path}, which is not in the ISO")


# ---------------------------------------------------------------------------
# initramfs


def _cpio_walk(data: bytes, offset: int) -> tuple[list[str], int]:
    """Names of one newc archive starting at offset, and the offset just after its trailer."""
    names: list[str] = []
    while data[offset : offset + 6] in (b"070701", b"070702"):
        header = data[offset : offset + 110]
        if len(header) < 110:
            raise VerifyError("truncated cpio header in the initramfs")
        filesize = int(header[54:62], 16)
        namesize = int(header[94:102], 16)
        name = data[offset + 110 : offset + 110 + namesize - 1].decode(errors="replace")
        offset = (offset + 110 + namesize + 3) & ~3
        offset = (offset + filesize + 3) & ~3
        if name == "TRAILER!!!":
            return names, offset
        names.append(name)
    return names, offset


def cpio_names(data: bytes) -> list[str]:
    """Names in one newc cpio archive (the format dracut writes)."""
    return _cpio_walk(data, 0)[0]


def initramfs_names(image: bytes) -> list[str]:
    """All file names of a dracut initramfs.

    It is a series of archives: early ones (CPU microcode) that are not compressed, padded
    with zeros to a block boundary, then the main archive compressed with xz or gzip.
    """
    names: list[str] = []
    offset = 0
    while offset < len(image):
        while offset < len(image) and image[offset] == 0:
            offset += 1
        head = image[offset : offset + 6]
        if head in (b"070701", b"070702"):
            found, offset = _cpio_walk(image, offset)
            names += found
        elif head == b"\xfd7zXZ\x00":
            try:
                names += cpio_names(lzma.LZMADecompressor().decompress(image[offset:]))
            except lzma.LZMAError as exc:
                raise VerifyError(f"the initramfs cannot be decompressed: {exc}") from exc
            break
        elif head[:2] == b"\x1f\x8b":
            import gzip

            names += cpio_names(gzip.decompress(image[offset:]))
            break
        else:
            raise VerifyError("the initramfs has an unknown format")
    return names


def check_initramfs(report: Report, names: Sequence[str], contract: Mapping[str, Any], has_dokeymap: bool) -> None:
    rules = contract.get("initramfs", {})
    files = set(names)
    missing = [p for p in rules.get("required", []) if p not in files]
    report.expect(not missing, "initramfs.files", f"{len(rules.get('required', []))} files present", "missing from the initramfs: " + ", ".join(missing))
    modules = {Path(n).name for n in names if Path(n).name.endswith((".ko", ".ko.xz", ".ko.zst", ".ko.gz"))}
    stems = {re.sub(r"\.ko(\.\w+)?$", "", m).replace("_", "-") for m in modules}
    wanted = [m.replace("_", "-") for m in rules.get("required_modules", [])]
    lacking = [m for m in wanted if m not in stems]
    report.expect(not lacking, "initramfs.modules", f"{len(wanted)} modules present", "kernel modules missing from the initramfs: " + ", ".join(lacking))
    if has_dokeymap:
        count = sum(1 for n in names if n.startswith("usr/lib/keymaps/") and n.endswith(".map"))
        report.expect(count >= int(rules.get("min_keymaps", 20)), "initramfs.keymaps", f"{count} keymaps", f"only {count} console keymaps in the initramfs, but an entry asks for dokeymap")


# ---------------------------------------------------------------------------
# squashfs


@dataclasses.dataclass
class Entry:
    kind: str
    mode: int
    uid: int
    gid: int
    target: str = ""


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return re.sub(r"\\(.)", r"\1", token[1:-1])
    return token


def parse_description(text: str) -> dict[str, Entry]:
    """Parse `rdsquashfs -d` output: type path mode uid gid [target]; names may be quoted."""
    entries: dict[str, Entry] = {}
    token = r'(?:"(?:[^"\\]|\\.)*"|\S+)'
    pattern = re.compile(rf"^(?P<kind>\w+) (?P<path>{token}) (?P<mode>[0-7]+) (?P<uid>\d+) (?P<gid>\d+)(?: (?P<target>.*))?$")
    for line in text.splitlines():
        match = pattern.match(line)
        if not match:
            if line.strip():
                raise VerifyError(f"cannot parse the squashfs listing line: {line[:120]!r}")
            continue
        path = _unquote(match["path"])
        target = _unquote(match["target"]) if match["target"] else ""
        entries[path] = Entry(match["kind"], int(match["mode"], 8), int(match["uid"]), int(match["gid"]), target)
    return entries


PACKAGE_VERSION = re.compile(r"^(?P<pn>.+?)-(?P<pv>[0-9]+(?:\.[0-9]+)*[a-z]?(?:_(?:alpha|beta|pre|rc|p)[0-9]*)*)(?:-r[0-9]+)?$")


def installed_packages(entries: Mapping[str, Entry]) -> list[str]:
    """category/package-version of every installed package, from /var/db/pkg."""
    packages = []
    for path, entry in entries.items():
        parts = path.split("/")
        if entry.kind == "dir" and len(parts) == 5 and parts[:3] == ["var", "db", "pkg"]:
            packages.append(f"{parts[3]}/{parts[4]}")
    return sorted(packages)


def package_names(packages: Iterable[str]) -> set[str]:
    names = set()
    for package in packages:
        category, _, pf = package.partition("/")
        match = PACKAGE_VERSION.match(pf)
        names.add(f"{category}/{match['pn'] if match else pf}")
    return names


def _matches(pattern: str, path: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern)


def _command_paths(name: str) -> list[str]:
    return [f"usr/bin/{name}", f"usr/sbin/{name}", f"bin/{name}", f"sbin/{name}"]


def check_squashfs(
    report: Report,
    entries: Mapping[str, Entry],
    contract: Mapping[str, Any],
    rcadd: Sequence[str],
    flavor: str,
    read_file: Callable[[str], bytes],
    root_stat: str,
    builder_ids: Iterable[int],
    source_exists: Callable[[str], bool],
    source_contains: Callable[[str, str], bool] = lambda path, text: False,
) -> None:
    rules = contract.get("squashfs", {})

    def applies(item: Mapping[str, Any]) -> bool:
        condition = item.get("when_source")
        if not condition:
            return True
        if isinstance(condition, str):
            return source_exists(condition)
        return source_contains(condition["path"], condition["contains"])

    def exists(path: str) -> bool:
        return path in entries

    # Required paths, with their type.
    missing: list[str] = []
    wrong: list[str] = []
    for item in rules.get("required", []):
        if not applies(item):
            continue
        path = item["path"]
        entry = entries.get(path)
        if entry is None:
            missing.append(path)
        elif item.get("type") and entry.kind not in item["type"].split("|"):
            wrong.append(f"{path} is a {entry.kind}, expected {item['type']}")
        elif item.get("executable") and not entry.mode & 0o111 and entry.kind == "file":
            wrong.append(f"{path} is not executable")
    report.expect(not missing, "squashfs.required", "", "missing from the image: " + ", ".join(missing))
    report.expect(not wrong, "squashfs.required_types", "", "; ".join(wrong))

    lacking = []
    for item in rules.get("commands", []):
        if not applies(item if isinstance(item, dict) else {}):
            continue
        name = item["name"] if isinstance(item, dict) else item
        if not any(exists(p) for p in _command_paths(name)):
            lacking.append(name)
    report.expect(not lacking, "squashfs.commands", f"{len(rules.get('commands', []))} commands present", "commands missing from the image: " + ", ".join(lacking))

    # Services: every rcadd entry is a runlevel symlink and a real init script.
    services = list(rcadd) + [f"{svc}|{level}" for svc, level in rules.get("implicit_services", [])]
    problems = []
    for item in services:
        service, _, level = item.partition("|")
        link = entries.get(f"etc/runlevels/{level}/{service}")
        script = entries.get(f"etc/init.d/{service}")
        if link is None or link.kind != "slink":
            problems.append(f"{service} is not in runlevel {level}")
        if script is None:
            problems.append(f"etc/init.d/{service} does not exist")
    report.expect(not problems, "squashfs.services", f"{len(services)} services enabled", "; ".join(problems))
    forbidden_services = [
        path for path in entries
        if any(_matches(f"etc/runlevels/*/{svc}", path) for svc in rules.get("forbidden_services", []))
    ]
    report.expect(not forbidden_services, "squashfs.forbidden_services", "", "services that must stay off are enabled: " + ", ".join(sorted(forbidden_services)))

    # Forbidden paths. An item is a glob (any entry type) or {"pattern": glob, "types": [...]}, which
    # ignores leftover directories: Portage keeps a directory that holds a file it does not own.
    forbidden = [(item, None) if isinstance(item, str) else (item["pattern"], set(item["types"])) for item in rules.get("forbidden", [])]
    hits = sorted({
        path for path, entry in entries.items() for pattern, types in forbidden
        if _matches(pattern, path) and (types is None or entry.kind in types)
    })
    report.expect(not hits, "squashfs.forbidden", "", "must not be in the image: " + ", ".join(hits[:12]) + (" ..." if len(hits) > 12 else ""))

    # Ownership: nothing may belong to the builder or to the live user outside its home.
    ids = {LIVE_USER_ID, *builder_ids}
    exceptions = rules.get("owner_exceptions", [])
    owned = sorted(
        path for path, entry in entries.items()
        if (entry.uid in ids or entry.gid in ids) and not any(_matches(p, path) for p in exceptions)
    )
    report.expect(
        not owned,
        "squashfs.ownership",
        "",
        f"{len(owned)} entries belong to uid/gid {sorted(ids)} outside the live user's home (the overlay was copied with the builder's ids): "
        + ", ".join(owned[:8]) + (" ..." if len(owned) > 8 else ""),
    )
    owner = re.search(r"UID:\s*(\d+).*?GID:\s*(\d+)", root_stat, re.S)
    report.expect(bool(owner) and owner.groups() == ("0", "0"), "squashfs.root_owner", "", "the root directory of the image is not owned by root")

    # Content rules for small files.
    for rule in rules.get("content", []):
        if not applies(rule) or rule.get("flavor", flavor) != flavor:
            continue
        path = rule["path"]
        if not exists(path):
            report.failed("squashfs.content", f"{path} is missing")
            continue
        try:
            body = read_file("/" + path).decode(errors="replace")
        except VerifyError as exc:
            report.failed("squashfs.content", f"{path} cannot be read: {exc}")
            continue
        for pattern in rule.get("must_match", []):
            report.expect(bool(re.search(pattern, body, re.M)), "squashfs.content", f"{path} ~ {pattern}", f"{path} does not contain the expected setting {pattern!r}: {rule.get('why', '')}".strip())
        for pattern in rule.get("must_not_match", []):
            report.expect(not re.search(pattern, body, re.M), "squashfs.content", f"{path} !~ {pattern}", f"{path} contains {pattern!r}: {rule.get('why', '')}".strip())

    # Packages.
    names = package_names(installed_packages(entries))
    for key, wanted in (("required_packages", True), ("forbidden_packages", False)):
        listed = rules.get(key, [])
        found = [pkg for pkg in listed if (pkg in names) == wanted]
        bad = [pkg for pkg in listed if (pkg in names) != wanted]
        report.expect(not bad, f"squashfs.{key}", f"{len(found)} packages", ("not installed: " if wanted else "must not be installed: ") + ", ".join(bad))

    # Flavour: a release carries no test access, a test image carries all of it.
    flavor_rules = contract.get("flavor", {})
    artifacts = flavor_rules.get("test_artifacts", [])
    present = sorted({p for pattern in artifacts for p in entries if _matches(pattern, p)})
    if flavor == "release":
        report.expect(not present, "flavor.release", "no test-access artifacts", "a release image contains test-access files: " + ", ".join(present))
        shadow_ok = False
        try:
            for line in read_file("/etc/shadow").decode(errors="replace").splitlines():
                if line.startswith("root:"):
                    shadow_ok = line.split(":")[1][:1] in ("!", "*")
        except VerifyError:
            shadow_ok = False
        report.expect(shadow_ok, "flavor.root_locked", "", "the root account of a release image must be locked in /etc/shadow")
    else:
        absent = [p for p in flavor_rules.get("testkey_required", []) if p not in entries]
        report.expect(not absent, "flavor.testkey", "test access present", "the test image lacks: " + ", ".join(absent))


# ---------------------------------------------------------------------------
# orchestration


def find_default(workspace: Path, relative: str) -> Path:
    return workspace / relative


def verify(
    iso: Path,
    *,
    flavor: str,
    workspace: Path,
    tools: Any,
    spec_path: Path | None = None,
    contract_path: Path | None = None,
    pins_path: Path | None = None,
    bootargs: Sequence[str] | None = None,
    volume_id: str | None = None,
    builder_ids: Iterable[int] = (),
    previous_size: int | None = None,
    iso_size: int = 0,
) -> tuple[Report, list[str]]:
    """Run every check. Returns the report and the sorted installed package list."""
    report = Report()
    contract = json.loads((contract_path or workspace / "iso/image-contract.json").read_text(encoding="utf-8"))
    if contract.get("schema_version") != SCHEMA_VERSION:
        raise VerifyError("unsupported iso/image-contract.json schema_version")
    pins = load_pins(pins_path or workspace / "iso/pins.env")
    spec = parse_spec((spec_path or workspace / "iso/specs/livecd-stage2.spec").read_text(encoding="utf-8"))
    expected_volid = flavor_volume_id(volume_id or pins["ISO_VOLID"], flavor)
    args = list(bootargs) if bootargs is not None else pins.get("ISO_BOOTARGS", "").split()
    rcadd = spec_list(spec, "livecd/rcadd")
    if flavor == "testkey" and not any(item.startswith("sshd|") for item in rcadd):
        rcadd.append("sshd|default")  # added by the builder next to gentoo-installerd

    # A rendered spec must agree with the pins; the unrendered template holds tokens.
    spec_volid = spec.get("livecd/volid")
    if isinstance(spec_volid, str) and not spec_volid.startswith("@"):
        report.expect(spec_volid == expected_volid, "spec.volid", spec_volid, f"livecd/volid is '{spec_volid}' but the boot menu searches for '{expected_volid}'")
    spec_bootargs = spec_list(spec, "livecd/bootargs")
    if spec_bootargs and not spec_bootargs[0].startswith("@"):
        report.expect(spec_bootargs == args, "spec.bootargs", " ".join(spec_bootargs), f"livecd/bootargs is '{' '.join(spec_bootargs)}' but the boot menu uses '{' '.join(args)}'")

    boot = parse_el_torito(tools.el_torito_report())
    files = check_iso(report, tools.iso_files(), boot, contract.get("iso", {}), expected_volid)
    grub_text = tools.iso_read("/boot/grub/grub.cfg").decode(errors="replace")
    check_grub(report, grub_text, expected_volid, args, files, contract)
    has_dokeymap = any("dokeymap" in line.split() for line in grub_text.splitlines() if line.strip().startswith("linux"))
    try:
        check_initramfs(report, initramfs_names(tools.iso_read("/boot/gentoo.igz")), contract, has_dokeymap)
    except VerifyError as exc:
        report.failed("initramfs.read", str(exc))

    entries = parse_description(tools.squashfs_listing())
    check_squashfs(
        report,
        entries,
        contract,
        rcadd,
        flavor,
        tools.squashfs_cat,
        tools.squashfs_root_stat(),
        builder_ids,
        lambda relative: (workspace / relative).exists(),
        lambda relative, text: (workspace / relative).is_file() and text in (workspace / relative).read_text(encoding="utf-8", errors="replace"),
    )
    if previous_size and iso_size:
        report.expect(
            iso_size <= previous_size * 1.2,
            "iso.size",
            f"{iso_size} bytes",
            "the ISO grew by more than 20 percent since the previous build",
            severity="warning",
        )
    return report, installed_packages(entries)


def format_report(report: Report) -> str:
    lines = []
    for check in report.checks:
        mark = "ok  " if check.ok else ("WARN" if check.severity == "warning" else "FAIL")
        lines.append(f"[{mark}] {check.id}" + (f": {check.detail}" if check.detail else ""))
    lines.append("")
    lines.append(f"{len(report.checks) - len(report.errors) - len(report.warnings)} passed, {len(report.warnings)} warnings, {len(report.errors)} failed")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("iso", type=Path)
    parser.add_argument("--flavor", choices=FLAVORS, default="release")
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--spec", type=Path, help="rendered or template livecd-stage2.spec (default: iso/specs/livecd-stage2.spec)")
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--pins", type=Path)
    parser.add_argument("--volid", help="expected volume id of a release (default: ISO_VOLID of the pins)")
    parser.add_argument("--bootargs", help="expected kernel arguments (default: ISO_BOOTARGS of the pins)")
    parser.add_argument("--builder-uid", type=int, action="append", default=[], help="uid/gid of the user who ran the build (also read from HOST_UID/HOST_GID)")
    parser.add_argument("--json", type=Path, help="write the report here")
    parser.add_argument("--packages-out", type=Path, help="write the installed package list here")
    parser.add_argument("--workdir", type=Path, help="scratch directory for image.squashfs (about 3 GiB; default: a temporary one)")
    parser.add_argument("--previous-manifest", type=Path, help="manifest.json of the previous build, for the size warning")
    args = parser.parse_args(argv)

    if not args.iso.is_file():
        print(f"verify-iso: no such file: {args.iso}", file=sys.stderr)
        return 2
    builder_ids = set(args.builder_uid)
    for variable in ("HOST_UID", "HOST_GID"):
        if os.environ.get(variable, "").isdigit():
            builder_ids.add(int(os.environ[variable]))
    previous_size = None
    if args.previous_manifest and args.previous_manifest.is_file():
        previous_size = json.loads(args.previous_manifest.read_text(encoding="utf-8")).get("iso", {}).get("size")

    own_workdir = args.workdir is None
    workdir = Path(tempfile.mkdtemp(prefix="verify-iso-", dir=os.environ.get("TMPDIR"))) if own_workdir else args.workdir
    try:
        tools = Tools(args.iso, workdir)
        report, packages = verify(
            args.iso,
            flavor=args.flavor,
            workspace=args.workspace,
            tools=tools,
            spec_path=args.spec,
            contract_path=args.contract,
            pins_path=args.pins,
            bootargs=args.bootargs.split() if args.bootargs else None,
            volume_id=args.volid,
            builder_ids=builder_ids,
            previous_size=previous_size,
            iso_size=args.iso.stat().st_size,
        )
    except (VerifyError, OSError, KeyError, ValueError) as exc:
        print(f"verify-iso: {exc}", file=sys.stderr)
        return 2
    finally:
        if own_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        elif args.workdir and (args.workdir / "image.squashfs").exists():
            # The caller owns the directory but not the 3 GiB copy.
            (args.workdir / "image.squashfs").unlink()

    print(format_report(report))
    if args.json:
        args.json.write_text(json.dumps(report.to_dict(args.flavor), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.packages_out:
        args.packages_out.write_text("".join(f"{p}\n" for p in packages), encoding="utf-8")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
