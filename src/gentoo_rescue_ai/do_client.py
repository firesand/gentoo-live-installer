"""``gentoo-rescue-do COMMAND...``: ask the person at the PC's console to run one command as root.

This is the only way the AI agents get root. It is NOT sudo: it sends the command line and the current folder over a Unix
socket to the approver service, which shows them on its own root-owned virtual terminal and waits for the per-boot password
to be typed THERE. Nothing is asked in this terminal, and nothing typed here can approve anything. The command's output
comes back as data; its standard input is not connected.

Exit status: the command's own; 77 when it was refused or not approved; 69 when the approver is not running.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import sys
from typing import Any, BinaryIO, Callable

from . import DO_SOCKET

EXIT_REFUSED = 77
EXIT_UNAVAILABLE = 69
EXIT_USAGE = 64


def request(path: str, argv: list[str], cwd: str, out: BinaryIO, err: BinaryIO, note: Callable[[str], None]) -> int:
    """Send one command to the approver and relay its answer; returns the exit status to use."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(path)
    except OSError as exc:
        note(f"gentoo-rescue-do: the approver is not running or not reachable ({exc}). It is started at boot in the 'AI rescue' entry only.")
        return EXIT_UNAVAILABLE
    with conn:
        conn.sendall((json.dumps({"argv": argv, "cwd": cwd}) + "\n").encode())
        buffer = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                note("gentoo-rescue-do: the approver closed the connection without an answer")
                return EXIT_UNAVAILABLE
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    event: dict[str, Any] = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("event")
                if kind == "waiting":
                    note("gentoo-rescue-do: " + str(event.get("message", "")) + ". The phone cannot approve; only the keyboard of this PC.")
                elif kind == "denied":
                    note("gentoo-rescue-do: not run: " + str(event.get("reason", "refused")))
                    return EXIT_REFUSED
                elif kind == "output":
                    target = err if event.get("stream") == "stderr" else out
                    target.write(base64.b64decode(str(event.get("data", ""))))
                    target.flush()
                elif kind == "exit":
                    return int(event.get("status", 1))


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if not arguments or arguments[0] in {"-h", "--help"}:
        print("usage: gentoo-rescue-do [--] COMMAND [ARGUMENT]...\n\n" + (__doc__ or "").strip().split("\n\n", 1)[1], file=sys.stderr if not arguments else sys.stdout)
        return EXIT_USAGE if not arguments else 0
    if os.geteuid() == 0:
        print("gentoo-rescue-do: you are root already; run the command directly", file=sys.stderr)
        return EXIT_USAGE
    return request(os.environ.get("GENTOO_RESCUE_DO_SOCKET", DO_SOCKET), arguments, os.getcwd(), sys.stdout.buffer, sys.stderr.buffer,
                   lambda text: print(text, file=sys.stderr, flush=True))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
