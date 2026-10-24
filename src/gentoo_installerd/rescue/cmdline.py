"""The kernel command line programs inside a repaired system get to see (review R3).

kernel-install, dracut --uefi, ukify and mkinitcpio UKI presets fall back to ``/proc/cmdline`` when the target has no
command line configured. In a chroot of the live system that file holds ``root=live:CDLABEL=...``, which must never reach a
boot entry. Rescue therefore bind-mounts a synthesized file over the target's ``/proc/cmdline`` for every chroot step; this
module derives its content from the target's own configuration and refuses to emit live-medium arguments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .bootinfo import first_text_cmdline, grub_default_cmdline
from .errors import RescueError
from .paths import RootFS

# What only a live medium has. Finding a leak (D21, the post-check) and choosing the lines of a boot file to clean act on these alone.
LIVE_LEAK_RE = re.compile(r"^(?:root=live:|rd\.live\.|rd\.live$|cdroot$|CDLABEL=)")
# Everything on the live medium's own command line. It is dropped from the synthesized /proc/cmdline and from a line that is a copy of the live
# command line. nox, nogui, dokeymap, nodhcp, dospeakup, secureconsole, overlayfs and rd.debug are ordinary settings of an installed system
# too, so they are never a reason to change a boot file by themselves.
LIVE_TOKEN_RE = re.compile(r"^(?:root=live:|rd\.live\.|rd\.live$|BOOT_IMAGE=|initrd=|cdroot$|dokeymap$|nodhcp$|secureconsole$|nox$|nogui$|dospeakup$|CDLABEL=|overlayfs$|rd\.debug$)")
MAX_LENGTH = 2000


@dataclass
class DerivedCmdline:
    text: str
    source: str
    added: list[str] = field(default_factory=list)


def sanitize(cmdline: str) -> str:
    """Drop every argument that only makes sense on the live medium."""
    return " ".join(token for token in cmdline.split() if not LIVE_TOKEN_RE.match(token))


def effective_text(text: str) -> str:
    """The text without comment lines: a commented-out setting does not reach any boot entry."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def contains_live_arguments(cmdline: str) -> bool:
    return any(LIVE_LEAK_RE.match(token) for token in cmdline.split())


def _bls_options(fs: RootFS) -> str:
    for name in sorted(fs.listdir_or("/boot/loader/entries")):
        if not name.endswith(".conf"):
            continue
        for line in fs.read_text_or(f"/boot/loader/entries/{name}", "", 8192).splitlines():
            if line.startswith("options"):
                return line.split(None, 1)[1].strip() if len(line.split(None, 1)) == 2 else ""
    return ""


def _grub_cfg_line(fs: RootFS) -> str:
    for path in ("/boot/grub/grub.cfg", "/boot/grub2/grub.cfg"):
        text = fs.read_text_or(path, "", 512 * 1024)
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("linux ", "linux16 ", "linuxefi ")):
                parts = stripped.split()[2:]  # drop the verb and the kernel path
                return " ".join(parts)
    return ""


def derive(fs: RootFS, root_uuid: str, *, rootflags: str = "", subvol_path: str = "", luks_uuid: str = "", lvm_vg: str = "", md_uuid: str = "") -> DerivedCmdline:
    """The command line to bind over ``/proc/cmdline``; never contains live-medium arguments."""
    base, source = first_text_cmdline(fs)
    if not base:
        options = _bls_options(fs)
        if options:
            base, source = options, "/boot/loader/entries"
    if not base:
        line = _grub_cfg_line(fs)
        if line:
            base, source = line, "grub.cfg"
    text = sanitize(base)
    added: list[str] = []

    def ensure(prefix: str, token: str) -> None:
        nonlocal text
        if not any(item.startswith(prefix) for item in text.split()):
            text = (text + " " + token).strip()
            added.append(token)

    if root_uuid:
        ensure("root=", f"root=UUID={root_uuid}")
    if not {"ro", "rw"} & set(text.split()):
        text = (text + " ro").strip()
        added.append("ro")
    if subvol_path:
        ensure("rootflags=", f"rootflags=subvol={subvol_path.strip('/')}")
    if luks_uuid:
        ensure("rd.luks.uuid=", f"rd.luks.uuid={luks_uuid}")
    if lvm_vg:
        ensure("rd.lvm.vg=", f"rd.lvm.vg={lvm_vg}")
    if md_uuid:
        ensure("rd.md.uuid=", f"rd.md.uuid={md_uuid}")
    text = text[:MAX_LENGTH].strip()
    if contains_live_arguments(text):
        raise RescueError("internal", "A live-medium argument reached the synthesized kernel command line")
    return DerivedCmdline(text, source or "the root file system", added)


# ---- Gentoo's installkernel chroot guard (review R3c) ---------------------------------------------------------------------------
def _dracut_cmdline_configured(fs: RootFS) -> bool:
    if fs.exists("/etc/cmdline"):
        return True
    if any(name.endswith(".conf") for name in fs.listdir_or("/etc/cmdline.d")):
        return True
    files = ["/etc/dracut.conf"] + [f"/etc/dracut.conf.d/{n}" for n in fs.listdir_or("/etc/dracut.conf.d") if n.endswith(".conf")]
    files += [f"/usr/lib/dracut/dracut.conf.d/{n}" for n in fs.listdir_or("/usr/lib/dracut/dracut.conf.d") if n.endswith(".conf")]
    text = "\n".join(fs.read_text_or(path, "", 65536) for path in files)
    if re.search(r"^kernel_cmdline=", text, re.M) or re.search(r"^hostonly_cmdline=.*no", text, re.M):
        return True
    return bool(re.search(r"^hostonly=.*no", text, re.M)) and not re.search(r"^hostonly_cmdline=.*yes", text, re.M)


def _kernel_cmdline_configured(fs: RootFS) -> bool:
    return any(fs.is_file(path) for path in ("/etc/kernel/cmdline", "/run/kernel/cmdline", "/usr/local/lib/kernel/cmdline", "/usr/lib/kernel/cmdline"))


def install_conf(fs: RootFS) -> dict[str, str]:
    """installkernel's settings: the vendor file in /usr/lib/kernel overridden by /etc/kernel (both are read, as kernel-install does)."""
    settings: dict[str, str] = {}
    for path in ("/usr/lib/kernel/install.conf", "/etc/kernel/install.conf"):
        for line in fs.read_text_or(path, "", 4096).splitlines():
            key, eq, value = line.partition("=")
            if eq and not key.lstrip().startswith("#"):
                settings[key.strip()] = value.strip().strip("\"'")
    return settings


@dataclass
class GuardInfo:
    blocks: bool
    reason: str = ""
    override_path: str = ""


def installkernel_guard(fs: RootFS, installkernel_use: str) -> GuardInfo:
    """Would Gentoo's installkernel 05-check-chroot hook refuse to run inside our chroot, and which override file lifts it."""
    use = set(installkernel_use.split())
    systemd_variant = "systemd" in use
    override = "/etc/kernel/install.d/05-check-chroot.install" if systemd_variant else "/etc/kernel/preinst.d/05-check-chroot.install"
    if fs.exists(override):
        return GuardInfo(False, "the chroot check is already overridden", override)
    if "dracut" in use and not _dracut_cmdline_configured(fs):
        return GuardInfo(True, "dracut would fall back to /proc/cmdline and no command line is configured for it", override)
    layout_bls = bool({"systemd-boot", "uki", "ukify"} & use) or install_conf(fs).get("layout") in {"bls", "uki"}
    if layout_bls and not _kernel_cmdline_configured(fs):
        return GuardInfo(True, "the boot entry would take its command line from /proc/cmdline and /etc/kernel/cmdline does not exist", override)
    return GuardInfo(False, "", override)


def grub_cmdline_hint(fs: RootFS) -> str:
    return grub_default_cmdline(fs.read_text_or("/etc/default/grub", "", 16384))
