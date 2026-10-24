"""Hardware discovery shared by the GUI, the installer daemon and rescue (no Qt).

``probe(root, runner)`` reads /sys, /proc and /run (below ``root``) and runs a
few read-only tools, and returns a ``HardwareProfile``. It works unprivileged,
never raises, and tolerates missing files and odd values: a collector that
fails records ``errors`` and leaves its section at a safe default.

The profile only *describes* the machine. Decisions (drivers, firmware,
power management, ...) are made by ``gentoo_installerd.hwplan`` from the
profile's ``to_dict()``; the profile carries warnings with stable ``hw.`` codes.

Testing: ``root`` may point at a captured fixture tree (see
``scripts/capture-hardware-fixture.py``), ``GENTOO_HW_ROOT`` does the same from
the environment. Against a fixture no program is ever run; command output comes
from ``<root>/.commands/*``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import types
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from . import blockdev
from . import hardware_data as data

SCHEMA = 1
ENV_ROOT = "GENTOO_HW_ROOT"
PCI_IDS_PATHS = ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids", "/usr/share/pci.ids")
# Fixture trees keep otherwise empty directories alive in git with this file; the probe never sees it.
KEEP_MARKER = ".keep"
EFI_GLOBAL_GUID = "8be4df61-93ca-11d2-aa0d-00e098032b8c"
MOK_GUID = "605dab50-e046-4300-abb6-3dd810dd8b23"
LIVE_MOUNT = "/run/initramfs/live"
# Where the live system starts its clock: images are built in 2026, so anything
# earlier is a dead CMOS battery, which breaks TLS certificate checks.
CLOCK_FLOOR_YEAR = 2025


# ---------------------------------------------------------------------------
# Profile data model
# ---------------------------------------------------------------------------

@dataclass
class HardwareWarning:
    code: str = ""  # always in the hw. namespace
    severity: str = "info"  # info | warning | error
    message: str = ""
    action: str = ""  # GUI affordance: reboot_firmware | sync_time | set_time | safe_graphics | ... | ""


@dataclass
class CpuInfo:
    vendor: str = ""  # intel | amd | hygon | other | "" (unknown)
    vendor_id: str = ""
    model_name: str = ""
    family: int = 0
    model: int = 0
    stepping: int = 0
    threads: int = 0
    cores: int = 0
    flags: list[str] = field(default_factory=list)
    x86_64_level: int = 0  # 1-4, 0 = no long mode / unknown
    hypervisor_flag: bool = False
    # CPU_FLAGS_X86 values: from cpuid2cpuflags when it ran, else derived from /proc/cpuinfo.
    cpu_flags_x86: list[str] = field(default_factory=list)
    cpu_flags_source: str = ""  # cpuid2cpuflags | cpuinfo | ""


@dataclass
class MemoryInfo:
    total_kib: int = 0
    available_kib: int = 0
    swap_total_kib: int = 0


@dataclass
class MachineInfo:
    sys_vendor: str = ""
    product_name: str = ""
    product_version: str = ""
    bios_vendor: str = ""
    bios_version: str = ""
    bios_date: str = ""
    board_vendor: str = ""
    board_name: str = ""
    chassis_type: int | None = None
    form_factor: str = ""  # desktop | laptop | tablet | server | vm | other
    hypervisor: str = ""  # "" | qemu | kvm | vmware | virtualbox | hyperv | xen | parallels | bhyve | other


@dataclass
class HypervisorInfo:
    detected: bool = False
    name: str = ""
    vendor: str = ""  # lscpu "Hypervisor vendor", informational
    evidence: list[str] = field(default_factory=list)


@dataclass
class FirmwareInfo:
    mode: str = "bios"  # uefi | bios
    efi_bits: int = 0  # 64 | 32 | 0 (BIOS)
    secure_boot: str = "n/a"  # enabled | disabled | setup | unsupported | unknown | n/a
    lockdown: str = ""  # "" | none | integrity | confidentiality
    booted_via_shim: bool = False
    can_reboot_to_firmware: bool = False
    efivars_writable: bool | None = None
    windows_boot_entry: bool | None = None
    tpm: str = ""  # "" (none) | "2.0" | "1.2" | "present" (version unknown)


@dataclass
class GpuInfo:
    slot: str = ""
    vendor_id: str = ""
    device_id: str = ""
    vendor: str = "other"  # nvidia | amd | intel | virtio | qxl | vmware | virtualbox | hyperv | bochs | cirrus | aspeed | matrox | other
    vendor_name: str = ""
    name: str = ""
    driver: str = ""  # bound kernel driver, "" when none
    boot_vga: bool = False
    generation: str = ""  # see hardware_data
    generation_guess: bool = False
    integrated: bool = False
    virtual: bool = False
    connected_outputs: int | None = None  # None: not read (device runtime-suspended or no DRM node)
    card: str = ""  # drm card name, e.g. card1


@dataclass
class DisplayInfo:
    connector: str = ""
    card: str = ""
    slot: str = ""
    internal: bool = False
    name: str = ""
    width: int = 0
    height: int = 0
    mm_width: int = 0
    mm_height: int = 0
    dpi: int = 0  # 0: physical size unknown


@dataclass
class StorageController:
    slot: str = ""
    vendor_id: str = ""
    device_id: str = ""
    class_code: str = ""
    kind: str = "other"  # ahci | raid | nvme | scsi | ide | ata | sas | sd-host | virtio | other
    driver: str = ""


@dataclass
class PartitionInfo:
    name: str = ""
    size: int = 0
    fstype: str = ""
    label: str = ""
    parttype: str = ""
    mounted: bool = False


@dataclass
class DiskInfo:
    name: str = ""
    path: str = ""
    model: str = ""
    vendor: str = ""
    size: int = 0
    tran: str = ""  # lsblk TRAN when known
    bus: str = "other"  # nvme | sata | usb | sd | virtio | scsi | sas | other
    rota: bool | None = None  # True: spinning disk, False: flash, None: unknown
    log_sec: int = 0
    phy_sec: int = 0
    hotplug: bool = False
    removable: bool = False
    ro: bool = False
    serial: str = ""
    wwn: str = ""
    ptuuid: str = ""
    pttype: str = ""
    by_path: str = ""
    maj_min: str = ""
    signatures: list[str] = field(default_factory=list)
    partitions: list[PartitionInfo] = field(default_factory=list)
    mounted: bool = False
    live_medium: bool = False
    installable: bool = True
    blocker: str = ""  # "" | read-only | live-medium | empty

    @property
    def external(self) -> bool:
        """A USB disk, SD card or other removable medium (internal eMMC is not)."""
        return self.hotplug or self.removable or self.bus == "usb"


@dataclass
class NicInfo:
    iface: str = ""  # "" for a PCI adapter without a network device (no driver)
    kind: str = "ethernet"  # ethernet | wifi | wwan | tether | other
    driver: str = ""
    bus: str = ""  # pci | usb | virtio | sdio | other
    vendor_id: str = ""
    device_id: str = ""
    slot: str = ""
    carrier: bool | None = None
    primary: bool = False
    no_driver: bool = False


@dataclass
class PowerInfo:
    has_battery: bool = False
    battery_count: int = 0
    battery_percent: int | None = None
    battery_status: str = ""  # Charging | Discharging | Full | Not charging | ""
    ac_online: bool | None = None
    has_lid: bool = False
    has_backlight: bool = False


@dataclass
class InputInfo:
    keyboard: bool = False
    touchpad: bool = False
    touchscreen: bool = False
    fingerprint_usb: list[str] = field(default_factory=list)  # "vid:pid" candidates


@dataclass
class AudioCard:
    slot: str = ""
    vendor_id: str = ""
    device_id: str = ""
    driver: str = ""


@dataclass
class AudioInfo:
    cards: list[AudioCard] = field(default_factory=list)
    needs_sof: bool = False


@dataclass
class BluetoothInfo:
    present: bool = False


@dataclass
class PeripheralInfo:
    usb_printers: int = 0
    thunderbolt: bool = False
    webcam: bool = False


@dataclass
class ClockInfo:
    ntp_synced: bool | None = None
    offset_s: float | None = None  # |system clock - NTP time| when chrony is tracking
    rtc_mode: str = "unknown"  # utc | local | unknown
    rtc_delta_s: int | None = None  # RTC minus system clock, seconds
    year: int = 0


@dataclass
class HardwareProfile:
    schema: int = SCHEMA
    cpu: CpuInfo = field(default_factory=CpuInfo)
    memory: MemoryInfo = field(default_factory=MemoryInfo)
    machine: MachineInfo = field(default_factory=MachineInfo)
    firmware: FirmwareInfo = field(default_factory=FirmwareInfo)
    gpus: list[GpuInfo] = field(default_factory=list)
    displays: list[DisplayInfo] = field(default_factory=list)
    storage_controllers: list[StorageController] = field(default_factory=list)
    disks: list[DiskInfo] = field(default_factory=list)
    nics: list[NicInfo] = field(default_factory=list)
    power: PowerInfo = field(default_factory=PowerInfo)
    input: InputInfo = field(default_factory=InputInfo)
    audio: AudioInfo = field(default_factory=AudioInfo)
    bluetooth: BluetoothInfo = field(default_factory=BluetoothInfo)
    peripherals: PeripheralInfo = field(default_factory=PeripheralInfo)
    clock: ClockInfo = field(default_factory=ClockInfo)
    hypervisor: HypervisorInfo = field(default_factory=HypervisorInfo)
    warnings: list[HardwareWarning] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self, redact: bool = False) -> dict[str, Any]:
        """JSON-safe dictionary. ``redact`` drops disk serials, WWNs and partition-table ids."""
        result = _plain(self)
        if redact:
            for disk in result["disks"]:
                for key in ("serial", "wwn", "ptuuid"):
                    disk[key] = ""
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> HardwareProfile:
        """Tolerant inverse of to_dict(): unknown keys are ignored, bad values keep the default."""
        return _from_dict(cls, raw or {})

    def errors_for(self, collector: str) -> list[str]:
        """Collector failures recorded in ``errors`` ('<collector>: <message>')."""
        return [error for error in self.errors if error.startswith(collector + ":")]

    def summary(self) -> str:
        """One line for the Readiness page, e.g. 'Intel Core i9 - 32 threads - 46 GiB - NVIDIA GeForce RTX 4090 - UEFI 64-bit'."""
        parts: list[str] = []
        if self.cpu.model_name:
            parts.append(" ".join(self.cpu.model_name.replace("(R)", "").replace("(TM)", "").split()))
        if self.cpu.threads:
            parts.append(f"{self.cpu.threads} threads")
        if self.memory.total_kib:
            parts.append(f"{self.memory.total_kib / 1048576:.0f} GiB")
        gpus = [gpu_label(gpu) for gpu in self.gpus if gpu.name or gpu.vendor_name]
        if gpus:
            parts.append(" + ".join(gpus))
        kinds = _disk_kinds(self.disks)
        if kinds:
            parts.append(kinds)
        parts.append("Legacy BIOS" if self.firmware.mode == "bios" else f"UEFI {self.firmware.efi_bits or 64}-bit")
        if self.machine.hypervisor:
            parts.append(f"virtual machine ({self.machine.hypervisor})")
        return " - ".join(parts)


def gpu_label(gpu: GpuInfo) -> str:
    """Human name of a GPU: the marketing name from pci.ids with its vendor, e.g. 'NVIDIA GeForce RTX 4090'."""
    bracket = re.search(r"\[(.+?)\]", gpu.name)
    name = bracket.group(1) if bracket else gpu.name
    prefix = {"nvidia": "NVIDIA ", "amd": "AMD ", "intel": "Intel "}.get(gpu.vendor, "")
    return name if not prefix or name.lower().startswith(prefix.strip().lower()) else prefix + name


def _disk_kinds(disks: Sequence[DiskInfo]) -> str:
    labels: list[str] = []
    for disk in disks:
        if not disk.installable:
            continue
        label = {"nvme": "NVMe SSD", "sata": "SATA SSD" if disk.rota is False else "SATA HDD", "usb": "USB disk",
                 "sd": "SD/eMMC", "virtio": "virtual disk"}.get(disk.bus, "SSD" if disk.rota is False else "disk")
        if label not in labels:
            labels.append(label)
    return " + ".join(labels)


# -- dict conversion --------------------------------------------------------

def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def _coerce(tp: Any, value: Any, default: Any) -> Any:
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        options = [arg for arg in typing.get_args(tp) if arg is not type(None)]
        if value is None:
            return None if type(None) in typing.get_args(tp) else default
        for option in options:
            result = _coerce(option, value, _MISSING)
            if result is not _MISSING:
                return result
        return default
    if origin is list:
        if not isinstance(value, (list, tuple)):
            return default
        item_type = typing.get_args(tp)[0] if typing.get_args(tp) else Any
        items = [_coerce(item_type, item, _MISSING) for item in value]
        return [item for item in items if item is not _MISSING]
    if isinstance(tp, type) and is_dataclass(tp):
        return _from_dict(tp, value) if isinstance(value, Mapping) else default
    if tp is bool:
        return value if isinstance(value, bool) else default
    if tp is int:
        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value == int(value):
            return int(value)
        return default
    if tp is float:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    if tp is str:
        return value if isinstance(value, str) else default
    return value if tp is Any else default


_MISSING = object()


def _from_dict(cls: type, raw: Mapping[str, Any]) -> Any:
    hints = typing.get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name in raw:
            value = _coerce(hints[f.name], raw[f.name], _MISSING)
            if value is not _MISSING:
                kwargs[f.name] = value
    return cls(**kwargs)


# ---------------------------------------------------------------------------
# Reading the machine: a rooted file view and a command runner
# ---------------------------------------------------------------------------

Recorder = Callable[[str, str], None]


class Fs:
    """Read-only view of /sys, /proc, /run ... below ``root``. Every method is total.

    Paths are absolute POSIX strings. Symlinks are never resolved with
    ``Path.resolve()``: the probe reads the link *text* (``readlink``) and takes
    names out of it. A captured fixture materialises class/bus symlinks as
    directories and keeps their original text in ``<root>/.links.json``, which
    ``readlink`` consults when the entry is not a real symlink.
    """

    def __init__(self, root: Path | str = "/", recorder: Recorder | None = None) -> None:
        self.root = Path(root)
        self.recorder = recorder
        self._links: dict[str, str] | None = None

    def _real(self, path: str) -> str:
        return os.path.join(str(self.root), path.lstrip("/"))

    def _note(self, kind: str, path: str) -> None:
        if self.recorder is not None:
            self.recorder(kind, path)

    def read_bytes(self, path: str, limit: int = 1 << 20) -> bytes | None:
        try:
            with open(self._real(path), "rb") as handle:
                content = handle.read(limit)
        except (OSError, ValueError):
            return None
        self._note("read", path)
        return content

    def read(self, path: str, limit: int = 1 << 20) -> str | None:
        content = self.read_bytes(path, limit)
        return None if content is None else content.decode("utf-8", "replace").replace("\x00", "")

    def line(self, path: str) -> str:
        """First line of a one-value file, stripped; '' when unreadable."""
        text = self.read(path, 4096)
        return text.strip().splitlines()[0].strip() if text and text.strip() else ""

    def int_of(self, path: str, base: int = 10) -> int | None:
        value = self.line(path)
        try:
            return int(value, base)
        except ValueError:
            return None

    def listdir(self, path: str) -> list[str]:
        try:
            names = sorted(name for name in os.listdir(self._real(path)) if name != KEEP_MARKER)
        except (OSError, ValueError):
            return []
        self._note("list", path)
        return names

    def exists(self, path: str) -> bool:
        self._note("stat", path)
        return os.path.lexists(self._real(path))

    def isdir(self, path: str) -> bool:
        self._note("stat", path)
        return os.path.isdir(self._real(path))

    def readlink(self, path: str) -> str | None:
        """Link text of ``path``; falls back to the fixture sidecar for materialised links."""
        try:
            text = os.readlink(self._real(path))
        except (OSError, ValueError):
            recorded = self._sidecar().get(path.rstrip("/"))
            if recorded is None:
                return None
            text = recorded
        self._note("link", path)
        return text

    def link_name(self, path: str) -> str:
        """Last component of a link target, e.g. the driver name behind ``<dev>/driver``."""
        text = self.readlink(path)
        return os.path.basename(text.rstrip("/")) if text else ""

    def _sidecar(self) -> dict[str, str]:
        if self._links is None:
            self._links = {}
            try:
                with open(os.path.join(str(self.root), ".links.json"), encoding="utf-8") as handle:
                    raw = json.load(handle)
                if isinstance(raw, dict):
                    self._links = {str(k): str(v) for k, v in raw.items()}
            except (OSError, ValueError):
                pass
        return self._links


class CommandRunner(Protocol):
    """Structural match for gentoo_installerd.runner.Runner (the GUI cannot import the daemon)."""

    def run(self, argv: Sequence[str], *, stdin: str | None = None, secret: bool = False,
            env: Mapping[str, str] | None = None, cwd: Path | None = None) -> Any: ...


@dataclass(frozen=True)
class _Result:
    argv: tuple[str, ...]
    returncode: int
    stdout: str


# Programs the probe may run. All are read-only; anything else is refused.
PROBE_COMMANDS = ("lsblk", "lscpu", "cpuid2cpuflags", "efibootmgr", "chronyc")


def command_key(argv: Sequence[str]) -> str:
    """Fixture file name (below .commands/) for a probe command."""
    name = os.path.basename(argv[0]) if argv else ""
    if name == "lsblk":
        return "lsblk.json"
    if name == "chronyc":
        return "chronyc-tracking.txt"
    return f"{name}.txt"


class LocalRunner:
    """Bounded runner for the live system: only PROBE_COMMANDS, 5 s timeout, C locale, no stdin."""

    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout

    def run(self, argv: Sequence[str], *, stdin: str | None = None, secret: bool = False,
            env: Mapping[str, str] | None = None, cwd: Path | None = None) -> _Result:
        name = os.path.basename(argv[0]) if argv else ""
        if name not in PROBE_COMMANDS:
            raise ValueError(f"The hardware probe may not run {name!r}")
        exe = shutil.which(argv[0])
        if exe is None:
            raise FileNotFoundError(argv[0])
        process = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, errors="replace", timeout=self.timeout,
                                 stdin=subprocess.DEVNULL, env={**os.environ, "LC_ALL": "C"}, check=False)
        return _Result(tuple(argv), process.returncode, process.stdout)


class FixtureRunner:
    """Serves recorded command output from ``<root>/.commands`` and never runs anything."""

    def __init__(self, root: Path | str) -> None:
        self.directory = Path(root) / ".commands"

    def run(self, argv: Sequence[str], *, stdin: str | None = None, secret: bool = False,
            env: Mapping[str, str] | None = None, cwd: Path | None = None) -> _Result:
        path = self.directory / command_key(argv)
        return _Result(tuple(argv), 0, path.read_text(encoding="utf-8", errors="replace"))


def default_root(root: Path | str | None = None) -> Path:
    """``root``, else $GENTOO_HW_ROOT, else the real filesystem."""
    if root is not None and str(root) != "/":
        return Path(root)
    return Path(os.environ.get(ENV_ROOT) or "/")


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------

def parse_cpuinfo(text: str) -> CpuInfo:
    """Parse /proc/cpuinfo: the first logical CPU describes the model, ``processor`` lines count threads."""
    cpu = CpuInfo()
    seen_first = False
    for raw in text.splitlines():
        if ":" not in raw:
            if not raw.strip() and cpu.threads:
                seen_first = True
            continue
        key, _, value = raw.partition(":")
        key, value = key.strip(), value.strip()
        if key == "processor":
            cpu.threads += 1
            continue
        if seen_first:
            continue
        if key == "vendor_id":
            cpu.vendor_id = value
        elif key == "model name":
            cpu.model_name = " ".join(value.split())
        elif key == "cpu family":
            cpu.family = _to_int(value)
        elif key == "model":
            cpu.model = _to_int(value)
        elif key == "stepping":
            cpu.stepping = _to_int(value)
        elif key == "cpu cores":
            cpu.cores = _to_int(value)
        elif key == "flags":
            cpu.flags = value.split()
    cpu.vendor = data.cpu_vendor(cpu.vendor_id)
    cpu.hypervisor_flag = "hypervisor" in cpu.flags
    cpu.x86_64_level = data.x86_64_level(cpu.flags)
    return cpu


def _to_int(value: str, base: int = 10) -> int:
    try:
        return int(value.strip(), base)
    except (ValueError, AttributeError):
        return 0


def parse_meminfo(text: str) -> MemoryInfo:
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key.strip()] = int(parts[0])
    return MemoryInfo(values.get("MemTotal", 0), values.get("MemAvailable", values.get("MemFree", 0)), values.get("SwapTotal", 0))


_CPU_FLAGS_LINE = re.compile(r"^\s*(?:\*/\*\s+)?CPU_FLAGS_X86\s*[:=]\s*(?P<flags>.+?)\s*$", re.M)


def parse_cpuid2cpuflags(text: str) -> list[str]:
    """Flag list from cpuid2cpuflags output.

    cpuid2cpuflags 18 prints ``*/* CPU_FLAGS_X86: aes avx ...`` (the package.use
    form), older and wrapped output uses ``CPU_FLAGS_X86: ...`` or an assignment.
    Only valid CPU_FLAGS_X86 values are kept, in the printed order.
    """
    match = _CPU_FLAGS_LINE.search(text or "")
    if not match:
        return []
    tokens = match.group("flags").strip("\"'").replace('"', " ").replace("'", " ").split()
    return list(dict.fromkeys(token for token in tokens if token in data.CPU_FLAGS_X86_VALUES))


def parse_secure_boot(secure_boot: bytes | None, setup_mode: bytes | None) -> str:
    """enabled | setup | disabled | unsupported from the raw efivar contents (4 attribute bytes + data)."""
    if secure_boot is None:
        return "unsupported"
    if len(secure_boot) < 5:
        return "unknown"
    enforcing = secure_boot[4] == 1
    in_setup = setup_mode is not None and len(setup_mode) >= 5 and setup_mode[4] == 1
    if enforcing and not in_setup:
        return "enabled"
    if in_setup:
        return "setup"
    return "disabled"


def parse_lockdown(text: str | None) -> str:
    match = re.search(r"\[(\w+)\]", text or "")
    return match.group(1) if match else ""


def parse_mountinfo(text: str) -> list[tuple[str, str, str, str]]:
    """(mount point, fstype, source, options) for each line of /proc/self/mountinfo or /proc/mounts."""
    entries: list[tuple[str, str, str, str]] = []
    for line in text.splitlines():
        if " - " in line:  # mountinfo: ... mountpoint options ... - fstype source superoptions
            head, _, tail = line.partition(" - ")
            head_parts, tail_parts = head.split(), tail.split()
            if len(head_parts) >= 6 and len(tail_parts) >= 2:
                entries.append((_unescape_mount(head_parts[4]), tail_parts[0], _unescape_mount(tail_parts[1]), head_parts[5]))
        else:  # /proc/mounts: source mountpoint fstype options ...
            parts = line.split()
            if len(parts) >= 4:
                entries.append((_unescape_mount(parts[1]), parts[2], _unescape_mount(parts[0]), parts[3]))
    return entries


def _unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)


def parse_pci_ids(text: str, wanted: Iterable[tuple[str, str]]) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """Look up names in pci.ids without parsing the whole database.

    Returns (vendor id -> vendor name, (vendor id, device id) -> device name)
    for the wanted pairs; ids are lower-case hex without 0x.
    """
    by_vendor: dict[str, set[str]] = {}
    for vendor, device in wanted:
        by_vendor.setdefault(vendor.lower(), set()).add(device.lower())
    vendors: dict[str, str] = {}
    devices: dict[tuple[str, str], str] = {}
    if not by_vendor:
        return vendors, devices
    current = ""
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        if line[0] != "\t":
            if line.startswith("C "):
                break  # the class section follows the last vendor
            current = line[:4].lower()
            if current in by_vendor:
                vendors[current] = line[6:].strip()
            else:
                current = ""
        elif current and line[1] != "\t":
            device = line[1:5].lower()
            if device in by_vendor[current]:
                devices[(current, device)] = line[7:].strip()
    return vendors, devices


def parse_edid(blob: bytes | None) -> dict[str, Any]:
    """Preferred mode, physical size and monitor name from an EDID base block ({} when invalid)."""
    if not blob or len(blob) < 128 or blob[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return {}
    result: dict[str, Any] = {"width": 0, "height": 0, "mm_width": 0, "mm_height": 0, "name": ""}
    cm_w, cm_h = blob[21], blob[22]
    if cm_w and cm_h:
        result["mm_width"], result["mm_height"] = cm_w * 10, cm_h * 10
    for offset in (54, 72, 90, 108):
        block = blob[offset:offset + 18]
        if len(block) < 18:
            continue
        if struct.unpack_from("<H", block, 0)[0] != 0:  # a detailed timing (pixel clock != 0)
            if not result["width"]:
                result["width"] = block[2] | ((block[4] & 0xF0) << 4)
                result["height"] = block[5] | ((block[7] & 0xF0) << 4)
                mm_w = block[12] | ((block[14] & 0xF0) << 4)
                mm_h = block[13] | ((block[14] & 0x0F) << 8)
                if mm_w and mm_h:
                    result["mm_width"], result["mm_height"] = mm_w, mm_h
        elif block[3] == 0xFC:  # monitor name descriptor
            result["name"] = block[5:18].split(b"\n")[0].decode("ascii", "replace").strip()
    return result


def dpi_from(width_px: int, width_mm: int, height_px: int = 0, height_mm: int = 0) -> int:
    """Pixels per inch from the physical size, 0 when the size is missing or implausible."""
    if width_px <= 0 or width_mm < 50:
        return 0
    dpi = 25.4 * width_px / width_mm
    if height_px > 0 and height_mm >= 30:
        vertical = 25.4 * height_px / height_mm
        if abs(vertical - dpi) > 0.25 * dpi:  # panel pixels are square; a mismatch means a bogus EDID
            return 0
    return round(dpi) if 40 <= dpi <= 600 else 0


def parse_chrony_tracking(text: str | None) -> tuple[bool | None, float | None]:
    """(synced, |offset| seconds) from ``chronyc -c tracking``; (None, None) when unusable."""
    if not text or not text.strip():
        return None, None
    fields_ = [item.strip() for item in text.strip().splitlines()[0].split(",")]
    if len(fields_) < 14:
        return None, None
    stratum = _to_int(fields_[2])
    synced = 0 < stratum < 16 and fields_[13].lower() != "not synchronised"
    try:
        offset = abs(float(fields_[4]))
    except ValueError:
        offset = None
    return synced, offset if synced else None


def parse_rtc(date_text: str, time_text: str) -> float | None:
    """Epoch seconds of the RTC's raw date/time (as stored, no timezone applied)."""
    import calendar
    try:
        year, month, day = (int(part) for part in date_text.strip().split("-"))
        hour, minute, second = (int(part) for part in time_text.strip().split(":"))
        return float(calendar.timegm((year, month, day, hour, minute, second)))
    except ValueError:
        return None


def rtc_mode(rtc_epoch: float | None, now: float, synced: bool | None) -> tuple[str, int | None]:
    """(utc | local | unknown, RTC minus system clock in seconds).

    Only meaningful once the system clock is NTP-correct: a Windows dual-boot
    keeps local time in the RTC, so the RTC differs from true UTC by a
    multiple of 15 minutes (timezone offset).
    """
    if rtc_epoch is None:
        return "unknown", None
    delta = int(round(rtc_epoch - now))
    if not synced:
        return "unknown", delta
    if abs(delta) <= 120:
        return "utc", delta
    nearest = round(delta / 900) * 900
    if nearest and abs(nearest) <= 14 * 3600 and abs(delta - nearest) <= 120:
        return "local", delta
    return "unknown", delta


# ---------------------------------------------------------------------------
# Warnings (pure functions of the profile)
# ---------------------------------------------------------------------------

def _warn(code: str, severity: str, message: str, action: str = "") -> HardwareWarning:
    return HardwareWarning("hw." + code, severity, message, action)


def is_hybrid(profile: HardwareProfile) -> bool:
    """Integrated + discrete GPU where switching helpers make sense.

    A desktop whose monitors sit on the discrete card is not hybrid even when
    the CPU's integrated GPU is enabled: it only counts on laptops/tablets or
    when the integrated GPU is the boot display and has connected outputs.
    """
    real = [gpu for gpu in profile.gpus if not gpu.virtual and gpu.vendor not in data.BMC_GPU_VENDORS]
    integrated = [gpu for gpu in real if gpu.integrated]
    discrete = [gpu for gpu in real if not gpu.integrated]
    if not integrated or not discrete:
        return False
    if profile.machine.form_factor in ("laptop", "tablet"):
        return True
    return any(gpu.boot_vga and (gpu.connected_outputs or 0) > 0 for gpu in integrated)


def derive_warnings(profile: HardwareProfile) -> list[HardwareWarning]:
    """Environment findings with stable ``hw.`` codes; cross-checks against the manifest live in hwplan."""
    out: list[HardwareWarning] = []
    cpu, memory, firmware, machine = profile.cpu, profile.memory, profile.firmware, profile.machine
    if cpu.flags and "lm" not in cpu.flags:
        out.append(_warn("arch_unsupported", "error", "This installer needs a 64-bit x86 PC (x86-64). This processor does not report 64-bit support."))
    if memory.total_kib:
        gib = memory.total_kib / 1048576
        if gib < 1:
            out.append(_warn("memory_critical", "error", f"Only {gib:.1f} GiB of memory was detected. Gentoo cannot be installed with less than 1 GiB."))
        elif gib < 2:
            out.append(_warn("memory_low", "warning", f"Only {gib:.1f} GiB of memory was detected. A graphical install is not recommended; choose Minimal and compressed RAM swap (zram)."))
    # Firmware
    if firmware.mode == "uefi":
        if firmware.secure_boot == "enabled":
            out.append(_warn("fw_sb_enabled", "warning",
                             "Secure Boot is turned on. The installed system's boot files are not signed with a key your firmware trusts, so it will only start if you turn Secure Boot off in the firmware settings (recommended) or set up your own keys (advanced).",
                             "reboot_firmware" if firmware.can_reboot_to_firmware else ""))
        elif firmware.secure_boot == "setup":
            out.append(_warn("fw_sb_setup", "info", "Secure Boot is available but no keys are enrolled, so it is not active. The installed system will start. If you enable Secure Boot later without setting up keys, it will stop starting."))
        elif firmware.secure_boot == "disabled":
            out.append(_warn("fw_sb_disabled", "info", "Secure Boot is off, which is what this installer expects."))
        if firmware.efi_bits == 32:
            out.append(_warn("fw_uefi32", "info", "This PC has 32-bit UEFI firmware with a 64-bit processor (common on small tablets). It needs the 32-bit boot files; this installer media includes them when built with that option."))
        if firmware.efivars_writable is False:
            out.append(_warn("fw_efivars_ro", "warning", "This PC does not allow new boot entries to be saved. The installer will use the fallback boot path instead. If the PC does not start Gentoo on its own, choose the disk in the firmware boot menu (often F12 or Esc)."))
        if firmware.windows_boot_entry:
            out.append(_warn("fw_win_entry", "info", "Windows is installed on this PC. Install next to it only if you have a backup. The hardware clock option matters for dual boot."))
    # Storage
    installable = [disk for disk in profile.disks if disk.installable]
    internal = [disk for disk in installable if not (disk.hotplug or disk.removable or disk.bus == "usb")]
    if not installable and not profile.errors_for("disks"):
        out.append(_warn("st_no_disk", "error", "No disk that can receive an installation was found."))
    raid_controllers = [c for c in profile.storage_controllers if c.kind == "raid" and c.vendor_id == data.VENDOR_INTEL and c.driver not in data.VMD_DRIVERS]
    if raid_controllers and not internal:
        severity = "error" if not installable else "warning"
        out.append(_warn("st_rst_no_disks", severity,
                         "No internal disk is visible. This PC's storage controller is in Intel RAID (RST) mode. In the firmware settings change SATA Operation to AHCI, then restart this installer. "
                         "If Windows is installed on this PC, change its storage driver first or Windows will not start after the switch.",
                         "reboot_firmware" if firmware.can_reboot_to_firmware else ""))
    if any(c.driver in data.VMD_DRIVERS for c in profile.storage_controllers):
        out.append(_warn("st_vmd_active", "info", "This PC uses Intel VMD. Your NVMe drive is visible; no change is needed."))
    if any(c.vendor_id == data.VENDOR_APPLE for c in profile.storage_controllers):
        out.append(_warn("st_apple_t2", "info", "Apple T2 Macs hide the internal drive from stock Linux kernels; this installer cannot use it."))
    # Graphics
    for gpu in profile.gpus:
        if not gpu.driver:
            out.append(_warn("gpu_no_driver", "warning",
                             f"The graphics card {gpu.name or gpu.vendor_name or gpu.slot} has no driver loaded, so the screen is using a basic fallback. 3D acceleration needs firmware or a newer kernel.",
                             "safe_graphics"))
    if is_hybrid(profile):
        out.append(_warn("gpu_hybrid", "info", "This PC has an integrated and a dedicated graphics card. GPU switching helpers can be installed."))
    # Power
    power = profile.power
    if power.has_battery and power.ac_online is False:
        low = power.battery_percent is not None and power.battery_percent < 30
        out.append(_warn("bat_low" if low else "bat_ac_offline", "warning",
                         "The charger is not connected and the battery is low. Connect it before you start; an installation can take several hours." if low
                         else "The charger is not connected. The installation can take several hours; connect it before you start.", "connect_charger"))
    # Clock
    clock = profile.clock
    if clock.offset_s is not None and clock.offset_s > 600:
        out.append(_warn("clock_skew", "error", f"The clock is off by about {int(clock.offset_s // 60)} minutes. Downloads will fail until the time is correct.", "sync_time"))
    elif clock.year and clock.year < CLOCK_FLOOR_YEAR:
        out.append(_warn("clock_skew", "error", f"The clock says {clock.year}, which is long in the past. Downloads over HTTPS fail until the date is correct.", "set_time"))
    elif clock.ntp_synced is False:
        out.append(_warn("clock_unsynced", "info", "The clock has not been checked against the network yet.", "sync_time"))
    if clock.rtc_mode == "local":
        out.append(_warn("clock_rtc_local", "info", "The hardware clock holds local time (typical when Windows is installed). Enable the local-time clock option for dual boot."))
    # Network
    physical = [nic for nic in profile.nics if nic.kind in ("ethernet", "wifi", "wwan", "tether") and not nic.no_driver]
    if not physical and not profile.errors_for("nics"):
        out.append(_warn("net_none", "warning", "No network adapter was found. Connect a USB Ethernet adapter or tether a phone over USB to download packages."))
    for nic in profile.nics:
        if nic.no_driver:
            what = "Wi-Fi" if nic.kind == "wifi" else "network"
            out.append(_warn("net_no_driver", "warning", f"A {what} adapter ({nic.vendor_id}:{nic.device_id}) has no driver in this kernel. Use Ethernet or USB tethering during the install."))
            if nic.vendor_id == data.VENDOR_BROADCOM and nic.kind == "wifi":
                out.append(_warn("net_broadcom_sta", "warning", "This Wi-Fi chip may need the proprietary broadcom-sta driver, which this installer does not provide. Use Ethernet or USB tethering during the install."))
    # Machine
    if machine.hypervisor:
        out.append(_warn("vm_detected", "info", f"Running in a virtual machine ({machine.hypervisor}). Guest tools can be installed; CPU microcode and power management are skipped.", "install_guest_tools"))
    if not profile.input.keyboard and not machine.hypervisor and not profile.errors_for("input"):
        out.append(_warn("no_keyboard", "warning", "No physical keyboard was detected. The installer needs a keyboard (USB or Bluetooth); there is no on-screen keyboard on the live image."))
    return out


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------

@dataclass
class _Pci:
    slot: str
    class_code: str
    vendor_id: str
    device_id: str
    driver: str
    boot_vga: bool
    runtime_status: str


# Domains can be longer than four digits (Intel VMD exposes 10000:e1:00.0).
_SLOT_RE = re.compile(r"[0-9a-f]{4,8}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", re.I)
_HEX_RE = re.compile(r"^(?:0x)?([0-9a-fA-F]+)$")


def _hex(value: str, width: int) -> str:
    match = _HEX_RE.match(value.strip())
    return match.group(1).lower().zfill(width) if match else ""


class _Prober:
    """Runs the collectors against one Fs and one command runner."""

    def __init__(self, fs: Fs, runner: CommandRunner, now: float) -> None:
        self.fs = fs
        self.runner = runner
        self.now = now
        self.profile = HardwareProfile()
        self.pci: list[_Pci] = []
        self.pci_vendors: dict[str, str] = {}
        self.pci_devices: dict[tuple[str, str], str] = {}
        self.roots: list[blockdev.BlockDevice] = []
        self.lscpu_vendor = ""

    # -- infrastructure ------------------------------------------------------

    def command(self, argv: Sequence[str]) -> str | None:
        """stdout of a read-only command, None on any failure."""
        try:
            result = self.runner.run(tuple(argv))
        except Exception:  # noqa: BLE001 - a probe must never raise: missing tool, timeout, RunnerError ...
            return None
        if getattr(result, "returncode", 0):
            return None
        stdout = getattr(result, "stdout", None)
        return stdout if isinstance(stdout, str) else None

    def run(self) -> HardwareProfile:
        steps: tuple[tuple[str, Callable[[], None]], ...] = (
            ("pci", self.collect_pci),
            ("cpu", self.collect_cpu),
            ("memory", self.collect_memory),
            ("machine", self.collect_machine),
            ("firmware", self.collect_firmware),
            ("gpus", self.collect_gpus),
            ("displays", self.collect_displays),
            ("storage_controllers", self.collect_storage_controllers),
            ("disks", self.collect_disks),
            ("nics", self.collect_nics),
            ("power", self.collect_power),
            ("input", self.collect_input),
            ("audio", self.collect_audio),
            ("usb", self.collect_usb),
            ("clock", self.collect_clock),
        )
        for name, step in steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001
                self.profile.errors.append(f"{name}: {type(exc).__name__}: {exc}")
        try:
            self.profile.machine.form_factor = data.form_factor(self.profile.machine.chassis_type, self.profile.power.has_battery, self.profile.machine.hypervisor)
            self.profile.warnings = derive_warnings(self.profile)
        except Exception as exc:  # noqa: BLE001
            self.profile.errors.append(f"warnings: {type(exc).__name__}: {exc}")
        return self.profile

    # -- PCI -------------------------------------------------------------------

    def collect_pci(self) -> None:
        base = "/sys/bus/pci/devices"
        for slot in self.fs.listdir(base):
            path = f"{base}/{slot}"
            class_code = _hex(self.fs.line(path + "/class"), 6)
            vendor, device = _hex(self.fs.line(path + "/vendor"), 4), _hex(self.fs.line(path + "/device"), 4)
            if not class_code or not vendor:
                continue
            self.pci.append(_Pci(slot, class_code, vendor, device, self.fs.link_name(path + "/driver"),
                                 self.fs.line(path + "/boot_vga") == "1", self.fs.line(path + "/power/runtime_status")))
        wanted = {(d.vendor_id, d.device_id) for d in self.pci if d.class_code[:2] in ("01", "02", "03", "04") or d.class_code[:4] == "0805"}
        for candidate in PCI_IDS_PATHS:
            text = self.fs.read(candidate, 8 << 20)
            if text:
                self.pci_vendors, self.pci_devices = parse_pci_ids(text, wanted)
                break

    # -- CPU, memory -------------------------------------------------------------

    def collect_cpu(self) -> None:
        text = self.fs.read("/proc/cpuinfo", 8 << 20)
        if text:
            self.profile.cpu = parse_cpuinfo(text)
        cpu = self.profile.cpu
        flags = parse_cpuid2cpuflags(self.command(("cpuid2cpuflags",)) or "")
        if flags:
            cpu.cpu_flags_x86, cpu.cpu_flags_source = flags, "cpuid2cpuflags"
        elif cpu.flags:
            cpu.cpu_flags_x86, cpu.cpu_flags_source = data.cpuinfo_to_cpu_flags(cpu.flags), "cpuinfo"
        if not cpu.threads and cpu.flags:
            cpu.threads = 1

    def collect_memory(self) -> None:
        text = self.fs.read("/proc/meminfo")
        if text:
            self.profile.memory = parse_meminfo(text)

    # -- machine, hypervisor -------------------------------------------------------

    def collect_machine(self) -> None:
        m = self.profile.machine
        dmi = "/sys/class/dmi/id/"
        m.sys_vendor, m.product_name = self.fs.line(dmi + "sys_vendor"), self.fs.line(dmi + "product_name")
        m.product_version, m.bios_vendor = self.fs.line(dmi + "product_version"), self.fs.line(dmi + "bios_vendor")
        m.bios_version, m.bios_date = self.fs.line(dmi + "bios_version"), self.fs.line(dmi + "bios_date")
        m.board_vendor, m.board_name = self.fs.line(dmi + "board_vendor"), self.fs.line(dmi + "board_name")
        m.chassis_type = self.fs.int_of(dmi + "chassis_type")
        lscpu = self.command(("lscpu",)) or ""
        match = re.search(r"^Hypervisor vendor:\s*(.+)$", lscpu, re.M)
        self.lscpu_vendor = match.group(1).strip() if match else ""
        name, evidence = data.classify_hypervisor(
            m.sys_vendor, m.product_name, m.bios_vendor, cpu_flag=self.profile.cpu.hypervisor_flag,
            lscpu_vendor=self.lscpu_vendor, sysfs_type=self.fs.line("/sys/hypervisor/type"))
        m.hypervisor = name
        self.profile.hypervisor = HypervisorInfo(bool(name), name, self.lscpu_vendor, evidence)

    # -- firmware ---------------------------------------------------------------------

    def collect_firmware(self) -> None:
        f = self.profile.firmware
        major = self.fs.line("/sys/class/tpm/tpm0/tpm_version_major")
        f.tpm = {"2": "2.0", "1": "1.2"}.get(major, "present" if self.fs.exists("/sys/class/tpm/tpm0") else "")
        efi = "/sys/firmware/efi"
        if not (self.fs.isdir(efi) and (self.fs.exists(efi + "/fw_platform_size") or self.fs.exists(efi + "/systab") or self.fs.isdir(efi + "/efivars"))):
            f.mode, f.efi_bits, f.secure_boot = "bios", 0, "n/a"
            return
        f.mode = "uefi"
        size = self.fs.line(efi + "/fw_platform_size")
        f.efi_bits = 32 if size == "32" else 64
        var = f"{efi}/efivars/%s-{EFI_GLOBAL_GUID}"
        f.secure_boot = parse_secure_boot(self.fs.read_bytes(var % "SecureBoot", 64), self.fs.read_bytes(var % "SetupMode", 64))
        if f.secure_boot == "unsupported" and not self.fs.isdir(efi + "/efivars"):
            f.secure_boot = "unknown"
        supported = self.fs.read_bytes(var % "OsIndicationsSupported", 64)
        f.can_reboot_to_firmware = bool(supported and len(supported) >= 5 and supported[4] & 1)
        f.booted_via_shim = self.fs.exists(f"{efi}/efivars/MokListRT-{MOK_GUID}")
        f.lockdown = parse_lockdown(self.fs.read("/sys/kernel/security/lockdown", 256))
        mounts = parse_mountinfo(self.fs.read("/proc/mounts") or "")
        efivarfs = [entry for entry in mounts if entry[1] == "efivarfs"]
        cmdline = (self.fs.read("/proc/cmdline", 8192) or "").split()
        if "efi=noruntime" in cmdline:
            f.efivars_writable = False
        elif efivarfs:
            f.efivars_writable = "rw" in efivarfs[0][3].split(",")
        boot = self.command(("efibootmgr", "-v"))
        f.windows_boot_entry = None if boot is None else "Windows Boot Manager" in boot

    # -- graphics --------------------------------------------------------------------

    def _drm_cards(self) -> dict[str, str]:
        """card name -> PCI slot, from the card's ``device`` link (or the card link text)."""
        cards: dict[str, str] = {}
        for entry in self.fs.listdir("/sys/class/drm"):
            if not re.fullmatch(r"card\d+", entry):
                continue
            slot = self.fs.link_name(f"/sys/class/drm/{entry}/device")
            if not _SLOT_RE.fullmatch(slot):
                found = _SLOT_RE.findall(self.fs.readlink(f"/sys/class/drm/{entry}") or "")
                slot = found[-1] if found else ""
            cards[entry] = slot.lower()
        return cards

    def collect_gpus(self) -> None:
        cards = self._drm_cards()
        card_of = {slot: card for card, slot in cards.items() if slot}
        gpus: list[GpuInfo] = []
        for dev in self.pci:
            if dev.class_code[:2] != "03":
                continue
            vendor = data.gpu_vendor(dev.vendor_id)
            name = self.pci_devices.get((dev.vendor_id, dev.device_id), "")
            gpu = GpuInfo(dev.slot, dev.vendor_id, dev.device_id, vendor, self.pci_vendors.get(dev.vendor_id, ""), name, dev.driver,
                          dev.boot_vga, card=card_of.get(dev.slot, ""))
            gpu.virtual = vendor in data.VIRTUAL_GPU_VENDORS
            if vendor == "nvidia":
                gpu.generation, gpu.generation_guess = data.classify_nvidia(name, dev.device_id)
            elif vendor == "amd":
                gpu.generation = data.classify_amd(name, dev.driver)
                gpu.integrated = data.amd_is_integrated(name)
            elif vendor == "intel":
                gpu.generation = data.classify_intel_gpu(name)
                gpu.integrated = not data.intel_is_discrete(name)
            gpus.append(gpu)
        physical = [g for g in gpus if not g.virtual and g.vendor not in data.BMC_GPU_VENDORS]
        if len(physical) > 1:
            for gpu in physical:  # unnamed AMD boot GPU next to another card: an APU
                if gpu.vendor == "amd" and not gpu.name and gpu.boot_vga and any(other is not gpu for other in physical):
                    gpu.integrated = True
        self.profile.gpus = gpus

    def collect_displays(self) -> None:
        cards = self._drm_cards()
        gpu_by_card = {gpu.card: gpu for gpu in self.profile.gpus if gpu.card}
        runtime = {dev.slot: dev.runtime_status for dev in self.pci}
        outputs: dict[str, int] = {}
        displays: list[DisplayInfo] = []
        for entry in self.fs.listdir("/sys/class/drm"):
            match = re.fullmatch(r"(card\d+)-(.+)", entry)
            if not match:
                continue
            card, connector = match.group(1), match.group(2)
            slot = cards.get(card, "")
            if runtime.get(slot) == "suspended":
                continue  # reading a connector would wake a powered-off GPU
            base = f"/sys/class/drm/{entry}"
            if self.fs.line(base + "/status") != "connected":
                continue
            outputs[card] = outputs.get(card, 0) + 1
            info = parse_edid(self.fs.read_bytes(base + "/edid", 4096))
            mode = (self.fs.line(base + "/modes") or "").split("x")
            width = info.get("width") or (_to_int(mode[0]) if len(mode) == 2 else 0)
            height = info.get("height") or (_to_int(mode[1]) if len(mode) == 2 else 0)
            mm_w, mm_h = info.get("mm_width", 0), info.get("mm_height", 0)
            displays.append(DisplayInfo(entry, card, slot, bool(re.match(r"(?:eDP|LVDS|DSI)", connector)), info.get("name", ""),
                                        width, height, mm_w, mm_h, dpi_from(width, mm_w, height, mm_h)))
        for card, gpu in gpu_by_card.items():
            if runtime.get(gpu.slot) != "suspended":
                gpu.connected_outputs = outputs.get(card, 0)
        self.profile.displays = displays

    # -- storage ---------------------------------------------------------------------

    def collect_storage_controllers(self) -> None:
        controllers: list[StorageController] = []
        for dev in self.pci:
            kind = data.STORAGE_CLASSES.get(dev.class_code[:4])
            if kind is None:
                continue
            if dev.vendor_id == data.VIRTIO_VENDOR:
                kind = "virtio"
            controllers.append(StorageController(dev.slot, dev.vendor_id, dev.device_id, dev.class_code[:4], kind, dev.driver))
        self.profile.storage_controllers = controllers

    def _by_path_names(self) -> dict[str, str]:
        """disk kernel name -> preferred /dev/disk/by-path link name."""
        names: dict[str, list[str]] = {}
        for entry in self.fs.listdir("/dev/disk/by-path"):
            if re.search(r"-part\d+$", entry):
                continue
            target = self.fs.link_name("/dev/disk/by-path/" + entry)
            if target:
                names.setdefault(target, []).append(entry)
        # prefer the plain pci-/platform- link over the virtio-pci- duplicate
        return {disk: sorted(links, key=lambda n: (n.startswith("virtio-"), len(n), n))[0] for disk, links in names.items()}

    @staticmethod
    def _bus_of(name: str, link: str, tran: str) -> str:
        if "/usb" in link or tran == "usb":
            return "usb"
        if "/nvme/" in link or name.startswith("nvme") or tran == "nvme":
            return "nvme"
        if "/virtio" in link or (name.startswith("vd") and not link):
            return "virtio"
        if "/mmc" in link or name.startswith("mmcblk"):
            return "sd"
        if tran in ("sata", "ata") or "/ata" in link:
            return "sata"
        if tran == "sas" or "/end_device" in link or "/sas" in link:
            return "sas"
        if "/host" in link or tran:
            return "scsi"
        return "other"

    def _live_sources(self) -> list[str]:
        mounts = parse_mountinfo(self.fs.read("/proc/self/mountinfo") or self.fs.read("/proc/mounts") or "")
        return [source for point, _fstype, source, _opts in mounts if point == LIVE_MOUNT and source.startswith("/dev/")]

    def collect_disks(self) -> None:
        fs = self.fs
        by_path = self._by_path_names()
        disks: dict[str, DiskInfo] = {}
        for name in fs.listdir("/sys/block"):
            if data.IGNORED_DISK_RE.match(name):
                continue
            base = f"/sys/block/{name}"
            link = fs.readlink(base) or ""
            sectors = fs.int_of(base + "/size") or 0
            rota = fs.int_of(base + "/queue/rotational")
            disk = DiskInfo(
                name=name, path="/dev/" + name, size=sectors * 512,
                model=" ".join(fs.line(base + "/device/model").split()), vendor=fs.line(base + "/device/vendor").strip(),
                rota=None if rota is None else bool(rota),
                log_sec=fs.int_of(base + "/queue/logical_block_size") or 0, phy_sec=fs.int_of(base + "/queue/physical_block_size") or 0,
                removable=bool(fs.int_of(base + "/removable")), ro=bool(fs.int_of(base + "/ro")),
                maj_min=fs.line(base + "/dev"), by_path=by_path.get(name, ""))
            disk.bus = self._bus_of(name, link, "")
            disk.hotplug = disk.removable or disk.bus == "usb"
            disks[name] = disk
        lsblk = self.command(blockdev.lsblk_argv())
        if lsblk:
            try:  # a runner that merges stderr into stdout may put a warning in front of the JSON
                self.roots = blockdev.parse_lsblk(lsblk[lsblk.index("{"):] if "{" in lsblk else lsblk)
            except (ValueError, KeyError, TypeError, AttributeError):
                self.profile.errors.append("disks: lsblk output could not be parsed")
        live_keys = set()
        if self.roots:
            live_keys = blockdev.live_medium_keys(self.roots, sources=self._live_sources(), cmdline=fs.read("/proc/cmdline", 8192) or "")
        for root in self.roots:
            if root.type != "disk" or data.IGNORED_DISK_RE.match(root.name):
                continue
            known = disks.get(root.name)
            if known is None:  # a disk lsblk lists but /sys/block did not (a fixture without /sys/block)
                known = DiskInfo(name=root.name, path=root.path, size=root.size, log_sec=root.log_sec, phy_sec=root.phy_sec,
                                 maj_min=root.maj_min, by_path=by_path.get(root.name, ""))
                known.bus = self._bus_of(root.name, "", root.transport)
                disks[root.name] = known
            disk = known
            disk.tran = root.transport
            if root.transport:
                disk.bus = self._bus_of(root.name, fs.readlink(f"/sys/block/{root.name}") or "", root.transport)
            disk.model = disk.model or root.model
            disk.serial, disk.wwn, disk.ptuuid, disk.pttype = root.serial, root.wwn, root.ptuuid, root.pttype
            disk.hotplug = disk.hotplug or root.hotplug
            disk.removable = disk.removable or root.removable
            disk.ro = disk.ro or root.ro
            disk.size = disk.size or root.size
            disk.log_sec, disk.phy_sec = disk.log_sec or root.log_sec, disk.phy_sec or root.phy_sec
            disk.mounted = root.mounted
            nodes = blockdev.all_nodes([root])
            disk.signatures = sorted({node.fstype for node in nodes if node.fstype})
            disk.partitions = [PartitionInfo(node.name, node.size, node.fstype, node.label, node.parttype, bool(node.mountpoints))
                               for node in root.children if node.type == "part"]
            disk.live_medium = root.key in live_keys
        if not self.roots:  # no lsblk: still recognise the live medium from the mount table
            sources = {parent_disk_name(os.path.basename(source)) for source in self._live_sources()}
            for disk in disks.values():
                disk.live_medium = disk.name in sources
        for disk in disks.values():
            disk.installable, disk.blocker = True, ""
            if disk.ro:
                disk.installable, disk.blocker = False, "read-only"
            elif disk.live_medium:
                disk.installable, disk.blocker = False, "live-medium"
            elif disk.size <= 0:
                disk.installable, disk.blocker = False, "empty"
        self.profile.disks = sorted(disks.values(), key=lambda d: d.name)

    # -- network ----------------------------------------------------------------------

    def _default_route_interface(self) -> str:
        best: tuple[int, str] | None = None
        for line in (self.fs.read("/proc/net/route") or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 7 and parts[1] == "00000000" and _to_int(parts[3], 16) & 2:
                metric = _to_int(parts[6])
                if best is None or metric < best[0]:
                    best = (metric, parts[0])
        return best[1] if best else ""

    def collect_nics(self) -> None:
        fs = self.fs
        primary = self._default_route_interface()
        nics: list[NicInfo] = []
        seen_slots: set[str] = set()
        for iface in fs.listdir("/sys/class/net"):
            base = f"/sys/class/net/{iface}"
            link = fs.readlink(base) or ""
            if link:
                if "/virtual/" in link:
                    continue
            elif iface == "lo" or iface.startswith(data.VIRTUAL_NIC_PREFIXES):
                continue
            driver = fs.link_name(base + "/device/driver")
            subsystem = fs.link_name(base + "/device/subsystem")
            if "/usb" in link or subsystem == "usb":
                bus = "usb"
            elif "/virtio" in link or subsystem == "virtio":
                bus = "virtio"
            elif subsystem in ("pci", "sdio"):
                bus = subsystem
            else:
                bus = subsystem or "other"
            if fs.exists(base + "/wireless") or fs.exists(base + "/phy80211"):
                kind = "wifi"
            elif iface.startswith("wwan") or driver in data.WWAN_DRIVERS:
                kind = "wwan"
            elif driver in data.TETHERING_DRIVERS:
                kind = "tether"
            else:
                kind = "ethernet"
            slot = fs.link_name(base + "/device").lower()
            if not _SLOT_RE.fullmatch(slot):  # virtio/usb parents: the PCI adapter is in the link text
                found = _SLOT_RE.findall(link)
                slot = found[-1].lower() if found else ""
            carrier = fs.line(base + "/carrier")
            nics.append(NicInfo(iface, kind, driver, bus, _hex(fs.line(base + "/device/vendor"), 4) if bus == "pci" else "",
                                _hex(fs.line(base + "/device/device"), 4) if bus == "pci" else "", slot,
                                None if carrier not in ("0", "1") else carrier == "1", iface == primary))
            if slot:
                seen_slots.add(slot)
        for dev in self.pci:  # PCI network controllers that never got a net device
            if dev.class_code[:2] == "02" and dev.slot not in seen_slots:
                nics.append(NicInfo("", "wifi" if dev.class_code[:4] == "0280" else "ethernet", dev.driver, "pci", dev.vendor_id,
                                    dev.device_id, dev.slot, None, False, no_driver=not dev.driver))
        self.profile.nics = nics

    # -- power, input, audio, usb, clock -----------------------------------------------

    def collect_power(self) -> None:
        fs = self.fs
        power = self.profile.power
        batteries: list[tuple[str, int | None]] = []
        ac: list[bool] = []
        for name in fs.listdir("/sys/class/power_supply"):
            base = f"/sys/class/power_supply/{name}"
            kind, scope = fs.line(base + "/type"), fs.line(base + "/scope")
            if scope == "Device":  # a mouse, headset or game controller, not the machine
                continue
            if kind == "Battery":
                if fs.line(base + "/present") not in ("", "1"):
                    continue
                batteries.append((fs.line(base + "/status"), fs.int_of(base + "/capacity")))
            elif kind in ("Mains", "USB") and fs.line(base + "/online") in ("0", "1"):
                ac.append(fs.line(base + "/online") == "1")
        power.battery_count = len(batteries)
        power.has_battery = bool(batteries)
        percents = [percent for _status, percent in batteries if percent is not None]
        power.battery_percent = round(sum(percents) / len(percents)) if percents else None
        statuses = [status for status, _percent in batteries if status]
        power.battery_status = "Discharging" if "Discharging" in statuses else (statuses[0] if statuses else "")
        power.ac_online = any(ac) if ac else None
        power.has_lid = bool(fs.listdir("/proc/acpi/button/lid"))
        power.has_backlight = bool(fs.listdir("/sys/class/backlight"))

    def collect_input(self) -> None:
        text = self.fs.read("/proc/bus/input/devices", 1 << 20)
        info = self.profile.input
        if text is None:
            raise OSError("/proc/bus/input/devices is not readable")
        for block in text.split("\n\n"):
            name_match = re.search(r'^N: Name="(.*)"', block, re.M)
            name = name_match.group(1) if name_match else ""
            handlers = re.search(r"^H: Handlers=(.*)$", block, re.M)
            handler_text = handlers.group(1) if handlers else ""
            if "kbd" in handler_text.split() and "leds" in handler_text.split():
                info.keyboard = True
            if data.GAME_CONTROLLER_RE.search(name):
                continue  # a DualSense or Xbox pad has a touchpad and a motion sensor too
            if name == "Lid Switch":
                self.profile.power.has_lid = True
            elif data.TOUCHPAD_RE.search(name):
                info.touchpad = True
            elif data.TOUCHSCREEN_RE.search(name):
                info.touchscreen = True

    def collect_audio(self) -> None:
        audio = self.profile.audio
        for dev in self.pci:
            if dev.class_code[:4] in ("0400", "0401", "0403", "0480"):  # 0480: AMD ACP and other audio DSPs
                audio.cards.append(AudioCard(dev.slot, dev.vendor_id, dev.device_id, dev.driver))
        for card in self.fs.listdir("/sys/class/sound"):
            if re.fullmatch(r"card\d+", card):
                driver = self.fs.link_name(f"/sys/class/sound/{card}/device/driver")
                if driver and not any(driver == known.driver for known in audio.cards):
                    audio.cards.append(AudioCard("", "", "", driver))
        audio.needs_sof = any(data.SOF_DRIVER_RE.search(card.driver) for card in audio.cards if card.driver)

    def collect_usb(self) -> None:
        fs = self.fs
        info, bluetooth, peripherals = self.profile.input, self.profile.bluetooth, self.profile.peripherals
        for hci in fs.listdir("/sys/class/bluetooth"):
            if hci.startswith("hci") and ":" not in hci:
                bluetooth.present = True
        for entry in fs.listdir("/sys/bus/thunderbolt/devices"):
            peripherals.thunderbolt = bool(entry)
        base = "/sys/bus/usb/devices"
        for name in fs.listdir(base):
            if ":" in name or name.startswith("usb"):
                continue
            path = f"{base}/{name}"
            vendor, product = _hex(fs.line(path + "/idVendor"), 4), _hex(fs.line(path + "/idProduct"), 4)
            classes = {fs.line(path + "/bDeviceClass").lower()}
            for child in fs.listdir(path):
                if child.startswith(name + ":"):
                    classes.add(fs.line(f"{path}/{child}/bInterfaceClass").lower())
            if "e0" in classes:
                bluetooth.present = True
            if data.USB_CLASS_PRINTER in classes:
                peripherals.usb_printers += 1
            if "0e" in classes:
                peripherals.webcam = True
            if vendor in data.FINGERPRINT_USB_VENDORS and data.USB_CLASS_VENDOR_SPECIFIC in classes:
                info.fingerprint_usb.append(f"{vendor}:{product}")

    def collect_clock(self) -> None:
        clock = self.profile.clock
        clock.year = time.gmtime(self.now).tm_year
        synced, offset = parse_chrony_tracking(self.command(("chronyc", "-c", "tracking")))
        clock.ntp_synced, clock.offset_s = synced, offset
        rtc = parse_rtc(self.fs.line("/sys/class/rtc/rtc0/date"), self.fs.line("/sys/class/rtc/rtc0/time"))
        clock.rtc_mode, clock.rtc_delta_s = rtc_mode(rtc, self.now, synced)


def parent_disk_name(name: str) -> str:
    """Kernel disk name for a partition or disk name (sda1 -> sda, nvme0n1p2 -> nvme0n1, mmcblk0p1 -> mmcblk0, sda -> sda)."""
    match = re.fullmatch(r"(nvme\d+n\d+|mmcblk\d+)p\d+", name) or re.fullmatch(r"((?:sd|vd|hd|xvd)[a-z]+)\d+", name)
    return match.group(1) if match else name


def probe(root: Path = Path("/"), runner: CommandRunner | None = None, *, now: float | None = None,
          recorder: Recorder | None = None) -> HardwareProfile:
    """Describe the machine below ``root`` (default: this machine).

    Reads only; works unprivileged; never raises. ``runner`` supplies command
    output (the daemon passes its own Runner, tests a fake). Without one, the
    real machine uses a bounded local runner and a fixture tree uses its
    recorded ``.commands``. ``now`` and ``recorder`` exist for tests and for
    the fixture capture tool.
    """
    base = default_root(root)
    if runner is None:
        runner = LocalRunner() if str(base) == "/" else FixtureRunner(base)
    profile = _Prober(Fs(base, recorder), runner, time.time() if now is None else now).run()
    return profile


# ---------------------------------------------------------------------------
# Command line: python -m gentoo_installer.hardware [--json] [--root DIR] [--redact]
# ---------------------------------------------------------------------------

def format_report(profile: HardwareProfile) -> str:
    """Plain-text report for a terminal or a bug report."""
    lines = [profile.summary(), ""]
    machine, cpu, firmware = profile.machine, profile.cpu, profile.firmware
    lines.append(f"Machine : {machine.sys_vendor} {machine.product_name} ({machine.form_factor or 'unknown form factor'})")
    lines.append(f"CPU     : {cpu.model_name} [{cpu.vendor or 'unknown'}] x86-64-v{cpu.x86_64_level}, {cpu.threads} threads")
    lines.append(f"Firmware: {firmware.mode.upper()} {firmware.efi_bits or ''}, Secure Boot {firmware.secure_boot}")
    for gpu in profile.gpus:
        lines.append(f"GPU     : {gpu.name or gpu.vendor_name} [{gpu.vendor} {gpu.generation}] driver={gpu.driver or '-'}"
                     f"{' boot' if gpu.boot_vga else ''}{' integrated' if gpu.integrated else ''}")
    for disk in profile.disks:
        lines.append(f"Disk    : {disk.path} {disk.size / 10**9:.0f} GB {disk.bus} {'SSD' if disk.rota is False else 'HDD' if disk.rota else '?'} "
                     f"{disk.model}{'' if disk.installable else ' (' + disk.blocker + ')'}")
    for nic in profile.nics:
        lines.append(f"Network : {nic.iface or '(no device)'} {nic.kind} driver={nic.driver or '-'}")
    for warning in profile.warnings:
        lines.append(f"[{warning.severity}] {warning.code}: {warning.message}")
    for error in profile.errors:
        lines.append(f"[probe error] {error}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m gentoo_installer.hardware", description="Describe this machine's hardware (read-only).")
    parser.add_argument("--root", help=f"read a captured fixture tree instead of this machine (default: ${ENV_ROOT} or /)")
    parser.add_argument("--json", action="store_true", help="print the full profile as JSON")
    parser.add_argument("--redact", action="store_true", help="drop disk serial numbers, WWNs and partition-table ids")
    parser.add_argument("--summary", action="store_true", help="print only the one-line summary")
    parser.add_argument("--now", type=float, help="pretend this is the current time (epoch seconds); defaults to the fixture's capture time")
    args = parser.parse_args(argv)
    root = default_root(Path(args.root) if args.root else None)
    now = args.now
    if now is None and str(root) != "/":
        try:  # a fixture was captured at a fixed moment; the RTC and clock checks only make sense relative to it
            now = float(json.loads((root / ".capture.json").read_text())["now"])
        except (OSError, ValueError, KeyError):
            now = None
    profile = probe(root, now=now)
    if args.json:
        print(json.dumps(profile.to_dict(redact=args.redact), indent=2, sort_keys=True))
    elif args.summary:
        print(profile.summary())
    else:
        print(format_report(profile))
    return 0


if __name__ == "__main__":
    sys.exit(main())
