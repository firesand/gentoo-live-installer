"""What a terminal in a repaired system shows and sets up: the "(chroot) " prompt and the banner of common fixes.

The Handbook says ``export PS1="(chroot) ${PS1}"`` after entering the chroot. Here the same prompt comes from a small rcfile
that is written into the session's own, fresh ``/run`` (a tmpfs the tool mounted over the system's ``/run``), so nothing of the
installed system is modified, and the shell is started as ``bash --rcfile /run/gentoo-rescue/bashrc -i`` (a login shell
ignores ``--rcfile``). The banner lists the commands that usually fix an unbootable system, fitted to what this system uses
(its kernel package, its boot loader and EFI partition, a user name that exists). Everything here only reads files.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from .errors import RescueError

RC_DIRECTORY = "/run/gentoo-rescue"
BASHRC_PATH = RC_DIRECTORY + "/bashrc"
# `. /etc/profile; ~/.bashrc; PS1="(chroot) ${PS1}"`, written for a shell that is interactive but not a login shell
BASHRC = (
    "# Made by the Gentoo Live repair tool in this session's own /run. Nothing of the installed system was changed for it.\n"
    "[ -r /etc/profile ] && . /etc/profile\n"
    "[ -f ~/.bashrc ] && . ~/.bashrc\n"
    'PS1="(chroot) ${PS1:-\\s-\\v\\$ }"\n'
)
LEAVE_UNMOUNTED = "Type exit to leave; the system is unmounted for you."
LEAVE_SESSION = "Type exit to leave. Unmount the system from the repair tool when you are done."
LEAVE_LINE = LEAVE_UNMOUNTED
USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")


def uses_bash(shell: str) -> bool:
    return posixpath.basename(shell) == "bash"


def virtual_run_ready(session: Any) -> bool:
    """The fresh /run of this session is mounted (the rcfile must never be written into the system's own /run)."""
    return any(entry.data.get("virtual_name") == "run" for entry in session.ledger.entries)


def write_bashrc(session: Any) -> bool:
    """Put the rcfile into the session's own /run; False (and a warning) when that is not possible, so the shell still starts."""
    if not virtual_run_ready(session):
        return False
    try:
        with session.fs(writable=True) as fs:
            fs.mkdir(RC_DIRECTORY, 0o755, parents=True)
            fs.write_atomic(BASHRC_PATH, BASHRC.encode(), mode=0o644, create=True)
    except (OSError, RescueError) as exc:
        session.warnings.append(f"The '(chroot)' prompt could not be prepared: {exc}")
        return False
    return True


# ---- the fixes of the banner -----------------------------------------------------------------------------------------------------------------
def _kernel_fix(view: Any, adapter: Any) -> list[tuple[str, str]]:
    packages = adapter.kernel_packages(view)
    if packages:
        atom = packages[adapter.newest_kernel_package(view, packages)]
        return [(f"emerge --ask --oneshot {atom}", "rebuild the kernel this system uses")]
    if view.which("genkernel"):
        return [("genkernel all", "rebuild the kernel and the initramfs")]
    if view.exists("/usr/src/linux"):
        return [("cd /usr/src/linux && make -j$(nproc) && make modules_install install", "rebuild the kernel from its sources (see the Handbook, chapter Configuring the Linux kernel)")]
    return []


def _grub_fix(view: Any, adapter: Any, boot: Any) -> list[tuple[str, str]]:
    config = boot.grub_cfg or "/boot/grub/grub.cfg"
    esp = view.esp()
    if esp is not None or "x86_64-efi" in boot.grub_platforms or boot.uki:
        directory = esp.mountpoint if esp is not None else "/efi"
        install = (f"grub-install --target=x86_64-efi --efi-directory={directory} --bootloader-id={adapter.default_bootloader_id}", f"reinstall GRUB to the EFI partition ({directory}, from its fstab)")
    else:
        disk = ""
        node = view.root_node
        if node is not None:
            try:
                disks = view.graph.parent_disks(node)
            except LookupError:
                disks = []
            disk = disks[0].path if disks else ""
        install = (f"grub-install {disk or '/dev/DISK'}", f"reinstall GRUB to the disk{'' if disk else ' (replace DISK with the whole disk, for example /dev/sda)'}")
    return [install, (f"grub-mkconfig -o {config}", "rebuild the boot menu")]


def fixes(view: Any) -> list[tuple[str, str]]:
    """(command, what it does) pairs that fit this system, in the order a person usually needs them."""
    from .adapters.gentoo import GentooAdapter

    adapter = GentooAdapter()
    result: list[tuple[str, str]] = [("emerge --sync", "update the package tree (needs the network)"),
                                     ("emerge --ask --update --deep --newuse @world", "update the system")]
    result += _kernel_fix(view, adapter)
    if adapter.initramfs_tool(view) == "dracut":
        result.append(("dracut --force --regenerate-all", "rebuild the initramfs of every kernel"))
    boot = view.boot()
    loader = adapter.active_loader(view)
    if loader == "grub":
        result += _grub_fix(view, adapter, boot)
    elif loader == "systemd-boot":
        esp = view.esp()
        result.append((f"bootctl --esp-path={esp.mountpoint if esp is not None else '/efi'} update", "update systemd-boot on the EFI partition"))
    if adapter.kernel_packages(view) and view.listdir("/var/db/pkg/x11-drivers") and any(name.startswith("nvidia-drivers") for name in view.listdir("/var/db/pkg/x11-drivers")):
        result.append(("emerge --ask --oneshot @module-rebuild", "rebuild the NVIDIA kernel modules after a kernel change"))
    users = [item["name"] for item in view.accounts() if item["name"] != "root" and USER_RE.match(item["name"])]
    result.append((f"passwd {users[0] if users else 'root'}", "set a new password" + (f" (root: passwd root)" if users else "")))
    editor = "nano" if view.which("nano") else "vi"
    result.append((f"{editor} /etc/fstab", "fix the list of disks"))
    return result


def banner(*, title: str, state: str, view: Any | None = None, dns: bool = True, leave: str = LEAVE_UNMOUNTED) -> str:
    """The text printed before the shell: where you are, whether changes are real, the fixes that fit, how to leave."""
    lines = ["", f"Gentoo Live rescue: a shell in {title}, {state}."]
    if dns:
        lines.append("The network settings of the live system are available here (DNS works).")
    if view is not None:
        try:
            listed = fixes(view)
        except (RescueError, OSError, LookupError, ValueError):
            listed = []
        if listed:
            lines.append("")
            lines.append("Common fixes:")
            width = max(len(command) for command, _ in listed)
            for command, note in listed:
                lines.append(f"  {command:<{width}}  # {note}")
    lines += ["", leave, ""]
    return "\n".join(lines) + "\n"
