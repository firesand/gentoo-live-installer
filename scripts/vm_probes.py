"""Probes of an installed system: pure parsers that judge the output of one command run in the guest.

Each probe is a command (run over SSH as the test user) and a parser that gets the exit status and
the output and returns a ``ProbeResult``. Nothing here starts a process or touches a file, so every
rule is tested on sample output without a VM. ``scripts/test-vm.py`` owns the list of probes and
runs them; this module owns what "correct" means for the checks added for the hardware-aware
installer: the install receipt, the hardware plan, the swap of an encrypted install, the removable
EFI fallback, the layout of a manual install and the state of the Portage world.
"""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
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
    """Whether the last boot option OVMF started was the removable-media path."""
    choices = firmware_boot_choices(serial_text)
    return bool(choices) and choices[-1]["path"].upper().endswith("\\EFI\\BOOT\\BOOTX64.EFI")


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
    (re.compile(r"^!!!", re.M), "Portage reported errors (!!!)"),
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


def parse_world_probe(returncode: int, output: str) -> list[ProbeResult]:
    """Two verdicts from one command: the world must resolve (a failure), and should not want rebuilds (a finding)."""
    problem = _sudo_problem("world-resolve", returncode, output)
    if problem:
        return [problem]
    clean = ANSI_RE.sub("", output)
    failures = [text for pattern, text in WORLD_FAILURES if pattern.search(clean)]
    if returncode != 0 and not failures:
        failures.append(f"emerge exited with status {returncode}")
    groups = summarize_world(clean)
    tail = "\n".join(clean.strip().splitlines()[-12:])
    summary = f"{len(groups['new'])} new, {len(groups['upgrade'])} upgrades, {len(groups['rebuild'])} rebuilds"
    resolve = ProbeResult("world-resolve", not failures, f"{summary}\n{tail}", "; ".join(failures))
    drift = groups["rebuild"]
    drift_text = ""
    if drift:
        drift_text = ("installed packages would be rebuilt because their USE flags differ from the configuration: "
                      + ", ".join(drift[:12]) + (" ..." if len(drift) > 12 else ""))
    return [resolve, ProbeResult("world-drift", not drift, summary, drift_text, advisory=True)]


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
    if plan.get("schema") != 1:
        problems.append(f"the plan has schema {plan.get('schema')!r}, not 1")
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


def parse_hardware_plan_file(text: str) -> dict[str, Any] | None:
    """The hardware-plan.json the daemon leaves in its state directory; None when it is absent or not a plan."""
    try:
        plan = json.loads(text)
    except json.JSONDecodeError:
        return None
    return plan if isinstance(plan, dict) and "schema" in plan else None
