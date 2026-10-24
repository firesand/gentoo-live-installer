"""Small, argument-safe QMP helpers for disposable installer VMs.

This is the library; ``vm-qmp.py`` (hyphen) is the operator-facing command line
on top of it, and ``test-vm.py`` imports the library directly.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import time


KEYCODES = {**{chr(code): chr(code) for code in range(ord("a"), ord("z") + 1)},
            **{chr(code): chr(code) for code in range(ord("0"), ord("9") + 1)}, "-": "minus"}
TEST_LUKS_PASSPHRASE = "vmtest-only-luks-passphrase"
KEY_EVENT_DELAY_SECONDS = 0.05
SUN_PATH_MAX = 107  # bytes usable in sockaddr_un.sun_path (108 including the NUL)


def connect_unix(path: str | os.PathLike[str], timeout: float | None = None) -> socket.socket:
    """connect() to an AF_UNIX socket even when its path exceeds the sun_path limit.

    A long path is reached through an O_DIRECTORY descriptor of the parent
    (/proc/self/fd/N/name), so the harness also works from deep checkouts.
    """
    raw = os.fspath(path)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if timeout is not None:
            client.settimeout(timeout)
        if len(os.fsencode(raw)) <= SUN_PATH_MAX:
            client.connect(raw)
        else:
            directory, name = os.path.split(raw)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                client.connect(f"/proc/self/fd/{descriptor}/{name}")
            finally:
                os.close(descriptor)
    except BaseException:
        client.close()
        raise
    return client


def _read_message(stream) -> dict:
    line = stream.readline()
    if not line:
        raise RuntimeError("QMP socket closed before the guest answered")
    return json.loads(line)


def execute(socket_path: Path, command: str, arguments: dict | None = None) -> dict:
    with connect_unix(socket_path, timeout=5) as client:
        stream = client.makefile("rwb")
        greeting = _read_message(stream)
        if "QMP" not in greeting:
            raise RuntimeError("QMP socket did not return a QMP greeting")
        for request in ({"execute": "qmp_capabilities"},
                        {"execute": command, **({"arguments": arguments} if arguments is not None else {})}):
            stream.write(json.dumps(request).encode() + b"\r\n")
            stream.flush()
            while True:
                response = _read_message(stream)
                if "event" in response:
                    continue
                if "error" in response:
                    raise RuntimeError(f"QMP {request['execute']} failed: {response['error']}")
                if "return" in response:
                    break
        return response["return"] if isinstance(response["return"], dict) else {"return": response["return"]}


def screendump(socket_path: Path, output: Path) -> Path:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    execute(socket_path, "screendump", {"filename": str(output)})
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError(f"QMP screendump did not create a nonempty image: {output}")
    return output


def screen_digest(image: Path) -> str:
    """Fingerprint of a screendump, to tell whether the guest screen changed."""
    return hashlib.sha256(Path(image).read_bytes()).hexdigest()


def type_luks_passphrase(socket_path: Path, passphrase: str) -> None:
    if passphrase != TEST_LUKS_PASSPHRASE or any(character not in KEYCODES for character in passphrase):
        raise ValueError("Only the fixed test LUKS passphrase is supported by this QMP helper")
    for character in passphrase:
        key = {"type": "qcode", "data": KEYCODES[character]}
        events = ({"type": "key", "data": {"key": key, "down": True}},
                  {"type": "key", "data": {"key": key, "down": False}})
        execute(socket_path, "input-send-event", {"events": list(events)})
        time.sleep(KEY_EVENT_DELAY_SECONDS)
    enter = {"type": "qcode", "data": "ret"}
    events = ({"type": "key", "data": {"key": enter, "down": True}},
              {"type": "key", "data": {"key": enter, "down": False}})
    execute(socket_path, "input-send-event", {"events": list(events)})
