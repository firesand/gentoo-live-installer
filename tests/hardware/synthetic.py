"""Builds the synthetic hardware fixtures under tests/hardware/fixtures.

Run ``python tests/hardware/synthetic.py`` to regenerate them (the captured
host fixture is produced by scripts/capture-hardware-fixture.py instead). The
fixtures are plain directory trees in the layout the probe reads, committed to
the repository; this module only exists so that nobody edits hundreds of tiny
files by hand. Every machine is modelled on real hardware but is synthetic:
identifiers are plausible, serial numbers and MAC addresses do not exist.

Device names come from the pci.ids database of the machine that runs the
builder (trimmed into the fixture), so regenerate with the same hwdata.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from gentoo_installer import hardware  # noqa: E402

_spec = importlib.util.spec_from_file_location("capture_hardware_fixture", ROOT / "scripts" / "capture-hardware-fixture.py")
_capture = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["capture_hardware_fixture"] = _capture
_spec.loader.exec_module(_capture)  # type: ignore[union-attr]
FixtureWriter: Any = _capture.FixtureWriter

FIXTURES = HERE / "fixtures"
NOW = 1_792_800_000  # 2026-10-24T00:00:00Z: every synthetic machine "boots" at this instant

# --- CPU flag sets (modelled on /proc/cpuinfo of the named CPUs) ---------------------------------

BASE_FLAGS = "fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush mmx fxsr sse sse2 syscall nx rdtscp lm constant_tsc rep_good nopl cpuid"
FLAGS_TIGERLAKE_H = (
    "fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush dts acpi mmx fxsr sse sse2 ss ht tm pbe syscall nx pdpe1gb rdtscp lm "
    "constant_tsc art arch_perfmon pebs bts rep_good nopl xtopology nonstop_tsc cpuid aperfmperf tsc_known_freq pni pclmulqdq dtes64 monitor ds_cpl vmx smx est "
    "tm2 ssse3 sdbg fma cx16 xtpr pdcm pcid sse4_1 sse4_2 x2apic movbe popcnt tsc_deadline_timer aes xsave avx f16c rdrand lahf_lm abm 3dnowprefetch cpuid_fault "
    "epb cat_l2 invpcid_single cdp_l2 ssbd ibrs ibpb stibp ibrs_enhanced tpr_shadow flexpriority ept vpid ept_ad fsgsbase tsc_adjust bmi1 avx2 smep bmi2 erms "
    "invpcid avx512f avx512dq rdseed adx smap avx512ifma clflushopt clwb intel_pt avx512cd sha_ni avx512bw avx512vl xsaveopt xsavec xgetbv1 xsaves dtherm ida "
    "arat pln pts hwp hwp_notify hwp_act_window hwp_epp hwp_pkg_req avx512vbmi umip pku ospke avx512_vbmi2 gfni vaes vpclmulqdq avx512_vnni avx512_bitalg "
    "avx512_vpopcntdq rdpid movdiri movdir64b fsrm avx512_vp2intersect md_clear flush_l1d arch_capabilities")
FLAGS_KABYLAKE_H = (
    "fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush dts acpi mmx fxsr sse sse2 ss ht tm pbe syscall nx pdpe1gb rdtscp lm "
    "constant_tsc art arch_perfmon pebs bts rep_good nopl xtopology nonstop_tsc cpuid aperfmperf tsc_known_freq pni pclmulqdq dtes64 monitor ds_cpl vmx est tm2 "
    "ssse3 sdbg fma cx16 xtpr pdcm pcid sse4_1 sse4_2 x2apic movbe popcnt tsc_deadline_timer aes xsave avx f16c rdrand lahf_lm abm 3dnowprefetch cpuid_fault "
    "epb invpcid_single pti ssbd ibrs ibpb stibp tpr_shadow flexpriority ept vpid ept_ad fsgsbase tsc_adjust bmi1 hle avx2 smep bmi2 erms invpcid rtm mpx "
    "rdseed adx smap clflushopt intel_pt xsaveopt xsavec xgetbv1 xsaves dtherm ida arat pln pts hwp hwp_notify hwp_act_window hwp_epp md_clear flush_l1d")
FLAGS_ZEN2_MOBILE = (
    "fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush mmx fxsr sse sse2 ht syscall nx mmxext fxsr_opt pdpe1gb rdtscp lm "
    "constant_tsc rep_good nopl nonstop_tsc cpuid extd_apicid aperfmperf rapl pni pclmulqdq monitor ssse3 fma cx16 sse4_1 sse4_2 movbe popcnt aes xsave avx "
    "f16c rdrand lahf_lm cmp_legacy svm extapic cr8_legacy abm sse4a misalignsse 3dnowprefetch osvw ibs skinit wdt tce topoext perfctr_core perfctr_nb bpext "
    "perfctr_llc mwaitx cpb cat_l3 cdp_l3 hw_pstate ssbd mba ibrs ibpb stibp vmmcall fsgsbase bmi1 avx2 smep bmi2 cqm rdt_a rdseed adx smap clflushopt clwb "
    "sha_ni xsaveopt xsavec xgetbv1 xsaves cqm_llc cqm_occup_llc cqm_mbm_total cqm_mbm_local clzero irperf xsaveerptr rdpru wbnoinvd cppc arat npt lbrv "
    "svm_lock nrip_save tsc_scale vmcb_clean flushbyasid decodeassists pausefilter pfthreshold avic v_vmsave_vmload vgif umip rdpid overflow_recov succor smca")
FLAGS_ATHLON_II = (
    "fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush mmx fxsr sse sse2 ht syscall nx mmxext fxsr_opt pdpe1gb rdtscp lm "
    "3dnowext 3dnow constant_tsc rep_good nopl nonstop_tsc cpuid extd_apicid pni monitor cx16 popcnt lahf_lm cmp_legacy svm extapic cr8_legacy abm sse4a "
    "misalignsse 3dnowprefetch osvw ibs skinit wdt hw_pstate vmmcall npt lbrv svm_lock nrip_save")
FLAGS_QEMU_SKYLAKE = (
    "fpu de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush mmx fxsr sse sse2 ss syscall nx pdpe1gb rdtscp lm constant_tsc rep_good "
    "nopl xtopology cpuid tsc_known_freq pni pclmulqdq ssse3 fma cx16 pcid sse4_1 sse4_2 x2apic movbe popcnt tsc_deadline_timer aes xsave avx f16c rdrand "
    "hypervisor lahf_lm abm 3dnowprefetch cpuid_fault pti ibrs ibpb stibp fsgsbase tsc_adjust bmi1 hle avx2 smep bmi2 erms invpcid rtm mpx rdseed adx smap "
    "clflushopt xsaveopt xsavec xgetbv1 xsaves arat md_clear arch_capabilities")


# --- binary helpers -------------------------------------------------------------------------------

def make_edid(width: int, height: int, mm_w: int, mm_h: int, name: str, manufacturer: str = "LEN") -> bytes:
    """A valid 128-byte EDID base block with one preferred detailed timing and a monitor name."""
    data = bytearray(128)
    data[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    code = ((ord(manufacturer[0]) - 64) << 10) | ((ord(manufacturer[1]) - 64) << 5) | (ord(manufacturer[2]) - 64)
    data[8:10] = struct.pack(">H", code)
    data[10:12] = struct.pack("<H", 0x4321)
    data[16], data[17], data[18], data[19] = 12, 32, 1, 4
    data[20] = 0xA5
    data[21], data[22] = min(255, round(mm_w / 10)), min(255, round(mm_h / 10))
    h_blank, v_blank = 160, 62
    data[54:56] = struct.pack("<H", 59000 if width > 2500 else 14850)
    data[56], data[57] = width & 0xFF, h_blank & 0xFF
    data[58] = ((width >> 8) << 4) | (h_blank >> 8)
    data[59], data[60] = height & 0xFF, v_blank & 0xFF
    data[61] = ((height >> 8) << 4) | (v_blank >> 8)
    data[62], data[63], data[64], data[65] = 48, 32, 0x33, 0
    data[66], data[67] = mm_w & 0xFF, mm_h & 0xFF
    data[68] = ((mm_w >> 8) << 4) | (mm_h >> 8)
    data[71] = 0x1A
    data[72:75] = b"\x00\x00\x00"
    data[75] = 0xFC
    data[77:90] = (name[:13] + "\n").ljust(13).encode()
    for offset in (90, 108):
        data[offset:offset + 3] = b"\x00\x00\x00"
        data[offset + 3] = 0x10
    data[127] = (-sum(data[:127])) & 0xFF
    return bytes(data)


@dataclass
class Builder:
    """Writes one machine in the probe's fixture layout."""

    name: str
    dest: Path = field(init=False)
    writer: Any = field(init=False)
    _pci: list[tuple[str, str]] = field(default_factory=list)  # (vendor, device) for the trimmed pci.ids
    _cmdline: str = "BOOT_IMAGE=/boot/gentoo root=live:CDLABEL=GENTOO_INSTALLER_AMD64 rd.live.dir=/ rd.live.squashimg=image.squashfs cdroot nodhcp secureconsole"
    _mounts: list[str] = field(default_factory=list)
    _input: list[str] = field(default_factory=list)
    _efi_label: list[str] = field(default_factory=list)
    _facts: dict[str, Any] = field(default_factory=dict)
    _meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.dest = FIXTURES / self.name
        if self.dest.exists():
            shutil.rmtree(self.dest)
        self.dest.mkdir(parents=True)
        self.writer = FixtureWriter(self.dest)

    # -- generic -------------------------------------------------------------------------------

    def file(self, path: str, content: str | bytes) -> None:
        self.writer.write_file(path, content)

    def link(self, path: str, target: str) -> None:
        self.writer.link(path, target)

    def command(self, key: str, output: str) -> None:
        self.writer.command(key, output)

    # -- identity ------------------------------------------------------------------------------

    def dmi(self, *, sys_vendor: str, product_name: str, product_version: str = "", bios_vendor: str, bios_version: str, bios_date: str,
            board_vendor: str = "", board_name: str = "", chassis_type: int) -> None:
        base = "/sys/class/dmi/id/"
        for key, value in {"sys_vendor": sys_vendor, "product_name": product_name, "product_version": product_version, "bios_vendor": bios_vendor,
                           "bios_version": bios_version, "bios_date": bios_date, "board_vendor": board_vendor, "board_name": board_name,
                           "chassis_type": str(chassis_type)}.items():
            self.file(base + key, value + "\n")

    def cpu(self, *, vendor_id: str, model_name: str, family: int, model: int, stepping: int, flags: str, threads: int, cores: int,
            hypervisor_vendor: str = "") -> None:
        first = (f"processor\t: 0\nvendor_id\t: {vendor_id}\ncpu family\t: {family}\nmodel\t\t: {model}\nmodel name\t: {model_name}\n"
                 f"stepping\t: {stepping}\nmicrocode\t: 0x1\ncpu MHz\t\t: 2400.000\ncache size\t: 8192 KB\nsiblings\t: {threads}\ncpu cores\t: {cores}\n"
                 f"flags\t\t: {flags}\n")
        stubs = "".join(f"\nprocessor\t: {index}\n" for index in range(1, threads))
        self.file("/proc/cpuinfo", first + stubs)
        self.file("/proc/cmdline", self._cmdline + "\n")
        output = hardware.data.cpuinfo_to_cpu_flags(flags.split())
        self.command("cpuid2cpuflags.txt", "*/* CPU_FLAGS_X86: " + " ".join(output) + "\n")
        lscpu = f"Architecture:                            x86_64\nModel name:                              {model_name}\n"
        if hypervisor_vendor:
            lscpu += f"Hypervisor vendor:                       {hypervisor_vendor}\nVirtualization type:                     full\n"
        self.command("lscpu.txt", lscpu)

    def memory(self, total_kib: int, available_kib: int, swap_kib: int = 0) -> None:
        self.file("/proc/meminfo", f"MemTotal:       {total_kib} kB\nMemFree:        {available_kib // 2} kB\nMemAvailable:   {available_kib} kB\nSwapTotal:      {swap_kib} kB\n")

    # -- firmware --------------------------------------------------------------------------------

    def efi(self, *, bits: int = 64, secure_boot: bool | None = None, setup_mode: bool = False, can_reboot_to_fw: bool = True, mok: bool = False,
            windows: bool = False, lockdown: str = "none", writable: bool = True, boot_entries: tuple[str, ...] = ("Gentoo",)) -> None:
        efi = "/sys/firmware/efi"
        self.file(efi + "/fw_platform_size", f"{bits}\n")
        guid = hardware.EFI_GLOBAL_GUID
        attr = b"\x07\x00\x00\x00"
        if secure_boot is not None:
            self.file(f"{efi}/efivars/SecureBoot-{guid}", attr + bytes([1 if secure_boot else 0]))
            self.file(f"{efi}/efivars/SetupMode-{guid}", attr + bytes([1 if setup_mode else 0]))
        self.file(f"{efi}/efivars/OsIndicationsSupported-{guid}", attr + (b"\x01" if can_reboot_to_fw else b"\x00") + b"\x00" * 7)
        if mok:
            self.file(f"{efi}/efivars/MokListRT-{hardware.MOK_GUID}", attr + b"\x00")
        options = {"none": "[none] integrity confidentiality", "integrity": "none [integrity] confidentiality"}.get(lockdown, "none integrity confidentiality")
        self.file("/sys/kernel/security/lockdown", options + "\n")
        self.file("/proc/mounts", f"efivarfs /sys/firmware/efi/efivars efivarfs {'rw' if writable else 'ro'},nosuid,nodev,noexec,relatime 0 0\n")
        entries = (["Windows Boot Manager"] if windows else []) + list(boot_entries)
        self.command("efibootmgr.txt", "".join(f"Boot{index:04X}* {label}\n" for index, label in enumerate(entries)))

    def bios_only(self) -> None:
        self.file("/proc/mounts", "")

    # -- PCI and graphics ------------------------------------------------------------------------

    def pci(self, slot: str, class_code: str, vendor: str, device: str, driver: str = "", *, boot_vga: bool = False, runtime: str = "active",
            parent: str = "", subsystem: tuple[str, str] | None = None) -> None:
        """``subsystem`` is the (vendor, device) pair the board maker burned in: sysfs shows it as subsystem_vendor and subsystem_device."""
        base = f"/sys/bus/pci/devices/{slot}"
        self.file(base + "/class", f"0x{class_code}\n")
        self.file(base + "/vendor", f"0x{vendor}\n")
        self.file(base + "/device", f"0x{device}\n")
        if subsystem:
            self.file(base + "/subsystem_vendor", f"0x{subsystem[0]}\n")
            self.file(base + "/subsystem_device", f"0x{subsystem[1]}\n")
        if class_code.startswith("03"):
            self.file(base + "/boot_vga", "1\n" if boot_vga else "0\n")
        self.file(base + "/power/runtime_status", runtime + "\n")
        self.link(base, f"../../../devices/pci0000:00/{parent + '/' if parent else ''}{slot}")
        if driver:
            self.link(base + "/driver", f"../../../bus/pci/drivers/{driver}")
        self._pci.append((vendor, device))

    def drm(self, card: str, slot: str, connectors: dict[str, tuple[str, bytes | None, str]], *, parent: str = "", virtio: str = "") -> None:
        """connectors: name -> (status, edid, modes)."""
        pci_path = f"{parent + '/' if parent else ''}{slot}"
        if virtio:
            self.link(f"/sys/class/drm/{card}", f"../../devices/pci0000:00/{pci_path}/{virtio}/drm/{card}")
            self.link(f"/sys/class/drm/{card}/device", f"../../../{virtio}")
        else:
            self.link(f"/sys/class/drm/{card}", f"../../devices/pci0000:00/{pci_path}/drm/{card}")
            self.link(f"/sys/class/drm/{card}/device", f"../../../{slot}")
        for connector, (status, edid, modes) in connectors.items():
            base = f"/sys/class/drm/{card}-{connector}"
            self.link(base, f"../../devices/pci0000:00/{pci_path}/drm/{card}/{card}-{connector}")
            self.file(base + "/status", status + "\n")
            if modes:
                self.file(base + "/modes", modes)
            if edid:
                self.file(base + "/edid", edid)

    # -- storage ---------------------------------------------------------------------------------

    def block(self, name: str, link: str, size_bytes: int, *, rota: bool, model: str = "", vendor: str = "", removable: bool = False,
              ro: bool = False, log_sec: int = 512, phy_sec: int = 512, dev: str = "8:0", by_path: str = "") -> None:
        base = f"/sys/block/{name}"
        self.link(base, link)
        self.file(base + "/size", f"{size_bytes // 512}\n")
        self.file(base + "/removable", "1\n" if removable else "0\n")
        self.file(base + "/ro", "1\n" if ro else "0\n")
        self.file(base + "/dev", dev + "\n")
        self.file(base + "/queue/rotational", "1\n" if rota else "0\n")
        self.file(base + "/queue/logical_block_size", f"{log_sec}\n")
        self.file(base + "/queue/physical_block_size", f"{phy_sec}\n")
        if model:
            self.file(base + "/device/model", model.ljust(16) + "\n")
        if vendor:
            self.file(base + "/device/vendor", vendor.ljust(8) + "\n")
        if by_path:
            self.link(f"/dev/disk/by-path/{by_path}", f"../../{name}")

    def lsblk(self, devices: list[dict[str, Any]]) -> None:
        self.command("lsblk.json", json.dumps({"blockdevices": devices}, indent=2) + "\n")

    def live_mount(self, device: str) -> None:
        self._mounts.append(f"{device} /run/initramfs/live iso9660 ro,relatime 0 0")

    # -- network ---------------------------------------------------------------------------------

    def net(self, iface: str, *, link: str, driver: str, subsystem: str, device_link: str, wireless: bool = False, carrier: bool | None = True,
            vendor: str = "", device: str = "") -> None:
        base = f"/sys/class/net/{iface}"
        self.link(base, link)
        self.link(base + "/device", device_link)
        self.link(base + "/device/driver", driver_link(subsystem, driver))
        self.link(base + "/device/subsystem", f"../../../bus/{subsystem}" if subsystem == "pci" else f"../../../../bus/{subsystem}")
        if vendor:
            self.file(base + "/device/vendor", f"0x{vendor}\n")
            self.file(base + "/device/device", f"0x{device}\n")
        if wireless:
            self.link(base + "/phy80211", "../../ieee80211/phy0")
            self.file(base + "/wireless/status", "0x0000\n")
        if carrier is not None:
            self.file(base + "/carrier", "1\n" if carrier else "0\n")

    def route(self, iface: str | None) -> None:
        header = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        if iface:
            self.file("/proc/net/route", header + f"{iface}\t00000000\t0101FEA9\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
                      f"{iface}\t0000FEA9\t00000000\t0001\t0\t0\t100\t0000FFFF\t0\t0\t0\n")
        else:
            self.file("/proc/net/route", header)

    # -- power, input, audio, usb, clock -----------------------------------------------------------

    def supply(self, name: str, kind: str, *, scope: str = "", status: str = "", capacity: int | None = None, online: bool | None = None) -> None:
        base = f"/sys/class/power_supply/{name}"
        self.file(base + "/type", kind + "\n")
        if scope:
            self.file(base + "/scope", scope + "\n")
        if status:
            self.file(base + "/status", status + "\n")
        if capacity is not None:
            self.file(base + "/capacity", f"{capacity}\n")
            self.file(base + "/present", "1\n")
        if online is not None:
            self.file(base + "/online", "1\n" if online else "0\n")

    def backlight(self, name: str = "intel_backlight") -> None:
        self.file(f"/sys/class/backlight/{name}/brightness", "100\n")

    def input_device(self, name: str, handlers: str, *, bus: str = "0011", vendor: str = "0001", product: str = "0001") -> None:
        self._input.append(f"I: Bus={bus} Vendor={vendor} Product={product} Version=ab41\nN: Name=\"{name}\"\nH: Handlers={handlers}\nB: PROP=0\nB: EV=120013\n")

    def sound_card(self, index: int, driver: str, subsystem: str = "platform") -> None:
        base = f"/sys/class/sound/card{index}"
        self.link(base, f"../../devices/pci0000:00/0000:00:1f.3/sound/card{index}")
        self.link(base + "/device", f"../../../{driver}.{index}")
        self.link(base + "/device/driver", driver_link(subsystem, driver))

    def usb(self, name: str, vendor: str, product: str, device_class: str = "00", interfaces: tuple[str, ...] = (), *, manufacturer: str = "",
            product_name: str = "", label: str = "") -> None:
        """A USB device. The ``manufacturer`` and ``product_name`` strings are what sysfs shows (``label`` is the older name of
        ``product_name``); the ``serial`` file is never written."""
        base = f"/sys/bus/usb/devices/{name}"
        self.file(base + "/idVendor", vendor + "\n")
        self.file(base + "/idProduct", product + "\n")
        if manufacturer:
            self.file(base + "/manufacturer", manufacturer + "\n")
        if product_name or label:
            self.file(base + "/product", (product_name or label) + "\n")
        self.file(base + "/bDeviceClass", device_class + "\n")
        for index, klass in enumerate(interfaces):
            self.file(f"{base}/{name}:1.{index}/bInterfaceClass", klass + "\n")

    # -- platform drivers and laptop sysfs -------------------------------------------------------------

    def platform_driver(self, name: str) -> None:
        self.file(f"/sys/bus/platform/drivers/{name}/bind", "")

    def platform_device(self, name: str, driver: str = "", attributes: dict[str, str] | None = None) -> None:
        """/sys/devices/platform/NAME with the driver that bound it and the attribute files that driver exposes (``asus-nb-wmi``: gpu_mux_mode, ...)."""
        base = f"/sys/devices/platform/{name}"
        self.link(f"/sys/bus/platform/devices/{name}", f"../../../devices/platform/{name}")
        self.writer.mkdir(base)
        if driver:
            self.link(base + "/driver", f"../../../bus/platform/drivers/{driver}")
        for attribute, value in (attributes or {}).items():
            self.file(f"{base}/{attribute}", value + "\n")

    def platform_profile(self, current: str = "balanced", choices: tuple[str, ...] = ("low-power", "balanced", "performance")) -> None:
        """/sys/firmware/acpi/platform_profile{,_choices}: the firmware's performance profiles (what power-profiles-daemon switches)."""
        self.file("/sys/firmware/acpi/platform_profile", current + "\n")
        self.file("/sys/firmware/acpi/platform_profile_choices", " ".join(choices) + "\n")

    def led(self, name: str) -> None:
        """A LED with only its brightness file (the short form of leds())."""
        self.file(f"/sys/class/leds/{name}/brightness", "0\n")

    def leds(self, name: str, *, brightness: int = 0, maximum: int = 3) -> None:
        """/sys/class/leds/NAME, for example ``asus::kbd_backlight`` or ``platform::micmute``."""
        self.file(f"/sys/class/leds/{name}/brightness", f"{brightness}\n")
        self.file(f"/sys/class/leds/{name}/max_brightness", f"{maximum}\n")

    def sleep(self, mem_sleep: str = "[s2idle] deep", pm_profile: int = 2) -> None:
        self.file("/sys/power/mem_sleep", mem_sleep + "\n")
        self.file("/sys/firmware/acpi/pm_profile", f"{pm_profile}\n")

    def charge_threshold(self, battery: str, end: int, start: int | None = None) -> None:
        """charge_control_end_threshold (and _start_) of a battery: the kernel's battery charge limit."""
        self.file(f"/sys/class/power_supply/{battery}/charge_control_end_threshold", f"{end}\n")
        if start is not None:
            self.file(f"/sys/class/power_supply/{battery}/charge_control_start_threshold", f"{start}\n")

    def charge_limit(self, battery: str, end: int = 100) -> None:
        self.charge_threshold(battery, end)

    def iio(self, index: int, name: str) -> None:
        self.file(f"/sys/bus/iio/devices/iio:device{index}/name", name + "\n")

    def firmware_attributes(self, name: str, attributes: dict[str, str]) -> None:
        """/sys/class/firmware-attributes/NAME/attributes/ATTR/current_value (``asus-armoury``, ``lenovo-wmi-other``, ``dell-wmi-sysman``)."""
        for attribute, value in attributes.items():
            self.file(f"/sys/class/firmware-attributes/{name}/attributes/{attribute}/current_value", value + "\n")

    def note(self, text: str, *, unverified: tuple[str, ...] = ()) -> None:
        """Provenance for .capture.json: these machines are invented, and ``unverified`` names what was not checked against a real device."""
        self._meta["note"] = text
        if unverified:
            self._meta["unverified"] = list(unverified)

    def tpm(self, major: str = "2") -> None:
        self.file("/sys/class/tpm/tpm0/tpm_version_major", major + "\n")

    def bluetooth(self) -> None:
        self.link("/sys/class/bluetooth/hci0", "../../devices/pci0000:00/0000:00:14.0/usb1/1-10/1-10:1.0/bluetooth/hci0")

    def rtc(self, epoch: int) -> None:
        import time as _time
        moment = _time.gmtime(epoch)
        self.file("/sys/class/rtc/rtc0/date", _time.strftime("%Y-%m-%d", moment) + "\n")
        self.file("/sys/class/rtc/rtc0/time", _time.strftime("%H:%M:%S", moment) + "\n")

    def chrony(self, offset: float = 0.0003, synced: bool = True) -> None:
        if synced:
            self.command("chronyc-tracking.txt", f"7F7F0101,127.127.1.1,3,{NOW}.0,{offset},-0.0001,0.0003,-25.8,-0.03,0.4,0.02,0.005,1038.4,Normal\n")
        else:
            self.command("chronyc-tracking.txt", "00000000,,0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,Not synchronised\n")

    # -- finish ------------------------------------------------------------------------------------

    def finish(self, summary_hint: str = "") -> None:
        if self._input:
            self.file("/proc/bus/input/devices", "\n".join(self._input))
        else:
            self.file("/proc/bus/input/devices", "")
        if self._mounts:
            existing = (self.dest / "proc/mounts").read_text() if (self.dest / "proc/mounts").exists() else ""
            self.file("/proc/mounts", existing + "\n".join(self._mounts) + "\n")
            self.file("/proc/self/mountinfo", "\n".join(f"{i + 30} 20 8:{i} / {line.split()[1]} ro - {line.split()[2]} {line.split()[0]} ro" for i, line in enumerate(self._mounts)) + "\n")
        # trimmed pci.ids from the building machine's database
        ids = Path("/usr/share/hwdata/pci.ids")
        vendors, devices = hardware.parse_pci_ids(ids.read_text(errors="replace"), self._pci)
        lines = []
        for vendor in sorted(vendors):
            lines.append(f"{vendor}  {vendors[vendor]}")
            lines.extend(f"\t{dev}  {label}" for (v, dev), label in sorted(devices.items()) if v == vendor)
        self.file("/usr/share/hwdata/pci.ids", "\n".join(lines) + "\n")
        self.writer.finish(None)
        profile = hardware.probe(self.dest, now=NOW)
        meta = {"tool": "synthetic", "tool_version": 1, "schema": hardware.SCHEMA, "now": NOW, "redacted": True, "synthetic": True,
                "summary": profile.summary(), **self._meta}
        (self.dest / ".capture.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")


def driver_link(subsystem: str, driver: str) -> str:
    return f"../../../bus/{subsystem}/drivers/{driver}" if subsystem == "pci" else f"../../../../bus/{subsystem}/drivers/{driver}"


def lsblk_disk(name: str, size: int, *, tran: str, model: str, serial: str = "", wwn: str = "", maj_min: str = "8:0", hotplug: bool = False,
               removable: bool = False, log_sec: int = 512, phy_sec: int = 512, pttype: str = "gpt", children: list[dict[str, Any]] | None = None,
               ptuuid: str = "") -> dict[str, Any]:
    return {"name": name, "kname": name, "path": f"/dev/{name}", "maj:min": maj_min, "type": "disk", "size": size, "ro": False,
            "rm": removable, "hotplug": hotplug, "tran": tran, "model": model, "serial": serial or None, "wwn": wwn or None,
            "fstype": None, "fsver": None, "label": None, "uuid": None, "parttype": None, "partlabel": None, "partuuid": None,
            "pttype": pttype if children else None, "ptuuid": ptuuid or ("00000000-0000-0000-0000-000000000000" if children else None),
            "log-sec": log_sec, "phy-sec": phy_sec, "mountpoints": [None], "children": children or []}


def lsblk_part(name: str, size: int, *, fstype: str | None, parttype: str = "", label: str = "", maj_min: str = "8:1", mountpoints: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "kname": name, "path": f"/dev/{name}", "maj:min": maj_min, "type": "part", "size": size, "ro": False, "rm": False,
            "hotplug": False, "tran": None, "model": None, "serial": None, "wwn": None, "fstype": fstype, "fsver": None, "label": label or None,
            "uuid": "00000000-0000-0000-0000-000000000000" if fstype else None, "parttype": parttype or None, "partlabel": None,
            "partuuid": "00000000-0000-0000-0000-000000000000", "pttype": "gpt", "ptuuid": None, "log-sec": 512, "phy-sec": 512,
            "mountpoints": mountpoints or [None]}


GIB = 1024 ** 3
ESP_TYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
LINUX_TYPE = "0fc63daf-8483-4772-8e79-3d69d8477de1"
MS_BASIC_TYPE = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
LIVE_LABEL = "GENTOO_INSTALLER_AMD64"


def live_usb(b: Builder, *, bus_slot: str, name: str = "sdb", dev: str = "8:16") -> dict[str, Any]:
    """The installer's own USB stick: present on every machine that boots from it."""
    b.block(name, f"../devices/pci0000:00/{bus_slot}/usb1/1-3/1-3:1.0/host6/target6:0:0/6:0:0:0/block/{name}", 16 * GIB, rota=False,
            model="USB DISK 3.0", vendor="Generic", removable=True, dev=dev, by_path=f"pci-{bus_slot}-usb-0:3:1.0-scsi-0:0:0:0")
    b.live_mount(f"/dev/{name}1")
    return lsblk_disk(name, 16 * GIB, tran="usb", model="USB DISK 3.0", maj_min=dev, hotplug=True, removable=True,
                      children=[lsblk_part(f"{name}1", 3 * GIB, fstype="iso9660", label=LIVE_LABEL, maj_min=dev.split(":")[0] + ":" + str(int(dev.split(":")[1]) + 1)),
                                lsblk_part(f"{name}2", 4 * 1024 ** 2, fstype="vfat", maj_min=dev.split(":")[0] + ":" + str(int(dev.split(":")[1]) + 2))])


# --- the machines -----------------------------------------------------------------------------------

def qemu_q35_virtio() -> None:
    b = Builder("qemu-q35-virtio")
    b.dmi(sys_vendor="QEMU", product_name="Standard PC (Q35 + ICH9, 2009)", product_version="pc-q35-9.2", bios_vendor="EDK II",
          bios_version="edk2-stable202408-prebuilt.qemu.org", bios_date="08/13/2024", board_vendor="QEMU", board_name="Standard PC (Q35 + ICH9, 2009)", chassis_type=1)
    b.cpu(vendor_id="GenuineIntel", model_name="Intel Core Processor (Skylake, IBRS)", family=6, model=94, stepping=3, flags=FLAGS_QEMU_SKYLAKE,
          threads=4, cores=4, hypervisor_vendor="KVM")
    b.memory(4 * 1024 ** 2 - 90_000, 3_400_000)
    b.efi(secure_boot=False, setup_mode=True, lockdown="none", boot_entries=("UEFI QEMU DVD-ROM QM00001",))
    b.pci("0000:00:00.0", "060000", "8086", "29c0")
    b.pci("0000:00:01.0", "030000", "1af4", "1050", "virtio-pci", boot_vga=True)
    b.pci("0000:00:02.0", "020000", "1af4", "1041", "virtio-pci")
    b.pci("0000:00:03.0", "010000", "1af4", "1042", "virtio-pci")
    b.pci("0000:00:1f.2", "010601", "8086", "2922", "ahci")
    b.drm("card0", "0000:00:01.0", {"Virtual-1": ("connected", make_edid(1280, 800, 340, 210, "QEMU Monitor", "RHT"), "1280x800\n1024x768\n")}, virtio="virtio0")
    live = live_usb(b, bus_slot="0000:00:04.0")  # an installer stick attached as USB mass storage
    b.block("vda", "../devices/pci0000:00/0000:00:03.0/virtio1/block/vda", 40 * GIB, rota=True, dev="254:0", by_path="pci-0000:00:03.0")
    b.link("/dev/disk/by-path/virtio-pci-0000:00:03.0", "../../vda")
    b.lsblk([lsblk_disk("vda", 40 * GIB, tran="", model="", maj_min="254:0"), live])
    b.net("ens3", link="../../devices/pci0000:00/0000:00:02.0/virtio0/net/ens3", driver="virtio_net", subsystem="virtio",
          device_link="../../../virtio0")
    b.route("ens3")
    b.input_device("AT Translated Set 2 keyboard", "sysrq kbd leds event0")
    b.input_device("QEMU QEMU USB Tablet", "mouse0 event1", bus="0003", vendor="0627", product="0001")
    b.rtc(NOW)
    b.finish()


def old_bios_sata_radeon() -> None:
    b = Builder("old-bios-sata-radeon")
    b.dmi(sys_vendor="Dell Inc.", product_name="OptiPlex 580", bios_vendor="Dell Inc.", bios_version="A12", bios_date="03/09/2011",
          board_vendor="Dell Inc.", board_name="0M3F3R", chassis_type=6)
    b.cpu(vendor_id="AuthenticAMD", model_name="AMD Athlon(tm) II X2 250 Processor", family=16, model=6, stepping=3, flags=FLAGS_ATHLON_II, threads=2, cores=2)
    b.memory(4_000_000, 3_300_000, 0)
    b.bios_only()
    b.pci("0000:00:11.0", "010601", "1002", "4391", "ahci")
    b.pci("0000:01:00.0", "030000", "1002", "68f9", "radeon", boot_vga=True, parent="0000:00:02.0")
    b.pci("0000:01:00.1", "040300", "1002", "aa68", "snd_hda_intel", parent="0000:00:02.0")
    b.pci("0000:02:00.0", "020000", "10ec", "8168", "r8169", parent="0000:00:06.0")
    b.drm("card0", "0000:01:00.0", {"DVI-I-1": ("connected", make_edid(1680, 1050, 474, 296, "DELL E228WFP", "DEL"), "1680x1050\n1280x1024\n"),
                                    "VGA-1": ("disconnected", None, "")}, parent="0000:00:02.0")
    b.block("sda", "../devices/pci0000:00/0000:00:11.0/ata1/host0/target0:0:0/0:0:0:0/block/sda", 500 * 10**9, rota=True, model="WDC WD5000AAKX-0",
            vendor="ATA", phy_sec=4096, by_path="pci-0000:00:11.0-ata-1")
    live = live_usb(b, bus_slot="0000:00:12.0")
    b.lsblk([lsblk_disk("sda", 500 * 10**9, tran="sata", model="WDC WD5000AAKX-0", serial="WD-XXXXXXXXXXXX", wwn="0x50014ee000000000", phy_sec=4096,
                        children=[lsblk_part("sda1", 100 * 10**9, fstype="ntfs", parttype=MS_BASIC_TYPE, label="Windows")]), live])
    b.net("enp2s0", link="../../devices/pci0000:00/0000:00:06.0/0000:02:00.0/net/enp2s0", driver="r8169", subsystem="pci",
          device_link="../../../0000:02:00.0", vendor="10ec", device="8168")
    b.route("enp2s0")
    b.input_device("AT Translated Set 2 keyboard", "sysrq kbd leds event0")
    b.sound_card(0, "snd_hda_intel", "pci")
    b.rtc(NOW)
    b.finish()


def thinkpad_intel_nvidia_hybrid() -> None:
    b = Builder("thinkpad-intel-nvidia-hybrid")
    b.dmi(sys_vendor="LENOVO", product_name="20Y3S0FB00", product_version="ThinkPad P1 Gen 4i", bios_vendor="LENOVO", bios_version="N40ET36W (1.36 )",
          bios_date="04/12/2023", board_vendor="LENOVO", board_name="20Y3S0FB00", chassis_type=10)
    b.cpu(vendor_id="GenuineIntel", model_name="11th Gen Intel(R) Core(TM) i7-11800H @ 2.30GHz", family=6, model=141, stepping=1,
          flags=FLAGS_TIGERLAKE_H, threads=16, cores=8)
    b.memory(32_000_000 - 1_000_000, 27_500_000, 0)
    b.efi(secure_boot=False, setup_mode=False, mok=False, windows=True, boot_entries=("ubuntu",))
    b.pci("0000:00:02.0", "030000", "8086", "9a60", "i915", boot_vga=True)
    b.pci("0000:01:00.0", "030200", "10de", "24b6", "nouveau", runtime="suspended", parent="0000:00:01.0")
    b.pci("0000:00:1f.3", "040100", "8086", "43c8", "sof-audio-pci-intel-tgl")
    b.pci("0000:00:14.3", "028000", "8086", "43f0", "iwlwifi")
    b.pci("0000:00:1d.0", "010802", "144d", "a80a", "nvme", parent="")
    b.drm("card0", "0000:00:02.0", {"eDP-1": ("connected", make_edid(3840, 2400, 344, 215, "LEN4123", "LEN"), "3840x2400\n1920x1200\n"),
                                    "DP-1": ("disconnected", None, "")})
    b.drm("card1", "0000:01:00.0", {"DP-1": ("connected", make_edid(1920, 1080, 600, 340, "Should not be read", "DEL"), "1920x1080\n")}, parent="0000:00:01.0")
    b.block("nvme0n1", "../devices/pci0000:00/0000:00:1d.0/nvme/nvme0/nvme0n1", 1_000_204_886_016, rota=False, model="SAMSUNG MZVL21T0HCLR-00BL7", dev="259:0",
            by_path="pci-0000:00:1d.0-nvme-1")
    live = live_usb(b, bus_slot="0000:00:14.0")
    b.lsblk([lsblk_disk("nvme0n1", 1_000_204_886_016, tran="nvme", model="SAMSUNG MZVL21T0HCLR-00BL7", serial="S64XXXXXXXXXXXX", maj_min="259:0",
                        children=[lsblk_part("nvme0n1p1", 500 * 1024 ** 2, fstype="vfat", parttype=ESP_TYPE, maj_min="259:1"),
                                  lsblk_part("nvme0n1p3", 400 * GIB, fstype="ntfs", parttype=MS_BASIC_TYPE, maj_min="259:3")]), live])
    b.net("wlp0s20f3", link="../../devices/pci0000:00/0000:00:14.3/net/wlp0s20f3", driver="iwlwifi", subsystem="pci",
          device_link="../../../0000:00:14.3", wireless=True, vendor="8086", device="43f0", carrier=False)
    b.route(None)
    b.supply("BAT0", "Battery", scope="System", status="Discharging", capacity=82)
    b.supply("AC", "Mains", online=False)
    b.backlight()
    b.input_device("AT Translated Set 2 keyboard", "sysrq kbd leds event0")
    b.input_device("SYNA8016:00 06CB:CE2D Touchpad", "mouse0 event5", bus="0018", vendor="06cb", product="ce2d")
    b.input_device("Lid Switch", "event1")
    b.sound_card(0, "sof_sdw", "platform")
    b.usb("1-3", "0951", "1666", "00", ("08",))
    b.usb("1-7", "06cb", "00bd", "ff", ("ff",))
    b.usb("1-9", "04f2", "b6ea", "ef", ("0e", "0e"))
    b.usb("1-10", "8087", "0033", "e0", ("e0", "e0"))
    b.bluetooth()
    b.tpm()
    b.rtc(NOW + 7 * 3600)  # Windows keeps local time (UTC+7) in the hardware clock
    b.chrony()
    b.finish()


def amd_laptop_sof() -> None:
    b = Builder("amd-laptop-sof")
    b.dmi(sys_vendor="LENOVO", product_name="82MF", product_version="IdeaPad 5 14ALC05", bios_vendor="LENOVO", bios_version="GXCN32WW", bios_date="06/21/2023",
          board_vendor="LENOVO", board_name="LNVNB161216", chassis_type=10)
    b.cpu(vendor_id="AuthenticAMD", model_name="AMD Ryzen 5 5500U with Radeon Graphics", family=23, model=104, stepping=1, flags=FLAGS_ZEN2_MOBILE,
          threads=12, cores=6)
    b.memory(16_000_000 - 512_000, 13_900_000, 0)
    b.efi(secure_boot=True, setup_mode=False, mok=False, boot_entries=("Windows Boot Manager",), windows=False)
    b.pci("0000:05:00.0", "030000", "1002", "164c", "amdgpu", boot_vga=True, parent="0000:00:08.1")
    b.pci("0000:05:00.5", "048000", "1022", "15e2", "snd_sof_amd_renoir", parent="0000:00:08.1")
    b.pci("0000:05:00.6", "040300", "1022", "15e3", "snd_hda_intel", parent="0000:00:08.1")
    b.pci("0000:01:00.0", "010802", "1344", "5407", "nvme", parent="0000:00:02.1")
    b.pci("0000:02:00.0", "028000", "14c3", "7961", "mt7921e", parent="0000:00:02.2")
    b.pci("0000:03:00.0", "020000", "10ec", "8168", "r8169", parent="0000:00:02.3")
    b.drm("card0", "0000:05:00.0", {"eDP-1": ("connected", make_edid(1920, 1080, 309, 174, "LEN40B2", "LEN"), "1920x1080\n1280x720\n")}, parent="0000:00:08.1")
    b.block("nvme0n1", "../devices/pci0000:00/0000:00:02.1/0000:01:00.0/nvme/nvme0/nvme0n1", 512_110_190_592, rota=False, model="Micron 2210 MTFDHBA512QFD",
            dev="259:0", log_sec=512, phy_sec=512, by_path="pci-0000:01:00.0-nvme-1")
    live = live_usb(b, bus_slot="0000:00:08.1")
    b.lsblk([lsblk_disk("nvme0n1", 512_110_190_592, tran="nvme", model="Micron 2210 MTFDHBA512QFD", serial="2236XXXXXXXX", maj_min="259:0",
                        children=[lsblk_part("nvme0n1p1", 260 * 1024 ** 2, fstype="vfat", parttype=ESP_TYPE, maj_min="259:1"),
                                  lsblk_part("nvme0n1p2", 450 * GIB, fstype="ntfs", parttype=MS_BASIC_TYPE, maj_min="259:2")]), live])
    b.net("wlp2s0", link="../../devices/pci0000:00/0000:00:02.2/0000:02:00.0/net/wlp2s0", driver="mt7921e", subsystem="pci",
          device_link="../../../0000:02:00.0", wireless=True, vendor="14c3", device="7961", carrier=False)
    b.net("enp3s0", link="../../devices/pci0000:00/0000:00:02.3/0000:03:00.0/net/enp3s0", driver="r8169", subsystem="pci",
          device_link="../../../0000:03:00.0", vendor="10ec", device="8168", carrier=False)
    b.route(None)
    b.supply("BAT1", "Battery", scope="System", status="Discharging", capacity=55)
    b.supply("ACAD", "Mains", online=False)
    b.backlight("amdgpu_bl0")
    b.input_device("AT Translated Set 2 keyboard", "sysrq kbd leds event0")
    b.input_device("ELAN06FA:00 04F3:3140 Touchpad", "mouse0 event6", bus="0018", vendor="04f3", product="3140")
    b.input_device("Lid Switch", "event1")
    b.sound_card(0, "acp_pdm_mach", "platform")
    b.usb("1-3", "0951", "1666", "00", ("08",))
    b.usb("1-4", "0e8d", "0608", "ef", ("e0", "e0"))
    b.usb("1-5", "5986", "9102", "ef", ("0e", "0e"))
    b.bluetooth()
    b.tpm()
    b.rtc(NOW)
    b.chrony(synced=False)
    b.finish()


def vmware_guest() -> None:
    b = Builder("vmware-guest")
    b.dmi(sys_vendor="VMware, Inc.", product_name="VMware20,1", product_version="None", bios_vendor="VMware, Inc.", bios_version="VMW201.00V.24006586.B64.2406042154",
          bios_date="06/04/2024", board_vendor="Intel Corporation", board_name="440BX Desktop Reference Platform", chassis_type=1)
    b.cpu(vendor_id="GenuineIntel", model_name="Intel(R) Core(TM) i7-7700HQ CPU @ 2.80GHz", family=6, model=158, stepping=9,
          flags=FLAGS_QEMU_SKYLAKE, threads=4, cores=4, hypervisor_vendor="VMware")
    b.memory(8 * 1024 ** 2 - 150_000, 7_200_000, 0)
    b.efi(secure_boot=False, setup_mode=False, boot_entries=("EFI VMware Virtual SCSI Hard Drive (0.0)",))
    b.pci("0000:00:0f.0", "030000", "15ad", "0405", "vmwgfx", boot_vga=True)
    b.pci("0000:00:10.0", "010000", "1000", "0030", "mptspi")
    b.pci("0000:03:00.0", "020000", "15ad", "07b0", "vmxnet3", parent="0000:00:15.0")
    b.pci("0000:00:11.0", "040100", "1274", "1371", "snd_ens1371")
    b.drm("card0", "0000:00:0f.0", {"Virtual-1": ("connected", make_edid(1920, 1080, 508, 286, "VMware Virtual Display", "VMW"), "1920x1080\n1280x800\n")})
    b.block("sda", "../devices/pci0000:00/0000:00:10.0/host2/target2:0:0/2:0:0:0/block/sda", 80 * GIB, rota=True, model="VMware Virtual S", vendor="VMware",
            by_path="pci-0000:00:10.0-scsi-0:0:0:0")
    live = live_usb(b, bus_slot="0000:02:00.0")
    b.lsblk([lsblk_disk("sda", 80 * GIB, tran="spi", model="VMware Virtual S", serial="", wwn=""), live])
    b.net("ens160", link="../../devices/pci0000:00/0000:00:15.0/0000:03:00.0/net/ens160", driver="vmxnet3", subsystem="pci",
          device_link="../../../0000:03:00.0", vendor="15ad", device="07b0")
    b.route("ens160")
    b.input_device("AT Translated Set 2 keyboard", "sysrq kbd leds event0")
    b.input_device("VMware VMware Virtual USB Mouse", "mouse0 event1", bus="0003", vendor="0e0f", product="0003")
    b.sound_card(0, "snd_ens1371", "pci")
    b.rtc(NOW)
    b.chrony()
    b.finish()


def intel_rst_hidden_nvme() -> None:
    b = Builder("intel-rst-hidden-nvme")
    b.dmi(sys_vendor="Dell Inc.", product_name="XPS 15 9560", bios_vendor="Dell Inc.", bios_version="1.19.1", bios_date="05/10/2021",
          board_vendor="Dell Inc.", board_name="05FFDN", chassis_type=10)
    b.cpu(vendor_id="GenuineIntel", model_name="Intel(R) Core(TM) i7-7700HQ CPU @ 2.80GHz", family=6, model=158, stepping=9, flags=FLAGS_KABYLAKE_H,
          threads=8, cores=4)
    b.memory(16_000_000 - 300_000, 14_100_000, 0)
    b.efi(secure_boot=False, setup_mode=False, windows=True, boot_entries=("Linux Boot Manager",))
    b.pci("0000:00:02.0", "030000", "8086", "591b", "i915", boot_vga=True)
    b.pci("0000:01:00.0", "030200", "10de", "1c8d", "nouveau", runtime="suspended", parent="0000:00:01.0")
    b.pci("0000:00:17.0", "010400", "8086", "2822", "ahci")
    b.pci("0000:00:1f.3", "040300", "8086", "a171", "snd_hda_intel")
    b.pci("0000:3b:00.0", "028000", "168c", "003e", "ath10k_pci", parent="0000:00:1c.0")
    b.drm("card0", "0000:00:02.0", {"eDP-1": ("connected", make_edid(3840, 2160, 344, 194, "SHP1453", "SHP"), "3840x2160\n1920x1080\n")})
    live = live_usb(b, bus_slot="0000:00:14.0")
    b.lsblk([live])
    b.net("wlp59s0", link="../../devices/pci0000:00/0000:00:1c.0/0000:3b:00.0/net/wlp59s0", driver="ath10k_pci", subsystem="pci",
          device_link="../../../0000:3b:00.0", wireless=True, vendor="168c", device="003e", carrier=False)
    b.route(None)
    b.supply("BAT0", "Battery", scope="System", status="Charging", capacity=47)
    b.supply("AC", "Mains", online=True)
    b.backlight()
    b.input_device("AT Translated Set 2 keyboard", "sysrq kbd leds event0")
    b.input_device("DLL07BE:01 06CB:7A13 Touchpad", "mouse0 event5", bus="0018", vendor="06cb", product="7a13")
    b.input_device("Lid Switch", "event1")
    b.sound_card(0, "snd_hda_intel", "pci")
    b.usb("1-3", "0951", "1666", "00", ("08",))
    b.tpm("1")
    b.rtc(NOW)
    b.finish()


# --- Wave 5 machines (H01): every one is SYNTHESIZED; see each note() for what is invented -----------------

SYNTHESIZED = "synthesized for the tests, not captured from a real machine; identifiers are plausible, serials and MACs do not exist"
DMI_UNVERIFIED = "the DMI strings follow public dmidecode reports and were not read from the device itself"


def nvme_install_disk(b: Builder, *, pci_slot: str, parent: str, model: str, size: int = 1_000_204_886_016, with_windows: bool = False,
                      live_slot: str = "0000:00:14.0") -> None:
    """One NVMe system disk plus the installer stick (shared by the new machines)."""
    b.pci(pci_slot, "010802", "144d", "a80a", "nvme", parent=parent)
    b.block("nvme0n1", f"../devices/pci0000:00/{parent + '/' if parent else ''}{pci_slot}/nvme/nvme0/nvme0n1", size, rota=False, model=model, dev="259:0",
            by_path=f"pci-{pci_slot}-nvme-1")
    live = live_usb(b, bus_slot=live_slot)
    children = ([lsblk_part("nvme0n1p1", 260 * 1024 ** 2, fstype="vfat", parttype=ESP_TYPE, maj_min="259:1"),
                 lsblk_part("nvme0n1p2", 300 * GIB, fstype="ntfs", parttype=MS_BASIC_TYPE, maj_min="259:2")] if with_windows else [])
    b.lsblk([lsblk_disk("nvme0n1", size, tran="nvme", model=model, serial="S6XXXXXXXXXXXXX", maj_min="259:0", children=children), live])


def wired_desktop_network(b: Builder, *, slot: str = "0000:06:00.0", parent: str = "0000:00:1c.2", iface: str = "enp6s0", driver: str = "r8169",
                          vendor: str = "10ec", device: str = "8125") -> None:
    b.pci(slot, "020000", vendor, device, driver, parent=parent)
    b.net(iface, link=f"../../devices/pci0000:00/{parent}/{slot}/net/{iface}", driver=driver, subsystem="pci", device_link=f"../../../{slot}",
          vendor=vendor, device=device)
    b.route(iface)


def nvidia_desktop(name: str, *, device: str, cpu_model: str, note: str, flags: str = FLAGS_KABYLAKE_H, vendor_id: str = "GenuineIntel", family: int = 6,
                   model: int = 183, board: str = "PRIME Z790-P", memory_kib: int = 32_000_000) -> None:
    """A tower PC with one NVIDIA card on a monitor. Pascal, Kepler and Blackwell differ in the device id, the CPU and the board."""
    b = Builder(name)
    b.dmi(sys_vendor="ASUSTeK COMPUTER INC.", product_name="System Product Name", product_version="System Version", bios_vendor="American Megatrends Inc.",
          bios_version="1664", bios_date="03/12/2025", board_vendor="ASUSTeK COMPUTER INC.", board_name=board, chassis_type=3)
    b.cpu(vendor_id=vendor_id, model_name=cpu_model, family=family, model=model, stepping=1, flags=flags, threads=16, cores=8)
    b.memory(memory_kib - 400_000, int(memory_kib * 0.88), 0)
    b.efi(secure_boot=False, setup_mode=False, boot_entries=("Windows Boot Manager",))
    b.pci("0000:01:00.0", "030000", "10de", device, "nouveau", boot_vga=True, parent="0000:00:01.0", subsystem=("1043", "8886"))
    b.pci("0000:01:00.1", "040300", "10de", "10f0", "snd_hda_intel", parent="0000:00:01.0")
    b.pci("0000:00:1f.3", "040300", "8086", "7a50", "snd_hda_intel")
    b.drm("card0", "0000:01:00.0", {"DP-1": ("connected", make_edid(2560, 1440, 597, 336, "DELL U2723QE", "DEL"), "2560x1440\n1920x1080\n"),
                                    "HDMI-A-1": ("disconnected", None, "")}, parent="0000:00:01.0")
    nvme_install_disk(b, pci_slot="0000:02:00.0", parent="0000:00:06.0", model="Samsung SSD 980 PRO 1TB", with_windows=True)
    wired_desktop_network(b)
    b.input_device("Logitech USB Keyboard", "sysrq kbd leds event0", bus="0003", vendor="046d", product="c31c")
    b.sound_card(0, "snd_hda_intel", "pci")
    b.usb("1-2", "046d", "c31c", "00", ("03",), manufacturer="Logitech", product_name="USB Keyboard")
    b.tpm()
    b.rtc(NOW)
    b.chrony()
    b.note(note)
    b.finish()


def pascal_desktop_gtx1060() -> None:
    nvidia_desktop("pascal-desktop-gtx1060", device="1c03", cpu_model="Intel(R) Core(TM) i7-6700K CPU @ 4.00GHz", model=94, board="Z170-A",
                   note=f"{SYNTHESIZED}; GP106 GTX 1060 (10de:1c03) is on the 580 branch's supported list: closed 580 modules, newer branches masked")


def kepler_desktop_gtx680() -> None:
    nvidia_desktop("kepler-desktop-gtx680", device="1180", cpu_model="Intel(R) Core(TM) i7-3770K CPU @ 3.50GHz", model=58, board="P8Z77-V",
                   note=f"{SYNTHESIZED}; GK104 GTX 680 (10de:1180): no supported proprietary driver in the tree, nouveau only (470 and 390 are masked)")


def blackwell_desktop_rtx5090() -> None:
    nvidia_desktop("blackwell-desktop-rtx5090", device="2b85", cpu_model="AMD Ryzen 9 9950X 16-Core Processor", vendor_id="AuthenticAMD", family=26, model=68,
                   flags=FLAGS_ZEN2_MOBILE, board="ROG CROSSHAIR X870E HERO", memory_kib=64_000_000,
                   note=f"{SYNTHESIZED}; GB202 RTX 5090 (10de:2b85): only the open kernel modules of the newest branch can drive it")


def asus_rog_laptop(name: str, *, eco: bool) -> None:
    """ROG Zephyrus: Intel iGPU + RTX 3070 Ti Laptop behind a MUX switch. eco=True is the same laptop with the dGPU switched off in firmware."""
    b = Builder(name)
    b.dmi(sys_vendor="ASUSTeK COMPUTER INC.", product_name="ROG Zephyrus M16 GU603ZW_GU603ZW", product_version="1.0",
          bios_vendor="American Megatrends International, LLC.", bios_version="GU603ZW.331", bios_date="08/04/2023", board_vendor="ASUSTeK COMPUTER INC.",
          board_name="GU603ZW", chassis_type=10)
    b.cpu(vendor_id="GenuineIntel", model_name="12th Gen Intel(R) Core(TM) i9-12900H", family=6, model=154, stepping=3, flags=FLAGS_TIGERLAKE_H, threads=20, cores=14)
    b.memory(32_000_000 - 1_100_000, 27_000_000, 0)
    b.efi(secure_boot=False, setup_mode=False, windows=True, boot_entries=("ubuntu",))
    b.pci("0000:00:02.0", "030000", "8086", "46a6", "i915", boot_vga=True, subsystem=("1043", "1ed2"))
    if not eco:
        b.pci("0000:01:00.0", "030200", "10de", "24a0", "nouveau", runtime="suspended", parent="0000:00:01.0", subsystem=("1043", "1ed2"))
    b.pci("0000:00:1f.3", "040100", "8086", "51ca", "sof-audio-pci-intel-tgl")
    b.pci("0000:00:14.3", "028000", "8086", "51f0", "iwlwifi")
    b.drm("card0", "0000:00:02.0", {"eDP-1": ("connected", make_edid(2560, 1600, 345, 215, "TL160ADMP05-0", "TMX"), "2560x1600\n1920x1200\n"),
                                    "HDMI-A-1": ("disconnected", None, "")})
    if not eco:
        b.drm("card1", "0000:01:00.0", {"HDMI-A-2": ("disconnected", None, "")}, parent="0000:00:01.0")
    nvme_install_disk(b, pci_slot="0000:00:06.0", parent="", model="WD PC SN560 SDDPNQE-1T00-1102", with_windows=True)
    b.net("wlp0s20f3", link="../../devices/pci0000:00/0000:00:14.3/net/wlp0s20f3", driver="iwlwifi", subsystem="pci", device_link="../../../0000:00:14.3",
          wireless=True, vendor="8086", device="51f0", carrier=False)
    b.route(None)
    b.supply("BAT0", "Battery", scope="System", status="Charging", capacity=71)
    b.supply("AC0", "Mains", online=True)
    b.charge_threshold("BAT0", 100)
    b.backlight("intel_backlight")
    b.leds("asus::kbd_backlight", brightness=1)
    b.leds("platform::micmute", maximum=1)
    dgpu_off = "1" if eco else "0"
    b.platform_device("asus-nb-wmi", "asus-nb-wmi", {"gpu_mux_mode": "1", "dgpu_disable": dgpu_off, "egpu_enable": "0", "panel_od": "0", "throttle_thermal_policy": "0"})
    b.firmware_attributes("asus-armoury", {"gpu_mux_mode": "1", "dgpu_disable": dgpu_off, "panel_overdrive": "0"})
    b.platform_profile("balanced", ("quiet", "balanced", "performance"))
    b.input_device("AT Translated Set 2 keyboard", "sysrq kbd leds event0")
    b.input_device("Asus Keyboard", "sysrq kbd leds event4", bus="0003", vendor="0b05", product="19b6")
    b.input_device("ASUE120A:00 04F3:319B Touchpad", "mouse0 event6", bus="0018", vendor="04f3", product="319b")
    b.input_device("Lid Switch", "event1")
    b.sound_card(0, "sof_sdw", "platform")
    b.usb("1-3", "0951", "1666", "00", ("08",))
    b.usb("1-5", "0b05", "19b6", "00", ("03", "03"), manufacturer="ASUSTeK Computer Inc.", product_name="N-KEY Device")
    b.usb("1-10", "8087", "0033", "e0", ("e0", "e0"))
    b.bluetooth()
    b.tpm()
    b.rtc(NOW)
    b.chrony()
    variant = "Variant of the MUX laptop with the NVIDIA GPU disabled in firmware (Eco mode): no 10de device is visible, dgpu_disable=1. " if eco else ""
    b.note(f"{SYNTHESIZED}; replace by a volunteer's capture. {variant}0b05:19b6 is named in the asusctl README; the sysfs attribute names follow the "
           "asus-nb-wmi and asus-armoury drivers", unverified=("dmi strings", "usb product string", "firmware-attributes names"))
    b.finish()


def asus_rog_zephyrus_intel_nvidia_mux() -> None:
    asus_rog_laptop("asus-rog-zephyrus-intel-nvidia-mux", eco=False)


def asus_rog_zephyrus_eco() -> None:
    asus_rog_laptop("asus-rog-zephyrus-eco", eco=True)


def razer_blade_intel_nvidia() -> None:
    b = Builder("razer-blade-intel-nvidia")
    b.dmi(sys_vendor="Razer", product_name="Blade 15 Base Model (Early 2020) - RZ09-0328", product_version="8.04", bios_vendor="Razer", bios_version="1.06",
          bios_date="01/09/2020", board_vendor="Razer", board_name="CH570", chassis_type=10)
    b.cpu(vendor_id="GenuineIntel", model_name="Intel(R) Core(TM) i7-10750H CPU @ 2.60GHz", family=6, model=165, stepping=2, flags=FLAGS_KABYLAKE_H, threads=12, cores=6)
    b.memory(16_000_000 - 700_000, 13_600_000, 0)
    b.efi(secure_boot=False, setup_mode=False, windows=True, boot_entries=("Windows Boot Manager",))
    b.pci("0000:00:02.0", "030000", "8086", "9bc4", "i915", boot_vga=True)
    b.pci("0000:01:00.0", "030200", "10de", "2191", "nouveau", runtime="suspended", parent="0000:00:01.0")
    b.pci("0000:00:1f.3", "040300", "8086", "06c8", "snd_hda_intel")
    b.pci("0000:00:14.3", "028000", "8086", "06f0", "iwlwifi")
    b.drm("card0", "0000:00:02.0", {"eDP-1": ("connected", make_edid(1920, 1080, 344, 194, "LQ156M1JW01", "SHP"), "1920x1080\n1280x720\n")})
    nvme_install_disk(b, pci_slot="0000:00:1d.0", parent="", model="SAMSUNG MZVLB512HBJQ-000L7", size=512_110_190_592, with_windows=True)
    b.net("wlp0s20f3", link="../../devices/pci0000:00/0000:00:14.3/net/wlp0s20f3", driver="iwlwifi", subsystem="pci", device_link="../../../0000:00:14.3",
          wireless=True, vendor="8086", device="06f0", carrier=False)
    b.route(None)
    b.supply("BAT0", "Battery", scope="System", status="Discharging", capacity=64)
    b.supply("AC0", "Mains", online=False)
    b.backlight("intel_backlight")
    b.input_device("AT Translated Set 2 keyboard", "sysrq kbd leds event0")
    b.input_device("Razer Razer Blade", "sysrq kbd leds event4", bus="0003", vendor="1532", product="0270")
    b.input_device("SYNA8004:00 06CB:CD8B Touchpad", "mouse0 event6", bus="0018", vendor="06cb", product="cd8b")
    b.input_device("Lid Switch", "event1")
    b.sound_card(0, "snd_hda_intel", "pci")
    b.usb("1-3", "0951", "1666", "00", ("08",))
    b.usb("1-9", "1532", "0270", "00", ("03", "03", "03"), manufacturer="Razer", product_name="Razer Blade")
    b.usb("1-10", "8087", "0026", "e0", ("e0", "e0"))
    b.bluetooth()
    b.tpm()
    b.rtc(NOW)
    b.chrony()
    b.note(f"{SYNTHESIZED}; {DMI_UNVERIFIED}. 1532:0270 is a plausible Blade 15 keyboard id, not read from a device", unverified=("dmi strings", "usb id 1532:0270"))
    b.finish()


def lenovo_legion_amd_nvidia() -> None:
    b = Builder("lenovo-legion-amd-nvidia")
    b.dmi(sys_vendor="LENOVO", product_name="82JW", product_version="Legion 5 15ACH6H", bios_vendor="LENOVO", bios_version="GKCN64WW", bios_date="07/15/2022",
          board_vendor="LENOVO", board_name="LNVNB161216", chassis_type=10)
    b.cpu(vendor_id="AuthenticAMD", model_name="AMD Ryzen 7 5800H with Radeon Graphics", family=25, model=80, stepping=0, flags=FLAGS_ZEN2_MOBILE, threads=16, cores=8)
    b.memory(16_000_000 - 520_000, 13_400_000, 0)
    b.efi(secure_boot=False, setup_mode=False, windows=True, boot_entries=("Windows Boot Manager",))
    b.pci("0000:06:00.0", "030000", "1002", "1638", "amdgpu", boot_vga=True, parent="0000:00:08.1")
    b.pci("0000:01:00.0", "030200", "10de", "2520", "nouveau", runtime="suspended", parent="0000:00:01.1")
    b.pci("0000:06:00.6", "040300", "1022", "15e3", "snd_hda_intel", parent="0000:00:08.1")
    b.pci("0000:03:00.0", "028000", "10ec", "c822", "rtw89_8852ae", parent="0000:00:02.2")
    b.pci("0000:04:00.0", "020000", "10ec", "8168", "r8169", parent="0000:00:02.3")
    b.drm("card0", "0000:06:00.0", {"eDP-1": ("connected", make_edid(1920, 1080, 344, 194, "LEN8AA2", "LEN"), "1920x1080\n1280x720\n")}, parent="0000:00:08.1")
    nvme_install_disk(b, pci_slot="0000:02:00.0", parent="0000:00:02.1", model="SAMSUNG MZALQ512HBLU-00BL2", size=512_110_190_592, with_windows=True)
    b.net("wlp3s0", link="../../devices/pci0000:00/0000:00:02.2/0000:03:00.0/net/wlp3s0", driver="rtw89_8852ae", subsystem="pci",
          device_link="../../../0000:03:00.0", wireless=True, vendor="10ec", device="c822", carrier=False)
    b.route(None)
    b.supply("BAT0", "Battery", scope="System", status="Discharging", capacity=90)
    b.supply("ADP0", "Mains", online=False)
    b.charge_threshold("BAT0", 100)
    b.backlight("amdgpu_bl0")
    b.platform_device("ideapad_acpi", "ideapad_acpi", {"conservation_mode": "0", "fn_lock": "0"})
    b.platform_profile("balanced", ("low-power", "balanced", "performance"))
    b.firmware_attributes("lenovo-wmi-other", {"gpu_mux_mode": "1"})
    b.input_device("AT Translated Set 2 keyboard", "sysrq kbd leds event0")
    b.input_device("SYNA2BA6:00 06CB:CE67 Touchpad", "mouse0 event6", bus="0018", vendor="06cb", product="ce67")
    b.input_device("Lid Switch", "event1")
    b.sound_card(0, "snd_hda_intel", "pci")
    b.usb("1-3", "0951", "1666", "00", ("08",))
    b.usb("1-4", "048d", "c965", "00", ("03", "03"), manufacturer="ITE Tech. Inc.", product_name="ITE Device(8295)")
    b.bluetooth()
    b.tpm()
    b.rtc(NOW)
    b.chrony()
    b.note(f"{SYNTHESIZED}; {DMI_UNVERIFIED}. 048d:c965 (ITE 8295 keyboard controller) and the lenovo-wmi-other attribute are modelled, not read",
           unverified=("dmi strings", "usb id 048d:c965", "firmware-attributes names"))
    b.finish()


def asus_tuf_amd_nvidia() -> None:
    """An AMD laptop with an NVIDIA GPU and no MUX switch (an ASUS TUF A15): offload only."""
    b = Builder("amd-laptop-nvidia")
    b.dmi(sys_vendor="ASUSTeK COMPUTER INC.", product_name="TUF Gaming A15 FA506ICB_FA506ICB", product_version="1.0",
          bios_vendor="American Megatrends International, LLC.", bios_version="FA506ICB.317", bios_date="11/03/2022", board_vendor="ASUSTeK COMPUTER INC.",
          board_name="FA506ICB", chassis_type=10)
    b.cpu(vendor_id="AuthenticAMD", model_name="AMD Ryzen 7 4800H with Radeon Graphics", family=23, model=96, stepping=1, flags=FLAGS_ZEN2_MOBILE, threads=16, cores=8)
    b.memory(16_000_000 - 510_000, 13_200_000, 0)
    b.efi(secure_boot=False, setup_mode=False, windows=True, boot_entries=("Windows Boot Manager",))
    b.pci("0000:05:00.0", "030000", "1002", "1638", "amdgpu", boot_vga=True, parent="0000:00:08.1")
    b.pci("0000:01:00.0", "030200", "10de", "25a0", "nouveau", runtime="suspended", parent="0000:00:01.1")
    b.pci("0000:05:00.6", "040300", "1022", "15e3", "snd_hda_intel", parent="0000:00:08.1")
    b.pci("0000:02:00.0", "028000", "14c3", "0616", "mt7921e", parent="0000:00:02.2")
    b.drm("card0", "0000:05:00.0", {"eDP-1": ("connected", make_edid(1920, 1080, 344, 194, "BOE0A72", "BOE"), "1920x1080\n1280x720\n")}, parent="0000:00:08.1")
    nvme_install_disk(b, pci_slot="0000:03:00.0", parent="0000:00:02.1", model="Micron_2210_MTFDHBA512QFD", size=512_110_190_592, with_windows=True)
    b.net("wlp2s0", link="../../devices/pci0000:00/0000:00:02.2/0000:02:00.0/net/wlp2s0", driver="mt7921e", subsystem="pci",
          device_link="../../../0000:02:00.0", wireless=True, vendor="14c3", device="0616", carrier=False)
    b.route(None)
    b.supply("BAT0", "Battery", scope="System", status="Discharging", capacity=77)
    b.supply("AC0", "Mains", online=False)
    b.charge_threshold("BAT0", 100)
    b.backlight("amdgpu_bl0")
    b.leds("asus::kbd_backlight")
    b.platform_device("asus-nb-wmi", "asus-nb-wmi", {"throttle_thermal_policy": "0"})
    b.platform_profile("balanced", ("quiet", "balanced", "performance"))
    b.input_device("AT Translated Set 2 keyboard", "sysrq kbd leds event0")
    b.input_device("Asus Keyboard", "sysrq kbd leds event4", bus="0003", vendor="0b05", product="19b6")
    b.input_device("ASUE1209:00 04F3:319F Touchpad", "mouse0 event6", bus="0018", vendor="04f3", product="319f")
    b.input_device("Lid Switch", "event1")
    b.sound_card(0, "snd_hda_intel", "pci")
    b.usb("1-3", "0951", "1666", "00", ("08",))
    b.usb("1-5", "0b05", "19b6", "00", ("03", "03"), manufacturer="ASUSTeK Computer Inc.", product_name="N-KEY Device")
    b.bluetooth()
    b.tpm()
    b.rtc(NOW)
    b.chrony()
    b.note(f"{SYNTHESIZED}; {DMI_UNVERIFIED}", unverified=("dmi strings", "usb product string"))
    b.finish()


def intel_arc_discrete_desktop() -> None:
    b = Builder("intel-arc-discrete-desktop")
    b.dmi(sys_vendor="Micro-Star International Co., Ltd.", product_name="MS-7E07", product_version="1.0", bios_vendor="American Megatrends International, LLC.",
          bios_version="1.A0", bios_date="04/16/2024", board_vendor="Micro-Star International Co., Ltd.", board_name="PRO B760M-P DDR4 (MS-7E07)", chassis_type=3)
    b.cpu(vendor_id="GenuineIntel", model_name="13th Gen Intel(R) Core(TM) i5-13400F", family=6, model=191, stepping=2, flags=FLAGS_TIGERLAKE_H, threads=16, cores=10)
    b.memory(32_000_000 - 450_000, 29_000_000, 0)
    b.efi(secure_boot=False, setup_mode=False, boot_entries=("Windows Boot Manager",))
    b.pci("0000:03:00.0", "030000", "8086", "56a0", "i915", boot_vga=True, parent="0000:00:01.0")
    b.pci("0000:04:00.0", "040300", "8086", "4f90", "snd_hda_intel", parent="0000:00:01.0")
    b.pci("0000:00:1f.3", "040300", "8086", "7a50", "snd_hda_intel")
    b.drm("card0", "0000:03:00.0", {"DP-1": ("connected", make_edid(2560, 1440, 597, 336, "LG ULTRAGEAR", "GSM"), "2560x1440\n1920x1080\n"),
                                    "HDMI-A-1": ("disconnected", None, "")}, parent="0000:00:01.0")
    nvme_install_disk(b, pci_slot="0000:02:00.0", parent="0000:00:06.0", model="KINGSTON SKC3000S1024G", with_windows=True)
    wired_desktop_network(b)
    b.input_device("AT Translated Set 2 keyboard", "sysrq kbd leds event0")
    b.sound_card(0, "snd_hda_intel", "pci")
    b.tpm()
    b.rtc(NOW)
    b.chrony()
    b.note(f"{SYNTHESIZED}; DG2 Arc A770 (8086:56a0) is a discrete card: no iGPU fix applies")
    b.finish()


def intel_core_ultra_laptop_arc_igpu() -> None:
    b = Builder("intel-core-ultra-laptop-arc-igpu")
    b.dmi(sys_vendor="Acer", product_name="Swift SFG14-72", product_version="V1.06", bios_vendor="Insyde Corp.", bios_version="V1.06", bios_date="03/12/2024",
          board_vendor="Acer", board_name="Meteor Lake", chassis_type=10)
    b.cpu(vendor_id="GenuineIntel", model_name="Intel(R) Core(TM) Ultra 7 155H", family=6, model=170, stepping=4, flags=FLAGS_TIGERLAKE_H, threads=22, cores=16)
    b.memory(16_000_000 - 800_000, 13_500_000, 0)
    b.efi(secure_boot=False, setup_mode=False, windows=True, boot_entries=("Windows Boot Manager",))
    b.pci("0000:00:02.0", "030000", "8086", "7d55", "i915", boot_vga=True)
    b.pci("0000:00:1f.3", "040100", "8086", "7e28", "sof-audio-pci-intel-mtl")
    b.pci("0000:00:14.3", "028000", "8086", "7e40", "iwlwifi")
    b.drm("card0", "0000:00:02.0", {"eDP-1": ("connected", make_edid(2880, 1800, 302, 189, "ATNA45AF01-0", "SDC"), "2880x1800\n1920x1200\n")})
    nvme_install_disk(b, pci_slot="0000:00:06.0", parent="", model="Micron MTFDKBA512TFH", size=512_110_190_592, with_windows=True)
    b.net("wlp0s20f3", link="../../devices/pci0000:00/0000:00:14.3/net/wlp0s20f3", driver="iwlwifi", subsystem="pci", device_link="../../../0000:00:14.3",
          wireless=True, vendor="8086", device="7e40", carrier=False)
    b.route(None)
    b.supply("BAT1", "Battery", scope="System", status="Discharging", capacity=88)
    b.supply("ACAD", "Mains", online=False)
    b.backlight("intel_backlight")
    b.platform_profile("balanced", ("low-power", "balanced", "performance"))
    b.input_device("AT Translated Set 2 keyboard", "sysrq kbd leds event0")
    b.input_device("ELAN0406:00 04F3:3077 Touchpad", "mouse0 event6", bus="0018", vendor="04f3", product="3077")
    b.input_device("Lid Switch", "event1")
    b.sound_card(0, "sof_sdw", "platform")
    b.usb("1-3", "0951", "1666", "00", ("08",))
    b.usb("1-7", "04f2", "b7b9", "ef", ("0e", "0e"), manufacturer="Chicony Electronics Co.,Ltd.", product_name="Integrated Camera")
    b.bluetooth()
    b.tpm()
    b.rtc(NOW)
    b.chrony()
    b.note(f"{SYNTHESIZED}; Meteor Lake Arc graphics (8086:7d55) is an integrated GPU: the discrete-Arc rules must not apply")
    b.finish()


def desktop_secureboot(dest: Path, source: Path | None = None) -> Path:
    """The captured RTX 4090 desktop as it would look with Secure Boot on (SecureBoot=1, kernel lockdown in integrity mode).

    Derived at test time from the captured host fixture, like desktop_igpu_idle: the NVIDIA policy for Secure Boot can be tested
    without a second copy of the real capture. The SecureBoot variable and the lockdown file are the invented part.
    """
    source = source or FIXTURES / "asus-desktop-rtx4090"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    writer = FixtureWriter(dest)
    writer.links = json.loads((dest / ".links.json").read_text())
    writer.write_file(f"/sys/firmware/efi/efivars/SecureBoot-{hardware.EFI_GLOBAL_GUID}", b"\x07\x00\x00\x00\x01")
    writer.write_file("/sys/kernel/security/lockdown", "none [integrity] confidentiality\n")
    writer.finish(None)
    meta = json.loads((dest / ".capture.json").read_text())
    meta.update({"synthetic": True, "note": "derived from the real asus-desktop-rtx4090 capture; only the SecureBoot variable and the lockdown file are synthesized",
                 "summary": hardware.probe(dest, now=meta["now"]).summary()})
    (dest / ".capture.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    return dest


WAVE5_MACHINES = (pascal_desktop_gtx1060, kepler_desktop_gtx680, blackwell_desktop_rtx5090, asus_rog_zephyrus_intel_nvidia_mux, asus_rog_zephyrus_eco,
                  razer_blade_intel_nvidia, lenovo_legion_amd_nvidia, asus_tuf_amd_nvidia, intel_arc_discrete_desktop, intel_core_ultra_laptop_arc_igpu)


def desktop_igpu_idle(dest: Path, source: Path | None = None) -> Path:
    """The captured RTX 4090 desktop with the CPU's integrated GPU switched on but unused (monitors stay on the dGPU).

    Derived at test time from the captured host fixture instead of being committed as a second copy of it.
    """
    source = source or FIXTURES / "asus-desktop-rtx4090"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    writer = FixtureWriter(dest)
    writer.links = json.loads((dest / ".links.json").read_text())
    base = "/sys/bus/pci/devices/0000:00:02.0"
    for name, value in {"class": "0x030000", "vendor": "0x8086", "device": "0xa780", "boot_vga": "0", "power/runtime_status": "active"}.items():
        writer.write_file(f"{base}/{name}", value + "\n")
    writer.link(base, "../../../devices/pci0000:00/0000:00:02.0")
    writer.link(base + "/driver", "../../../bus/pci/drivers/i915")
    writer.link("/sys/class/drm/card0", "../../devices/pci0000:00/0000:00:02.0/drm/card0")
    writer.link("/sys/class/drm/card0/device", "../../../0000:00:02.0")
    for connector in ("DP-1", "HDMI-A-1"):
        writer.link(f"/sys/class/drm/card0-{connector}", f"../../devices/pci0000:00/0000:00:02.0/drm/card0/card0-{connector}")
        writer.write_file(f"/sys/class/drm/card0-{connector}/status", "disconnected\n")
    ids = dest / "usr/share/hwdata/pci.ids"  # a fixed name: this runs inside the tests and must not depend on the local hwdata
    ids.write_text(ids.read_text().replace("8086  Intel Corporation\n", "8086  Intel Corporation\n\ta780  Raptor Lake-S GT1 [UHD Graphics 770]\n", 1))
    writer.finish(None)
    meta = json.loads((dest / ".capture.json").read_text())
    meta.update({"synthetic": True, "summary": hardware.probe(dest, now=meta["now"]).summary()})
    (dest / ".capture.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    return dest


def laptop_disk(b: Builder, model: str = "Samsung SSD 980 PRO 1TB") -> None:
    """One NVMe disk and the live USB stick, for the laptops that only exist to exercise vendor probes."""
    b.pci("0000:00:1d.0", "010802", "144d", "a80a", "nvme")
    b.block("nvme0n1", "../devices/pci0000:00/0000:00:1d.0/nvme/nvme0/nvme0n1", 1_000_204_886_016, rota=False, model=model, dev="259:0",
            by_path="pci-0000:00:1d.0-nvme-1")
    live = live_usb(b, bus_slot="0000:00:14.0")
    b.lsblk([lsblk_disk("nvme0n1", 1_000_204_886_016, tran="nvme", model=model, serial="S64XXXXXXXXXXXX", maj_min="259:0",
                        children=[lsblk_part("nvme0n1p1", 500 * 1024 ** 2, fstype="vfat", parttype=ESP_TYPE, maj_min="259:1")]), live])


def asus_rog_laptop() -> None:
    b = Builder("asus-rog-laptop")
    b.dmi(sys_vendor="ASUSTeK COMPUTER INC.", product_name="ROG Zephyrus G14 GA402XI_GA402XI", product_version="1.0", bios_vendor="American Megatrends International, LLC.",
          bios_version="GA402XI.318", bios_date="03/05/2024", board_vendor="ASUSTeK COMPUTER INC.", board_name="GA402XI", chassis_type=10)
    b.cpu(vendor_id="AuthenticAMD", model_name="AMD Ryzen 9 7940HS w/ Radeon 780M Graphics", family=25, model=116, stepping=1, flags=FLAGS_ZEN2_MOBILE,
          threads=16, cores=8)
    b.memory(16_000_000 - 512_000, 13_900_000, 0)
    b.efi(secure_boot=False, setup_mode=False, boot_entries=("Windows Boot Manager",))
    b.pci("0000:65:00.0", "030000", "1002", "15bf", "amdgpu", boot_vga=True, parent="0000:00:08.1")
    b.pci("0000:01:00.0", "030200", "10de", "28e0", "nouveau", runtime="suspended", parent="0000:00:01.1")
    b.drm("card0", "0000:65:00.0", {"eDP-1": ("connected", make_edid(2560, 1600, 301, 188, "AUO8AA0", "AUO"), "2560x1600\n1920x1200\n")}, parent="0000:00:08.1")
    b.drm("card1", "0000:01:00.0", {}, parent="0000:00:01.1")
    b.supply("BAT0", "Battery", scope="System", status="Discharging", capacity=70)
    b.charge_limit("BAT0")
    b.supply("ACAD", "Mains", online=False)
    b.backlight("amdgpu_bl1")
    b.input_device("AT Translated Set 2 keyboard", "sysrq kbd leds event0")
    b.input_device("ASUE120A:00 04F3:319B Touchpad", "mouse0 event6", bus="0018", vendor="04f3", product="319b")
    b.input_device("Lid Switch", "event1")
    b.usb("1-3", "0b05", "19b6", "00", ("03",), manufacturer="ASUSTeK Computer Inc.", label="N-KEY Device")
    b.usb("1-4", "0b05", "19af", "00", ("03",), manufacturer="ASUSTeK Computer Inc.", label="AURA LED Controller")
    b.bluetooth()
    b.platform_driver("asus-nb-wmi")
    b.platform_profile()
    b.led("asus::kbd_backlight")
    b.sleep("[s2idle]", 2)
    b.tpm()
    b.rtc(NOW)
    laptop_disk(b)
    b.finish()


def razer_blade_laptop() -> None:
    b = Builder("razer-blade-laptop")
    b.dmi(sys_vendor="Razer", product_name="Blade 15 Base Model (2020)", product_version="8.04", bios_vendor="Razer", bios_version="01.04",
          bios_date="09/15/2020", board_vendor="Razer", board_name="CH570", chassis_type=10)
    b.cpu(vendor_id="GenuineIntel", model_name="Intel(R) Core(TM) i7-10750H CPU @ 2.60GHz", family=6, model=165, stepping=2, flags=FLAGS_KABYLAKE_H,
          threads=12, cores=6)
    b.memory(16_000_000 - 512_000, 13_900_000, 0)
    b.efi(secure_boot=False, setup_mode=False, boot_entries=("Windows Boot Manager",))
    b.pci("0000:00:02.0", "030000", "8086", "9bc4", "i915", boot_vga=True)
    b.pci("0000:01:00.0", "030200", "10de", "249d", "nouveau", runtime="suspended", parent="0000:00:01.0")
    b.drm("card0", "0000:00:02.0", {"eDP-1": ("connected", make_edid(1920, 1080, 344, 194, "SDC4147", "SDC"), "1920x1080\n")})
    b.drm("card1", "0000:01:00.0", {}, parent="0000:00:01.0")
    b.supply("BAT0", "Battery", scope="System", status="Discharging", capacity=64)
    b.supply("AC0", "Mains", online=False)
    b.backlight()
    b.input_device("AT Translated Set 2 keyboard", "sysrq kbd leds event0")
    b.input_device("Lid Switch", "event1")
    b.usb("1-3", "1532", "026f", "00", ("03",), manufacturer="Razer", label="Razer Blade")
    b.bluetooth()
    b.sleep("[s2idle]", 2)
    b.tpm()
    b.rtc(NOW)
    laptop_disk(b)
    b.finish()


def arc_ultra_convertible() -> None:
    b = Builder("arc-ultra-convertible")
    b.dmi(sys_vendor="LENOVO", product_name="83DN", product_version="Yoga Pro 9 16IMH9", bios_vendor="LENOVO", bios_version="N0XET32W (1.14 )",
          bios_date="05/23/2024", board_vendor="LENOVO", board_name="LNVNB161216", chassis_type=31)
    b.cpu(vendor_id="GenuineIntel", model_name="Intel(R) Core(TM) Ultra 9 185H", family=6, model=170, stepping=4, flags=FLAGS_TIGERLAKE_H, threads=22, cores=16)
    b.memory(32_000_000 - 1_000_000, 27_500_000, 0)
    b.efi(secure_boot=False, setup_mode=False, boot_entries=("Windows Boot Manager",))
    b.pci("0000:00:02.0", "030000", "8086", "7d55", "i915", boot_vga=True)
    b.pci("0000:01:00.0", "030200", "10de", "28a1", "nouveau", runtime="suspended", parent="0000:00:01.0")
    b.pci("0000:00:05.0", "048000", "8086", "7d19", "intel-ipu6")
    b.drm("card0", "0000:00:02.0", {"eDP-1": ("connected", make_edid(3200, 2000, 346, 216, "LEN8DA3", "LEN"), "3200x2000\n")})
    b.drm("card1", "0000:01:00.0", {}, parent="0000:00:01.0")
    b.supply("BAT0", "Battery", scope="System", status="Discharging", capacity=90)
    b.charge_limit("BAT0")
    b.supply("AC", "Mains", online=False)
    b.backlight()
    b.input_device("AT Translated Set 2 keyboard", "sysrq kbd leds event0")
    b.input_device("GXTP7863:00 27C6:0F95 Touchpad", "mouse0 event6", bus="0018", vendor="27c6", product="0f95")
    b.input_device("GXTP7863:00 27C6:0F95", "mouse1 event7", bus="0018", vendor="27c6", product="0f95")
    b.input_device("ELAN9008:00 04F3:2C5A Stylus", "event8", bus="0018", vendor="04f3", product="2c5a")
    b.input_device("Intel HID switches", "event9")
    b.input_device("Lid Switch", "event1")
    b.iio(0, "accel_3d")
    b.iio(1, "als")
    b.usb("1-6", "27c6", "6594", "ff", ("ff",), manufacturer="Goodix", label="Goodix MOC Fingerprint Sensor")
    b.bluetooth()
    b.sleep("[s2idle]", 2)
    b.tpm()
    b.rtc(NOW)
    laptop_disk(b)
    b.finish()


def thinkpad_amd_laptop() -> None:
    b = Builder("thinkpad-amd-laptop")
    b.dmi(sys_vendor="LENOVO", product_name="21CQ", product_version="ThinkPad T14s Gen 3", bios_vendor="LENOVO", bios_version="R22ET41W (1.16 )",
          bios_date="02/01/2024", board_vendor="LENOVO", board_name="21CQ", chassis_type=10)
    b.cpu(vendor_id="AuthenticAMD", model_name="AMD Ryzen 7 PRO 6850U with Radeon Graphics", family=25, model=68, stepping=1, flags=FLAGS_ZEN2_MOBILE, threads=16, cores=8)
    b.memory(32_000_000 - 1_000_000, 27_500_000, 0)
    b.efi(secure_boot=False, setup_mode=False, boot_entries=("Windows Boot Manager",))
    b.pci("0000:33:00.0", "030000", "1002", "1681", "amdgpu", boot_vga=True, parent="0000:00:08.1")
    b.drm("card0", "0000:33:00.0", {"eDP-1": ("connected", make_edid(1920, 1200, 301, 188, "LEN40B2", "LEN"), "1920x1200\n")}, parent="0000:00:08.1")
    b.supply("BAT0", "Battery", scope="System", status="Discharging", capacity=77)
    b.charge_limit("BAT0", 100)
    b.supply("AC", "Mains", online=False)
    b.backlight("amdgpu_bl0")
    b.input_device("AT Translated Set 2 keyboard", "sysrq kbd leds event0")
    b.input_device("SYNA8016:00 06CB:CE2D Touchpad", "mouse0 event5", bus="0018", vendor="06cb", product="ce2d")
    b.input_device("Lid Switch", "event1")
    b.usb("1-3", "06cb", "00fc", "ff", ("ff",), manufacturer="Synaptics", label="Prometheus MIS Touch Fingerprint Reader")
    b.bluetooth()
    b.platform_driver("thinkpad_acpi")
    b.platform_profile()
    b.led("tpacpi::kbd_backlight")
    b.sleep("[s2idle] deep", 2)
    b.tpm()
    b.rtc(NOW)
    laptop_disk(b)
    b.finish()


def build_all() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for build in (qemu_q35_virtio, old_bios_sata_radeon, thinkpad_intel_nvidia_hybrid, amd_laptop_sof, vmware_guest, intel_rst_hidden_nvme,
                  asus_rog_laptop, razer_blade_laptop, arc_ultra_convertible, thinkpad_amd_laptop, *WAVE5_MACHINES):
        build()


if __name__ == "__main__":
    build_all()
    for path in sorted(FIXTURES.iterdir()):
        meta = json.loads((path / ".capture.json").read_text()) if (path / ".capture.json").exists() else {}
        print(f"{path.name:32s} {meta.get('summary', '')}")
