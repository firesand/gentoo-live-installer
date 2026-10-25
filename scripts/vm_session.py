"""The graphical session of an installed desktop and the Safe graphics boot entry (hardware backlog H04, items 2 and 3).

A VM cannot log in to a desktop the way a person does, so the checks are the ones that fail when a desktop is broken:

* ``desktop-session``: the display manager is running, its greeter or X server is up, and a session file of the chosen desktop exists
  in ``/usr/share/xsessions`` or ``/usr/share/wayland-sessions`` (what the greeter offers).
* ``audio-pactl``: ``pactl info`` answers with PipeWire's PulseAudio server in a login session (the SSH login of the test user is one:
  PAM and elogind or systemd-logind give it ``/run/user/UID``). On systemd the user units start PipeWire by socket activation; on
  OpenRC the desktop's XDG autostart does, which an SSH login does not run, so the OpenRC probe starts ``gentoo-pipewire-launcher``
  itself. That proves the packages and the wiring, not the autostart of a real desktop login.
* the Safe graphics entry (``nomodeset`` and the graphics modules blacklisted) in the installed system's boot menu: find it, boot it
  once, and check the kernel command line and the loaded modules.

Everything here is a command and a parser of its output; nothing starts a process.
"""
from __future__ import annotations

import re
import shlex
from typing import Mapping

from vm_probes import ANSI_RE, ProbeResult

# The packages and session names of every Guided desktop. The backend's registry (backlog B14) will own this table; until it exists
# the harness keeps its own copy, checked against the tree by tests/hardware/test_tree_evidence.py.
DESKTOPS: dict[str, dict[str, object]] = {
    "kde": {"package": "kde-plasma/plasma-meta", "session": r"plasma"},
    "gnome": {"package": "gnome-base/gnome", "session": r"gnome"},
    "xfce": {"package": "xfce-base/xfce4-meta", "session": r"xfce"},
    "cinnamon": {"package": "gnome-extra/cinnamon", "session": r"cinnamon"},
    "mate": {"package": "mate-base/mate", "session": r"mate"},
    "lxqt": {"package": "lxqt-base/lxqt-meta", "session": r"lxqt"},
    "gnome-light": {"package": "gnome-base/gnome-light", "session": r"gnome"},
    "sway": {"package": "gui-wm/sway", "session": r"sway", "display_manager": False},
    "i3": {"package": "x11-wm/i3", "session": r"i3", "display_manager": False},
}
GUIDED_DESKTOPS = tuple(DESKTOPS)
GREETER_PROCESSES = frozenset({"sddm", "sddm-greeter", "sddm-greeter-qt6", "gdm", "gdm-session-wor", "gdm-x-session", "gdm-wayland-ses", "lightdm",
                               "lightdm-gtk-gre", "Xorg", "X", "Xwayland", "kwin_wayland", "gnome-shell", "greetd"})


def desktop_session_command(init: str) -> str:
    """Session files, the display manager's state, the process names: three sections separated by ``---``."""
    manager = "systemctl is-active display-manager" if init == "systemd" else "rc-service display-manager status"
    return f"ls /usr/share/xsessions /usr/share/wayland-sessions 2>/dev/null; echo ---; {manager}; echo ---; ps -eo comm="


def parse_desktop_session_probe(returncode: int, output: str, desktop: str, init: str) -> ProbeResult:
    clean = ANSI_RE.sub("", output)
    parts = clean.split("\n---\n")
    if len(parts) != 3 or desktop not in DESKTOPS:
        return ProbeResult("desktop-session", False, clean.strip()[-400:], f"the check returned no usable report for {desktop!r}")
    sessions, manager, processes = (part.strip() for part in parts)
    info = DESKTOPS[desktop]
    problems: list[str] = []
    names = [line.strip() for line in sessions.splitlines() if line.strip().endswith(".desktop")]
    if not any(re.search(str(info["session"]), name, re.I) for name in names):
        problems.append(f"no session file for {desktop} in /usr/share/xsessions or /usr/share/wayland-sessions (found: {', '.join(names) or 'none'})")
    needs_manager = info.get("display_manager", True)
    active = manager.splitlines()[:1] == ["active"] if init == "systemd" else bool(re.search(r"status:\s*started", manager))
    if needs_manager and not active:
        problems.append(f"the display manager is not running ({manager.splitlines()[0] if manager else 'no answer'})")
    running = {line.strip() for line in processes.splitlines()}
    if needs_manager and active and not running & GREETER_PROCESSES:
        problems.append("the display manager runs, but neither its greeter nor an X or Wayland server does (a black screen at login)")
    return ProbeResult("desktop-session", not problems, f"sessions: {', '.join(names)}; display manager: {manager.splitlines()[0] if manager else '-'}", "; ".join(problems))


def pactl_command(init: str) -> str:
    runtime = "XDG_RUNTIME_DIR=/run/user/$(id -u)"
    if init == "systemd":
        return f"{runtime} pactl info"
    return f"{runtime} sh -c '(gentoo-pipewire-launcher restart >/dev/null 2>&1 &); sleep 5; pactl info'"


def parse_pactl_probe(returncode: int, output: str) -> ProbeResult:
    """The sound server must be PipeWire's PulseAudio replacement; a machine without a sound device still answers (with a dummy sink)."""
    clean = ANSI_RE.sub("", output).strip()
    server = re.search(r"(?m)^Server Name:\s*(.+)$", clean)
    if returncode != 0 or server is None:
        return ProbeResult("audio-pactl", False, clean[-400:], f"pactl info gave no answer (exit {returncode}): no sound server runs in the login session")
    if "pipewire" not in server.group(1).lower():
        return ProbeResult("audio-pactl", False, clean[-400:], f"the sound server is {server.group(1)!r}, not PipeWire")
    return ProbeResult("audio-pactl", True, server.group(1).strip())


# --- the Safe graphics boot entry -----------------------------------------------------------------------------------------

SAFE_TITLE_RE = re.compile(r"safe graphics", re.I)
SAFE_ARGUMENTS = ("nomodeset",)
SAFE_BLACKLISTED = ("nvidia", "nvidia_drm", "nvidia_modeset", "nvidia_uvm", "nouveau")
LOADER_LIST_COMMANDS = {
    "grub": "cat /boot/grub/grub.cfg",
    "systemd-boot": "sudo -S -p '' sh -c 'for f in /efi/loader/entries/*.conf /boot/loader/entries/*.conf; do [ -f \"$f\" ] && echo \"== $f\" && cat \"$f\"; done'",
    "efi-stub": "sudo -S -p '' efibootmgr",
}


def safe_entry_listing_command(loader: str) -> str:
    return LOADER_LIST_COMMANDS[loader]


def find_safe_entry(loader: str, listing: str) -> dict[str, str]:
    """The Safe graphics entry in what the loader's configuration lists: ``{"id": ..., "label": ...}``, or {} when there is none.

    GRUB: the title in grub.cfg (inside the submenu, if the installer put it there). systemd-boot: a ``title`` line in
    loader/entries/*.conf (the id is the file name). EFI stub: a boot option of efibootmgr (the id is its number).
    """
    text = ANSI_RE.sub("", listing)
    if loader == "grub":
        import vm_iso  # noqa: PLC0415 - scripts/ is on sys.path where the harness runs

        for entry in vm_iso.parse_boot_menu(text):
            if entry.bootable and SAFE_TITLE_RE.search(entry.title):
                return {"id": entry.label, "label": entry.label, "arguments": " ".join(entry.arguments)}
        return {}
    if loader == "systemd-boot":
        for block in re.split(r"(?m)^== ", text):
            title = re.search(r"(?m)^title\s+(.+)$", block)
            if title and SAFE_TITLE_RE.search(title.group(1)):
                path = block.splitlines()[0].strip() if block.strip() else ""
                options = re.search(r"(?m)^options\s+(.+)$", block)
                return {"id": path.rsplit("/", 1)[-1], "label": title.group(1).strip(), "arguments": options.group(1).strip() if options else ""}
        return {}
    match = re.search(r"(?m)^Boot([0-9A-Fa-f]{4})\*?\s+(.*safe graphics.*)$", text, re.I)
    return {"id": match.group(1), "label": match.group(2).strip().split("\t")[0], "arguments": ""} if match else {}


def oneshot_command(loader: str, entry: Mapping[str, str]) -> str:
    """Choose the entry for the next boot only (the harness reboots right after)."""
    if loader == "grub":
        return "sudo -S -p '' grub-reboot " + shlex.quote(entry["id"].replace(" > ", ">"))
    if loader == "systemd-boot":
        return "sudo -S -p '' bootctl set-oneshot " + shlex.quote(entry["id"])
    return "sudo -S -p '' efibootmgr --bootnext " + shlex.quote(entry["id"])


def safe_entry_problems(entry: Mapping[str, str]) -> list[str]:
    """The entry's own arguments (when the listing shows them) must switch the graphics drivers off."""
    arguments = entry.get("arguments", "")
    if not arguments:
        return []
    tokens = arguments.split()
    problems = [f"the Safe graphics entry lacks {token}" for token in SAFE_ARGUMENTS if token not in tokens]
    blacklist = next((token.split("=", 1)[1].split(",") for token in tokens if token.startswith("module_blacklist=")), [])
    missing = [name for name in SAFE_BLACKLISTED if name not in blacklist]
    if missing:
        problems.append("the Safe graphics entry does not blacklist " + ", ".join(missing))
    return problems


def safe_boot_problems(cmdline: str, modules: str) -> list[str]:
    """After booting the entry: the running kernel has the arguments and none of the graphics modules is loaded."""
    tokens = cmdline.split()
    problems = [f"the running kernel was not started with {token}" for token in SAFE_ARGUMENTS if token not in tokens]
    blacklist = next((token.split("=", 1)[1].split(",") for token in tokens if token.startswith("module_blacklist=")), [])
    if not blacklist:
        problems.append("the running kernel has no module_blacklist= argument")
    loaded = {line.split()[0] for line in modules.splitlines() if line.split()}
    present = sorted(name for name in SAFE_BLACKLISTED if name in loaded)
    if present:
        problems.append("graphics modules are loaded in the Safe graphics boot: " + ", ".join(present))
    return problems


SAFE_BOOT_COMMAND = "cat /proc/cmdline; echo ---; cat /proc/modules"


def parse_safe_boot_probe(returncode: int, output: str) -> ProbeResult:
    clean = ANSI_RE.sub("", output)
    cmdline, _, modules = clean.partition("\n---\n")
    if returncode != 0 or not cmdline.strip():
        return ProbeResult("safe-graphics-boot", False, clean.strip()[-400:], f"the Safe graphics boot could not be inspected (exit {returncode})")
    problems = safe_boot_problems(cmdline.strip(), modules)
    return ProbeResult("safe-graphics-boot", not problems, cmdline.strip()[:300], "; ".join(problems))
