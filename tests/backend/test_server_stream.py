"""D3/D4/D5/NOBS-11: the streaming path survives slow, stalled and vanishing clients and failing jobs."""
from __future__ import annotations

import errno
import json
import logging
import queue
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gentoo_installerd import server as server_module
from gentoo_installerd.events import Event
from gentoo_installerd.server import InstallBusy, InstallManager, InstallNotRunning, Listener

from tests.backend.test_server_support import DaemonCase, finishing, read_events, scripted, types, wait_for


class ListenerTests(unittest.TestCase):
    def test_events_come_out_in_order_and_none_ends_the_stream(self) -> None:
        listener = Listener()
        for index in range(3):
            listener.offer(Event("log", message=str(index)))
        listener.finish()
        self.assertEqual([listener.get().message for _ in range(3)], ["0", "1", "2"])
        self.assertIsNone(listener.get())
        self.assertIsNone(listener.get(timeout=0))

    def test_get_times_out_with_queue_empty_like_a_plain_queue(self) -> None:
        listener = Listener()
        started = time.monotonic()
        with self.assertRaises(queue.Empty):
            listener.get(timeout=0.1)
        self.assertGreaterEqual(time.monotonic() - started, 0.09)
        with self.assertRaises(queue.Empty):
            listener.get_nowait()

    def test_a_waiting_get_wakes_up_for_an_offer_and_for_finish(self) -> None:
        listener = Listener()
        results: list[object] = []
        thread = threading.Thread(target=lambda: results.extend([listener.get(), listener.get()]))
        thread.start()
        time.sleep(0.1)
        listener.offer(Event("stage", stage="x"))
        time.sleep(0.1)
        listener.finish()
        thread.join(5)
        self.assertEqual(results[0].stage, "x")
        self.assertIsNone(results[1])

    def test_a_full_listener_sheds_the_oldest_log_lines_and_never_stage_error_or_completed(self) -> None:
        listener = Listener(capacity=10)
        listener.offer(Event("stage", stage="storage"))
        for index in range(50):
            listener.offer(Event("log", message=f"line {index}"))
        listener.offer(Event("error", error="boom"))
        for index in range(50, 60):
            listener.offer(Event("log", message=f"line {index}"))
        listener.offer(Event("completed", data={"success": False}))
        listener.finish()
        self.assertLessEqual(listener.qsize(), 11 + 1)
        received = []
        while (event := listener.get()) is not None:
            received.append(event)
        kinds = [event.type for event in received]
        self.assertEqual(kinds[0], "log")  # the notice about the dropped lines comes first
        self.assertIn("were skipped", received[0].message)
        self.assertEqual([event.type for event in received if event.type != "log"], ["stage", "error", "completed"])
        lines = [event.message for event in received if event.message.startswith("line")]
        self.assertEqual(lines[-1], "line 59")
        self.assertEqual(lines, sorted(lines, key=lambda text: int(text.split()[1])))
        self.assertGreater(listener.skipped_total, 40)

    def test_progress_events_are_shed_after_the_logs_but_before_protected_ones(self) -> None:
        listener = Listener(capacity=3)
        listener.offer(Event("stage", stage="a"))
        listener.offer(Event("progress", progress=1.0))
        listener.offer(Event("progress", progress=2.0))
        listener.offer(Event("stage", stage="b"))
        listener.offer(Event("stage", stage="c"))
        listener.finish()
        remaining = []
        while (event := listener.get()) is not None:
            remaining.append((event.type, event.stage or event.progress))
        self.assertEqual([item for item in remaining if item[0] == "stage"], [("stage", "a"), ("stage", "b"), ("stage", "c")])

    def test_only_protected_events_may_exceed_the_capacity(self) -> None:
        listener = Listener(capacity=2)
        for index in range(5):
            listener.offer(Event("stage", stage=str(index)))
        self.assertEqual(listener.qsize(), 5)

    def test_close_always_succeeds_and_discards(self) -> None:
        listener = Listener()
        listener.offer(Event("log", message="x"))
        listener.close()
        listener.close()
        listener.offer(Event("log", message="ignored"))
        self.assertEqual(listener.qsize(), 0)
        self.assertIsNone(listener.get())


class StreamingTests(DaemonCase):
    def setUp(self) -> None:
        self.start_daemon()

    def test_start_streams_status_then_job_events_with_increasing_seq_then_completed(self) -> None:
        def script(installer):
            installer.stage("preflight", 0.0)
            installer.log("hello")
            installer.stage("preflight", 100.0, "completed")
            return finishing(installer)

        self.use_installer(scripted(script))
        events = self.call(self.start_request())
        self.assertEqual(types(events), ["status", "stage", "log", "stage", "completed"])
        self.assertEqual(events[0]["data"]["status"], "running")
        job = events[0]["data"]["job_id"]
        self.assertTrue(all(event["job_id"] == job for event in events))
        seqs = [event["seq"] for event in events[1:]]
        self.assertEqual(seqs, [1, 2, 3, 4])
        self.assertNotIn("seq", events[0])
        self.assertTrue(events[-1]["data"]["success"])

    def test_a_second_start_while_running_is_install_busy(self) -> None:
        release = threading.Event()

        def script(installer):
            installer.stage("preflight")
            release.wait(10)
            return finishing(installer)

        self.use_installer(scripted(script))
        first = self.connect()
        client = first.__enter__()
        try:
            client.sendall((json.dumps({"version": 1, **self.start_request()}) + "\n").encode())
            self.assertTrue(wait_for(lambda: self.manager.is_running()))
            events = self.call(self.start_request())
            self.assertEqual(types(events), ["error", "completed"])
            self.assertEqual(events[0]["data"]["code"], "install.busy")
            self.assertIn("already running", events[0]["error"])
        finally:
            release.set()
            first.__exit__(None, None, None)

    def test_busy_check_blocks_a_start_with_its_reason(self) -> None:
        self.manager.busy_check = lambda: "A repair session holds /dev/nvme0n1p3"
        self.use_installer(scripted(lambda installer: finishing(installer)))
        events = self.call(self.start_request())
        self.assertEqual(events[0]["data"]["code"], "install.busy")
        self.assertIn("/dev/nvme0n1p3", events[0]["error"])
        self.assertEqual(self.manager.status, "idle")

    def test_a_busy_check_that_crashes_refuses_instead_of_starting(self) -> None:
        self.manager.busy_check = lambda: 1 / 0
        self.use_installer(scripted(lambda installer: finishing(installer)))
        with self.assertLogs("gentoo_installerd.server", level="ERROR"):
            events = self.call(self.start_request())
        self.assertEqual(events[0]["data"]["code"], "install.busy")
        self.assertEqual(self.manager.status, "idle")

    def test_a_busy_check_that_returns_nothing_lets_the_install_start(self) -> None:
        self.manager.busy_check = lambda: None
        self.use_installer(scripted(lambda installer: finishing(installer)))
        self.assertEqual(types(self.call(self.start_request()))[-1], "completed")

    def test_an_invalid_manifest_is_install_invalid_manifest(self) -> None:
        request = self.start_request()
        request["manifest"]["accounts"]["password"] = "short"
        events = self.call(request)
        self.assertEqual(types(events), ["error", "completed"])
        self.assertEqual(events[0]["data"]["code"], "install.invalid_manifest")

    def test_a_missing_manifest_is_a_bad_request(self) -> None:
        events = self.call({"action": "start"})
        self.assertEqual(events[0]["data"]["code"], "proto.bad_request")

    def test_cancel_when_idle_is_install_not_running(self) -> None:
        events = self.call({"action": "cancel"})
        self.assertEqual(types(events), ["error", "completed"])
        self.assertEqual(events[0]["data"]["code"], "install.not_running")

    def test_cancel_reaches_the_job_and_the_status_says_cancelling_then_cancelled(self) -> None:
        running = threading.Event()

        def script(installer):
            installer.stage("storage")
            running.set()
            installer.cancel.wait(10)
            installer.emit(Event("error", job_id=installer.job_id, stage="storage", message="Installation cancelled", error="cancelled", data={"code": "install.cancelled", "retryable": True}))
            installer.emit(Event("completed", job_id=installer.job_id, message="Installation cancelled", data={"success": False}))
            return False

        self.use_installer(scripted(script))
        stream = self.connect()
        client = stream.__enter__()
        try:
            client.sendall((json.dumps({"version": 1, **self.start_request()}) + "\n").encode())
            self.assertTrue(running.wait(5))
            reply = self.call({"action": "cancel"})
            self.assertEqual(reply[0]["data"], {"status": "cancelling"})
            events = read_events(client)
        finally:
            stream.__exit__(None, None, None)
        self.assertEqual(types(events)[-2:], ["error", "completed"])
        self.assertTrue(wait_for(lambda: self.manager.status == "cancelled"))
        status = self.call({"action": "status"})[0]["data"]
        self.assertEqual((status["status"], status["error"], status["error_stage"], status["error_code"]), ("cancelled", "cancelled", "storage", "install.cancelled"))
        self.assertIs(status["cancelling"], False)

    def test_a_crashing_installer_still_ends_the_stream_with_error_and_completed(self) -> None:
        def script(installer):
            installer.stage("storage")
            raise RuntimeError("kaboom [Errno 28]")

        self.use_installer(scripted(script))
        with self.assertLogs("gentoo_installerd.server", level="ERROR"):
            events = self.call(self.start_request())
        self.assertEqual(types(events)[-2:], ["error", "completed"])
        self.assertEqual(events[-2]["data"]["code"], "install.failed")
        self.assertIn("kaboom", events[-2]["error"])
        self.assertIs(events[-1]["data"]["success"], False)
        self.assertTrue(wait_for(lambda: self.manager.status == "failed"))
        self.assertIn("kaboom", self.manager.snapshot()["error"])
        # and the daemon is usable again
        self.use_installer(scripted(lambda installer: finishing(installer)))
        self.assertTrue(self.call(self.start_request())[-1]["data"]["success"])

    def test_an_installer_that_forgets_to_complete_gets_a_completed_event_anyway(self) -> None:
        self.use_installer(scripted(lambda installer: (installer.stage("preflight"), True)[1]))
        events = self.call(self.start_request())
        self.assertEqual(types(events), ["status", "stage", "completed"])
        self.assertIs(events[-1]["data"]["success"], True)
        self.assertEqual(self.manager.snapshot()["status"], "completed")

    def test_a_job_that_completes_itself_is_not_completed_twice(self) -> None:
        self.use_installer(scripted(lambda installer: finishing(installer)))
        self.assertEqual(types(self.call(self.start_request())).count("completed"), 1)

    def test_installer_construction_failure_is_reported_and_leaves_the_manager_idle(self) -> None:
        # D4: the state directory cannot be created (a regular file is in the way).
        (self.directory / "state").write_text("not a directory")
        with self.assertLogs("gentoo_installerd.server", level="ERROR") as logs:
            events = self.call(self.start_request())
        self.assertEqual(types(events), ["error", "completed"])
        self.assertIn("Installer backend error", events[0]["error"])
        self.assertEqual(events[0]["data"]["code"], "proto.internal")
        self.assertEqual(self.call({"action": "status"})[0]["data"]["status"], "idle")
        self.assertTrue(any("Traceback" in line or "Error" in line for line in logs.output))

    def test_a_client_that_leaves_mid_stream_does_not_stop_the_job_and_its_listener_is_removed(self) -> None:
        proceed = threading.Event()

        def script(installer):
            installer.log("first")
            proceed.wait(10)
            for index in range(300):
                installer.log("x" * 1000)
            return finishing(installer)

        self.use_installer(scripted(script))
        with self.connect() as client:
            client.sendall((json.dumps({"version": 1, **self.start_request()}) + "\n").encode())
            client.makefile("rb").readline()  # the status event
            self.assertTrue(wait_for(lambda: len(self.manager.listeners) == 1))
        proceed.set()
        self.assertTrue(wait_for(lambda: self.manager.status == "completed"))
        self.assertTrue(wait_for(lambda: not self.manager.listeners))
        self.assertEqual(self.manager.snapshot()["status"], "completed")

    def test_a_client_that_is_gone_before_the_first_event_leaves_no_listener_behind(self) -> None:
        release = threading.Event()
        self.use_installer(scripted(lambda installer: (release.wait(10), finishing(installer))[1]))
        original = server_module.Channel.send

        def refuse_first_status(channel, event):
            if event.type == "status" and event.data.get("status") == "running":
                raise server_module.ChannelClosed("gone")
            return original(channel, event)

        try:
            with patch.object(server_module.Channel, "send", refuse_first_status):
                events = self.call(self.start_request())
            self.assertEqual(events, [])
            self.assertTrue(self.manager.is_running(), "the install itself must go on")
            self.assertTrue(wait_for(lambda: not self.manager.listeners), "the listener of the vanished client leaked")
        finally:
            release.set()
        self.assertTrue(wait_for(lambda: self.manager.status == "completed"))

    def test_a_stalled_reader_does_not_break_the_stream_and_gets_the_terminal_events_later(self) -> None:
        # D3: the 30 s request timeout used to stay armed on a streaming connection.
        total = 50000

        def script(installer):
            installer.stage("storage")
            for index in range(total):
                installer.log(f"line {index} " + "x" * 40)
            installer.stage("storage", 100.0, "completed")
            return finishing(installer)

        self.use_installer(scripted(script))
        events: list[dict] = []
        with patch.object(server_module, "REQUEST_TIMEOUT", 0.3), self.connect(30) as client:
            self.manager.listener_capacity = 2000
            client.sendall((json.dumps({"version": 1, **self.start_request()}) + "\n").encode())
            self.assertTrue(wait_for(lambda: self.manager.status != "running", 30))
            time.sleep(1.0)  # well beyond the (patched) request timeout, without reading a byte
            events = read_events(client)
        self.assertEqual(types(events)[0], "status")
        self.assertEqual(types(events)[-1], "completed")
        self.assertTrue(events[-1]["data"]["success"])
        stage_events = [event for event in events if event["type"] == "stage"]
        self.assertEqual(len(stage_events), 2)
        logs = [event for event in events if event["type"] == "log"]
        self.assertLess(len(logs), total, "a client that cannot keep up loses old log lines, not stage or completed events")
        self.assertTrue(any("were skipped" in event["message"] for event in logs))
        self.assertEqual(logs[-1]["message"], f"line {total - 1} " + "x" * 40)
        seqs = [event["seq"] for event in events if "seq" in event]
        self.assertEqual(seqs, sorted(seqs))

    def test_a_client_that_never_reads_is_dropped_after_the_send_timeout_and_the_job_finishes(self) -> None:
        def script(installer):
            for index in range(3000):
                installer.log("y" * 2000)
            time.sleep(0.5)
            return finishing(installer)

        self.use_installer(scripted(script))
        with patch.object(server_module, "SEND_TIMEOUT", 0.4), self.connect() as client:
            client.sendall((json.dumps({"version": 1, **self.start_request()}) + "\n").encode())
            self.assertTrue(wait_for(lambda: self.manager.status == "completed", 20))
            self.assertTrue(wait_for(lambda: not self.manager.listeners, 20), "the stalled client's listener leaked")
        self.assertEqual(self.manager.snapshot()["status"], "completed")

    def test_a_quiet_stream_notices_that_its_client_left(self) -> None:
        release = threading.Event()
        self.use_installer(scripted(lambda installer: (release.wait(10), finishing(installer))[1]))
        with patch.object(server_module, "STREAM_POLL_SECONDS", 0.1):
            with self.connect() as client:
                client.sendall((json.dumps({"version": 1, **self.start_request()}) + "\n").encode())
                client.makefile("rb").readline()
                self.assertTrue(wait_for(lambda: len(self.manager.listeners) == 1))
            self.assertTrue(wait_for(lambda: not self.manager.listeners), "the handler of a vanished client kept waiting")
        release.set()

    def test_the_snapshot_error_is_the_specific_one_not_a_generic_replacement(self) -> None:
        # NOBS-11
        def script(installer):
            installer.emit(Event("error", job_id=installer.job_id, stage="kernel", message="boom", error="boom", data={"code": "install.failed", "retryable": True}))
            installer.emit(Event("completed", job_id=installer.job_id, data={"success": False}))
            return False

        self.use_installer(scripted(script))
        self.call(self.start_request())
        self.assertTrue(wait_for(lambda: self.manager.status == "failed"))
        snapshot = self.manager.snapshot()
        self.assertEqual((snapshot["status"], snapshot["error"], snapshot["error_stage"], snapshot["retryable"]), ("failed", "boom", "kernel", True))

    def test_without_an_error_event_a_failure_gets_the_generic_text(self) -> None:
        self.use_installer(scripted(lambda installer: False))
        self.call(self.start_request())
        self.assertTrue(wait_for(lambda: self.manager.status == "failed"))
        self.assertEqual(self.manager.snapshot()["error"], server_module.GENERIC_FAILURE)

    def test_the_last_of_several_errors_wins_and_all_are_listed(self) -> None:
        def script(installer):
            installer.emit(Event("error", job_id=installer.job_id, stage="kernel", error="dracut failed", message="dracut failed", data={"code": "install.failed"}))
            installer.emit(Event("error", job_id=installer.job_id, stage="cleanup", error="umount busy", message="umount busy", data={"code": "install.cleanup_failed"}))
            return False

        self.use_installer(scripted(script))
        self.call(self.start_request())
        self.assertTrue(wait_for(lambda: self.manager.status == "failed"))
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["error"], "umount busy")
        self.assertEqual([entry["message"] for entry in snapshot["errors"]], ["dracut failed", "umount busy"])

    def test_request_cancel_and_attach_errors_are_install_errors(self) -> None:
        manager = InstallManager(Path("/tmp/t"), Path("/tmp/s"), Path("/tmp/c"))
        with self.assertRaises(RuntimeError):
            manager.request_cancel()
        with self.assertRaises(InstallNotRunning):
            manager.request_cancel()
        with self.assertRaises(InstallNotRunning):
            manager.attach()
        self.assertTrue(issubclass(InstallBusy, RuntimeError))


if __name__ == "__main__":
    unittest.main()
