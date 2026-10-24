"""Small constructors for hardware profiles used by the planner tests (the fixtures cover real machines)."""

from __future__ import annotations

from typing import Any

from gentoo_installer import hardware as hw
from gentoo_installer.manifest import InstallManifest

GIB = 1024**3


def cpu(vendor: str = "intel", threads: int = 8, flags: tuple[str, ...] = ("aes", "avx", "avx2", "mmx", "mmxext", "sse", "sse2", "sse3", "ssse3")) -> hw.CpuInfo:
    return hw.CpuInfo(vendor=vendor, vendor_id={"intel": "GenuineIntel", "amd": "AuthenticAMD"}.get(vendor, "Other"), model_name=f"Test {vendor} CPU",
                      threads=threads, flags=["lm"], x86_64_level=3, cpu_flags_x86=list(flags), cpu_flags_source="cpuid2cpuflags")


def gpu(vendor: str, generation: str = "", *, driver: str = "", integrated: bool = False, boot: bool = False, outputs: int | None = 1, name: str = "",
        device: str = "0000", slot: str = "0000:01:00.0", guess: bool = False) -> hw.GpuInfo:
    virtual = vendor in {"virtio", "qxl", "vmware", "virtualbox", "bochs", "cirrus", "hyperv", "parallels"}
    return hw.GpuInfo(slot=slot, vendor_id={"nvidia": "10de", "amd": "1002", "intel": "8086"}.get(vendor, "1af4"), device_id=device, vendor=vendor,
                      vendor_name=vendor.title(), name=name or f"{vendor} {generation} card", driver=driver or {"nvidia": "nouveau", "amd": "amdgpu", "intel": "i915"}.get(vendor, "x"),
                      boot_vga=boot, generation=generation, generation_guess=guess, integrated=integrated, virtual=virtual, connected_outputs=outputs)


def disk(path: str = "/dev/nvme0n1", *, bus: str = "nvme", rota: bool | None = False, size: int = 512 * GIB, hotplug: bool = False, log_sec: int = 512,
         serial: str = "S1", installable: bool = True, **extra: Any) -> hw.DiskInfo:
    name = path.rsplit("/", 1)[1]
    return hw.DiskInfo(name=name, path=path, size=size, bus=bus, rota=rota, hotplug=hotplug, removable=hotplug, log_sec=log_sec, phy_sec=log_sec,
                       serial=serial, installable=installable, **extra)


def profile(*, vendor: str = "intel", form: str = "desktop", hypervisor: str = "", gpus: tuple[hw.GpuInfo, ...] = (), disks: tuple[hw.DiskInfo, ...] | None = None,
            ram_gib: float = 16, threads: int = 8, firmware: str = "uefi", wifi: bool = False, bluetooth: bool = False, battery: bool | None = None,
            audio_vendors: tuple[str, ...] = (), needs_sof: bool = False, flags: tuple[str, ...] | None = None, displays: tuple[hw.DisplayInfo, ...] = (),
            nics: tuple[hw.NicInfo, ...] = (), lockdown: str = "") -> dict[str, Any]:
    """HardwareProfile.to_dict() for a made-up machine."""
    cpu_info = cpu(vendor, threads) if flags is None else cpu(vendor, threads, flags)
    has_battery = form in ("laptop", "tablet") if battery is None else battery
    profile_ = hw.HardwareProfile(
        cpu=cpu_info,
        memory=hw.MemoryInfo(total_kib=int(ram_gib * 1024 * 1024), available_kib=int(ram_gib * 0.9 * 1024 * 1024)),
        machine=hw.MachineInfo(form_factor="vm" if hypervisor else form, hypervisor=hypervisor),
        firmware=hw.FirmwareInfo(mode=firmware, efi_bits=64 if firmware == "uefi" else 0, lockdown=lockdown),
        gpus=list(gpus), displays=list(displays),
        disks=[disk()] if disks is None else list(disks),
        nics=[*nics, *([hw.NicInfo(iface="wlan0", kind="wifi", driver="iwlwifi", bus="pci")] if wifi else [])],
        power=hw.PowerInfo(has_battery=has_battery),
        bluetooth=hw.BluetoothInfo(present=bluetooth),
        audio=hw.AudioInfo(cards=[hw.AudioCard(vendor_id=v) for v in audio_vendors], needs_sof=needs_sof),
        hypervisor=hw.HypervisorInfo(bool(hypervisor), hypervisor))
    return profile_.to_dict()


def manifest(**sections: dict[str, Any]) -> InstallManifest:
    """A valid manifest with GUI defaults (KDE, OpenRC, binhost) on /dev/nvme0n1, plus overrides per section."""
    result = InstallManifest()
    result.storage.device = "/dev/nvme0n1"
    result.storage.device_id = "S1"
    result.accounts.password = result.accounts.root_password = "secret-pass"
    for section, values in sections.items():
        for name, value in values.items():
            setattr(getattr(result, section), name, value)
    return result


def systemd(**sections: dict[str, Any]) -> InstallManifest:
    sections.setdefault("system", {}).update({"init": "systemd", "stage_variant": "amd64-desktop-systemd", "profile": "default/linux/amd64/23.0/desktop/plasma/systemd"})
    return manifest(**sections)
