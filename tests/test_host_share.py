"""The user's PC stays usable while the project works: scripts/low-priority.sh, scripts/host_share.py and their users."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

import host_share

ROOT = Path(__file__).resolve().parents[1]
LOW = ROOT / "scripts/low-priority.sh"


def low(*argv: str, cpus: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    prefix = ["taskset", "-c", cpus] if cpus else []
    return subprocess.run([*prefix, str(LOW), *argv], capture_output=True, text=True, timeout=30,
                          env={**os.environ, **(env or {})})


@pytest.mark.parametrize(("cpus", "reserve", "expected"), [
    ("0-31", "8", "8-31"),
    ("0-3,8-11,20", "8", "8-11,20"),
    ("0-3,9,11-12", "8", "9,11-12"),
    ("0-31", "0", "0-31"),
    ("0-7", "8", "0-7"),            # nothing would be left: all stay
    ("8-31", "8", "8-31"),          # already low priority: the same CPUs
])
def test_the_cpus_left_are_those_numbered_at_least_the_reserve(cpus: str, reserve: str, expected: str) -> None:
    result = low("--cpus", cpus=cpus, env={"HOST_RESERVE_CPUS": reserve})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected
    assert host_share.share_cpus(cpu_set(cpus), int(reserve)) == cpu_set(expected)


def cpu_set(text: str) -> set[int]:
    cpus: set[int] = set()
    for part in text.split(","):
        first, _, last = part.partition("-")
        cpus.update(range(int(first), int(last or first) + 1))
    return cpus


def test_a_command_runs_at_nice_19_off_the_reserved_cpus_and_keeps_its_exit_status() -> None:
    result = low("sh", "-c", "grep Cpus_allowed_list /proc/self/status; cut -d' ' -f19 /proc/self/stat; exit 7", cpus="0-15")
    assert result.returncode == 7
    allowed, nice = result.stdout.split("\n")[:2]
    assert allowed.split()[-1] == "8-15" and nice == "19"


@pytest.mark.parametrize("argv", [[], ["--bogus"], ["--cpus", "extra"]])
def test_usage_errors(argv: list[str]) -> None:
    assert low(*argv).returncode == 2


def test_a_bad_reserve_is_refused() -> None:
    assert low("--cpus", env={"HOST_RESERVE_CPUS": "some"}).returncode == 2
    with pytest.raises(ValueError, match="HOST_RESERVE_CPUS"):
        host_share.reserve_cpus({"HOST_RESERVE_CPUS": "some"})


def test_the_defaults_keep_8_cpus_and_16_gib_for_the_user() -> None:
    assert host_share.reserve_cpus({}) == 8 and host_share.reserve_mib({}) == 16 * 1024
    assert host_share.reserve_mib({"HOST_RESERVE_GIB": "4"}) == 4096


def test_a_vm_starts_only_while_the_reserve_stays_free() -> None:
    assert host_share.memory_problem(4096, available=4096 + 16384, reserve=16384) == ""
    short = host_share.memory_problem(4096, available=4096 + 16383, reserve=16384)
    assert "only 20479 MiB" in short and "16 GiB stay free" in short
    assert host_share.memory_problem(4096, available=None, reserve=16384) == ""  # unreadable: not checked


def test_the_available_memory_is_read_from_meminfo(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 48000000 kB\nMemAvailable: 20971520 kB\n")
    assert host_share.available_mib(meminfo) == 20480
    assert host_share.available_mib(tmp_path / "missing") is None


def test_the_harness_lowers_its_own_priority_in_a_child() -> None:
    code = ("import os, host_share; host_share.lower_own_priority({'HOST_RESERVE_CPUS': '4'}); "
            "print(sorted(os.sched_getaffinity(0))[0], os.getpriority(os.PRIO_PROCESS, 0))")
    result = subprocess.run(["taskset", "-c", "0-7", "python3", "-c", code], capture_output=True, text=True, timeout=30,
                            env={**os.environ, "PYTHONPATH": str(ROOT / "scripts")})
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["4", "19"]


def test_the_vm_matrix_keeps_the_same_reserve() -> None:
    import vm_matrix
    assert vm_matrix.MARGIN_MIB == host_share.reserve_mib()


def test_the_sandbox_and_the_iso_build_use_it() -> None:
    sandbox = (ROOT / "scripts/sandbox.sh").read_text()
    assert sandbox.rstrip().endswith('exec "$root/scripts/low-priority.sh" bwrap "${args[@]}" -- "$@"')
    build = (ROOT / "scripts/build-iso.sh").read_text()
    assert '--cpuset-cpus "${BUILD_CPUSET}" --cpu-shares 2' in build
    harness = (ROOT / "scripts/test-vm.py").read_text()
    assert "host_share.lower_own_priority()" in harness and "host_share.memory_problem(args.memory" in harness
