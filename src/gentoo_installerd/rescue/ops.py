"""Operations: long-running work on a manager-owned thread with a sequenced event buffer (review R6).

A request handler only *streams* an operation's events; the work itself never depends on the connection. A GUI that stalls
for 30 s, crashes or reconnects loses nothing: the events stay in a ring buffer that always keeps stage, error, result and
completed events, and ``rescue.events`` replays them after a given sequence number.
"""

from __future__ import annotations

import secrets
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from gentoo_installerd.events import Event
from gentoo_installerd.runner import InstallCancelled

from .errors import RescueError

ESSENTIAL = frozenset({"stage", "error", "result", "completed"})
DEFAULT_LIMIT = 5000


@dataclass(frozen=True)
class RescueEvent(Event):
    """An ``Event`` with the per-job sequence number docs/protocol.md specifies."""

    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        if self.seq:
            result["seq"] = self.seq
        else:
            result.pop("seq", None)  # an event outside a job has no sequence number (docs/protocol.md)
        return result


class EventBuffer:
    def __init__(self, limit: int = DEFAULT_LIMIT) -> None:
        self.limit = limit
        self._events: list[RescueEvent] = []
        self._seq = 0
        self._done = False
        self._cond = threading.Condition()

    def append(self, make: Callable[[int], RescueEvent]) -> RescueEvent:
        with self._cond:
            self._seq += 1
            event = make(self._seq)
            self._events.append(event)
            if len(self._events) > self.limit:
                self._evict()
            self._cond.notify_all()
            return event

    def _evict(self) -> None:
        excess = len(self._events) - self.limit
        kept: list[RescueEvent] = []
        for event in self._events:
            if excess > 0 and event.type not in ESSENTIAL:
                excess -= 1
                continue
            kept.append(event)
        self._events = kept

    def close(self) -> None:
        with self._cond:
            self._done = True
            self._cond.notify_all()

    @property
    def last_seq(self) -> int:
        with self._cond:
            return self._seq

    @property
    def done(self) -> bool:
        return self._done

    def since(self, after_seq: int) -> list[RescueEvent]:
        with self._cond:
            return [event for event in self._events if event.seq > after_seq]

    def wait(self, after_seq: int, timeout: float) -> tuple[list[RescueEvent], bool]:
        with self._cond:
            if not any(event.seq > after_seq for event in self._events) and not self._done:
                self._cond.wait(timeout)
            return [event for event in self._events if event.seq > after_seq], self._done


class Operation:
    """One streaming request: an id, a worker thread, a buffer and a cancel flag checked between steps."""

    def __init__(self, kind: str, session_id: str = "", request_id: str = "", *, limit: int = DEFAULT_LIMIT, clock: Callable[[], float] = time.time) -> None:
        self.id = "o" + secrets.token_hex(5)
        self.kind = kind
        self.session_id = session_id
        self.request_id = request_id
        self.buffer = EventBuffer(limit)
        self.cancel_requested = threading.Event()
        self.force_cancel = threading.Event()
        self.thread: threading.Thread | None = None
        self.success: bool | None = None
        self.result: dict[str, Any] = {}
        self.started = clock()
        self.finished: float | None = None
        self.clock = clock
        self._finish_lock = threading.Lock()

    # ---- producing events --------------------------------------------------------------------------------------------
    def _data(self, data: dict[str, Any] | None) -> dict[str, Any]:
        merged = dict(data or {})
        if self.session_id:
            merged.setdefault("session_id", self.session_id)
        if self.request_id:
            merged.setdefault("request_id", self.request_id)
        return merged

    def emit(self, type: str, message: str = "", *, stage: str = "", progress: float | None = None, data: dict[str, Any] | None = None, error: str = "") -> RescueEvent:
        payload = self._data(data)
        return self.buffer.append(lambda seq: RescueEvent(type, job_id=self.id, stage=stage, message=message, progress=progress, data=payload, error=error, seq=seq))

    def log(self, line: str) -> None:
        self.emit("log", line)

    def stage(self, name: str, message: str = "", progress: float | None = None, **data: Any) -> None:
        self.emit("stage", message or name, stage=name, progress=progress, data=data)

    def progress(self, message: str, progress: float | None = None, **data: Any) -> None:
        self.emit("progress", message, progress=progress, data=data)

    def publish(self, payload: dict[str, Any], message: str = "") -> None:
        self.result = payload
        self.emit("result", message, data=payload)

    def fail(self, error: RescueError | Exception) -> None:
        if isinstance(error, RescueError):
            self.emit("error", error.message, error=error.code, data={**error.to_data()})
        else:
            self.emit("error", "Internal error in the rescue engine", error="proto.internal", data={"code": "proto.internal"})

    def complete(self, success: bool, message: str = "") -> None:
        with self._finish_lock:
            if self.success is not None:
                return
            self.success = success
            self.finished = self.clock()
        self.emit("completed", message or ("Done" if success else "Failed"), progress=100.0 if success else None, data={"success": success})
        self.buffer.close()

    # ---- running -----------------------------------------------------------------------------------------------------------
    def run(self, work: Callable[[Operation], None]) -> None:
        """Run ``work`` in this thread; every outcome ends with exactly one ``completed`` event."""
        try:
            work(self)
            self.complete(True)
        except RescueError as exc:
            self.fail(exc)
            self.complete(False, exc.message)
        except InstallCancelled:
            cancelled = RescueError("cancelled", "The operation was cancelled")
            self.fail(cancelled)
            self.complete(False, cancelled.message)
        except Exception as exc:  # never lets an OSError/KeyError kill the handler thread silently
            self.emit("log", traceback.format_exc(limit=6))
            self.fail(exc)
            self.complete(False, "Internal error")

    def start(self, work: Callable[[Operation], None]) -> Operation:
        self.thread = threading.Thread(target=self.run, args=(work,), name=f"rescue-{self.kind}-{self.id}", daemon=True)
        self.thread.start()
        return self

    def wait(self, timeout: float | None = None) -> bool:
        if self.thread is not None:
            self.thread.join(timeout)
            return not self.thread.is_alive()
        return self.buffer.done

    def request_cancel(self, *, force: bool = False) -> None:
        self.cancel_requested.set()
        if force:
            self.force_cancel.set()

    # ---- consuming ---------------------------------------------------------------------------------------------------------
    def stream(self, send: Callable[[RescueEvent], None], after_seq: int = 0, poll: float = 0.5) -> None:
        """Replay buffered events after ``after_seq`` and follow live ones until the operation completed."""
        cursor = after_seq
        while True:
            events, done = self.buffer.wait(cursor, poll)
            for event in events:
                send(event)
                cursor = event.seq
            if done and not events:
                return

    def summary(self) -> dict[str, Any]:
        return {"job_id": self.id, "kind": self.kind, "session_id": self.session_id, "running": self.success is None, "success": self.success, "seq": self.buffer.last_seq,
                "started": self.started, "finished": self.finished}


class OperationTable:
    """The most recent operations, so a client can reattach to one by id or by session."""

    def __init__(self, keep: int = 40) -> None:
        self.keep = keep
        self._items: dict[str, Operation] = {}
        self._lock = threading.Lock()

    def add(self, op: Operation) -> Operation:
        with self._lock:
            self._items[op.id] = op
            finished = [key for key, item in self._items.items() if item.success is not None]
            while len(self._items) > self.keep and finished:
                self._items.pop(finished.pop(0), None)
        return op

    def get(self, op_id: str) -> Operation | None:
        return self._items.get(op_id)

    def latest(self, session_id: str = "") -> Operation | None:
        with self._lock:
            items = [op for op in self._items.values() if not session_id or op.session_id == session_id]
        return items[-1] if items else None

    def running(self) -> list[Operation]:
        return [op for op in list(self._items.values()) if op.success is None]
