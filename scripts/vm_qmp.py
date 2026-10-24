"""Small, argument-safe QMP helpers for disposable installer VMs."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import time


KEYCODES = {**{chr(code): chr(code) for code in range(ord("a"), ord("z") + 1)},
            **{chr(code): chr(code) for code in range(ord("0"), ord("9") + 1)}, "-": "minus"}
TEST_LUKS_PASSPHRASE = "vmtest-only-luks-passphrase"
KEY_EVENT_DELAY_SECONDS = 0.05


def execute(socket_path: Path, command: str, arguments: dict | None = None) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(socket_path))
        stream = client.makefile("rwb")
        greeting = json.loads(stream.readline())
        if "QMP" not in greeting:
            raise RuntimeError("QMP socket did not return a QMP greeting")
        for request in ({"execute": "qmp_capabilities"},
                        {"execute": command, **({"arguments": arguments} if arguments is not None else {})}):
            stream.write(json.dumps(request).encode() + b"\r\n")
            stream.flush()
            while True:
                response = json.loads(stream.readline())
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
