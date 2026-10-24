"""Starting other programs: a terminal inside the repaired system, and the restart.

Everything goes through ``start_detached`` (the pattern of ``keyboard.start_detached``), so the tests never start a program.
"""

from __future__ import annotations

import os
import shutil
from typing import Callable

from PySide6.QtCore import QProcess

from . import model

NO_TERMINAL = "No terminal program was found on this system. Use the terminal tool from another console: sudo gentoo-rescue"


def start_detached(program: str, arguments: list[str]) -> bool:
    return bool(QProcess.startDetached(program, arguments))


def find_terminal(which: Callable[[str], str | None] = shutil.which) -> str | None:
    return next((name for name in model.TERMINAL_EMULATORS if which(name)), None)


def open_terminal(socket_path: str, session_id: str, root_device: str, title: str, *, writable: bool = False, host: bool = False,
                  which: Callable[[str], str | None] = shutil.which, access: Callable[[str, int], bool] = os.access) -> tuple[bool, str]:
    """Open a terminal window that runs ``gentoo-rescue shell`` for the session. Returns (started, a problem to show).

    The terminal tool talks to the daemon over the same socket as this window and gets the daemon's PTY handed over, which needs a
    real terminal. ``sudo`` is only added when this user cannot open the socket itself (it normally can: the window needs it too).
    """
    emulator = find_terminal(which)
    if emulator is None:
        return False, NO_TERMINAL
    sudo = not access(socket_path, os.R_OK | os.W_OK)
    argv = model.terminal_argv(socket_path, session_id, root_device, writable=writable, host=host, sudo=sudo)
    started = start_detached(emulator, model.terminal_emulator_argv(emulator, title, model.shell_line(argv)))
    return started, "" if started else f"{emulator} could not be started."


def open_tool_terminal(title: str = "Repair tool", *, which: Callable[[str], str | None] = shutil.which) -> tuple[bool, str]:
    """A terminal that runs ``sudo gentoo-rescue``: the text version of the repair tool, for when this window cannot work."""
    emulator = find_terminal(which)
    if emulator is None:
        return False, NO_TERMINAL
    started = start_detached(emulator, model.terminal_emulator_argv(emulator, title, model.shell_line(["sudo", "gentoo-rescue"])))
    return started, "" if started else f"{emulator} could not be started."


def restart_computer() -> bool:
    for program, arguments in (("loginctl", ["reboot"]), ("systemctl", ["reboot"]), ("reboot", [])):
        if start_detached(program, arguments):
            return True
    return False
