"""Hardware-aware advice and defaults for the wizard (pure, no Qt, no I/O).

Two kinds of functions live here:

* numeric formulas (build jobs, swap, minimum root size, display scale) that the
  GUI shows next to its inputs and the planner reuses, and
* ``recommend()`` / ``apply_defaults()``, which turn a ``HardwareProfile`` and
  the current ``InstallManifest`` into advice and pre-selected values.

The planner (``gentoo_installerd.hwplan``) owns every decision. This module asks
it what ``auto`` resolves to (imported lazily: hwplan imports this module for
the formulas), so the GUI and the daemon cannot disagree.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from . import hardware_data as data
from .hardware import HardwareProfile, gpu_label, is_hybrid
from .manifest import InstallManifest, Storage

KIB_PER_GIB = 1024 * 1024
DEFAULT_SWAP_MIB = Storage().swap_mib
LEGACY_MAKE_OPTS = "-j2"  # the old manifest default; "" means automatic
HWMONUX_COMFORTABLE_GIB = 8  # below this, compiling PySide for HWMonux is slow and tight on memory


# ---------------------------------------------------------------------------
# Formulas
# ---------------------------------------------------------------------------

def recommend_jobs(threads: int, available_kib: int, swap_mib: int = 0) -> int:
    """Parallel compile jobs: about 2 GiB per job (LLVM, Rust and Qt links), never above the thread count or 32."""
    budget_gib = available_kib / KIB_PER_GIB + 0.5 * swap_mib / 1024
    return max(1, min(max(threads, 1), 32, int(budget_gib // 2)))


# Qt WebEngine, LLVM and PySide's bindings need about this much memory per compile job, twice what MAKEOPTS assumes.
GIB_PER_HEAVY_JOB = 4


def memory_heavy_jobs(total_kib: int, swap_mib: int = 0) -> int:
    """Compile jobs for the memory-heavy packages: all the memory and half the swap, about 4 GiB per job, at least one."""
    budget_gib = total_kib / KIB_PER_GIB + 0.5 * swap_mib / 1024
    return max(1, int(budget_gib // GIB_PER_HEAVY_JOB))


def make_opts_jobs(make_opts: str) -> int | None:
    """The number of jobs a MAKEOPTS-style text allows: the last ``-jN`` or ``--jobs=N`` wins, as in make.

    None when it names no job count or leaves the jobs unlimited (``-j`` alone, ``-j0``).
    """
    tokens = make_opts.split()
    jobs: int | None = None
    for index, token in enumerate(tokens):
        if token in ("-j", "--jobs"):
            following = tokens[index + 1] if index + 1 < len(tokens) else ""
            value = following if following.isdecimal() else ""
        elif token.startswith("--jobs="):
            value = token[len("--jobs="):]
        elif token.startswith("-j"):
            value = token[2:]
        else:
            continue
        jobs = int(value) if value.isdecimal() and int(value) > 0 else None
    return jobs


def recommend_emerge_jobs(total_kib: int, threads: int) -> int:
    """Packages built at the same time by emerge."""
    total_gib = total_kib / KIB_PER_GIB
    if total_gib >= 60 and threads >= 16:
        return 3
    if total_gib >= 14 and threads >= 8:
        return 2
    return 1


def recommend_make_opts(threads: int, available_kib: int, swap_mib: int = 0) -> str:
    """MAKEOPTS for this machine, e.g. '-j24 -l32'."""
    return f"-j{recommend_jobs(threads, available_kib, swap_mib)} -l{max(threads, 1)}"


def nominal_ram_gib(total_kib: int) -> int:
    """Installed RAM in whole GiB (MemTotal is a few percent below what was fitted)."""
    return max(1, math.ceil(total_kib / KIB_PER_GIB))


def recommend_swap_mib(total_kib: int, *, zram: bool = False, hibernate: bool = False) -> int:
    """Swap size in MiB, following the table of the Gentoo Handbook ("What about swap space?").

    =============  =====================  ==============================
    RAM            ordinary use           with hibernation
    =============  =====================  ==============================
    up to 2 GiB    4 GiB                  3x RAM (at least 4 GiB)
    2 to 8 GiB     as much as the RAM     2x RAM
    8 to 64 GiB    8 GiB (16 GiB > 32)    1.5x RAM
    over 64 GiB    8 GiB                  hibernation is not advised: 8 GiB
    =============  =====================  ==============================

    With compressed RAM swap (zram) the disk only needs a small overflow area.
    """
    ram = nominal_ram_gib(total_kib)
    if zram and not hibernate:
        return 2048 if ram <= 8 else 0
    if ram > HIBERNATION_MAX_GIB:
        return 8192
    if hibernate:
        if ram <= 2:
            return max(4096, 3 * ram * 1024)
        return (2 * ram * 1024) if ram <= 8 else (ram * 3 * 512)
    if ram <= 2:
        return 4096
    if ram <= 8:
        return ram * 1024
    return 8192 if ram <= 32 else 16384


# The Handbook does not advise hibernation above this much memory (the swap would have to be as large as the memory).
HIBERNATION_MAX_GIB = 64


def hibernation_advised(total_kib: int) -> bool:
    """Whether the Handbook table has a hibernation size for this much memory."""
    return nominal_ram_gib(total_kib) <= HIBERNATION_MAX_GIB


def zram_by_default(total_kib: int) -> bool:
    """Compressed RAM swap helps when memory is short: below 8 GiB."""
    return 0 < total_kib < 8 * KIB_PER_GIB


def minimum_root_gib(desktop: str, binhost: bool = True) -> tuple[int, int]:
    """(minimum, recommended) size of the root filesystem in GiB for a desktop choice."""
    minimum, recommended = data.ROOT_SIZES_GIB.get(desktop, data.ROOT_SIZES_GIB["kde"])
    extra = 0 if binhost else data.BUILD_EXTRA_GIB.get(desktop, 16)
    return minimum + extra, recommended + extra


_SCALE_STEPS = (96, 120, 144, 168, 192, 216, 240)


def recommend_dpi(displays: list[Any]) -> int:
    """Interface density in dpi for the primary display (96 means no scaling).

    The primary display is the internal panel, else the first connected one.
    The measured density is rounded *down* to a step (120, 144, ... 240): the
    highest step that is at most 6 dpi above it, so a 111 dpi ultrawide stays
    at 96 and a 14-inch Full HD laptop (158 dpi) gets 144.
    """
    known = sorted((d for d in displays if getattr(d, "dpi", 0) > 0), key=lambda d: not getattr(d, "internal", False))
    measured = known[0].dpi if known else 0
    if not measured:
        return 96
    return max([step for step in _SCALE_STEPS if step <= measured + 6] or [96])


def console_font_hint(displays: list[Any]) -> str:
    """'ter-132b' for very large or dense panels (the default 8x16 console font is unreadable), else ''."""
    for display in displays:
        if getattr(display, "width", 0) >= 3000 or getattr(display, "dpi", 0) >= 180:
            return "ter-132b"
    return ""


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Recommendation:
    code: str  # stable id, "rec.<name>"
    severity: str  # info | warning
    message: str
    field: str = ""  # dotted manifest path the advice is about, e.g. "hardware.target"
    suggestion: str = ""  # value to store when the user accepts ("" for information only)

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "message": self.message, "field": self.field, "suggestion": self.suggestion}


def _profile(profile: HardwareProfile | Mapping[str, Any] | None) -> HardwareProfile:
    if isinstance(profile, HardwareProfile):
        return profile
    return HardwareProfile.from_dict(profile if isinstance(profile, Mapping) else None)


def _target_disk(profile: HardwareProfile, manifest: InstallManifest) -> Any:
    device = manifest.storage.device
    return next((disk for disk in profile.disks if disk.path == device), None)


def _plan(manifest: InstallManifest, profile: HardwareProfile) -> Any:
    from gentoo_installerd import hwplan  # lazy: hwplan imports this module

    return hwplan.plan(manifest, profile.to_dict())


def recommend(profile: HardwareProfile | Mapping[str, Any] | None, manifest: InstallManifest) -> list[Recommendation]:
    """Advice that needs a decision, in plain language. What the installer will do by itself is in the plan summary."""
    hw = _profile(profile)
    out: list[Recommendation] = []
    desktop, hardware = manifest.system.desktop, manifest.hardware
    total_kib = hw.memory.total_kib
    gib = total_kib / KIB_PER_GIB
    disk = _target_disk(hw, manifest)

    # Memory
    if total_kib:
        if gib < 2 and desktop != "minimal":
            out.append(Recommendation("rec.low_memory_desktop", "warning",
                                      f"This PC has {gib:.1f} GiB of memory. A graphical desktop is not recommended; choose Minimal (no desktop) and keep compressed RAM swap on.",
                                      "system.desktop", "minimal"))
        elif gib < 4 and desktop in ("kde", "gnome"):
            out.append(Recommendation("rec.tight_memory", "info",
                                      f"This PC has {gib:.1f} GiB of memory. {desktop.upper()} will feel slow; Xfce needs less.", "system.desktop", "xfce"))
        if manifest.options.install_hwmonux and nominal_ram_gib(total_kib) < HWMONUX_COMFORTABLE_GIB:
            # The factory default stays on: the PC owner decides, with the time and the memory in front of them.
            out.append(Recommendation("rec.hwmonux_low_memory", "info",
                                      f"This PC has {gib:.1f} GiB of memory. HWMonux needs PySide6, which is compiled from source: about 40 minutes and up to 4 GiB of memory "
                                      "per compile job. Turn HWMonux off if you do not need it.", "options.install_hwmonux", "false"))
        if hardware.hibernate and not hibernation_advised(total_kib):
            out.append(Recommendation("rec.hibernate_large_memory", "warning",
                                      f"This PC has {gib:.0f} GiB of memory. The Gentoo Handbook does not advise hibernation above {HIBERNATION_MAX_GIB} GiB: "
                                      "the swap would have to be as large as the memory. Turn hibernation off.", "hardware.hibernate", "false"))
        elif hardware.hibernate:
            wanted = recommend_swap_mib(total_kib, hibernate=True)
            if manifest.storage.swap_mib < wanted:
                out.append(Recommendation("rec.hibernate_swap", "warning",
                                          f"Hibernation writes all memory to swap: the Handbook advises at least {wanted / 1024:.0f} GiB of swap for {gib:.0f} GiB of memory.",
                                          "storage.swap_mib", str(wanted)))
        elif manifest.storage.swap_mib == DEFAULT_SWAP_MIB and manifest.storage.swap_choice == "custom":
            wanted = recommend_swap_mib(total_kib)
            if wanted != DEFAULT_SWAP_MIB:
                out.append(Recommendation("rec.swap_size", "info", f"For {gib:.0f} GiB of memory the Handbook advises {wanted / 1024:.0f} GiB of swap.", "storage.swap_mib", str(wanted)))
        if zram_by_default(total_kib) and hardware.zram != "no" and manifest.storage.swap_kind == "auto" and not hardware.hibernate:
            out.append(Recommendation("rec.swap_zram", "info",
                                      f"With {gib:.0f} GiB of memory, a small compressed-memory swap (zram) is used first and is much faster than swap on the disk. "
                                      "Choose it as the swap type to skip the swap partition.", "storage.swap_kind", "zram"))

    # Build speed: the old fixed default is far too low on a big machine
    if manifest.portage.make_opts == LEGACY_MAKE_OPTS and hw.cpu.threads > 4:
        jobs = recommend_jobs(hw.cpu.threads, hw.memory.available_kib, manifest.storage.swap_mib)
        if jobs > 2:
            out.append(Recommendation("rec.make_opts", "info", f"This PC can compile with -j{jobs}; the default -j2 is slow when packages are built from source.",
                                      "portage.make_opts", recommend_make_opts(hw.cpu.threads, hw.memory.available_kib, manifest.storage.swap_mib)))

    # Target disk
    if disk is not None and disk.external and hardware.target == "this":
        out.append(Recommendation("rec.portable_target", "info",
                                  "The selected disk is removable. If you will start it on other computers, choose 'portable': no CPU tuning, both microcode families, "
                                  "all firmware, and a fallback boot entry.", "hardware.target", "portable"))
    if disk is not None and disk.size and desktop in data.ROOT_SIZES_GIB:
        minimum, recommended = minimum_root_gib(desktop, manifest.portage.binhost)
        size_gib = disk.size / 1024**3
        if minimum <= size_gib < recommended:
            out.append(Recommendation("rec.disk_size", "info", f"{desktop.upper() if desktop != 'minimal' else 'A minimal system'} fits on this disk, "
                                      f"but {recommended} GiB leaves room for updates and applications.", "storage.device"))

    # Graphics
    nvidia = [gpu for gpu in hw.gpus if gpu.vendor == "nvidia"]
    if nvidia and hardware.gpu_driver in ("auto", "nouveau") and not hw.machine.hypervisor:
        branch = data.nvidia_branch_for(nvidia[0].generation)
        if branch:
            out.append(Recommendation("rec.nvidia_proprietary", "info",
                                      f"The NVIDIA proprietary driver (version {branch}) is available for {gpu_label(nvidia[0]) if nvidia[0].name else 'your NVIDIA card'}. "
                                      "The open driver works everywhere; the proprietary one is faster for games and CUDA, but it is non-free and needs you to accept NVIDIA's licence.",
                                      "hardware.gpu_driver", "nvidia"))
        elif nvidia[0].generation in ("kepler", "fermi", "older"):
            out.append(Recommendation("rec.nvidia_legacy", "info", "This NVIDIA card is too old for a supported proprietary driver; the open nouveau driver will be used."))
    if any(gpu.vendor == "intel" and gpu.generation == "gen3" for gpu in hw.gpus) and manifest.portage.binhost:
        out.append(Recommendation("rec.old_intel_graphics", "info",
                                  "This Intel graphics chip would need Mesa compiled from source for hardware 3D. The installer keeps binary packages and uses software rendering; "
                                  "a Minimal or Xfce install suits this PC best."))
    if is_hybrid(hw) and hardware.prime == "off":
        out.append(Recommendation("rec.prime", "info", "This PC has two graphics cards; GPU offload helpers are switched off.", "hardware.prime", "offload"))

    # Laptop and virtual machine
    if hw.machine.form_factor in ("laptop", "tablet") and hardware.power == "none":
        out.append(Recommendation("rec.laptop_power", "info", "This is a laptop; power management extends the battery life.", "hardware.power", "auto"))
    if hw.machine.hypervisor and hardware.guest_tools == "none":
        out.append(Recommendation("rec.guest_tools", "info",
                                  f"Running in {hw.machine.hypervisor}: guest tools give clipboard sharing, screen resizing and clean shutdown.", "hardware.guest_tools", "auto"))
    if (hw.clock.rtc_mode == "local" or hw.firmware.windows_boot_entry) and not manifest.system.rtc_local:
        out.append(Recommendation("rec.rtc_local", "info",
                                  "Windows keeps local time in the hardware clock. For dual boot, enable the local-time clock option so both systems agree.", "system.rtc_local", "true"))

    # Displays
    dpi = recommend_dpi(hw.displays)
    if dpi > 96 and hardware.hidpi == "auto" and desktop == "xfce":
        out.append(Recommendation("rec.hidpi", "info", f"The screen is dense ({dpi} dpi); Xfce will be set to {dpi} dpi so text and buttons are readable.", "hardware.hidpi", f"dpi:{dpi}"))
    return out


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def defaults_for(profile: HardwareProfile | Mapping[str, Any] | None, manifest: InstallManifest, *, resolve_auto: bool = False,
                 include_firmware: bool = True) -> dict[str, Any]:
    """Dotted field -> value for the fields apply_defaults() would change (pure; the manifest is not modified)."""
    hw = _profile(profile)
    changes: dict[str, Any] = {}
    if not (hw.cpu.threads or hw.gpus or hw.disks or hw.memory.total_kib):
        return changes  # no detection data: keep the manifest as it is
    if include_firmware and not hw.errors_for("firmware") and hw.firmware.mode in ("uefi", "bios"):
        changes["storage.firmware"] = hw.firmware.mode
    if hw.memory.total_kib and manifest.storage.swap_mib == DEFAULT_SWAP_MIB and manifest.storage.swap_choice == "custom":
        # A new installation opens on "Recommended for this computer"; the size is stored too, so every screen shows the same number.
        hibernate = manifest.hardware.hibernate and hibernation_advised(hw.memory.total_kib)
        changes["storage.swap_choice"] = "hibernate" if hibernate else "recommended"
        changes["storage.swap_mib"] = recommend_swap_mib(hw.memory.total_kib, hibernate=hibernate)
    if not hw.errors_for("usb") and not hw.bluetooth.present and manifest.options.bluetooth:
        changes["options.bluetooth"] = False
    if hw.machine.hypervisor and manifest.options.microcode:
        changes["options.microcode"] = False  # the hypervisor owns the CPU microcode
    # A disk that moves to other screens keeps the default font; so does a font the user chose.
    font = "" if manifest.system.console_font or manifest.hardware.target == "portable" else console_font_hint(hw.displays)
    if font:
        changes["system.console_font"] = font
    if resolve_auto:
        scalars = _plan(manifest, hw).scalars
        for name, key in (("power", "power"), ("thermald", "thermald"), ("zram", "zram"), ("trim", "trim"), ("sof_firmware", "sof_firmware"),
                          ("nvk", "nvk"), ("prime", "prime"), ("removable_fallback", "removable_fallback"), ("hidpi", "hidpi")):
            if getattr(manifest.hardware, name) == "auto" and scalars.get(key) not in (None, "", "auto"):
                changes[f"hardware.{name}"] = scalars[key]
        if manifest.hardware.gpu_driver == "auto":
            changes["hardware.gpu_driver"] = "nouveau"
    return changes


def apply_defaults(manifest: InstallManifest, profile: HardwareProfile | Mapping[str, Any] | None, *, resolve_auto: bool = False,
                   include_firmware: bool = True) -> None:
    """Pre-select detected values in a fresh manifest. The user can still change every one of them.

    Call it once when the wizard starts (and again after "Check again" on an
    untouched manifest), not on every page change: it seeds values that depend
    only on the machine (firmware type, swap size from RAM, Bluetooth, microcode
    in a VM). ``resolve_auto`` additionally turns the hardware choices that say
    "auto" into the concrete value the planner would pick now (for headless
    manifests; a GUI keeps "auto" so choices follow later edits). VIDEO_CARDS
    is never filled in: the planner leaves it unset to keep binary packages,
    and an explicit value would switch that policy off.
    """
    for dotted, value in defaults_for(profile, manifest, resolve_auto=resolve_auto, include_firmware=include_firmware).items():
        section, _, name = dotted.partition(".")
        setattr(getattr(manifest, section), name, value)
