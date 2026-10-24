"""Hardware plan: every hardware-dependent target setting, decided in one place.

``plan(manifest, hardware)`` is a pure function of the install manifest and a
hardware profile (``HardwareProfile.to_dict()``). It returns packages, USE
flags, licences, make.conf values, files, services, kernel arguments and
chroot steps; ``configure.py`` and ``install.py`` only apply them and never
re-derive a hardware decision. ``validate_plan`` returns the cross-checks that
need the probed machine (the manifest only checks structure).

Policy in brief
---------------
* Prefer the binary host. VIDEO_CARDS and CPU_FLAGS_X86 are left to the profile
  unless a choice cannot be made without changing them (proprietary NVIDIA only
  appends ``nvidia``, which no Mesa/libdrm flag depends on). Empty values are
  never written, so profile defaults survive.
* Never widen ACCEPT_LICENSE. A licence line is written for an atom only when
  the option that needs it is in effect.
* Hardware-optional steps degrade instead of aborting: microcode and firmware
  are ``early_atoms``; GPU kernel modules are ``driver_atoms`` (own emerge,
  failure leaves nouveau); power management, guest tools, printer drivers and
  audio firmware are ``optional_atoms`` (one emerge each).
* ``portable`` targets are tuned for no machine: no CPU flags, both microcode
  families, all firmware, a fallback boot path.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from gentoo_installer import hardware_data as data
from gentoo_installer import keyboard_data, recommend
from gentoo_installer.hardware import DiskInfo, GpuInfo, HardwareProfile, gpu_label, is_hybrid, parent_disk_name
from gentoo_installer.manifest import InstallManifest

PLAN_SCHEMA = 1
PORTAGE_FILE = "installer-hardware"  # name inside /etc/portage/package.{use,license,mask}/

# -- atoms (every one is checked against the pinned tree by tests/hardware/test_tree_evidence.py) --------------------
A_INTEL_UCODE = "sys-firmware/intel-microcode"
A_FIRMWARE = "sys-kernel/linux-firmware"
A_SOF = "sys-firmware/sof-firmware"
A_NVIDIA = data.NVIDIA_ATOM
A_PPD = "sys-power/power-profiles-daemon"
A_TLP = "sys-power/tlp"
A_THERMALD = "sys-power/thermald"
A_SWITCHEROO = "sys-power/switcheroo-control"
A_FPRINTD = "sys-auth/fprintd"
A_FWUPD = "sys-apps/fwupd"
A_QEMU_GA = "app-emulation/qemu-guest-agent"
A_SPICE = "app-emulation/spice-vdagent"
A_VMTOOLS = "app-emulation/open-vm-tools"
A_VBOX_GA = "app-emulation/virtualbox-guest-additions"
A_ZRAM_INIT = "sys-block/zram-init"
A_ZRAM_GENERATOR = "sys-apps/zram-generator"
A_GUTENPRINT = "net-print/gutenprint"
A_HPLIP = "net-print/hplip"
A_CUPS = "net-print/cups"
A_CRONIE = "sys-process/cronie"
A_PIPEWIRE = "media-video/pipewire"
A_MESA = "media-libs/mesa"
A_AVAHI = "net-dns/avahi"
A_HWMONUX = "app-misc/hwmonux"
A_NOTO = "media-fonts/noto"
A_NOTO_CJK = "media-fonts/noto-cjk"

# Packages the installer can pull in that need far more memory per compile job than the machine-wide MAKEOPTS assumes (about
# 2 GiB per job). The binary host installs them ready-made; compiled, a few big C++ compiles or links at once exhaust the memory
# (the first VM acceptance run: the compiler of dev-python/pyside was killed on a 4 GiB machine). Every atom is in the pinned tree
# and reached by a package set, and the ebuilds of all but pyside say why they are heavy (tests/hardware/test_plan_memory.py).
# Left out on purpose: dev-lang/rust (rust.eclass prefers rust-bin) and sys-devel/gcc (in the stage3; a job needs little memory).
MEMORY_HEAVY_ATOMS = (
    "dev-python/pyside",     # HWMonux's Qt bindings: unity builds of the Qt modules
    "dev-qt/qtwebengine",    # KDE (kdeplasma-addons, plasma-nm, kmail ...): the ebuild itself refuses a build below 2.5 GB per job
    "llvm-core/llvm",        # mesa[llvm] and pyside: "very high memory consumption while linking"
    "llvm-core/clang",       # built beside LLVM; pyside needs it
    "net-libs/webkit-gtk",   # GNOME (yelp, epiphany, evolution-data-server): the ebuild trims the linker's memory
)
MEMORY_ENV_NAME = "installer-memory"  # /etc/portage/env/<name>, named by /etc/portage/package.env/installer-hardware

LIC_FIRMWARE = "linux-fw-redistributable"
LIC_INTEL_UCODE = "intel-ucode"

# Kernel drivers whose adapters want a firmware file from linux-firmware to work properly.
FIRMWARE_NIC_DRIVERS = frozenset({"r8169", "bnx2", "bnx2x", "tg3", "qede", "qed"})
DIST_KERNELS = frozenset({"gentoo-kernel-bin", "gentoo-kernel"})


@dataclass(frozen=True)
class Issue:
    """A finding about the combination of manifest and hardware. ``code`` is in the ``hw.`` namespace."""

    code: str
    severity: str  # error | warning | info
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "message": self.message}


@dataclass
class HardwarePlan:
    """Resolved hardware-dependent decisions (see docs/contracts.md for the meaning of each field)."""

    early_atoms: list[str] = field(default_factory=list)
    driver_atoms: list[str] = field(default_factory=list)
    required_atoms: list[str] = field(default_factory=list)
    optional_atoms: list[str] = field(default_factory=list)
    package_use: dict[str, list[str]] = field(default_factory=dict)
    package_license: dict[str, list[str]] = field(default_factory=dict)
    package_mask: list[str] = field(default_factory=list)
    make_conf: dict[str, str] = field(default_factory=dict)
    files: list[tuple[str, str, int]] = field(default_factory=list)
    services: list[tuple[str, str]] = field(default_factory=list)
    kernel_cmdline: list[str] = field(default_factory=list)
    post_steps: list[list[str]] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)
    # Additive to the contract:
    optional_services: list[tuple[str, str]] = field(default_factory=list)  # enabled only if the service exists (its package is optional)
    scalars: dict[str, Any] = field(default_factory=dict)  # resolved choices, JSON-safe
    notes: list[str] = field(default_factory=list)  # why something non-obvious was decided
    warnings: list[Issue] = field(default_factory=list)  # decisions that degrade or surprise
    schema: int = PLAN_SCHEMA

    # -- convenience for the consumers -------------------------------------------------------------------------------

    def skel_files(self) -> list[tuple[str, str, int]]:
        """Files under /etc/skel: write them before the first user is created, or useradd will not copy them."""
        return [item for item in self.files if item[0].startswith("/etc/skel/")]

    def system_files(self) -> list[tuple[str, str, int]]:
        return [item for item in self.files if not item[0].startswith("/etc/skel/")]

    def services_to_enable(self, exists: Callable[[str], bool]) -> list[tuple[str, str]]:
        """Services to enable on the target: ``services`` always, ``optional_services`` only when ``exists(name)``.

        An optional service belongs to a best-effort package; when that emerge
        failed its init script or unit is absent and must be skipped, not fail the install.
        """
        return [*self.services, *(item for item in self.optional_services if exists(item[0]))]

    def all_atoms(self) -> list[str]:
        return data.distinct([*self.early_atoms, *self.driver_atoms, *self.required_atoms, *self.optional_atoms])

    def to_dict(self, *, redact_files: bool = False) -> dict[str, Any]:
        """JSON-safe form. ``redact_files`` keeps path, mode, size and sha256 instead of the content (for logs and receipts)."""
        files: list[Any]
        if redact_files:
            files = [{"path": path, "mode": mode, "size": len(content.encode()), "sha256": hashlib.sha256(content.encode()).hexdigest()}
                     for path, content, mode in self.files]
        else:
            files = [[path, content, mode] for path, content, mode in self.files]
        return {
            "schema": self.schema,
            "early_atoms": list(self.early_atoms), "driver_atoms": list(self.driver_atoms),
            "required_atoms": list(self.required_atoms), "optional_atoms": list(self.optional_atoms),
            "package_use": {atom: list(flags) for atom, flags in sorted(self.package_use.items())},
            "package_license": {atom: list(items) for atom, items in sorted(self.package_license.items())},
            "package_mask": list(self.package_mask),
            "make_conf": dict(sorted(self.make_conf.items())),
            "files": files,
            "services": [list(item) for item in self.services], "optional_services": [list(item) for item in self.optional_services],
            "kernel_cmdline": list(self.kernel_cmdline),
            "post_steps": [list(step) for step in self.post_steps],
            "summary": list(self.summary), "notes": list(self.notes),
            "warnings": [issue.to_dict() for issue in self.warnings],
            "scalars": dict(sorted(self.scalars.items())),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> HardwarePlan:
        def strings(key: str) -> list[str]:
            return [str(item) for item in raw.get(key, []) if isinstance(item, str)]

        def pairs(key: str) -> list[tuple[str, str]]:
            return [(str(item[0]), str(item[1])) for item in raw.get(key, []) if isinstance(item, (list, tuple)) and len(item) == 2]

        files = [(str(item[0]), str(item[1]), int(item[2])) for item in raw.get("files", []) if isinstance(item, (list, tuple)) and len(item) == 3]
        return cls(
            early_atoms=strings("early_atoms"), driver_atoms=strings("driver_atoms"), required_atoms=strings("required_atoms"),
            optional_atoms=strings("optional_atoms"),
            package_use={str(k): [str(x) for x in v] for k, v in dict(raw.get("package_use", {})).items()},
            package_license={str(k): [str(x) for x in v] for k, v in dict(raw.get("package_license", {})).items()},
            package_mask=strings("package_mask"), make_conf={str(k): str(v) for k, v in dict(raw.get("make_conf", {})).items()},
            files=files, services=pairs("services"), optional_services=pairs("optional_services"), kernel_cmdline=strings("kernel_cmdline"),
            post_steps=[[str(x) for x in step] for step in raw.get("post_steps", []) if isinstance(step, (list, tuple))],
            summary=strings("summary"), notes=strings("notes"),
            warnings=[Issue(str(w.get("code", "")), str(w.get("severity", "info")), str(w.get("message", "")))
                      for w in raw.get("warnings", []) if isinstance(w, Mapping)],
            scalars=dict(raw.get("scalars", {})))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _profile(hardware: Mapping[str, Any] | HardwareProfile | None) -> HardwareProfile:
    return hardware if isinstance(hardware, HardwareProfile) else HardwareProfile.from_dict(hardware if isinstance(hardware, Mapping) else None)


def _physical(gpus: Sequence[GpuInfo]) -> list[GpuInfo]:
    return [gpu for gpu in gpus if not gpu.virtual and gpu.vendor not in data.BMC_GPU_VENDORS]


def _target_disks(manifest: InstallManifest, hw: HardwareProfile) -> list[DiskInfo]:
    """Disks the installation writes to: the chosen disk, plus the disks of manual-mode partitions."""
    devices = [manifest.storage.device, *(p.device for p in manifest.storage.partitions)]
    wanted = {parent_disk_name(device.rsplit("/", 1)[-1]) for device in devices if device.startswith("/dev/")}
    return [disk for disk in hw.disks if disk.name in wanted or any(device == f"/dev/{part.name}" for device in devices for part in disk.partitions)]


def _typed_flags(text: str) -> tuple[str, list[str]]:
    """(valid CPU_FLAGS_X86 values typed by the user, tokens that were ignored); 'auto' and an empty field mean "decide for me"."""
    tokens = [token for token in text.split() if token.lower() != "auto"]
    valid = [token for token in dict.fromkeys(tokens) if token in data.CPU_FLAGS_X86_VALUES]
    return " ".join(valid), [token for token in dict.fromkeys(tokens) if token not in data.CPU_FLAGS_X86_VALUES]


def _is_mobile(hw: HardwareProfile) -> bool:
    return hw.machine.form_factor in ("laptop", "tablet")


def _detected(hw: HardwareProfile) -> bool:
    """False for an empty profile (no probe ran): firmware and disk checks would only produce noise."""
    return bool(hw.cpu.threads or hw.gpus or hw.disks or hw.memory.total_kib)


@dataclass
class NvidiaDecision:
    branch: str  # "" when the proprietary path is off or impossible
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)


def nvidia_decision(manifest: InstallManifest, hw: HardwareProfile) -> NvidiaDecision:
    """Whether the proprietary NVIDIA driver can be installed, and which branch. Shared by plan() and validate_plan()."""
    choice = manifest.hardware
    if choice.gpu_driver != "nvidia":
        return NvidiaDecision("")
    errors: list[Issue] = []
    warnings: list[Issue] = []
    portable = choice.target == "portable"
    gpus = [gpu for gpu in _physical(hw.gpus) if gpu.vendor == "nvidia"]
    if manifest.system.kernel not in DIST_KERNELS or manifest.system.initramfs != "dracut":
        errors.append(Issue("hw.nvidia_kernel", "error", "The proprietary NVIDIA driver needs a Gentoo distribution kernel (gentoo-kernel or gentoo-kernel-bin) and Dracut: "
                            "its modules are built against the installed kernel and added to the initramfs."))
    if manifest.system.secure_boot:
        errors.append(Issue("hw.nvidia_secure_boot", "error", "The proprietary NVIDIA driver cannot be used together with Secure Boot signing: its kernel modules would be unsigned "
                            "and refused. Turn Secure Boot signing off or use the open driver."))
    if not portable and not gpus and _detected(hw):  # a probe that found nothing at all cannot tell
        errors.append(Issue("hw.nvidia_no_gpu", "error", "The proprietary NVIDIA driver was selected but no NVIDIA graphics card was found in this PC. "
                            "Choose the open driver, or select 'portable' if the disk will run on another computer."))
    explicit = choice.nvidia_branch
    if explicit != "auto" and explicit not in data.NVIDIA_BRANCHES:
        errors.append(Issue("hw.nvidia_branch_unavailable", "error", f"NVIDIA driver branch {explicit} is not available in this installer. Choose one of: "
                            + ", ".join(sorted(data.NVIDIA_BRANCHES)) + "."))
    generations = [gpu.generation for gpu in gpus]
    supported = [generation for generation in generations if generation in ("turing+", "maxwell-volta")]
    if explicit in data.NVIDIA_BRANCHES:
        branch = explicit
    elif "maxwell-volta" in supported:
        branch = "580"  # the long-term branch also drives any Turing or newer card in the same PC
    elif supported:
        branch = "595"
    elif generations and not portable:
        branch = ""
    else:
        branch = "595"
    if gpus and not portable:
        covered: Sequence[str] = data.NVIDIA_BRANCHES[branch]["generations"] if branch else ()  # type: ignore[assignment]
        for gpu in gpus:
            if gpu.generation not in covered:
                errors.append(Issue("hw.nvidia_unsupported_gpu", "error",
                                    f"{gpu_label(gpu) if gpu.name else 'This NVIDIA card'} is not supported by"
                                    + (f" driver branch {branch}" if branch else " any proprietary driver offered here")
                                    + ". " + ("Choose branch 580 (the last for Maxwell, Pascal and Volta cards) or the open driver."
                                              if branch == "595" and gpu.generation == "maxwell-volta" else "The open nouveau driver supports it.")))
            elif gpu.generation_guess:
                warnings.append(Issue("hw.nvidia_generation_guess", "warning",
                                      f"The generation of the NVIDIA card {gpu.vendor_id}:{gpu.device_id} is guessed from its id (it is newer than the device database)."))
    if errors:
        return NvidiaDecision("", errors, warnings)
    return NvidiaDecision(branch, errors, warnings)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

class _Planner:
    def __init__(self, manifest: InstallManifest, hw: HardwareProfile) -> None:
        self.m = manifest
        self.hw = hw
        self.p = HardwarePlan()
        self.init = manifest.system.init
        self.desktop = manifest.system.desktop
        self.binhost = manifest.portage.binhost
        self.portable = manifest.hardware.target == "portable"
        self.vm = bool(hw.machine.hypervisor)
        self.mobile = _is_mobile(hw)
        self.gpus = _physical(hw.gpus)
        self.nvidia_gpus = [gpu for gpu in self.gpus if gpu.vendor == "nvidia"]
        self.proprietary = ""  # nvidia branch when the proprietary path is active
        self.firmware_reasons: list[str] = []
        self.fonts: tuple[str, str] | None = None  # (atom, what it covers) when the locale needs a font package

    # -- accumulation helpers ---------------------------------------------------------------------------------------

    def use(self, atom: str, *flags: str) -> None:
        current = self.p.package_use.setdefault(atom, [])
        current.extend(flag for flag in flags if flag not in current)

    def license(self, atom: str, *licenses: str) -> None:
        current = self.p.package_license.setdefault(atom, [])
        current.extend(item for item in licenses if item not in current)

    def service(self, name: str, level: str = "default", *, optional: bool = False) -> None:
        target = self.p.optional_services if optional else self.p.services
        if (name, level) not in target:
            target.append((name, level))

    def file(self, path: str, content: str, mode: int = 0o644) -> None:
        self.p.files = [item for item in self.p.files if item[0] != path] + [(path, content, mode)]

    def warn(self, code: str, severity: str, message: str) -> None:
        self.p.warnings.append(Issue("hw." + code, severity, message))

    def note(self, message: str) -> None:
        self.p.notes.append(message)

    def post(self, *argv: str) -> None:
        self.p.post_steps.append(list(argv))

    # -- decisions ---------------------------------------------------------------------------------------------------

    def run(self) -> HardwarePlan:
        p, hw = self.p, self.hw
        p.scalars.update({"target": self.m.hardware.target, "cpu_vendor": hw.cpu.vendor, "form_factor": hw.machine.form_factor,
                          "hypervisor": hw.machine.hypervisor, "gpu_vendors": [g.vendor for g in hw.gpus], "init": self.init})
        if not (hw.cpu.threads or hw.gpus or hw.disks):
            self.note("Hardware detection was not available, so safe defaults are used.")
        amd_microcode = self.microcode()
        self.graphics()
        self.firmware(amd_microcode)
        self.sof()
        self.cpu_flags()
        self.power()
        self.storage()
        self.memory()
        self.heavy_builds()
        self.guest_tools()
        self.peripherals()
        self.audio()
        self.boot()
        self.display()
        self.locale()
        self.companions()
        self.finish()
        return p

    # CPU: microcode and flags ----------------------------------------------------------------------------------------

    def microcode(self) -> bool:
        """Choose microcode packages by CPU vendor (NOBS-1). Returns True when AMD microcode (linux-firmware) is wanted."""
        vendor = self.hw.cpu.vendor
        families: set[str] = set()
        if self.m.options.microcode:
            if self.portable:
                families = {"intel", "amd"}
                self.note("Portable install: both Intel and AMD microcode are installed.")
            elif self.vm:
                self.note("CPU microcode skipped: the hypervisor of this virtual machine owns it.")
            elif vendor in ("intel", "amd"):
                families = {vendor}
            elif vendor in ("hygon", "other"):
                self.note("CPU microcode skipped: no microcode package exists for this processor vendor.")
            else:
                families = {"intel", "amd"}
                self.note("The CPU vendor could not be detected, so both Intel and AMD microcode are installed.")
        if "intel" in families:
            self.p.early_atoms.append(A_INTEL_UCODE)
            self.license(A_INTEL_UCODE, LIC_INTEL_UCODE)
            self.use(A_INTEL_UCODE, "initramfs")
        self.p.scalars["microcode"] = "both" if len(families) == 2 else (next(iter(families)) if families else "none")
        return "amd" in families

    def cpu_flags(self) -> None:
        choice = self.m.hardware.cpu_flags_mode
        typed, ignored = _typed_flags(self.m.portage.cpu_flags)
        if ignored:
            self.warn("cpu_flags_ignored", "warning", "These CPU flags are not valid CPU_FLAGS_X86 values and were ignored: " + " ".join(ignored) + ".")
        flags = self.hw.cpu.cpu_flags_x86
        value, mode = "", "profile"
        if choice == "manual" or (choice == "auto" and typed):
            value, mode = typed, "manual"
            if choice == "manual" and not typed:
                self.warn("cpu_flags_empty", "warning", "CPU flags are set to 'manual' but none were entered; the profile defaults are used.")
        elif choice == "detect" or (choice == "auto" and not self.binhost):
            if self.portable:
                self.note("Portable install: CPU_FLAGS_X86 is left at the profile default so the system runs on any PC.")
            elif flags:
                value, mode = " ".join(flags), "detect"
            else:
                self.warn("cpu_flags_unavailable", "warning", "The CPU flags of this PC could not be read; the profile defaults are used.")
        if value:
            self.p.make_conf["CPU_FLAGS_X86"] = value
        self.p.scalars["cpu_flags_mode"] = mode

    # Firmware ---------------------------------------------------------------------------------------------------------

    def firmware(self, amd_microcode: bool) -> None:
        reasons = self.firmware_reasons = self.firmware_needs()
        explicit = self.m.options.firmware
        if explicit or reasons or amd_microcode:
            self.p.early_atoms.append(A_FIRMWARE)
            self.license(A_FIRMWARE, LIC_FIRMWARE)
            self.use(A_FIRMWARE, *(["initramfs", "redistributable"] if amd_microcode else ["redistributable"]))
            if reasons and not explicit:
                self.note("Linux firmware was added because this hardware needs it: " + ", ".join(reasons) + ".")
        self.p.scalars["firmware_forced"] = bool(reasons) and not explicit
        self.p.scalars["firmware_reasons"] = reasons

    def firmware_needs(self) -> list[str]:
        reasons = ["a portable install can meet any hardware"] if self.portable else []
        if self.vm:
            return reasons
        if any(gpu.vendor == "amd" for gpu in self.gpus):
            reasons.append("AMD graphics")
        if self.nvidia_gpus and not self.proprietary and any(gpu.generation == "turing+" for gpu in self.nvidia_gpus):
            reasons.append("NVIDIA GPU system processor firmware")
        if any(gpu.vendor == "intel" and gpu.generation == "gen8+" for gpu in self.gpus):
            reasons.append("Intel graphics")
        if any(nic.kind == "wifi" for nic in self.hw.nics):
            reasons.append("Wi-Fi")
        if self.hw.bluetooth.present:
            reasons.append("Bluetooth")
        if any(nic.driver in FIRMWARE_NIC_DRIVERS for nic in self.hw.nics):
            reasons.append("Ethernet adapter")
        return reasons

    def sof(self) -> None:
        choice = self.m.hardware.sof_firmware
        audio_vendors = {card.vendor_id for card in self.hw.audio.cards}
        if choice == "yes":
            want = True
        elif choice == "no" or self.vm:
            want = False
        else:
            want = self.hw.audio.needs_sof or self.portable or (self.mobile and bool(audio_vendors & {"8086", "1022"}) and self.m.options.firmware)
        if want:
            self.p.optional_atoms.append(A_SOF)
        self.p.scalars["sof_firmware"] = "yes" if want else "no"

    # Graphics -------------------------------------------------------------------------------------------------------

    def graphics(self) -> None:
        decision = nvidia_decision(self.m, self.hw)
        self.p.warnings.extend(decision.warnings)
        self.proprietary = decision.branch
        if self.m.hardware.gpu_driver == "nvidia" and not decision.branch:
            self.warn("nvidia_skipped", "warning", "The proprietary NVIDIA driver was not installed because it does not fit this PC or these settings "
                      "(" + ", ".join(issue.code.removeprefix("hw.") for issue in decision.errors) + "). The open driver is used.")
        nvk = self.nvk()
        if self.proprietary:
            self.nvidia_driver(self.proprietary)
        self.video_cards(nvk)
        self.p.scalars["gpu_driver"] = "nvidia" if self.proprietary else "open"
        self.p.scalars["nvidia_branch"] = self.proprietary
        self.p.scalars["hybrid"] = is_hybrid(self.hw)
        self.prime()

    def nvk(self) -> bool:
        """NVK (Mesa's Vulkan driver for NVIDIA GPUs) is part of a source build for Turing and newer, or on request."""
        choice = self.m.hardware.nvk
        if self.proprietary or choice == "no":
            result = False
        elif choice == "yes":
            result = bool(self.nvidia_gpus) or self.portable
        else:
            result = not self.binhost and not self.portable and any(g.generation == "turing+" for g in self.nvidia_gpus)
        if result:
            self.use(A_MESA, "vulkan")  # video_cards_nvk? ( vulkan video_cards_nouveau )
        self.p.scalars["nvk"] = "yes" if result else "no"
        return result

    def nvidia_driver(self, branch: str) -> None:
        info = data.NVIDIA_BRANCHES[branch]
        self.p.driver_atoms.append(A_NVIDIA)
        self.license(A_NVIDIA, str(info["license"]))
        flags = ["dist-kernel"]  # modules follow kernel updates
        if self.desktop in ("kde", "gnome"):
            flags.append("wayland")
        if branch == "580" and self.nvidia_gpus and all(g.generation == "turing+" for g in self.nvidia_gpus):
            flags.append("kernel-open")  # open modules on Turing and newer; Maxwell to Volta need the closed ones
        self.use(A_NVIDIA, *flags)
        if info["mask"]:
            self.p.package_mask.append(str(info["mask"]))
        # Explicit and harmless: both are the driver defaults since 595, and the packaged modprobe.d file
        # (which also blacklists nouveau) sets modeset on 580.
        self.p.kernel_cmdline.extend(["nvidia-drm.modeset=1", "nvidia-drm.fbdev=1"])
        # The initramfs must contain the driver's modprobe.d file or nouveau can bind first. Rebuilt only when
        # the driver really installed, so a failed build leaves a bootable nouveau system.
        self.post("sh", "-c", "if [ -f /etc/modprobe.d/nvidia.conf ]; then dracut --force --regenerate-all; fi")
        self.note(f"NVIDIA proprietary driver {info['label']}; if it fails to build, the open nouveau driver stays in use.")

    def video_cards(self, nvk: bool) -> None:
        explicit = self.m.portage.video_cards.strip()
        if explicit.lower() == "auto":
            explicit = ""
        if explicit:
            self.p.make_conf["VIDEO_CARDS"] = explicit
            return
        tokens: list[str] = []
        source_build = not self.binhost and not self.portable
        if source_build:
            for gpu in self.gpus:
                tokens.extend(self.gpu_tokens(gpu, nvk))
            for gpu in self.hw.gpus:
                if gpu.vendor == "virtio" and gpu.virtual:
                    tokens.append("virgl")
        tokens = data.distinct(tokens)
        if self.proprietary:  # `nvidia` is no flag of Mesa or libdrm, so the profile list can stay for binary packages
            self.p.make_conf["VIDEO_CARDS"] = " ".join([*tokens, "nvidia"]) if source_build else "${VIDEO_CARDS} nvidia"
        elif nvk and (self.binhost or self.portable):
            self.p.make_conf["VIDEO_CARDS"] = "${VIDEO_CARDS} nvk"
            self.note("NVK was requested: Mesa is compiled instead of downloaded.")
        elif tokens:
            self.p.make_conf["VIDEO_CARDS"] = " ".join(tokens)
        if any(gpu.vendor == "intel" and gpu.generation == "gen3" for gpu in self.gpus) and self.binhost:
            self.note("This old Intel graphics chip needs Mesa compiled from source for hardware 3D; binary packages and software rendering are used instead.")

    def gpu_tokens(self, gpu: GpuInfo, nvk: bool) -> list[str]:
        if gpu.vendor == "intel":
            return ["intel", "i915"] if gpu.generation == "gen3" else ["intel"]
        if gpu.vendor == "amd":
            return {"gcn3+": ["amdgpu", "radeonsi"], "gcn1-2": ["amdgpu", "radeonsi", "radeon"], "terascale": ["radeon", "r600"], "r300": ["radeon", "r300"]}.get(gpu.generation, [])
        if gpu.vendor == "nvidia":
            return [] if self.proprietary else ["nouveau", *(["nvk"] if nvk else [])]
        return []

    def prime(self) -> None:
        choice = self.m.hardware.prime
        hybrid = is_hybrid(self.hw)
        on = hybrid if choice == "auto" else (choice == "offload" and len(self.gpus) > 1)
        self.p.scalars["prime"] = "offload" if on else "off"
        if not on:
            return
        self.p.optional_atoms.append(A_SWITCHEROO)
        self.service("switcheroo-control", optional=True)
        if self.proprietary:
            self.file("/usr/local/bin/prime-run", PRIME_RUN, 0o755)

    # Power ------------------------------------------------------------------------------------------------------------

    def power(self) -> None:
        choice = self.m.hardware.power
        if choice == "auto":
            choice = "none" if (not self.mobile or self.vm or self.portable) else ("ppd" if self.desktop in ("kde", "gnome") else "tlp")
        self.p.scalars["power"] = choice
        if choice == "ppd":
            self.p.optional_atoms.append(A_PPD)
            self.service(A_PPD.split("/")[1], optional=True)
        elif choice == "tlp":
            self.p.optional_atoms.append(A_TLP)
            ppd_ui = self.desktop in ("kde", "gnome")
            flags = [] if ppd_ui else ["-ppd"]  # tlp[ppd] replaces power-profiles-daemon for the desktop's profile switcher
            if self.m.network.manager != "networkmanager":
                flags.append("-rdw")  # rdw needs NetworkManager
            if flags:
                self.use(A_TLP, *flags)
            self.service("tlp", optional=True)
            if ppd_ui:
                self.service("tlp-pd", optional=True)
        thermald = self.m.hardware.thermald
        want = (thermald == "yes") or (thermald == "auto" and self.hw.cpu.vendor == "intel" and self.mobile and not self.vm and not self.portable)
        self.p.scalars["thermald"] = "yes" if want else "no"
        if want:
            self.p.optional_atoms.append(A_THERMALD)
            self.service("thermald", optional=True)
        if self.m.hardware.fingerprint:
            self.p.optional_atoms.append(A_FPRINTD)
        if self.m.hardware.fwupd:
            self.p.optional_atoms.append(A_FWUPD)

    # Storage: TRIM and swap ------------------------------------------------------------------------------------------

    def storage(self) -> None:
        choice, luks = self.m.hardware.trim, self.m.storage.luks
        disks = _target_disks(self.m, self.hw)
        flash = [d for d in disks if d.rota is False and not d.external]  # not USB sticks or SD cards: TRIM is often unsupported there
        if choice == "weekly":
            weekly = True
        elif choice == "off":
            weekly = False
        else:
            weekly = bool(flash) and (not luks or self.m.hardware.luks_discard) and not self.portable
        self.p.scalars["trim"] = "weekly" if weekly else "off"
        if luks and weekly and not self.m.hardware.luks_discard:
            self.note("TRIM on an encrypted disk only works when discards are allowed through LUKS (see the security note on that option).")
        if weekly:
            if self.init == "systemd":
                self.service("fstrim.timer")
            else:
                self.file("/etc/cron.weekly/fstrim", FSTRIM_CRON, 0o755)
                if not self.m.options.cron:
                    self.p.required_atoms.append(A_CRONIE)
                    self.service("cronie")
                    self.note("A cron daemon was added to run the weekly TRIM.")
        self.p.scalars["btrfs_discard_async"] = bool(weekly and flash and self.m.storage.filesystem == "btrfs")
        self.p.scalars["luks_allow_discards"] = bool(luks and self.m.hardware.luks_discard)

    def memory(self) -> None:
        choice = self.m.hardware.zram
        total_kib = self.hw.memory.total_kib
        if choice == "auto":
            want = recommend.zram_by_default(total_kib) and not (self.portable and self.init == "openrc")
        else:
            want = choice == "yes"
        self.p.scalars["zram"] = "yes" if want else "no"
        if not want:
            return
        percent = self.m.hardware.zram_percent
        size_mib = max(256, min(total_kib // 1024 * percent // 100, 16384)) if total_kib else 1024
        if self.init == "systemd":
            self.p.optional_atoms.append(A_ZRAM_GENERATOR)
            self.file("/etc/systemd/zram-generator.conf", zram_generator_conf(percent))
        else:
            self.p.optional_atoms.append(A_ZRAM_INIT)
            self.file("/etc/conf.d/zram-init", zram_init_conf(size_mib))
            self.service("zram-init", optional=True)
        self.p.scalars["zram_mib"] = size_mib

    def heavy_builds(self) -> None:
        """Fewer compile jobs than the machine-wide setting for the memory-heavy packages (MEMORY_HEAVY_ATOMS), when memory asks for it.

        Portage reads /etc/portage/package.env, so this limits those packages only; everything else keeps the setting of
        make.conf. Only a limit below that setting is written, and nothing when the memory of this PC is not known.
        """
        total_kib = self.hw.memory.total_kib
        if not total_kib:
            return
        swap_mib = _swap_mib(self.m, self.hw) or 0
        jobs = recommend.memory_heavy_jobs(total_kib, swap_mib)
        typed = self.m.portage.make_opts.strip()
        machine = recommend.make_opts_jobs(typed) if typed else recommend.recommend_jobs(self.hw.cpu.threads, self.hw.memory.available_kib, swap_mib)
        if machine is not None and jobs >= machine:
            return
        self.file(f"/etc/portage/env/{MEMORY_ENV_NAME}", "# Written by the Gentoo installer: the compile jobs for packages that need a lot of memory per job.\n"
                  f'MAKEOPTS="-j{jobs}"\n')
        self.file(f"/etc/portage/package.env/{PORTAGE_FILE}", "# Written by the Gentoo installer: packages that need a lot of memory per compile job.\n"
                  + "".join(f"{atom} {MEMORY_ENV_NAME}\n" for atom in MEMORY_HEAVY_ATOMS))
        self.p.scalars["memory_heavy_jobs"] = jobs
        gib = total_kib / recommend.KIB_PER_GIB
        self.note(f"With {gib:.1f} GiB of memory (and the swap), the packages that need about {recommend.GIB_PER_HEAVY_JOB} GiB per compile job "
                  f"({', '.join(atom.split('/')[1] for atom in MEMORY_HEAVY_ATOMS)}) get {jobs} compile job{'s' if jobs != 1 else ''} when they are built from source.")

    # Virtual machines ---------------------------------------------------------------------------------------------------

    def guest_tools(self) -> None:
        hypervisor = self.hw.machine.hypervisor
        if not hypervisor or self.m.hardware.guest_tools == "none":
            return
        graphical = self.desktop != "minimal"
        if hypervisor in ("qemu", "kvm"):
            self.p.optional_atoms.append(A_QEMU_GA)
            self.service("qemu-guest-agent", optional=True)
            if graphical:
                self.p.optional_atoms.append(A_SPICE)
                self.service("spice-vdagent" if self.init == "openrc" else "spice-vdagentd.socket", optional=True)
        elif hypervisor == "vmware":
            # Never add vmware to VIDEO_CARDS: open-vm-tools[resolutionkms] needs mesa[-video_cards_vmware].
            self.p.optional_atoms.append(A_VMTOOLS)
            if not graphical:
                # The resolution plugin is for graphical sessions; it would pull the whole X server into a console-only system.
                self.use(A_VMTOOLS, "-resolutionkms")
            self.service("vmware-tools" if self.init == "openrc" else "vmtoolsd", optional=True)
            if self.init == "systemd":
                self.service("vgauthd", optional=True)
        elif hypervisor == "virtualbox":
            # The guest modules are out-of-tree and built against the kernel tree: best effort.
            self.p.optional_atoms.append(A_VBOX_GA)
            if graphical:
                self.use(A_VBOX_GA, "gui")
            self.service("virtualbox-guest-additions", optional=True)
            self.note("VirtualBox guest additions are built against the installed kernel; if that fails the system still boots.")
        else:
            self.note(f"No guest tools are available for {hypervisor}; the kernel drivers cover shutdown and time.")

    # Printers, audio --------------------------------------------------------------------------------------------------------

    def peripherals(self) -> None:
        drivers = [name for name in ("gutenprint", "hplip") if name in self.m.hardware.printer_drivers]
        if not drivers:
            return
        if not self.m.options.printing:
            self.p.required_atoms.append(A_CUPS)
            self.service("cupsd" if self.init == "openrc" else "cups")
            self.note("CUPS was added because printer drivers were selected.")
        if "gutenprint" in drivers:
            self.p.optional_atoms.append(A_GUTENPRINT)
            self.use(A_GUTENPRINT, "cups")
        if "hplip" in drivers:
            self.p.optional_atoms.append(A_HPLIP)
            self.use(A_AVAHI, "python")  # hplip needs avahi[python]

    def audio(self) -> None:
        if not self.m.options.pipewire:
            return
        # sound-server is on in the desktop profiles but off in the base ones; without it
        # there is no PulseAudio-protocol server. Identical to the profile default on desktops.
        self.use(A_PIPEWIRE, "sound-server")
        if self.m.hardware.pipewire_alsa:
            self.use(A_PIPEWIRE, "pipewire-alsa")
        self.post("sh", "-c", 'if getent group pipewire >/dev/null; then usermod -aG pipewire "$1"; fi; exit 0', "sh", self.m.accounts.username)

    # Boot, display ------------------------------------------------------------------------------------------------------------

    def boot(self) -> None:
        self.p.scalars["removable_fallback"] = resolve_removable_fallback(self.m, self.hw)
        efi_bits = self.hw.firmware.efi_bits if self.m.storage.firmware == "uefi" else 0
        self.p.scalars["grub_target"] = "i386-pc" if self.m.storage.firmware == "bios" else ("i386-efi" if efi_bits == 32 else "x86_64-efi")
        if efi_bits == 32 and self.m.system.bootloader == "grub":
            # A 64-bit CPU with 32-bit UEFI firmware (small tablets) can only run a 32-bit EFI image.
            self.p.make_conf["GRUB_PLATFORMS"] = "pc efi-64 efi-32"
            self.note("This PC has 32-bit UEFI firmware: GRUB is installed for i386-efi.")

    def display(self) -> None:
        choice = self.m.hardware.hidpi
        if choice == "off":
            dpi = 96
        elif choice.startswith("dpi:") and choice[4:].isdigit():
            dpi = int(choice[4:])
        else:
            dpi = recommend.recommend_dpi(self.hw.displays)
        self.p.scalars["hidpi"] = "off" if dpi <= 96 else f"dpi:{dpi}"
        if dpi > 96 and self.desktop == "xfce":
            self.file("/etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml", xfce_xsettings(dpi))

    def locale(self) -> None:
        """Fonts for the script of the chosen language; without them a translated desktop shows empty boxes.

        DejaVu, Liberation and Adwaita cover Latin, Greek and Cyrillic only. The KDE package set already brings
        media-fonts/noto (kde-plasma/plasma-integration needs it); nothing in it has Chinese, Japanese or Korean
        glyphs. Both packages are large downloads, so they are optional atoms: a failure only degrades.
        """
        if self.desktop == "minimal":
            return
        script = keyboard_data.locale_script(self.m.system.locale)
        if script == "cjk":
            self.fonts = (A_NOTO_CJK, "Chinese, Japanese and Korean text")
        elif script == "complex" and self.desktop != "kde":
            self.fonts = (A_NOTO, "Arabic, Hebrew, Indic, Thai and other scripts")
        if self.fonts:
            self.p.optional_atoms.append(self.fonts[0])
            self.note(f"{self.fonts[0]} was added for the language {self.m.system.locale}: the default fonts have no glyphs for {self.fonts[1]}.")

    def companions(self) -> None:
        """COMP-NEW-3: HWMonux reads NVIDIA sensors through NVML only when the proprietary driver is installed.

        dev-python/nvidia-ml-py needs the licence-masked nvidia-drivers, so the flag is not a static package.use line
        (a failed driver build would then abort the whole base stage). The step rebuilds HWMonux only when the driver
        really installed and never fails the installation: a failed rebuild removes the flag again.
        """
        if self.proprietary and self.m.options.install_hwmonux:
            flag_file = "/etc/portage/package.use/installer-hwmonux-nvidia"
            script = ("set -- /var/db/pkg/app-misc/hwmonux-*; "
                      "if [ -f /etc/modprobe.d/nvidia.conf ] && [ -d \"$1\" ]; then "
                      f"echo 'app-misc/hwmonux nvidia' > {flag_file} && emerge --oneshot app-misc/hwmonux "
                      f"|| {{ rm -f {flag_file}; echo 'HWMonux NVIDIA sensors skipped'; }}; fi; exit 0")
            self.post("sh", "-c", script)

    # Finish ---------------------------------------------------------------------------------------------------------------------

    def finish(self) -> None:
        p = self.p
        self.respect_user_package_use()
        p.early_atoms = data.distinct(p.early_atoms)
        p.driver_atoms = data.distinct(p.driver_atoms)
        p.required_atoms = data.distinct(p.required_atoms)
        p.optional_atoms = data.distinct(p.optional_atoms)
        p.package_mask = data.distinct(p.package_mask)
        p.kernel_cmdline = data.distinct(p.kernel_cmdline)
        p.package_use = {atom: flags for atom, flags in sorted(p.package_use.items()) if flags}
        p.package_license = {atom: items for atom, items in sorted(p.package_license.items()) if items}
        p.make_conf = {key: value for key, value in p.make_conf.items() if value}
        p.summary = self.describe()

    def respect_user_package_use(self) -> None:
        """An explicit user flag wins over the plan's; the conflict is reported."""
        for atom, wanted in list(self.p.package_use.items()):
            theirs = set(self.m.portage.package_use.get(atom, []))
            kept = []
            for flag in wanted:
                opposite = flag[1:] if flag.startswith("-") else "-" + flag
                if opposite in theirs:
                    self.warn("package_use_conflict", "warning", f"{atom}: your package.use sets '{opposite}', so the hardware setup's '{flag}' was not applied.")
                else:
                    kept.append(flag)
            self.p.package_use[atom] = kept

    def describe(self) -> list[str]:
        hw, s = self.hw, self.p.scalars
        lines: list[str] = []
        if hw.cpu.model_name:
            ucode = {"intel": "Intel microcode", "amd": "AMD microcode (linux-firmware)", "both": "Intel and AMD microcode", "none": "no microcode"}[s["microcode"]]
            lines.append(f"CPU: {' '.join(hw.cpu.model_name.replace('(R)', '').replace('(TM)', '').split())}, {hw.cpu.threads} threads; {ucode}")
        if s.get("cpu_flags_mode") in ("detect", "manual"):
            lines.append("CPU flags: " + ("tuned for this CPU (packages are built, not downloaded)" if s["cpu_flags_mode"] == "detect" else "set by you"))
        for gpu in self.gpus + [g for g in hw.gpus if g.virtual]:
            lines.append("Graphics: " + (gpu_label(gpu) if gpu.name or gpu.vendor_name else gpu.vendor) + self.graphics_mode(gpu))
        if s["hybrid"]:
            lines.append("Graphics: two graphics cards" + ("; GPU switching helpers installed" if s["prime"] == "offload" else ""))
        if self.p.early_atoms and A_FIRMWARE in self.p.early_atoms:
            lines.append("Firmware: Linux firmware" + (" (" + ", ".join(s["firmware_reasons"]) + ")" if s["firmware_reasons"] else ""))
        if s["sof_firmware"] == "yes":
            lines.append("Audio: Sound Open Firmware (many laptop audio chips need it)")
        if s["power"] != "none":
            lines.append("Power: " + {"ppd": "power-profiles-daemon", "tlp": "TLP"}[s["power"]] + (" and thermald" if s["thermald"] == "yes" else ""))
        if s["trim"] == "weekly":
            lines.append("Storage: weekly TRIM for the solid-state disk" + (" (fstrim.timer)" if self.init == "systemd" else " (cron)"))
        if s["zram"] == "yes":
            lines.append(f"Memory: compressed RAM swap (zram), {s.get('zram_mib', 0)} MiB")
        if s.get("memory_heavy_jobs") == 1:
            lines.append("Memory: large programs that have to be compiled (Qt, LLVM, PySide) are built with one compile job, to stay within the memory of this PC")
        if hw.machine.hypervisor:
            tools = hw.machine.hypervisor in ("qemu", "kvm", "vmware", "virtualbox") and self.m.hardware.guest_tools != "none"
            lines.append(f"Virtual machine: {hw.machine.hypervisor}; " + ("guest tools installed" if tools else "no guest tools"))
        if s["removable_fallback"] != "never" and self.m.storage.firmware == "uefi":
            lines.append("Boot: " + {"always": "fallback boot path is always written", "if-missing": "fallback boot path is written only if missing"}[s["removable_fallback"]])
        if s["hidpi"] != "off" and self.desktop == "xfce":
            lines.append(f"Display: {s['hidpi'][4:]} dpi interface scale for Xfce")
        if self.fonts:
            lines.append(f"Fonts: {self.fonts[0].rsplit('/', 1)[1]} for {self.fonts[1]}")
        if self.p.warnings:
            lines.extend(issue.message for issue in self.p.warnings if issue.severity != "info")
        return lines

    def graphics_mode(self, gpu: GpuInfo) -> str:
        if gpu.virtual:
            return " (virtual, software rendering unless the host provides 3D)"
        if gpu.vendor == "nvidia":
            if self.proprietary:
                return f" - proprietary driver {self.proprietary}"
            nvk = " + NVK" if self.p.scalars.get("nvk") == "yes" else ""
            return f" - open driver (nouveau{nvk})"
        driver = {"intel": "Intel", "amd": "AMD"}.get(gpu.vendor, gpu.vendor)
        return f" - open {driver} driver"


PRIME_RUN = """#!/bin/sh
# Run a program on the NVIDIA GPU (PRIME render offload): prime-run glxgears
export __NV_PRIME_RENDER_OFFLOAD=1
export __VK_LAYER_NV_optimus=NVIDIA_only
export __GLX_VENDOR_LIBRARY_NAME=nvidia
exec "$@"
"""

FSTRIM_CRON = """#!/bin/sh
# Weekly TRIM of mounted solid-state filesystems (written by the Gentoo installer).
exec /sbin/fstrim --all --quiet-unsupported
"""


def zram_init_conf(size_mib: int) -> str:
    # sys-block/zram-init reads /etc/conf.d/zram-init: one swap device of size0 MiB.
    return ('# Compressed RAM swap, written by the Gentoo installer.\n'
            'load_on_start="yes"\nunload_on_stop="yes"\nnum_devices="1"\n'
            f'type0="swap"\nsize0="{size_mib}"\nmaxs0="1"\nalgo0="zstd"\nlabl0="zram_swap"\n')


def zram_generator_conf(percent: int) -> str:
    return ("# Compressed RAM swap, written by the Gentoo installer.\n[zram0]\n"
            f"zram-size = min(ram * {percent} / 100, 16384)\ncompression-algorithm = zstd\nswap-priority = 100\n")


def xfce_xsettings(dpi: int) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n\n<channel name="xsettings" version="1.0">\n'
            '  <property name="Xft" type="empty">\n'
            f'    <property name="DPI" type="int" value="{dpi}"/>\n'
            '  </property>\n</channel>\n')


def resolve_removable_fallback(manifest: InstallManifest, hw: HardwareProfile) -> str:
    """always | if-missing | never: whether GRUB also writes EFI/BOOT/BOOTX64.EFI."""
    storage, system = manifest.storage, manifest.system
    if storage.firmware != "uefi":
        return "never"
    if system.bootloader == "efi-stub" and not system.uki:
        return "never"  # a bare kernel cannot carry its command line as BOOTX64.EFI
    choice = manifest.hardware.removable_fallback
    if choice != "auto":
        return choice
    if manifest.hardware.target == "portable" or hw.firmware.mode == "bios":
        return "always"
    disks = _target_disks(manifest, hw)
    if any(disk.external for disk in disks):
        return "always"
    if storage.mode == "manual":
        esp = next((p for p in storage.partitions if p.mountpoint in ("/efi", "/boot/efi")), None)
        if esp is not None and not esp.format:
            return "if-missing"  # shared with Windows or another Linux: do not replace what is there
    return "always"


def plan(manifest: InstallManifest, hardware: Mapping[str, Any] | HardwareProfile | None) -> HardwarePlan:
    """Resolve every hardware-dependent choice for ``manifest`` on the machine described by ``hardware``."""
    return _Planner(manifest, _profile(hardware)).run()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_plan(manifest: InstallManifest, hardware: Mapping[str, Any] | HardwareProfile | None) -> list[Issue]:
    """Cross-checks of the manifest against the probed machine (structure is checked by manifest.validate())."""
    hw = _profile(hardware)
    m = manifest
    decision = nvidia_decision(m, hw)
    issues: list[Issue] = [*decision.errors, *decision.warnings]
    issues.extend(_memory_issues(m, hw))
    issues.extend(_power_issues(m, hw))
    if _detected(hw):  # an empty profile (no probe ran) says nothing about firmware or disks
        issues.extend(_firmware_issues(m, hw))
        issues.extend(_target_disk_issues(m, hw))
    issues.extend(_network_issues(m, hw))
    return issues


def blocking_issues(manifest: InstallManifest, hardware: Mapping[str, Any] | HardwareProfile | None) -> list[Issue]:
    """What stops an installation: errors of validate_plan plus the probe's own error warnings (no disk, RST mode, wrong clock ...)."""
    hw = _profile(hardware)
    found = {issue.code: issue for issue in validate_plan(manifest, hw) if issue.severity == "error"}
    for warning in hw.warnings:
        if warning.severity == "error":
            found.setdefault(warning.code, Issue(warning.code, "error", warning.message))
    return list(found.values())


def _memory_issues(m: InstallManifest, hw: HardwareProfile) -> list[Issue]:
    out: list[Issue] = []
    gib = hw.memory.total_kib / recommend.KIB_PER_GIB
    if hw.memory.total_kib and 2 <= gib < 4 and m.system.desktop in ("kde", "gnome"):
        out.append(Issue("hw.memory_tight", "info", f"This PC has {gib:.1f} GiB of memory. {m.system.desktop.upper()} will feel slow; Xfce needs less."))
    if m.hardware.hibernate:
        if m.storage.luks:
            out.append(Issue("hw.hibernate_luks", "error", "Hibernation is not available on an encrypted install: its swap is a file inside the encrypted root, "
                             "and the installer does not set up resuming from it. Turn hibernation off or install without disk encryption."))
        needed = recommend.nominal_ram_gib(hw.memory.total_kib) * 1024 if hw.memory.total_kib else 0
        swap = _swap_mib(m, hw)
        if swap == 0:
            out.append(Issue("hw.hibernate_swap_small", "error", "Hibernation needs a swap partition as large as the memory, but this installation has no swap partition. "
                             "Add one, or turn hibernation off."))
        elif swap is not None and needed and swap < needed:
            out.append(Issue("hw.hibernate_swap_small", "error", f"Hibernation needs a swap partition of at least {needed} MiB (the size of memory); it is {swap} MiB."))
        if hw.firmware.lockdown in ("integrity", "confidentiality"):
            out.append(Issue("hw.hibernate_lockdown", "warning", "The kernel is in lockdown mode, which blocks hibernation."))
    return out


def _swap_mib(m: InstallManifest, hw: HardwareProfile) -> int | None:
    """MiB of swap the installation will have; None when a manual layout's swap is not a partition the probe lists (so it is not measured)."""
    if m.storage.mode != "manual":
        return m.storage.swap_mib
    rows = [row for row in m.storage.partitions if row.filesystem == "swap"]
    if not rows:
        return 0
    sizes = [next((part.size for disk in hw.disks for part in disk.partitions if f"/dev/{part.name}" == row.device), None) for row in rows]
    return None if None in sizes else sum(size or 0 for size in sizes) // 1024**2


def _power_issues(m: InstallManifest, hw: HardwareProfile) -> list[Issue]:
    out: list[Issue] = []
    extras = set(m.options.extra_packages)  # two daemons that provide the same D-Bus service cannot be installed together
    if m.hardware.power == "ppd" and A_TLP in extras:
        out.append(Issue("hw.power_conflict", "error", "Power management is set to power-profiles-daemon, but sys-power/tlp is in the extra packages. Use only one."))
    if m.hardware.power == "tlp" and A_PPD in extras:
        out.append(Issue("hw.power_conflict", "error", "Power management is set to TLP, but power-profiles-daemon is in the extra packages. Use only one."))
    if m.hardware.thermald == "yes" and hw.cpu.vendor not in ("intel", ""):
        out.append(Issue("hw.thermald_not_intel", "warning", "thermald is for Intel processors; it has no effect on this CPU."))
    return out


def _firmware_issues(m: InstallManifest, hw: HardwareProfile) -> list[Issue]:
    out: list[Issue] = []
    live, requested = hw.firmware.mode, m.storage.firmware
    if live == "uefi" and requested == "bios":
        out.append(Issue("hw.fw_other_type", "warning", "This PC started in UEFI mode, but a legacy BIOS installation was selected. It will only start in legacy/CSM mode."))
    elif live == "bios" and requested == "uefi":
        severity = "warning" if m.system.bootloader == "grub" else "error"
        out.append(Issue("hw.fw_uefi_from_bios", severity,
                         "This session was started in legacy BIOS mode, so a UEFI install cannot register its boot entry. "
                         + ("GRUB will be installed with the fallback boot path only; restart the installer in UEFI mode for a normal install."
                            if severity == "warning" else "Choose GRUB, or restart the installer in UEFI mode.")))
    if requested == "uefi" and live == "uefi" and hw.firmware.efi_bits == 32:
        problem = _uefi32_problem(m)
        if problem:
            out.append(Issue("hw.fw_uefi32", "error", problem))
        else:
            out.append(Issue("hw.fw_uefi32", "warning", "This PC has 32-bit UEFI firmware: the 32-bit boot files are needed. If it does not start, use legacy BIOS/CSM mode."))
    return out


def _uefi32_problem(m: InstallManifest) -> str:
    """Why this boot setup cannot start on 32-bit UEFI firmware ("" when it can).

    Only a 32-bit program starts there: GRUB is installed for i386-efi, and systemd-boot has an ia32 image on a
    multilib system. A Unified Kernel Image and the bare kernel of the EFI stub are 64-bit programs.
    """
    system = m.system
    alternatives = "GRUB" + (" or systemd-boot" if system.multilib else "")
    if system.uki:
        what = "a Unified Kernel Image, which is a 64-bit program"
        return f"This PC has 32-bit UEFI firmware, which cannot start {what}. Turn the Unified Kernel Image off and choose {alternatives}, or use legacy BIOS mode if the firmware offers it."
    if system.bootloader == "efi-stub":
        return f"This PC has 32-bit UEFI firmware, which cannot start the 64-bit kernel that the EFI stub boots directly. Choose {alternatives}, or use legacy BIOS mode if the firmware offers it."
    if system.bootloader == "systemd-boot" and not system.multilib:
        return "This PC has 32-bit UEFI firmware, and systemd-boot has no 32-bit image on a pure 64-bit (no-multilib) system. Choose GRUB, or use legacy BIOS mode if the firmware offers it."
    return ""


def _target_disk_issues(m: InstallManifest, hw: HardwareProfile) -> list[Issue]:
    disk = next((d for d in hw.disks if d.path == m.storage.device), None)
    if disk is not None:
        return _disk_issues(m, hw, disk)
    # The probe lists real disks only: an assembled RAID array (/dev/md0) or a loop device (a disk image) is not "missing".
    if hw.disks and re.fullmatch(r"/dev/[A-Za-z0-9._-]+", m.storage.device) and not data.IGNORED_DISK_RE.match(m.storage.device.rsplit("/", 1)[-1]):
        hidden = not any(d.installable for d in hw.disks)
        return [Issue("hw.st_disk_missing", "error", f"{m.storage.device} was not found on this PC. "
                      + ("Only the installer's own medium is visible: the internal disk may be hidden by the storage mode in the firmware settings."
                         if hidden else "Choose one of the detected disks."))]
    return []


def _network_issues(m: InstallManifest, hw: HardwareProfile) -> list[Issue]:
    out: list[Issue] = []
    wifi_only = bool(hw.nics) and all(nic.kind == "wifi" for nic in hw.nics if not nic.no_driver)
    if wifi_only and m.network.manager != "networkmanager":
        out.append(Issue("hw.net_wifi_needs_nm", "warning", "The only network adapter is Wi-Fi; only NetworkManager sets up Wi-Fi networks on the installed system."))
    target_disks = _target_disks(m, hw)
    if m.network.import_live_profiles and (m.hardware.target == "portable" or any(disk.external for disk in target_disks)):
        out.append(Issue("hw.import_profiles_portable", "warning", "Wi-Fi passwords from this session would be copied to a disk meant for other computers. Turn that option off."))
    if m.hardware.target == "portable" and m.network.manager == "netifrc":
        out.append(Issue("hw.net_names_hardware_specific", "warning", "Network interface names differ between PCs; NetworkManager works better for a portable install."))
    if m.hardware.cpu_flags_mode == "manual" and not m.portage.cpu_flags.strip():
        out.append(Issue("hw.cpu_flags_empty", "warning", "CPU flags are set to 'manual' but none were entered."))
    if hw.machine.hypervisor == "virtualbox" and m.hardware.guest_tools != "none":
        out.append(Issue("hw.virtualbox_best_effort", "info", "VirtualBox guest additions are built against the installed kernel and may be skipped if that fails."))
    return out


def _disk_issues(m: InstallManifest, hw: HardwareProfile, disk: DiskInfo) -> list[Issue]:
    out: list[Issue] = []
    storage = m.storage
    if disk.live_medium:
        out.append(Issue("hw.st_live_medium", "error", f"{disk.path} holds the installer itself and cannot be erased."))
    if disk.ro:
        out.append(Issue("hw.st_read_only", "error", f"{disk.path} is read-only (a write-protect switch or a broken drive)."))
    if storage.firmware == "bios":
        if disk.log_sec == 4096:
            out.append(Issue("hw.bios_4kn", "error", "This disk uses 4096-byte sectors, which legacy BIOS firmware generally cannot boot. Use UEFI, or a disk with 512-byte sectors."))
        if disk.bus == "nvme":
            out.append(Issue("hw.nvme_bios", "warning", "Many PCs in legacy BIOS mode cannot boot from an NVMe drive."))
    limit = (4 if disk.log_sec == 4096 else 2) * 1024**4
    if storage.table == "dos" and disk.size > limit:
        out.append(Issue("hw.dos_over_2tib", "error", f"A DOS partition table cannot address more than {limit // 1024**4} TiB; this disk has more. Use GPT."))
    signatures = set(disk.signatures)
    if signatures & {"linux_raid_member", "isw_raid_member", "ddf_raid_member"}:
        out.append(Issue("hw.st_raid_member", "warning", "This disk is part of a firmware or software RAID volume. Erasing it destroys the volume and everything on it, including any Windows installation."))
    if "LVM2_member" in signatures:
        out.append(Issue("hw.st_lvm_member", "warning", "This disk holds LVM volumes. Erasing it destroys every logical volume on it."))
    if disk.external and m.hardware.target == "this":
        out.append(Issue("hw.st_removable_target", "info", "The selected disk is removable. Choose 'portable' if you will start it on other computers."))
    if not disk.serial and not disk.wwn and storage.device_id_kind != "path":
        out.append(Issue("hw.st_no_serial", "info", "This disk has no serial number or WWN. It must be identified by its bus connection, size and model, which you have to approve."))
    if storage.mode == "erase" and disk.size:
        root_mib = disk.size // 1024**2 - (1024 if storage.firmware == "uefi" else 3) - (1024 if storage.luks else 0) - storage.swap_mib \
            - (storage.home_mib if storage.separate_home else 0)
        minimum, recommended = recommend.minimum_root_gib(m.system.desktop, m.portage.binhost)
        if root_mib < minimum * 1024:
            out.append(Issue("hw.disk_too_small", "error", f"The root filesystem would get {max(root_mib, 0) / 1024:.0f} GiB, but {m.system.desktop.upper() if m.system.desktop != 'minimal' else 'a minimal system'} needs at least {minimum} GiB."))
        elif root_mib < recommended * 1024:
            out.append(Issue("hw.disk_small", "warning", f"The root filesystem would get {root_mib / 1024:.0f} GiB; {recommended} GiB is recommended."))
    return out

