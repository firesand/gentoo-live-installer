"""D6: SIGTERM/SIGINT cancel a running install (cleanup still runs), run the shutdown hooks and stop the server."""
from __future__ import annotations

import json
import os
import signal
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_installerd import server as server_module
from gentoo_installerd.events import Event
from gentoo_installerd.server import (
    ActionRegistry,
    InstallManager,
    ShutdownController,
    UnixServer,
    install_cancel_signals,
    install_signal_handlers,
    serve,
)

from tests.backend.helpers import manifest as make_manifest
from tests.backend.test_server_support import scripted, wait_for


def blocked_until_cancelled(cleanup_ran: threading.Event, started: threading.Event):
    """An installation that only ends when it is cancelled, and then 'cleans up' like Installer.run does."""

    def script(installer):
        installer.stage("packages")
        started.set()
        installer.cancel.wait(30)
        time.sleep(0.2)  # the cleanup takes a moment
        cleanup_ran.set()
        installer.emit(Event("error", job_id=installer.job_id, stage="packages", message="Installation cancelled", error="cancelled", data={"code": "install.cancelled"}))
        installer.emit(Event("completed", job_id=installer.job_id, message="Installation cancelled", data={"success": False}))
        return False

    return scripted(script)


class FakeServer:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.shutdowns = 0

    def shutdown(self) -> None:
        self.shutdowns += 1
        self.order.append("server.shutdown")


class ShutdownControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="gi-"))
        self.addCleanup(__import__("shutil").rmtree, self.directory, True)

    def manager(self) -> InstallManager:
        return InstallManager(self.directory / "t", self.directory / "s", self.directory / "c")

    def test_the_install_is_cancelled_and_cleaned_up_before_hooks_run_and_the_server_stops(self) -> None:
        cleanup_ran, started, order = threading.Event(), threading.Event(), []
        manager = self.manager()
        with patch("gentoo_installerd.server.Installer", blocked_until_cancelled(cleanup_ran, started)):
            manager.start(make_manifest())
            self.assertTrue(started.wait(5))
            registry = ActionRegistry()
            registry.add_shutdown_hook(lambda: order.append(f"hook(cleanup_ran={cleanup_ran.is_set()})"))
            server = FakeServer(order)
            controller = ShutdownController(server, manager, registry, grace=10)
            controller.request("SIGTERM")
            controller.request("SIGINT")  # a second signal must not start a second shutdown
            self.assertTrue(controller.finished.wait(10))
        self.assertEqual(order, ["hook(cleanup_ran=True)", "server.shutdown"])
        self.assertEqual(server.shutdowns, 1)
        self.assertFalse(manager.is_running())
        self.assertEqual(manager.status, "cancelled")

    def test_without_a_running_install_it_just_runs_the_hooks_and_stops(self) -> None:
        order: list[str] = []
        registry = ActionRegistry()
        registry.add_shutdown_hook(lambda: order.append("hook"))
        controller = ShutdownController(FakeServer(order), self.manager(), registry)
        controller.request("test")
        self.assertTrue(controller.finished.wait(5))
        self.assertEqual(order, ["hook", "server.shutdown"])

    def test_the_server_is_stopped_even_when_the_install_cannot_be_stopped_in_time(self) -> None:
        order: list[str] = []
        release = threading.Event()
        started = threading.Event()

        def script(installer):
            installer.stage("packages")
            started.set()
            release.wait(30)  # ignores the cancellation
            return False

        manager = self.manager()
        try:
            with patch("gentoo_installerd.server.Installer", scripted(script)), self.assertLogs("gentoo_installerd.server", level="ERROR"):
                manager.start(make_manifest())
                self.assertTrue(started.wait(5))
                controller = ShutdownController(FakeServer(order), manager, ActionRegistry(), grace=0.3)
                controller.request("test")
                self.assertTrue(controller.finished.wait(10))
        finally:
            release.set()
        self.assertEqual(order, ["server.shutdown"])

    def test_a_failing_hook_does_not_prevent_the_server_from_stopping(self) -> None:
        order: list[str] = []
        registry = ActionRegistry()
        registry.add_shutdown_hook(lambda: 1 / 0)
        controller = ShutdownController(FakeServer(order), self.manager(), registry)
        with self.assertLogs("gentoo_installerd.server", level="ERROR"):
            controller.request("test")
            self.assertTrue(controller.finished.wait(5))
        self.assertEqual(order, ["server.shutdown"])


class SignalHandlerTests(unittest.TestCase):
    def test_sigterm_and_sigint_request_a_shutdown_and_the_old_handlers_come_back(self) -> None:
        before = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
        requests: list[str] = []

        class Recorder:
            def request(self, reason: str) -> None:
                requests.append(reason)

        restore = install_signal_handlers(Recorder())  # type: ignore[arg-type]
        try:
            self.assertTrue(callable(signal.getsignal(signal.SIGTERM)) and signal.getsignal(signal.SIGTERM) not in before)
            signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGINT)
        finally:
            restore()
        self.assertEqual(requests, ["SIGTERM", "SIGINT"])
        self.assertEqual((signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)), before)

    def test_handlers_are_only_installed_on_the_main_thread(self) -> None:
        before = signal.getsignal(signal.SIGTERM)
        result: list[object] = []

        def other_thread() -> None:
            restore = install_signal_handlers(object())  # type: ignore[arg-type]
            result.append(signal.getsignal(signal.SIGTERM))
            restore()

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(5)
        self.assertEqual(result, [before])

    def test_cli_runs_are_cancelled_not_killed(self) -> None:
        before = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
        cancel = threading.Event()
        restore = install_cancel_signals(cancel)
        try:
            signal.raise_signal(signal.SIGTERM)
            self.assertTrue(cancel.is_set())
        finally:
            restore()
        self.assertEqual((signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)), before)


class ServeShutdownTests(unittest.TestCase):
    """The whole daemon in this process: a real serve(), a real SIGTERM."""

    def test_sigterm_during_an_install_cancels_it_cleans_up_and_serve_returns(self) -> None:
        directory = Path(tempfile.mkdtemp(prefix="gi-"))
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        path = directory / "run" / "s.sock"
        cleanup_ran, started = threading.Event(), threading.Event()
        servers: list[UnixServer] = []
        registry = ActionRegistry()
        hook_saw_cleanup: list[bool] = []
        registry.add_shutdown_hook(lambda: hook_saw_cleanup.append(cleanup_ran.is_set()))
        problems: list[str] = []
        before = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)

        class RecordingServer(UnixServer):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                servers.append(self)

        def driver() -> None:
            try:
                if not wait_for(lambda: path.exists() and registry.install_manager is not None and callable(signal.getsignal(signal.SIGTERM)) and signal.getsignal(signal.SIGTERM) != before[0]):
                    problems.append("the daemon did not come up or did not install its handler")
                    return
                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(str(path))
                    client.sendall((json.dumps({"version": 1, "action": "start", "manifest": make_manifest().to_dict()}) + "\n").encode())
                    if not started.wait(10):
                        problems.append("the install never started")
                        return
                    os.kill(os.getpid(), signal.SIGTERM)
                    client.settimeout(15)
                    stream = [json.loads(line) for line in client.makefile("rb")]
                    if [event["type"] for event in stream][-2:] != ["error", "completed"]:
                        problems.append(f"the client did not see the cancellation: {stream[-3:]}")
            finally:
                if problems and servers:
                    servers[0].shutdown()  # never leave the test hanging

        fake_group = type("Group", (), {"gr_gid": 4242})()
        thread = threading.Thread(target=driver, daemon=True)
        with patch("gentoo_installerd.server.Installer", blocked_until_cancelled(cleanup_ran, started)), patch("gentoo_installerd.server.UnixServer", RecordingServer), patch("gentoo_installerd.server.grp.getgrnam", return_value=fake_group), patch("gentoo_installerd.server.os.chown"):
            thread.start()
            serve(path, require_root=False, registry=registry, state_dir=directory / "state", cache_dir=directory / "cache", target_root=directory / "target")
        thread.join(10)
        self.assertEqual(problems, [])
        self.assertTrue(cleanup_ran.is_set(), "the cleanup of the cancelled installation did not run")
        self.assertEqual(hook_saw_cleanup, [True])
        self.assertFalse(path.exists())
        self.assertEqual((signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)), before)
        self.assertEqual(registry.install_manager.status, "cancelled")


if __name__ == "__main__":
    unittest.main()
