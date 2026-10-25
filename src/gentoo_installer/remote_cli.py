"""``gentoo-remote-access [on|off|status]``: log in to this live medium over SSH from another computer (interface I9).

Run it in the live system's terminal. ``on`` gives the live user ``gentoo`` a password (shown once) and prints the ``ssh`` line to type
on the other computer; ``off`` removes the password again. The work is done by the installer service (``remote_enable``,
``remote_disable``, ``remote_status``); this program only asks it and prints the answer.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Any, TextIO

from . import netclient
from .daemon_client import DEFAULT_SOCKET

HANDBOOK_WAY = ("The official way works too: run  sudo passwd root  here, then  ssh root@ADDRESS  from the other computer.")


def describe(status: dict[str, Any], password: str = "", console: bool = True) -> list[str]:
    """The lines to print for a status answer (and the password, only right after ``on``)."""
    lines: list[str] = []
    user, port = status.get("user", "gentoo"), status.get("port", 22)
    addresses = [str(a) for a in status.get("addresses") or []]
    if status.get("enabled"):
        lines.append("Remote access is ON. From another computer on the same network type:")
        port_part = f" -p {port}" if port != 22 else ""
        if addresses:
            lines.extend(f"    ssh{port_part} {user}@{address}" for address in addresses)
        else:
            lines.append("    (this computer has no network address yet: connect to the network first)")
        if password:
            lines.append(f"The password is: {password}")
            lines.append("It is shown only now. Turn remote access off when you are done" + (":  gentoo-remote-access off" if console else "."))
    else:
        lines.append("Remote access is OFF. Nobody can log in over the network.")
        if console:
            lines.append("Turn it on with:  gentoo-remote-access on")
    if not status.get("sshd_running"):
        lines.append("The SSH server is not running.")
    fingerprints = status.get("fingerprints") or []
    if fingerprints and status.get("enabled"):
        lines.append("The first time, ssh asks you to trust this computer. Its fingerprints:")
        lines.extend(f"    {item.get('type', '?')}  {item.get('sha256', '')}" for item in fingerprints)
    if not status.get("enabled") and console:
        lines.append(HANDBOOK_WAY)
    return lines


def main(argv: list[str] | None = None, out: TextIO | None = None, socket_path: str = DEFAULT_SOCKET) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="gentoo-remote-access", description="Log in to this live system over SSH from another computer.")
    parser.add_argument("action", nargs="?", choices=("on", "off", "status"), default="status")
    parser.add_argument("--ask-password", action="store_true", help="type the password yourself instead of getting a generated one")
    args = parser.parse_args(argv)
    if args.action == "on":
        password = getpass.getpass("Password for the user gentoo (empty: make one up): ") if args.ask_password else ""
        answer = netclient.call(socket_path, "remote_enable", {"password": password}, timeout=30.0)
    elif args.action == "off":
        answer = netclient.call(socket_path, "remote_disable", {}, timeout=30.0)
    else:
        answer = netclient.call(socket_path, "remote_status", {})
    if not answer.get("ok"):
        print(answer.get("message") or "That did not work.", file=out)
        return 1
    for line in describe(answer, str(answer.get("password") or "")):
        print(line, file=out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
