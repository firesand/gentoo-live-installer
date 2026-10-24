"""The installer daemon as the console installer uses it.

A thin layer over ``gentoo_installer.daemon_client`` (the same blocking client the graphical
installer uses). One request per connection, JSON lines back; docs/protocol.md has the actions.
"""

from __future__ import annotations

from typing import Any, Iterator

from .. import daemon_client
from ..daemon_client import DaemonError, DaemonState, event_data

# The daemon sends no heartbeat, so a quiet build is not a dead connection. After this many silent
# seconds the follower asks the daemon whether the job still runs and re-attaches.
STREAM_READ_TIMEOUT = 90.0
CONNECT_TIMEOUT = 10.0

__all__ = ["Daemon", "DaemonError", "DaemonState", "STREAM_READ_TIMEOUT"]


class Daemon:
    """One socket path and the few requests the console installer makes."""

    def __init__(self, socket_path: str = daemon_client.DEFAULT_SOCKET) -> None:
        self.socket_path = socket_path

    def probe(self) -> DaemonState:
        """Ask for the status and the advertised features. Never raises."""
        return daemon_client.probe(self.socket_path, timeout=3.0)

    def status(self) -> dict[str, Any]:
        """The daemon's status data. Raises OSError when it cannot be reached."""
        return daemon_client.status(self.socket_path, timeout=3.0)

    def inspect(self, proxy: str = "") -> dict[str, Any]:
        """The ``inspect`` reply: facts about this computer. Raises OSError (not reachable) or DaemonError.

        The proxy, when given, is sent so the network check goes through it; a daemon that does not
        know that is asked again without it."""
        attempts: list[dict[str, Any] | None] = [None]
        if proxy:
            attempts.insert(0, {"manifest": {"network": {"proxy": proxy}}})
        error: tuple[str, str] | None = None
        for payload in attempts:
            events = daemon_client.request(self.socket_path, "inspect", payload, timeout=15.0, max_seconds=40.0)
            for event in events:
                if event.get("type") == "status" and "architecture" in event_data(event):
                    return event_data(event)
            error = daemon_client.first_error(events)
        raise DaemonError(error[0] if error else "The installer service did not describe this computer.", error[1] if error else "")

    def action(self, name: str, **arguments: Any) -> tuple[bool, str]:
        """Run a short action (``sync_time``, ``set_time``): (ok, message). Raises OSError when not reachable."""
        events = daemon_client.request(self.socket_path, name, arguments, timeout=30.0, max_seconds=90.0)
        error = daemon_client.first_error(events)
        if error:
            return False, error[0]
        for event in events:
            if event.get("type") == "result":
                data = event_data(event)
                return bool(data.get("ok", True)), str(data.get("message") or event.get("message") or "")
        return True, ""

    def stream(self, request: dict[str, Any], *, read_timeout: float = STREAM_READ_TIMEOUT) -> Iterator[dict[str, Any]]:
        """Send ``request`` (start, attach) and yield events until the daemon closes the connection.

        Raises OSError when it cannot connect or when it is silent for ``read_timeout`` seconds."""
        return daemon_client.stream(self.socket_path, request, timeout=CONNECT_TIMEOUT, read_timeout=read_timeout)

    def cancel(self) -> tuple[bool, str]:
        return daemon_client.request_cancel(self.socket_path)

    def log_lines(self, tail: int = 0) -> list[str]:
        """The redacted installation log (the last ``tail`` lines when given). Raises DaemonError or OSError."""
        return list(daemon_client.fetch_log(self.socket_path, tail=tail))
