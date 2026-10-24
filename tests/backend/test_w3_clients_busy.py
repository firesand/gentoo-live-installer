"""Wave 3 review fixes: a start that the repair module refused says so in the error it sends (BUSY-1)."""

from __future__ import annotations

import json
import threading

from gentoo_installerd.server import InstallBusy

from tests.backend.test_server_support import DaemonCase, finishing, scripted, types, wait_for


class RefusedStartTests(DaemonCase):
    def setUp(self) -> None:
        self.start_daemon()
        self.use_installer(scripted(lambda installer: finishing(installer)))

    def test_a_start_refused_by_the_busy_check_is_marked_as_a_refusal(self) -> None:
        self.manager.busy_check = lambda: "A repair session holds /dev/sda3; unmount it first"
        events = self.call(self.start_request())
        self.assertEqual(types(events), ["error", "completed"])
        self.assertEqual(events[0]["data"]["code"], "install.busy")  # the code stays, so older clients behave as before
        self.assertIs(events[0]["data"]["blocked"], True)
        self.assertIn("/dev/sda3", events[0]["error"])

    def test_so_is_a_busy_check_that_crashes(self) -> None:
        self.manager.busy_check = lambda: 1 / 0
        with self.assertLogs("gentoo_installerd.server", level="ERROR"):
            events = self.call(self.start_request())
        self.assertEqual((events[0]["data"]["code"], events[0]["data"]["blocked"]), ("install.busy", True))

    def test_a_second_start_while_a_job_really_runs_is_not_marked(self) -> None:
        release = threading.Event()
        self.use_installer(scripted(lambda installer: (release.wait(10), finishing(installer))[1]))
        first = self.connect()
        client = first.__enter__()
        try:
            client.sendall((json.dumps({"version": 1, **self.start_request()}) + "\n").encode())
            self.assertTrue(wait_for(lambda: self.manager.is_running()))
            events = self.call(self.start_request())
            self.assertEqual(events[0]["data"]["code"], "install.busy")
            self.assertNotIn("blocked", events[0]["data"])
        finally:
            release.set()
            first.__exit__(None, None, None)

    def test_the_exception_carries_the_marker_for_other_callers(self) -> None:
        self.manager.busy_check = lambda: "reason"
        with self.assertRaises(InstallBusy) as caught:
            self.manager.start(__import__("tests.backend.helpers", fromlist=["manifest"]).manifest())
        self.assertEqual(caught.exception.data, {"blocked": True})
