"""The real Installer behind the real socket server: what a GUI client actually receives."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_installerd import install as install_module
from gentoo_installerd.install import STAGES, Installer
from gentoo_installerd.runner import DryRunRunner

from tests.backend.helpers import FakeInspector, FakeRunner
from tests.backend.helpers import manifest as make_manifest
from tests.backend.test_server_support import DaemonCase, types, wait_for


def dry_run_installer(manifest, **kwargs):
    kwargs.pop("resume", None)
    return Installer(manifest, runner=DryRunRunner(), dry_run=True, inspector=FakeInspector(), **kwargs)


class RealInstallerOverTheSocketTests(DaemonCase):
    def setUp(self) -> None:
        self.start_daemon()
        patcher = patch("gentoo_installerd.server.Installer", dry_run_installer)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_full_dry_run_streams_every_stage_with_monotonic_progress_and_ends_with_completed(self) -> None:
        events = self.call(self.start_request())
        self.assertEqual(types(events)[0], "status")
        self.assertEqual(types(events)[-1], "completed")
        self.assertTrue(events[-1]["data"]["success"])
        self.assertEqual(events[-1]["progress"], 100.0)
        completed_stages = [event["stage"] for event in events if event["type"] == "stage" and event["data"]["status"] == "completed"]
        self.assertEqual(completed_stages, list(STAGES))
        progress = [event["progress"] for event in events if "progress" in event]
        self.assertEqual(progress, sorted(progress))
        self.assertTrue(all(0 <= value <= 100 for value in progress))
        seqs = [event["seq"] for event in events if "seq" in event]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        self.assertTrue(all(event["job_id"] == events[0]["job_id"] for event in events))

    def test_status_and_attach_agree_with_what_was_streamed(self) -> None:
        streamed = self.call(self.start_request())
        self.assertTrue(wait_for(lambda: self.manager.status == "completed"))
        status = self.call({"action": "status"})[0]["data"]
        self.assertEqual((status["status"], status["stage"], status["progress"], status["error"]), ("completed", "cleanup", 100.0, ""))
        self.assertEqual(status["seq"], streamed[-1]["seq"])
        attached = self.call({"action": "attach", "after_seq": 0})
        self.assertEqual([event["seq"] for event in attached], [event["seq"] for event in streamed if "seq" in event])
        again = self.call({"action": "attach", "after_seq": streamed[-1]["seq"] - 1})
        self.assertEqual(types(again), ["completed"])

    def test_no_secret_reaches_the_client(self) -> None:
        events = self.call(self.start_request())
        text = json.dumps(events)
        self.assertNotIn("user-password", text)
        self.assertNotIn("root-password", text)

    def test_a_failing_install_reports_stage_code_and_retryable_to_the_client(self) -> None:
        def failing(manifest, **kwargs):
            installer = dry_run_installer(manifest, **kwargs)
            installer._kernel = lambda: (_ for _ in ()).throw(install_module.InstallError("dracut exploded"))  # type: ignore[method-assign]
            return installer

        with patch("gentoo_installerd.server.Installer", failing):
            events = self.call(self.start_request())
        error = next(event for event in events if event["type"] == "error" and event["stage"] == "kernel")
        self.assertEqual((error["data"]["code"], error["data"]["retryable"]), ("install.failed", True))
        self.assertIn("dracut exploded", error["error"])
        self.assertEqual(types(events)[-1], "completed")
        self.assertFalse(events[-1]["data"]["success"])
        self.assertTrue(wait_for(lambda: self.manager.status == "failed"))
        status = self.call({"action": "status"})[0]["data"]
        self.assertIn("dracut exploded", status["error"])
        self.assertEqual(status["error_stage"], "kernel")

    def test_a_manual_kernel_stop_is_resumable_information_for_the_client(self) -> None:
        request = self.start_request()
        request["manifest"]["system"]["kernel"] = "manual"
        events = self.call(request)
        error = next(event for event in events if event["type"] == "error")
        self.assertEqual(error["stage"], "kernel")
        self.assertIn("chroot", error["error"])


class InstallLogRoundTripTests(DaemonCase):
    def test_the_log_action_reads_what_the_installer_wrote(self) -> None:
        self.start_daemon()
        state = self.directory / "state"
        installer = Installer(make_manifest(), runner=FakeRunner(), target_root=self.directory / "target", state_dir=state, inspector=FakeInspector())
        installer._stage = "base"
        installer.log("first line")
        installer.emit("stage", "base", "Completed base", status="completed")
        installer.emit("error", message="boom", error="boom", code="install.failed")
        installer._log_file.close()
        events = self.call({"action": "log"})
        lines = events[0]["data"]["lines"]
        self.assertEqual(len(lines), 4)
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\dT[\d:.]+Z created +Install job created$")
        self.assertRegex(lines[1], r" log +\[base\] first line$")
        self.assertRegex(lines[2], r" stage +\[base\] Completed base$")
        self.assertRegex(lines[3], r" error +\[base\] boom \[install.failed\]$")
        raw = self.call({"action": "log", "format": "jsonl", "tail": 1})[0]["data"]["lines"]
        self.assertEqual(json.loads(raw[0])["code"], "install.failed")


class HotPathTests(unittest.TestCase):
    def test_a_realistic_amount_of_emerge_output_goes_through_the_logger_quickly(self) -> None:
        import time

        with tempfile.TemporaryDirectory() as temporary:
            events: list = []
            installer = Installer(make_manifest(), runner=FakeRunner(), target_root=Path(temporary, "t"), state_dir=Path(temporary, "s"), inspector=FakeInspector(), emit=lambda event: events.append(event) if len(events) < 10 else None)
            installer._stage = "desktop"
            started = time.monotonic()
            for index in range(30000):
                installer.log(f">>> /usr/lib64/libfoo{index}.so.1")
            elapsed = time.monotonic() - started
            installer._log_file.close()
            size = Path(temporary, "s/install.log").stat().st_size
        self.assertLess(elapsed, 10, "30k log lines took too long")
        self.assertGreater(size, 30000 * 80)


if __name__ == "__main__":
    unittest.main()
