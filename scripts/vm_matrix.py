"""The acceptance matrices: which scenarios run on which machine shapes, how much memory each needs, and at most two at a time.

``test-vm.py --matrix wave3`` runs the matrix as one child ``test-vm.py`` process per entry, each with its own SSH port,
its own signal handling and its own QEMU, so a crash in one entry cannot take another down. This module is the data (the
entries and their sizing, documented in docs/TESTING.md) and the scheduler: it never starts more VMs than the cap, and
never more than the memory budget and the free memory of the host allow. Starting a process and reading the free memory
are injected, so the whole schedule is tested without a VM.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

PARALLEL_MAX = 2                 # VMs at the same time, however many the caller asks for
MARGIN_MIB = 3072                # host memory that stays free beyond the VMs (docs/TESTING.md, "Memory rule")
DEFAULT_BUDGET_MIB = 24576       # the sum of the memory of all running VMs
RUN_DIR_RE = re.compile(r"^RUN (\S+): logs in (.+)$", re.M)


@dataclass(frozen=True)
class MatrixEntry:
    name: str
    scenario: str
    memory_mib: int
    cpus: int
    options: tuple[str, ...] = ()
    minutes: int = 45                # a rough duration on a machine with KVM, for the plan listing only
    note: str = ""

    def sizing(self) -> str:
        return f"{self.memory_mib // 1024 if self.memory_mib % 1024 == 0 else self.memory_mib / 1024:g} GiB RAM, {self.cpus} CPUs"


@dataclass(frozen=True)
class Matrix:
    name: str
    title: str
    entries: tuple[MatrixEntry, ...]


# Every UEFI install also boots once more with an empty NVRAM (the removable fallback), and every job fails on advisory findings.
UEFI = ("--fresh-nvram-check",)
STRICT = ("--strict-probes",)

WAVE3 = Matrix("wave3", "Wave 3 acceptance: other hardware, other disks, other distributions", (
    MatrixEntry("defaults", "defaults", 12288, 4,
                (*STRICT, *UEFI, "--install-mode", "gui", "--no-disk-serial", "--disk-size", "64", "--build-jobs", "1", "--swap-mib", "4096"), 150,
                "the untouched wizard (KDE, OpenRC, binhost) on a virtio disk without a serial number, so the path identity is used; needs the GUI to offer it (known gap G3)"),
    MatrixEntry("rescue-gentoo", "rescue-gentoo", 4096, 2, (*STRICT, *UEFI), 100,
                "a minimal Gentoo installed, then broken three ways (menu file, initramfs, fstab) and repaired with gentoo-rescue"),
    MatrixEntry("uefi-nvme-luks-openrc", "uefi-nvme-luks-openrc", 4096, 2, (*STRICT, *UEFI), 55,
                "LUKS on OpenRC, NVMe, QXL display; swap must be a file inside the encrypted root"),
    MatrixEntry("systemd", "systemd", 4096, 2, (*STRICT, *UEFI), 40, "re-acceptance: the guided erase sequence changed in Wave 1"),
    MatrixEntry("openrc", "openrc", 4096, 2, (*STRICT, *UEFI), 40, "re-acceptance"),
    MatrixEntry("uefi-luks", "uefi-luks", 4096, 2, (*STRICT, *UEFI), 50, "re-acceptance: LUKS on systemd, unlocked on the serial console"),
    MatrixEntry("uefi-manual-two-disks", "uefi-manual-two-disks", 4096, 2, (*STRICT, *UEFI), 45,
                "manual partitioning across two disks next to a decoy disk that must stay byte for byte as it was"),
    MatrixEntry("bios", "bios", 4096, 2, STRICT, 40, "re-acceptance: SeaBIOS, MBR, static network configuration"),
    MatrixEntry("bios-sata-minimal", "bios-sata-minimal", 4096, 2, STRICT, 40, "an old PC: SeaBIOS, SATA, Intel NIC, standard VGA"),
    MatrixEntry("text-install", "text-install", 4096, 2, (*STRICT, *UEFI), 45, "the ISO's text entry and gentoo-installer-text with a device confirmation"),
    MatrixEntry("rescue-fedora", "rescue-fedora", 4096, 2, (), 30, "Fedora cloud image: repair, then UEFI and BIOS boot (one restart for the SELinux relabel)"),
    MatrixEntry("rescue-ubuntu", "rescue-ubuntu", 4096, 2, (), 30, "Ubuntu cloud image with a separate /boot partition"),
    MatrixEntry("rescue-debian", "rescue-debian", 4096, 2, (), 25, "Debian cloud image"),
    MatrixEntry("rescue-arch", "rescue-arch", 4096, 2, (), 15, "Arch cloud image: detection and diagnosis only, every repair refused"),
))
MATRICES: dict[str, Matrix] = {WAVE3.name: WAVE3}

# The options of the caller that every child gets (the matrix only decides the scenario, the sizing and the entry's own options).
PASSTHROUGH_VALUES = (
    ("--iso", "iso"), ("--ssh-host", "ssh_host"), ("--ssh-private-key", "ssh_private_key"), ("--ssh-user", "ssh_user"), ("--boot-timeout", "boot_timeout"),
    ("--install-timeout", "install_timeout"), ("--shutdown-timeout", "shutdown_timeout"), ("--command-timeout", "command_timeout"),
    ("--gui-timeout", "gui_timeout"), ("--luks-timeout", "luks_timeout"), ("--luks-unlock", "luks_unlock"), ("--companions", "companions"),
    ("--qemu-binary", "qemu_binary"), ("--qemu-img", "qemu_img"), ("--ssh-binary", "ssh_binary"), ("--ssh-keygen", "ssh_keygen"),
    ("--rescue-images", "rescue_images"), ("--rescue-timeout", "rescue_timeout"), ("--broken-timeout", "broken_timeout"), ("--login-timeout", "login_timeout"),
    ("--world-timeout", "world_timeout"), ("--rescue-transport", "rescue_transport"),
)


def passthrough_arguments(args: Any) -> list[str]:
    """The caller's options that all children share (paths, keys, timeouts, tool names)."""
    out: list[str] = []
    for flag, attribute in PASSTHROUGH_VALUES:
        value = getattr(args, attribute, None)
        if value is not None:
            out += [flag, str(value)]
    return out


def select_entries(matrix: Matrix, only: Sequence[str]) -> list[MatrixEntry]:
    """The entries to run: all of them, or the named ones in the matrix's order."""
    if not only:
        return list(matrix.entries)
    known = {entry.name for entry in matrix.entries}
    unknown = [name for name in only if name not in known]
    if unknown:
        raise ValueError(f"the {matrix.name} matrix has no entry {', '.join(unknown)} (it has: {', '.join(sorted(known))})")
    return [entry for entry in matrix.entries if entry.name in set(only)]


def job_argv(entry: MatrixEntry, *, port: int, shared: Sequence[str]) -> list[str]:
    """The arguments of one child ``test-vm.py``: the entry decides the scenario and sizing, the caller's shared options come first."""
    return [*shared, "--scenario", entry.scenario, "--memory", str(entry.memory_mib), "--cpus", str(entry.cpus), "--ssh-port", str(port), *entry.options]


def host_memory_available_mib(meminfo: Path = Path("/proc/meminfo")) -> int:
    """MemAvailable of the host in MiB (0 when it cannot be read)."""
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def format_plan(matrix: Matrix, entries: Sequence[MatrixEntry], *, parallel: int = PARALLEL_MAX, budget_mib: int = DEFAULT_BUDGET_MIB) -> str:
    lines = [f"{matrix.name}: {matrix.title}",
             f"at most {min(parallel, PARALLEL_MAX)} VMs at a time, {budget_mib // 1024} GiB of VM memory in total, {MARGIN_MIB // 1024} GiB of host memory kept free",
             f"{'job':<24} {'scenario':<24} {'memory':>7} {'cpus':>4} {'min':>4}  options and notes"]
    for entry in entries:
        lines.append(f"{entry.name:<24} {entry.scenario:<24} {entry.memory_mib // 1024:>4} GiB {entry.cpus:>4} {entry.minutes:>4}  "
                     f"{' '.join(entry.options)}{' - ' + entry.note if entry.note else ''}")
    total = sum(entry.minutes for entry in entries)
    lines.append(f"about {total} minutes one after the other, about {max(total // max(1, min(parallel, PARALLEL_MAX)), max((e.minutes for e in entries), default=0))} minutes at the cap")
    return "\n".join(lines)


# --- running -------------------------------------------------------------------------------------------------------

class Process(Protocol):
    returncode: int | None

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


@dataclass
class JobResult:
    name: str
    scenario: str
    argv: list[str]
    port: int = 0
    status: str = "PENDING"          # PASS | FAIL | USAGE | KILLED | SKIPPED
    exit_code: int | None = None
    run_dir: str = ""
    log: str = ""
    seconds: float = 0.0
    note: str = ""
    verdict_line: str = field(default="", repr=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("verdict_line", None)
        return data


def classify(code: int | None) -> str:
    if code == 0:
        return "PASS"
    if code == 1:
        return "FAIL"
    if code == 2:
        return "USAGE"
    return "KILLED" if code is not None and code > 128 else "FAIL"


def read_run_dir(log: Path) -> str:
    try:
        found = RUN_DIR_RE.findall(log.read_text(errors="replace"))
    except OSError:
        return ""
    return found[-1][1].strip() if found else ""


class Scheduler:
    """Starts the entries in order, never more than ``parallel`` at once and never more memory than allowed; collects their results."""

    def __init__(self, entries: Sequence[MatrixEntry], *, directory: Path, spawn: Callable[[list[str], Path], Process],
                 parallel: int = PARALLEL_MAX, budget_mib: int = DEFAULT_BUDGET_MIB, base_port: int = 40222,
                 available_mib: Callable[[], int] = host_memory_available_mib, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, poll: float = 2.0, say: Callable[[str], None] = lambda _text: None) -> None:
        self.parallel = max(1, min(parallel, PARALLEL_MAX))
        self.budget_mib = budget_mib
        self.directory = directory
        self.spawn, self.available_mib, self.clock, self.sleep, self.poll, self.say = spawn, available_mib, clock, sleep, poll, say
        self.base_port = base_port
        self.entries = list(entries)
        self.pending = list(entries)
        self.running: list[tuple[MatrixEntry, JobResult, Process, float]] = []
        self.results: list[JobResult] = []
        self.interrupted = False

    # -- one step of the loop ------------------------------------------------------------------------------------------

    def free_port(self) -> int:
        used = {result.port for _entry, result, _process, _started in self.running}
        return next(self.base_port + slot for slot in range(self.parallel) if self.base_port + slot not in used)

    def memory_in_use(self) -> int:
        return sum(entry.memory_mib for entry, _result, _process, _started in self.running)

    def fits(self, entry: MatrixEntry) -> bool:
        return self.memory_in_use() + entry.memory_mib <= self.budget_mib and entry.memory_mib + MARGIN_MIB <= self.available_mib()

    def start(self, entry: MatrixEntry, shared: Sequence[str]) -> None:
        port = self.free_port()
        log = self.directory / f"{entry.name}.log"
        argv = job_argv(entry, port=port, shared=shared)
        result = JobResult(entry.name, entry.scenario, argv, port=port, log=str(log))
        process = self.spawn(argv, log)
        self.running.append((entry, result, process, self.clock()))
        self.say(f"START {entry.name} (port {port}, {entry.sizing()}): log {log}")

    def finish(self, entry: MatrixEntry, result: JobResult, started: float) -> None:
        result.seconds = round(self.clock() - started, 1)
        result.run_dir = read_run_dir(Path(result.log))
        self.results.append(result)
        self.say(f"{result.status} {entry.name} in {result.seconds:.0f}s" + (f": {result.run_dir}" if result.run_dir else ""))

    def reap(self) -> None:
        for item in list(self.running):
            entry, result, process, started = item
            code = process.poll()
            if code is None:
                continue
            self.running.remove(item)
            result.exit_code, result.status = code, classify(code)
            self.finish(entry, result, started)

    def skip(self, entry: MatrixEntry, shared: Sequence[str], note: str) -> None:
        self.results.append(JobResult(entry.name, entry.scenario, job_argv(entry, port=0, shared=shared), status="SKIPPED", note=note))
        self.say(f"SKIPPED {entry.name}: {note}")

    def run(self, shared: Sequence[str]) -> list[JobResult]:
        while (self.pending or self.running) and not self.interrupted:
            self.reap()
            while self.pending and len(self.running) < self.parallel:
                entry = self.pending[0]
                if not self.fits(entry):
                    if self.running:
                        break  # wait for a running job to give its memory back
                    self.pending.pop(0)
                    self.skip(entry, shared, f"not enough memory: {entry.memory_mib} MiB for the VM plus {MARGIN_MIB} MiB kept free "
                                             f"(budget {self.budget_mib} MiB, the host has {self.available_mib()} MiB available)")
                    continue
                self.pending.pop(0)
                self.start(entry, shared)
            if self.pending or self.running:
                self.sleep(self.poll)
        return self.ordered()

    def ordered(self) -> list[JobResult]:
        order = {entry.name: index for index, entry in enumerate(self.entries)}
        return sorted(self.results, key=lambda result: order.get(result.name, len(order)))

    def stop_all(self, grace: float = 60.0) -> None:
        """Ask every running child to stop (its own cleanup stops its QEMU), then make sure; the jobs that never started are skipped."""
        self.interrupted = True
        for _entry, _result, process, _started in self.running:
            if process.poll() is None:
                process.terminate()
        deadline = self.clock() + grace
        for item in list(self.running):
            entry, result, process, started = item
            while process.poll() is None and self.clock() < deadline:
                self.sleep(0.5)
            if process.poll() is None:
                process.kill()
            self.running.remove(item)
            result.exit_code, result.status = process.poll(), "KILLED"
            self.finish(entry, result, started)
        for entry in self.pending:
            self.results.append(JobResult(entry.name, entry.scenario, [], status="SKIPPED", note="the matrix was interrupted before this job started"))
        self.pending = []


def spawn_child(script: Path) -> Callable[[list[str], Path], subprocess.Popen]:
    """A spawner that starts ``python script argv`` with its output in the job's log (own session: it handles its own signals)."""
    def spawn(argv: list[str], log: Path) -> subprocess.Popen:
        stream = log.open("ab")
        try:
            return subprocess.Popen([sys.executable, str(script), *argv], stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            stream.close()
    return spawn


def summarize(matrix: Matrix, results: Sequence[JobResult]) -> dict[str, Any]:
    return {"matrix": matrix.name, "success": bool(results) and all(result.status == "PASS" for result in results),
            "passed": [result.name for result in results if result.status == "PASS"],
            "failed": [result.name for result in results if result.status != "PASS"],
            "jobs": [result.to_dict() for result in results]}


def write_summary(directory: Path, matrix: Matrix, results: Sequence[JobResult]) -> Path:
    path = directory / "matrix-result.json"
    path.write_text(json.dumps(summarize(matrix, results), indent=2) + "\n")
    return path


def format_results(results: Sequence[JobResult]) -> str:
    lines = [f"{'job':<24} {'result':<8} {'minutes':>8}  run directory"]
    for result in results:
        lines.append(f"{result.name:<24} {result.status:<8} {result.seconds / 60:>8.1f}  {result.run_dir or result.note}")
    return "\n".join(lines)
