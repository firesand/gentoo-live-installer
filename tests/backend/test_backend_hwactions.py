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
        runner = FakeRunner()
        state = hardware.ClockInfo(chrony_running=True, ntp_synced=False, offset_s=7200.0, rtc_mode="utc", year=2026)
        after = hardware.ClockInfo(chrony_running=True, ntp_synced=True, offset_s=0.2, rtc_mode="utc", year=2026)
        reads = 0

    fixture = Fixture()

    def read():
        fixture.reads += 1
        return fixture.state if fixture.reads == 1 else fixture.after

    monkeypatch.setattr(hwactions, "read_clock", read)
    monkeypatch.setattr(hwactions, "make_runner", lambda: fixture.runner)
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
    def test_a_running_chrony_is_told_to_step_the_clock(self, clock):
        channel = Channel()
        hwactions.handle_sync_time({}, channel)
        assert clock.runner.commands == [("chronyc", "makestep")]
        assert clock.runner.options == [{"timeout": 60.0, "capture": False, "root_identity": False}]
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
        clock.after = hardware.ClockInfo(chrony_running=True, ntp_synced=False, offset_s=7199.0, rtc_mode="utc", year=2026)
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
