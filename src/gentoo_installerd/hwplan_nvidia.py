"""The NVIDIA part of the hardware planner: which driver, which branch, which kernel modules, and why.

``nvidia_decision`` is a pure function of the manifest and the probed machine, shared by ``plan()``, ``validate_plan()`` and the
wizard's NVIDIA card (``pages/hardware.nvidia_offer``). The plan details that follow from it (USE flags, kernel arguments, the Safe
graphics entry, power saving of a hybrid laptop) are in ``hwplan.py``; the reasons and the matrix are here.

The Automatic table
-------------------
============================================  ====================================================
machine                                       result
============================================  ====================================================
Turing, Ampere, Ada (desktop or laptop)       595, open kernel modules
Blackwell (RTX 50)                            595, open kernel modules (the closed ones do not exist for it)
Maxwell, Pascal, Volta only                   580, closed modules, later branches masked
Maxwell-Volta next to Turing or newer         580 (it drives both), closed modules
Blackwell next to Maxwell-Volta               error hw.nvidia_mixed_unsupported
hybrid laptop with Ampere or newer            595 and PRIME render offload
hybrid laptop with Turing                     595 (Advanced: 580, closed modules, runtime power management)
Kepler, Fermi and older                       nouveau
virtual machine                               nouveau, unless a real NVIDIA card is passed through
portable disk                                 595
============================================  ====================================================

Automatic installs the NVIDIA driver only once its licence is ticked; without the tick it is nouveau and ``hw.nvidia_skipped`` says why.
470, 390 and 535 are never offered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from gentoo_installer import hardware_data as data
from gentoo_installer.hardware import GpuInfo, HardwareProfile, gpu_label, is_detected, is_hybrid, physical_gpus
from gentoo_installer.manifest import InstallManifest

from .hwissue import Issue


@dataclass
class NvidiaDecision:
    """What the NVIDIA settings and this machine add up to."""

    branch: str  # "595" or "580" when the proprietary driver is installed, "" when it is off or impossible
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    flavour: str = ""  # "open" | "closed" kernel modules, "" without a branch
    mode: str = "nouveau"  # nvidia | nouveau | basic: the driver family that ends up in use
    wanted: bool = False  # the proprietary driver was asked for (chosen, or Automatic with the licence ticked)
    reason: str = ""  # one plain sentence about the choice, for the card
    hybrid: bool = False
    available: tuple[str, str] = ("", "")  # (branch, flavour) that the licence tick would switch on, when it is not ticked yet
    generations: tuple[str, ...] = ()  # of the NVIDIA cards in this PC


def card_name(gpus: Sequence[GpuInfo]) -> str:
    names = [gpu_label(gpu) for gpu in gpus if gpu.name]
    return names[0] if len(names) == 1 else ("your NVIDIA cards" if len(gpus) > 1 else "your NVIDIA card")


def nvidia_decision(manifest: InstallManifest, hw: HardwareProfile) -> NvidiaDecision:
    """Whether the proprietary NVIDIA driver is installed, which branch and which kernel modules (see the module text for the table)."""
    choice = manifest.hardware
    if choice.gpu_driver in ("nouveau", "basic"):
        reason = ("You chose the open-source nouveau driver." if choice.gpu_driver == "nouveau"
                  else "You chose basic display mode: no 3D acceleration, the safest start.")
        cards = [gpu for gpu in physical_gpus(hw.gpus) if gpu.vendor == "nvidia"]
        if choice.gpu_driver == "nouveau" and cards and all(gpu.generation not in data.PROPRIETARY_GENERATIONS for gpu in cards):
            # Too old for every NVIDIA driver here anyway: the real reason, the same sentence "Automatic" gives.
            reason = nvidia_reason(card_name(cards), [gpu.generation for gpu in cards], "", "", True, is_hybrid(hw))
        return NvidiaDecision("", mode=choice.gpu_driver, reason=reason)
    explicit = choice.gpu_driver == "nvidia"
    wanted = explicit or choice.nvidia_license_accepted
    errors: list[Issue] = []
    warnings: list[Issue] = []
    portable = choice.target == "portable"
    gpus = [gpu for gpu in physical_gpus(hw.gpus) if gpu.vendor == "nvidia"]
    decision = NvidiaDecision("", errors, warnings, wanted=wanted, hybrid=is_hybrid(hw))
    if not gpus and not portable and (is_detected(hw) or not explicit):  # a probe that found nothing at all cannot tell: an explicit choice stands
        if explicit and is_detected(hw):
            errors.append(Issue("hw.nvidia_no_gpu", "error", "The proprietary NVIDIA driver was selected but no NVIDIA graphics card was found in this PC. "
                                "Choose Automatic or the open driver, or select 'portable' if the disk will run on another computer."))
        decision.reason = "No NVIDIA graphics card was found in this PC."
        return decision
    generations = [gpu.generation for gpu in gpus] if gpus else ["turing"]  # a portable disk is planned for a card of the current kind
    decision.generations = tuple(generations)
    name = card_name(gpus)
    old = "maxwell-volta" in generations
    blackwell = "blackwell" in generations
    unsupported = [gpu for gpu in gpus if gpu.generation not in data.PROPRIETARY_GENERATIONS]  # Kepler and older, or unknown
    explicit_branch, modules = choice.nvidia_branch, choice.nvidia_modules
    if explicit_branch != "auto" and explicit_branch not in data.NVIDIA_BRANCHES:
        errors.append(Issue("hw.nvidia_branch_unavailable", "error", f"NVIDIA driver branch {explicit_branch} is not available in this installer. Choose one of: "
                            + ", ".join(sorted(data.NVIDIA_BRANCHES)) + "."))
    if manifest.system.kernel not in data.DIST_KERNELS or manifest.system.initramfs != "dracut":
        errors.append(Issue("hw.nvidia_kernel", "error", "The proprietary NVIDIA driver needs a Gentoo distribution kernel (gentoo-kernel or gentoo-kernel-bin) and Dracut: "
                            "its modules are built against the installed kernel and added to the initramfs."))
    if manifest.system.secure_boot:
        errors.append(Issue("hw.nvidia_secure_boot", "error", data.SECURE_BOOT_MODULES_MESSAGE.format(what="The NVIDIA driver")))
    specific = False  # a more precise error was given, so the generic "not supported by branch" one is left out
    if blackwell and old:
        specific = True
        errors.append(Issue("hw.nvidia_mixed_unsupported", "error", "This PC has a GeForce RTX 50 series card (Blackwell) next to an older GTX 900, GTX 10 or Titan V card. "
                            "The Blackwell card needs driver 595 and the older one needs driver 580, and only one NVIDIA driver can be installed. "
                            "Remove one of the cards, or use the open nouveau driver."))
    if modules == "closed" and blackwell:
        specific = True
        errors.append(Issue("hw.nvidia_closed_blackwell", "error", "The GeForce RTX 50 series (Blackwell) only works with the open kernel modules; "
                            "the closed modules do not support it. Choose the open modules."))
    if modules == "open" and old:
        specific = True
        errors.append(Issue("hw.nvidia_open_unsupported_gpu", "error", "The open kernel modules need a GeForce RTX 20 series or newer card; "
                            "this PC has an older NVIDIA card that needs the closed modules (driver 580)."))
    if (modules == "closed" and explicit_branch == "595") or (modules == "open" and explicit_branch == "580" and old):
        specific = True
        errors.append(Issue("hw.nvidia_modules_branch", "error", f"The kernel modules you chose do not exist in driver branch {explicit_branch}. "
                            "Set the driver version to Automatic, or change the kernel modules."))
    # The branch: an explicit one wins, then the kernel modules, then the Automatic table.
    if explicit_branch in data.NVIDIA_BRANCHES:
        branch = explicit_branch
    elif modules == "closed":
        branch = "580"
    elif modules == "open":
        branch = "595"
    elif old:
        branch = "580"
    elif unsupported:
        branch = ""
    else:
        branch = "595"
    if gpus and not portable and not specific:
        covered: Sequence[str] = data.NVIDIA_BRANCHES[branch]["generations"] if branch else ()  # type: ignore[assignment]
        for gpu in gpus:
            if gpu.generation not in covered:
                label = gpu_label(gpu) if gpu.name else "This NVIDIA card"
                if gpu.generation == "blackwell":
                    advice = "Choose branch 595 with the open modules."
                elif branch == "595" and gpu.generation == "maxwell-volta":
                    advice = "Choose branch 580 (the last for Maxwell, Pascal and Volta cards) or the open driver."
                else:
                    advice = "The open nouveau driver supports it."
                errors.append(Issue("hw.nvidia_unsupported_gpu", "error", f"{label} is not supported by"
                                    + (f" driver branch {branch}" if branch else " any proprietary driver offered here") + ". " + advice))
            elif gpu.generation_guess:
                warnings.append(Issue("hw.nvidia_generation_guess", "warning",
                                      f"The generation of the NVIDIA card {gpu.vendor_id}:{gpu.device_id} is guessed from its id (it is newer than the device database)."))
    flavour = ""
    if branch == "595":
        flavour = "open"
    elif branch == "580":
        flavour = "closed" if modules == "closed" else ("open" if modules == "open" or all(data.is_turing_plus(g) for g in generations) else "closed")
    decision.reason = nvidia_reason(name, generations, branch, flavour, bool(unsupported), decision.hybrid)
    if not wanted:
        # Automatic without the licence tick asks nothing of this PC, so nothing is refused: nouveau, and the card says why.
        fits = not errors
        errors.clear()
        warnings.clear()
        if branch and fits:
            decision.available = (branch, flavour)
            warnings.append(Issue("hw.nvidia_skipped", "info", f"The NVIDIA driver is available for {name}, but its licence has not been accepted, so the open nouveau driver "
                                  "is used. Accept the licence on the Hardware page to use the NVIDIA driver."))
        return decision
    if errors and not explicit and not any(issue.code == "hw.nvidia_mixed_unsupported" for issue in errors):
        # Automatic with the licence ticked is a wish, not an order: what cannot be done is said, and nouveau runs the card.
        reasons = " ".join(issue.message for issue in errors)
        errors.clear()
        warnings.append(Issue("hw.nvidia_skipped", "warning", f"The NVIDIA driver was not installed: {reasons} The open nouveau driver is used."))
        return decision
    if errors:
        return decision
    decision.branch, decision.flavour = branch, flavour
    decision.mode = "nvidia" if branch else "nouveau"
    return decision


def nvidia_reason(name: str, generations: Sequence[str], branch: str, flavour: str, legacy: bool, hybrid: bool) -> str:
    """A plain sentence for the NVIDIA card: why this driver (or none) for this card."""
    if not branch:
        return f"{name} is too old for any NVIDIA driver offered here; the open nouveau driver runs it." if legacy else ""
    modules = "open kernel modules" if flavour == "open" else "closed kernel modules"
    if branch == "580" and "maxwell-volta" in generations:
        return f"{name} is from the GeForce GTX 900 and 10 series era: driver 580 with {modules} is the last NVIDIA driver that supports it."
    suffix = " Programs can be started on the NVIDIA card when they need it (render offload)." if hybrid else ""
    return f"Recommended for {name}: the NVIDIA driver (version {branch}) with {modules}.{suffix}"
