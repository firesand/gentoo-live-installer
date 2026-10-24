"""PORT: the Gentoo binary package host (Handbook: Optional, Adding a binary package host).

Gentoo publishes ready-made packages for plain x86-64 (runs on every 64-bit PC) and for x86-64-v3 (needs a processor from
about 2015 on: AVX2, BMI2, FMA). The v3 packages are a little faster, but they do not start on an older processor, so the
installer picks them only when it has detected a processor that can run them and the system is not meant to move to another PC.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .manifest import Portage

BINHOST_BASE = "https://distfiles.gentoo.org/releases/amd64/binpackages/23.0/"
GENERIC_URL = BINHOST_BASE + "x86-64/"
V3_URL = BINHOST_BASE + "x86-64-v3/"
V3_LEVEL = 3
BINREPO_NAME = "gentoobinhost"  # the section and file name the stage3 itself ships, so nothing is doubled


def uses_official_host(url: str) -> bool:
    """True for the official generic address (with or without a trailing slash): the only one the variant can replace."""
    return url.rstrip("/") == GENERIC_URL.rstrip("/")


def choose_variant(variant: str, cpu_level: int, portable: bool) -> tuple[str, str]:
    """(generic | v3, a note for the user or "") from the setting, the detected x86-64 level (0: not detected) and the portable choice."""
    if variant == "generic":
        return "generic", ""
    if portable:
        return "generic", "" if variant == "auto" else "The x86-64-v3 packages were not used: a portable installation has to start on any 64-bit PC."
    if cpu_level >= V3_LEVEL:
        return "v3", ""
    if variant == "v3":
        reason = "this processor was not recognised" if cpu_level <= 0 else f"this processor only supports x86-64-v{cpu_level}"
        return "generic", f"The x86-64-v3 packages were not used because {reason}; the generic packages work on every 64-bit PC."
    return "generic", ""


def binhost_url(portage: "Portage", cpu_level: int = 0, portable: bool = False) -> tuple[str, str]:
    """(address written to binrepos.conf, a note or ""). A custom address is used exactly as typed."""
    if not uses_official_host(portage.binhost_url):
        note = "" if portage.binhost_variant == "auto" else "The packages variant was not applied: the binary package host address was changed by hand."
        return portage.binhost_url, note
    variant, note = choose_variant(portage.binhost_variant, cpu_level, portable)
    return (V3_URL if variant == "v3" else GENERIC_URL), note


def render_binrepos(url: str) -> str:
    """binrepos.conf as the Handbook shows it, with signature checking spelled out (it is also the default)."""
    return (f"[{BINREPO_NAME}]\npriority = 9999\nlocation = /var/cache/binhost/{BINREPO_NAME}\nsync-uri = {url}\nverify-signature = true\n")


def cpu_level_of(hardware: dict | None) -> int:
    """The detected x86-64 level from the daemon's hardware probe (0 when there is none)."""
    try:
        return int((hardware or {}).get("cpu", {}).get("x86_64_level", 0) or 0)
    except (TypeError, ValueError):
        return 0

