from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .manifest import InstallManifest


class BackendWorker(QThread):
    eventReceived = Signal(dict)
    connectionFailed = Signal(str)

    def __init__(self, manifest: InstallManifest, socket_path: str = "/run/gentoo-installer/server.sock", parent=None):
        super().__init__(parent)
        self.manifest = manifest
        self.socket_path = socket_path
        self._socket: socket.socket | None = None

    def run(self) -> None:
        try:
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket.settimeout(10)
            self._socket.connect(self.socket_path)
            request = {"version": 1, "action": "start", "manifest": self.manifest.to_dict()}
            self._socket.sendall(json.dumps(request).encode() + b"\n")
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
        except (OSError, TimeoutError) as exc:
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
        cancel_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        cancel_socket.settimeout(2)
        try:
            cancel_socket.connect(self.socket_path)
            cancel_socket.sendall(json.dumps({"version": 1, "action": "cancel"}).encode() + b"\n")
        except OSError:
            pass
        finally:
            cancel_socket.close()
