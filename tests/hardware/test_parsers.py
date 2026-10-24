from __future__ import annotations

import struct

import pytest

from gentoo_installer import hardware as hw
from gentoo_installer import hardware_data as data

from tests.hardware.synthetic import make_edid

CPUINFO = """processor\t: 0
vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 183
model name\t: Intel(R) Core(TM) i9-14900K
stepping\t: 1
cpu cores\t: 24
flags\t\t: fpu lm sse sse2 hypervisor avx2

processor\t: 1
vendor_id\t: GenuineIntel
model name\t: SOMETHING ELSE
flags\t\t: fpu

processor\t: 2
"""


def test_cpuinfo_counts_threads_and_reads_the_first_cpu_only():
    cpu = hw.parse_cpuinfo(CPUINFO)
    assert (cpu.vendor, cpu.threads, cpu.cores, cpu.family, cpu.model, cpu.stepping) == ("intel", 3, 24, 6, 183, 1)
    assert cpu.model_name == "Intel(R) Core(TM) i9-14900K"
    assert cpu.flags == ["fpu", "lm", "sse", "sse2", "hypervisor", "avx2"]
    assert cpu.hypervisor_flag


def test_cpuinfo_garbage_is_tolerated():
    assert hw.parse_cpuinfo("").threads == 0
    cpu = hw.parse_cpuinfo("processor : x\nmodel : not-a-number\nflags\n\x00\x00")
    assert cpu.model == 0 and cpu.vendor == ""


def test_meminfo():
    memory = hw.parse_meminfo("MemTotal: 16000000 kB\nMemFree: 1 kB\nMemAvailable: 9000000 kB\nSwapTotal: 100 kB\nBogus: x\n")
    assert (memory.total_kib, memory.available_kib, memory.swap_total_kib) == (16000000, 9000000, 100)
    assert hw.parse_meminfo("MemTotal: 5 kB\nMemFree: 3 kB\n").available_kib == 3  # kernels without MemAvailable
    assert hw.parse_meminfo("garbage") == hw.MemoryInfo()


@pytest.mark.parametrize("text", [
    "*/* CPU_FLAGS_X86: aes avx avx2 sse4_1",   # cpuid2cpuflags 18 (package.use form)
    "CPU_FLAGS_X86: aes avx avx2 sse4_1",        # older and documented form
    'CPU_FLAGS_X86="aes avx avx2 sse4_1"',       # shell assignment
    "warning: something\n*/* CPU_FLAGS_X86: aes avx avx2 sse4_1\n",
    "  */*   CPU_FLAGS_X86:   aes avx avx2 sse4_1   ",
])
def test_cpuid2cpuflags_accepts_every_prefix(text):
    assert hw.parse_cpuid2cpuflags(text) == ["aes", "avx", "avx2", "sse4_1"]


def test_cpuid2cpuflags_keeps_only_valid_values_and_dedupes():
    assert hw.parse_cpuid2cpuflags("*/* CPU_FLAGS_X86: avx avx bogus sse; rm -rf /") == ["avx"]
    assert hw.parse_cpuid2cpuflags("") == []
    assert hw.parse_cpuid2cpuflags("no flags here") == []


def sb(value: int | None) -> bytes | None:
    return None if value is None else b"\x07\x00\x00\x00" + bytes([value])


@pytest.mark.parametrize(("secure_boot", "setup", "state"), [
    (1, 0, "enabled"), (0, 1, "setup"), (0, 0, "disabled"), (1, 1, "setup"), (None, None, "unsupported"), (1, None, "enabled"),
])
def test_secure_boot_truth_table(secure_boot, setup, state):
    assert hw.parse_secure_boot(sb(secure_boot), sb(setup)) == state


def test_secure_boot_short_variable_is_unknown():
    assert hw.parse_secure_boot(b"\x07\x00", None) == "unknown"


@pytest.mark.parametrize(("text", "mode"), [
    ("none [integrity] confidentiality\n", "integrity"), ("[none] integrity confidentiality", "none"),
    ("none integrity [confidentiality]", "confidentiality"), ("", ""), (None, ""),
])
def test_lockdown(text, mode):
    assert hw.parse_lockdown(text) == mode


def test_mounts_and_mountinfo():
    mountinfo = "36 35 98:0 / /run/initramfs/live ro,relatime shared:1 - iso9660 /dev/sdb1 ro\n40 35 0:30 / /mnt/a\\040b rw - ext4 /dev/sda2 rw\n"
    entries = hw.parse_mountinfo(mountinfo)
    assert ("/run/initramfs/live", "iso9660", "/dev/sdb1", "ro,relatime") in entries
    assert entries[1][0] == "/mnt/a b"
    mounts = hw.parse_mountinfo("efivarfs /sys/firmware/efi/efivars efivarfs rw,nosuid 0 0\n")
    assert mounts == [("/sys/firmware/efi/efivars", "efivarfs", "efivarfs", "rw,nosuid")]
    assert hw.parse_mountinfo("garbage") == []


PCI_IDS = """# comment
8086  Intel Corporation
\t0042  Core Processor Integrated Graphics Controller
\t\t1043 844d  subsystem line is ignored
\t9a49  TigerLake-LP GT2 [Iris Xe Graphics]
10de  NVIDIA Corporation
\t2684  AD102 [GeForce RTX 4090]
\t2685  AD102 [GeForce RTX 4090 D]
ffff  Illegal Vendor ID

C 00  Unclassified device
\t00  Non-VGA unclassified device
"""


def test_pci_ids_lookup_reads_only_wanted_entries():
    vendors, devices = hw.parse_pci_ids(PCI_IDS, [("10de", "2684"), ("8086", "9A49"), ("1234", "0001"), ("8086", "ffff")])
    assert vendors == {"10de": "NVIDIA Corporation", "8086": "Intel Corporation"}
    assert devices == {("10de", "2684"): "AD102 [GeForce RTX 4090]", ("8086", "9a49"): "TigerLake-LP GT2 [Iris Xe Graphics]"}
    assert hw.parse_pci_ids(PCI_IDS, []) == ({}, {})
    assert hw.parse_pci_ids("", [("10de", "2684")]) == ({}, {})


def test_edid_preferred_timing_size_and_name():
    edid = hw.parse_edid(make_edid(3840, 2160, 597, 336, "Example 27", "DEL"))
    assert (edid["width"], edid["height"], edid["mm_width"], edid["mm_height"], edid["name"]) == (3840, 2160, 597, 336, "Example 27")


def test_edid_rejects_junk():
    assert hw.parse_edid(None) == {}
    assert hw.parse_edid(b"\x00" * 128) == {}
    assert hw.parse_edid(b"short") == {}


@pytest.mark.parametrize(("w_px", "w_mm", "h_px", "h_mm", "dpi"), [
    (3840, 597, 2160, 336, 163), (1920, 344, 1080, 194, 142), (1920, 509, 1080, 286, 96), (3840, 344, 2400, 215, 284),
    (1920, 0, 1080, 0, 0),       # no physical size
    (1920, 30, 1080, 20, 0),     # implausibly small panel
    (1920, 509, 1080, 100, 0),   # width and height disagree: a bogus EDID, not a real density
    (800, 4000, 600, 3000, 0),   # a projector reporting its image size
])
def test_dpi(w_px, w_mm, h_px, h_mm, dpi):
    assert hw.dpi_from(w_px, w_mm, h_px, h_mm) == dpi


CHRONY_OK = "A29FC801,162.159.200.1,3,1792816205.66,0.000373817,-0.000223613,0.00036,-25.850,-0.036,0.487,0.02,0.005,1038.4,Normal"
CHRONY_BAD = "00000000,,0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,Not synchronised"


def test_chrony_tracking():
    assert hw.parse_chrony_tracking(CHRONY_OK) == (True, pytest.approx(0.000373817))
    assert hw.parse_chrony_tracking(CHRONY_BAD) == (False, None)
    assert hw.parse_chrony_tracking("") == (None, None)
    assert hw.parse_chrony_tracking(None) == (None, None)
    assert hw.parse_chrony_tracking("1,2,3") == (None, None)


def test_rtc_parsing_and_mode():
    now = 1_792_800_000.0
    assert hw.parse_rtc("2026-10-24", "00:00:00") == now
    assert hw.parse_rtc("x", "y") is None
    assert hw.rtc_mode(now + 30, now, True) == ("utc", 30)
    assert hw.rtc_mode(now + 7 * 3600 + 20, now, True) == ("local", 7 * 3600 + 20)       # UTC+7 dual boot
    assert hw.rtc_mode(now - 5 * 3600, now, True)[0] == "local"                          # UTC-5
    assert hw.rtc_mode(now + 5 * 3600 + 45 * 60, now, True)[0] == "local"                # UTC+5:45
    assert hw.rtc_mode(now + 7 * 3600, now, False) == ("unknown", 7 * 3600)              # not synced: cannot tell
    assert hw.rtc_mode(now + 400, now, True)[0] == "unknown"                              # a dead CMOS battery
    assert hw.rtc_mode(now + 20 * 3600, now, True)[0] == "unknown"
    assert hw.rtc_mode(None, now, True) == ("unknown", None)


def test_profile_dict_roundtrip_is_tolerant():
    profile = hw.HardwareProfile()
    profile.cpu.vendor = "amd"
    profile.gpus = [hw.GpuInfo(slot="0000:01:00.0", vendor="nvidia", connected_outputs=None)]
    profile.machine.chassis_type = 10
    raw = profile.to_dict()
    assert hw.HardwareProfile.from_dict(raw) == profile
    # unknown keys, wrong types and missing sections must not raise
    messy = {"cpu": {"vendor": 5, "threads": "many", "future_key": 1}, "gpus": [{"vendor": "intel"}, "junk", {"connected_outputs": 2}],
             "disks": None, "machine": {"chassis_type": 9.0}, "extra": {}}
    loaded = hw.HardwareProfile.from_dict(messy)
    assert loaded.cpu.vendor == "" and loaded.cpu.threads == 0
    assert [g.vendor for g in loaded.gpus] == ["intel", "other"] and loaded.gpus[1].connected_outputs == 2
    assert loaded.machine.chassis_type == 9
    assert hw.HardwareProfile.from_dict(None) == hw.HardwareProfile()
    assert hw.HardwareProfile.from_dict("not a mapping") == hw.HardwareProfile()  # type: ignore[arg-type]


def test_redaction_drops_identifiers():
    profile = hw.HardwareProfile(disks=[hw.DiskInfo(name="sda", serial="S1", wwn="W1", ptuuid="P1", model="M")])
    plain = profile.to_dict()
    assert plain["disks"][0]["serial"] == "S1"
    redacted = profile.to_dict(redact=True)
    assert (redacted["disks"][0]["serial"], redacted["disks"][0]["wwn"], redacted["disks"][0]["ptuuid"], redacted["disks"][0]["model"]) == ("", "", "", "M")
    assert profile.disks[0].serial == "S1"  # the original is untouched


def test_every_warning_code_is_in_the_hw_namespace():
    profile = hw.HardwareProfile(
        cpu=hw.CpuInfo(flags=["fpu"]), memory=hw.MemoryInfo(total_kib=512 * 1024),
        firmware=hw.FirmwareInfo(mode="uefi", efi_bits=32, secure_boot="enabled", can_reboot_to_firmware=True, efivars_writable=False, windows_boot_entry=True),
        power=hw.PowerInfo(has_battery=True, ac_online=False, battery_percent=10),
        clock=hw.ClockInfo(offset_s=1200.0, rtc_mode="local", year=2026),
        nics=[hw.NicInfo(kind="wifi", vendor_id="14e4", device_id="43a0", no_driver=True)],
        gpus=[hw.GpuInfo(name="X")],
        machine=hw.MachineInfo(hypervisor="qemu"))
    warnings = hw.derive_warnings(profile)
    assert warnings and all(w.code.startswith("hw.") and w.severity in {"info", "warning", "error"} and w.message for w in warnings)
    codes = {w.code for w in warnings}
    assert {"hw.arch_unsupported", "hw.memory_critical", "hw.fw_sb_enabled", "hw.fw_uefi32", "hw.fw_efivars_ro", "hw.fw_win_entry", "hw.bat_low",
            "hw.clock_skew", "hw.clock_rtc_local", "hw.net_no_driver", "hw.net_broadcom_sta", "hw.gpu_no_driver", "hw.vm_detected", "hw.st_no_disk"} <= codes
    shown = next(w for w in warnings if w.code == "hw.fw_sb_enabled")
    assert shown.action == "reboot_firmware" and shown.severity == "warning"


def test_hybrid_rule_does_not_misclassify_the_authors_desktop():
    igpu = hw.GpuInfo(vendor="intel", integrated=True, boot_vga=False, connected_outputs=0)
    dgpu = hw.GpuInfo(vendor="nvidia", integrated=False, boot_vga=True, connected_outputs=2)
    desktop = hw.HardwareProfile(gpus=[igpu, dgpu], machine=hw.MachineInfo(form_factor="desktop"))
    assert not hw.is_hybrid(desktop)
    laptop = hw.HardwareProfile(gpus=[igpu, dgpu], machine=hw.MachineInfo(form_factor="laptop"))
    assert hw.is_hybrid(laptop)
    tablet = hw.HardwareProfile(gpus=[igpu, dgpu], machine=hw.MachineInfo(form_factor="tablet"))
    assert hw.is_hybrid(tablet)
    # a desktop whose monitors sit on the integrated GPU while a discrete card is installed
    on_igpu = hw.GpuInfo(vendor="intel", integrated=True, boot_vga=True, connected_outputs=1)
    assert hw.is_hybrid(hw.HardwareProfile(gpus=[on_igpu, dgpu], machine=hw.MachineInfo(form_factor="desktop")))
    # a virtual GPU or a BMC chip never makes a machine hybrid
    virtual = hw.GpuInfo(vendor="virtio", virtual=True)
    assert not hw.is_hybrid(hw.HardwareProfile(gpus=[igpu, virtual], machine=hw.MachineInfo(form_factor="laptop")))
    assert not hw.is_hybrid(hw.HardwareProfile(gpus=[dgpu], machine=hw.MachineInfo(form_factor="laptop")))


def test_efibootmgr_entries():
    text = ("BootCurrent: 0001\nTimeout: 1 seconds\nBootOrder: 0001,0000\n"
            "Boot0000* Windows Boot Manager\tHD(1,GPT,aaaa-bbbb,0x800,0x32000)/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)WINDOWS\n"
            "Boot0001* Gentoo\tHD(1,GPT,cccc)/File(\\EFI\\Gentoo\\grubx64.efi)\n"
            "Boot0002  Disabled Entry\tHD(2,GPT,dddd)\n"
            "Boot00ZZ* not an entry\n")
    entries = hw.parse_efibootmgr(text)
    assert [(e.id, e.label, e.active) for e in entries] == [("0000", "Windows Boot Manager", True), ("0001", "Gentoo", True), ("0002", "Disabled Entry", False)]
    assert hw.parse_efibootmgr("") == [] and hw.parse_efibootmgr(None) == []
