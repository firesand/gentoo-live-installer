"""Leave the user's PC usable while VMs run (the user's rule of 2026-10-28: "when you build or compile I can still use my PC").

The VM harness lowers its own priority before it starts anything, so QEMU and every other child inherits it:
nice 19, and no logical CPU numbered below HOST_RESERVE_CPUS (default 8, on the i9-14900K four P-cores with their
second threads). A VM starts only while HOST_RESERVE_GIB (default 16) of memory stays available beyond it.
scripts/low-priority.sh is the same rule for the shell scripts and the sandbox.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

RESERVE_CPUS = 8
RESERVE_GIB = 16
LOWEST_PRIORITY = 19


def _number(env: Mapping[str, str], name: str, default: int) -> int:
    value = env.get(name, "")
    if not value:
        return default
    if not value.isdigit():
        raise ValueError(f"{name} must be a whole number, not {value!r}")
    return int(value)


def reserve_cpus(env: Mapping[str, str] = os.environ) -> int:
    return _number(env, "HOST_RESERVE_CPUS", RESERVE_CPUS)


def reserve_mib(env: Mapping[str, str] = os.environ) -> int:
    return _number(env, "HOST_RESERVE_GIB", RESERVE_GIB) * 1024


def share_cpus(allowed: set[int], reserve: int) -> set[int]:
    """The CPUs of ``allowed`` numbered at least ``reserve``; all of them when none would be left."""
    return {cpu for cpu in allowed if cpu >= reserve} or set(allowed)


def lower_own_priority(env: Mapping[str, str] = os.environ) -> None:
    """Move this process (and so whatever it starts later) to the lowest CPU priority and off the reserved CPUs."""
    os.sched_setaffinity(0, share_cpus(os.sched_getaffinity(0), reserve_cpus(env)))
    os.setpriority(os.PRIO_PROCESS, 0, LOWEST_PRIORITY)


def available_mib(meminfo: Path = Path("/proc/meminfo")) -> int | None:
    """MemAvailable in MiB, or None when it cannot be read."""
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def memory_problem(need_mib: int, *, available: int | None, reserve: int) -> str:
    """Why something that needs ``need_mib`` must not start now ("" when it may)."""
    if available is None or available >= need_mib + reserve:
        return ""
    return (f"only {available} MiB of memory are available: this needs {need_mib} MiB and {reserve // 1024} GiB stay free "
            f"for your own programs (HOST_RESERVE_GIB); close something or try later")
