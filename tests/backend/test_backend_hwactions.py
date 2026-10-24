"""interface I3: the clock actions sync_time and set_time (feature hardware.v1), run with fakes only."""

from __future__ import annotations

import calendar

import pytest

from gentoo_installer import hardware
from gentoo_installerd import hwactions
from gentoo_installerd.events import ErrorCode
from gentoo_installerd.runner import RunnerError
from gentoo_installerd.server import ActionError, ActionRegistry, load_plugins

from tests.backend.helpers import FakeRunner

EPOCH = calendar.timegm((2026, 10, 24, 12, 30, 5))
# `chronyc -c tracking` as chronyd 4.x prints it (14 fields: reference id, name, stratum, reference time, system time offset, ...).
SYNCED = "C0A80101,192.168.1.1,3,1792800000.123456789,0.000123456,0.000012345,0.000234567,12.345,0.012,0.123,0.012345,0.001234,64.5,Normal"
# A chronyd that has not found a time server yet (a network that blocks NTP): stratum 0, "Not synchronised".
NOT_SYNCED = "00000000,,0,0.000000000,0.000000000,0.000000000,0.000000000,0.000,0.000,0.000,0.000000,0.000000,0.0,Not synchronised"
# chronyd tracks a server and sees the clock far off (it has not stepped it yet).
SYNCED_BUT_OFF = "C0A80101,192.168.1.1,3,1792800000.123456789,7199.000123456,0.000012345,0.000234567,12.345,0.012,0.123,0.012345,0.001234,64.5,Normal"


class TrackingRunner(FakeRunner):
    """A FakeRunner that also answers `chronyc -c tracking` like chronyd does: one scripted line per call, the last one repeats."""

    def __init__(self, *tracking: str) -> None:
        super().__init__()
        self.tracking = list(tracking) or [SYNCED]
        self.polls = 0

    def run(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        if tuple(argv) == ("chronyc", "-c", "tracking"):
            self.polls += 1
            line = self.tracking.pop(0) if len(self.tracking) > 1 else self.tracking[0]
            self.responses = [item for item in self.responses if item[0] != "chronyc -c tracking"]
            self.respond("chronyc -c tracking", line)
        return super().run(argv, **kwargs)


class Channel:
    def __init__(self, running: bool = False) -> None:
        self.sent: list = []
        manager = type("Manager", (), {"is_running": lambda self: running})()
        self.install_manager = manager

    def send(self, event) -> None:
        self.sent.append(event)


@pytest.fixture
def clock(monkeypatch):
    """The clock the probe reports; tests change .state, the fake runner records the commands."""
    class Fixture:
        runner = TrackingRunner()
        # What the probe reports before the action: chronyd runs and has found no server (the real probe gives no offset then).
        state = hardware.ClockInfo(chrony_running=True, ntp_synced=False, offset_s=None, rtc_mode="utc", year=2026)
        sleeps: list[float] = []

    fixture = Fixture()
    fixture.sleeps = []
    monkeypatch.setattr(hwactions, "read_clock", lambda: fixture.state)
    monkeypatch.setattr(hwactions, "make_runner", lambda: fixture.runner)
    monkeypatch.setattr(hwactions, "_sleep", fixture.sleeps.append)  # the waits for chrony take no time in a test
    return fixture


def reply(channel: Channel) -> tuple[bool, str]:
    kinds = [event.type for event in channel.sent]
    assert kinds == ["result", "completed"], kinds
    result, done = channel.sent
    assert set(result.data) == {"ok", "message"} and done.data["success"] is result.data["ok"]
    return result.data["ok"], result.data["message"]


class TestRegistration:
    def test_both_actions_are_registered_under_the_hardware_feature(self):
        registry = ActionRegistry()
        hwactions.register(registry)
        assert {"sync_time", "set_time"} <= set(registry.actions())
        assert "hardware.v1" in registry.capabilities()["features"]
        assert "sync_time" in registry.capabilities()["actions"]  # the window shows its clock button when it finds this name

    def test_the_daemon_loads_the_module_by_itself(self):
        registry = ActionRegistry()
        assert "gentoo_installerd.hwactions" in load_plugins(registry, rescue=False)
        assert registry.lookup("set_time") is hwactions.handle_set_time


class TestSyncTime:
    def test_a_running_chrony_is_told_to_step_the_clock_and_asked_whether_it_worked(self, clock):
        channel = Channel()
        hwactions.handle_sync_time({}, channel)
        assert clock.runner.commands == [("chronyc", "makestep"), ("chronyc", "-c", "tracking")]
        assert clock.runner.options[0] == {"timeout": 60.0, "capture": False, "root_identity": False}
        assert reply(channel) == (True, "The clock was set from the network.")

    def test_without_chrony_a_one_shot_chronyd_does_it(self, clock):
        clock.state = hardware.ClockInfo(chrony_running=False, rtc_mode="unknown", year=2000)
        hwactions.handle_sync_time({}, Channel())
        assert clock.runner.commands == [("chronyd", "-q", "pool pool.ntp.org iburst")]

    def test_a_network_that_cannot_be_reached_is_explained(self, clock):
        clock.runner.failures["chronyc"] = 1
        channel = Channel()
        hwactions.handle_sync_time({}, channel)
        ok, message = reply(channel)
        assert not ok and "Check the network connection" in message

    def test_a_command_that_succeeds_without_fixing_the_clock_is_not_reported_as_success(self, clock):
        clock.runner = TrackingRunner(SYNCED_BUT_OFF)
        channel = Channel()
        hwactions.handle_sync_time({}, channel)
        ok, message = reply(channel)
        assert not ok and "still off by about 7199 seconds" in message

    def test_the_clock_is_not_touched_while_an_installation_runs(self, clock):
        with pytest.raises(ActionError) as caught:
            hwactions.handle_sync_time({}, Channel(running=True))
        assert caught.value.code == ErrorCode.BUSY and clock.runner.commands == []


class TestSetTime:
    def request(self, text="2026-10-24 12:30:05") -> dict:
        return {"action": "set_time", "utc": text}

    def test_the_time_is_set_and_saved_to_a_hardware_clock_that_keeps_utc(self, clock):
        channel = Channel()
        hwactions.handle_set_time(self.request(), channel)
        assert clock.runner.commands == [("date", "-u", "-s", f"@{EPOCH}"), ("hwclock", "--systohc")]
        assert reply(channel) == (True, "The clock was set to 2026-10-24 12:30:05 UTC and saved to the hardware clock.")

    @pytest.mark.parametrize("mode", ["local", "unknown"])
    def test_a_hardware_clock_that_may_keep_local_time_is_never_written(self, clock, mode):
        clock.state = hardware.ClockInfo(rtc_mode=mode, year=2026)
        channel = Channel()
        hwactions.handle_set_time(self.request(), channel)
        assert clock.runner.commands == [("date", "-u", "-s", f"@{EPOCH}")]
        ok, message = reply(channel)
        assert ok and "hardware clock was left alone" in message

    def test_iso_forms_with_a_t_and_a_z_are_accepted(self, clock):
        hwactions.handle_set_time(self.request("2026-10-24T12:30:05Z"), Channel())
        assert clock.runner.commands[0] == ("date", "-u", "-s", f"@{EPOCH}")

    @pytest.mark.parametrize("text", ["", "yesterday", "2019-12-31 23:59:59", "2101-01-01 00:00:00", "2026-13-01 00:00:00", "2026-10-24 25:00:00", None, 5])
    def test_a_time_that_makes_no_sense_is_refused_before_anything_runs(self, clock, text):
        with pytest.raises(ActionError) as caught:
            hwactions.handle_set_time(self.request(text), Channel())
        assert caught.value.code == ErrorCode.BAD_REQUEST and "YYYY-MM-DD HH:MM:SS" in str(caught.value)
        assert clock.runner.commands == []

    def test_a_missing_time_is_refused(self, clock):
        with pytest.raises(ActionError):
            hwactions.handle_set_time({"action": "set_time"}, Channel())

    def test_a_failing_date_command_is_reported(self, clock):
        clock.runner.failures["date"] = 1
        channel = Channel()
        hwactions.handle_set_time(self.request(), channel)
        assert reply(channel) == (False, "The clock could not be set.")
        assert [c[0] for c in clock.runner.commands] == ["date"]

    def test_a_hardware_clock_that_cannot_be_written_is_a_partial_success_with_a_warning(self, clock):
        clock.runner.failures["hwclock"] = 1
        channel = Channel()
        hwactions.handle_set_time(self.request(), channel)
        ok, message = reply(channel)
        assert ok and "could not be saved to the hardware clock" in message

    def test_the_clock_is_not_touched_while_an_installation_runs(self, clock):
        with pytest.raises(ActionError) as caught:
            hwactions.handle_set_time(self.request(), Channel(running=True))
        assert caught.value.code == ErrorCode.BUSY and clock.runner.commands == []


def test_a_refused_request_becomes_an_error_event_through_the_servers_boundary():
    # ActionError is what the connection handler turns into one error event and one completed event
    from gentoo_installerd.server import RequestHandler

    sent: list = []

    class Peer:
        completed_sent = False

        def send(self, event) -> None:
            sent.append(event)

    handler = RequestHandler.__new__(RequestHandler)
    handler._fail(Peer(), ActionError("Enter the date.", ErrorCode.BAD_REQUEST))  # type: ignore[arg-type]
    assert [e.type for e in sent] == ["error", "completed"] and sent[0].data["code"] == "proto.bad_request"


def test_the_real_runner_is_a_subprocess_runner():
    from gentoo_installerd.runner import SubprocessRunner

    assert isinstance(hwactions.make_runner(), SubprocessRunner)


def test_runner_errors_are_the_only_failures_that_become_replies(clock):
    clock.runner.failures["chronyc"] = 1
    with pytest.raises(RunnerError):
        clock.runner.run(("chronyc", "makestep"))  # the fake really raises what the handler catches
