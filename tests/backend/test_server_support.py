"""Helpers shared by the test_server_* files: a real daemon on a temporary socket and scripted installers."""
from __future__ import annotations

import contextlib
import json
import socket
import tempfile
import threading
import unittest
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import patch

from gentoo_installerd.events import Event
from gentoo_installerd.server import ActionRegistry, InstallManager, UnixServer

from tests.backend.helpers import manifest as make_manifest


class ScriptedInstaller:
    """Stands in for Installer: ``run()`` executes a script that emits events and decides the result."""

    script: Callable[[ScriptedInstaller], bool]
    instances: list[ScriptedInstaller]

    def __init__(self, manifest, **kwargs) -> None:
        self.emit = kwargs["emit"]
        self.cancel = kwargs["cancel"]
        self.resume = kwargs.get("resume", False)
        self.kwargs = kwargs
        self.job_id = uuid.uuid4().hex
        self.secrets: tuple[str, ...] = ()
        type(self).instances.append(self)

    def run(self) -> bool:
        return type(self).script(self)

    def stage(self, name: str, progress: float = 0.0, status: str = "running") -> None:
        self.emit(Event("stage", job_id=self.job_id, stage=name, message=f"{status} {name}", progress=progress, data={"status": status}))

    def log(self, text: str) -> None:
        self.emit(Event("log", job_id=self.job_id, message=text))


def scripted(script: Callable[[ScriptedInstaller], bool]) -> type[ScriptedInstaller]:
    return type("Scripted", (ScriptedInstaller,), {"script": staticmethod(script), "instances": []})


def finishing(installer: ScriptedInstaller, success: bool = True) -> bool:
    installer.emit(Event("completed", job_id=installer.job_id, message="done", data={"success": success}))
    return success


class DaemonCase(unittest.TestCase):
    """A real UnixServer with its own registry and manager on a temporary socket."""

    registry: ActionRegistry
    manager: InstallManager
    server: UnixServer
    path: str
    directory: Path

    def start_daemon(self, **manager_kwargs) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="gi-"))
        self.addCleanup(self._remove_directory)
        self.path = str(self.directory / "s.sock")
        self.registry = ActionRegistry()
        self.manager = InstallManager(self.directory / "target", self.directory / "state", self.directory / "cache", **manager_kwargs)
        self.server = UnixServer(self.path, self.manager, self.registry)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self._stop_daemon, thread)

    def _stop_daemon(self, thread: threading.Thread) -> None:
        self.manager.shutdown(5)
        self.server.shutdown()
        self.server.server_close()
        thread.join(5)

    def _remove_directory(self) -> None:
        import shutil

        shutil.rmtree(self.directory, ignore_errors=True)

    def use_installer(self, installer_class: type) -> None:
        patcher = patch("gentoo_installerd.server.Installer", installer_class)
        patcher.start()
        self.addCleanup(patcher.stop)

    @contextlib.contextmanager
    def connect(self, timeout: float = 10.0) -> Iterator[socket.socket]:
        client = socket.socket(socket.AF_UNIX)
        client.settimeout(timeout)
        client.connect(self.path)
        try:
            yield client
        finally:
            client.close()

    def call(self, request: dict, *, timeout: float = 10.0, raw: bytes | None = None) -> list[dict]:
        """Send one request and read events until the daemon closes the connection."""
        with self.connect(timeout) as client:
            client.sendall(raw if raw is not None else (json.dumps({"version": 1, **request}) + "\n").encode())
            return read_events(client)

    def start_request(self) -> dict:
        return {"action": "start", "manifest": make_manifest().to_dict()}


def read_events(client: socket.socket) -> list[dict]:
    reader = client.makefile("rb")
    events = []
    for line in reader:
        events.append(json.loads(line))
    return events


def types(events: list[dict]) -> list[str]:
    return [event["type"] for event in events]


def wait_for(predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.01) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class SupportSelfTest(unittest.TestCase):
    def test_a_scripted_installer_records_its_instances(self) -> None:
        installer_class = scripted(lambda installer: finishing(installer))
        events: list[Event] = []
        instance = installer_class(make_manifest(), emit=events.append, cancel=threading.Event())
        self.assertTrue(instance.run())
        self.assertEqual(installer_class.instances, [instance])
        self.assertEqual([event.type for event in events], ["completed"])
