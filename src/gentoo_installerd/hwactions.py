"""Hardware actions of the daemon (feature ``hardware.v1``): put the clock right before an install.

A PC with a dead clock battery starts in the wrong year and cannot verify a single HTTPS certificate, and a
PC without a network cannot ask for the time. These two actions fix the clock from the installer window:

* ``sync_time`` (no arguments) asks the network, with chrony.
* ``set_time`` (``{"utc": "YYYY-MM-DD HH:MM:SS"}``) sets it by hand; the hardware clock is written too, but only
  when it is known to keep UTC (a Windows dual-boot PC keeps local time there and must not be touched).

Each reply is one ``result`` event with ``data = {"ok": bool, "message": str}`` followed by ``completed``.
Neither action runs while an installation is running: a clock that jumps under emerge breaks timestamps.
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
    _refuse_while_installing(channel)
    clock = read_clock()
    runner = make_runner()
    try:
        runner.run(hardware.sync_time_argv(clock), timeout=COMMAND_TIMEOUT_SECONDS, capture=False)
    except RunnerError as exc:
        LOG.warning("Setting the clock from the network failed: %s", exc)
        _reply(channel, False, "The clock could not be set from the network. Check the network connection, or set the date and time by hand.")
        return
    offset = read_clock().offset_s
    if offset is not None and offset > SYNCED_WITHIN_SECONDS:
        _reply(channel, False, f"The network was asked for the time, but the clock is still off by about {int(offset)} seconds. Try again in a minute, or set the date and time by hand.")
        return
    _reply(channel, True, "The clock was set from the network.")


def handle_set_time(request: dict, channel: Channel) -> None:
    text = request.get("utc")
    epoch = hardware.parse_set_time(text) if isinstance(text, str) else None
    if epoch is None:
        raise ActionError("Enter the date and time in UTC as YYYY-MM-DD HH:MM:SS, with a year from 2020 to 2100.", ErrorCode.BAD_REQUEST)
    _refuse_while_installing(channel)
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


def _refuse_while_installing(channel: Channel) -> None:
    manager: Any = channel.install_manager
    if manager is not None and manager.is_running():
        raise ActionError("The clock cannot be changed while an installation is running.", ErrorCode.BUSY)


def _reply(channel: Channel, ok: bool, message: str) -> None:
    channel.send(Event("result", message=message, data={"ok": ok, "message": message}))
    channel.send(completed_event(ok, message))
