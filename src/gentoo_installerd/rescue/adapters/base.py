"""Distribution adapters: how each family reinstalls a boot loader, rebuilds an initramfs and reinstalls a kernel.

An adapter only *describes* work as plan steps through a ``PlanBuilder``; running them is the executor's job. Programs
of the repaired system run inside a chroot (clean environment, gid 0, no supplementary groups); the live system supplies
only block devices, file systems and EFI tooling.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from .. import efi
from ..builder import PlanBuilder
from ..cmdline import contains_live_arguments, effective_text, sanitize
from ..errors import RescueError, Unsupported
from ..fstab import unified_diff
from ..view import SessionView

SUPPORTED_FAMILIES = ("gentoo", "debian", "fedora")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


class Adapter:
    family: ClassVar[str] = "generic"
    label: ClassVar[str] = "this system"
    tools: ClassVar[frozenset[str]] = frozenset()
    default_bootloader_id: ClassVar[str] = "linux"
    esp_default: ClassVar[str] = "/boot/efi"
    selects_kernels: ClassVar[bool] = False  # 'Reinstall the kernel' takes a list of kernel versions

    # ---- capabilities --------------------------------------------------------------------------------------------------
    def write_refusal(self, view: SessionView) -> str:
        """Why repairs are refused for this system, or '' when they are offered."""
        if view.immutable == "ostree":
            return "This is an image-based system (rpm-ostree). Roll back from its boot menu or run 'rpm-ostree rollback' on the running system."
        if view.immutable == "transactional":
            return "This is a transactional system. Boot the previous snapshot or run 'transactional-update rollback' on the running system."
        if view.immutable == "nix":
            return "NixOS keeps boot generations; use 'nixos-enter' and 'nixos-rebuild boot --install-bootloader' in the terminal."
        if view.arch not in {"x86_64", "i386", ""}:
            return f"This is a {view.arch} system; its programs cannot run on this computer, so only the terminal and read-only views are available."
        if view.family not in SUPPORTED_FAMILIES:
            return f"Automatic repairs are not available for {view.os.title} in this version. You can inspect the system, read its logs and open a terminal in it."
        return ""

    # ---- the actions an adapter implements ---------------------------------------------------------------------------------------
    def bootloader(self, b: PlanBuilder) -> None:
        raise Unsupported("Reinstalling the boot loader is not available for this system")

    def config(self, b: PlanBuilder) -> None:
        raise Unsupported("Regenerating the boot configuration is not available for this system")

    def initramfs(self, b: PlanBuilder) -> None:
        raise Unsupported("Rebuilding the initramfs is not available for this system")

    def kernel(self, b: PlanBuilder) -> None:
        raise Unsupported("Reinstalling the kernel is not available for this system")

    def finish(self, b: PlanBuilder) -> None:
        raise Unsupported("Finishing interrupted package operations is not available for this system")


# ---- shared building blocks ------------------------------------------------------------------------------------------------------------
def firmware_choice(b: PlanBuilder) -> str:
    """'uefi' or 'bios': what the installed system boots with (not the live session's mode)."""
    wanted = str(b.params.get("firmware", "auto"))
    if wanted in {"uefi", "bios"}:
        return wanted
    boot = b.view.boot()
    esp = b.view.esp()
    if esp is not None or "x86_64-efi" in boot.grub_platforms or boot.uki:
        return "uefi"
    if "i386-pc" in boot.grub_platforms:
        return "bios"
    return "uefi" if b.env.uefi else "bios"


def esp_for(b: PlanBuilder) -> Any:
    esp = b.view.esp()
    b.precondition(esp is not None, "The EFI system partition is mounted" if esp is not None else esp_missing_reason(b))
    return esp


def esp_missing_reason(b: PlanBuilder) -> str:
    """Why there is no EFI system partition to work on, in terms of what the user can do about it."""
    for failed in b.session.failed_mounts:
        if failed.get("kind") == "missing_directory" and failed["mountpoint"] in {"/efi", "/boot/efi", "/boot/EFI"}:
            return (f"The directory {failed['mountpoint']} is missing, so the EFI system partition was not mounted. Enable repairs first (the directory is created and the partition mounted "
                    "then) and make this plan again.")
    return "The EFI system partition is not mounted. Fix fstab (or choose it in the mount plan) and try again."


def bootloader_id(b: PlanBuilder, esp: Any, default: str, preferred: tuple[str, ...] = ()) -> str:
    """Reuse the directory of a GRUB that is already on the ESP; otherwise the distribution's own name."""
    explicit = str(b.params.get("id", ""))
    if explicit:
        if not ID_RE.match(explicit):
            raise RescueError("bad_request", "The boot loader name may only contain letters, digits, dot, dash and underscore", param="id")
        return explicit
    if esp is not None:
        for name, files in esp.files.items():
            lowered = {item.lower() for item in files}
            if name.lower() not in {"boot", "microsoft", "systemd", "linux"} and ({"grubx64.efi", "shimx64.efi"} & lowered) and (not preferred or name.lower() in preferred):
                return name
        for name in preferred:
            for existing in esp.files:
                if existing.lower() == name:
                    return existing
    return default


def removable_wanted(b: PlanBuilder, esp: Any) -> bool:
    """Fallback boot path: on when asked, when the firmware cannot store entries, or when no fallback loader exists yet."""
    mode = str(b.params.get("removable", "auto"))
    if mode in {"yes", "true", "1", True}:
        return True
    if mode in {"no", "false", "0", False}:
        return False
    if not b.env.uefi:
        return True
    return esp is not None and not any(f.lower() == "bootx64.efi" for f in esp.files.get("BOOT", esp.files.get("boot", [])))


def target_disks(b: PlanBuilder) -> list[str]:
    """Whole disks a BIOS boot loader goes to: the user's choice, else the disks of the root and /boot."""
    chosen = b.params.get("disk")
    graph = b.view.graph
    allowed = b.session.ctx.devices.disks
    if chosen:
        disks = [chosen] if isinstance(chosen, str) else list(chosen)
        for disk in disks:
            if disk not in allowed:
                raise RescueError("bad_request", f"{disk} is not one of the disks of this system", param="disk")
        return disks
    found: list[str] = []
    for path in b.session.plan.devices():
        try:
            for disk in graph.parent_disks(graph.find(path)):
                if disk.path in allowed and disk.path not in found:
                    found.append(disk.path)
        except LookupError:
            continue
    return found[:1] if len(found) > 1 and not b.params.get("all_disks") else found


def grub_target(b: PlanBuilder) -> str:
    if b.env.uefi and b.env.fw_platform_size == 32:
        return "i386-efi"
    return "x86_64-efi"


def nvram_ok(env: Any) -> bool:
    """Boot entries can be written: the live session runs in UEFI mode and the variables are not read-only."""
    return bool(env.uefi) and env.efivars != "ro"


def grub_uefi_steps(b: PlanBuilder, install: str, esp: Any, ident: str, *, secure_boot: bool = False, removable: bool = False) -> None:
    """GRUB for UEFI inside the system, with the live-side GRUB as the fallback when the system cannot run its programs."""
    target = grub_target(b)
    common = [f"--target={target}", f"--efi-directory={esp.mountpoint}"]
    flags = ["--recheck"] + (["--uefi-secure-boot"] if secure_boot else []) + ([] if nvram_ok(b.env) else ["--no-nvram"])
    if not nvram_ok(b.env):
        b.warn("This live session cannot store UEFI boot entries; the boot loader is written to the EFI partition and the firmware must be pointed at it (fallback path or its boot menu).")
    b.chroot([install, *common, f"--bootloader-id={ident}", *flags], writes=(f"{esp.mountpoint}/EFI/{ident}/*", "UEFI boot entry"), timeout=600, when="chroot_ok")
    if removable:
        b.chroot([install, *common, "--removable", "--recheck", *(["--uefi-secure-boot"] if secure_boot else [])], writes=(f"{esp.mountpoint}/EFI/BOOT/BOOTX64.EFI",), timeout=600, when="chroot_ok")
    live = ["grub-install", f"--boot-directory={b.root}/boot", f"--efi-directory={b.root}{esp.mountpoint}", f"--target={target}", f"--bootloader-id={ident}", "--recheck"]
    live += [] if nvram_ok(b.env) else ["--no-nvram"]
    b.host(live, display="grub-install (live system's GRUB) " + " ".join(live[1:]), writes=(f"{esp.mountpoint}/EFI/{ident}/*",), timeout=600, when="chroot_broken", atomic=False)
    if removable:
        b.host(["grub-install", f"--boot-directory={b.root}/boot", f"--efi-directory={b.root}{esp.mountpoint}", f"--target={target}", "--removable", "--recheck"],
               display="grub-install (live system's GRUB) --removable", writes=(f"{esp.mountpoint}/EFI/BOOT/BOOTX64.EFI",), timeout=600, when="chroot_broken", atomic=False)
    b.warn("If the system's own programs cannot run, the boot loader is installed with the live system's GRUB and the menu file (grub.cfg) is left as it is.")


def grub_bios_steps(b: PlanBuilder, install: str, disks: list[str]) -> None:
    b.precondition(bool(disks), "A disk for the boot loader was found" if disks else "No disk for the boot loader could be determined; choose one")
    for disk in disks:
        b.chroot([install, "--target=i386-pc", "--recheck", disk], writes=(f"the boot sector and embedding area of {disk}",), timeout=600, when="chroot_ok")
        b.host(["grub-install", f"--boot-directory={b.root}/boot", "--target=i386-pc", "--recheck", disk], display=f"grub-install (live system's GRUB) --target=i386-pc {disk}",
               writes=(f"the boot sector and embedding area of {disk}",), timeout=600, when="chroot_broken", atomic=False)
    for disk_path in disks:
        try:
            disk_node = b.view.graph.find(disk_path)
        except LookupError:
            continue
        has_bios_boot = any(node.parttype == "21686148-6449-6e6f-744e-656564726976" for node in b.view.graph.descendants(disk_node))
        if disk_node.pttype == "gpt" and not has_bios_boot:
            b.precondition(False, f"{disk_path} uses a GPT partition table but has no BIOS boot partition; GRUB cannot be embedded. Rescue never repartitions disks.")


GRUB_CMDLINE_RE = re.compile(r"^(\s*GRUB_CMDLINE_LINUX(?:_DEFAULT)?=)([\"']?)(.*?)\2\s*$")


def _without_live_arguments(b: PlanBuilder, path: str, text: str) -> str:
    """The same file with the arguments that belong to the live medium removed (anything else is kept as it is)."""
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if path == "/etc/default/grub":
            match = GRUB_CMDLINE_RE.match(line)
            if match and contains_live_arguments(match.group(3)):
                line = f"{match.group(1)}{match.group(2)}{sanitize(match.group(3))}{match.group(2)}"
        elif path.startswith("/boot/loader/entries/"):
            if stripped.startswith("options") and contains_live_arguments(stripped):
                options = sanitize(stripped.split(None, 1)[1] if len(stripped.split(None, 1)) == 2 else "")
                if not any(token.startswith("root=") for token in options.split()) and b.view.root_uuid:
                    options = f"root=UUID={b.view.root_uuid} {options}".strip()
                line = f"options {options}"
        elif stripped and not stripped.startswith("#") and contains_live_arguments(stripped):
            line = sanitize(line)
            if path == "/etc/kernel/cmdline" and "root=" not in line and b.view.root_uuid:
                line = f"root=UUID={b.view.root_uuid} {line}".strip()
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") or not text else "")


def clean_cmdline_sources(b: PlanBuilder) -> list[str]:
    """Remove the live system's arguments from the files boot entries are made from (finding D21).

    Regenerating the menu alone would write them out again: the source files are shown as a diff, backed up and checked
    against the digest they were read with before they are replaced.
    """
    view = b.view
    boot = view.boot()
    sources = [path for path in boot.cmdline_sources if path.startswith("/etc/")] + [f"/boot/loader/entries/{name}" for name in boot.bls_entries]
    changed: list[str] = []
    for path in sources:
        old = view.read(path, 65536)
        if not contains_live_arguments(effective_text(old)):
            continue
        new = _without_live_arguments(b, path, old)
        if new == old or contains_live_arguments(effective_text(new)):
            b.warn(f"{path} contains arguments of the live system that could not be removed automatically; edit it in the terminal.")
            continue
        b.write_file(path, new, display=f"write {path} without the live system's arguments (a backup copy is kept beside it)")
        b.diff += unified_diff(old, new, path.lstrip("/"))
        changed.append(path)
    if changed:
        b.warn("Arguments of the live system (root=live:..., rd.live...) were found in " + ", ".join(changed) + "; they are removed so the system can boot from its disk.")
    return changed


def grub_config_checks(b: PlanBuilder, path: str) -> None:
    b.check("file_min_size", path=path, bytes=1024)
    b.check("grep", path=path, pattern=r"menuentry|blscfg|configfile")
    b.check("no_live_args", path=path)


def esp_loader_checks(b: PlanBuilder, esp: Any, ident: str, removable: bool, files: tuple[str, ...] = ("grubx64.efi",)) -> None:
    for name in files:
        b.check("esp_file", dir=f"{esp.mountpoint}/EFI/{ident}", name=name)
    if removable:
        b.check("esp_file", dir=f"{esp.mountpoint}/EFI/BOOT", name="BOOTX64.EFI")


def backup_esp(b: PlanBuilder, esp: Any) -> None:
    """Archive the EFI directory into the repaired system before a boot loader is written (data safety for the ESP).

    The backup is best effort: if it cannot be made, the plan says so and goes on (the boot loader's own files are what is replaced).
    """
    from ..executor import MAX_BACKUP_FILE

    b.internal("backup-esp", f"Back up {esp.mountpoint}/EFI into /var/lib/gentoo-rescue/ before changing it (files over {MAX_BACKUP_FILE >> 20} MiB are left out)", {"mountpoint": esp.mountpoint},
               writes=("/var/lib/gentoo-rescue/",), on_fail="continue")


def need_network(b: PlanBuilder, reason: str) -> None:
    b.needs["network"] = True
    network = b.env.network
    if network is None:
        b.precondition(True, f"The network is needed to {reason} (it is checked when the plan runs)")
        return
    b.precondition(bool(network.get("ip")), "The computer is online" if network.get("ip") else "The computer has no network route; connect to the network first")
    if network.get("ip"):
        b.precondition(bool(network.get("dns")), "Name lookup works" if network.get("dns") else "Name lookup (DNS) does not work")
    if not b.env.clock_ok:
        b.warn("The computer's clock looks wrong; downloads over HTTPS may fail until the time is set.")
