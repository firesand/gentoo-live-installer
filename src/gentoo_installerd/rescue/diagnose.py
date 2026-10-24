"""Static diagnosis of a mounted system: findings with evidence and the actions that fix them (no commands are run).

Everything is derived from files read through ``SessionView``: the boot inventory (kernels, initramfs, loader files and
configuration), fstab and crypttab against the real devices, the ESP and the firmware matrix. A finding carries the ids of
the actions that address it, so the interface can mark them "recommended" in the order fstab, kernel, initramfs, config,
bootloader, NVRAM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import efi
from .cmdline import contains_live_arguments
from .devices import GPT_BIOS_BOOT
from .view import SessionView

HEX_UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,36}$")
ROOT_UUID_RE = re.compile(r"\broot=(?:UUID=)?(?:\"?)([0-9a-fA-F-]{36})\b")
SEARCH_UUID_RE = re.compile(r"--fs-uuid[^\n]*?\b([0-9a-fA-F-]{36}|[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4})\b")
MIN_FREE = 50 << 20
ORDER = ["fstab.fix", "boot.kernel", "boot.initramfs", "boot.config", "boot.bootloader", "boot.esp_fallback", "efi.add"]


@dataclass
class Finding:
    id: str
    severity: str  # info | warn | error
    title: str
    detail: str
    evidence: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "severity": self.severity, "title": self.title, "detail": self.detail, "evidence": self.evidence, "actions": self.actions}


def _graph_uuids(view: SessionView) -> set[str]:
    values: set[str] = set()
    for node in view.graph:
        for value in (node.uuid, node.partuuid):
            if value:
                values.add(value.lower())
    return values


def diagnose(view: SessionView, *, hardware_changed: bool = False, efi_state: efi.EfiState | None = None) -> list[Finding]:
    findings: list[Finding] = []
    boot = view.boot()
    esp = view.esp()
    session = view._session
    for failed in session.failed_mounts:
        findings.append(Finding("M01", "error", f"{failed['mountpoint']} could not be mounted", failed.get("reason", ""),
                                [f"device: {failed.get('device', 'not mounted')}"], ["fs.check"]))
    mounted_boot = view.is_mounted("/boot") or not any(f["mountpoint"] == "/boot" for f in session.failed_mounts)
    images = [kernel for kernel in boot.kernels if kernel.image]
    if not images and not boot.uki and mounted_boot and view.family != "generic":
        findings.append(Finding("D01", "error", "No kernel was found", "There is no kernel image in /boot, so nothing can be started.",
                                [f"/boot lists: {', '.join(view.listdir('/boot')[:8]) or 'nothing'}", f"installed module trees: {', '.join(k.version for k in boot.kernels) or 'none'}"], ["boot.kernel"]))
    for kernel in images:
        if not kernel.initramfs and boot.uses_initramfs and view.family in {"gentoo", "debian", "fedora", "arch", "suse"}:
            findings.append(Finding("D02", "error", f"Kernel {kernel.version} has no initramfs", "The system uses an initramfs, but none exists for this kernel; it cannot mount its root file system.",
                                    [f"kernel: {kernel.image}"], ["boot.initramfs"]))
        elif kernel.initramfs_truncated:
            findings.append(Finding("D03", "error", f"The initramfs of kernel {kernel.version} is incomplete", "The file is smaller than 1 MiB, which means a write was interrupted.",
                                    [f"{kernel.initramfs}: {kernel.initramfs_size} bytes"], ["boot.initramfs"]))
        if not kernel.modules and any(k.modules for k in boot.kernels) and kernel.version not in {"linux"}:
            findings.append(Finding("D04", "error", f"Kernel {kernel.version} has no module tree", f"/usr/lib/modules/{kernel.version} is missing, so the kernel cannot load drivers.",
                                    [f"kernel: {kernel.image}"], ["boot.kernel"]))
    findings.extend(_loader_findings(view, boot, esp))
    findings.extend(_fstab_findings(view))
    findings.extend(_esp_findings(view, boot, esp))
    findings.extend(_firmware_findings(view, boot, esp))
    findings.extend(_misc_findings(view, boot))
    if efi_state is not None and efi_state.usable and esp is not None:
        findings.extend(_nvram_findings(view, esp, efi_state))
    if hardware_changed:
        findings.append(Finding("D20", "info", "The hardware changed", "The initramfs of a system moved to another computer may lack the storage drivers of the new machine, and its firmware has no boot entry.",
                                [], ["boot.initramfs", "boot.bootloader", "efi.add"]))
    return findings


def _loader_findings(view: SessionView, boot: Any, esp: Any) -> list[Finding]:
    found: list[Finding] = []
    kernels_present = any(k.image for k in boot.kernels) or bool(boot.uki)
    loaders = set(boot.loaders)
    if "grub" in loaders:
        if not boot.grub_cfg:
            found.append(Finding("D05", "error", "The boot menu file is missing", "GRUB is installed but has no grub.cfg, so it stops at its command prompt.",
                                 ["/boot/grub/grub.cfg and /boot/grub2/grub.cfg: not found", f"kernels: {', '.join(k.version for k in boot.kernels if k.image) or 'none'}"], ["boot.config"]))
        elif boot.grub_cfg_size < 100:
            found.append(Finding("D05", "error", "The boot menu file is empty", f"{boot.grub_cfg} is only {boot.grub_cfg_size} bytes.", [boot.grub_cfg], ["boot.config"]))
        else:
            text = view.read(boot.grub_cfg, 1 << 20)
            if kernels_present and "menuentry" not in text and "blscfg" not in text and "configfile" not in text:
                found.append(Finding("D05", "error", "The boot menu lists no system", f"{boot.grub_cfg} contains no menu entry.", [boot.grub_cfg], ["boot.config"]))
            found.extend(_uuid_findings(view, boot.grub_cfg, text))
    if view.family == "fedora" and kernels_present and not boot.bls_entries and not boot.grub_cfg:
        found.append(Finding("D05", "error", "There are no boot entries", "/boot/loader/entries is empty although kernels are installed.", ["/boot/loader/entries: empty"], ["boot.config"]))
    if boot.bls_entries and kernels_present:
        for name in boot.bls_entries:
            found.extend(_uuid_findings(view, f"/boot/loader/entries/{name}", view.read(f"/boot/loader/entries/{name}", 8192)))
    if not loaders and not boot.uki and view.family != "generic":
        found.append(Finding("D05", "error", "No boot loader was found", "Neither GRUB, systemd-boot nor another loader has files on this system.", [], ["boot.bootloader"]))
    if len(loaders) > 1:
        found.append(Finding("D22", "info", "More than one boot loader is installed", f"Found: {', '.join(sorted(loaders))}. The firmware's boot order decides which one starts.", [], []))
    return found


def _uuid_findings(view: SessionView, path: str, text: str) -> list[Finding]:
    known = _graph_uuids(view)
    wanted = {m.lower() for m in ROOT_UUID_RE.findall(text)} | {m.lower() for m in SEARCH_UUID_RE.findall(text) if len(m) == 36}
    missing = sorted(uuid for uuid in wanted if uuid not in known)
    findings: list[Finding] = []
    if missing:
        findings.append(Finding("D06", "error", "The boot menu points at a disk that does not exist", f"{path} refers to {', '.join(missing)}, which is not any disk on this computer.",
                                [f"{path}: {uuid}" for uuid in missing] + [f"this system's root is {view.root_uuid}"], ["fstab.fix", "boot.config"]))
    if contains_live_arguments(text):
        findings.append(Finding("D21", "error", "A live-medium argument is stored in the boot configuration", f"{path} contains root=live: or rd.live arguments from a live session, so the system cannot boot from its disk.",
                                [path], ["boot.config"]))
    return findings


def _fstab_findings(view: SessionView) -> list[Finding]:
    findings: list[Finding] = []
    dead = [item for item in view.resolved_fstab() if item.status in {"unresolved", "ambiguous"}]
    critical = [item for item in dead if item.entry.file in {"/", "/boot", "/efi", "/boot/efi", "/usr"}]
    if dead:
        findings.append(Finding("D07", "error" if critical else "warn", "fstab refers to disks that are not there",
                                "Entries that cannot be resolved make the boot stop or wait for a timeout.", [f"{item.entry.file}: {item.entry.spec} ({item.reason})" for item in dead], ["fstab.fix"]))
    mismatched = [item for item in view.resolved_fstab() if item.status == "resolved" and item.type_mismatch]
    if mismatched:
        findings.append(Finding("D07", "warn", "fstab names the wrong file system type", "The type in fstab differs from the one on the device.", [f"{item.entry.file}: {item.type_mismatch}" for item in mismatched], ["fstab.fix"]))
    ambiguous = [item for item in dead if item.status == "ambiguous"]
    if ambiguous:
        findings.append(Finding("D17", "warn", "Two disks have the same ID", "Cloned disks share a UUID; the system cannot know which one fstab means.", [f"{item.entry.spec}: {', '.join(n.path for n in item.devices)}" for item in ambiguous], []))
    unstable = [item for item in view.resolved_fstab() if item.unstable_name]
    if unstable:
        findings.append(Finding("D07", "info", "fstab uses device names that can change", "Names like /dev/sda2 depend on the order the disks are found. UUIDs are stable.", [f"{item.entry.file}: {item.entry.spec}" for item in unstable], ["fstab.fix"]))
    return findings


def _esp_findings(view: SessionView, boot: Any, esp: Any) -> list[Finding]:
    findings: list[Finding] = []
    fstab_wants_esp = any(item.entry.file in {"/efi", "/boot/efi", "/boot/EFI"} for item in view.resolved_fstab() if item.status != "skipped")
    if esp is None:
        if fstab_wants_esp and not any(f["mountpoint"] in {"/efi", "/boot/efi"} for f in view._session.failed_mounts):
            findings.append(Finding("D08", "error", "The EFI system partition is not available", "fstab lists an EFI partition but it could not be mounted.", [], ["fstab.fix", "fs.check"]))
        return findings
    names = {name.lower(): files for name, files in esp.files.items()}
    loaders = {name for name, files in esp.files.items() if name.lower() not in {"boot", "microsoft"} and any(f.lower().endswith(".efi") for f in files)}
    if not esp.files:
        findings.append(Finding("D08", "error", "The EFI system partition is empty", "There is no EFI directory on it, so the firmware has nothing to start.", [f"{esp.device} mounted at {esp.mountpoint}"], ["boot.bootloader", "fs.check"]))
    elif not loaders and not boot.uki:
        findings.append(Finding("D08", "error", "The EFI system partition has no boot loader", "No loader file exists for this system.", [f"EFI directories: {', '.join(sorted(esp.files)) or 'none'}"], ["boot.bootloader"]))
    elif "boot" not in names or not any(f.lower() == "bootx64.efi" for f in names.get("boot", [])):
        findings.append(Finding("D08", "warn", "There is no fallback boot file", "Without EFI/BOOT/BOOTX64.EFI some firmwares cannot find the system when their boot entry is lost.", [], ["boot.esp_fallback", "boot.bootloader"]))
    return findings


def _firmware_findings(view: SessionView, boot: Any, esp: Any) -> list[Finding]:
    findings: list[Finding] = []
    env = view.environment
    target_uefi = bool(esp) or "x86_64-efi" in boot.grub_platforms or bool(boot.uki)
    target_bios = "i386-pc" in boot.grub_platforms and not target_uefi
    code, text = efi.firmware_message(env.uefi, target_uefi, target_bios)
    if code:
        findings.append(Finding("D10", "warn", "The firmware type does not match the installed system", text, [f"live session: {'UEFI' if env.uefi else 'BIOS'}"], ["boot.bootloader"] if code == "live_bios_target_uefi" else []))
    root = view.root_node
    if target_bios and root is not None:
        disks = view.graph.parent_disks(root)
        for disk in disks:
            if disk.pttype == "gpt" and not any(n.parttype == GPT_BIOS_BOOT for n in view.graph.descendants(disk)):
                findings.append(Finding("D09", "error", "The disk cannot start in legacy BIOS mode", f"{disk.path} has a GPT partition table but no BIOS boot partition, which GRUB needs for BIOS booting. "
                                        "Rescue never repartitions disks; create a 1 MiB BIOS boot partition with GParted, or boot this machine in UEFI mode.", [disk.path], []))
    return findings


def _nvram_findings(view: SessionView, esp: Any, state: efi.EfiState) -> list[Finding]:
    mine = efi.entries_for_esp(state, _esp_partuuid(view, esp))
    live = [entry for entry in mine if entry.loader and efi.esp_file_exists(entry.loader, view.listdir, esp.mountpoint)]
    if live:
        return []
    detail = "No firmware boot entry points at a loader that exists on this system's EFI partition."
    if mine:
        detail = "The firmware's boot entries for this system point at files that no longer exist."
    return [Finding("D11", "warn", "The firmware has no working boot entry for this system", detail, [f"entry {e.number} {e.label}: {e.loader}" for e in mine], ["efi.add", "boot.bootloader"])]


def _esp_partuuid(view: SessionView, esp: Any) -> str:
    try:
        return view.graph.find(esp.device).partuuid
    except LookupError:
        return ""


def _misc_findings(view: SessionView, boot: Any) -> list[Finding]:
    findings: list[Finding] = []
    if view.degraded_view:
        findings.append(Finding("D12", "warn", "The file system needed recovery options to mount", "Some data may be unreadable. Run a file system check before repairing.", [], ["fs.check"]))
    for path in ("/", "/boot"):
        if path == "/boot" and not view.is_mounted("/boot"):
            continue
        free = view.free_bytes(path)
        if free and free < MIN_FREE:
            findings.append(Finding("D13", "warn", f"{path} is almost full", f"Only {free // (1 << 20)} MiB are free; a kernel or initramfs cannot be written.", [], []))
    passwd = view.read("/etc/passwd", 65536)
    shadow = view.read("/etc/shadow", 65536)
    if not passwd.strip() or (view.exists("/etc/shadow") and not shadow.strip()):
        findings.append(Finding("D14", "error", "The account database is empty or missing", "/etc/passwd or /etc/shadow has no content. Restore it from a backup in the terminal.", [], []))
    pending = _pending_package_work(view)
    if pending:
        findings.append(Finding("D18", "warn", "A package operation was interrupted", "The package manager was stopped in the middle of an operation.", pending, []))
    for source in boot.cmdline_sources:
        text = view.read(source, 16384)
        if contains_live_arguments(text):
            findings.append(Finding("D21", "error", "A live-medium argument is stored in the kernel command line", f"{source} contains arguments from a live session.", [source], ["boot.config"]))
    root = view.root_node
    if root is not None and root.uuid:
        twins = [node.path for node in view.graph.by_uuid(root.uuid) if node.key != root.key and node.fstype != "btrfs"]
        if twins:
            findings.append(Finding("D17", "warn", "Another disk has the same ID as this system", "A cloned disk is connected; the system may start from the wrong one.", [f"{root.uuid}: {root.path}, {', '.join(twins)}"], []))
    return findings


def _pending_package_work(view: SessionView) -> list[str]:
    evidence: list[str] = []
    if view.family == "debian":
        half = view.dpkg_half_configured()
        if half:
            evidence.append("half-configured: " + ", ".join(half[:6]))
    elif view.family == "arch" and view.exists("/var/lib/pacman/db.lck"):
        evidence.append("/var/lib/pacman/db.lck exists")
    elif view.family == "gentoo" and view.portage_resume_pending():
        evidence.append("Portage has an unfinished merge list (emerge --resume)")
    return evidence


def recommended_actions(findings: list[Finding]) -> list[str]:
    """Action ids referenced by error findings, in repair order (fstab, kernel, initramfs, config, bootloader, NVRAM)."""
    wanted = {action for finding in findings if finding.severity == "error" for action in finding.actions}
    ordered = [action for action in ORDER if action in wanted]
    return ordered + sorted(wanted - set(ordered))
