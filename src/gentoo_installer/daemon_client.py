"""Blocking client for the installer daemon's JSON-lines protocol (docs/protocol.md).

No Qt in here: the GUI worker threads, the diagnostics bundle and the tests
share these helpers. Every function degrades gracefully when the daemon is old
(no ``capabilities``, ``attach`` or ``log`` action): such a daemon answers with
an ``error`` event and the helpers report "feature not available" instead of
raising.
"""

from __future__ import annotations

import errno
import json
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

DEFAULT_SOCKET = "/run/gentoo-installer/server.sock"
PROTOCOL_VERSION = 1
TERMINAL_TYPES = frozenset({"completed", "failed"})

SERVICE_COMMAND = "sudo rc-service gentoo-installerd restart"
GROUP_COMMAND = "sudo gpasswd -a $USER gentoo-installer"


class DaemonError(RuntimeError):
    """The daemon answered a request with an error event."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DaemonState:
    """Result of ping(): can this GUI talk to the installer service?

    kind is "running" (the daemon answered), "not-running" (no socket),
    "stale" (a socket nobody listens on), "no-access" (permission denied) or
    "error" (anything else, including a daemon that does not answer).
    """

    kind: str
    message: str = ""
    status: dict[str, Any] = field(default_factory=dict)
    features: frozenset[str] = frozenset()
    actions: frozenset[str] = frozenset()

    @property
    def reachable(self) -> bool:
        return self.kind == "running"

    @property
    def job_status(self) -> str:
        """idle | running | completed | failed | cancelled ("" when unknown)."""
        return str(self.status.get("status", ""))

    @property
    def job_running(self) -> bool:
        return self.job_status == "running"

    def supports(self, name: str) -> bool:
        """True when the daemon advertises this feature (``attach.v1``) or action."""
        return name in self.features or name in self.actions


def describe_os_error(exc: OSError, socket_path: str = DEFAULT_SOCKET) -> tuple[str, str]:
    """Classify a socket error into (DaemonState.kind, an instruction for the user)."""
    code = exc.errno
    if isinstance(exc, FileNotFoundError) or code in (errno.ENOENT, errno.ENOTDIR):
        return "not-running", (
            f"The installer service is not running (there is no socket at {socket_path}). "
            f"Start it with: {SERVICE_COMMAND}"
        )
    if isinstance(exc, ConnectionRefusedError) or code == errno.ECONNREFUSED:
        return "stale", (
            f"The installer service is not answering on {socket_path} (a stale socket was left behind). "
            f"Restart it with: {SERVICE_COMMAND}"
        )
    if isinstance(exc, PermissionError) or code in (errno.EACCES, errno.EPERM):
        return "no-access", (
            "This user is not allowed to talk to the installer service. "
            f"Add the user to the gentoo-installer group ({GROUP_COMMAND}) and log in again."
        )
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "error", "The installer service did not answer in time. It may be busy; check again in a moment."
    return "error", f"Could not talk to the installer service: {exc.strerror or exc}"


def stream(
    socket_path: str,
    payload: dict[str, Any],
    *,
    timeout: float = 3.0,
    read_timeout: float | None = None,
) -> Iterator[dict[str, Any]]:
    """Send one request and yield the daemon's events until it closes the
    connection or sends ``completed``. Raises OSError (connect/read errors,
    TimeoutError when the daemon is silent for ``read_timeout`` seconds)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        sock.sendall(json.dumps({"version": PROTOCOL_VERSION, **payload}).encode() + b"\n")
        sock.settimeout(read_timeout if read_timeout is not None else timeout)
        with sock.makefile("r", encoding="utf-8", errors="replace") as lines:
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    event = {"type": "log", "message": line}
                if not isinstance(event, dict):
                    continue
                yield event
                if event.get("type") in TERMINAL_TYPES:
                    return
    finally:
        sock.close()


def request(
    socket_path: str,
    action: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 3.0,
    max_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Run a short, non-streaming request and return all its events.

    ``timeout`` bounds connecting and each wait for the next event;
    ``max_seconds`` (default: ``timeout`` * 4) bounds the whole call."""
    deadline = time.monotonic() + (max_seconds if max_seconds is not None else timeout * 4)
    events: list[dict[str, Any]] = []
    for event in stream(socket_path, {"action": action, **(payload or {})}, timeout=timeout):
        events.append(event)
        if time.monotonic() > deadline:
            raise TimeoutError(f"The installer service took too long to answer '{action}'")
    return events


def event_data(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def first_error(events: list[dict[str, Any]]) -> tuple[str, str] | None:
    """(message, machine code) of the first error event, if any."""
    for event in events:
        if event.get("type") == "error":
            data = event_data(event)
            return str(event.get("error") or event.get("message") or "Unknown error"), str(data.get("code", ""))
    return None


def ping(socket_path: str = DEFAULT_SOCKET, timeout: float = 3.0) -> DaemonState:
    """Ask the daemon for its status. Never raises."""
    try:
        events = request(socket_path, "status", timeout=timeout)
    except OSError as exc:
        kind, message = describe_os_error(exc, socket_path)
        return DaemonState(kind, message)
    for event in events:
        if event.get("type") == "status":
            return DaemonState("running", "", event_data(event))
    error = first_error(events)
    if error:
        return DaemonState("running", error[0])
    return DaemonState("error", "The installer service sent an unexpected reply.")


def status(socket_path: str = DEFAULT_SOCKET, timeout: float = 3.0) -> dict[str, Any]:
    """The daemon's status data (``status``, ``stage``, ``progress``, ``seq``, ``resumable``,
    ``error`` ...). Raises OSError when the daemon cannot be reached."""
    for event in request(socket_path, "status", timeout=timeout):
        if event.get("type") == "status":
            return event_data(event)
    return {}


def capabilities(socket_path: str = DEFAULT_SOCKET, timeout: float = 3.0) -> tuple[frozenset[str], frozenset[str]]:
    """(features, actions) advertised by the daemon; empty for an old daemon."""
    try:
        events = request(socket_path, "capabilities", timeout=timeout)
    except OSError:
        return frozenset(), frozenset()
    for event in events:
        data = event_data(event)
        if event.get("type") == "result" and isinstance(data.get("features"), list):
            actions = data.get("actions")
            return (
                frozenset(str(item) for item in data["features"]),
                frozenset(str(item) for item in actions) if isinstance(actions, list) else frozenset(),
            )
    return frozenset(), frozenset()


def probe(socket_path: str = DEFAULT_SOCKET, timeout: float = 3.0) -> DaemonState:
    """ping() plus the advertised features and actions."""
    state = ping(socket_path, timeout)
    if not state.reachable:
        return state
    features, actions = capabilities(socket_path, timeout)
    return DaemonState(state.kind, state.message, state.status, features, actions)


def request_cancel(socket_path: str, timeout: float = 3.0) -> tuple[bool, str]:
    """Ask the daemon to cancel the running installation: (accepted, message)."""
    try:
        events = request(socket_path, "cancel", timeout=timeout)
    except OSError as exc:
        return False, describe_os_error(exc, socket_path)[1]
    error = first_error(events)
    if error:
        return False, error[0]
    for event in events:
        if event.get("type") == "status":
            return True, str(event.get("message") or "Cancellation requested")
    return False, "The installer service closed the connection without confirming the cancellation."


def fetch_log(socket_path: str, tail: int = 0, timeout: float = 5.0) -> Iterator[str]:
    """Yield the daemon's redacted install log, oldest line first.

    Raises DaemonError when the daemon has no ``log`` action (old daemon) or
    refuses it, and OSError when it cannot be reached."""
    produced = False
    for event in stream(socket_path, {"action": "log", "tail": int(tail)}, timeout=timeout, read_timeout=timeout * 6):
        kind = event.get("type")
        if kind == "error":
            data = event_data(event)
            raise DaemonError(str(event.get("error") or event.get("message") or "log is not available"), str(data.get("code", "")))
        if kind == "result":
            lines = event_data(event).get("lines")
            if isinstance(lines, list):
                produced = True
                for line in lines:
                    yield str(line)
        elif kind == "log" and event.get("message"):
            produced = True
            yield str(event["message"])
    if not produced:
        return


def fetch_diagnostics(socket_path: str, timeout: float = 20.0) -> dict[str, Any] | None:
    """The daemon's diagnostics document, or None when it has no such action."""
    chunks: list[str] = []
    try:
        for event in stream(socket_path, {"action": "diagnostics"}, timeout=3.0, read_timeout=timeout):
            kind = event.get("type")
            if kind == "error":
                return None
            if kind in {"log", "result"}:
                data = event_data(event)
                if isinstance(data.get("lines"), list):
                    chunks.extend(str(line) for line in data["lines"])
                elif event.get("message"):
                    chunks.append(str(event["message"]))
    except OSError:
        return None
    text = "\n".join(chunks).strip()
    if not text:
        return None
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}
    return document if isinstance(document, dict) else {"raw": document}
