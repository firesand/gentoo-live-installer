"""NVIDIA in a repaired Gentoo: what is installed, what no longer fits the kernel, and the three repairs (hardware item R01).

Facts are read from files only (Portage's database in /var/db/pkg, the kernel module trees, /etc/portage/make.conf, /etc/modprobe.d,
the files the kernel command line is made from). The repairs only describe steps; nothing runs until a person confirmed the plan.

* ``gfx.nvidia_rebuild``      rebuild the kernel modules for the installed kernels (``emerge --oneshot @module-rebuild``), then the initramfs.
* ``gfx.nvidia_fbdev_off``    remove ``nvidia-drm.fbdev=1`` from the files the boot entries are made from (it can blank the text console).
* ``gfx.nouveau_switch``      go back to the open-source driver without a network: unmerge the NVIDIA driver, drop ``nvidia`` from VIDEO_CARDS,
                              unblock nouveau, remove the NVIDIA kernel arguments, rebuild the initramfs and the boot menu.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .adapters import base
from .builder import PlanBuilder
from .errors import RescueError, Unsupported
from .fstab import unified_diff
from .view import SessionView

PACKAGE = "x11-drivers/nvidia-drivers"
VDB_DIR = "/var/db/pkg/x11-drivers"
DRIVER_RE = re.compile(r"^nvidia-drivers-(\d[\w.]*?)(?:_[a-z]+\d*)*(?:-r\d+)?$")
MODULE_SUFFIXES = ("", ".xz", ".zst", ".gz")
MODULE_VERSION = (re.compile(rb"\x00version=(\d+(?:\.\d+)+)\x00"), re.compile(rb"NVRM version: NVIDIA UNIX[^\n\x00]*?\s(\d+(?:\.\d+)+)\s"))
DISTFILE_DIRS = ("/var/cache/distfiles",)
NVIDIA_ARGUMENTS = frozenset({"nvidia-drm.fbdev=1", "nvidia-drm.modeset=1"})
FBDEV_ARGUMENTS = frozenset({"nvidia-drm.fbdev=1"})
NOUVEAU_BLOCKS = frozenset({"modprobe.blacklist=nouveau", "rd.driver.blacklist=nouveau", "module_blacklist=nouveau", "nouveau.modeset=0"})
NOUVEAU_LINE_RE = re.compile(r"^\s*(?:blacklist\s+nouveau\b|install\s+nouveau\s+\S+|options\s+nouveau\s+modeset=0\b)")
VIDEO_CARDS_RE = re.compile(r"^(\s*(?:export\s+)?VIDEO_CARDS\s*=\s*)([\"']?)(.*?)\2(\s*(?:#.*)?)$")
GRUB_CMDLINE_RE = base.GRUB_CMDLINE_RE
CMDLINE_FILES = ("/etc/default/grub", "/etc/kernel/cmdline", "/etc/cmdline")


def normalized(token: str) -> str:
    """nvidia_drm.fbdev=1 and nvidia-drm.fbdev=1 mean the same to the kernel."""
    return token.replace("nvidia_drm", "nvidia-drm", 1)


# ---- facts -----------------------------------------------------------------------------------------------------------------------------------------
def driver_version(view: SessionView) -> str:
    """The installed x11-drivers/nvidia-drivers version ('580.126.18'), '' when the package is not installed."""
    versions = [match.group(1) for match in (DRIVER_RE.match(entry) for entry in view.listdir(VDB_DIR)) if match]
    return max(versions, key=lambda item: tuple(int(part) for part in re.findall(r"\d+", item))) if versions else ""


@dataclass
class ModuleState:
    kernel: str
    path: str = ""  # the module file, '' when the kernel has none
    version: str = ""  # '' when it could not be read (a compressed module)

    def to_line(self) -> str:
        if not self.path:
            return f"kernel {self.kernel}: no NVIDIA kernel module"
        return f"kernel {self.kernel}: module {self.version or 'version unknown'} ({self.path})"


def module_states(view: SessionView) -> list[ModuleState]:
    """One entry per kernel that can be started: where its nvidia.ko is and which driver version it is."""
    states: list[ModuleState] = []
    for kernel in view.kernels():
        if not kernel.image or not kernel.modules:
            continue
        directory = f"/usr/lib/modules/{kernel.version}/video"
        names = set(view.listdir(directory))
        state = ModuleState(kernel.version)
        for suffix in MODULE_SUFFIXES:
            if f"nvidia.ko{suffix}" in names:
                state.path = f"{directory}/nvidia.ko{suffix}"
                if not suffix:
                    found = view.search_binary(state.path, MODULE_VERSION)
                    state.version = found.decode("ascii", "replace") if found else ""
                break
        states.append(state)
    return states


def nouveau_blocks(view: SessionView) -> list[tuple[str, list[str]]]:
    """(file, lines) that keep nouveau from loading: blacklists in /etc/modprobe.d."""
    found: list[tuple[str, list[str]]] = []
    for name in sorted(view.listdir("/etc/modprobe.d")):
        if not name.endswith(".conf"):
            continue
        path = f"/etc/modprobe.d/{name}"
        lines = [line for line in view.read(path, 65536).splitlines() if NOUVEAU_LINE_RE.match(line)]
        if lines:
            found.append((path, lines))
    return found


def cmdline_files(view: SessionView) -> list[str]:
    """The files in /etc and the boot entries the kernel command line is made from (the files a repair may rewrite)."""
    boot = view.boot()
    files = [path for path in boot.cmdline_sources if path in CMDLINE_FILES or (path.startswith("/etc/cmdline.d/") and path.endswith(".conf"))]
    return files + [f"/boot/loader/entries/{name}" for name in boot.bls_entries]


def _tokens_of(path: str, text: str) -> list[str]:
    tokens: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if path == "/etc/default/grub":
            match = GRUB_CMDLINE_RE.match(line)
            if match:
                tokens += match.group(3).split()
        elif path.startswith("/boot/loader/entries/"):
            if stripped.startswith("options"):
                tokens += stripped.split()[1:]
        elif stripped and not stripped.startswith("#"):
            tokens += stripped.split()
    return tokens


def cmdline_uses(view: SessionView, arguments: frozenset[str]) -> list[str]:
    """The command line files that carry one of the arguments."""
    return [path for path in cmdline_files(view) if any(normalized(token) in arguments for token in _tokens_of(path, view.read(path, 65536)))]


def without_arguments(path: str, text: str, arguments: frozenset[str]) -> str:
    """The same file with the arguments removed (every other character is kept as it was)."""

    def clean(value: str) -> str:
        return " ".join(token for token in value.split() if normalized(token) not in arguments)

    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if path == "/etc/default/grub":
            match = GRUB_CMDLINE_RE.match(line)
            if match and any(normalized(token) in arguments for token in match.group(3).split()):
                line = f"{match.group(1)}{match.group(2)}{clean(match.group(3))}{match.group(2)}"
        elif path.startswith("/boot/loader/entries/"):
            if stripped.startswith("options") and any(normalized(token) in arguments for token in stripped.split()[1:]):
                line = f"options {clean(stripped.split(None, 1)[1])}"
        elif stripped and not stripped.startswith("#") and any(normalized(token) in arguments for token in stripped.split()):
            line = clean(stripped)
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") or not text else "")


def video_cards_files(view: SessionView) -> list[tuple[str, str]]:
    """(path, text) of the make.conf files that set VIDEO_CARDS."""
    paths = ["/etc/portage/make.conf"]
    if view.listdir("/etc/portage/make.conf"):  # make.conf can be a directory of files
        paths = [f"/etc/portage/make.conf/{name}" for name in sorted(view.listdir("/etc/portage/make.conf"))]
    result = []
    for path in paths:
        text = view.read(path, 262144)
        if any(VIDEO_CARDS_RE.match(line) for line in text.splitlines()):
            result.append((path, text))
    return result


def with_video_cards(text: str, *, remove: tuple[str, ...] = (), add: tuple[str, ...] = ()) -> str:
    out = []
    for line in text.splitlines():
        match = VIDEO_CARDS_RE.match(line)
        if match:
            cards = [card for card in match.group(3).split() if card not in remove]
            cards += [card for card in add if card not in cards]
            line = f"{match.group(1)}{match.group(2) or chr(34)}{' '.join(cards)}{match.group(2) or chr(34)}{match.group(4)}"
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") or not text else "")


def kernels_without_nouveau(view: SessionView) -> list[str]:
    """Bootable kernels whose module list is readable and names no nouveau driver."""
    missing = []
    for kernel in view.kernels():
        if not kernel.image or not kernel.modules:
            continue
        deps = view.read(f"/usr/lib/modules/{kernel.version}/modules.dep", 8 << 20)
        if deps.strip() and "nouveau.ko" not in deps:
            missing.append(kernel.version)
    return missing


# ---- the plans -------------------------------------------------------------------------------------------------------------------------------------
def _need_emerge(b: PlanBuilder) -> None:
    if not b.view.can_run_programs:
        raise Unsupported("Programs of this system cannot run here (inspection-only session).")
    if not b.view.which("emerge"):
        raise Unsupported("Portage (emerge) is not installed in this system.")


def _initramfs_and_menu(b: PlanBuilder, adapter: Any, *, menu: bool) -> None:
    view = b.view
    if adapter.initramfs_tool(view) == "dracut":
        b.chroot(["dracut", "--force", "--regenerate-all"], writes=("/boot/initramfs-<version>.img",), timeout=1800)
    if menu and adapter.active_loader(view) == "grub":
        path = view.boot().grub_cfg or "/boot/grub/grub.cfg"
        b.chroot(["grub-mkconfig", "-o", path], writes=(path,), timeout=600)
        base.grub_config_checks(b, path)


def plan_rebuild(b: PlanBuilder, adapter: Any) -> None:
    view = b.view
    _need_emerge(b)
    version = driver_version(view)
    if not version:
        raise Unsupported("The NVIDIA driver (x11-drivers/nvidia-drivers) is not installed in this system.")
    mode = b.choice("mode", {"module-rebuild", "remerge"}, "module-rebuild")
    b.title, b.risk = "Rebuild the NVIDIA kernel modules", "medium"
    b.summary = "Builds the NVIDIA kernel modules again for the kernels in /boot, then rebuilds the initramfs."
    states = module_states(view)
    b.precondition(bool(states), "A kernel with a module tree was found" if states else "No kernel with a module tree (/usr/lib/modules/<version>) was found")
    b.precondition(view.exists("/usr/src/linux/Makefile") or view.exists("/usr/src/linux"), "The kernel sources (/usr/src/linux) are present" if view.exists("/usr/src/linux")
                   else "The kernel sources (/usr/src/linux) are missing; install and select them first (eselect kernel)")
    distfiles = [name for directory in DISTFILE_DIRS for name in view.listdir(directory) if name.startswith(f"NVIDIA-Linux-x86_64-{version}")]
    if not distfiles:
        base.need_network(b, f"download the NVIDIA driver {version} (it is not in /var/cache/distfiles)")
    wanted = ["emerge", "--ask=n", "--oneshot", "@module-rebuild" if mode == "module-rebuild" else PACKAGE]
    b.chroot(wanted, writes=("/usr/lib/modules/*/video/nvidia*.ko", "/var/db/pkg/x11-drivers/"), timeout=3600)
    _initramfs_and_menu(b, adapter, menu=False)
    b.hints.append("Reboot and remove the live medium. If emerge says there is nothing to rebuild, run this again with method 'remerge'.")


def plan_fbdev_off(b: PlanBuilder, adapter: Any) -> None:
    view = b.view
    b.title, b.risk = "Remove nvidia-drm.fbdev=1 from the kernel command line", "medium"
    b.summary = "Removes nvidia-drm.fbdev=1 from the files the boot entries are made from, then rebuilds the boot menu. The driver then uses its own default."
    changed = _strip_arguments(b, FBDEV_ARGUMENTS)
    if not changed:
        raise Unsupported("No kernel command line of this system sets nvidia-drm.fbdev=1.")
    if not view.can_run_programs:
        raise Unsupported("Programs of this system cannot run here (inspection-only session).")
    _initramfs_and_menu(b, adapter, menu=True)
    b.warn("Gentoo's driver notes say nvidia-drm.fbdev=1 can break the text console on some setups; the driver chooses its default without it.")
    b.hints.append("Reboot and remove the live medium.")


def plan_nouveau(b: PlanBuilder, adapter: Any) -> None:
    view = b.view
    _need_emerge(b)
    version = driver_version(view)
    blocks = nouveau_blocks(view)
    arguments = cmdline_uses(view, NVIDIA_ARGUMENTS | NOUVEAU_BLOCKS)
    cards = [(path, text) for path, text in video_cards_files(view) if "nvidia" in _cards(text)]
    if not (version or blocks or arguments or cards):
        raise Unsupported("Nothing of the NVIDIA driver is set up in this system; there is nothing to switch away from.")
    lacking = kernels_without_nouveau(view)
    if lacking:
        raise Unsupported(f"The kernel {', '.join(lacking)} has no nouveau driver, so the computer would have no graphics driver after the switch. Install a distribution kernel first (Reinstall the kernel).")
    b.title, b.risk = "Switch to the open-source driver (nouveau)", "high"
    b.summary = ("Removes the NVIDIA driver and its settings so the open-source nouveau driver is used. It needs no network. "
                 "Your files, your accounts and the rest of the system stay as they are.")
    for path, text in cards:  # VIDEO_CARDS: nvidia out, nouveau in (Mesa builds its driver for it at the next update)
        new = with_video_cards(text, remove=("nvidia",), add=("nouveau",))
        if new != text:
            b.write_file(path, new, display=f"write {path} with VIDEO_CARDS without nvidia (a backup copy is kept beside it)")
            b.diff += unified_diff(text, new, path.lstrip("/"))
    for path, lines in blocks:  # keep the user's file, only switch the lines off
        text = view.read(path, 65536)
        new = "\n".join(f"# switched off by the Gentoo Live repair tool: {line}" if NOUVEAU_LINE_RE.match(line) else line for line in text.splitlines()) + ("\n" if text.endswith("\n") else "")
        b.write_file(path, new, display=f"write {path} without the block on nouveau (a backup copy is kept beside it)")
        b.diff += unified_diff(text, new, path.lstrip("/"))
    _strip_arguments(b, NVIDIA_ARGUMENTS | NOUVEAU_BLOCKS)
    if version:
        b.chroot(["emerge", "--ask=n", "--unmerge", PACKAGE], writes=("the NVIDIA driver and its modules",), timeout=1800)
    _initramfs_and_menu(b, adapter, menu=True)
    if cards:
        b.warn("VIDEO_CARDS changed from nvidia to nouveau: Mesa gets its nouveau support at the next update. When the network works, run: emerge --ask --update --deep --newuse @world")
    else:
        b.warn("VIDEO_CARDS was not found in /etc/portage/make.conf, so it was not changed. Set VIDEO_CARDS=\"nouveau\" there before the next system update.")
    b.hints.append("Reboot and remove the live medium. To go back to the NVIDIA driver later, install it again with emerge x11-drivers/nvidia-drivers.")


def _cards(text: str) -> list[str]:
    for line in text.splitlines():
        match = VIDEO_CARDS_RE.match(line)
        if match:
            return match.group(3).split()
    return []


def _strip_arguments(b: PlanBuilder, arguments: frozenset[str]) -> list[str]:
    changed: list[str] = []
    for path in cmdline_files(b.view):
        old = b.view.read(path, 65536)
        new = without_arguments(path, old, arguments)
        if new == old:
            continue
        b.write_file(path, new, display=f"write {path} without {', '.join(sorted(arguments))} (a backup copy is kept beside it)")
        b.diff += unified_diff(old, new, path.lstrip("/"))
        changed.append(path)
    return changed


PLANNERS = {"gfx.nvidia_rebuild": plan_rebuild, "gfx.nvidia_fbdev_off": plan_fbdev_off, "gfx.nouveau_switch": plan_nouveau}


def plan(b: PlanBuilder, adapter: Any, action: str) -> None:
    if getattr(adapter, "family", "") != "gentoo":
        raise Unsupported("The NVIDIA repairs are for Gentoo systems.")
    if action not in PLANNERS:
        raise RescueError("bad_request", f"{action} cannot be planned here", action=action)
    PLANNERS[action](b, adapter)


__all__ = ["PLANNERS", "driver_version", "module_states", "plan"]
