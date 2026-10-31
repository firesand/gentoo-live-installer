"""Probes of an installed system: pure parsers that judge the output of one command run in the guest.

Each probe is a command (run over SSH as the test user) and a parser that gets the exit status and
the output and returns a ``ProbeResult``. Nothing here starts a process or touches a file, so every
rule is tested on sample output without a VM. ``scripts/test-vm.py`` owns the list of probes and
runs them; this module owns what "correct" means for the checks added for the hardware-aware
installer: the install receipt, the hardware plan, the swap of an encrypted install, the removable
EFI fallback, the boot flag of a BIOS install, the layout of a manual install and the state of the Portage world.
"""
from __future__ import annotations

import inspect
import json
import re
import shlex
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

if TYPE_CHECKING:  # pragma: no cover
    from gentoo_installer.manifest import InstallManifest

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@dataclass
class ProbeResult:
    name: str
    success: bool
    output: str
    error: str = ""
    # An advisory probe records a finding without failing the scenario (unless --strict-probes).
    advisory: bool = False
    # What the probe collected for the record (world-drift: the packages), apart from its verdict.
    data: dict[str, Any] = field(default_factory=dict)

    def record(self) -> dict[str, Any]:
        """The probe as result.json keeps it: ``data`` only when the probe collected some."""
        record = asdict(self)
        if not record["data"]:
            del record["data"]
        return record


def parse_probe_result(name: str, returncode: int, output: str, expected: str | None = None) -> ProbeResult:
    clean = ANSI_RE.sub("", output).strip()
    if returncode != 0:
        return ProbeResult(name, False, clean, f"Probe exited with status {returncode}")
    if expected is not None and clean != expected:
        return ProbeResult(name, False, clean, f"Expected {expected!r}")
    return ProbeResult(name, True, clean)


def sudo_failed(output: str) -> bool:
    """Whether a command that was run through ``sudo -S`` never got to run."""
    text = ANSI_RE.sub("", output).lower()
    return any(marker in text for marker in ("sudo:", "a password is required", "is not in the sudoers", "incorrect password"))


def _sudo_problem(name: str, returncode: int, output: str) -> ProbeResult | None:
    """A probe that needs root cannot judge anything when sudo itself fails: report that, as advisory."""
    if returncode != 0 and sudo_failed(output):
        return ProbeResult(name, False, ANSI_RE.sub("", output).strip()[-400:],
                           "the test user cannot use sudo, so this check could not run (see the sudo probe)", advisory=True)
    return None


# --- the install receipt (docs/contracts.md) ----------------------------------------------------------------------

RECEIPT_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
UUID_RE = re.compile(r"[0-9A-Fa-f][0-9A-Fa-f-]{7,35}")
RECEIPT_KEYS = ("version", "installer", "created", "firmware", "bootloader", "bootloader_id", "esp", "boot_separate", "init", "kernel",
                "initramfs", "uki", "secure_boot", "profile", "root", "cmdline_file", "hardware")
RECEIPT_ROOT_KEYS = ("fstype", "uuid", "luks_uuid", "lvm", "raid")


def efi_mountpoint(manifest: "InstallManifest") -> str:
    """Where the installer mounts the EFI system partition (configure.efi_mountpoint)."""
    storage = manifest.storage
    if storage.firmware != "uefi":
        return ""
    if storage.mode == "manual" and any(row.mountpoint == "/boot/efi" for row in storage.partitions):
        return "/boot/efi"
    return "/efi"


def expected_root_filesystem(manifest: "InstallManifest") -> str:
    storage = manifest.storage
    if storage.mode == "manual":
        return next((row.filesystem for row in storage.partitions if row.mountpoint == "/"), "")
    return storage.filesystem


def receipt_problems(receipt: Mapping[str, Any], manifest: "InstallManifest") -> list[str]:
    """Everything wrong with an install receipt, judged against the manifest the machine was installed from."""
    problems: list[str] = []
    missing = [key for key in RECEIPT_KEYS if key not in receipt]
    if missing:
        problems.append("missing keys: " + ", ".join(missing))
    if receipt.get("version") != 1:
        problems.append(f"version {receipt.get('version')!r} is not 1")
    if not isinstance(receipt.get("installer"), str) or not receipt.get("installer"):
        problems.append("no installer version")
    if not RECEIPT_TIME_RE.fullmatch(str(receipt.get("created", ""))):
        problems.append(f"created {receipt.get('created')!r} is not a UTC time like 2026-10-24T12:00:00Z")
    system, storage = manifest.system, manifest.storage
    expected = {
        "firmware": storage.firmware, "bootloader": system.bootloader, "init": system.init, "kernel": system.kernel,
        "initramfs": system.initramfs, "uki": bool(system.uki), "secure_boot": bool(system.secure_boot),
        "profile": manifest.effective_profile(), "esp": efi_mountpoint(manifest),
    }
    for key, wanted in expected.items():
        if key in receipt and receipt[key] != wanted:
            problems.append(f"{key} is {receipt[key]!r}, the manifest says {wanted!r}")
    raw_root = receipt.get("root")
    root: Mapping[str, Any] = raw_root if isinstance(raw_root, dict) else {}
    missing_root = [key for key in RECEIPT_ROOT_KEYS if key not in root]
    if missing_root:
        problems.append("root is missing: " + ", ".join(missing_root))
    else:
        wanted_fs = expected_root_filesystem(manifest)
        if wanted_fs and root["fstype"] != wanted_fs:
            problems.append(f"root.fstype is {root['fstype']!r}, the manifest says {wanted_fs!r}")
        if not UUID_RE.fullmatch(str(root["uuid"])):
            problems.append(f"root.uuid {root['uuid']!r} is not a filesystem UUID")
        if bool(root["luks_uuid"]) != bool(storage.luks):
            problems.append("root.luks_uuid does not match the encryption choice")
        elif storage.luks and not UUID_RE.fullmatch(str(root["luks_uuid"])):
            problems.append(f"root.luks_uuid {root['luks_uuid']!r} is not a UUID")
        if bool(root["lvm"]) != bool(storage.lvm):
            problems.append("root.lvm does not match the LVM choice")
        if str(root["raid"]) != str(storage.raid_level):
            problems.append(f"root.raid is {root['raid']!r}, the manifest says {storage.raid_level!r}")
    if storage.luks and receipt.get("boot_separate") is not True:
        problems.append("an encrypted install needs a separate /boot, but boot_separate is not true")
    if "hardware" in receipt and (not isinstance(receipt["hardware"], list) or not all(isinstance(line, str) for line in receipt["hardware"])):
        problems.append("hardware is not a list of text lines")
    return problems


def parse_receipt_probe(returncode: int, output: str, manifest: "InstallManifest") -> ProbeResult:
    """/etc/gentoo-live-installer/install.json: absent is a finding, a receipt that contradicts the manifest is a failure."""
    if returncode != 0 or not output.strip():
        return ProbeResult("receipt", False, output.strip(), "the install receipt is missing", advisory=True)
    try:
        receipt = json.loads(output)
    except json.JSONDecodeError as exc:
        return ProbeResult("receipt", False, output.strip()[:300], f"the install receipt is not JSON: {exc}")
    if not isinstance(receipt, dict):
        return ProbeResult("receipt", False, output.strip()[:300], "the install receipt is not a JSON object")
    problems = receipt_problems(receipt, manifest)
    return ProbeResult("receipt", not problems, json.dumps(receipt, sort_keys=True)[:600], "; ".join(problems))


# --- swap ---------------------------------------------------------------------------------------------------------

def parse_swap_probe(returncode: int, output: str, manifest: "InstallManifest") -> ProbeResult:
    """Swap of the requested size must be active (zram does not count).

    An encrypted guided install keeps its swap in a file inside the encrypted root: a swap *partition*
    next to it would put memory contents on disk in the clear, so one is a failure.
    """
    result = parse_probe_result("swap", returncode, output)
    if not result.success:
        return result
    storage = manifest.storage
    wanted = storage.swap_mib * (1 << 20)
    if storage.mode != "erase" or wanted == 0:
        return ProbeResult("swap", True, result.output, "")
    rows: list[tuple[str, str, int]] = []
    for line in result.output.splitlines():
        fields = line.split()
        if len(fields) >= 3 and not fields[0].startswith("/dev/zram") and fields[-1].isdigit():
            rows.append((fields[0], fields[1].lower(), int(fields[-1])))
        elif len(fields) == 2 and not fields[0].startswith("/dev/zram") and fields[-1].isdigit():  # an older swapon without TYPE
            rows.append((fields[0], "", int(fields[-1])))
    if not any(size >= wanted * 0.9 for _name, _kind, size in rows):
        return ProbeResult("swap", False, result.output, f"no active swap of about {storage.swap_mib} MiB")
    if storage.luks:
        partitions = [name for name, kind, _size in rows if kind == "partition"]
        if partitions:
            return ProbeResult("swap", False, result.output,
                               "an encrypted install has a raw swap partition (" + ", ".join(partitions) + "): swap must be a file inside the encrypted root")
        if not any(kind == "file" and size >= wanted * 0.9 for _name, kind, size in rows):
            return ProbeResult("swap", False, result.output, "an encrypted install should keep its swap in a file inside the encrypted root")
    return ProbeResult("swap", True, result.output, "")


# --- the removable EFI fallback ---------------------------------------------------------------------------------------

EFI_FALLBACK_SNIPPET = r"""
import json, os, sys
path = sys.argv[1]
try:
    with open(path, 'rb') as stream:
        magic = stream.read(2).decode('latin-1')
    print(json.dumps({'path': path, 'size': os.path.getsize(path), 'magic': magic}))
except OSError as exc:
    print(json.dumps({'path': path, 'error': str(exc)}))
    sys.exit(1)
"""


def expects_efi_fallback(manifest: "InstallManifest") -> bool:
    """Whether this install must leave EFI/BOOT/BOOTX64.EFI on the ESP (hwplan.resolve_removable_fallback, configure.removable_fallback_wanted)."""
    storage, system, hardware = manifest.storage, manifest.system, manifest.hardware
    if storage.firmware != "uefi" or hardware.removable_fallback == "never":
        return False
    if system.bootloader == "efi-stub" and not system.uki:
        return False  # a bare kernel cannot carry its command line as BOOTX64.EFI
    if storage.mode == "manual":  # a shared ESP keeps what is on it: only a freshly formatted one is guaranteed
        esp = next((row for row in storage.partitions if row.mountpoint in ("/efi", "/boot/efi")), None)
        return bool(esp is not None and esp.format)
    return True


def efi_fallback_command(manifest: "InstallManifest") -> str:
    """The ESP is mounted with umask 0077, so only root can look into it."""
    path = f"{efi_mountpoint(manifest)}/EFI/BOOT/BOOTX64.EFI"
    return "sudo -S -p '' " + shlex.join(("python3", "-c", EFI_FALLBACK_SNIPPET, path))


def parse_efi_fallback_probe(returncode: int, output: str) -> ProbeResult:
    problem = _sudo_problem("efi-fallback", returncode, output)
    if problem:
        return problem
    clean = ANSI_RE.sub("", output).strip()
    try:
        report = json.loads(clean.splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return ProbeResult("efi-fallback", False, clean[-400:], "the check returned no JSON evidence")
    if "error" in report:
        return ProbeResult("efi-fallback", False, clean, f"{report.get('path')} is not readable on the EFI partition: {report['error']}")
    if report.get("size", 0) <= 0:
        return ProbeResult("efi-fallback", False, clean, f"{report.get('path')} is empty")
    if report.get("magic") != "MZ":
        return ProbeResult("efi-fallback", False, clean, f"{report.get('path')} is not an EFI program (it does not start with MZ)")
    return ProbeResult("efi-fallback", True, clean)


# OVMF prints which boot option it started to the serial console: BdsDxe: loading Boot0001 "UEFI ..." from PciRoot(...)/\EFI\BOOT\BOOTX64.EFI
BDS_START_RE = re.compile(r'BdsDxe: starting Boot([0-9A-Fa-f]{4}) "([^"]*)" from (\S+)')


def firmware_boot_choices(serial_text: str) -> list[dict[str, str]]:
    """The boot options OVMF started, in order, from a serial log."""
    return [{"number": number, "name": name, "path": path} for number, name, path in BDS_START_RE.findall(ANSI_RE.sub("", serial_text))]


def booted_through_fallback(serial_text: str) -> bool:
    """Whether the last boot option OVMF started was the removable-media path.

    Older OVMF names the file (``.../\\EFI\\BOOT\\BOOTX64.EFI``). With an empty NVRAM the OVMF of 2026 makes its own option for the
    disk ("UEFI Misc Device", "UEFI QEMU HARDDISK ...") with the device path alone, ``PciRoot(0x0)/Pci(0x2,0x0)``; a device path
    without a file is started through that same default file (UEFI spec 3.5.1.1). The first Wave 6 matrix saw only that form.
    """
    choices = firmware_boot_choices(serial_text)
    if not choices:
        return False
    path, name = choices[-1]["path"], choices[-1]["name"]
    if path.upper().endswith("\\EFI\\BOOT\\BOOTX64.EFI"):
        return True
    # the firmware's own entry for a whole device: a PCI device path, no partition, no file (the UEFI Shell is in the firmware, Fv(...))
    return name.startswith("UEFI ") and path.startswith("PciRoot(") and "\\" not in path and "HD(" not in path.upper()


# --- the boot flag of a BIOS install ----------------------------------------------------------------------------------------------

def decode_mbr(sector: bytes) -> dict:
    # This function also runs in the guest (BOOT_FLAG_SNIPPET is built from its source): keep it standalone and its annotations builtin.
    """The boot signature and the four partition entries of a first sector: status byte (0x80 = active), type, first sector and length."""
    entries = []
    for number in range(1, 5):
        base = 446 + 16 * (number - 1)
        entries.append({"number": number, "status": sector[base], "type": sector[base + 4],
                        "start": int.from_bytes(sector[base + 8:base + 12], "little"), "sectors": int.from_bytes(sector[base + 12:base + 16], "little")})
    return {"signature": sector[510:512].hex(), "entries": entries}


BOOT_FLAG_MARK = "gentoo-vm boot-flag probe"
BOOT_FLAG_SNIPPET = f"# {BOOT_FLAG_MARK}\n" + inspect.getsource(decode_mbr) + r"""
import json, os, re, subprocess, sys

sysfs = sys.argv[1] if len(sys.argv) > 1 else '/sys/class/block'
devices = sys.argv[2] if len(sys.argv) > 2 else '/dev'


def source(mountpoint):
    done = subprocess.run(['findmnt', '-no', 'SOURCE', mountpoint], capture_output=True, text=True)
    return re.sub(r'\[.*\]$', '', done.stdout.strip()) if done.returncode == 0 else ''


def partition_of(name, depth=0):
    base = os.path.join(sysfs, name)
    if os.path.exists(os.path.join(base, 'partition')):
        return name
    slaves = os.path.join(base, 'slaves')
    if depth < 4 and os.path.isdir(slaves):
        for slave in sorted(os.listdir(slaves)):
            found = partition_of(slave, depth + 1)
            if found:
                return found
    return ''


report = {}
try:
    for mountpoint in ('/boot', '/'):
        device = source(mountpoint)
        if device:
            report.update(holder=mountpoint, source=device)
            break
    if not report:
        raise RuntimeError('neither /boot nor / is a mount point')
    name = partition_of(os.path.basename(os.path.realpath(report['source'])))
    if not name:
        raise RuntimeError(report['source'] + ' is not on a partition')
    base = os.path.realpath(os.path.join(sysfs, name))
    disk = os.path.basename(os.path.dirname(base))
    with open(os.path.join(base, 'partition')) as stream:
        report['partition'] = int(stream.read())
    with open(os.path.join(devices, disk), 'rb') as stream:
        sector = stream.read(512)
    report.update(disk='/dev/' + disk, **decode_mbr(sector))
except (OSError, RuntimeError, ValueError) as exc:
    report['error'] = str(exc)
print(json.dumps(report))
sys.exit(1 if 'error' in report else 0)
"""


def boot_flag_command() -> str:
    """Reads the first sector of the disk that holds /boot (else /); the disk can only be read by root."""
    return "sudo -S -p '' " + shlex.join(("python3", "-c", BOOT_FLAG_SNIPPET))


def boot_flag_problems(report: Mapping[str, Any]) -> list[str]:
    """Why strict BIOS firmware would not start this disk: a GPT disk needs the boot flag in its protective MBR, a DOS disk one active partition, the one that holds /boot (else /)."""
    disk = report.get("disk", "the disk")
    if report.get("signature") != "55aa":
        return [f"the first sector of {disk} has no boot signature (55aa), so no firmware starts it"]
    entries = report["entries"]
    problems = [f"partition entry {entry['number']} has the status byte 0x{entry['status']:02x}, which a BIOS refuses (only 0x00 and 0x80 are valid)"
                for entry in entries if entry["status"] not in (0, 0x80)]
    if entries[0]["type"] == 0xEE:  # GPT: the protective MBR is what a BIOS looks at
        if entries[0]["status"] != 0x80:
            problems.append(f"the protective MBR of the GPT disk {disk} has no boot flag (byte 446 is 0x{entries[0]['status']:02x}): "
                            "strict BIOS firmware says 'no bootable device'")
        return problems
    active = [entry["number"] for entry in entries if entry["status"] == 0x80]
    wanted, holder = report.get("partition"), report.get("holder", "/")
    if isinstance(wanted, int) and wanted > 4:
        problems.append(f"{holder} is on logical partition {wanted}, which cannot be active")
    elif not active:
        problems.append(f"no partition of the DOS disk {disk} is active (0x80): strict BIOS firmware says 'no bootable device'")
    elif len(active) > 1:
        problems.append(f"{len(active)} partitions are active ({', '.join(map(str, active))}): a BIOS expects one")
    elif isinstance(wanted, int) and active != [wanted]:
        problems.append(f"partition {active[0]} is active, but {holder} is on partition {wanted}")
    return problems


def parse_boot_flag_probe(returncode: int, output: str) -> ProbeResult:
    problem = _sudo_problem("boot-flag", returncode, output)
    if problem:
        return problem
    clean = ANSI_RE.sub("", output).strip()
    try:
        report = json.loads(clean.splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return ProbeResult("boot-flag", False, clean[-400:], "the check returned no JSON evidence")
    if "error" in report:
        return ProbeResult("boot-flag", False, clean, f"the boot flag of the installed disk could not be read: {report['error']}")
    problems = boot_flag_problems(report)
    return ProbeResult("boot-flag", not problems, clean, "; ".join(problems))


# --- manual layouts -----------------------------------------------------------------------------------------------------------

LAYOUT_COMMAND = "findmnt -rno TARGET,SOURCE; echo ---; swapon --noheadings --raw --show=NAME"


def parse_layout_probe(returncode: int, output: str, rows: Sequence[Any]) -> ProbeResult:
    """Every row of a manual layout (partition, mount point) must be mounted from that partition, swap rows must be active."""
    result = parse_probe_result("layout", returncode, output)
    if not result.success:
        return result
    mounts_text, _, swaps_text = result.output.partition("\n---\n")
    mounted: dict[str, str] = {}
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            mounted[fields[0]] = re.sub(r"\[.*\]$", "", fields[1])  # btrfs shows /dev/vda2[/@]
    swaps = set(swaps_text.split())
    problems: list[str] = []
    for row in rows:
        device, mountpoint, filesystem = row.device, row.mountpoint, row.filesystem
        if filesystem == "swap" or not mountpoint:
            if device not in swaps:
                problems.append(f"swap on {device} is not active")
        elif mounted.get(mountpoint) != device:
            problems.append(f"{mountpoint} is mounted from {mounted.get(mountpoint, 'nothing')}, the layout says {device}")
    return ProbeResult("layout", not problems, result.output, "; ".join(problems))


# --- Portage's view of the installed world ------------------------------------------------------------------------------

WORLD_COMMAND = "sudo -S -p '' emerge --color=n -pvuDN @world"
WORLD_FAILURES = (
    # Every '!!!' line counts except Portage's notice that a binary package did not fit (non matching USE, changed dependencies):
    # the package is then built from source, and the world still resolves.
    (re.compile(r"^!!!(?! The following binary packages have been ignored due to )", re.M), "Portage reported errors (!!!)"),
    (re.compile(r"\[blocks", re.I), "a blocker prevents the update"),
    (re.compile(r"The following (?:USE|keyword|license|mask) changes are necessary", re.I), "the configuration cannot satisfy the world (changes are necessary)"),
    (re.compile(r"dependency conflict", re.I), "a dependency conflict"),
    (re.compile(r"Multiple package instances within a single package slot", re.I), "two versions compete for one slot"),
    (re.compile(r"Problem resolving dependencies|there are no ebuilds to satisfy|circular dependenc", re.I), "dependencies cannot be resolved"),
    (re.compile(r"REQUIRED_USE", re.I), "a REQUIRED_USE constraint is violated"),
)
EBUILD_LINE_RE = re.compile(r"^\[(?:ebuild|binary)\s+([^\]]*)\]\s+(\S+)", re.M)


def summarize_world(output: str) -> dict[str, list[str]]:
    """The packages ``emerge -pvuDN @world`` would merge, grouped: new, upgrade, rebuild."""
    groups: dict[str, list[str]] = {"new": [], "upgrade": [], "rebuild": []}
    for flags, atom in EBUILD_LINE_RE.findall(output):
        if "N" in flags:
            groups["new"].append(atom)
        elif "U" in flags or "D" in flags:
            groups["upgrade"].append(atom)
        elif "R" in flags or "r" in flags:
            groups["rebuild"].append(atom)
    return groups


DRIFT_NOTE = ("information only: right after an install this usually comes from a tree that is newer than the stage3 (new versions, "
              "changed USE defaults), not from the installer")
DRIFT_LISTED = 40  # packages named per group in the text of the probe (the data has all of them)


def plain_atom(atom: str) -> str:
    """cat/pkg-version as emerge prints it, without the slot and the repository (``app-arch/zstd-1.5.7-r1-10:0/1::gentoo``)."""
    return atom.split("::", 1)[0].split(":", 1)[0]


def world_drift(output: str) -> dict[str, Any]:
    """What ``emerge -pvuDN @world`` would still do after an install: how many and which upgrades, new packages and reinstalls."""
    groups = summarize_world(ANSI_RE.sub("", output))
    listed = {"upgrades": [plain_atom(atom) for atom in groups["upgrade"]], "new": [plain_atom(atom) for atom in groups["new"]],
              "reinstalls": [plain_atom(atom) for atom in groups["rebuild"]]}
    return {"counts": {name: len(atoms) for name, atoms in listed.items()}, **listed}


def drift_text(drift: Mapping[str, Any], returncode: int) -> str:
    counts = drift["counts"]
    lines = [f"{counts['upgrades']} upgrades, {counts['new']} new, {counts['reinstalls']} reinstalls ({DRIFT_NOTE})"]
    for name in ("upgrades", "new", "reinstalls"):
        atoms = drift[name]
        if atoms:
            lines.append(f"{name}: " + ", ".join(atoms[:DRIFT_LISTED]) + (f" ... and {len(atoms) - DRIFT_LISTED} more" if len(atoms) > DRIFT_LISTED else ""))
    if returncode != 0 and not any(drift[name] for name in ("upgrades", "new", "reinstalls")):
        lines.append(f"nothing could be listed: emerge exited with status {returncode}")
    return "\n".join(lines)


def parse_world_probe(returncode: int, output: str) -> list[ProbeResult]:
    """Two results from one command: the world must resolve (a failure), and what it would still change is information.

    The drift (upgrades, new packages, reinstalls) never fails a job: right after an install it comes from the tree being
    newer than the stage3, which is not the installer's doing. It is kept in ``data`` for result.json.
    """
    problem = _sudo_problem("world-resolve", returncode, output)
    if problem:
        return [problem]
    clean = ANSI_RE.sub("", output)
    failures = [text for pattern, text in WORLD_FAILURES if pattern.search(clean)]
    if returncode != 0 and not failures:
        failures.append(f"emerge exited with status {returncode}")
    drift = world_drift(clean)
    counts = drift["counts"]
    tail = "\n".join(clean.strip().splitlines()[-12:])
    summary = f"{counts['upgrades']} upgrades, {counts['new']} new, {counts['reinstalls']} reinstalls"
    resolve = ProbeResult("world-resolve", not failures, f"{summary}\n{tail}", "; ".join(failures))
    return [resolve, ProbeResult("world-drift", True, drift_text(drift, returncode), data=drift)]


# --- a part the installer could not install -----------------------------------------------------------------------------------

COMPANION_PACKAGES = {"hwmonux": "app-misc/hwmonux", "portagehub": "app-portage/portagehub", "gentoo-settings": "app-admin/gentoo-settings",
                      "kernelsmith": "sys-kernel/kernelsmith"}


def explain_missing_part(result: ProbeResult, degraded: Sequence[str]) -> ProbeResult:
    """A companion probe that failed gets the installer's own words about that package, when its final event named it as not installed.

    'hwmonux: Probe exited with status 127' says that the program is not there; the reason is in the installer's report.
    """
    package = COMPANION_PACKAGES.get(result.name)
    if result.success or package is None:
        return result
    reasons = [text for text in degraded if package in text or result.name in text.lower()]
    return replace(result, error=f"{result.error} (the installer reported: {'; '.join(reasons)})") if reasons else result


# --- the hardware plan -------------------------------------------------------------------------------------------------

INTEL_MICROCODE = "sys-firmware/intel-microcode"
GUEST_TOOL_ATOMS = ("app-emulation/qemu-guest-agent", "app-emulation/spice-vdagent", "app-emulation/open-vm-tools",
                    "app-emulation/virtualbox-guest-additions")
# The guest tools a hypervisor's DMI strings must get (a QEMU guest with another vendor's strings is not asserted).
HYPERVISOR_TOOL = {"qemu": "app-emulation/qemu-guest-agent", "vmware": "app-emulation/open-vm-tools", "virtualbox": "app-emulation/virtualbox-guest-additions"}


def plan_atoms(plan: Mapping[str, Any], *fields: str) -> list[str]:
    names = fields or ("early_atoms", "driver_atoms", "required_atoms", "optional_atoms")
    return [str(atom) for field_name in names for atom in plan.get(field_name, []) if isinstance(atom, str)]


def hardware_plan_problems(plan: Mapping[str, Any], manifest: "InstallManifest", smbios: str = "qemu") -> list[str]:
    """What the installer decided for this virtual machine must be what a virtual machine needs."""
    problems: list[str] = []
    if plan.get("schema") not in (1, 2):
        problems.append(f"the plan has schema {plan.get('schema')!r}, not 1 or 2")
    portable = manifest.hardware.target == "portable"
    early = plan_atoms(plan, "early_atoms", "driver_atoms", "required_atoms")
    if INTEL_MICROCODE in early and not portable:
        problems.append("the plan installs Intel microcode in a virtual machine (the hypervisor owns the microcode)")
    tools = [atom for atom in plan_atoms(plan) if atom in GUEST_TOOL_ATOMS]
    guest_tools = manifest.hardware.guest_tools
    if guest_tools == "none" and tools:
        problems.append("guest tools are switched off, but the plan lists " + ", ".join(tools))
    wanted = HYPERVISOR_TOOL.get(smbios)
    if guest_tools == "auto" and wanted and wanted not in plan_atoms(plan, "optional_atoms", "required_atoms"):
        problems.append(f"guest tools are on automatic and this machine looks like {smbios}, but the plan does not list {wanted}")
    return problems


def package_check_command(atoms: Iterable[str]) -> str:
    """Prints 'installed <atom>' or 'absent <atom>' for each atom, from the package database of the installed system."""
    script = ('for atom in "$@"; do if ls -d "/var/db/pkg/${atom}"-[0-9]* > /dev/null 2>&1; then echo "installed $atom"; '
              'else echo "absent $atom"; fi; done')
    return shlex.join(("sh", "-c", script, "sh", *atoms))


def parse_hardware_probe(returncode: int, output: str, plan: Mapping[str, Any], manifest: "InstallManifest", degraded: Sequence[str] = ()) -> ProbeResult:
    """The installed system must match the plan: no Intel microcode, and every guest tool of the plan, unless the installer said it failed."""
    result = parse_probe_result("hardware-plan", returncode, output)
    if not result.success:
        return result
    state: dict[str, bool] = {}
    for line in result.output.splitlines():
        kind, _, atom = line.partition(" ")
        if kind in ("installed", "absent") and atom:
            state[atom] = kind == "installed"
    problems: list[str] = []
    if state.get(INTEL_MICROCODE) and manifest.hardware.target != "portable":
        problems.append("Intel microcode is installed in a virtual machine")
    degraded_text = " ".join(degraded).lower()
    for atom in [a for a in plan_atoms(plan) if a in GUEST_TOOL_ATOMS]:
        if state.get(atom):
            continue
        if atom.split("/", 1)[1].lower() in degraded_text:
            continue  # an optional package that failed is reported as degraded: the install says so itself
        problems.append(f"the plan asked for {atom}, it is not installed and the installer reported no problem")
    if manifest.hardware.guest_tools == "none":
        problems += [f"{atom} is installed although guest tools are switched off" for atom in GUEST_TOOL_ATOMS if state.get(atom)]
    return ProbeResult("hardware-plan", not problems, result.output, "; ".join(problems))


# virtual/dist-kernel's providers. RC3 emerged the firmware and microcode before the kernel, so Portage took the first one,
# gentoo-kernel, and compiled it next to gentoo-kernel-bin (found in the RC4 VMs): exactly the chosen one must be installed.
DIST_KERNEL_PROVIDERS = ("sys-kernel/gentoo-kernel", "sys-kernel/gentoo-kernel-bin", "sys-kernel/vanilla-kernel", "sys-kernel/gentoo-kernel-modprep")


def parse_kernel_provider_probe(returncode: int, output: str, kernel: str) -> ProbeResult:
    """package_check_command(DIST_KERNEL_PROVIDERS): the chosen distribution kernel is installed, and no other one."""
    result = parse_probe_result("kernel-provider", returncode, output)
    if not result.success:
        return result
    installed = {atom for kind, _, atom in (line.partition(" ") for line in result.output.splitlines()) if kind == "installed"}
    wanted = f"sys-kernel/{kernel}"
    problems = [] if wanted in installed else [f"{wanted} is not installed"]
    if others := sorted(installed - {wanted}):
        problems.append(f"{', '.join(others)} is installed next to {wanted}: a second kernel, built from source again with every update")
    return ProbeResult("kernel-provider", not problems, result.output, "; ".join(problems))


def parse_hardware_plan_file(text: str) -> dict[str, Any] | None:
    """The plan in the hardware-plan.json the daemon leaves in its state directory; None when it is absent or holds no plan.

    The daemon writes a record, ``{version, fingerprint, created, plan}`` (the fingerprint binds it to the manifest, so that a
    resume does not decide again). A bare plan, which has a ``schema`` of its own, is accepted too.
    """
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(record, dict) and isinstance(record.get("plan"), dict):
        record = record["plan"]
    return record if isinstance(record, dict) and "schema" in record else None


# --- administrators and the installer's own files ---------------------------------------------------------------------

# What a password dialog does: pkexec in a real terminal, answered with the user's own password when it asks. The wizard turns root's login off by default,
# so a dialog that wants root can never be passed. Gentoo Settings, PortageHub and KernelSmith all get their root rights this way.
POLKIT_COMMAND = r"""command -v pkexec >/dev/null || { echo 'no pkexec'; exit 0; }
python3 -c '
import os, pty, select, sys, time
password = sys.stdin.readline().encode()
pid, terminal = pty.fork()
if pid == 0:
    os.execvp("pkexec", ["pkexec", "/usr/bin/id", "-u"])
seen, sent, end = b"", False, time.monotonic() + 60
while time.monotonic() < end:
    if not select.select([terminal], [], [], 1)[0]:
        continue
    try:
        data = os.read(terminal, 4096)
    except OSError:
        break
    if not data:
        break
    seen += data
    if not sent and b"Password:" in seen:
        os.write(terminal, password)
        sent = True
print(seen.decode(errors="replace"))
'"""
CONFIG_PROPOSALS_COMMAND = "find /etc -name '._cfg*' 2>/dev/null; true"


def parse_polkit_probe(returncode: int, output: str, manifest: Any) -> ProbeResult:
    result = parse_probe_result("polkit", returncode, output)
    accounts = manifest.accounts
    if not result.success or output.strip() == "no pkexec" or accounts.privilege == "su" or "wheel" not in accounts.groups:
        return result  # su asks for root's password by choice, and a user outside wheel is no administrator
    if "Authenticating as: root" in output:
        return ProbeResult("polkit", False, output, "polkit asks for root's password, not the user's: the wheel rule did not reach the installed system")
    if not re.search(r"^0\s*$", ANSI_RE.sub("", output), re.MULTILINE):
        return ProbeResult("polkit", False, output, "pkexec did not get root with the user's own password")
    return result


def parse_config_proposals_probe(returncode: int, output: str) -> ProbeResult:
    """A new system does not ask after its first emerge to merge a stock file over the one the installer wrote."""
    result = parse_probe_result("config-proposals", returncode, output)
    if result.success and output.strip():
        return ProbeResult("config-proposals", False, output, f"Portage's stock files wait beside the installer's: {' '.join(output.split())}")
    return result


# --- system tuning (gentoo_installerd/hwplan_tuning.py) ----------------------------------------------------------------

# One "key value" line per fact; the sysctl --system run (as root, it only applies the files again) reports a key no kernel has.
# A key written with a leading "-" may be refused: procps says 'setting key "K", ignoring: ...' (a VM without a hardware
# NMI watchdog answers ENOTSUPP, "Unknown error 524", to kernel.nmi_watchdog), which is what the "-" is for.
# Kernels up to 6.18 make that same key read-only there. procps checks the mode before it looks at the "-", so it says
# 'setting key "K": Operation not permitted' and sysctl --system exits 1, with every other key applied. Only a manual kernel gets here.
SYSCTL_TOLERATED = re.compile(r'sysctl: setting key "(?:[^"]+", ignoring: |kernel\.nmi_watchdog": Operation not permitted$)')
TUNING_COMMAND = r"""for f in vm/vfs_cache_pressure vm/dirty_bytes vm/swappiness vm/page-cluster; do printf 'sysctl %s %s\n' "$f" "$(cat /proc/sys/$f)"; done
printf 'thp_defrag %s\n' "$(cat /sys/kernel/mm/transparent_hugepage/defrag 2>/dev/null)"
printf 'thp_ptes %s\n' "$(cat /sys/kernel/mm/transparent_hugepage/khugepaged/max_ptes_none 2>/dev/null)"
for q in /sys/block/*/queue/scheduler; do d=${q#/sys/block/}; d=${d%%/*}; printf 'sched %s %s %s\n' "$d" "$(cat /sys/block/$d/queue/rotational)" "$(cat $q)"; done
if [ -r /sys/module/snd_hda_intel/parameters/power_save ]; then printf 'hda %s\n' "$(cat /sys/module/snd_hda_intel/parameters/power_save)"; fi
printf 'cmdline %s\n' "$(cat /proc/cmdline)"
sudo -S -p '' sysctl --system 2>&1 >/dev/null | sed 's/^/sysctl_error /'
true"""
SCHEDULER_FOR = ((re.compile(r"sd[a-z]+"), "1", "bfq"), (re.compile(r"sd[a-z]+|mmcblk\d+"), "0", "mq-deadline"), (re.compile(r"nvme\d+n\d+"), "0", "kyber"))


def tuning_expected(plan: Mapping[str, Any]) -> dict[str, bool] | None:
    """What the plan asked for: None when tuning is off or the plan predates it (an older ISO)."""
    scalars = plan.get("scalars") or {}
    if scalars.get("tuning") != "on":
        return None
    paths = {item[0] if isinstance(item, (list, tuple)) else item.get("path") for item in plan.get("files") or []}
    # By name without the number in front: RC4 moved them ahead of Gentoo Settings' own files (60-/59-/19-, were 80-/60-/80-).
    has = lambda folder, name: any(str(path).startswith(folder) and str(path).endswith(name) for path in paths)  # noqa: E731
    return {"zram": scalars.get("zram") == "yes", "schedulers": has("/etc/udev/rules.d/", "-installer-ioschedulers.rules"),
            "thp": "/etc/tmpfiles.d/installer-thp.conf" in paths, "audio": has("/etc/udev/rules.d/", "-installer-audio-power.rules")}


def tuning_problems(output: str, expected: Mapping[str, bool]) -> list[str]:
    facts: dict[str, str] = {}
    schedulers: list[tuple[str, str, str]] = []
    problems: list[str] = []
    for line in output.splitlines():
        key, _, rest = line.partition(" ")
        if key == "sysctl":
            name, _, value = rest.partition(" ")
            facts[name] = value.strip()
        elif key == "sched":
            parts = rest.split(" ", 2)
            if len(parts) == 3:
                device, rotational, choices = parts
                bracketed = re.search(r"\[([^\]]+)\]", choices)
                schedulers.append((device, rotational, bracketed.group(1) if bracketed else choices.strip()))
        elif key == "sysctl_error":
            if not SYSCTL_TOLERATED.match(rest.strip()):
                problems.append(f"sysctl --system: {rest.strip()}")
        else:
            facts[key] = rest.strip()
    for name, want in (("vm/vfs_cache_pressure", "50"), ("vm/dirty_bytes", "268435456")):
        if facts.get(name) != want:
            problems.append(f"{name} is {facts.get(name)!r}, the tuning sets {want}")
    if expected.get("zram"):
        for name, want in (("vm/swappiness", "150"), ("vm/page-cluster", "0")):
            if facts.get(name) != want:
                problems.append(f"{name} is {facts.get(name)!r}, zram's setting is {want}")
        if "zswap.enabled=0" not in facts.get("cmdline", "").split():
            problems.append("zswap.enabled=0 is not on the kernel command line, although zram is on")
    if expected.get("thp"):
        if "[defer+madvise]" not in facts.get("thp_defrag", ""):
            problems.append(f"transparent_hugepage/defrag is {facts.get('thp_defrag')!r}, the tuning sets defer+madvise")
        if facts.get("thp_ptes") != "409":
            problems.append(f"khugepaged/max_ptes_none is {facts.get('thp_ptes')!r}, the tuning sets 409")
    if expected.get("schedulers"):
        for device, rotational, active in schedulers:
            scheduler = next((name for pattern, rota, name in SCHEDULER_FOR if pattern.fullmatch(device) and rota == rotational), None)
            if scheduler and active != scheduler:
                problems.append(f"{device} uses the {active} scheduler, the tuning sets {scheduler}")
    if expected.get("audio") and facts.get("hda", "0") != "0":
        problems.append(f"snd_hda_intel power_save is {facts.get('hda')}, it should be 0 without a battery")
    return problems


def parse_tuning_probe(returncode: int, output: str, expected: Mapping[str, bool]) -> ProbeResult:
    result = parse_probe_result("tuning", returncode, output)
    if not result.success:
        return result
    problems = tuning_problems(result.output, expected)
    return ProbeResult("tuning", not problems, result.output, "; ".join(problems))
