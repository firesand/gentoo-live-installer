"""The real SubprocessRunner inside a real (non-dry-run) Installer: output, progress and cancellation end to end."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from gentoo_installerd.events import Event
from gentoo_installerd.install import Installer

from tests.backend.helpers import FakeInspector, manifest


def build(temporary: str, events: list[Event], **kwargs) -> Installer:
    return Installer(manifest(), target_root=Path(temporary, "target"), state_dir=Path(temporary, "state"), inspector=FakeInspector(), emit=events.append, **kwargs)


class RunnerThroughInstallerTests(unittest.TestCase):
    def test_command_output_is_logged_redacted_and_drives_package_progress(self) -> None:
        events: list[Event] = []
        with tempfile.TemporaryDirectory() as temporary:
            installer = build(temporary, events)
            installer._stage = "desktop"
            script = "print('>>> Emerging (2 of 4) kde-plasma/plasma-meta-6.4::gentoo'); print('the password is user-password!')"
            installer.runner.run([sys.executable, "-c", script])
            installer._log_file.close()
            lines = [json.loads(line) for line in Path(temporary, "state/install.log").read_text().splitlines()]
        logs = [event for event in events if event.type == "log"]
        self.assertTrue(any(event.message.startswith("$ ") for event in logs))
        self.assertTrue(any(event.message == ">>> Emerging (2 of 4) kde-plasma/plasma-meta-6.4::gentoo" for event in logs))
        self.assertFalse(any("user-password" in event.message for event in logs))
        progress = [event for event in events if event.type == "progress"]
        self.assertEqual([(event.data["package"], event.data["packages"], event.data["stage_progress"]) for event in progress], [(2, 4, 25.0)])
        self.assertEqual(progress[0].stage, "desktop")
        self.assertTrue(any("password" in line.get("message", "") and "<redacted>" in line["message"] for line in lines))
        self.assertNotIn("user-password", json.dumps(lines))

    def test_cancelling_during_a_command_ends_the_command_runs_cleanup_and_reports_cancelled(self) -> None:
        events: list[Event] = []
        cancel = threading.Event()
        with tempfile.TemporaryDirectory() as temporary:
            installer = build(temporary, events, cancel=cancel)
            ready = threading.Event()
            original = installer._event_sink

            def sink(event: Event) -> None:
                original(event)
                if event.type == "log" and event.message == "ready":
                    ready.set()

            installer._event_sink = sink
            installer._preflight = lambda: installer.runner.run([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"])  # type: ignore[method-assign]
            threading.Thread(target=lambda: (ready.wait(10), cancel.set()), daemon=True).start()
            started = time.monotonic()
            self.assertFalse(installer.run())
            elapsed = time.monotonic() - started
            lines = [json.loads(line) for line in Path(temporary, "state/install.log").read_text().splitlines()]
        self.assertLess(elapsed, 15)
        error = next(event for event in events if event.type == "error")
        self.assertEqual((error.stage, error.error, error.data["code"]), ("preflight", "cancelled", "install.cancelled"))
        self.assertEqual([event.data["success"] for event in events if event.type == "completed"], [False])
        self.assertTrue(any(event.stage == "cleanup" and event.data.get("status") == "completed" for event in events if event.type == "stage"))
        self.assertEqual([line["type"] for line in lines if line["type"] in {"error", "completed"}], ["error", "completed"])


if __name__ == "__main__":
    unittest.main()
