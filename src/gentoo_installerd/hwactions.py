"""Hardware actions of the daemon (feature ``hardware.v1``): put the clock right before an install.

A PC with a dead clock battery starts in the wrong year and cannot verify a single HTTPS certificate, and a
PC without a network cannot ask for the time. These two actions fix the clock from the installer window:

* ``sync_time`` (no arguments) asks the network, with chrony.
* ``set_time`` (``{"utc": "YYYY-MM-DD HH:MM:SS"}``) sets it by hand; the hardware clock is written too, but only
  when it is known to keep UTC (a Windows dual-boot PC keeps local time there and must not be touched).

Each reply is one ``result`` event with ``data = {"ok": bool, "message": str}`` followed by ``completed``.
Neither action runs while an installation is running, or while a repair operation is: a clock that jumps under emerge
breaks timestamps.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from gentoo_installer import hardware

from .events import ErrorCode, Event, completed_event
from .runner import Runner, RunnerError, SubprocessRunner
from .server import ActionError, Channel

if TYPE_CHECKING:
    from .server import ActionRegistry

LOG = logging.getLogger(__name__)

FEATURE = "hardware.v1"
ACTIONS = ("sync_time", "set_time")
COMMAND_TIMEOUT_SECONDS = 60.0
# After a sync the clock counts as right when it is within this many seconds of the network's time.
SYNCED_WITHIN_SECONDS = 5.0
# `chronyc makestep` answers "200 OK" even when chronyd has no time server yet, so chrony is asked what it sees: this often,
# this many times (about fifteen seconds in all). chronyd may still be sending its first requests when the button is pressed.
SYNC_POLLS = 15
SYNC_POLL_SECONDS = 1.0
TRACKING_TIMEOUT_SECONDS = 10.0
_sleep = time.sleep  # a module attribute so that tests wait for nothing


def make_runner() -> Runner:
    """The runner that executes the clock commands (tests replace this with a fake)."""
    return SubprocessRunner()


def read_clock() -> hardware.ClockInfo:
    """What the probe knows about the clock right now (chrony's state and the hardware clock's mode)."""
    return hardware.probe().clock


def register(registry: ActionRegistry) -> None:
    registry.register_action("sync_time", handle_sync_time, feature=FEATURE)
    registry.register_action("set_time", handle_set_time, feature=FEATURE)


# ------------------------------------------------------------------------------------------ the actions


def handle_sync_time(request: dict, channel: Channel) -> None:
    _refuse_while_busy(channel)
    clock = read_clock()
    runner = make_runner()
    argv = hardware.sync_time_argv(clock)
    try:
        runner.run(argv, timeout=COMMAND_TIMEOUT_SECONDS, capture=False)
    except RunnerError as exc:
        LOG.warning("Setting the clock from the network failed: %s", exc)
        _reply(channel, False, "The clock could not be set from the network. Check the network connection, or set the date and time by hand.")
        return
    if argv[0] == "chronyc":
        # chronyd keeps running, and `chronyc makestep` says "200 OK" even when it has no time server (a network that blocks
        # NTP): only what chrony sees tells whether the clock is right. A one-shot `chronyd -q` leaves nothing to ask; its exit status is the answer.
        problem = _wait_for_sync(runner)
        if problem:
            _reply(channel, False, problem)
            return
    _reply(channel, True, "The clock was set from the network.")


def handle_set_time(request: dict, channel: Channel) -> None:
    text = request.get("utc")
    epoch = hardware.parse_set_time(text) if isinstance(text, str) else None
    if epoch is None:
        raise ActionError("Enter the date and time in UTC as YYYY-MM-DD HH:MM:SS, with a year from 2020 to 2100.", ErrorCode.BAD_REQUEST)
    _refuse_while_busy(channel)
    clock = read_clock()
    runner = make_runner()
    try:
        runner.run(("date", "-u", "-s", f"@{epoch}"), timeout=COMMAND_TIMEOUT_SECONDS, capture=False)
    except RunnerError as exc:
        LOG.warning("Setting the clock failed: %s", exc)
        _reply(channel, False, "The clock could not be set.")
        return
    shown = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))
    if not hardware.rtc_write_allowed(clock):
        _reply(channel, True, f"The clock was set to {shown} UTC. The hardware clock was left alone because it may keep local time.")
        return
    try:
        runner.run(("hwclock", "--systohc"), timeout=COMMAND_TIMEOUT_SECONDS, capture=False)
    except RunnerError as exc:
        LOG.warning("Saving the time to the hardware clock failed: %s", exc)
        _reply(channel, True, f"The clock was set to {shown} UTC, but it could not be saved to the hardware clock, so it will be wrong again after a restart.")
        return
    _reply(channel, True, f"The clock was set to {shown} UTC and saved to the hardware clock.")


# ----------------------------------------------------------------------------------------------- helpers


def _refuse_while_busy(channel: Channel) -> None:
    manager: Any = channel.install_manager
    if manager is not None and manager.is_running():
        raise ActionError("The clock cannot be changed while an installation is running.", ErrorCode.BUSY)
    if _repair_running(channel):
        raise ActionError("The clock cannot be changed while a repair is running.", ErrorCode.BUSY)


def _repair_running(channel: Channel) -> bool:
    """Whether the repair module has an operation running (a step such as emerge or grub-install in a chroot).

    A repair session that only holds a mounted system does not count: the repair may need the right clock first."""
    registry = getattr(getattr(channel, "server", None), "registry", None)
    if registry is None:
        return False
    try:
        summary = registry.status_extensions().get("rescue")
        return bool(summary.get("running")) if isinstance(summary, dict) else False
    except Exception:  # noqa: BLE001 - a repair module that cannot answer must not stop the clock from being fixed
        LOG.exception("Could not ask the repair module whether an operation is running")
        return False


def _read_tracking(runner: Runner) -> tuple[bool | None, float | None]:
    """(chrony follows a time server, seconds the clock is off) from `chronyc -c tracking`; (None, None) when chronyd does not answer."""
    try:
        text = runner.run(("chronyc", "-c", "tracking"), timeout=TRACKING_TIMEOUT_SECONDS).stdout
    except RunnerError:
        return None, None
    return hardware.parse_chrony_tracking(text)


def _wait_for_sync(runner: Runner) -> str:
    """"" once chrony follows a time server and the clock is within SYNCED_WITHIN_SECONDS of it; otherwise what is wrong."""
    synced: bool | None = None
    offset: float | None = None
    for attempt in range(SYNC_POLLS):
        if attempt:
            _sleep(SYNC_POLL_SECONDS)
        synced, offset = _read_tracking(runner)
        if synced and (offset or 0.0) <= SYNCED_WITHIN_SECONDS:
            return ""
    if synced:
        return f"The network was asked for the time, but the clock is still off by about {int(offset or 0)} seconds. Try again in a minute, or set the date and time by hand."
    return ("The network was asked for the time, but no time server has answered yet. Check the network connection (a firewall can block time servers), "
            "then try again in a minute, or set the date and time by hand.")


def _reply(channel: Channel, ok: bool, message: str) -> None:
    channel.send(Event("result", message=message, data={"ok": ok, "message": message}))
    channel.send(completed_event(ok, message))
