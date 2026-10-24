"""Disposable-disk guard for the VM harness.

The harness destroys whatever disk it installs to, so it must be certain that the
disk it is about to touch is the qcow2 it created for this run, and that no other
disk is present by mistake. The guard identifies a VM disk by properties that
cannot be a real machine's disk by accident, none of which depends on a serial
number (a disk may deliberately have none, or a SATA disk gets one from QEMU):

* its exact size in bytes: whole GiB plus a few MiB, different for every disk and run;
* its model, transport, sector size and removable flag where the emulated bus defines them;
* the PCI vendor of the controller it hangs off (virtio, QEMU NVMe/xHCI, ICH9 AHCI);
* its serial number when the harness gave it one;
* a per-run nonce passed to the guest as an SMBIOS OEM string;
* exactly the expected disks, and nothing else, besides the live CD-ROM.

This file is a library for the host (it builds the expectation and judges the
report) and, run as ``python3 -c "$(cat vm_guard.py)" '<spec json>'`` inside the
guest, the program that inspects the guest and prints one JSON report line. It
uses only the standard library and the guest's own ``lsblk``; it never writes.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

REPORT_TYPE = "vm_disk_guard"
NONCE_KEY = "org.gentoo-installer.vm-nonce"
MIB = 1 << 20
GIB = 1 << 30
LSBLK_COLUMNS = "PATH,KNAME,TYPE,SIZE,SERIAL,MODEL,TRAN,RM,RO,LOG-SEC,PHY-SEC,PTTYPE,FSTYPE,MOUNTPOINTS"
# Block devices that are not disks the installer could be pointed at.
IGNORED_PREFIXES = ("zram", "ram", "loop", "nbd")

# What each emulated bus looks like from inside the guest.
BUS_FACTS = {
    "virtio": {"model": "", "tran": "", "pci_vendors": ["0x1af4"]},
    "sata": {"model": "QEMU HARDDISK", "tran": "sata", "pci_vendors": ["0x8086"]},
    "scsi": {"model": "QEMU HARDDISK", "tran": "", "pci_vendors": ["0x1af4", "0x1000", "0x8086"]},
    "nvme": {"model": "QEMU NVMe Ctrl", "tran": "nvme", "pci_vendors": ["0x1b36"]},
    "usb": {"model": "QEMU HARDDISK", "tran": "usb", "pci_vendors": ["0x1b36", "0x8086"]},
}


def unusual_sizes(nominal_mib: list[int], salt: int) -> list[int]:
    """Exact byte sizes for disks of the given nominal sizes, all different and not round.

    Every size is the nominal size plus 1..64 MiB, so a size match is not an accident of
    a real 40 GB or 256 GiB disk; sizes within one run never coincide.
    """
    sizes: list[int] = []
    for index, nominal in enumerate(nominal_mib):
        extra = 1 + (salt * 7 + index * 13) % 61
        size = nominal * MIB + extra * MIB
        while size in sizes:
            size += MIB
        sizes.append(size)
    return sizes


@dataclass
class DiskExpectation:
    role: str
    size: int
    serial: str = ""                 # "" = the disk has none (or its serial is not checked)
    model: str | None = None         # None: not checked; "": the disk must report no model (virtio-blk)
    tran: str = ""
    pci_vendors: list[str] = field(default_factory=list)
    log_sec: int = 512
    removable: bool | None = None
    blank: bool | None = None  # True: no partition table or filesystem; False: must have one; None: unchecked


@dataclass
class GuardSpec:
    disks: list[DiskExpectation]
    nonce: str = ""
    allow_unlisted: bool = False  # tolerate disks the harness did not create (never for installs)
    version: int = 1

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "GuardSpec":
        raw = json.loads(text)
        return cls(disks=[DiskExpectation(**disk) for disk in raw["disks"]], nonce=raw.get("nonce", ""),
                   allow_unlisted=bool(raw.get("allow_unlisted", False)), version=int(raw.get("version", 1)))

    def disk(self, role: str) -> DiskExpectation:
        return next(disk for disk in self.disks if disk.role == role)

    def with_blank(self, role: str, blank: bool | None) -> "GuardSpec":
        disks = [DiskExpectation(**{**asdict(disk), "blank": blank if disk.role == role else disk.blank})
                 for disk in self.disks]
        return GuardSpec(disks, self.nonce, self.allow_unlisted, self.version)


# --- evaluation (pure) ---------------------------------------------------------------------------

def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def flatten(devices: list[dict]) -> list[dict]:
    """lsblk's tree as a flat list of nodes, each with its direct children's count."""
    nodes: list[dict] = []

    def visit(node: dict) -> None:
        nodes.append(node)
        for child in node.get("children") or []:
            visit(child)

    for device in devices:
        visit(device)
    return nodes


def _mounted(node: dict) -> bool:
    points = node.get("mountpoints")
    if points is None and node.get("mountpoint"):
        points = [node["mountpoint"]]
    return any(point for point in points or [])


def _describe(node: dict, extra: dict) -> dict:
    children = node.get("children") or []
    return {
        "path": node.get("path") or "/dev/" + str(node.get("name", "")),
        "kname": node.get("kname") or "",
        "size": _int(node.get("size")),
        "serial": (node.get("serial") or "").strip(),
        "model": (node.get("model") or "").strip(),
        "tran": (node.get("tran") or "").strip(),
        "removable": bool(_int(node.get("rm")) or node.get("rm") is True),
        "read_only": bool(_int(node.get("ro")) or node.get("ro") is True),
        "log_sec": _int(node.get("log-sec")),
        "phy_sec": _int(node.get("phy-sec")),
        "pttype": (node.get("pttype") or "").strip(),
        "fstype": (node.get("fstype") or "").strip(),
        "partitions": len(children),
        "mounted": any(_mounted(n) for n in flatten([node])),
        **extra,
    }


def parse_dmi_strings(raw: bytes) -> list[str]:
    """The strings of one raw SMBIOS record (type 11, OEM strings, is the one the harness sets)."""
    if len(raw) < 5:
        return []
    area = raw[raw[1]:]
    return [part.decode(errors="replace") for part in area.split(b"\0") if part]


def parse_dmi_table(table: bytes) -> list[str]:
    """OEM strings (SMBIOS type 11) from the raw table /sys/firmware/dmi/tables/DMI."""
    strings: list[str] = []
    offset = 0
    while offset + 4 <= len(table):
        kind, length = table[offset], table[offset + 1]
        if length < 4 or kind == 127:  # a malformed record, or the end-of-table marker
            break
        end = table.find(b"\0\0", offset + length)
        area = table[offset + length:end if end >= 0 else len(table)]
        if kind == 11:
            strings += [part.decode(errors="replace") for part in area.split(b"\0") if part]
        if end < 0:
            break
        offset = end + 2
    return strings


def evaluate(spec: GuardSpec, devices: list[dict], *, by_path: dict[str, list[str]] | None = None,
             pci_vendor: dict[str, str] | None = None, dmi_strings: list[str] | None = None,
             signatures: dict[str, bool] | None = None) -> dict:
    """Judge the guest's disks against the expectation; returns the JSON report.

    ``by_path`` maps a kernel name to its /dev/disk/by-path link names, ``pci_vendor`` a kernel
    name to the vendor id of its controller, ``dmi_strings`` the OEM strings (None when they
    could not be read), ``signatures`` whether wipefs found anything on a kernel name (None when
    unreadable). Every problem is a sentence; the report is ok only when there is none.
    """
    by_path = by_path or {}
    pci_vendor = pci_vendor or {}
    signatures = signatures or {}
    problems: list[str] = []
    disks = [node for node in flatten(devices)
             if node.get("type") == "disk" and not str(node.get("kname") or node.get("name") or "").startswith(IGNORED_PREFIXES)]
    report: dict = {"type": REPORT_TYPE, "ok": False, "problems": problems, "disks": {}, "unlisted": []}
    claimed: set[str] = set()
    for expected in spec.disks:
        matching = [node for node in disks if _int(node.get("size")) == expected.size]
        if not matching:
            sizes = ", ".join(str(_int(node.get("size"))) for node in disks) or "none"
            problems.append(f"No disk with the exact size {expected.size} for {expected.role} (disks present: {sizes})")
            continue
        if len(matching) > 1:
            problems.append(f"{len(matching)} disks have the exact size {expected.size}; cannot tell which is {expected.role}")
            continue
        node = matching[0]
        kname = str(node.get("kname") or node.get("name") or "")
        claimed.add(kname)
        facts = _describe(node, {"by_path": by_path.get(kname, []), "pci_vendor": pci_vendor.get(kname, ""),
                                 "has_signatures": signatures.get(kname)})
        report["disks"][expected.role] = facts
        if expected.serial and facts["serial"] != expected.serial:
            problems.append(f"{expected.role} {facts['path']} has serial {facts['serial']!r}, expected {expected.serial!r}")
        if expected.model is not None and facts["model"] != expected.model:
            problems.append(f"{expected.role} {facts['path']} has model {facts['model']!r}, expected {expected.model!r}")
        if expected.tran and facts["tran"] and facts["tran"] != expected.tran:
            problems.append(f"{expected.role} {facts['path']} uses transport {facts['tran']!r}, expected {expected.tran!r}")
        if expected.log_sec and facts["log_sec"] and facts["log_sec"] != expected.log_sec:
            problems.append(f"{expected.role} {facts['path']} has {facts['log_sec']}-byte sectors, expected {expected.log_sec}")
        if expected.removable is not None and facts["removable"] != expected.removable:
            problems.append(f"{expected.role} {facts['path']} removable flag is {facts['removable']}, expected {expected.removable}")
        vendor = facts["pci_vendor"]
        if expected.pci_vendors and vendor and vendor.lower() not in [v.lower() for v in expected.pci_vendors]:
            problems.append(f"{expected.role} {facts['path']} hangs off PCI vendor {vendor}, expected one of {expected.pci_vendors}")
        if facts["read_only"]:
            problems.append(f"{expected.role} {facts['path']} is read-only")
        if facts["mounted"]:
            problems.append(f"{expected.role} {facts['path']} is mounted")
        if expected.blank is not None:
            used = bool(facts["pttype"] or facts["fstype"] or facts["partitions"] or facts["has_signatures"])
            if expected.blank and used:
                problems.append(f"{expected.role} {facts['path']} is not blank (partition table, filesystem or signatures present)")
            if expected.blank is False and not used:
                problems.append(f"{expected.role} {facts['path']} is blank but should hold an installation")
    unlisted = [_describe(node, {}) for node in disks if str(node.get("kname") or node.get("name") or "") not in claimed]
    report["unlisted"] = [{"path": item["path"], "size": item["size"], "model": item["model"], "serial": item["serial"]}
                          for item in unlisted]
    if unlisted and not spec.allow_unlisted:
        names = ", ".join(f"{item['path']} ({item['size']} bytes)" for item in unlisted)
        problems.append(f"Unexpected disk(s) present besides the harness disks: {names}")
    report["nonce_ok"] = None
    if spec.nonce:
        if dmi_strings is None:
            report["nonce_ok"] = None
        else:
            report["nonce_ok"] = f"{NONCE_KEY}={spec.nonce}" in dmi_strings
            if not report["nonce_ok"]:
                problems.append("The guest does not carry this run's SMBIOS nonce: it is not the VM the harness started")
    target = report["disks"].get("target")
    report["target"] = target
    report["ok"] = not problems
    return report


# --- the guest side ---------------------------------------------------------------------------------

def _read(path: str) -> str:
    with open(path, "r", errors="replace") as stream:
        return stream.read().strip()


def _pci_vendor(sysfs: str, kname: str) -> str:
    """Vendor id of the nearest PCI ancestor of a block device ('' when none is found)."""
    try:
        directory = os.path.realpath(os.path.join(sysfs, "class", "block", kname, "device"))
    except OSError:
        return ""
    while directory and directory not in ("/", os.path.realpath(sysfs)):
        candidate = os.path.join(directory, "vendor")
        if os.path.isfile(candidate):
            try:
                value = _read(candidate)
            except OSError:
                return ""
            # A SCSI device on the way up has a 'vendor' too: the INQUIRY string ("ATA     ", "QEMU    "), not an id.
            if re.fullmatch(r"0x[0-9a-fA-F]{4}", value):
                return value
        directory = os.path.dirname(directory)
    return ""


def collect(spec: GuardSpec, *, sysfs: str = "/sys", dev: str = "/dev", run=subprocess.run) -> dict:
    """Inspect this machine and return the report for ``spec``."""
    result = run(["lsblk", "--json", "--bytes", "--output", LSBLK_COLUMNS], stdout=subprocess.PIPE,
                 stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode != 0:
        return {"type": REPORT_TYPE, "ok": False, "problems": [f"lsblk failed: {result.stderr.strip()[:300]}"],
                "disks": {}, "unlisted": [], "target": None, "nonce_ok": None}
    devices = json.loads(result.stdout).get("blockdevices") or []
    knames = [str(node.get("kname") or node.get("name") or "") for node in flatten(devices) if node.get("type") == "disk"]
    by_path: dict[str, list[str]] = {}
    for link in sorted(glob.glob(os.path.join(dev, "disk", "by-path", "*"))):
        base = os.path.basename(os.path.realpath(link))
        if "-part" not in os.path.basename(link):
            by_path.setdefault(base, []).append(os.path.basename(link))
    vendors = {kname: _pci_vendor(sysfs, kname) for kname in knames}
    dmi: list[str] | None = None
    for entry in sorted(glob.glob(os.path.join(sysfs, "firmware", "dmi", "entries", "11-*", "raw"))):
        try:
            with open(entry, "rb") as stream:
                dmi = (dmi or []) + parse_dmi_strings(stream.read())
        except OSError:
            dmi = None
            break
    if dmi is None:  # dmi-sysfs may be off in the kernel: the raw table is always there (root only)
        try:
            with open(os.path.join(sysfs, "firmware", "dmi", "tables", "DMI"), "rb") as stream:
                dmi = parse_dmi_table(stream.read())
        except OSError:
            dmi = None
    signatures: dict[str, bool] = {}
    for expected in spec.disks:
        if expected.blank is None:
            continue
        for node in flatten(devices):
            if node.get("type") == "disk" and _int(node.get("size")) == expected.size:
                kname = str(node.get("kname") or node.get("name") or "")
                probe = run(["wipefs", "--no-act", "--parsable", os.path.join(dev, kname)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
                if probe.returncode == 0:
                    signatures[kname] = any(line and not line.startswith("#") for line in probe.stdout.splitlines())
    return evaluate(spec, devices, by_path=by_path, pci_vendor=vendors, dmi_strings=dmi, signatures=signatures)


def main(argv: list[str]) -> int:
    spec = GuardSpec.from_json(argv[0])
    report = collect(spec)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


# --- the host side --------------------------------------------------------------------------------

def guest_command(spec: GuardSpec) -> list[str]:
    """argv that runs this file in the guest: python3 -c <source> <spec>."""
    with open(os.path.abspath(__file__), "r", encoding="utf-8") as stream:
        return ["python3", "-c", stream.read(), spec.to_json()]


def parse_report(returncode: int, output: str) -> dict:
    """The guard's report from ssh output; raises ValueError when there is none."""
    for line in reversed(output.strip().splitlines()):
        try:
            report = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(report, dict) and report.get("type") == REPORT_TYPE:
            report["returncode"] = returncode
            return report
    raise ValueError("the guest returned no disk-guard report" + (f" (exit {returncode})" if returncode else ""))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
