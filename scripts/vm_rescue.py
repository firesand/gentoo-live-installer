"""Rescue scenarios: the distribution images they run on, and everything pure about repairing with gentoo-rescue.

``scripts/test-vm.py`` owns the virtual machines; this module owns what is judged:

* ``IMAGES``: the official cloud images (Debian, Ubuntu, Fedora, Arch) and what the rescue tool is
  expected to do with each (repair it, or only detect and diagnose it);
* the serial console of a booted image: is there a login prompt, did it panic, did the kernel get the
  live medium's arguments;
* ``RescueDriver``: the non-interactive repair, one ``gentoo-rescue`` command at a time (scan, mount,
  diagnose, plan, run, unmount), with the exit codes and the JSON events the command line really prints.

Nothing here starts a process: the driver gets a ``run(remote_command, log_name, timeout)`` function
(``ScenarioRunner.ssh`` in a real run, a fake in tests) and returns what the commands said.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from vm_probes import ANSI_RE

# --- the images ------------------------------------------------------------------------------------------------------

BREAK_MODES = ("grub-cfg", "initramfs", "fstab-uuid")
HEAD_BYTES = 1 << 20


@dataclass(frozen=True)
class RescueImage:
    """An official cloud image used as the machine that does not boot."""

    key: str
    filename: str                  # the file name in the images directory
    pattern: str                   # a glob that finds it when the exact name is not there (a newer release)
    title: str
    os_id: str                     # ID= of its /etc/os-release: what scan must report
    family: str                    # the family the rescue tool must put it in
    root_fs: str
    layout: str                    # one line: what the partition table looks like
    break_mode: str                # the damage the rescue plans can repair on it
    repairable: bool = True        # False: detection and diagnosis only, writes are refused by design
    final_boots: tuple[str, ...] = ("uefi", "bios")
    note: str = ""


IMAGES: dict[str, RescueImage] = {
    "debian": RescueImage(
        "debian", "debian-13-genericcloud-amd64.qcow2", "debian-*-genericcloud-amd64.qcow2", "Debian 13 (genericcloud)", "debian", "debian", "ext4",
        "p1 root ext4, p14 BIOS boot, p15 ESP; /boot is part of the root", "grub-cfg",
        note="fstab uses labels, so the damage is the menu file: update-grub makes it again"),
    "ubuntu": RescueImage(
        "ubuntu", "noble-server-cloudimg-amd64.img", "*-server-cloudimg-amd64.img", "Ubuntu 24.04 (cloudimg)", "ubuntu", "debian", "ext4",
        "p1 root ext4, p14 BIOS boot, p15 ESP, p16 /boot (ext4, a partition of its own)", "initramfs",
        note="the initramfs lives on the separate /boot partition: rescue must mount it from fstab to repair it"),
    "fedora": RescueImage(
        "fedora", "Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2", "Fedora-Cloud-Base-Generic-*.x86_64.qcow2", "Fedora Cloud Base", "fedora", "fedora", "btrfs",
        "p1 BIOS boot, p2 ESP, p3 root btrfs with subvolumes", "grub-cfg",
        note="BLS entries and SELinux: the first boot after a repair relabels the files and restarts once"),
    "arch": RescueImage(
        "arch", "Arch-Linux-x86_64-cloudimg.qcow2", "Arch-Linux-x86_64-cloudimg*.qcow2", "Arch Linux (cloudimg)", "arch", "arch", "btrfs",
        "p1 BIOS boot, p2 ESP, p3 root btrfs", "", repairable=False, final_boots=(),
        note="detected and diagnosed, never written to: the tool refuses repairs for Arch by design"),
}


class RescueFlowError(RuntimeError):
    """The default error of the driver; test-vm.py passes HarnessError instead."""


def resolve_image(directory: Path, image: RescueImage, error: type[Exception] = RescueFlowError) -> Path:
    """The file of ``image`` below ``directory``: the exact name, else the only file the pattern finds."""
    exact = directory / image.filename
    if exact.is_file():
        return exact
    found = sorted(path for path in directory.glob(image.pattern) if path.is_file()) if directory.is_dir() else []
    if len(found) == 1:
        return found[0]
    if found:
        raise error(f"several files in {directory} look like the {image.title} image ({', '.join(path.name for path in found)}): "
                    f"remove the ones you do not want or pass --rescue-images with a directory that has one")
    raise error(f"the {image.title} image {image.filename} was not found in {directory}: download it there (it is only ever used as a read-only backing file) "
                "or pass --rescue-images DIR")


def fingerprint(path: Path) -> dict[str, Any]:
    """Enough to notice that a base image was written to: size, modification time, and a checksum of its first MiB."""
    status = path.stat()
    with path.open("rb") as stream:
        head = hashlib.sha256(stream.read(HEAD_BYTES)).hexdigest()
    return {"path": str(path), "size": status.st_size, "mtime_ns": status.st_mtime_ns, "head_sha256": head}


def fingerprint_problems(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    names = {"size": "size", "mtime_ns": "modification time", "head_sha256": "first MiB"}
    return [f"the {label} of the base image changed" for key, label in names.items() if before.get(key) != after.get(key)]


# --- the serial console of a booted machine -----------------------------------------------------------------------------

# "debian login:" at the start of a line; Ubuntu goes on writing cloud-init messages after the prompt on the same line.
LOGIN_RE = re.compile(r"(?m)^[ \t]*[\w.-]+ login:")
# Text that means the machine will not come up on its own: waiting longer cannot help.
FATAL_MARKERS = (
    "Kernel panic - not syncing", "VFS: Unable to mount root fs", "Entering emergency mode", "You are in emergency mode",
    "Give root password for maintenance", "Cannot open access to console, the root account is locked", "grub rescue>",
    "UEFI Interactive Shell", "No bootable device", "Dropping to a shell", "Starting Dracut Emergency Shell", "dracut-emergency",
)
FATAL_PATTERNS = (re.compile(r"(?m)^grub> ?"), re.compile(r"(?m)^\(initramfs\) "))
# Evidence worth keeping when a system is broken (a superset of the fatal markers).
EVIDENCE_MARKERS = FATAL_MARKERS + ("grub>", "error: file", "error: unknown filesystem", "error: no such device", "Boot failed", "Failed to mount", "Dependency failed for")
REBOOT_MARKERS = ("reboot: Restarting system", "systemd-shutdown[1]: Rebooting")
COMMAND_LINE_RE = re.compile(r"(?m)^(?:\[[^\]]*\]\s*)?(?:Kernel command line|Command line):\s*(.+?)\s*$")
LIVE_ARGUMENT_RE = re.compile(r"root=live:\S*|rd\.live\.\S*|\S*CDLABEL=\S*|\bcdroot\b")


def clean_serial(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "")


def login_prompt(text: str) -> str:
    """The login prompt in a serial log ('debian login:'), or ''."""
    match = LOGIN_RE.search(clean_serial(text))
    return match.group(0).strip() if match else ""


def fatal_marker(text: str) -> str:
    cleaned = clean_serial(text)
    for marker in FATAL_MARKERS:
        if marker in cleaned:
            return marker
    for pattern in FATAL_PATTERNS:
        found = pattern.search(cleaned)
        if found:
            return found.group(0).strip()
    return ""


def evidence_markers(text: str) -> list[str]:
    cleaned = clean_serial(text)
    return [marker for marker in EVIDENCE_MARKERS if marker in cleaned]


def rebooted(text: str) -> bool:
    cleaned = clean_serial(text)
    return any(marker in cleaned for marker in REBOOT_MARKERS)


def kernel_command_line(text: str) -> str:
    """The command line the kernel printed at boot ('' when its log level hid it)."""
    found = COMMAND_LINE_RE.findall(clean_serial(text))
    return found[-1] if found else ""


def live_arguments(text: str) -> list[str]:
    """Arguments that belong to the live medium (root=live:..., rd.live..., CDLABEL=...) in a kernel or boot-loader line."""
    return sorted(set(LIVE_ARGUMENT_RE.findall(text)))


def classify_boot(text: str, *, exited: bool = False, exit_code: int | None = None) -> str:
    """'login', 'fatal:<marker>', 'rebooted' (the machine restarted itself), 'exited' or 'waiting'."""
    if login_prompt(text):
        return "login"
    marker = fatal_marker(text)
    if marker:
        return f"fatal:{marker}"
    if exited:
        return "rebooted" if exit_code == 0 and rebooted(text) else "exited"
    return "waiting"


# --- the files a repaired system boots from, read from the live side --------------------------------------------------------

# Run in the live system as: python3 -c <this> '<json>'. The tree is the mounted, untrusted target: nothing is followed outside it.
BOOT_FILES_SNIPPET = r"""
import glob, json, os, re, sys
spec = json.loads(sys.argv[1])
root = os.path.realpath(spec['root'])
pattern = re.compile(spec['pattern'])
report = {'checked': [], 'matches': [], 'skipped': []}
seen = set()
for relative in spec['files']:
    for path in sorted(glob.glob(os.path.join(root, relative))):
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        if not real.startswith(root + os.sep):
            report['skipped'].append({'file': path[len(root):], 'why': 'it points outside the system'})
            continue
        if not os.path.isfile(real) or os.path.getsize(real) > (1 << 20):
            continue
        with open(real, 'rb') as stream:
            text = stream.read().decode('utf-8', 'replace')
        name = real[len(root):]
        report['checked'].append(name)
        for number, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                report['matches'].append({'file': name, 'line': number, 'text': line.strip()[:300]})
print(json.dumps(report))
"""
BOOT_FILES = ("boot/grub/grub.cfg", "boot/grub2/grub.cfg", "boot/efi/EFI/*/grub.cfg", "efi/EFI/*/grub.cfg", "boot/loader/entries/*.conf",
              "boot/efi/loader/entries/*.conf", "efi/loader/entries/*.conf", "etc/kernel/cmdline", "etc/default/grub", "etc/default/grub.d/*.cfg")
# Files that decide what the next boot loads: at least one of them must exist, or the check proved nothing.
MENU_FILES = ("boot/grub/grub.cfg", "boot/grub2/grub.cfg", "boot/efi/EFI/*/grub.cfg", "efi/EFI/*/grub.cfg", "boot/loader/entries/*.conf",
              "boot/efi/loader/entries/*.conf", "efi/loader/entries/*.conf")
LIVE_PATTERN = r"root=live:|rd\.live\.|CDLABEL=|\bcdroot\b"


def boot_files_command(root: str) -> str:
    spec = {"root": root, "files": list(BOOT_FILES), "pattern": LIVE_PATTERN}
    return shlex.join(("python3", "-c", BOOT_FILES_SNIPPET, json.dumps(spec)))


def parse_boot_files(returncode: int, output: str) -> dict[str, Any]:
    """The report of BOOT_FILES_SNIPPET; raises ValueError when there is none."""
    for line in reversed(clean_serial(output).strip().splitlines()):
        try:
            report = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(report, dict) and "checked" in report and "matches" in report:
            return report
    raise ValueError("the live system returned no report on the boot files" + (f" (exit {returncode})" if returncode else ""))


def boot_file_problems(report: Mapping[str, Any]) -> list[str]:
    """Live-medium arguments in a boot file are why a repaired system would fail to boot from its disk; no boot file at all proves nothing."""
    problems = [f"{item['file']}:{item['line']} carries an argument of the live medium: {item['text']}" for item in report.get("matches", [])]
    menu = [name for name in report.get("checked", []) if re.search(r"grub2?/grub\.cfg$|loader/entries/[^/]+\.conf$|EFI/[^/]+/grub\.cfg$", name)]
    if not menu:
        problems.append("no boot menu file (grub.cfg or boot loader entry) was found in the repaired system to check")
    return problems


# --- gentoo-rescue ------------------------------------------------------------------------------------------------------------

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED, EXIT_CONFIRM, EXIT_NOT_FOUND, EXIT_BUSY, EXIT_UNSUPPORTED = 0, 1, 2, 3, 4, 5, 6, 7
EXIT_NAMES = {0: "ok", 1: "failed", 2: "usage", 3: "refused by the safety policy", 4: "needs confirmation", 5: "not found", 6: "busy", 7: "unsupported", 130: "interrupted"}
# Per break mode: the findings a diagnosis must contain and a pattern the plan's commands must match.
EXPECTATIONS: dict[str, dict[str, Any]] = {
    "grub-cfg": {"findings": ("D05",), "plan": r"update-grub|grub-?mkconfig|grub2-mkconfig|grub-?install|reinstall"},
    "initramfs": {"findings": ("D02", "D03", "D15"), "plan": r"update-initramfs|dracut|mkinitcpio|genkernel"},
    "fstab-uuid": {"findings": ("D07",), "plan": r"/etc/fstab"},
}


def parse_cli_output(output: str) -> list[dict[str, Any]]:
    """The JSON events in the output of ``gentoo-rescue --json``; the human text it prints in between is skipped."""
    events: list[dict[str, Any]] = []
    for line in clean_serial(output).splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and "type" in event:
            events.append(event)
    return events


def results_of(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(event["data"]) for event in events if event.get("type") == "result" and isinstance(event.get("data"), dict)]


def last_result(events: Iterable[Mapping[str, Any]], key: str) -> dict[str, Any] | None:
    """The last result event whose data has ``key`` (a mount prints its dry run first, a run prints its plan first)."""
    found = [data for data in results_of(events) if key in data]
    return found[-1] if found else None


def error_of(events: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    for event in events:
        if event.get("type") == "error":
            data = event.get("data") or {}
            return {"code": str(data.get("code") or event.get("error") or ""), "message": str(event.get("message") or event.get("error") or "")}
    return {"code": "", "message": ""}


def last_line(output: str) -> str:
    lines = clean_serial(output).strip().splitlines()
    return lines[-1].strip() if lines else ""


def device_on_disk(device: str, disk: str) -> bool:
    return device == disk or re.fullmatch(re.escape(disk) + r"p?\d+", device) is not None


def systems_on_disk(scan: Mapping[str, Any], disk: str) -> list[dict[str, Any]]:
    """The installed systems scan found on ``disk`` (snapshots are history, not systems)."""
    return [candidate for candidate in scan.get("candidates", []) if device_on_disk(str(candidate.get("device", "")), disk) and candidate.get("kind") != "snapshot"]


def describe_systems(scan: Mapping[str, Any]) -> str:
    items = [f"{c.get('device')} ({(c.get('os') or {}).get('pretty_name') or (c.get('os') or {}).get('id') or 'unknown'}, {c.get('kind', 'root')})"
             for c in scan.get("candidates", [])]
    return ", ".join(items) or "none"


def plan_touches_nvram(plan: Mapping[str, Any]) -> bool:
    """Whether the plan writes firmware boot entries: then the next boot must see the same NVRAM the live session had."""
    if (plan.get("needs") or {}).get("uefi_nvram"):
        return True
    for step in plan.get("steps", []):
        argv = [str(part) for part in step.get("argv", [])]
        if not argv:
            continue
        name = argv[0].rsplit("/", 1)[-1]
        if name == "efibootmgr":
            return True
        if name in ("grub-install", "grub2-install") and "--no-nvram" not in argv and not any(a.startswith("--target=i386-pc") for a in argv):
            return True
        if name == "bootctl" and "--variables=no" not in argv:
            return True
    return False


def plan_commands(plan: Mapping[str, Any]) -> list[str]:
    return [str(step.get("display", "")) for step in plan.get("steps", [])]


def plan_expectation_problems(mode: str, findings: Sequence[Mapping[str, Any]], plan: Mapping[str, Any] | None) -> list[str]:
    """What the diagnosis and the plan must show for the damage that was done."""
    expected = EXPECTATIONS.get(mode)
    if expected is None:
        return []
    ids = {str(item.get("id")) for item in findings}
    problems = []
    if not ids & set(expected["findings"]):
        problems.append(f"the diagnosis does not mention the damage ({' or '.join(expected['findings'])} expected, it reports {', '.join(sorted(ids)) or 'nothing'})")
    if plan is not None:
        commands = [f"{step.get('display', '')} {' '.join(str(p) for p in step.get('argv', []))}" for step in plan.get("steps", [])]
        if not any(re.search(expected["plan"], command) for command in commands):
            problems.append(f"no command of the plan repairs it (a command matching {expected['plan']!r} expected)")
    return problems


def confirmation_flags(plan: Mapping[str, Any]) -> list[str]:
    """The flags ``run`` needs for exactly this plan: its digest, the typed device name of a risky plan, the backup promise of a dangerous one."""
    confirm = plan.get("confirm") or {}
    flags = ["--confirm-plan", str(plan["digest"])]
    if confirm.get("typed"):
        flags += ["--typed", str(confirm["typed"])]
    if confirm.get("phrase"):
        flags.append("--i-have-a-backup")
    return flags


class RescueDriver:
    """Repair a system with ``gentoo-rescue``, step by step and without a person: what an operator types, one command per step.

    ``run(remote, log_name, timeout)`` executes a command line in the live system and returns (exit status, output).
    ``transport`` is "auto" (the daemon when it answers, else this process), "daemon" or "local".
    """

    def __init__(self, run: Callable[[str, str, int], tuple[int, str]], *, transport: str = "auto", error: type[Exception] = RescueFlowError,
                 command_timeout: int = 600, run_timeout: int = 1800) -> None:
        if transport not in ("auto", "daemon", "local"):
            raise ValueError(f"unknown transport {transport!r}")
        self.execute = run
        self.transport = transport
        self.error = error
        self.command_timeout = command_timeout
        self.run_timeout = run_timeout
        self.commands: list[dict[str, Any]] = []

    # -- plumbing -----------------------------------------------------------------------------------------------------

    def argv(self, *arguments: str) -> list[str]:
        base = ["gentoo-rescue", "--json"]
        if self.transport == "local":
            base.insert(1, "--local")
        elif self.transport == "daemon":
            base[1:1] = ["--socket", "/run/gentoo-installer/server.sock"]
        return [*base, *arguments]

    def call(self, name: str, *arguments: str, timeout: int | None = None, allow: Sequence[int] = (EXIT_OK,)) -> tuple[int, list[dict[str, Any]], str]:
        command = shlex.join(self.argv(*arguments))
        code, output = self.execute(command, f"rescue-{name}.log", timeout or self.command_timeout)
        events = parse_cli_output(output)
        self.commands.append({"step": name, "command": command, "exit": code, "events": len(events)})
        if code not in allow:
            reason = error_of(events)["message"] or last_line(output) or "no output"
            raise self.error(f"gentoo-rescue {name} failed with exit status {code} ({EXIT_NAMES.get(code, 'unknown')}): {reason}; see rescue-{name}.log")
        return code, events, output

    # -- the steps ----------------------------------------------------------------------------------------------------

    def scan(self) -> dict[str, Any]:
        _code, events, _output = self.call("scan", "scan", timeout=self.command_timeout)
        data = last_result(events, "candidates")
        if data is None:
            raise self.error("gentoo-rescue scan printed no scan result; see rescue-scan.log")
        return data

    def choose(self, scan: Mapping[str, Any], *, disk: str, os_id: str) -> dict[str, Any]:
        """The system on the guarded disk that is the one under test; the live medium must be hidden from the list."""
        live = set(scan.get("live_medium") or [])
        shown = [str(c.get("device")) for c in scan.get("candidates", [])]
        if live & set(shown):
            raise self.error(f"scan lists the live medium as a system to repair ({', '.join(sorted(live & set(shown)))})")
        found = systems_on_disk(scan, disk)
        wanted = [c for c in found if not os_id or (c.get("os") or {}).get("id") == os_id]
        if len(wanted) == 1:
            return wanted[0]
        if not wanted:
            raise self.error(f"scan found no {os_id or 'Linux'} system on {disk}; it found: {describe_systems(scan)}")
        raise self.error(f"scan found several {os_id or 'Linux'} systems on {disk} ({describe_systems({'candidates': wanted})}); the test needs exactly one")

    def mount(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        _code, events, _output = self.call("mount", "mount", str(candidate["id"]), "--yes")
        data = last_result(events, "session_id")
        if data is None or not data.get("session_id"):
            raise self.error("gentoo-rescue mount printed no session; see rescue-mount.log")
        return data

    def diagnose(self, session: str, *, deep: bool = False) -> dict[str, Any]:
        _code, events, _output = self.call("diagnose", "diagnose", session, *(["--deep"] if deep else []))
        data = last_result(events, "query")
        findings = (data or {}).get("data") if isinstance((data or {}).get("data"), dict) else None
        if findings is None:
            raise self.error("gentoo-rescue diagnose printed no findings; see rescue-diagnose.log")
        return findings

    def plan(self, session: str, action: str) -> dict[str, Any]:
        _code, events, _output = self.call("plan", "plan", session, action)
        data = last_result(events, "digest")
        if data is None:
            raise self.error("gentoo-rescue plan printed no plan; see rescue-plan.log")
        failed = [item.get("text", "") for item in data.get("preconditions", []) if not item.get("ok", True)]
        if failed:
            raise self.error("the plan cannot run here: " + "; ".join(str(text) for text in failed))
        return data

    def refusal(self, session: str, action: str) -> dict[str, Any]:
        """``plan`` for a system the tool only diagnoses: the answer must be 'unsupported' (exit 7) and nothing may have been run."""
        code, events, output = self.call(f"plan-{action}", "plan", session, action, allow=tuple(range(0, 256)))
        failure = error_of(events)
        if code == EXIT_OK:
            raise self.error(f"gentoo-rescue offered a plan for {action} on a system it must only diagnose")
        if code != EXIT_UNSUPPORTED:
            raise self.error(f"gentoo-rescue plan {action} ended with exit status {code} ({EXIT_NAMES.get(code, 'unknown')}), not 7 (unsupported): {failure['message']}")
        return {"action": action, "exit": code, "code": failure["code"], "message": failure["message"] or clean_serial(output).strip()[-300:]}

    def execute_plan(self, session: str, action: str, plan: Mapping[str, Any]) -> dict[str, Any]:
        """Run exactly the plan that was shown (its digest), with the confirmation its risk asks for."""
        _code, events, _output = self.call("run", "run", session, action, *confirmation_flags(plan), timeout=self.run_timeout, allow=(EXIT_OK, EXIT_FAILED))
        result = last_result(events, "outcome")
        if result is None:
            raise self.error("gentoo-rescue run printed no result; see rescue-run.log")
        failed = [f"{step.get('id')} (exit {step.get('rc')})" for step in result.get("steps", []) if step.get("status") == "failed"]
        bad_checks = [str(check.get("text")) for check in result.get("post_checks", []) if not check.get("ok")]
        if result.get("outcome") != "success":
            detail = "; ".join([*(f"step {item} failed" for item in failed), *(f"check failed: {text}" for text in bad_checks)]) or str(result.get("outcome"))
            raise self.error(f"the repair ended with outcome {result.get('outcome')!r}: {detail}; see rescue-run.log")
        return result

    def audit(self, session: str) -> str:
        _code, events, output = self.call("audit", "audit", session, "--text", allow=tuple(range(0, 256)))
        data = last_result(events, "text")
        return str((data or {}).get("text") or clean_serial(output))

    def unmount(self, session: str) -> dict[str, Any]:
        _code, events, _output = self.call("unmount", "unmount", session)
        data = last_result(events, "released") or {}
        if data.get("leftovers"):
            raise self.error("unmount left mounts behind: " + ", ".join(str(item) for item in data["leftovers"]))
        return data


# Run as root in the live system after the unmount: nothing of the repaired system may stay mounted, held or read-only.
# HARNESS_FS_ROOT only exists so the tests can point it at a fixture tree.
CLEANUP_SNIPPET = r"""
import json, os, sys
prefix = os.environ.get('HARNESS_FS_ROOT', '')
disk = os.path.basename(sys.argv[1])
report = {'mounts': [], 'sessions': [], 'read_only': [], 'dm': []}
with open(prefix + '/proc/self/mountinfo') as stream:
    for line in stream:
        fields = line.split()
        if len(fields) > 4 and (fields[4] == '/mnt/rescue' or fields[4].startswith('/mnt/rescue/')):
            report['mounts'].append(fields[4])
directory = prefix + '/run/gentoo-rescue/sessions'
if os.path.isdir(directory):
    report['sessions'] = sorted(name for name in os.listdir(directory) if name.endswith('.json'))
block = prefix + '/sys/class/block'
if os.path.isdir(block):
    for name in sorted(os.listdir(block)):
        if name == disk or name.startswith(disk):
            with open(os.path.join(block, name, 'ro')) as stream:
                if stream.read().strip() != '0':
                    report['read_only'].append('/dev/' + name)
if os.path.isdir(prefix + '/dev/mapper'):
    report['dm'] = sorted(name for name in os.listdir(prefix + '/dev/mapper') if name.startswith('rescue-'))
print(json.dumps(report))
sys.exit(1 if any(report.values()) else 0)
"""


def cleanup_command(disk: str) -> str:
    return shlex.join(("python3", "-c", CLEANUP_SNIPPET, disk))


def cleanup_problems(returncode: int, output: str) -> list[str]:
    for line in reversed(clean_serial(output).strip().splitlines()):
        try:
            report = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(report, dict) and "mounts" in report:
            labels = {"mounts": "still mounted", "sessions": "session state left behind", "read_only": "still marked read-only", "dm": "device-mapper volume left open"}
            return [f"{labels[key]}: {', '.join(report[key])}" for key in labels if report.get(key)]
    return ["the live system returned no cleanup report" + (f" (exit {returncode})" if returncode else "")]
