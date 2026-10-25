"""PORT: compiler optimisation for this processor (Handbook: CFLAGS and CXXFLAGS, RUSTFLAGS, CPU_FLAGS_X86).

Generic (the default) builds programs that run on every 64-bit PC and works best with the binary packages. Native builds with
-march=native, a Rust target of the same processor and the CPU_FLAGS_X86 that cpuid2cpuflags reports (written to
package.use as the Handbook shows, not to make.conf). Native programs may not start on an older PC, so a portable
installation is always generic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .manifest import Portage

NATIVE_MARCH = "-march=native"
NATIVE_RUSTFLAGS = "-C target-cpu=native"
CPU_FLAGS_FILE = "etc/portage/package.use/00cpu-flags"
PORTABLE_NATIVE_MESSAGE = (
    "A portable installation has to start on any PC, so it cannot be tuned for this processor. "
    "Choose the generic optimisation, or turn the portable choice off in the Hardware tab.")
TUNING_HELP = ("Tuning makes programs a little faster on this exact processor, but programs built this way may not start on an older PC.")

# Compiler flags the Handbook warns about: they are not "more optimisation", they can change what a program does.
_RISKY_FLAGS = {
    "-O3": "-O3 can make programs larger and sometimes slower, and a few programs fail to build or misbehave with it. The Handbook advises -O2.",
    "-Ofast": "-Ofast turns off parts of the floating-point rules and can make programs give wrong answers. The Handbook advises -O2.",
    "-fomit-frame-pointer": "-fomit-frame-pointer makes it hard to find the cause of a crash, and Gentoo's tools already choose it where it helps. "
                            "The Handbook advises leaving it out.",
}


def native_wanted(portage: "Portage", portable: bool) -> bool:
    """True when the compiler flags should be tuned for this processor (never for a portable installation)."""
    return portage.optimize == "native" and not portable


def optimize_problem(portage: "Portage", portable: bool) -> str:
    """Why this combination cannot be installed ("" when it can)."""
    return PORTABLE_NATIVE_MESSAGE if portage.optimize == "native" and portable else ""


def tuned_flags(cflags: str, rustflags: str, native: bool) -> tuple[str, str]:
    """COMMON_FLAGS and RUSTFLAGS with the native tuning added (unless the user already chose a -march or a target-cpu)."""
    if not native:
        return cflags, rustflags
    if "-march=" not in cflags and "-mtune=" not in cflags:
        cflags = f"{cflags} {NATIVE_MARCH}".strip()
    if "target-cpu" not in rustflags:
        rustflags = f"{rustflags} {NATIVE_RUSTFLAGS}".strip()
    return cflags, rustflags


def flag_warnings(cflags: str) -> list[str]:
    """Plain-English warnings for the risky flags in a COMMON_FLAGS value."""
    words = cflags.split()
    return [message for flag, message in _RISKY_FLAGS.items() if flag in words]


def cxxflags_value(cxxflags: str) -> str:
    """CXXFLAGS follows COMMON_FLAGS unless the user wrote something else on purpose."""
    return "${COMMON_FLAGS}" if cxxflags.strip() in {"", "${CFLAGS}", "${COMMON_FLAGS}"} else cxxflags


def cpu_flags_of(hardware: dict[str, Any] | None) -> list[str]:
    """CPU_FLAGS_X86 values from the hardware probe of the daemon (cpuid2cpuflags, else /proc/cpuinfo); empty when unknown."""
    try:
        flags = (hardware or {}).get("cpu", {}).get("cpu_flags_x86", [])
        return [str(flag) for flag in flags if isinstance(flag, str)]
    except AttributeError:
        return []


def render_cpu_flags(flags: str) -> str:
    """package.use/00cpu-flags: the Handbook's ``*/* CPU_FLAGS_X86: ...`` line."""
    return f"*/* CPU_FLAGS_X86: {' '.join(flags.split())}\n"
