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
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication


@pytest.fixture(autouse=True)
def no_stuck_dialogs(qapp):
    """A modal dialog nobody answers would hang the whole run: close it after
    a moment and fail the test that opened it."""
    seen: list[str] = []

    def watch() -> None:
        modal = QApplication.activeModalWidget()
        if modal is not None:
            seen.append(modal.windowTitle() or type(modal).__name__)
            modal.reject()

    timer = QTimer()
    timer.timeout.connect(watch)
    timer.start(1500)
    yield
    timer.stop()
    assert not seen, f"a test left a modal dialog open: {seen}"


@pytest.fixture(autouse=True)
def deferred_deletes_happen(qapp):
    """Carry out the deleteLater() calls a test scheduled (pytest-qt's own for the windows it closes, and the pages' for
    rebuilt rows), as the application's event loop does. Without a running loop they never happen, and the closed windows
    and rows pile up: every theme change re-polishes all of them (13,000 widgets in a full run, 20 s per set-up)."""
    yield
    from PySide6.QtCore import QEvent

    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture(autouse=True)
def private_user_dirs(monkeypatch, tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("GENTOO_INSTALLER_DEV", "1")
    # Qt reads the settings directory once per process: without this the experience mode, the draft and the
    # window geometry of one test would be restored by the next one.
    for format_ in (QSettings.Format.NativeFormat, QSettings.Format.IniFormat):
        QSettings.setPath(format_, QSettings.Scope.UserScope, str(home / ".config"))
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


def sample_info(**overrides):
    from gentoo_installer.system_info import DiskInfo, SystemInfo

    info = SystemInfo(
        architecture="x86_64", memory_gib=16, uefi=True,
        disks=[DiskInfo("/dev/sda", "Virtual disk", 64 * 1024**3, serial="test-disk")],
        network=True, synchronized_clock=True, missing_tools=[],
    )
    for name, value in overrides.items():
        setattr(info, name, value)
    return info


@pytest.fixture
def info():
    return sample_info()


@pytest.fixture
def make_window(qtbot, monkeypatch, tmp_path):
    """Build an InstallerWindow whose inspection is patched (synchronous by default)."""
    from gentoo_installer.app import InstallerWindow
    from gentoo_installer.manifest import InstallManifest

    def build(info=None, manifest=None, **kwargs):
        chosen = info if info is not None else sample_info()
        monkeypatch.setattr("gentoo_installer.app.inspect_system", lambda: chosen)
        # A test that leaves an "installation" running must not block at teardown on the close dialog.
        monkeypatch.setattr(InstallerWindow, "_confirm_close_during_install", lambda self: True)
        kwargs.setdefault("background_inspection", False)
        window = InstallerWindow(manifest if manifest is not None else InstallManifest(), str(tmp_path / "backend.sock"), **kwargs)
        qtbot.addWidget(window)
        window.show()
        return window

    return build
