"""NET: the GUI's side of the daemon's network and remote-access actions (interfaces I8 and I9), without Qt widgets.

``call`` runs one action and returns its answer as a dictionary; it never raises. ``CallWorker`` does the same on a thread for
the windows. The passwords given to ``call`` travel over the daemon's local socket only; they are never printed or logged here.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

import shiboken6
from PySide6.QtCore import QObject, QThread, Signal

from . import daemon_client

CONNECT_SECONDS = 75.0  # wifi_connect waits up to 45 s for the network and 60 s for the command
DEFAULT_TIMEOUT = 12.0

# (label, DNS servers): the presets of the static-address form. Router means "the gateway address" (filled in when the form is read).
DNS_PRESETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Router (its address, same as the gateway)", ()),
    ("Cloudflare (1.1.1.1, 1.0.0.1)", ("1.1.1.1", "1.0.0.1")),
    ("Quad9 (9.9.9.9, 149.112.112.112)", ("9.9.9.9", "149.112.112.112")),
    ("Google (8.8.8.8, 8.8.4.4)", ("8.8.8.8", "8.8.4.4")),
)
SECURITY_LABELS = {"open": "open", "wpa-psk": "WPA2", "sae": "WPA3", "owe": "open (encrypted)", "wep": "WEP (not supported)",
                   "enterprise": "needs a login (802.1X)"}
UNAVAILABLE = "The installer service is not available, so this cannot be done here."

Caller = Callable[[str, dict[str, Any]], dict[str, Any]]


def call(socket_path: str, action: str, payload: dict[str, Any] | None = None, timeout: float = DEFAULT_TIMEOUT,
         max_seconds: float | None = None) -> dict[str, Any]:
    """Run one daemon action; the answer is the ``data`` of its result event, with ``ok`` and ``message`` always present."""
    try:
        events = daemon_client.request(socket_path, action, payload, timeout=timeout, max_seconds=max_seconds or timeout * 2)
    except (OSError, TimeoutError, ValueError) as exc:
        return {"ok": False, "message": daemon_client.describe_os_error(exc, socket_path)[1] if isinstance(exc, OSError) else str(exc)}
    error = daemon_client.first_error(events)
    if error is not None:
        return {"ok": False, "message": error[0], "code": error[1]}
    for event in events:
        if event.get("type") == "result":
            data = dict(daemon_client.event_data(event))
            data.setdefault("ok", True)
            data.setdefault("message", str(event.get("message") or ""))
            return data
    return {"ok": False, "message": "The installer service gave no answer."}


def caller_for(socket_path: str) -> Caller:
    """A function (action, payload) -> answer bound to a daemon socket; connecting gets the longer wait."""
    def run(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        long = action in {"wifi_connect", "network_static"}
        return call(socket_path, action, payload, timeout=CONNECT_SECONDS if long else DEFAULT_TIMEOUT,
                    max_seconds=CONNECT_SECONDS + 30 if long else None)
    return run


_LIVE: set["CallWorker"] = set()  # running workers stay referenced until they have ended, whatever happens to their widget


class CallWorker(QThread):
    """Runs ``caller(action, payload)`` off the GUI thread and emits ``answered(action, answer)``.

    Destroying a QThread that still runs aborts the whole program, so a worker owns its own life: it has no parent (a page
    closed during a Wi-Fi scan must not take a running thread with it), it stays referenced while it runs, and it is
    deleted only once it has really ended. ``finished`` is emitted from the thread just before it ends, so the worker waits
    for that end before it lets go (deleting it on ``answered``, or right on ``finished``, aborted test runs now and then).
    ``parent`` is accepted for compatibility and ignored.
    """

    answered = Signal(str, object)

    def __init__(self, caller: Caller, action: str, payload: dict[str, Any], parent: Any = None) -> None:
        super().__init__(None)
        self._caller, self._action, self._payload = caller, action, payload
        self.finished.connect(self._ended)

    def start(self, *args: Any) -> None:
        _LIVE.add(self)
        super().start(*args)

    def _ended(self) -> None:
        self.wait()
        _LIVE.discard(self)
        self.deleteLater()

    def run(self) -> None:
        try:
            answer = self._caller(self._action, self._payload)
        except Exception as exc:  # noqa: BLE001 - a thread must always answer
            answer = {"ok": False, "message": f"{type(exc).__name__}: the request failed."}
        self.answered.emit(self._action, answer)


def deliver(owner: QObject, callback: Callable[..., Any]) -> Callable[..., None]:
    """A slot that passes an answer on to ``callback`` only while ``owner``, the widget that asked, still exists."""

    def slot(*arguments: Any) -> None:
        if shiboken6.isValid(owner):
            callback(*arguments)

    return slot


def read_live_secret(connection_uuid: str, timeout: float = 3.0) -> str:
    """The saved Wi-Fi password of a live connection as the live user (no prompt, short wait); '' when it is not available.

    A profile made with the panel's network icon often keeps the password in the user's keyring, which this can read.
    """
    try:
        result = subprocess.run(
            ["nmcli", "--show-secrets", "-g", "802-11-wireless-security.psk", "connection", "show", "uuid", connection_uuid],
            capture_output=True, text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""
