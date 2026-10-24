"""Wave 3 review fixes: the clock actions of the daemon tell the truth about what chrony did (CLOCK-1) and keep out of a repair (CLOCK-3)."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gentoo_installer import hardware
from gentoo_installerd import hwactions
from gentoo_installerd.events import ErrorCode
from gentoo_installerd.rescue.manager import RescueManager
from gentoo_installerd.rescue.ops import Operation, OperationTable
from gentoo_installerd.server import ActionError

from tests.backend.helpers import FakeRunner
from tests.backend.test_backend_hwactions import NOT_SYNCED, SYNCED, SYNCED_BUT_OFF, Channel, TrackingRunner, reply
from tests.backend.test_server_support import DaemonCase


@pytest.fixture
def clock(monkeypatch):
    class Fixture:
        runner = TrackingRunner()
        state = hardware.ClockInfo(chrony_running=True, ntp_synced=False, offset_s=None, rtc_mode="utc", year=2026)
        sleeps: list[float] = []

    fixture = Fixture()
    fixture.sleeps = []
    monkeypatch.setattr(hwactions, "read_clock", lambda: fixture.state)
    monkeypatch.setattr(hwactions, "make_runner", lambda: fixture.runner)
    monkeypatch.setattr(hwactions, "_sleep", fixture.sleeps.append)
    return fixture


def test_the_fixture_lines_are_what_the_parser_reads_in_real_life() -> None:
    assert hardware.parse_chrony_tracking(NOT_SYNCED) == (False, None)
    assert hardware.parse_chrony_tracking(SYNCED) == (True, pytest.approx(0.000123456))
    assert hardware.parse_chrony_tracking(SYNCED_BUT_OFF) == (True, pytest.approx(7199.000123456))


# --- CLOCK-1: `chronyc makestep` returns 200 OK without a source, so the answer must come from chrony's own state ---------------------


class TestAChronydThatNeverFindsAServer:
    """A network that blocks NTP: makestep answers '200 OK' and changes nothing."""

    def test_the_action_does_not_say_the_clock_was_set(self, clock):
        clock.runner = TrackingRunner(NOT_SYNCED)
        channel = Channel()
        hwactions.handle_sync_time({}, channel)
        ok, message = reply(channel)
        assert not ok
        assert message != "The clock was set from the network."
        assert "no time server" in message and "set the date and time by hand" in message

    def test_it_waits_a_while_before_giving_up(self, clock):
        clock.runner = TrackingRunner(NOT_SYNCED)
        hwactions.handle_sync_time({}, Channel())
        assert clock.runner.polls == hwactions.SYNC_POLLS and len(clock.sleeps) == hwactions.SYNC_POLLS - 1
        assert 10 <= sum(clock.sleeps) <= 30, "about fifteen seconds in all"


class TestAChronydThatFindsAServerAfterAWhile:
    def test_the_action_waits_for_it(self, clock):
        clock.runner = TrackingRunner(NOT_SYNCED, NOT_SYNCED, SYNCED)
        channel = Channel()
        hwactions.handle_sync_time({}, channel)
        assert reply(channel) == (True, "The clock was set from the network.")
        assert clock.runner.polls == 3 and len(clock.sleeps) == 2

    def test_a_clock_that_is_right_at_once_needs_no_waiting(self, clock):
        hwactions.handle_sync_time({}, Channel())
        assert clock.runner.polls == 1 and clock.sleeps == []


class TestAChronydThatSeesTheClockStillFarOff:
    def test_the_offset_is_reported(self, clock):
        clock.runner = TrackingRunner(SYNCED_BUT_OFF)
        channel = Channel()
        hwactions.handle_sync_time({}, channel)
        ok, message = reply(channel)
        assert not ok and "still off by about 7199 seconds" in message and "set the date and time by hand" in message

    def test_a_step_that_happens_while_waiting_is_noticed(self, clock):
        clock.runner = TrackingRunner(SYNCED_BUT_OFF, SYNCED_BUT_OFF, SYNCED)
        assert reply_of(clock)[0] is True


def reply_of(clock) -> tuple[bool, str]:
    channel = Channel()
    hwactions.handle_sync_time({}, channel)
    return reply(channel)


def test_a_chronyc_that_cannot_talk_to_chronyd_counts_as_not_synchronised(clock):
    clock.runner = TrackingRunner(SYNCED)
    clock.runner.failures["chronyc -c tracking"] = 1  # "506 Cannot talk to daemon"
    ok, message = reply_of(clock)
    assert not ok and "no time server" in message


class TestWithoutARunningChrony:
    """A one-shot `chronyd -q` leaves no daemon behind, so there is no tracking to ask: its exit status is the answer."""

    def test_a_one_shot_that_succeeds_is_a_success(self, clock):
        clock.state = hardware.ClockInfo(chrony_running=False, rtc_mode="unknown", year=2000)
        assert reply_of(clock) == (True, "The clock was set from the network.")
        assert clock.runner.commands == [("chronyd", "-q", "pool pool.ntp.org iburst")] and clock.runner.polls == 0

    def test_a_one_shot_that_fails_is_a_failure(self, clock):
        clock.state = hardware.ClockInfo(chrony_running=False, rtc_mode="unknown", year=2000)
        clock.runner.failures["chronyd"] = 1
        ok, message = reply_of(clock)
        assert not ok and "Check the network connection" in message


# --- CLOCK-3: a repair operation that runs commands in a chroot must not have the clock stepped under it ------------------------------------


class Registry:
    """What the daemon's registry says about the repair module: its status summary lists the running operations."""

    def __init__(self, running: list | None = None, sessions: list | None = None, broken: bool = False) -> None:
        self.running, self.sessions, self.broken = running or [], sessions or [], broken

    def status_extensions(self) -> dict:
        if self.broken:
            raise RuntimeError("the status provider failed")
        return {"rescue": {"api": 1, "sessions": self.sessions, "pool": 0, "running": self.running}}


class RepairChannel(Channel):
    def __init__(self, registry: Registry, installing: bool = False) -> None:
        super().__init__(running=installing)
        self.server = type("Server", (), {"registry": registry})()


RUNNING_REPAIR = {"op_id": "op-1", "kind": "run", "session_id": "s1", "state": "running"}


@pytest.mark.parametrize("action", ["sync_time", "set_time"])
def test_the_clock_is_not_touched_while_a_repair_operation_runs(clock, action):
    channel = RepairChannel(Registry(running=[RUNNING_REPAIR]))
    handler = hwactions.handle_sync_time if action == "sync_time" else hwactions.handle_set_time
    with pytest.raises(ActionError) as caught:
        handler({"utc": "2026-10-24 12:30:05"}, channel)
    assert caught.value.code == ErrorCode.BUSY and "repair" in str(caught.value) and clock.runner.commands == []


def test_an_open_repair_session_that_is_only_mounted_does_not_block_the_clock(clock):
    """The repair may need the right clock first; only an operation that is running counts, not a mounted system."""
    channel = RepairChannel(Registry(sessions=[{"session_id": "s1", "state": "mounted"}]))
    hwactions.handle_sync_time({}, channel)
    assert reply(channel)[0] is True


def test_a_repair_module_that_cannot_answer_never_blocks_the_clock(clock):
    channel = RepairChannel(Registry(broken=True))
    hwactions.handle_sync_time({}, channel)
    assert reply(channel)[0] is True


def test_a_daemon_without_a_repair_module_just_works(clock):
    channel = Channel()  # no server at all
    hwactions.handle_set_time({"utc": "2026-10-24 12:30:05"}, channel)
    assert clock.runner.commands[0][0] == "date"


# --- the same thing through the real socket server and the real repair status ---------------------------------------------------------------


class TheClockOverTheSocket(DaemonCase):
    """A repair operation that is running makes the daemon refuse both clock actions; one that has ended does not."""

    def setUp(self) -> None:
        self.start_daemon()
        hwactions.register(self.registry)
        self.table = OperationTable()
        manager = SimpleNamespace(lock=threading.RLock(), sessions={}, pool_ledger=[], ops=self.table)
        self.registry.add_status_provider("rescue", lambda: RescueManager.status(manager))  # type: ignore[arg-type]
        self.runner = FakeRunner()
        for target, value in (("make_runner", lambda: self.runner), ("read_clock", lambda: hardware.ClockInfo(rtc_mode="local", year=2026))):
            patcher = patch.object(hwactions, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_running_repair_step_refuses_the_clock(self) -> None:
        self.table.add(Operation("run", "s1"))
        for request in ({"action": "set_time", "utc": "2026-10-24 12:30:05"}, {"action": "sync_time"}):
            events = self.call(request)
            error = next(event for event in events if event["type"] == "error")
            self.assertEqual(error["data"]["code"], "install.busy", request)
            self.assertIn("repair", error["error"])
        self.assertEqual(self.runner.commands, [])

    def test_a_repair_step_that_has_ended_does_not(self) -> None:
        op = self.table.add(Operation("run", "s1"))
        op.complete(True)
        events = self.call({"action": "set_time", "utc": "2026-10-24 12:30:05"})
        self.assertEqual([event["type"] for event in events], ["result", "completed"])
        self.assertTrue(events[0]["data"]["ok"])
        self.assertEqual(self.runner.commands[0][0], "date")
