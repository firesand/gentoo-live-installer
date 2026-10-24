"""Qt side of the daemon protocol: one worker thread per streaming request.

The blocking helpers (request, ping, capabilities, fetch_log, ...) live in
daemon_client and are re-exported here so callers have a single import.
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Any

from PySide6.QtCore import QThread, Signal

from . import daemon_client
from .daemon_client import (  # noqa: F401  (re-exported)
    DEFAULT_SOCKET,
    PROTOCOL_VERSION,
    DaemonError,
    DaemonState,
    capabilities,
    fetch_diagnostics,
    fetch_log,
    ping,
    probe,
    request,
    request_cancel,
)
from .manifest import InstallManifest


class BackendWorker(QThread):
    """Sends one request and relays the daemon's event stream as Qt signals.

    The default request is ``start`` with the manifest. Keyword-only options
    select the other streaming requests: ``resume=True`` resumes a stopped
    installation (the manifest must be the one that started it), ``attach=True``
    re-attaches to the current or last job and replays events after
    ``after_seq``, and ``request`` sends an arbitrary request instead (the
    manifest may then be None). ``cancel_request`` replaces the default
    ``cancel`` request sent by cancel().
    """

    eventReceived = Signal(dict)
    connectionFailed = Signal(str)
    cancelAcknowledged = Signal(bool, str)

    def __init__(
        self,
        manifest: InstallManifest | None,
        socket_path: str = DEFAULT_SOCKET,
        parent=None,
        *,
        resume: bool = False,
        attach: bool = False,
        after_seq: int = 0,
        request: dict[str, Any] | None = None,
        cancel_request: dict[str, Any] | None = None,
    ):
        super().__init__(parent)
        self.manifest = manifest
        self.socket_path = socket_path
        self.resume = resume
        self.attach = attach
        self.after_seq = after_seq
        self.request = request
        self.cancel_request = cancel_request
        self._socket: socket.socket | None = None

    def build_request(self) -> dict[str, Any]:
        if self.request is not None:
            return {"version": PROTOCOL_VERSION, **self.request}
        if self.attach:
            return {"version": PROTOCOL_VERSION, "action": "attach", "after_seq": int(self.after_seq)}
        if self.manifest is None:
            raise ValueError("A start request needs an installation manifest")
        request: dict[str, Any] = {"version": PROTOCOL_VERSION, "action": "start", "manifest": self.manifest.to_dict()}
        if self.resume:
            request["resume"] = True
        return request

    def run(self) -> None:
        try:
            payload = self.build_request()
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket.settimeout(10)
            self._socket.connect(self.socket_path)
            self._socket.sendall(json.dumps(payload).encode() + b"\n")
            stream = self._socket.makefile("r", encoding="utf-8")
            self._socket.settimeout(None)
            for line in stream:
                if self.isInterruptionRequested():
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    self.eventReceived.emit({"event": "log", "message": line.rstrip()})
                    continue
                self.eventReceived.emit(event)
                if event.get("event") in {"completed", "failed"} or event.get("type") in {"completed", "failed"}:
                    break
        except OSError as exc:  # TimeoutError is an OSError
            self.connectionFailed.emit(daemon_client.describe_os_error(exc, self.socket_path)[1])
        except ValueError as exc:
            self.connectionFailed.emit(str(exc))
        finally:
            if self._socket:
                self._socket.close()
                self._socket = None

    def cancel(self) -> None:
        # The start connection is a one-request event stream. Cancellation is
        # a second request and therefore needs its own connection.
        threading.Thread(target=self._send_cancel, name="gentoo-installer-cancel", daemon=True).start()

    def _send_cancel(self) -> None:
        """Send the cancel request, read the daemon's reply and report it."""
        payload = {"action": "cancel", **(self.cancel_request or {})}
        payload.pop("version", None)
        cancel_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        cancel_socket.settimeout(3)
        try:
            cancel_socket.connect(self.socket_path)
            cancel_socket.sendall(json.dumps({"version": PROTOCOL_VERSION, **payload}).encode() + b"\n")
            reply = cancel_socket.makefile("r", encoding="utf-8").readline()
        except OSError as exc:
            self.cancelAcknowledged.emit(False, daemon_client.describe_os_error(exc, self.socket_path)[1])
            return
        finally:
            cancel_socket.close()
        self.cancelAcknowledged.emit(*self._parse_cancel_reply(reply))

    @staticmethod
    def _parse_cancel_reply(reply: str) -> tuple[bool, str]:
        if not reply.strip():
            return False, "The installer service closed the connection without confirming the cancellation."
        try:
            event = json.loads(reply)
        except json.JSONDecodeError:
            return False, "The installer service sent an unreadable reply to the cancellation."
        if not isinstance(event, dict):
            return False, "The installer service sent an unreadable reply to the cancellation."
        if event.get("type") == "error":
            return False, str(event.get("error") or event.get("message") or "The cancellation was refused.")
        return True, str(event.get("message") or "Cancellation requested")
