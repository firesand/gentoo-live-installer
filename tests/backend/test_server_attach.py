"""D1/D2: attach with a ring buffer, extended status, and log export."""
from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch

from gentoo_installerd import server as server_module
from gentoo_installerd.events import Event
from gentoo_installerd.server import EventHistory, format_log_record

from tests.backend.test_server_support import DaemonCase, finishing, read_events, scripted, types, wait_for


class EventHistoryTests(unittest.TestCase):
    def events(self, count: int, kind: str = "log") -> list[Event]:
        return [Event(kind, message=str(index), seq=index + 1) for index in range(count)]

    def test_the_last_events_are_kept_whatever_they_are(self) -> None:
        history = EventHistory(limit=100)
        for event in self.events(250):
            history.append(event)
        replay = history.replay()
        self.assertEqual(len(replay), 100)
        self.assertEqual([event.seq for event in replay], list(range(151, 251)))

    def test_stage_error_and_completed_events_are_never_dropped(self) -> None:
        history = EventHistory(limit=50)
        history.append(Event("stage", stage="storage", seq=1))
        history.append(Event("error", error="early", seq=2))
        for index in range(3, 500):
            history.append(Event("log", message="x", seq=index))
        history.append(Event("stage", stage="late", seq=500))
        history.append(Event("completed", seq=501, data={"success": False}))
        replay = history.replay()
        self.assertEqual([event.type for event in replay if event.type != "log"], ["stage", "error", "stage", "completed"])
        self.assertEqual([event.seq for event in replay], sorted(event.seq for event in replay))
        self.assertEqual(sum(event.type == "log" for event in replay), 48)  # the 50 newest events, two of them protected

    def test_replay_filters_by_sequence_number(self) -> None:
        history = EventHistory(limit=10)
        for event in self.events(10):
            history.append(event)
        self.assertEqual([event.seq for event in history.replay(7)], [8, 9, 10])
        self.assertEqual(history.replay(10), [])
        self.assertEqual(len(history.replay(0)), 10)

    def test_at_least_five_thousand_events_are_kept_by_default(self) -> None:
        history = EventHistory()
        for event in self.events(7000):
            history.append(event)
        self.assertGreaterEqual(len(history.replay()), 5000)


class AttachTests(DaemonCase):
    def setUp(self) -> None:
        self.start_daemon()

    def start_job(self, script) -> None:
        self.use_installer(scripted(script))

    def test_attach_without_any_job_is_install_not_running(self) -> None:
        events = self.call({"action": "attach"})
        self.assertEqual(types(events), ["error", "completed"])
        self.assertEqual(events[0]["data"]["code"], "install.not_running")

    def test_attach_replays_the_buffer_then_streams_live_events_until_completed(self) -> None:
        proceed = threading.Event()
        ready = threading.Event()

        def script(installer):
            installer.stage("storage")
            for index in range(5):
                installer.log(f"early {index}")
            ready.set()
            proceed.wait(10)
            installer.log("late 0")
            installer.stage("storage", 100.0, "completed")
            return finishing(installer)

        self.start_job(script)
        starter = self.connect()
        client = starter.__enter__()
        try:
            client.sendall((json.dumps({"version": 1, **self.start_request()}) + "\n").encode())
            self.assertTrue(ready.wait(5))
            # a second, reconnecting client
            with self.connect() as second:
                second.sendall(b'{"version":1,"action":"attach","after_seq":0,"request_id":"a1"}\n')
                reader = second.makefile("rb")
                replayed = [json.loads(reader.readline()) for _ in range(6)]
                self.assertEqual([event["seq"] for event in replayed], [1, 2, 3, 4, 5, 6])
                self.assertEqual(replayed[0]["type"], "stage")
                self.assertEqual(replayed[1]["message"], "early 0")
                self.assertTrue(all(event["data"].get("request_id") == "a1" for event in replayed if event.get("data")))
                proceed.set()
                live = [json.loads(line) for line in reader]
            first = read_events(client)
        finally:
            proceed.set()
            starter.__exit__(None, None, None)
        self.assertEqual(types(live), ["log", "stage", "completed"])
        self.assertEqual([event["seq"] for event in live], [7, 8, 9])
        # both listeners saw the terminal event, and the original stream is untouched
        self.assertEqual(types(first)[-1], "completed")
        self.assertEqual([event["seq"] for event in first if "seq" in event][-3:], [7, 8, 9])

    def test_attach_after_seq_skips_what_the_client_already_has(self) -> None:
        def script(installer):
            for index in range(10):
                installer.log(f"line {index}")
            return finishing(installer)

        self.start_job(script)
        self.call(self.start_request())
        self.assertTrue(wait_for(lambda: self.manager.status == "completed"))
        events = self.call({"action": "attach", "after_seq": 8})
        self.assertEqual([event["seq"] for event in events], [9, 10, 11])
        self.assertEqual(types(events), ["log", "log", "completed"])

    def test_attach_after_the_job_ended_still_ends_with_completed_even_if_the_client_saw_everything(self) -> None:
        self.start_job(lambda installer: (installer.log("only"), finishing(installer, False))[1])
        self.call(self.start_request())
        self.assertTrue(wait_for(lambda: self.manager.status == "failed"))
        events = self.call({"action": "attach", "after_seq": 999})
        self.assertEqual(types(events), ["completed"])
        self.assertIs(events[0]["data"]["success"], False)
        self.assertEqual(events[0]["seq"], 2)

    def test_the_replay_of_a_long_job_keeps_every_stage_error_and_completed_event(self) -> None:
        self.manager.history_events = 100

        def script(installer):
            installer.stage("preflight", 0.0)
            installer.stage("preflight", 100.0, "completed")
            for index in range(1000):
                installer.log(f"line {index}")
            installer.emit(Event("error", job_id=installer.job_id, stage="base", error="emerge failed", message="emerge failed", data={"code": "install.failed"}))
            return finishing(installer, False)

        self.start_job(script)
        self.call(self.start_request())
        self.assertTrue(wait_for(lambda: self.manager.status == "failed"))
        events = self.call({"action": "attach"})
        important = [event["type"] for event in events if event["type"] != "log"]
        self.assertEqual(important, ["stage", "stage", "error", "completed"])
        self.assertEqual(sum(event["type"] == "log" for event in events), 100 - 2)
        seqs = [event["seq"] for event in events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(seqs[0], 1)

    def test_a_second_job_replaces_the_buffer(self) -> None:
        self.start_job(lambda installer: (installer.log("job"), finishing(installer))[1])
        first = self.call(self.start_request())
        self.assertTrue(wait_for(lambda: self.manager.status == "completed"))
        second = self.call(self.start_request())
        attached = self.call({"action": "attach"})
        self.assertNotEqual(first[0]["job_id"], second[0]["job_id"])
        self.assertTrue(all(event["job_id"] == second[0]["job_id"] for event in attached))
        self.assertEqual(attached[0]["seq"], 1)

    def test_bad_after_seq_is_a_bad_request(self) -> None:
        for value in (-1, "7", 1.5, True, None, [1]):
            with self.subTest(value=value):
                events = self.call({"action": "attach", "after_seq": value})
                if value is None:
                    self.assertEqual(events[0]["data"]["code"], "install.not_running")
                else:
                    self.assertEqual(events[0]["data"]["code"], "proto.bad_request")

    def test_a_client_that_leaves_an_attach_does_not_leak_its_listener(self) -> None:
        release = threading.Event()
        self.start_job(lambda installer: (installer.log("a"), release.wait(10), finishing(installer))[2])
        starter = self.connect()
        client = starter.__enter__()
        try:
            with patch.object(server_module, "STREAM_POLL_SECONDS", 0.1):
                client.sendall((json.dumps({"version": 1, **self.start_request()}) + "\n").encode())
                self.assertTrue(wait_for(lambda: self.manager.is_running()))
                with self.connect() as second:
                    second.sendall(b'{"version":1,"action":"attach"}\n')
                    second.makefile("rb").readline()
                    self.assertTrue(wait_for(lambda: len(self.manager.listeners) == 2))
                self.assertTrue(wait_for(lambda: len(self.manager.listeners) == 1), "the attach handler of a vanished client kept its listener")
        finally:
            release.set()
            starter.__exit__(None, None, None)


class StatusTests(DaemonCase):
    def setUp(self) -> None:
        self.start_daemon()

    def test_idle_status_has_every_field(self) -> None:
        data = self.call({"action": "status"})[0]["data"]
        self.assertEqual(data["status"], "idle")
        for key in ("stage", "progress", "seq", "started_at", "finished_at", "resumable", "error", "job_id", "retryable", "log_path", "leftovers", "stages", "cancelling"):
            self.assertIn(key, data)
        self.assertIs(data["resumable"], False)
        self.assertEqual((data["seq"], data["stage"], data["error"], data["leftovers"], data["log_path"]), (0, "", "", [], ""))
        self.assertIsNone(data["started_at"])

    def test_running_status_reports_stage_progress_seq_and_start_time(self) -> None:
        hold = threading.Event()
        running = threading.Event()

        def script(installer):
            installer.stage("kernel", 42.5)
            installer.log("compiling")
            running.set()
            hold.wait(10)
            return finishing(installer)

        self.use_installer(scripted(script))
        starter = self.connect()
        client = starter.__enter__()
        try:
            client.sendall((json.dumps({"version": 1, **self.start_request()}) + "\n").encode())
            self.assertTrue(running.wait(5))
            data = self.call({"action": "status"})[0]["data"]
            self.assertEqual((data["status"], data["stage"], data["progress"], data["seq"]), ("running", "kernel", 42.5, 2))
            self.assertIsNotNone(data["started_at"])
            self.assertIsNone(data["finished_at"])
            self.assertEqual(data["message"], "running kernel")
            self.assertEqual(data["leftovers"], [])
            self.assertIs(data["resumable"], False)
        finally:
            hold.set()
            starter.__exit__(None, None, None)
        self.assertTrue(wait_for(lambda: self.manager.status == "completed"))
        data = self.call({"action": "status"})[0]["data"]
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["progress"], 100.0)
        self.assertGreaterEqual(data["finished_at"], data["started_at"])

    def test_resumable_needs_a_storage_checkpoint_and_layout(self) -> None:
        state = self.directory / "state"
        state.mkdir()
        (state / "checkpoints.json").write_text(json.dumps({"version": 1, "fingerprint": "x", "completed": ["storage", "stage3"]}))
        self.assertIs(self.call({"action": "status"})[0]["data"]["resumable"], False)
        (state / "layout.json").write_text("{}")
        self.assertIs(self.call({"action": "status"})[0]["data"]["resumable"], True)
        (state / "checkpoints.json").write_text(json.dumps({"version": 1, "fingerprint": "x", "completed": ["stage3"]}))
        self.assertIs(self.call({"action": "status"})[0]["data"]["resumable"], False)
        (state / "checkpoints.json").write_text("not json")
        self.assertIs(self.call({"action": "status"})[0]["data"]["resumable"], False)

    def test_a_failed_job_with_a_checkpoint_is_resumable_and_reports_why_it_failed(self) -> None:
        state = self.directory / "state"
        state.mkdir()
        (state / "checkpoints.json").write_text(json.dumps({"version": 1, "fingerprint": "x", "completed": ["storage"]}))
        (state / "layout.json").write_text("{}")

        def script(installer):
            installer.emit(Event("error", job_id=installer.job_id, stage="kernel", message="dracut failed", error="dracut failed", data={"code": "install.failed", "retryable": True}))
            return finishing(installer, False)

        self.use_installer(scripted(script))
        self.call(self.start_request())
        self.assertTrue(wait_for(lambda: self.manager.status == "failed"))
        data = self.call({"action": "status"})[0]["data"]
        self.assertEqual((data["status"], data["resumable"], data["error"], data["error_stage"], data["retryable"]), ("failed", True, "dracut failed", "kernel", True))

    def test_a_completed_job_is_not_resumable(self) -> None:
        state = self.directory / "state"
        state.mkdir()
        (state / "checkpoints.json").write_text(json.dumps({"version": 1, "fingerprint": "x", "completed": ["storage"]}))
        (state / "layout.json").write_text("{}")
        self.use_installer(scripted(lambda installer: finishing(installer)))
        self.call(self.start_request())
        self.assertTrue(wait_for(lambda: self.manager.status == "completed"))
        self.assertIs(self.call({"action": "status"})[0]["data"]["resumable"], False)

    def test_the_stage_table_in_status_has_the_weights_of_the_job(self) -> None:
        data = self.call({"action": "status"})[0]["data"]
        self.assertEqual([stage["name"] for stage in data["stages"]][-1], "cleanup")
        self.assertEqual(sum(stage["weight"] for stage in data["stages"]), 100)

    def test_log_path_appears_once_the_log_exists(self) -> None:
        state = self.directory / "state"
        state.mkdir()
        (state / "install.log").write_text("{}\n")
        self.assertEqual(self.call({"action": "status"})[0]["data"]["log_path"], str(state / "install.log"))


class LogActionTests(DaemonCase):
    def setUp(self) -> None:
        self.start_daemon()
        self.state = self.directory / "state"
        self.state.mkdir()
        self.log = self.state / "install.log"

    def write_log(self, count: int) -> None:
        records = [{"ts": f"2026-10-24T10:00:{index % 60:02d}.000Z", "job_id": "j", "type": "log", "stage": "base", "message": f"line {index}"} for index in range(count)]
        self.log.write_text("".join(json.dumps(record) + "\n" for record in records))

    def lines_of(self, events: list[dict]) -> list[str]:
        return [line for event in events if event["type"] == "result" for line in event["data"]["lines"]]

    def test_the_whole_log_is_streamed_in_chunks_of_at_most_1000_lines(self) -> None:
        self.write_log(2500)
        events = self.call({"action": "log"})
        self.assertEqual(types(events), ["result", "result", "result", "completed"])
        self.assertEqual([len(event["data"]["lines"]) for event in events[:3]], [1000, 1000, 500])
        self.assertEqual(events[-1]["data"], {"success": True, "lines": 2500})
        lines = self.lines_of(events)
        self.assertEqual(len(lines), 2500)
        self.assertIn("line 0", lines[0])
        self.assertIn("line 2499", lines[-1])

    def test_tail_limits_the_output_to_the_last_lines(self) -> None:
        self.write_log(100)
        lines = self.lines_of(self.call({"action": "log", "tail": 7}))
        self.assertEqual(len(lines), 7)
        self.assertIn("line 93", lines[0])
        self.assertIn("line 99", lines[-1])
        self.assertEqual(len(self.lines_of(self.call({"action": "log", "tail": 0}))), 100)
        self.assertEqual(len(self.lines_of(self.call({"action": "log", "tail": 500}))), 100)

    def test_text_format_is_readable_and_jsonl_is_the_raw_record(self) -> None:
        self.write_log(1)
        text = self.lines_of(self.call({"action": "log"}))[0]
        self.assertEqual(text, "2026-10-24T10:00:00.000Z log       [base] line 0")
        raw = self.lines_of(self.call({"action": "log", "format": "jsonl"}))[0]
        self.assertEqual(json.loads(raw)["message"], "line 0")
        events = self.call({"action": "log", "format": "xml"})
        self.assertEqual(events[0]["data"]["code"], "proto.bad_request")

    def test_a_missing_log_is_an_empty_successful_reply(self) -> None:
        events = self.call({"action": "log"})
        self.assertEqual(types(events), ["completed"])
        self.assertEqual(events[0]["data"], {"success": True, "lines": 0})

    def test_secrets_are_scrubbed_again_on_the_way_out(self) -> None:
        self.log.write_text(json.dumps({"ts": "t", "type": "log", "message": 'password=hunter2xx and {"wifi_password": "secret-wifi"}'}) + "\n")
        lines = self.lines_of(self.call({"action": "log"}))
        self.assertNotIn("hunter2xx", lines[0])
        self.assertNotIn("secret-wifi", lines[0])

    def test_the_known_secrets_of_the_last_job_are_scrubbed(self) -> None:
        installer_class = scripted(lambda installer: finishing(installer))
        self.use_installer(installer_class)
        self.call(self.start_request())
        self.assertTrue(wait_for(lambda: self.manager.status == "completed"))
        installer_class.instances[0].secrets = ("opaque-value-123",)
        self.log.write_text(json.dumps({"ts": "t", "type": "log", "message": "got opaque-value-123 in the output"}) + "\n")
        self.assertNotIn("opaque-value-123", self.lines_of(self.call({"action": "log"}))[0])

    def test_legacy_and_garbled_lines_are_passed_through(self) -> None:
        self.log.write_text('{"job_id": "j", "event": "created"}\nnot json at all\n[1, 2]\n')
        lines = self.lines_of(self.call({"action": "log"}))
        self.assertEqual(lines, ['{"job_id": "j", "event": "created"}', "not json at all", "[1, 2]"])

    def test_bad_tail_is_a_bad_request(self) -> None:
        for value in (-1, "5", True):
            with self.subTest(value=value):
                self.assertEqual(self.call({"action": "log", "tail": value})[0]["data"]["code"], "proto.bad_request")

    def test_a_large_log_is_not_loaded_into_memory_to_take_its_tail(self) -> None:
        self.write_log(30000)
        lines = self.lines_of(self.call({"action": "log", "tail": 3}))
        self.assertEqual(len(lines), 3)

    def test_format_log_record_handles_errors_and_codes(self) -> None:
        line = json.dumps({"ts": "T", "type": "error", "stage": "kernel", "message": "dracut failed", "error": "dracut failed (2)", "code": "install.failed"})
        self.assertEqual(format_log_record(line), "T error     [kernel] dracut failed (dracut failed (2)) [install.failed]")


if __name__ == "__main__":
    unittest.main()
