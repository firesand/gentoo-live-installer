"""D13: only one installation per state directory, whether started by the daemon or on the command line."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from gentoo_installerd.events import Event
from gentoo_installerd.install import InstallError, Installer
from gentoo_installerd.storage import StorageManager

from tests.backend.helpers import FakeInspector, FakeRunner, manifest


class RunLockTests(unittest.TestCase):
    def installer(self, temporary: str, events: list[Event], **kwargs) -> Installer:
        return Installer(manifest(), runner=kwargs.pop("runner", FakeRunner()), target_root=Path(temporary, "target"), state_dir=Path(temporary, "state"), inspector=FakeInspector(), emit=events.append, **kwargs)

    def test_a_second_run_on_one_state_directory_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first_events: list[Event] = []
            second_events: list[Event] = []
            holding, release = threading.Event(), threading.Event()
            first = self.installer(temporary, first_events)

            def blocked_preflight() -> None:
                holding.set()
                self.assertTrue(release.wait(10))
                raise InstallError("stop after the lock was proven")

            first._preflight = blocked_preflight  # type: ignore[method-assign]
            thread = threading.Thread(target=first.run)
            thread.start()
            try:
                self.assertTrue(holding.wait(10))
                runner = FakeRunner()
                second = self.installer(temporary, second_events, runner=runner)
                second.storage.owned_mounts = [Path(temporary, "target")]
                self.assertFalse(second.run())
            finally:
                release.set()
                thread.join(10)
            errors = [event for event in second_events if event.type == "error"]
            self.assertEqual(len(errors), 1)
            self.assertIn("Another installation is already running", errors[0].error)
            self.assertEqual(errors[0].data["code"], "install.busy")
            self.assertEqual([event.data["success"] for event in second_events if event.type == "completed"], [False])
            # The refused run must not touch what the running one owns.
            self.assertEqual(runner.commands, [])
            second._log_file.close()
            first._log_file.close()

    def test_the_lock_is_released_when_the_run_ends(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events: list[Event] = []
            first = self.installer(temporary, events)
            first._preflight = lambda: (_ for _ in ()).throw(InstallError("fails at once"))  # type: ignore[method-assign]
            self.assertFalse(first.run())
            second = self.installer(temporary, [])
            second._preflight = lambda: (_ for _ in ()).throw(InstallError("also fails"))  # type: ignore[method-assign]
            events_second: list[Event] = []
            second._event_sink = events_second.append
            self.assertFalse(second.run())
            self.assertIn("also fails", next(e for e in events_second if e.type == "error").error)

    def test_dry_runs_take_no_lock_and_create_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary, "state")
            installer = Installer(manifest(), runner=FakeRunner(), target_root=Path(temporary, "t"), state_dir=state, dry_run=True, inspector=FakeInspector())
            installer.run()
            self.assertFalse(state.exists())

    def test_the_lock_file_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer = self.installer(temporary, [])
            installer._preflight = lambda: (_ for _ in ()).throw(InstallError("x"))  # type: ignore[method-assign]
            installer.run()
            self.assertEqual(Path(temporary, "state/install.lock").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
