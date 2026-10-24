"""The action catalogue: which repairs exist, whether they are available here, and the plan each one produces.

Distribution-specific work (boot loader, initramfs, kernel, interrupted packages) is delegated to the adapter; everything
that is the same on every system (EFI boot entries, fstab, file system checks, accounts, the read-only/read-write switch)
lives here. ``build_plan`` only describes; ``executor.PlanExecutor`` runs a plan the user confirmed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import efi as efimod
from .adapters import base as efi_base
from .adapters import detect_adapter
from .builder import USERNAME_RE, PlanBuilder
from .devices import GPT_ESP, MBR_ESP
from .diagnose import Finding, diagnose, recommended_actions
from .errors import PreconditionFailed, RescueError, Unsupported
from .fsprobe import CHECK_TOOLS
from .fstab import Fstab, apply_choices, propose_fixes, resolve_all, unified_diff, uuid_spec
from .plan import Plan, Step
from .scan import probe_mounted
from .view import SessionView

if TYPE_CHECKING:  # pragma: no cover
    from .manager import RescueManager
    from .session import RescueSession

ACTIONS: dict[str, dict[str, Any]] = {
    "session.enter_write": {"title": "Enable repairs (switch to read-write)", "group": "session", "risk": "high", "writes": True,
                            "description": "Unmounts the system and mounts it again read-write so repairs can change files."},
    "session.leave_write": {"title": "Return to read-only", "group": "session", "risk": "low", "writes": False, "description": "Mounts the system read-only again."},
    "repair.recommended": {"title": "Fix everything recommended", "group": "recommended", "risk": "high", "writes": True,
                           "description": "Applies the repairs the diagnosis recommends, in the right order (disk list, kernel, initramfs, boot menu, boot loader, firmware entry) as one reviewed plan."},
    "boot.bootloader": {"title": "Reinstall the boot loader", "group": "boot", "risk": "high", "writes": True,
                        "description": "Writes the boot loader to the EFI partition or the disk and regenerates its menu.",
                        "params": [{"name": "loader", "type": "enum", "label": "Boot loader", "choices": ["grub", "systemd-boot"], "default": ""},
                                   {"name": "firmware", "type": "enum", "label": "Install for", "choices": ["auto", "uefi", "bios"], "default": "auto"},
                                   {"name": "removable", "type": "enum", "label": "Also create the fallback boot path (EFI/BOOT/BOOTX64.EFI)", "choices": ["auto", "yes", "no"], "default": "auto"},
                                   {"name": "disk", "type": "device", "label": "Disk for a legacy BIOS boot loader", "required": False}]},
    "boot.config": {"title": "Regenerate the boot menu", "group": "boot", "risk": "medium", "writes": True, "description": "Rebuilds the boot menu from the installed kernels."},
    "boot.initramfs": {"title": "Rebuild the initramfs", "group": "boot", "risk": "medium", "writes": True, "description": "Creates new initramfs images for the installed kernels.",
                       "params": [{"name": "kernels", "type": "list", "label": "Kernels (empty for all)"}, {"name": "generic", "type": "bool", "label": "Include drivers for many kinds of hardware (for a disk moved to another computer)", "default": False}]},
    "boot.kernel": {"title": "Reinstall the kernel", "group": "boot", "risk": "high", "writes": True, "description": "Reinstalls the kernel package, then rebuilds the initramfs and the boot menu.",
                    "params": [{"name": "package", "type": "text", "label": "Kernel package (Gentoo)", "required": False}, {"name": "mode", "type": "enum", "label": "Method (Gentoo)", "choices": ["config", "remerge"], "default": ""}]},
    "boot.esp_fallback": {"title": "Create the fallback boot file", "group": "boot", "risk": "medium", "writes": True, "description": "Copies the boot loader to EFI/BOOT/BOOTX64.EFI so the firmware can find the system."},
    "efi.add": {"title": "Add a firmware boot entry", "group": "efi", "risk": "medium", "writes": False, "needs_nvram": True, "description": "Registers an existing boot loader file with the firmware.",
                "params": [{"name": "loader", "type": "text", "label": "Loader file (for example \\EFI\\debian\\shimx64.efi)", "required": False}, {"name": "label", "type": "text", "label": "Name", "required": False},
                           {"name": "make_default", "type": "bool", "label": "Start it first", "default": True}]},
    "efi.delete": {"title": "Delete a firmware boot entry", "group": "efi", "risk": "medium", "writes": False, "needs_nvram": True, "description": "Removes one boot entry from the firmware.",
                   "params": [{"name": "bootnum", "type": "text", "label": "Entry number (for example 0003)", "required": True}]},
    "efi.order": {"title": "Set the firmware boot order", "group": "efi", "risk": "low", "writes": False, "needs_nvram": True, "description": "Changes which boot entry the firmware tries first.",
                  "params": [{"name": "order", "type": "list", "label": "Entry numbers in order", "required": True}]},
    "efi.next": {"title": "Boot an entry once", "group": "efi", "risk": "low", "writes": False, "needs_nvram": True, "description": "Starts the chosen entry on the next boot only.",
                 "params": [{"name": "bootnum", "type": "text", "label": "Entry number", "required": True}]},
    "fstab.fix": {"title": "Fix the disk list (fstab)", "group": "files", "risk": "high", "writes": True, "description": "Replaces entries of /etc/fstab that point at disks that are not there.",
                  "params": [{"name": "choices", "type": "object", "label": "Per line: replace, nofail, comment or keep", "required": False}]},
    "fs.check": {"title": "Check or repair a file system", "group": "files", "risk": "medium", "writes": False, "description": "Checks a file system read-only; repairs only when you ask, with the system unmounted.",
                 "params": [{"name": "device", "type": "device", "label": "Device (default: the root file system)", "required": False},
                            {"name": "mode", "type": "enum", "label": "Mode", "choices": ["check", "repair", "repair_force"], "default": "check"}]},
    "acct.password": {"title": "Set a user's password", "group": "accounts", "risk": "high", "writes": True, "description": "Sets a new password inside the system.",
                      "params": [{"name": "username", "type": "text", "label": "User", "required": True}, {"name": "unlock", "type": "bool", "label": "Unlock the account", "default": True},
                                 {"name": "expire", "type": "bool", "label": "Ask for a new password at the next login", "default": False}], "secrets": ["password"]},
    "acct.unlock": {"title": "Unlock an account", "group": "accounts", "risk": "medium", "writes": True, "description": "Removes a lock or failed-login block without changing the password.",
                    "params": [{"name": "username", "type": "text", "label": "User", "required": True}]},
    "pkg.finish": {"title": "Finish an interrupted package operation", "group": "packages", "risk": "medium", "writes": True, "description": "Completes what the package manager was doing when it was interrupted."},
}
QUERIES = ("actions.list", "diag.summary", "diag.deep", "boot.inventory", "efi.list", "fstab.show", "acct.users", "logs.sources", "logs.tail", "logs.journal", "logs.pstore", "logs.live_dmesg")
PROBE_PARAMS = {"fs.check", "efi.delete", "efi.order", "efi.next", "acct.password", "acct.unlock", "efi.add"}


@dataclass
class ActionSpec:
    id: str
    title: str
    group: str
    risk: str
    description: str
    available: bool = True
    reason: str = ""
    recommended: bool = False
    needs: dict[str, Any] = field(default_factory=dict)
    params: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "group": self.group, "risk": self.risk, "description": self.description, "available": self.available, "reason": self.reason,
                "recommended": self.recommended, "needs": self.needs, "params": self.params}


# ---- the plan factory ---------------------------------------------------------------------------------------------------------------------------------
def build_plan(manager: RescueManager, session: RescueSession, view: SessionView, action: str, params: dict[str, Any]) -> Plan:
    if action not in ACTIONS:
        raise RescueError("bad_request", f"Unknown action {action}", action=action)
    meta = ACTIONS[action]
    adapter = detect_adapter(view)
    if action not in {"session.enter_write", "session.leave_write"}:
        reason = adapter.write_refusal(view)
        if reason:
            raise Unsupported(reason)
    if not session.allow_write and action != "session.leave_write":
        raise RescueError("needs_write", "This session was opened for inspection only. Mount the system again with repairs allowed.")
    b = _new_builder(view, session, action, params)
    b.title, b.risk = meta["title"], meta["risk"]
    b.summary = meta["description"]
    if action == "session.enter_write":
        return _plan_enter_write(b)
    if action == "session.leave_write":
        return _plan_leave_write(b)
    if action == "repair.recommended":
        _plan_recommended(manager, session, view, adapter, b)
        return b.build()
    fill_builder(manager, session, view, adapter, b, action)
    return b.build()


def _new_builder(view: SessionView, session: RescueSession, action: str, params: dict[str, Any]) -> PlanBuilder:
    efi_state = _efi_state(session) if action.startswith("efi.") and view.environment.uefi else None
    return PlanBuilder(view, action, params, environment=view.environment, efi_state=efi_state)


def fill_builder(manager: RescueManager, session: RescueSession, view: SessionView, adapter: Any, b: PlanBuilder, action: str) -> None:
    """Describe one action in a builder (the steps, checks and warnings; the caller assembles the plan)."""
    if action.startswith("boot.") and action != "boot.esp_fallback":
        if not view.can_run_programs:
            raise Unsupported("Programs of this system cannot run here (inspection-only session).")
        getattr(adapter, {"boot.bootloader": "bootloader", "boot.config": "config", "boot.initramfs": "initramfs", "boot.kernel": "kernel"}[action])(b)
    elif action == "boot.esp_fallback":
        _plan_esp_fallback(b)
    elif action == "pkg.finish":
        if not view.can_run_programs:
            raise Unsupported("Programs of this system cannot run here (inspection-only session).")
        adapter.finish(b)
    elif action.startswith("efi."):
        _plan_efi(b, manager)
    elif action == "fstab.fix":
        _plan_fstab_fix(b, manager)
    elif action == "fs.check":
        _plan_fs_check(b)
    elif action in {"acct.password", "acct.unlock"}:
        if not view.can_run_programs:
            raise Unsupported("Programs of this system cannot run here (inspection-only session).")
        _plan_account(b, adapter)
    else:
        raise RescueError("bad_request", f"{action} cannot be planned here", action=action)


REPAIR_ORDER = ("fstab.fix", "boot.kernel", "boot.initramfs", "boot.config", "boot.bootloader", "boot.esp_fallback", "efi.add")
RISK_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "dangerous": 4}


def _plan_recommended(manager: RescueManager, session: RescueSession, view: SessionView, adapter: Any, b: PlanBuilder) -> None:
    """One plan for everything the diagnosis flags as an error: a novice reviews and confirms once (review R18)."""
    findings = diagnose(view, efi_state=_efi_state(session) if view.environment.uefi else None)
    wanted = [action for action in REPAIR_ORDER if action in set(recommended_actions(findings))]
    if "boot.kernel" in wanted:  # reinstalling the kernel rebuilds the initramfs and the menu itself
        wanted = [a for a in wanted if a not in {"boot.initramfs", "boot.config"}]
    if "boot.bootloader" in wanted:  # a bootloader reinstall regenerates the menu
        wanted = [a for a in wanted if a != "boot.config"]
    if not wanted:
        raise Unsupported("The diagnosis found nothing that needs repairing.")
    included: list[str] = []
    skipped: list[str] = []
    for action in wanted:
        sub = _new_builder(view, session, action, {})
        sub.title, sub.risk = ACTIONS[action]["title"], ACTIONS[action]["risk"]
        try:
            fill_builder(manager, session, view, adapter, sub, action)
        except Unsupported as exc:
            skipped.append(f"{ACTIONS[action]['title']}: {exc.message}")
            continue
        _merge(b, sub, action)
        included.append(action)
    if not included:
        raise Unsupported("None of the recommended repairs can be done here. " + " ".join(skipped))
    b.title, b.params = "Fix everything recommended", {"actions": included}
    b.summary = "Runs, in this order: " + "; ".join(ACTIONS[a]["title"].lower() for a in included) + "."
    b.risk = max((ACTIONS[a]["risk"] for a in included), key=lambda r: RISK_RANK[r])
    for note in skipped:
        b.warn("Skipped: " + note)


def _merge(master: PlanBuilder, sub: PlanBuilder, action: str) -> None:
    tag = action.replace(".", "-")
    for step in sub.steps:
        step.id = f"{tag}-{step.id}"
        master.steps.append(step)
    for step in sub.cleanup:
        step.id = f"{tag}-{step.id}"
        master.cleanup.append(step)
    master.uses_chroot = master.uses_chroot or sub.uses_chroot
    master.chroot_tools.update(sub.chroot_tools)
    master.post_checks.extend(sub.post_checks)
    master.preconditions.extend({"ok": item["ok"], "text": f"{ACTIONS[action]['title']}: {item['text']}"} for item in sub.preconditions)
    for warning in sub.warnings:
        master.warn(warning)
    for hint in sub.hints:
        if hint not in master.hints:
            master.hints.append(hint)
    master.needs["network"] = bool(master.needs.get("network")) or bool(sub.needs.get("network"))
    master.needs["uefi_nvram"] = bool(master.needs.get("uefi_nvram")) or bool(sub.needs.get("uefi_nvram"))
    if sub.diff:
        master.diff += sub.diff
    master.secrets = tuple(dict.fromkeys((*master.secrets, *sub.secrets)))


def _efi_state(session: RescueSession) -> efimod.EfiState:
    result = session.runner.execute(("efibootmgr", "-v"), timeout=20)
    return efimod.parse_efibootmgr(result.stdout, result.returncode)


# ---- session switches --------------------------------------------------------------------------------------------------------------------------------
def _plan_enter_write(b: PlanBuilder) -> Plan:
    session = b.session
    if session.mode == "rw":
        raise Unsupported("Repairs are already enabled for this session.")
    b.summary = "Unmounts the system and mounts it again read-write. Nothing else changes."
    b.needs["write"] = False  # the transition steps are the plan itself
    b.warn("A read-write mount replays the file system journal, exactly as a normal boot would.")
    b.steps = session.enter_write_steps(with_virtual=False)
    return b.build()


def _plan_leave_write(b: PlanBuilder) -> Plan:
    session = b.session
    if session.mode == "ro":
        raise Unsupported("The system is already read-only.")
    b.needs["write"] = False
    b.summary = "Unmounts the system and mounts it read-only again."
    b.steps = session.leave_write_steps()
    return b.build()


# ---- EFI boot entries ----------------------------------------------------------------------------------------------------------------------------------
def _plan_efi(b: PlanBuilder, manager: RescueManager) -> None:
    ok, why = efimod.efi_usable(b.env)
    b.needs.update({"write": False, "uefi_nvram": True})
    b.precondition(ok, "The firmware's boot entries can be changed from this session" if ok else why)
    state = b.efi_state
    if state is not None and not state.usable:
        b.precondition(False, state.error or "The firmware's boot entries cannot be read")
    action = b.action
    if action == "efi.add":
        _efi_add(b)
    elif action == "efi.delete":
        number = _bootnum(b, "bootnum", state)
        b.host(["efibootmgr", "--bootnum", number, "--delete-bootnum"], writes=(f"firmware boot entry {number}",), timeout=30)
        b.summary = f"Removes boot entry {number} from the firmware."
    elif action == "efi.order":
        order = b.params.get("order")
        if not isinstance(order, list) or not order:
            raise RescueError("bad_request", "order must be a list of entry numbers", param="order")
        numbers = [_bootnum(b, None, state, value=str(item)) for item in order]
        if len(set(numbers)) != len(numbers):
            raise RescueError("bad_request", "An entry appears twice in the boot order", param="order")
        left_out = [n for n in (state.order if state else []) if n not in numbers]
        if left_out:
            b.warn(f"Entries {', '.join(left_out)} are removed from the boot order (they are not deleted).")
        b.host(["efibootmgr", "--bootorder", ",".join(numbers)], writes=("firmware boot order",), timeout=30)
        b.risk = "low"
    elif action == "efi.next":
        number = _bootnum(b, "bootnum", state)
        b.host(["efibootmgr", "--bootnext", number], writes=("firmware BootNext",), timeout=30)
        b.risk = "low"


def _bootnum(b: PlanBuilder, name: str | None, state: efimod.EfiState | None, value: str | None = None) -> str:
    raw = value if value is not None else str(b.params.get(name or "", ""))
    number = raw.upper()
    if len(number) != 4 or any(ch not in "0123456789ABCDEF" for ch in number):
        raise RescueError("bad_request", "Boot entry numbers have four hexadecimal digits, for example 0003", param=name or "order")
    if state is not None and state.usable and state.entry(number) is None:
        raise RescueError("not_found", f"The firmware has no boot entry {number}", param=name or "order")
    return number


def _efi_add(b: PlanBuilder) -> None:
    view = b.view
    esp = view.esp()
    if esp is None:
        raise Unsupported(efi_base.esp_missing_reason(b))
    node = view.graph.find(esp.device)
    disk = esp.disk
    b.precondition(bool(disk) and disk in b.session.ctx.devices.disks, "The disk of the EFI partition is one of this system's disks" if disk else "The disk of the EFI partition is unknown")
    loader = str(b.params.get("loader", ""))
    if not loader:
        loader = _guess_loader(view, esp)
        if not loader:
            raise Unsupported("No boot loader file was found on the EFI system partition. Reinstall the boot loader first.")
    loader = efimod.loader_for_entry(loader)
    exists = efimod.esp_file_exists(loader, view.listdir, esp.mountpoint)
    b.precondition(exists, f"{loader} exists on the EFI partition" if exists else f"{loader} was not found on the EFI partition")
    label = efimod.sanitize_label(str(b.params.get("label", "")) or f"{view.os.title}"[:32])
    make_default = b.params.get("make_default", True) not in {False, "false", "no", "0"}
    b.title, b.summary = "Add a firmware boot entry", f"Registers {loader} as the boot entry '{label}'."
    b.host(["efibootmgr", "--create" if make_default else "--create-only", "--disk", disk, "--part", str(node.partn or 1), "--label", label, "--loader", loader],
           writes=("firmware boot entry",), timeout=30)
    b.check("efi_entry", loader=loader.lstrip("\\"))


def _guess_loader(view: SessionView, esp: Any) -> str:
    """The most likely loader on the ESP: a shim, else GRUB, else systemd-boot, from the directory of this distribution first."""
    order = ("shimx64.efi", "grubx64.efi", "systemd-bootx64.efi", "refind_x64.efi")
    directories = sorted(esp.files, key=lambda name: (name.lower() not in {view.os.flavor, view.os.id, "gentoo", "debian", "ubuntu", "fedora", "redhat", "systemd"}, name.lower()))
    for name in directories:
        if name.lower() in {"boot", "microsoft", "linux"}:
            continue
        lowered = {item.lower(): item for item in esp.files[name]}
        for candidate in order:
            if candidate in lowered:
                return f"\\EFI\\{name}\\{lowered[candidate]}"
    return ""


def _plan_esp_fallback(b: PlanBuilder) -> None:
    view = b.view
    esp = view.esp()
    if esp is None:
        raise Unsupported(efi_base.esp_missing_reason(b))
    b.title, b.risk = "Create the fallback boot file", "medium"
    names = {name.lower(): name for name in esp.files}
    source = ""
    for directory in sorted(esp.files, key=lambda n: n.lower()):
        lowered = {item.lower(): item for item in esp.files[directory]}
        if directory.lower() in {"boot", "microsoft", "linux"}:
            continue
        if "shimx64.efi" in lowered:
            raise Unsupported("This system boots through a signed shim chain. Use 'Reinstall the boot loader' so shim, MokManager and GRUB are laid out together.")
        if "grubx64.efi" in lowered and not source:
            source = f"{esp.mountpoint}/EFI/{directory}/{lowered['grubx64.efi']}"
        if "systemd-bootx64.efi" in lowered and not source:
            source = f"{esp.mountpoint}/EFI/{directory}/{lowered['systemd-bootx64.efi']}"
    if not source:
        raise Unsupported("No GRUB or systemd-boot file was found on the EFI partition to copy.")
    boot_dir = f"{esp.mountpoint}/EFI/{names.get('boot', 'BOOT')}"
    b.summary = "Copies the boot loader to EFI/BOOT/BOOTX64.EFI, the file firmwares try when no boot entry works."
    b.warn("If Secure Boot is enabled, an unsigned copy will be refused by the firmware.")
    b.file({"op": "mkdir", "path": boot_dir, "mode": 0o755, "parents": True}, f"mkdir {boot_dir}", writes=(boot_dir,))
    b.file({"op": "copy", "src": source, "path": f"{boot_dir}/BOOTX64.EFI", "backup": True}, f"copy {source} to {boot_dir}/BOOTX64.EFI", writes=(f"{boot_dir}/BOOTX64.EFI",))
    b.check("esp_file", dir=boot_dir, name="BOOTX64.EFI")


# ---- fstab ---------------------------------------------------------------------------------------------------------------------------------------------------
def probe_esp_candidates(manager: RescueManager, session: RescueSession, view: SessionView) -> dict[str, bool]:
    """Whether each partition with the EFI system type holds an EFI directory (read-only probe mount)."""
    result: dict[str, bool] = {}
    for node in view.graph:
        if node.parttype not in {GPT_ESP, MBR_ESP} or node.fstype != "vfat" or node.live_medium:
            continue
        if view.esp() is not None and view.esp().device == node.path:  # type: ignore[union-attr]
            result[node.key] = bool(view.esp().files)  # type: ignore[union-attr]
            continue
        try:
            runner = manager.pool_runner()
            with probe_mounted(runner, manager.env.host, manager.env.probe_root, node, sleep=manager.env.sleep) as (target, _rung):
                from .paths import RootFS

                with RootFS(target) as fs:
                    result[node.key] = fs.is_dir("/EFI")
        except RescueError:
            result[node.key] = False
    return result


def _plan_fstab_fix(b: PlanBuilder, manager: RescueManager) -> None:
    view = b.view
    text = view.read("/etc/fstab", 1 << 20)
    if not text.strip():
        raise Unsupported("This system has no /etc/fstab to fix.")
    fstab = Fstab.parse(text)
    resolved = view.resolved_fstab()
    root = view.root_node
    probes = probe_esp_candidates(manager, b.session, view)
    proposals = propose_fixes(fstab, resolved, view.graph, root_node=root, esp_has_efi_dir=probes)
    if not proposals:
        raise Unsupported("Every entry of /etc/fstab points at a disk that is present; there is nothing to fix.")
    chosen: dict[int, dict[str, str]] = {}
    for proposal in proposals:
        chosen[proposal.lineno] = ({"action": "replace", "spec": proposal.selected} if proposal.selected else {"action": proposal.default_action})
    given = b.params.get("choices") or {}
    if not isinstance(given, dict):
        raise RescueError("bad_request", "choices must be an object keyed by line number", param="choices")
    for key, value in given.items():
        if not str(key).isdigit() or not isinstance(value, dict):
            raise RescueError("bad_request", "choices must map a line number to {action, spec}", param="choices")
        chosen[int(key)] = {str(k): str(v) for k, v in value.items()}
    try:
        updated = apply_choices(fstab, chosen, view.graph)
    except ValueError as exc:
        raise RescueError("bad_request", str(exc), param="choices") from exc
    new_text = updated.render()
    b.title, b.risk = "Fix the disk list (fstab)", "high"
    b.summary = "Replaces or relaxes the entries of /etc/fstab that point at disks that are not there. A copy of the old file is kept."
    b.diff = unified_diff(text, new_text)
    b.write_file("/etc/fstab", new_text, mode=None, backup=True, display="write /etc/fstab (a backup copy is kept beside it)")
    b.params = {**b.params, "choices": {str(k): v for k, v in chosen.items()}}
    b.needs["write"] = True
    b.check("fstab_resolves", ignore=[int(k) for k, v in chosen.items() if v.get("action") in {"nofail", "comment", "keep"}])
    for proposal in proposals:
        if not proposal.selected and not given.get(str(proposal.lineno)) and proposal.default_action != "keep":
            b.warn(f"{proposal.mountpoint}: no safe replacement was chosen automatically; the entry is marked 'nofail' so booting does not wait for it.")
    verify = _verify_fstab(b, new_text)
    if verify:
        b.warn("findmnt --verify on the result: " + verify)


def _verify_fstab(b: PlanBuilder, text: str) -> str:
    path = b.session.state_dir_path("fstab.proposed")
    try:
        path.write_text(text)
        path.chmod(0o600)
        result = b.runner.execute(("findmnt", "--verify", "--tab-file", str(path)), timeout=30)
    except OSError:
        return ""
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    lines = [line.strip() for line in result.stdout.splitlines() if "[E]" in line or "[W]" in line or "Success" in line or "error" in line.lower()]
    return "; ".join(lines[:4])


# ---- file system checks -----------------------------------------------------------------------------------------------------------------------------------
CHECK_COMMANDS = {
    "ext": {"check": (["e2fsck", "-n", "-f"], (0, 4)), "repair": (["e2fsck", "-f", "-p"], (0, 1, 2)), "repair_force": (["e2fsck", "-f", "-y"], (0, 1, 2))},
    "xfs": {"check": (["xfs_repair", "-n"], (0, 1)), "repair": (["xfs_repair"], (0,)), "repair_force": (["xfs_repair", "-L"], (0,))},
    "vfat": {"check": (["fsck.vfat", "-n"], (0, 1)), "repair": (["fsck.vfat", "-a"], (0, 1))},
    "f2fs": {"check": (["fsck.f2fs", "--dry-run"], (0,)), "repair": (["fsck.f2fs", "-f"], (0, 1))},
    "exfat": {"check": (["fsck.exfat", "-n"], (0, 1)), "repair": (["fsck.exfat", "-p"], (0, 1))},
    "jfs": {"check": (["jfs_fsck", "-n"], (0, 1)), "repair": (["jfs_fsck", "-a"], (0, 1))},
    "btrfs": {"check": (["btrfs", "check", "--readonly"], (0,)), "repair_force": (["btrfs", "rescue", "zero-log"], (0,))},
    "ntfs": {"check": (["ntfsfix", "-n"], (0,))},
}


def _external_log_flags(fstype: str, session: RescueSession, device: str) -> list[str]:
    """xfs_repair needs ``-l``/``-r`` for an external log or realtime device that the mount options named."""
    if fstype != "xfs":
        return []
    options = session.plan.root.options if device == session.plan.root.device else next((item.options for item in session.plan.items if item.device == device), [])
    flags: list[str] = []
    for option in options:
        key, _, value = option.partition("=")
        if key == "logdev" and value.startswith("/dev/"):
            flags += ["-l", value]
        elif key == "rtdev" and value.startswith("/dev/"):
            flags += ["-r", value]
    return flags


def _check_family(fstype: str) -> str:
    return "ext" if fstype in {"ext2", "ext3", "ext4"} else fstype


def _plan_fs_check(b: PlanBuilder) -> None:
    session, view = b.session, b.view
    device = str(b.params.get("device") or view.root_device)
    mode = b.choice("mode", {"check", "repair", "repair_force"}, "check")
    allowed = set(session.plan.devices())
    if device not in allowed:
        raise RescueError("bad_request", f"{device} is not part of this repair session", param="device")
    try:
        node = view.graph.find(device)
    except LookupError as exc:
        raise RescueError("not_found", f"{device} was not found", param="device") from exc
    table = CHECK_COMMANDS.get(_check_family(node.fstype), {})
    if not table or mode not in table:
        raise Unsupported(f"A {mode.replace('_', ' ')} of a {node.fstype or 'unknown'} file system is not offered here." + (" Use the terminal." if table else ""))
    argv, ok_rc = table[mode]
    b.title = {"check": "Check the file system (read-only)", "repair": "Repair the file system", "repair_force": "Repair the file system (may lose data)"}[mode]
    b.risk = {"check": "medium", "repair": "high", "repair_force": "dangerous"}[mode]
    b.summary = f"Runs {argv[0]} on {device} with the system unmounted." + ("" if mode == "check" else " Make a backup first if you can.")
    b.needs.update({"write": False, "unmounted_device": device})
    if mode == "repair_force":
        b.warn("This repair can discard data that cannot be made consistent. Copy important files off first if you can (ddrescue, btrfs restore).")
    steps = [Step("fs-idle", "internal", "Check that no terminal is open and no other step is running", handler="check-idle", group="fs-check")]
    for index, entry in enumerate(reversed(session._mount_entries())):
        steps.append(session._umount_step(entry, f"fs-umount-{index}", "fs-check"))
    members = session.plan.members_of(device)  # a multi-device btrfs or an external log is checked and made writable as a whole (review R7c)
    if mode != "check":
        for index, member in enumerate(members):
            steps.append(Step(f"fs-setrw-{index}", "host", f"blockdev --setrw {member}", ("blockdev", "--setrw", member), timeout=20, group="fs-check",
                              effect={"kind": "blockro", "device": member, "ro_now": False}))
        for array in session.readonly_md:
            steps.append(Step("fs-mdrw", "host", f"mdadm --readwrite {array}", ("mdadm", "--readwrite", array), timeout=30, group="fs-check", ok_rc=(0, 1)))
    command = tuple([*argv, *_external_log_flags(node.fstype, session, device), device])
    steps.append(Step("fs-run", "host", " ".join(command), command, writes=tuple(members) if mode != "check" else (), timeout=14400, ok_rc=ok_rc, atomic=False, group="fs-check"))
    cleanup: list[Step] = []
    if mode != "check":
        for index, member in enumerate(members):
            cleanup.append(Step(f"fs-setro-{index}", "host", f"blockdev --setro {member}", ("blockdev", "--setro", member), timeout=20, group="fs-check",
                                effect={"kind": "blockro", "device": member, "ro_now": True}, ok_rc=(0,)))
    cleanup.extend(session.mount_steps(rw=False, group="fs-check"))
    b.steps = steps
    b.cleanup = cleanup
    b.hints.append("The system was mounted read-only again after the check." if mode == "check" else "The system was mounted read-only again. Enable repairs to continue.")
    if mode == "check":
        b.hints.append("Exit code 4 of e2fsck means errors were found, not that the check failed.")


# ---- accounts -------------------------------------------------------------------------------------------------------------------------------------------------
def _plan_account(b: PlanBuilder, adapter: Any) -> None:
    view = b.view
    username = str(b.params.get("username", ""))
    if not USERNAME_RE.match(username) or username.startswith("-"):
        raise RescueError("bad_request", "That is not a valid user name", param="username")
    users = {item["name"]: item for item in view.accounts()}
    if username not in users:
        raise RescueError("not_found", f"There is no login user {username!r} in this system", param="username")
    b.needs["write"] = True
    if b.action == "acct.password":
        b.title, b.risk = f"Set the password of {username}", "high"
        b.summary = f"Sets a new password for {username} inside the system. The password is sent once, on standard input, and is never stored or logged."
        b.secrets = ("password",)
        if view.which("chpasswd"):
            b.chroot(["chpasswd"], tools={"chpasswd"}, stdin="secret:password", stdin_template=username + ":{secret}\n", display=f"chroot {b.root} chpasswd   (password read from standard input, not shown)",
                     writes=("/etc/shadow",), timeout=60, atomic=True)
        elif view.which("passwd"):
            b.chroot(["passwd", "--stdin", username], tools={"passwd"}, stdin="secret:password", stdin_template="{secret}\n", display=f"chroot {b.root} passwd --stdin {username}   (password read from standard input, not shown)",
                     writes=("/etc/shadow",), timeout=60, atomic=True)
        else:
            raise Unsupported("Neither chpasswd nor passwd was found in this system.")
        if b.params.get("unlock", True) not in {False, "false", "no", "0"} and view.which("usermod"):
            b.chroot(["usermod", "-U", username], writes=("/etc/shadow",), timeout=60, on_fail="continue")
        if b.params.get("expire") in {True, "true", "yes", "1"} and view.which("chage"):
            b.chroot(["chage", "-d", "0", username], writes=("/etc/shadow",), timeout=60)
    else:
        b.title, b.risk = f"Unlock the account {username}", "medium"
        b.summary = "Removes a lock or a failed-login block. The password is not changed."
        if view.which("usermod"):
            b.chroot(["usermod", "-U", username], writes=("/etc/shadow",), timeout=60)
        if view.which("faillock"):
            b.chroot(["faillock", "--user", username, "--reset"], writes=("/var/run/faillock/*",), timeout=60, on_fail="continue")
        if not b.steps:
            raise Unsupported("Neither usermod nor faillock was found in this system.")
    b.check("grep", path="/etc/shadow", pattern=rf"^{username}:")


# ---- the catalogue for the interface ----------------------------------------------------------------------------------------------------------------------------
def available_actions(manager: RescueManager, session: RescueSession, view: SessionView, findings: list[Finding]) -> list[ActionSpec]:
    recommended = set(recommended_actions(findings))
    adapter = detect_adapter(view)
    refusal = adapter.write_refusal(view)
    specs: list[ActionSpec] = []
    for action, meta in ACTIONS.items():
        spec = ActionSpec(action, meta["title"], meta["group"], meta["risk"], meta["description"], params=list(meta.get("params", [])),
                          needs={"write": bool(meta.get("writes")), "uefi_nvram": bool(meta.get("needs_nvram")), "secrets": list(meta.get("secrets", []))},
                          recommended=action in recommended or (action == "repair.recommended" and bool(recommended)))
        if action == "session.enter_write":
            spec.available, spec.reason = (session.allow_write and session.mode == "ro"), ("" if session.allow_write else "This session is inspection-only") if session.mode == "ro" else "Repairs are already enabled"
        elif action == "session.leave_write":
            spec.available, spec.reason = session.mode == "rw", "" if session.mode == "rw" else "The system is already read-only"
        elif not session.allow_write:
            spec.available, spec.reason = False, "This session was opened for inspection only"
        elif refusal:
            spec.available, spec.reason = False, refusal
        elif action in PROBE_PARAMS:
            spec.available, spec.reason = _static_availability(action, view, manager)
        else:
            try:
                build_plan(manager, session, view, action, {})
            except Unsupported as exc:
                spec.available, spec.reason = False, exc.message
            except (RescueError, OSError, LookupError):
                pass  # needs parameters or preconditions the plan will show
        specs.append(spec)
    return specs


def _static_availability(action: str, view: SessionView, manager: RescueManager) -> tuple[bool, str]:
    if action.startswith("efi."):
        ok, why = efimod.efi_usable(view.environment)
        return ok, why
    if action.startswith("acct.") and not view.can_run_programs:
        return False, "Programs of this system cannot run here"
    return True, ""


def describe_session(manager: RescueManager, session: RescueSession, graph: Any) -> dict[str, Any]:
    view = manager.view_for(session)
    findings = diagnose(view)
    specs = available_actions(manager, session, view, findings)
    return {
        "family": view.family, "os": view.os.to_dict(), "init": view.init, "arch": view.arch, "immutable": view.immutable, "findings": [item.to_dict() for item in findings],
        "recommended": recommended_actions(findings), "actions": [spec.to_dict() for spec in specs], "environment": view.environment.to_dict(), "boot": view.boot().to_dict(),
        "esp": {"mountpoint": view.esp().mountpoint, "device": view.esp().device} if view.esp() else None,
    }


# ---- read-only queries (no confirmation; allowed in a read-only session) ------------------------------------------------------------------------------------------------
def run_query(manager: RescueManager, session: RescueSession, name: str, params: dict[str, Any]) -> dict[str, Any]:
    from . import logs

    view = manager.view_for(session)
    if name == "diag.summary":
        findings = diagnose(view, hardware_changed=bool(params.get("hardware_changed")), efi_state=_efi_state(session) if view.environment.uefi else None)
        return {"findings": [item.to_dict() for item in findings], "recommended": recommended_actions(findings)}
    if name == "actions.list":
        findings = diagnose(view)
        return {"actions": [spec.to_dict() for spec in available_actions(manager, session, view, findings)], "recommended": recommended_actions(findings)}
    if name == "diag.deep":
        return {"findings": [item.to_dict() for item in deep_findings(manager, session, view)]}
    if name == "boot.inventory":
        boot = view.boot()
        esp = view.esp()
        return {**boot.to_dict(), "esp": {"mountpoint": esp.mountpoint, "device": esp.device, "dirs": esp.files} if esp else None, "cmdline_source": view.kernel_cmdline_source()[1]}
    if name == "efi.list":
        ok, why = efimod.efi_usable(view.environment)
        if not view.environment.uefi:
            return {"usable": False, "error": why, "entries": []}
        return _efi_state(session).to_dict()
    if name == "fstab.show":
        text = view.read("/etc/fstab", 1 << 20)
        return {"text": text, "entries": [item.to_dict() for item in view.resolved_fstab()], "crypttab": view.read("/etc/crypttab", 65536)}
    if name == "acct.users":
        return {"users": view.accounts()}
    if name == "logs.sources":
        return logs.list_sources(view)
    if name == "logs.tail":
        return logs.tail(view, str(params.get("path", "")), int(params.get("lines", 200)), int(params.get("offset", 0)))
    if name == "logs.pstore":
        return logs.pstore(manager.env.host)
    if name == "logs.live_dmesg":
        return logs.live_dmesg(session.runner)
    if name == "logs.journal":
        return logs.journal(view, session.runner, confirm_exec=bool(params.get("confirm_exec")), boot=int(params.get("boot", 0)), priority=str(params.get("priority", "")),
                            kernel=bool(params.get("kernel")), lines=int(params.get("lines", 200)))
    raise RescueError("bad_request", f"Unknown query {name}", query=name)


def deep_findings(manager: RescueManager, session: RescueSession, view: SessionView) -> list[Finding]:
    """Look inside the initramfs images with lsinitrd (a host tool that only reads the file; nothing of the system runs)."""
    findings: list[Finding] = []
    root = view.root_node
    stack = view.graph.stack_of(root) if root is not None else []
    needs: dict[str, str] = {}
    if any(node.type == "crypt" for node in stack):
        needs["crypt"] = "the root file system is encrypted"
    if any(node.type == "lvm" for node in stack):
        needs["lvm"] = "the root file system is on LVM"
    if any(node.type.startswith("raid") for node in stack):
        needs["mdraid"] = "the root file system is on RAID"
    if root is not None and root.fstype == "btrfs":
        needs["btrfs"] = "the root file system is btrfs"
    if not needs:
        return findings
    for kernel in view.kernels():
        if not kernel.initramfs:
            continue
        with session.fs() as fs:
            host_path = fs.host_path(kernel.initramfs)
        result = session.runner.execute(("lsinitrd", "-m", host_path), timeout=120)
        modules = result.stdout.lower()
        if result.returncode != 0 or "dracut modules" not in modules:
            continue
        for module, reason in needs.items():
            if module not in modules.split("dracut modules:", 1)[-1].split():
                findings.append(Finding("D15", "error", f"The initramfs of kernel {kernel.version} cannot open the root file system",
                                        f"{reason}, but the initramfs has no '{module}' module.", [kernel.initramfs], ["boot.initramfs"]))
    return findings
