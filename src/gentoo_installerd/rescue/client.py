"""Clients of the rescue API: the daemon socket, or the same API in this process (``--local``). No Qt, standard library only."""

from __future__ import annotations

import json
import os
import queue
import select
import signal
import socket
import struct
import sys
import termios
import threading
import tty
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Protocol

from .api import RescueApi
from .errors import RescueError

DEFAULT_SOCKET = "/run/gentoo-installer/server.sock"
OLD_DAEMON = ("Action must be", "Unknown action", "proto.unsupported_action")


class RescueClientError(Exception):
    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}

    @property
    def short(self) -> str:
        return self.code.removeprefix("rescue.")


class Transport(Protocol):
    def call(self, request: dict[str, Any]) -> Iterator[dict[str, Any]]: ...

    def shell(self, request: dict[str, Any]) -> tuple[dict[str, Any], int]: ...


class SocketTransport:
    """One JSON request line per connection, JSON-lines events back (docs/protocol.md)."""

    def __init__(self, path: str = DEFAULT_SOCKET, timeout: float | None = None) -> None:
        self.path = path
        self.timeout = timeout

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout:
            sock.settimeout(self.timeout)
        sock.connect(self.path)
        return sock

    def available(self) -> bool:
        try:
            self._connect().close()
        except OSError:
            return False
        return True

    def call(self, request: dict[str, Any]) -> Iterator[dict[str, Any]]:
        sock = self._connect()
        try:
            sock.sendall((json.dumps({"version": 1, **request}) + "\n").encode())
            buffer = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        event = json.loads(line)
                        yield event
                        if event.get("type") == "completed":
                            return
        finally:
            sock.close()

    def shell(self, request: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """Send rescue.shell and receive the PTY master the daemon passes with its reply (SCM_RIGHTS)."""
        sock = self._connect()
        try:
            sock.sendall((json.dumps({"version": 1, **request}) + "\n").encode())
            message, fds, _flags, _addr = socket.recv_fds(sock, 65536, 4)
        finally:
            sock.close()
        line = message.split(b"\n", 1)[0]
        event = json.loads(line) if line.strip() else {}
        if event.get("type") == "error" or not fds:
            for fd in fds:
                os.close(fd)
            raise RescueClientError((event.get("data") or {}).get("code", event.get("error", "rescue.internal")), event.get("message", "The daemon did not start a terminal"), event.get("data"))
        for extra in fds[1:]:
            os.close(extra)
        return event.get("data", {}), fds[0]


class _QueueChannel:
    """Collects the events of an in-process dispatch."""

    def __init__(self, sink: queue.Queue[Any]) -> None:
        self.sink = sink
        self.peer = type("Peer", (), {"pid": os.getpid(), "uid": os.geteuid(), "gid": os.getegid()})()
        self.closed = False

    def send(self, event: Any) -> None:
        self.sink.put(event.to_dict())

    def send_with_fds(self, event: Any, fds: Any) -> None:  # the console form of the terminal never goes through here
        self.sink.put(event.to_dict())


class LocalTransport:
    """Run the API inside this process (root, no daemon); separate invocations find saved sessions again."""

    def __init__(self, api: RescueApi) -> None:
        self.api = api

    def call(self, request: dict[str, Any]) -> Iterator[dict[str, Any]]:
        sink: queue.Queue[Any] = queue.Queue()
        name = str(request["action"])
        handler = self.api.handler(name)
        done = threading.Event()

        def run() -> None:
            try:
                handler({"version": 1, **request}, _QueueChannel(sink))
            finally:
                done.set()
                sink.put(None)

        threading.Thread(target=run, name="rescue-local", daemon=True).start()
        while True:
            event = sink.get()
            if event is None:
                return
            yield event

    def shell(self, request: dict[str, Any]) -> tuple[dict[str, Any], int]:
        try:
            status = self.api.manager.run_shell_here(request)
        except RescueError as exc:  # the daemon form reports these as error events; the console form must not end in a traceback
            raise rescue_error_to_client(exc) from exc
        return {"exit": status}, -1


@dataclass
class Outcome:
    result: dict[str, Any]
    events: list[dict[str, Any]]


class RescueClient:
    def __init__(self, transport: Transport, on_event: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.transport = transport
        self.on_event = on_event or (lambda _event: None)

    def request(self, action: str, **params: Any) -> Outcome:
        """Send one request and collect the stream; raises RescueClientError when the daemon reports a failure."""
        events: list[dict[str, Any]] = []
        result: dict[str, Any] = {}
        failure: dict[str, Any] | None = None
        success: bool | None = None
        try:
            for event in self.transport.call({"action": action, **params}):
                events.append(event)
                self.on_event(event)
                kind = event.get("type")
                if kind == "result":
                    result = event.get("data", {})
                elif kind == "status" and not result and event.get("data"):
                    result = event.get("data", {})
                elif kind == "error":
                    failure = event
                elif kind == "completed":
                    success = bool(event.get("data", {}).get("success", True))
        except (ConnectionRefusedError, FileNotFoundError, PermissionError) as exc:
            raise RescueClientError("rescue.disabled", f"The installer daemon is not reachable: {exc}") from exc
        if failure is not None or success is False:
            event = failure or {}
            message = event.get("message") or event.get("error") or "The operation failed"
            code = (event.get("data") or {}).get("code") or event.get("error") or "rescue.internal"
            if any(marker in str(message) for marker in OLD_DAEMON) and not str(code).startswith("rescue."):
                raise RescueClientError("rescue.disabled", "This backend does not support repair mode (the daemon is older than the installer, or rescue is disabled).")
            raise RescueClientError(code, message, event.get("data"))
        if success is None and not events:
            raise RescueClientError("rescue.disabled", "The daemon closed the connection without an answer.")
        return Outcome(result, events)

    def result(self, action: str, **params: Any) -> dict[str, Any]:
        return self.request(action, **params).result


# ---- the terminal on the client side (socket mode) ------------------------------------------------------------------------------------------------
def attach_terminal(master: int, stdin_fd: int = 0, stdout_fd: int = 1) -> int:
    """Copy keystrokes to the daemon's PTY and its output to the screen until the shell exits; returns the exit code (0)."""
    interactive = os.isatty(stdin_fd)
    saved = termios.tcgetattr(stdin_fd) if interactive else None
    previous = signal.getsignal(signal.SIGWINCH)

    def resize(*_args: object) -> None:
        if not interactive:
            return
        packed = struct.pack("HHHH", 0, 0, 0, 0)
        import fcntl

        try:
            size = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, packed)
            fcntl.ioctl(master, termios.TIOCSWINSZ, size)
        except OSError:
            pass

    try:
        if interactive:
            tty.setraw(stdin_fd)
            signal.signal(signal.SIGWINCH, resize)
            resize()
        open_input = True
        while True:
            readers = [master] + ([stdin_fd] if open_input else [])
            ready, _w, _x = select.select(readers, [], [], 0.5)
            if master in ready:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
                os.write(stdout_fd, data)
            if stdin_fd in ready:
                data = os.read(stdin_fd, 4096)
                if not data:
                    open_input = False
                else:
                    os.write(master, data)
    finally:
        if saved is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
            signal.signal(signal.SIGWINCH, previous)
        os.close(master)
    return 0


def rescue_error_to_client(exc: RescueError) -> RescueClientError:
    return RescueClientError(exc.code, exc.message, exc.to_data())


def stdout_is_tty() -> bool:
    return sys.stdout.isatty()
