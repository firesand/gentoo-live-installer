"""PORT: MAKEOPTS sized for a computer, shared by the wizard (what it shows) and the daemon (what it installs).

Each compile job needs about 2 GiB of memory (big C++ and Rust builds), so the number of jobs is the smaller of the
processor count, 32 and the memory budget divided by 2 GiB. Half of a swap area counts towards the budget. The load limit
(-l) is the processor count.
"""

from __future__ import annotations

import re

GIB_PER_JOB = 2
MAX_JOBS = 32


def auto_jobs(cpus: int, mem_bytes: int, swap_mib: int = 0) -> int:
    """Parallel build jobs a computer with this many processors and bytes of memory can sustain."""
    budget_gib = mem_bytes / 1024**3 + 0.5 * max(0, swap_mib) / 1024
    return max(1, min(cpus, MAX_JOBS, int(budget_gib // GIB_PER_JOB)))


def auto_make_opts(cpus: int, mem_bytes: int, swap_mib: int = 0) -> str:
    """The MAKEOPTS the installer chooses when the user leaves the jobs on automatic."""
    return f"-j{auto_jobs(cpus, mem_bytes, swap_mib)} -l{max(1, cpus)}"


def parse_make_opts(make_opts: str) -> tuple[int | None, float | None]:
    """The job count (-j) and the load limit (-l) of a MAKEOPTS string; None for a part that is missing or has no number."""
    tokens = make_opts.split()
    jobs: int | None = None
    load: float | None = None
    for index, token in enumerate(tokens):
        number = None
        flag = ""
        match = re.fullmatch(r"(-j|--jobs=|-l|--load-average=)([0-9]+(?:\.[0-9]+)?)", token)
        if match:
            flag, number = match.group(1), match.group(2)
        elif token in {"-j", "--jobs", "-l", "--load-average"} and index + 1 < len(tokens) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", tokens[index + 1]):
            flag, number = token, tokens[index + 1]
        if number is None:
            continue
        if flag in {"-j", "--jobs=", "--jobs"}:
            jobs = int(float(number)) or None
        else:
            load = float(number)
    return jobs, load


def format_make_opts(jobs: int, load: float | None) -> str:
    """-jN, followed by -lM when a load limit is wanted."""
    text = f"-j{jobs}"
    if load:
        text += f" -l{int(load) if float(load).is_integer() else load}"
    return text
