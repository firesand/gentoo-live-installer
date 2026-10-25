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


# --- typing arbitrary text on the guest's console (Wave 6: the person at the keyboard of gentoo-rescue-do) -----------------------------
# A US keyboard: (qcode, shift). Only what the per-boot rescue password and the few answers of the launcher need; anything else is refused
# with a clear error instead of being typed wrongly.
_SHIFTED = {"_": "minus", "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0", "+": "equal",
            ":": "semicolon", "?": "slash"}
_PLAIN = {"-": "minus", ".": "dot", "/": "slash", " ": "spc", "=": "equal", ",": "comma", ";": "semicolon"}


def key_for(character: str) -> tuple[str, bool] | None:
    """(qcode, shift) of a character on a US keyboard, or None when this helper cannot type it."""
    if len(character) != 1:
        return None
    if "a" <= character <= "z" or "0" <= character <= "9":
        return character, False
    if "A" <= character <= "Z":
        return character.lower(), True
    if character in _PLAIN:
        return _PLAIN[character], False
    if character in _SHIFTED:
        return _SHIFTED[character], True
    return None


def untypeable(text: str) -> list[str]:
    """The distinct characters of ``text`` that ``type_text`` cannot type, in order of appearance."""
    return list(dict.fromkeys(character for character in text if key_for(character) is None))


def key_events(qcode: str, shift: bool = False) -> list[dict]:
    key = {"type": "qcode", "data": qcode}
    shift_key = {"type": "qcode", "data": "shift"}
    events = ([{"type": "key", "data": {"key": shift_key, "down": True}}] if shift else [])
    events += [{"type": "key", "data": {"key": key, "down": True}}, {"type": "key", "data": {"key": key, "down": False}}]
    if shift:
        events.append({"type": "key", "data": {"key": shift_key, "down": False}})
    return events


def type_text(socket_path: Path, text: str, *, enter: bool = True, send=None, sleep=time.sleep) -> int:
    """Type ``text`` (then Enter) on the guest's keyboard with QMP key events. Returns the number of keys pressed.

    The text is checked before the first key goes out, so a half-typed secret never happens. ``send(socket_path, command, arguments)`` is
    ``execute`` unless a test passes another.
    """
    bad = untypeable(text)
    if bad:
        raise ValueError(f"cannot type {''.join(bad)!r} on the guest's keyboard (letters, digits and - _ . / space ! @ # $ % ^ & * ( ) + = , ; : ? are supported)")
    send = send or execute
    pressed = 0
    for character in text:
        qcode, shift = key_for(character)  # type: ignore[misc]
        send(socket_path, "input-send-event", {"events": key_events(qcode, shift)})
        pressed += 1
        sleep(KEY_EVENT_DELAY_SECONDS)
    if enter:
        send(socket_path, "input-send-event", {"events": key_events("ret")})
        pressed += 1
    return pressed


def hostfwd_add(socket_path: Path, host_port: int, guest_port: int, *, guest_address: str = "10.0.2.15", host_address: str = "127.0.0.1",
                netdev: str = "net0", send=None) -> None:
    """Forward 127.0.0.1:host_port to the guest (QEMU's user-mode network) while the VM runs: for a service on a port the guest picks itself."""
    send = send or execute
    answer = send(socket_path, "human-monitor-command",
                  {"command-line": f"hostfwd_add {netdev} tcp:{host_address}:{host_port}-{guest_address}:{guest_port}"})
    text = str(answer.get("return", "")).strip() if isinstance(answer, dict) else ""
    if text:  # the monitor prints nothing when it worked
        raise RuntimeError(f"QEMU refused the port forward: {text}")


def hostfwd_remove(socket_path: Path, host_port: int, *, host_address: str = "127.0.0.1", netdev: str = "net0", send=None) -> None:
    send = send or execute
    send(socket_path, "human-monitor-command", {"command-line": f"hostfwd_remove {netdev} tcp:{host_address}:{host_port}"})
