"""The QEMU machine shape of a harness VM: disk bus, sector size, NIC, display, CPU, SMBIOS identity.

Every option has a default that reproduces the original single machine (q35, one virtio-blk
disk with a serial, virtio NIC and display, default CPU), so existing scenarios and tools keep
their exact command line. Everything here is pure: it builds argument lists and small files,
and never starts anything.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

from vm_guard import BUS_FACTS, NONCE_KEY

DISK_BUSES = ("virtio", "sata", "nvme", "usb", "scsi")
NICS = ("virtio", "e1000e", "rtl8139")
VGAS = ("virtio", "std", "qxl", "cirrus", "vmware", "none")
MACHINES = ("q35", "pc")
OVMF_VARS_MODES = ("persist", "fresh-on-target", "readonly")
RTC_MODES = ("utc", "localtime")
SECTOR_SIZES = (512, 4096)
CPU_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:,[+-]?[A-Za-z0-9_.-]+(?:=[A-Za-z0-9_.-]+)?)*")


@dataclass(frozen=True)
class VMHardware:
    disk_bus: str = "virtio"
    disk_serial: bool = True        # False: the disk has no serial number (virtio only)
    sector_size: int = 512          # 4096 for a 4Kn disk
    removable: bool = False         # usb only: the disk reports itself removable
    nic: str = "virtio"
    vga: str = "virtio"
    cpu: str = ""                   # "" keeps QEMU's default model
    machine: str = "q35"
    smbios: str = "qemu"            # a key of SMBIOS_PRESETS
    extra_disks: tuple[int, ...] = ()   # nominal sizes in MiB of further raw disks
    secure_boot: bool = False       # OVMF secure-boot build (setup mode unless a vars template enrols keys)
    ovmf_vars: str = "persist"      # persist | fresh-on-target | readonly
    rtc: str = "utc"

    def problems(self) -> list[str]:
        found: list[str] = []
        for name, value, choices in (("disk bus", self.disk_bus, DISK_BUSES), ("NIC", self.nic, NICS),
                                     ("display", self.vga, VGAS), ("machine", self.machine, MACHINES),
                                     ("OVMF variable store mode", self.ovmf_vars, OVMF_VARS_MODES),
                                     ("RTC", self.rtc, RTC_MODES), ("SMBIOS preset", self.smbios, tuple(SMBIOS_PRESETS))):
            if value not in choices:
                found.append(f"Unknown {name} {value!r}; choose one of {', '.join(map(str, choices))}")
        if self.sector_size not in SECTOR_SIZES:
            found.append("The sector size must be 512 or 4096")
        if not self.disk_serial and self.disk_bus != "virtio":
            found.append(f"A disk without a serial number is only supported on the virtio bus (QEMU gives {self.disk_bus} disks one "
                         "of its own, and NVMe requires one)")
        if self.sector_size == 4096 and self.disk_bus == "sata":
            found.append("QEMU's IDE/SATA disks only have 512-byte logical sectors: use --disk-bus nvme, virtio, scsi or usb for 4096")
        if self.removable and self.disk_bus != "usb":
            found.append("A removable disk needs --disk-bus usb")
        if self.cpu and not CPU_RE.fullmatch(self.cpu):
            found.append(f"Invalid --cpu {self.cpu!r}")
        if any(size < 64 or size > 1 << 21 for size in self.extra_disks):
            found.append("Extra disks must be between 64 MiB and 2 TiB")
        return found

    def require_valid(self) -> "VMHardware":
        problems = self.problems()
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def facts(self) -> dict:
        """What the guest sees on this disk bus (model, transport, PCI vendors)."""
        return BUS_FACTS[self.disk_bus]


# --- SMBIOS identity ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class SmbiosPreset:
    description: str
    manufacturer: str = ""
    product: str = ""
    version: str = ""
    bios_vendor: str = ""
    chassis_type: int = 0           # SMBIOS enclosure type (0 = leave QEMU's)
    chassis_manufacturer: str = ""


SMBIOS_PRESETS: dict[str, SmbiosPreset] = {
    "qemu": SmbiosPreset("QEMU's own strings (the default)"),
    "laptop-lenovo": SmbiosPreset("A ThinkPad-like notebook", "LENOVO", "20XW0055GE", "ThinkPad X1 Carbon Gen 9",
                                  "LENOVO", 10, "LENOVO"),
    "laptop-dell": SmbiosPreset("A Dell XPS-like notebook", "Dell Inc.", "XPS 13 9310", "",
                                "Dell Inc.", 10, "Dell Inc."),
    "laptop-hp": SmbiosPreset("An HP convertible", "HP", "HP Spectre x360 Convertible 14-ea0xxx", "",
                              "HP", 31, "HP"),
    "desktop-asus": SmbiosPreset("An ASUS desktop board", "ASUSTeK COMPUTER INC.", "System Product Name", "System Version",
                                 "American Megatrends Inc.", 3, "Default string"),
    # Wave 5: the vendors whose laptops get their own tools. The strings follow public dmidecode reports (and the tests/hardware fixtures of the same
    # names); the Razer ones are NOT verified against a real Blade.
    "laptop-asus-rog": SmbiosPreset("An ASUS ROG Zephyrus laptop (asusctl, supergfxctl offers)", "ASUSTeK COMPUTER INC.", "ROG Zephyrus M16 GU603ZW_GU603ZW", "1.0",
                                    "American Megatrends International, LLC.", 10, "ASUSTeK COMPUTER INC."),
    "laptop-razer-blade": SmbiosPreset("A Razer Blade laptop (OpenRazer offers; the strings are unverified)", "Razer", "Blade 15 Base Model (Early 2020) - RZ09-0328",
                                       "8.04", "Razer", 10, "Razer"),
    "laptop-lenovo-legion": SmbiosPreset("A Lenovo Legion laptop (platform profiles, battery conservation)", "LENOVO", "82JW", "Legion 5 15ACH6H", "LENOVO", 10, "LENOVO"),
    "vmware": SmbiosPreset("VMware's strings (hypervisor detection)", "VMware, Inc.", "VMware Virtual Platform",
                           "None", "Phoenix Technologies LTD"),
    "virtualbox": SmbiosPreset("VirtualBox's strings (hypervisor detection)", "innotek GmbH", "VirtualBox",
                               "1.2", "innotek GmbH"),
    "hyperv": SmbiosPreset("Hyper-V's strings (hypervisor detection)", "Microsoft Corporation", "Virtual Machine",
                           "Hyper-V UEFI Release v4.1", "Microsoft Corporation"),
}


def qemu_escape(value: str) -> str:
    """QEMU option values double a comma to include a literal one."""
    return value.replace(",", ",,")


def chassis_blob(chassis_type: int, manufacturer: str, version: str = "", serial: str = "0", asset: str = "") -> bytes:
    """One SMBIOS type 3 (System Enclosure) structure for ``-smbios file=``.

    QEMU's ``-smbios type=3`` cannot set the enclosure type, which is what the kernel exposes
    as /sys/class/dmi/id/chassis_type (10 = notebook, 31 = convertible, 3 = desktop).
    Layout per SMBIOS 3.x: formatted area of 0x16 bytes, then the strings, then a double NUL.
    """
    strings = [manufacturer, version, serial, asset]
    indexes: list[int] = []
    rank = 0
    for text in strings:  # a string's index is its rank among the strings that are present
        rank += bool(text)
        indexes.append(rank if text else 0)
    formatted = struct.pack(
        "<BBHBBBBBBBBBIBBBB",
        3, 0x16, 0x0300,                 # type, length, handle (clear of QEMU's own handles)
        indexes[0],                      # manufacturer
        chassis_type,                    # enclosure type
        indexes[1], indexes[2], indexes[3],   # version, serial number, asset tag
        3, 3, 3, 3,                      # boot-up, power supply, thermal state, security status: safe
        0,                               # OEM-defined
        0, 1, 0, 0,                      # height, power cords, contained element count and record length
    ) + b"\x00"                          # SKU number string index
    assert len(formatted) == 0x16, len(formatted)
    area = b"".join(item.encode() + b"\0" for item in strings if item)
    return formatted + (area or b"\0") + b"\0"


def smbios_arguments(hardware: VMHardware, run_dir: Path, nonce: str = "") -> list[str]:
    """``-smbios`` options for the preset and the per-run nonce (writes the chassis file if needed)."""
    preset = SMBIOS_PRESETS[hardware.smbios]
    args: list[str] = []
    if preset.bios_vendor:
        args += ["-smbios", f"type=0,vendor={qemu_escape(preset.bios_vendor)}"]
    if preset.manufacturer:
        fields = [f"manufacturer={qemu_escape(preset.manufacturer)}", f"product={qemu_escape(preset.product)}"]
        if preset.version:
            fields.append(f"version={qemu_escape(preset.version)}")
        args += ["-smbios", "type=1," + ",".join(fields)]
    if preset.chassis_type:
        blob = run_dir / "smbios-chassis.bin"
        blob.write_bytes(chassis_blob(preset.chassis_type, preset.chassis_manufacturer or preset.manufacturer))
        args += ["-smbios", f"file={blob}"]
    if nonce:
        args += ["-smbios", f"type=11,value={NONCE_KEY}={nonce}"]
    return args


# --- devices -----------------------------------------------------------------------------------------

def block_size_options(hardware: VMHardware) -> str:
    if hardware.sector_size == 512:
        return ""
    return f",logical_block_size={hardware.sector_size},physical_block_size={hardware.sector_size}"


def disk_device_arguments(hardware: VMHardware, drive: str, serial: str, index: int = 0) -> list[str]:
    """The ``-device`` options that attach the already declared ``-drive ...,id=<drive>`` on the chosen bus.

    ``index`` numbers the disks of one VM so controllers and ports do not collide. An empty
    serial leaves the property out (virtio only; see VMHardware.problems()).
    """
    bus = hardware.disk_bus
    ident = f",serial={serial}" if serial else ""
    sizes = block_size_options(hardware)
    if bus == "virtio":
        return ["-device", f"virtio-blk-pci,drive={drive}{ident}{sizes}"]
    if bus == "sata":
        controller = f"ahci{index}"
        return ["-device", f"ich9-ahci,id={controller}",
                "-device", f"ide-hd,bus={controller}.0,drive={drive}{ident}{sizes}"]
    if bus == "scsi":
        controller = f"scsi{index}"
        return ["-device", f"virtio-scsi-pci,id={controller}",
                "-device", f"scsi-hd,bus={controller}.0,drive={drive}{ident}{sizes}"]
    if bus == "usb":
        controller = f"xhci{index}"
        removable = ",removable=on" if hardware.removable else ""
        return ["-device", f"qemu-xhci,id={controller}",
                "-device", f"usb-storage,bus={controller}.0,drive={drive}{ident}{removable}{sizes}"]
    if hardware.sector_size == 512:  # nvme: the controller needs a serial
        return ["-device", f"nvme,drive={drive}{ident}"]
    controller = f"nvme{index}"
    return ["-device", f"nvme,id={controller}{ident}",
            "-device", f"nvme-ns,drive={drive},bus={controller},nsid=1{sizes}"]


def nic_arguments(hardware: VMHardware, netdev: str = "net0") -> list[str]:
    model = {"virtio": "virtio-net-pci", "e1000e": "e1000e", "rtl8139": "rtl8139"}[hardware.nic]
    return ["-device", f"{model},netdev={netdev}"]


def machine_arguments(hardware: VMHardware) -> list[str]:
    spec = f"{hardware.machine},accel=kvm:tcg"
    if hardware.secure_boot:
        spec = f"{hardware.machine},smm=on,accel=kvm:tcg"
    args = ["-machine", spec]
    if hardware.cpu:
        args += ["-cpu", hardware.cpu]
    if hardware.secure_boot:
        args += ["-global", "driver=cfi.pflash01,property=secure,value=on"]
    if hardware.rtc == "localtime":
        args += ["-rtc", "base=localtime"]
    return args
