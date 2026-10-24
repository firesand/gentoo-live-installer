"""NVIDIA details of the hardware plan: what follows from ``hwplan_nvidia.nvidia_decision`` (USE flags, kernel arguments, the Safe graphics
entry, basic display mode, power saving of a hybrid laptop, the Advanced add-ons).

``NvidiaPlanMixin`` is mixed into ``hwplan._Planner``, which supplies ``m`` (the manifest), ``hw`` (the probed machine), ``p`` (the plan being
built), ``desktop`` and the small helpers ``use``, ``license``, ``keyword``, ``file``, ``post``, ``note``, ``warn``, ``service`` and ``first_step``.
"""

from __future__ import annotations

from typing import Any

from gentoo_installer import hardware_data as data
from gentoo_installer.hardware import HardwareProfile, physical_gpus
from gentoo_installer.manifest import InstallManifest

from .hwissue import Issue
from .hwplan_nvidia import NvidiaDecision

SAFE_GRAPHICS_TITLE = "Gentoo Linux (safe graphics)"
# The boot entry that starts when the NVIDIA driver misbehaves: no kernel mode setting, none of the NVIDIA modules, no nouveau.
# The boot stage builds the entry from scalars["safe_graphics"].
SAFE_GRAPHICS_CMDLINE = (
    "nomodeset",
    "modprobe.blacklist=nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm,nouveau",
    "rd.driver.blacklist=nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm,nouveau",
)
BASIC_GRAPHICS_MODPROBE = """# Basic display mode (written by the Gentoo installer): no graphics driver is loaded, the firmware framebuffer draws the screen.
blacklist nouveau
blacklist nvidia
blacklist nvidia_drm
blacklist nvidia_modeset
blacklist nvidia_uvm
"""
# Runtime power management of a Turing laptop's NVIDIA card with the closed modules (NVIDIA driver README, "PCI-Express Runtime D3 Power Management").
RTD3_MODPROBE = """# Runtime power management of the NVIDIA card (written by the Gentoo installer). Delete this file and
# /etc/udev/rules.d/80-nvidia-pm.rules, then run `dracut --force --regenerate-all`, to undo it.
options nvidia "NVreg_DynamicPowerManagement=0x02"
"""
RTD3_UDEV = """# Let the kernel power the NVIDIA card down when nothing uses it (written by the Gentoo installer).
ACTION=="bind", SUBSYSTEM=="pci", ATTR{vendor}=="0x10de", ATTR{class}=="0x030000", TEST=="power/control", ATTR{power/control}="auto"
ACTION=="bind", SUBSYSTEM=="pci", ATTR{vendor}=="0x10de", ATTR{class}=="0x030200", TEST=="power/control", ATTR{power/control}="auto"
ACTION=="unbind", SUBSYSTEM=="pci", ATTR{vendor}=="0x10de", ATTR{class}=="0x030000", TEST=="power/control", ATTR{power/control}="on"
ACTION=="unbind", SUBSYSTEM=="pci", ATTR{vendor}=="0x10de", ATTR{class}=="0x030200", TEST=="power/control", ATTR{power/control}="on"
"""
RETRY_NVIDIA = ("The NVIDIA driver could not be installed during the installation, so this PC runs on the open nouveau driver. "
                "To try again once the network works: emerge --ask x11-drivers/nvidia-drivers && dracut --force --regenerate-all "
                "(the driver is downloaded from download.nvidia.com and compiled on this PC).")
# Suspend and hibernate write the video memory to a temporary file; it needs this much room (NVIDIA driver README, "Configuring Power Management Support").
VRAM_MARGIN = 1.05
NVK_GENERATIONS = frozenset({"kepler", "maxwell-volta", "turing", "ampere_plus", "blackwell", "turing+"})  # Mesa's NVK drives Kepler and newer
# Appended to the user's first-steps.txt by a best-effort step when the driver did not install: nothing is written when it did.
FIRST_STEPS_APPEND = ('if [ ! -f /etc/modprobe.d/nvidia.conf ] && [ -d "/home/$1" ]; then '
                      'printf "%s\\n" "$2" >> "/home/$1/first-steps.txt"; chown "$1:$(id -gn "$1")" "/home/$1/first-steps.txt"; fi; exit 0')


def rtd3_blocker(manifest: InstallManifest, hw: HardwareProfile, decision: NvidiaDecision) -> str:
    """Why runtime power management cannot be set up on this machine ('' when it can): Turing hybrid laptop, closed modules, no screen on the card."""
    gpus = [gpu for gpu in physical_gpus(hw.gpus) if gpu.vendor == "nvidia"]
    if decision.branch != "580" or decision.flavour != "closed":
        return "it needs the closed kernel modules of driver 580 (Advanced: choose the closed modules)"
    if hw.machine.form_factor not in ("laptop", "tablet"):
        return "it is for laptops; a desktop PC never powers its graphics card down"
    if not decision.hybrid:
        return "it needs a laptop with an integrated and a separate graphics card"
    if any(gpu.generation != "turing" for gpu in gpus):
        return "it is only needed for GeForce RTX 20 and GTX 16 series cards; newer cards manage their power with the open modules"
    if any((gpu.connected_outputs or 0) > 0 for gpu in gpus):
        return "a screen is connected to the NVIDIA card itself"
    return ""


def nvidia_policy_issues(manifest: InstallManifest, hw: HardwareProfile, decision: NvidiaDecision) -> list[Issue]:
    """Settings of the Wave 5 NVIDIA fields that cannot be honoured: each choice either takes effect or is refused here, none is ignored in silence."""
    choice = manifest.hardware
    out: list[Issue] = []
    if choice.signed_modules != "off":
        out.append(Issue("hw.signed_modules_unavailable", "error",
                         "Signed kernel modules are not available yet: they need a Secure Boot test that has not been done. Set the signed modules option to off."))
    if choice.nvidia_rtd3 == "on":
        blocker = "the NVIDIA driver is not installed" if not decision.branch else rtd3_blocker(manifest, hw, decision)
        if blocker:
            out.append(Issue("hw.nvidia_rtd3_unavailable", "error", f"Runtime power management of the NVIDIA card cannot be set up: {blocker}."))
    if choice.nvidia_extras and not decision.branch:
        out.append(Issue("hw.nvidia_extras_need_driver", "error",
                         "The NVIDIA add-ons (video decoding, CUDA, nvtop, persistence daemon) need the NVIDIA driver. Accept its licence, or remove the add-ons."))
    if choice.gpu_driver == "basic" and manifest.system.desktop == "sway":
        out.append(Issue("hw.basic_wayland_only", "error",
                         "Basic display mode has no 3D, and Sway only runs on Wayland. Choose another desktop, or a graphics driver."))
    if choice.gpu_driver in ("nouveau", "basic") and (choice.nvidia_modules != "auto" or choice.nvidia_branch != "auto" or choice.nvidia_rtd3 == "on"):
        out.append(Issue("hw.nvidia_choice_unused", "info", "The NVIDIA kernel module and version choices have no effect while the NVIDIA driver is switched off."))
    return out


class NvidiaPlanMixin:
    """The planner's NVIDIA methods."""

    # Supplied by hwplan._Planner (declared for the reader and the type checker only).
    m: InstallManifest
    hw: HardwareProfile
    p: Any
    desktop: str

    def nvidia_driver(self, decision: NvidiaDecision) -> None:
        branch, flavour = decision.branch, decision.flavour
        info = data.NVIDIA_BRANCHES[branch]
        self.p.driver_atoms.append(data.NVIDIA_ATOM)
        self.license(data.NVIDIA_ATOM, str(info["license"]))  # type: ignore[attr-defined]
        flags = ["dist-kernel"]  # the modules are rebuilt with every kernel update
        if self.desktop in data.WAYLAND_DESKTOPS:
            flags.append("wayland")  # pulls egl-gbm, egl-wayland, egl-wayland2 and egl-x11; they are compiled here
        if branch == "580" and flavour == "open":
            flags.append("kernel-open")  # driver 595 has no such flag: its modules are always the open ones
        if "persistenced" in self.m.hardware.nvidia_extras:
            flags.append("persistenced")
        self.use(data.NVIDIA_ATOM, *flags)  # type: ignore[attr-defined]
        if info["mask"]:
            self.p.package_mask.append(str(info["mask"]))
        if branch == "580":
            self.p.kernel_cmdline.append("nvidia-drm.modeset=1")  # kept explicit on 580; 595 needs nothing, and fbdev=1 is the driver's own default
        # The initramfs must contain the driver's modprobe.d file or nouveau can bind first. Rebuilt only when
        # the driver really installed, so a failed build leaves a bootable nouveau system.
        self.post("sh", "-c", "if [ -f /etc/modprobe.d/nvidia.conf ]; then dracut --force --regenerate-all; fi")  # type: ignore[attr-defined]
        self.post("sh", "-c", FIRST_STEPS_APPEND, "sh", self.m.accounts.username, RETRY_NVIDIA)  # type: ignore[attr-defined]
        self.note(f"NVIDIA proprietary driver {info['label']}; if it fails to build, the open nouveau driver stays in use.")  # type: ignore[attr-defined]
        self.note("The NVIDIA driver is not on the binary host: it is downloaded from download.nvidia.com and compiled on this PC, "  # type: ignore[attr-defined]
                  "which adds several minutes and needs internet access to that address.")
        if self.desktop in ("gnome", "gnome-light"):
            self.note("GNOME's window manager (mutter) and login screen (gdm) are rebuilt for the NVIDIA driver, which adds build time.")  # type: ignore[attr-defined]
        if decision.hybrid:
            self.note("Two graphics cards: programs run on the integrated card; start one on the NVIDIA card with prime-run.")  # type: ignore[attr-defined]
        if not self.m.hardware.safe_graphics_entry:
            self.warn("safe_graphics_required", "warning",  # type: ignore[attr-defined]
                      "The Safe graphics boot entry is always added when the NVIDIA driver is installed: it is the way back to a working screen "
                      "if the driver misbehaves.")
        self.p.scalars["safe_graphics"] = {"title": SAFE_GRAPHICS_TITLE, "cmdline": list(SAFE_GRAPHICS_CMDLINE)}
        self.p.scalars["nvidia_flavour"] = flavour
        self.first_step(f'If the screen stays black after an update, choose "{SAFE_GRAPHICS_TITLE}" in the boot menu; it starts without the NVIDIA driver.')  # type: ignore[attr-defined]
        self.first_step("The NVIDIA modules are rebuilt by emerge after every kernel update (USE=dist-kernel). If an update reports that the build failed, "  # type: ignore[attr-defined]
                        "run: emerge --ask @module-rebuild")

    def nvidia_rtd3(self, decision: NvidiaDecision) -> None:
        choice = self.m.hardware.nvidia_rtd3
        blocker = rtd3_blocker(self.m, self.hw, decision)
        on = choice == "on" or (choice == "auto" and not blocker)
        if not on or blocker or not decision.branch:
            self.p.scalars["nvidia_rtd3"] = "off"
            return
        self.file("/etc/modprobe.d/nvidia-pm.conf", RTD3_MODPROBE)  # type: ignore[attr-defined]
        self.file("/etc/udev/rules.d/80-nvidia-pm.rules", RTD3_UDEV)  # type: ignore[attr-defined]
        self.p.scalars["nvidia_rtd3"] = "on"
        self.note("Runtime power management lets this laptop power its NVIDIA card down when it is idle.")  # type: ignore[attr-defined]
        self.first_step("To undo the NVIDIA card's power saving, delete /etc/modprobe.d/nvidia-pm.conf and /etc/udev/rules.d/80-nvidia-pm.rules, "  # type: ignore[attr-defined]
                        "then run: dracut --force --regenerate-all")

    def basic_graphics(self) -> None:
        """Basic display mode: no 3D driver at all, the firmware framebuffer, and X11 sessions only."""
        self.p.kernel_cmdline.append("nomodeset")
        self.file("/etc/modprobe.d/installer-basic-graphics.conf", BASIC_GRAPHICS_MODPROBE)  # type: ignore[attr-defined]
        if self.desktop == "kde":
            self.file("/etc/sddm.conf.d/10-installer-x11.conf",  # type: ignore[attr-defined]
                      "# Basic display mode: no Wayland session (written by the Gentoo installer).\n[General]\nDisplayServer=x11\n")
        elif self.desktop in ("gnome", "gnome-light"):
            self.file("/etc/gdm/custom.conf", "# Basic display mode: no Wayland session (written by the Gentoo installer).\n[daemon]\nWaylandEnable=false\n")  # type: ignore[attr-defined]
        self.p.scalars["gpu_mode"] = "basic"
        self.note("Basic display mode: no 3D acceleration and no Wayland; the firmware framebuffer draws the screen.")  # type: ignore[attr-defined]
        self.first_step("This PC starts in basic display mode (no 3D). To get a normal graphics driver, delete /etc/modprobe.d/installer-basic-graphics.conf "  # type: ignore[attr-defined]
                        "and remove the nomodeset argument from the boot loader configuration.")

    def nvidia_extras(self, decision: NvidiaDecision) -> None:
        extras = list(self.m.hardware.nvidia_extras)
        if not extras or not decision.branch:
            return
        if "vaapi" in extras:
            self.p.optional_atoms.append("media-libs/nvidia-vaapi-driver")
            self.keyword("=media-libs/nvidia-vaapi-driver-0.0.18", "~amd64")  # type: ignore[attr-defined]
            self.first_step("Hardware video decoding on the NVIDIA card: start Firefox with the environment variable NVD_BACKEND=direct. "  # type: ignore[attr-defined]
                            "media-libs/nvidia-vaapi-driver is a testing package, so it is built on this PC and only that version is allowed.")
        if "nvtop" in extras:
            self.p.optional_atoms.append("sys-process/nvtop")
        if "persistenced" in extras:
            self.service("nvidia-persistenced", optional=True)  # type: ignore[attr-defined]
        if "cuda" in extras and self.m.hardware.nvidia_cuda_license_accepted:
            self.p.optional_atoms.append("dev-util/nvidia-cuda-toolkit")
            self.keyword("=dev-util/nvidia-cuda-toolkit-12.9.2", "~amd64")  # type: ignore[attr-defined]
            self.license("dev-util/nvidia-cuda-toolkit", "NVIDIA-CUDA")  # type: ignore[attr-defined]
            self.note("The CUDA toolkit is a very large download and may compile an older GCC on this PC.")  # type: ignore[attr-defined]

    def nvidia_vram_tmp(self, decision: NvidiaDecision, free_mib: int | None) -> None:
        """Suspend and hibernate with the NVIDIA driver write the whole video memory to a temporary file."""
        if not decision.branch or free_mib is None:
            return
        gpus = [gpu for gpu in physical_gpus(self.hw.gpus) if gpu.vendor == "nvidia"]
        vram = max((gpu.vram_mib for gpu in gpus), default=0)
        if vram and free_mib < vram * VRAM_MARGIN:
            self.warn("nvidia_vram_tmp", "warning",  # type: ignore[attr-defined]
                      f"Suspend with the NVIDIA driver saves the {vram / 1024:.0f} GiB of video memory in a temporary file, but /var/tmp has room for only about "
                      f"{max(free_mib, 0) / 1024:.0f} GiB. Give the root filesystem more room, or suspend may fail.")
