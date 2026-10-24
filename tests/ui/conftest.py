"""Shared fixtures for the GUI tests.

Every GUI test runs against a private XDG tree (QSettings, the GUI log and the
saved draft must never touch the developer's real home) and, unless a test
opts out, in development mode so the window does not look for a daemon socket.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Callable

import pytest


@pytest.fixture(autouse=True)
def private_user_dirs(monkeypatch, tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("GENTOO_INSTALLER_DEV", "1")
    return home


Handler = Callable[[dict], list[dict]]


class FakeDaemon:
    """A tiny JSON-lines server on a unix socket: one request line in, a list of events out."""

    def __init__(self, path: Path, handler: Handler):
        self.path = str(path)
        self.handler = handler
        self.requests: list[dict] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection: socket.socket) -> None:
        try:
            with connection, connection.makefile("rwb") as stream:
                line = stream.readline()
                if not line:
                    return
                request = json.loads(line)
                self.requests.append(request)
                for event in self.handler(request):
                    stream.write(json.dumps(event).encode() + b"\n")
                    stream.flush()
        except OSError:
            pass  # the client left early (a worker stops reading after "completed")

    def close(self) -> None:
        self._stop = True
        try:
            self._server.close()
        except OSError:
            pass


@pytest.fixture
def fake_daemon(tmp_path):
    daemons: list[FakeDaemon] = []

    def make(handler: Handler, name: str = "daemon.sock") -> FakeDaemon:
        daemon = FakeDaemon(tmp_path / name, handler)
        daemons.append(daemon)
        return daemon

    yield make
    for daemon in daemons:
        daemon.close()
