"""Checks of the Security design (point 4) that ``gentoo-rescue-ai`` runs as root before it opens the session.

It cannot prove the image is safe, but it catches the ways it can be wrong: a build that put ``rescue-ai`` in a group that opens
the console or the disks, a kernel without Yama's ptrace limit, a console device an agent could open, a sudo rule, or an approver
socket with the wrong owner. Every check is a function of facts passed in, so the tests need no root.
"""

from __future__ import annotations

import glob
import grp
import os
import pwd
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import AI_USER, DO_SOCKET, STATE_DIR

FORBIDDEN_GROUPS = frozenset({"tty", "wheel", "disk", "video", "input", "audio", "plugdev", "usb", "kvm", "portage", "root", "sudo", "adm", "lp", "dialout", "floppy", "cdrom"})
DEFAULT_APPROVER_VT = 9
# The password is typed on the keyboard: whoever can read or inject key events, read the screen buffers or memory could sniff or fake it.
KEYBOARD_PATTERNS = ("/dev/input/*", "/dev/uinput", "/dev/vcs*", "/dev/vcsa*", "/dev/vcsu*", "/dev/mem", "/dev/kmem", "/dev/port", "/dev/kmsg")
SHADOW = "/etc/shadow"
YAMA = "/proc/sys/kernel/yama/ptrace_scope"
VT_FILE = f"{STATE_DIR}/approver-vt"


@dataclass(frozen=True)
class Facts:
    uid: int
    gids: frozenset[int]
    group_names: frozenset[str]
    ptrace_scope: int | None
    sudo_allows: bool | None
    socket_stat: os.stat_result | None
    devices: dict[str, os.stat_result | None]
    socket_gid: int | None
    root_locked: bool | None = None  # None: not known (not checked)


def can_open(info: os.stat_result | None, uid: int, gids: frozenset[int]) -> bool:
    """Whether a process with this uid and groups could open the device for reading or writing, from its owner, group and mode."""
    if info is None:
        return False
    mode = stat.S_IMODE(info.st_mode)
    if uid == 0:
        return True
    if info.st_uid == uid:
        return bool(mode & 0o600)
    if info.st_gid in gids:
        return bool(mode & 0o060)
    return bool(mode & 0o006)


def problems(facts: Facts, approver_vt: int = DEFAULT_APPROVER_VT) -> list[str]:
    found: list[str] = []
    bad = sorted(facts.group_names & FORBIDDEN_GROUPS)
    if bad:
        found.append(f"{AI_USER} is in the group(s) {', '.join(bad)}: that can open the console or disks. The agents must have none of these.")
    if facts.ptrace_scope is None:
        found.append("The kernel has no Yama (no kernel.yama.ptrace_scope): an agent could inspect the processes of its own user, including the tmux client that holds tty1.")
    elif facts.ptrace_scope < 1:
        found.append(f"kernel.yama.ptrace_scope is {facts.ptrace_scope}; it must be 1 or more.")
    if facts.sudo_allows:
        found.append(f"sudo lets {AI_USER} run something. {AI_USER} must have no sudo rule at all; root commands go through gentoo-rescue-do.")
    for name, info in facts.devices.items():
        if can_open(info, facts.uid, facts.gids):
            found.append(f"{name} can be opened by {AI_USER}: the approval screen would not be private.")
    if facts.root_locked is False:
        found.append(f"The root account has a password ({SHADOW}); it must be locked (!) so that nothing can become root with 'su'. Root commands go through gentoo-rescue-do.")
    if facts.socket_stat is None:
        found.append(f"The approver is not running ({DO_SOCKET} is missing), so the agents cannot ask for root commands.")
    else:
        mode = stat.S_IMODE(facts.socket_stat.st_mode)
        if facts.socket_stat.st_uid != 0 or mode & 0o007 or (facts.socket_gid is not None and facts.socket_stat.st_gid != facts.socket_gid):
            found.append(f"{DO_SOCKET} must be owned by root, group {AI_USER}, mode 0660 (it is {mode:04o}).")
    return found


def root_is_locked(shadow: str = SHADOW) -> bool | None:
    """Whether the password field of root is a lock (starts with ! or *); None when /etc/shadow cannot be read."""
    try:
        for line in Path(shadow).read_text(encoding="utf-8").splitlines():
            fields = line.split(":")
            if fields[0] == "root" and len(fields) > 1:
                return fields[1][:1] in {"!", "*"}
    except OSError:
        return None
    return None


def gather(user: str = AI_USER, approver_vt: int | None = None, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> Facts:
    info = pwd.getpwnam(user)
    names = {grp.getgrgid(info.pw_gid).gr_name}
    gids = {info.pw_gid}
    for group in grp.getgrall():
        if user in group.gr_mem:
            names.add(group.gr_name)
            gids.add(group.gr_gid)
    try:
        ptrace: int | None = int(Path(YAMA).read_text().strip())
    except (OSError, ValueError):
        ptrace = None
    try:
        sudo = run(["sudo", "-n", "-l", "-U", user], capture_output=True, text=True, timeout=10, check=False)
        text = (sudo.stdout + sudo.stderr).lower()
        allows: bool | None = False if ("not allowed to run sudo" in text or "may not run sudo" in text) else True if "may run the following" in text else None
    except (OSError, subprocess.SubprocessError):
        allows = None  # no sudo installed: nothing to grant
    if approver_vt is None:
        try:
            approver_vt = int(Path(VT_FILE).read_text().strip())
        except (OSError, ValueError):
            approver_vt = DEFAULT_APPROVER_VT
    devices: dict[str, os.stat_result | None] = {}
    names = ["/dev/tty1", f"/dev/tty{approver_vt}", "/dev/tty0", "/dev/console"]
    for pattern in KEYBOARD_PATTERNS:
        names.extend(sorted(glob.glob(pattern)) or [pattern])
    for device in names:
        try:
            info_dev = os.stat(device)
            devices[device] = None if stat.S_ISDIR(info_dev.st_mode) else info_dev  # /dev/input/by-path and the like are folders, not devices
        except OSError:
            devices[device] = None
    try:
        socket_stat: os.stat_result | None = os.stat(DO_SOCKET)
    except OSError:
        socket_stat = None
    return Facts(info.pw_uid, frozenset(gids), frozenset(names), ptrace, allows, socket_stat, devices, info.pw_gid, root_is_locked())


def check(user: str = AI_USER, approver_vt: int | None = None) -> list[str]:
    facts = gather(user, approver_vt)
    return problems(facts)
