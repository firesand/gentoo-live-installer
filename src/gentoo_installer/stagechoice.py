"""PORT: which stage3 file and which profile fit the chosen desktop, init system and multilib (Handbook: Choosing a stage file).

The Handbook's tip is to take the Desktop stage3 for a graphical system (its profile already enables the desktop USE flags)
and the Standard one for a minimal system. ``derive_stage_variant`` does that; ``list_profiles`` lists the profiles the stage
may switch to: the stable amd64 23.0 ones that fit the init system and multilib choice, never a developer profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .manifest import (
    DESKTOPS, PROFILE_BASE, PUBLISHED_PROFILES, STAGE3_VARIANTS, derive_profile, desktop_problem, stage_family,
)
from .profiles_data import AMD64_PROFILES

# Profile parts the installer does not offer: other machines (prefix, x32), other /usr layouts (split-usr needs its own
# stage3) and the developer profile.
_NEVER_OFFERED = ("prefix", "x32", "split-usr", "developer")
# Offered only after "show experimental": the tree calls them stable, but the installer has no stage3 and no test for them.
_EXPERIMENTAL_PARTS = ("selinux", "llvm")


@dataclass(frozen=True)
class StageChoice:
    """The stage3 variant and Portage profile that go together for a desktop, init and multilib choice."""

    variant: str
    profile: str


@dataclass(frozen=True)
class ProfileChoice:
    path: str
    stability: str  # stable | dev | exp
    label: str
    experimental: bool  # hidden unless "show experimental"
    supported: bool  # the installer can install it (it is a published profile with a matching stage3)


def derive_stage_variant(desktop: str, init: str, multilib: bool = True) -> str:
    """The published stage3 variant for a system: Desktop for a graphical desktop, Standard for Minimal, No multilib without multilib."""
    if init not in {"openrc", "systemd"}:
        raise ValueError(f"unknown init system {init!r}")
    if desktop not in DESKTOPS:
        raise ValueError(f"unknown desktop {desktop!r}")
    if not multilib:
        family = "amd64-nomultilib"  # there is no Desktop stage3 without multilib
    elif desktop == "minimal":
        family = "amd64"
    else:
        family = "amd64-desktop"
    variant = f"{family}-{init}"
    assert variant in STAGE3_VARIANTS, variant
    return variant


def derive_stage(desktop: str, init: str, multilib: bool = True) -> StageChoice:
    """The stage3 variant plus the profile that goes with it (the profile is what manifest.derive_profile returns)."""
    return StageChoice(derive_stage_variant(desktop, init, multilib), derive_profile(init, desktop, multilib=multilib))


def effective_stage_variant(stage_variant: str, desktop: str, init: str, multilib: bool, stage_auto: bool) -> str:
    """The stage3 that is downloaded.

    With ``stage_auto`` the Standard and Desktop families follow the desktop, as the Handbook advises. A Hardened or No multilib
    stage3 is an explicit choice and is never replaced; without ``stage_auto`` the chosen variant is used as it is.
    """
    if not stage_auto or stage_family(stage_variant) not in {"standard", "desktop"}:
        return stage_variant
    return derive_stage_variant(desktop, init, multilib)


def stage_problem(stage_variant: str, desktop: str) -> str:
    """Why this desktop cannot be installed on this stage3 (for example KDE Plasma on Hardened), in plain words; "" when it can."""
    return desktop_problem(stage_variant, desktop)


def _is_experimental(path: str) -> bool:
    return any(part in path.split("/") for part in _EXPERIMENTAL_PARTS)


def describe_profile(path: str) -> str:
    """A plain-English name for a profile path."""
    rest = path.removeprefix(PROFILE_BASE).strip("/")
    parts = rest.split("/") if rest else []
    init = "systemd" if parts[-1:] == ["systemd"] else "OpenRC"
    if parts[-1:] == ["systemd"]:
        parts = parts[:-1]
    names = {
        (): "Standard", ("desktop",): "Desktop", ("desktop", "gnome"): "Desktop for GNOME", ("desktop", "plasma"): "Desktop for KDE Plasma",
        ("no-multilib",): "No multilib", ("hardened",): "Hardened", ("no-multilib", "hardened"): "Hardened, no multilib",
        ("hardened", "selinux"): "Hardened with SELinux", ("no-multilib", "hardened", "selinux"): "Hardened with SELinux, no multilib",
        ("llvm",): "LLVM toolchain",
    }
    name = names.get(tuple(parts), " / ".join(parts))
    return f"{name} ({init})"


def parse_eselect_profile_list(text: str) -> list[tuple[str, str]]:
    """(path, stability) pairs from ``eselect profile list`` ("  [3]   default/linux/amd64/23.0/desktop (stable) *")."""
    result = []
    for line in text.splitlines():
        match = re.match(r"\s*\[\d+\]\s+(\S+)\s+\((\w+)\)", line)
        if match:
            result.append((match.group(1), match.group(2)))
    return result


def list_profiles(init: str, multilib: bool = True, show_experimental: bool = False, *, eselect_list: str | None = None,
                  hardened: bool | None = None) -> list[ProfileChoice]:
    """The profiles that can be chosen for this init system and multilib choice.

    ``eselect_list`` is the output of ``eselect profile list`` of the stage3 (the daemon has it after unpacking); without it the
    pinned tree's list is used. Only stable profiles are listed, never a developer one, and only those of the init system
    (systemd profiles end in /systemd) and multilib setting. ``hardened`` limits the list to hardened or non-hardened
    profiles (None: both). Experimental ones (SELinux, LLVM, and unstable profiles) appear only with ``show_experimental``.
    """
    entries = parse_eselect_profile_list(eselect_list) if eselect_list is not None else list(AMD64_PROFILES)
    result: list[ProfileChoice] = []
    for path, stability in entries:
        if not path.startswith(PROFILE_BASE) or any(part in path.split("/") for part in _NEVER_OFFERED):
            continue
        if ("/systemd" in path) != (init == "systemd") or ("no-multilib" in path) != (not multilib):
            continue
        if hardened is not None and ("hardened" in path) != hardened:
            continue
        experimental = stability != "stable" or _is_experimental(path)
        if experimental and not show_experimental:
            continue
        result.append(ProfileChoice(path, stability, describe_profile(path), experimental, path in PUBLISHED_PROFILES))
    return result
