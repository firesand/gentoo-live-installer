from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_installerd.events import Event, PROTOCOL_VERSION
from gentoo_installerd.server import InstallManager, UnixServer, main, serve

from tests.backend.helpers import FakeInspector
from tests.backend.helpers import manifest as make_manifest


class ServerProtocolTests(unittest.TestCase):
    def test_status_action_and_version_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary, "server.sock"))
            manager = InstallManager(Path(temporary, "target"), Path(temporary, "state"), Path(temporary, "cache"))
            server = UnixServer(path, manager)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(path)
                    client.sendall((json.dumps({"version": PROTOCOL_VERSION, "action": "status"}) + "\n").encode())
                    response = json.loads(client.makefile().readline())
                    self.assertEqual(response["type"], "status")
                    self.assertEqual(response["data"]["status"], "idle")
                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(path)
                    client.sendall(b'{"version":999,"action":"status"}\n')
                    response = json.loads(client.makefile().readline())
                    self.assertEqual(response["type"], "error")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_cancel_without_job_returns_error(self) -> None:
        manager = InstallManager(Path("/tmp/target"), Path("/tmp/state"), Path("/tmp/cache"))
        with self.assertRaises(RuntimeError):
            manager.request_cancel()

    def test_start_streams_engine_events(self) -> None:
        class FakeInstaller:
            def __init__(self, manifest, **kwargs):
                self.job_id = "job"
                self.emit = kwargs["emit"]

            def run(self):
                self.emit(Event("stage", stage="preflight", message="checked"))
                self.emit(Event("completed", data={"success": True}))
                return True

        with tempfile.TemporaryDirectory() as temporary, patch("gentoo_installerd.server.Installer", FakeInstaller):
            manager = InstallManager(Path(temporary, "target"), Path(temporary, "state"), Path(temporary, "cache"))
            listener = manager.start(make_manifest())
            events = []
            while (event := listener.get(timeout=2)) is not None:
                events.append(event)
            self.assertEqual([event.type for event in events], ["stage", "completed"])
            self.assertEqual(manager.snapshot()["status"], "completed")

    def test_socket_and_parent_permissions_are_restricted(self) -> None:
        class FakeUnixServer:
            socket_mode = 0

            def __init__(self, path, manager):
                self.socket = socket.socket(socket.AF_UNIX)
                self.socket.bind(path)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.socket.close()

            def serve_forever(self, poll_interval):
                FakeUnixServer.socket_mode = os.stat(self.socket.getsockname()).st_mode & 0o777

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "run/server.sock")
            fake_group = type("Group", (), {"gr_gid": 4242})()
            with patch("gentoo_installerd.server.UnixServer", FakeUnixServer), patch("gentoo_installerd.server.grp.getgrnam", return_value=fake_group), patch("gentoo_installerd.server.os.chown"), patch("os.geteuid", return_value=0):
                serve(path)
            self.assertEqual(FakeUnixServer.socket_mode, 0o660)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o750)
            self.assertFalse(path.exists())


class CliTests(unittest.TestCase):
    def test_dry_run_cli_uses_same_engine_without_root_or_target_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manifest_path = base / "manifest.json"
            manifest_path.write_text(make_manifest().to_json())
            target, state = base / "target", base / "state"
            output = io.StringIO()
            from gentoo_installerd import server as server_module
            real_installer = server_module.Installer
            def factory(*args, **kwargs):
                kwargs["inspector"] = FakeInspector()
                return real_installer(*args, **kwargs)
            with patch.object(server_module, "Installer", factory), patch("os.geteuid", return_value=1000), contextlib.redirect_stdout(output):
                result = main(["--manifest", str(manifest_path), "--dry-run", "--target-root", str(target), "--state-dir", str(state), "--cache-dir", str(base / "cache")])
            self.assertEqual(result, 0)
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertTrue(events[-1]["data"]["success"])
            self.assertFalse(target.exists())
            self.assertFalse(state.exists())

    def test_cli_requires_explicit_yes_for_real_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "manifest.json")
            path.write_text(make_manifest().to_json())
            with patch("os.geteuid", return_value=0), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--manifest", str(path)]), 2)

    def test_real_mode_requires_root(self) -> None:
        with patch("os.geteuid", return_value=1000), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([]), 1)


if __name__ == "__main__":
    unittest.main()
