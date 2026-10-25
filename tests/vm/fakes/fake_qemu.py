#!/usr/bin/env python3
"""A stand-in for qemu-system-x86_64 that lets the harness run its whole control flow in tests.

It starts nothing: it parses the harness's QEMU command line, creates the serial and QMP
sockets the harness connects to, answers the QMP commands the harness uses (capabilities,
quit, system_powerdown, screendump, input-send-event), can print a LUKS prompt on the serial socket, records
what it saw next to itself, and exits on SIGTERM, on QMP ``quit`` or after a scripted delay.

Behaviour comes from the JSON file named by FAKE_QEMU_CONFIG (all keys optional):
  serial_text_target / serial_text_live   text written to every serial client after it connects
  serial_rules                            a list of rules, the first that matches this start decides the serial text and when the
                                          fake powers off. A rule may have: phase ("live" | "target"), firmware ("uefi" | "bios"),
                                          boot (1-based number of this start within its phase), requires / absent (files in the run
                                          directory, which is the working directory), text, touch (files to create), exit_after, exit_code
  unlock_line                             a serial line that creates ./fake-unlocked
  exit_after                              seconds until the fake "powers off" by itself
  exit_code                               exit status used when it powers off (default 0)
  fail_to_start                           print this to stderr and exit 1 before any socket exists

Every start is recorded as fake-qemu-argv-<phase>.json (the latest) and fake-qemu-argv-<phase>-<n>.json.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

CONFIG = json.loads(Path(os.environ["FAKE_QEMU_CONFIG"]).read_text()) if os.environ.get("FAKE_QEMU_CONFIG") else {}
STOP = threading.Event()
EXIT_CODE = [0]


def option_values(argv: list[str], flag: str) -> list[str]:
    return [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == flag]


def parse_sockets(argv: list[str]) -> tuple[str, str]:
    serial = next(v.split("path=", 1)[1].split(",", 1)[0] for v in option_values(argv, "-chardev") if "id=serial0" in v)
    qmp = next(v[len("unix:"):].split(",", 1)[0] for v in option_values(argv, "-qmp") if v.startswith("unix:"))
    return serial, qmp


def listen(path: str) -> socket.socket:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(4)
    server.settimeout(0.2)
    return server


def append(name: str, text: str) -> None:
    with open(name, "a") as stream:
        stream.write(text)


def matching_rule(phase: str, firmware: str, boot: int) -> dict:
    """The first serial rule of the configuration that applies to this start ({} when none does)."""
    for rule in CONFIG.get("serial_rules", []):
        if rule.get("phase") not in (None, phase) or rule.get("firmware") not in (None, firmware) or rule.get("boot") not in (None, boot):
            continue
        if rule.get("requires") and not Path(rule["requires"]).exists():
            continue
        if rule.get("absent") and Path(rule["absent"]).exists():
            continue
        return rule
    return {}


def serve_serial(server: socket.socket, live: bool, banner: str | None = None) -> None:
    if banner is None:
        banner = CONFIG.get("serial_text_live" if live else "serial_text_target", "")
    while not STOP.is_set():
        try:
            client, _ = server.accept()
        except OSError:
            continue
        client.settimeout(0.2)
        if banner:
            client.sendall(banner.encode())
        threading.Thread(target=serial_client, args=(client,), daemon=True).start()


def serial_client(client: socket.socket) -> None:
    pending = b""
    while not STOP.is_set():
        try:
            data = client.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            return
        if not data:
            return
        pending += data
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            append("fake-serial-input.txt", line.decode(errors="replace") + "\n")
            if CONFIG.get("unlock_line") and line.decode(errors="replace") == CONFIG["unlock_line"]:
                Path("fake-unlocked").write_text("1")


def serve_qmp(server: socket.socket) -> None:
    while not STOP.is_set():
        try:
            client, _ = server.accept()
        except OSError:
            continue
        threading.Thread(target=qmp_client, args=(client,), daemon=True).start()


def qmp_client(client: socket.socket) -> None:
    client.settimeout(2)
    stream = client.makefile("rwb")
    stream.write(b'{"QMP": {"version": {"qemu": {"major": 99}}, "capabilities": []}}\r\n')
    stream.flush()
    try:
        for line in stream:
            request = json.loads(line)
            command = request.get("execute")
            arguments = request.get("arguments") or {}
            if command == "screendump":
                width, height = display_size()
                Path(arguments["filename"]).write_bytes(b"P6\n%d %d\n255\n" % (width, height) + b"\x00" * (3 * width * height))
            elif command == "input-send-event":
                append("fake-qmp-keys.jsonl", json.dumps(arguments) + "\n")
            elif command == "human-monitor-command":
                append("fake-qmp-hmp.jsonl", json.dumps(arguments) + "\n")
                stream.write(json.dumps({"return": CONFIG.get("hmp_answer", "")}).encode() + b"\r\n")
                stream.flush()
                continue
            stream.write(json.dumps({"return": {}}).encode() + b"\r\n")
            stream.flush()
            if command in ("quit", "system_powerdown"):  # a powered-down guest ends the QEMU process
                STOP.set()
                return
    except (OSError, ValueError):
        return


def display_size() -> tuple[int, int]:
    """The size the harness asked the display for (xres=, yres= of the graphics device), else a 1x1 picture."""
    import re

    match = re.search(r"xres=(\d+),yres=(\d+)", " ".join(sys.argv))
    return (int(match.group(1)), int(match.group(2))) if match else (1, 1)


def main() -> int:
    argv = sys.argv[1:]
    if CONFIG.get("fail_to_start"):
        print(CONFIG["fail_to_start"], file=sys.stderr)
        return 1
    live = any("media=cdrom" in value for value in option_values(argv, "-drive"))
    phase = "live" if live else "target"
    counter = Path(f"fake-qemu-boots-{phase}.txt")
    boot = (int(counter.read_text()) if counter.exists() else 0) + 1
    counter.write_text(str(boot))
    Path(f"fake-qemu-argv-{phase}.json").write_text(json.dumps(argv, indent=2))
    Path(f"fake-qemu-argv-{phase}-{boot}.json").write_text(json.dumps(argv, indent=2))
    Path(f"fake-qemu-pid-{phase}.txt").write_text(str(os.getpid()))
    firmware = "uefi" if any("if=pflash" in value for value in option_values(argv, "-drive")) else "bios"
    rule = matching_rule(phase, firmware, boot)
    for name in rule.get("touch", []):
        Path(name).write_text("1")
    serial_path, qmp_path = parse_sockets(argv)
    servers = [listen(serial_path), listen(qmp_path)]
    threading.Thread(target=serve_serial, args=(servers[0], live, rule.get("text") if rule else None), daemon=True).start()
    threading.Thread(target=serve_qmp, args=(servers[1],), daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    exit_after = rule.get("exit_after", CONFIG.get("exit_after")) if rule else CONFIG.get("exit_after")
    deadline = time.monotonic() + float(exit_after) if exit_after is not None else None
    while not STOP.is_set():
        if deadline is not None and time.monotonic() >= deadline:
            EXIT_CODE[0] = int((rule or {}).get("exit_code", CONFIG.get("exit_code", 0)))
            break
        time.sleep(0.05)
    for server, path in zip(servers, (serial_path, qmp_path)):
        server.close()
        try:
            os.unlink(path)
        except OSError:
            pass
    return EXIT_CODE[0]


if __name__ == "__main__":
    raise SystemExit(main())
