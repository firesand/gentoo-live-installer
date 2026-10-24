"""Operations: sequence numbers, the ring buffer that keeps the essential events, reattaching, one completed event, and the table of recent operations."""

from __future__ import annotations

import threading
import time

import pytest

from gentoo_installerd.rescue.errors import RescueError
from gentoo_installerd.rescue.ops import ESSENTIAL, EventBuffer, Operation, OperationTable, RescueEvent
from gentoo_installerd.runner import InstallCancelled


def test_events_are_numbered_from_one_and_carry_the_request() -> None:
    op = Operation("scan", "r1a2b3c", "req-7")
    op.stage("devices", "Looking at disks", 10)
    op.progress("Reading /dev/sda1", 20, device="/dev/sda1")
    op.log("a line")
    op.publish({"candidates": []}, "rescue.scan")
    op.complete(True)
    events = op.buffer.since(0)
    assert [e.seq for e in events] == [1, 2, 3, 4, 5] and [e.type for e in events] == ["stage", "progress", "log", "result", "completed"]
    assert all(e.job_id == op.id and e.data["session_id"] == "r1a2b3c" and e.data["request_id"] == "req-7" for e in events)
    assert events[0].to_dict()["seq"] == 1 and events[-1].to_dict()["data"]["success"] is True and events[-1].progress == 100.0
    assert op.buffer.last_seq == 5 and op.buffer.done and op.result == {"candidates": []}


def test_the_wire_form_has_a_seq_only_when_it_has_a_job() -> None:
    assert "seq" not in RescueEvent("result", data={"x": 1}).to_dict()  # not part of a job: no sequence number
    assert RescueEvent("log", message="m", seq=3).to_dict()["seq"] == 3


def test_exactly_one_completed_event_whatever_happens() -> None:
    op = Operation("run")
    op.complete(True)
    op.complete(False, "too late")
    assert [e.type for e in op.buffer.since(0)] == ["completed"] and op.success is True
    failing = Operation("run")
    failing.run(lambda o: (_ for _ in ()).throw(RescueError("busy", "Another step is running")))
    types = [e.type for e in failing.buffer.since(0)]
    assert types == ["error", "completed"] and failing.success is False
    error = failing.buffer.since(0)[0]
    assert error.error == "rescue.busy" and error.data["code"] == "rescue.busy"


def test_unexpected_exceptions_and_cancellation_are_reported_without_a_traceback_in_the_error() -> None:
    boom = Operation("run")
    boom.run(lambda o: (_ for _ in ()).throw(KeyError("secret-looking-key")))
    error = [e for e in boom.buffer.since(0) if e.type == "error"][0]
    assert error.error == "proto.internal" and "secret-looking-key" not in error.message + str(error.data)
    cancelled = Operation("run")
    cancelled.run(lambda o: (_ for _ in ()).throw(InstallCancelled("stop")))
    assert [e.error for e in cancelled.buffer.since(0) if e.type == "error"] == ["rescue.cancelled"] and cancelled.success is False


def test_the_ring_buffer_drops_chatter_first_and_keeps_every_essential_event() -> None:
    op = Operation("run", limit=50)
    op.stage("one", "first stage")
    for number in range(500):
        op.log(f"line {number}")
        if number % 100 == 0:
            op.stage(f"s{number}", f"stage {number}")
    op.publish({"outcome": "success"})
    op.fail(RescueError("failed", "something"))
    op.complete(False)
    kept = op.buffer.since(0)
    essentials = [e for e in kept if e.type in ESSENTIAL]
    assert len(essentials) == 1 + 5 + 3  # all stages, the result, the error and the completion survive
    assert len([e for e in kept if e.type == "log"]) <= 50 and kept[-1].type == "completed"
    assert [e.seq for e in kept] == sorted(e.seq for e in kept) and len({e.seq for e in kept}) == len(kept)
    assert op.buffer.last_seq == 1 + 500 + 5 + 3  # numbering is never reused after events were dropped


def test_a_client_that_comes_back_gets_what_it_missed_in_order() -> None:
    op = Operation("run")
    for number in range(10):
        op.log(f"line {number}")
    op.complete(True)
    first = []
    op.stream(first.append, after_seq=0)
    assert [e.seq for e in first] == list(range(1, 12))
    later = []
    op.stream(later.append, after_seq=7)
    assert [e.seq for e in later] == [8, 9, 10, 11] and later[-1].type == "completed"
    nothing = []
    op.stream(nothing.append, after_seq=11)  # a finished operation with nothing new: returns at once
    assert nothing == []


def test_streaming_follows_a_running_operation_and_ends_with_it() -> None:
    op = Operation("run")
    seen: list[RescueEvent] = []
    consumer = threading.Thread(target=lambda: op.stream(seen.append, poll=0.05), daemon=True)
    consumer.start()
    time.sleep(0.1)
    op.log("while it runs")
    op.stage("late", "a later stage")
    time.sleep(0.1)
    assert [e.type for e in seen] == ["log", "stage"] and consumer.is_alive()
    op.complete(True)
    consumer.join(timeout=3)
    assert not consumer.is_alive() and seen[-1].type == "completed"


def test_the_work_runs_on_its_own_thread_and_survives_a_consumer_that_left() -> None:
    gate = threading.Event()

    def work(op: Operation) -> None:
        op.log("started")
        gate.wait(5)
        op.log("finished anyway")

    op = Operation("run").start(work)
    assert op.thread is not None and op.thread is not threading.current_thread()
    assert not op.wait(0.1)  # still running; nobody is listening
    gate.set()
    assert op.wait(5) and op.success is True
    assert [e.message for e in op.buffer.since(0) if e.type == "log"] == ["started", "finished anyway"]


def test_cancel_is_a_flag_the_work_looks_at_and_force_is_separate() -> None:
    op = Operation("run")
    assert not op.cancel_requested.is_set()
    op.request_cancel()
    assert op.cancel_requested.is_set() and not op.force_cancel.is_set()
    op.request_cancel(force=True)
    assert op.force_cancel.is_set()


def test_the_table_remembers_recent_operations_and_never_forgets_a_running_one() -> None:
    table = OperationTable(keep=3)
    ops = []
    for number in range(6):
        ops.append(table.add(Operation("run", "r1")))
        if number < 5:
            ops[-1].complete(True)
    assert [op for op in ops if table.get(op.id) is not None] == ops[3:]  # finished operations are dropped oldest first
    assert table.running() == [ops[5]] and table.latest("r1") is ops[5] and table.latest("nobody") is None and table.latest() is ops[5]
    ops[5].complete(False)
    assert table.running() == []
    busy = OperationTable(keep=1)
    first, second = busy.add(Operation("scan")), busy.add(Operation("mount"))
    assert busy.get(first.id) is first and busy.get(second.id) is second  # nothing that still runs is ever forgotten, whatever the limit


def test_the_summary_tells_a_reconnecting_client_where_an_operation_stands() -> None:
    op = Operation("mount", "r1a2b3c")
    op.log("x")
    summary = op.summary()
    assert summary["running"] is True and summary["success"] is None and summary["seq"] == 1 and summary["kind"] == "mount" and summary["session_id"] == "r1a2b3c"
    op.complete(True)
    assert op.summary()["running"] is False and op.summary()["success"] is True and op.summary()["finished"] is not None


@pytest.mark.parametrize("limit", [10, 100])
def test_wait_returns_new_events_or_times_out_empty(limit: int) -> None:
    buffer = EventBuffer(limit)
    started = time.monotonic()
    events, done = buffer.wait(0, 0.1)
    assert events == [] and done is False and time.monotonic() - started >= 0.09
    buffer.append(lambda seq: RescueEvent("log", message="x", seq=seq))
    events, done = buffer.wait(0, 1.0)
    assert [e.seq for e in events] == [1] and done is False
    buffer.close()
    events, done = buffer.wait(1, 1.0)
    assert events == [] and done is True
