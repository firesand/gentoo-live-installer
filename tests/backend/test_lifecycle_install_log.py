"""D5 (a failing install log never aborts the install) and D8 (structured, timestamped, copied into the target)."""
from __future__ import annotations

import errno
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_installerd import install as install_module
from gentoo_installerd.events import Event
from gentoo_installerd.install import InstallError, Installer
from gentoo_installerd.runner import DryRunRunner
from gentoo_installerd.storage import StorageManager

from tests.backend.helpers import FakeInspector, FakeRunner, manifest


class FullLog:
    """A log file whose disk is full."""

    closed = False

    def __init__(self) -> None:
        self.attempts = 0

    def write(self, text: str) -> int:
        self.attempts += 1
        raise OSError(errno.ENOSPC, "No space left on device")

    def flush(self) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    def close(self) -> None:
        self.closed = True


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class LogWriteFailureTests(unittest.TestCase):
    def test_a_full_disk_never_aborts_the_install_and_cleanup_still_runs(self) -> None:
        events: list[Event] = []
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as temporary:
            storage = StorageManager(runner)
            storage.owned_mounts = [Path("/fake-target")]
            installer = Installer(manifest(), runner=runner, storage=storage, target_root=Path(temporary, "target"), state_dir=Path(temporary, "state"), dry_run=True, inspector=FakeInspector(), emit=events.append)
            full = installer._log_file = FullLog()  # type: ignore[assignment]
            self.assertTrue(installer.run())
        self.assertGreater(full.attempts, 1, "the writer must keep being tried (the disk may free up)")
        self.assertIn(("umount", "-R", "/fake-target"), runner.commands)
        warnings = [event for event in events if event.data.get("warning") == "install_log_unwritable"]
        self.assertEqual(len(warnings), 1, "exactly one warning, not one per line")
        self.assertIn("No space left on device", warnings[0].message)
        completed = [event for event in events if event.type == "completed"]
        self.assertEqual([event.data["success"] for event in completed], [True])

    def test_a_failure_is_still_reported_truthfully_when_the_log_is_broken(self) -> None:
        events: list[Event] = []
        runner = FakeRunner()
        runner.failures["chroot"] = 1
        with tempfile.TemporaryDirectory() as temporary:
            installer = Installer(manifest(), runner=runner, target_root=Path(temporary, "target"), state_dir=Path(temporary, "state"), dry_run=True, inspector=FakeInspector(), emit=events.append)
            installer._log_file = FullLog()  # type: ignore[assignment]
            self.assertFalse(installer.run())
        errors = [event for event in events if event.type == "error"]
        self.assertTrue(errors and "Command failed" in errors[0].message)
        self.assertEqual([event.data["success"] for event in events if event.type == "completed"], [False])

    def test_the_log_resumes_after_the_disk_recovers_and_says_what_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer = Installer(manifest(), runner=FakeRunner(), target_root=Path(temporary, "target"), state_dir=Path(temporary, "state"), inspector=FakeInspector())
            real = installer._log_file
            installer._log_file = FullLog()  # type: ignore[assignment]
            installer.log("lost one")
            installer.log("lost two")
            installer._log_file = real
            installer.log("after recovery")
            real.close()
            text = (Path(temporary, "state") / "install.log").read_text()
        lines = [json.loads(line) for line in text.splitlines() if line.strip()]
        self.assertTrue(any("2 install log records could not be written" in line.get("message", "") for line in lines))
        self.assertEqual(lines[-1]["message"], "after recovery")
        self.assertNotIn("lost one", text)

    def test_a_log_that_cannot_even_be_opened_does_not_stop_the_installer_being_built(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(install_module, "_LogFile", side_effect=OSError(errno.EROFS, "Read-only file system")):
                installer = Installer(manifest(), runner=FakeRunner(), target_root=Path(temporary, "target"), state_dir=Path(temporary, "state"), inspector=FakeInspector())
            installer.log("still fine")  # must not raise
            self.assertIsNone(installer._log_file)

    def test_a_failing_event_sink_does_not_stop_the_install(self) -> None:
        def broken(_event: Event) -> None:
            raise RuntimeError("sink bug")

        with tempfile.TemporaryDirectory() as temporary:
            installer = Installer(manifest(), runner=DryRunRunner(), target_root=Path(temporary, "t"), state_dir=Path(temporary, "s"), dry_run=True, inspector=FakeInspector(), emit=broken)
            self.assertTrue(installer.run())

    def test_a_checkpoint_that_cannot_be_saved_only_costs_the_resume(self) -> None:
        events: list[Event] = []
        with tempfile.TemporaryDirectory() as temporary:
            installer = Installer(manifest(), runner=FakeRunner(), target_root=Path(temporary, "target"), state_dir=Path(temporary, "state"), inspector=FakeInspector(), emit=events.append)
            with patch.object(installer.checkpoints, "complete", side_effect=OSError(errno.ENOSPC, "No space left on device")):
                installer._save_checkpoint("stage3")
        self.assertTrue(any("could not save the checkpoint for stage3" in event.message for event in events))


class StructuredLogTests(unittest.TestCase):
    def build(self, temporary: str, **kwargs) -> tuple[Installer, list[Event], Path]:
        events: list[Event] = []
        state = Path(temporary, "state")
        installer = Installer(kwargs.pop("manifest", manifest()), runner=kwargs.pop("runner", FakeRunner()), target_root=Path(temporary, "target"), state_dir=state, inspector=FakeInspector(), emit=events.append, **kwargs)
        return installer, events, state / "install.log"

    def test_every_record_has_a_timestamp_a_type_and_the_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer, _events, path = self.build(temporary)
            installer._stage = "kernel"
            installer.log("emerge says hello")
            installer.emit("stage", "kernel", "Starting kernel", status="running")
            installer.emit("error", message="boom", error="boom", code="install.failed", retryable=True)
            installer._log_file.close()
            lines = records(path)
        self.assertEqual([line["type"] for line in lines], ["created", "log", "stage", "error"])
        for line in lines:
            self.assertRegex(line["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
            self.assertEqual(line["job_id"], installer.job_id)
        self.assertEqual(lines[1]["message"], "emerge says hello")
        self.assertEqual(lines[1]["stage"], "kernel")
        self.assertEqual(lines[2]["status"], "running")
        self.assertEqual((lines[3]["code"], lines[3]["error"], lines[3]["retryable"]), ("install.failed", "boom", True))

    def test_the_created_record_has_no_secrets_but_identifies_the_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer, _events, path = self.build(temporary)
            installer._log_file.close()
            text = path.read_text()
            mode = path.stat().st_mode & 0o777
        created = json.loads(text.splitlines()[0])
        self.assertEqual(created["type"], "created")
        self.assertRegex(created["manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(created["pid"], os.getpid())
        self.assertNotIn("user-password", text)
        self.assertEqual(mode, 0o600)

    def test_a_failed_run_records_stage_error_cleanup_and_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer, _events, path = self.build(temporary)
            with patch.object(installer, "_preflight"), patch.object(installer, "_storage"), patch.object(installer, "_stage3", side_effect=InstallError("download failed")):
                self.assertFalse(installer.run())
            lines = records(path)
        types = [(line["type"], line.get("stage", "")) for line in lines]
        self.assertIn(("stage", "storage"), types)
        self.assertIn(("error", "stage3"), types)
        self.assertIn(("stage", "cleanup"), types)
        self.assertEqual(types[-1], ("completed", ""))
        error = next(line for line in lines if line["type"] == "error")
        self.assertEqual(error["code"], "install.failed")
        self.assertIn("download failed", error["error"])
        self.assertIs(lines[-1]["success"], False)
        self.assertTrue(all(re.match(r"^\d{4}-", line["ts"]) for line in lines))

    def test_a_cancelled_run_is_recorded_as_cancelled(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with tempfile.TemporaryDirectory() as temporary:
            installer, events, path = self.build(temporary, cancel=cancel)
            self.assertFalse(installer.run())
            lines = records(path)
        error = next(line for line in lines if line["type"] == "error")
        self.assertEqual((error["code"], error["error"]), ("install.cancelled", "cancelled"))
        self.assertEqual(lines[-1]["type"], "completed")
        completed = [event for event in events if event.type == "completed"]
        self.assertEqual(completed[0].message, "Installation cancelled")

    def test_secrets_never_reach_the_log_or_the_events(self) -> None:
        m = manifest()
        m.storage.luks = True
        m.storage.luks_passphrase = "luks-passphrase-value"
        m.network.wifi_ssid = "net"
        m.network.wifi_password = "wifi-password-value"
        m.network.proxy = "http://proxyuser:proxy-pass-value@proxy.lan:3128"
        with tempfile.TemporaryDirectory() as temporary:
            installer, events, path = self.build(temporary, manifest=m)
            for secret in ("user-password", "root-password", "luks-passphrase-value", "wifi-password-value", "proxy-pass-value"):
                installer.log(f"echo {secret} leaked")
                installer.emit("error", message=f"failed with {secret}", error=f"failed with {secret}")
            installer._log_file.close()
            persisted = path.read_text() + (path.parent / "manifest.json").read_text()
        rendered = persisted + "\n".join(event.to_json() for event in events)
        for secret in ("user-password", "root-password", "luks-passphrase-value", "wifi-password-value", "proxy-pass-value"):
            self.assertNotIn(secret, rendered)
        self.assertIn("proxy.lan:3128", persisted)


class LogCopyIntoTargetTests(unittest.TestCase):
    def prepared(self, temporary: str, *, mounted: bool = True, release: bool = True):
        events: list[Event] = []
        runner = FakeRunner()
        target = Path(temporary, "target")
        (target / "etc").mkdir(parents=True)
        if release:
            (target / "etc/gentoo-release").write_text("Gentoo Base System release 2.18\n")
        installer = Installer(manifest(), runner=runner, target_root=target, state_dir=Path(temporary, "state"), inspector=FakeInspector(), emit=events.append)
        patcher = patch.object(installer, "_target_holds_system", return_value=mounted and release)
        patcher.start()
        self.addCleanup(patcher.stop)
        return installer, events, target

    def test_the_redacted_log_is_copied_into_the_new_system_before_unmounting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer, _events, target = self.prepared(temporary)
            installer.log("some output with user-password inside")
            installer._outcome = "completed"
            installer._cleanup()
            installer._log_file.close()
            copy = target / "var/log/gentoo-installer/install.log"
            self.assertTrue(copy.is_file())
            self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
            self.assertEqual((target / "var/log/gentoo-installer").stat().st_mode & 0o777, 0o700)
            lines = records(copy)
            manifest_copy = (target / "var/log/gentoo-installer/manifest.json").read_text()
        self.assertEqual(lines[0]["type"], "created")
        self.assertEqual(lines[-1]["type"], "copy")
        self.assertIn("job outcome: completed", lines[-1]["message"])
        self.assertNotIn("user-password", copy.read_text() if copy.exists() else "")
        self.assertNotIn("user-password", manifest_copy)
        self.assertIn("<redacted>", manifest_copy)

    def test_the_copy_happens_before_the_unmount(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer, _events, target = self.prepared(temporary)
            seen_at_unmount: list[bool] = []
            installer.storage.owned_mounts = [target]
            original = installer.runner.run

            def run(argv, **kwargs):
                if argv[0] == "umount":
                    seen_at_unmount.append((target / "var/log/gentoo-installer/install.log").is_file())
                return original(argv, **kwargs)

            installer.runner.run = run  # type: ignore[method-assign]
            installer._cleanup()
            installer._log_file.close()
        self.assertEqual(seen_at_unmount, [True])

    def test_a_failed_install_leaves_its_log_in_the_target_too(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer, _events, target = self.prepared(temporary)
            with patch.object(installer, "_preflight", side_effect=InstallError("nope")):
                self.assertFalse(installer.run())
            lines = records(target / "var/log/gentoo-installer/install.log")
        self.assertTrue(any(line["type"] == "error" for line in lines))
        self.assertIn("job outcome: failed", lines[-1]["message"])

    def test_nothing_is_copied_when_there_is_no_system_in_the_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer, _events, target = self.prepared(temporary, release=False)
            installer._cleanup()
            installer._log_file.close()
            self.assertFalse((target / "var").exists())

    def test_a_copy_that_fails_is_logged_and_never_blocks_the_unmount(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer, _events, target = self.prepared(temporary)
            (target / "var").write_text("a file where a directory should be")
            installer.storage.owned_mounts = [target]
            installer._cleanup()
            installer._log_file.close()
            lines = records(installer.log_path)
        self.assertIn(("umount", "-R", str(target)), installer.runner.commands)
        self.assertTrue(any("Could not copy the install log" in line.get("message", "") for line in lines))

    def test_files_in_the_target_are_chowned_to_root_when_running_as_root(self) -> None:
        chowned: list[tuple[str, int, int]] = []
        with tempfile.TemporaryDirectory() as temporary:
            installer, _events, target = self.prepared(temporary)
            with patch.object(install_module.os, "geteuid", return_value=0), patch.object(install_module.os, "chown", lambda path, uid, gid: chowned.append((str(path), uid, gid))):
                installer._cleanup()
            installer._log_file.close()
        paths = {Path(path).name for path, uid, gid in chowned if (uid, gid) == (0, 0)}
        self.assertTrue({"gentoo-installer", "install.log.tmp", "manifest.json.tmp"} <= paths, chowned)

    def test_dry_runs_never_touch_the_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "target")
            installer = Installer(manifest(), runner=DryRunRunner(), target_root=target, state_dir=Path(temporary, "state"), dry_run=True, inspector=FakeInspector())
            installer._cleanup()
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
