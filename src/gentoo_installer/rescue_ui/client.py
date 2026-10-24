"""Qt side of the ``rescue.*`` actions: one worker thread per request, the answer as signals on the GUI thread.

``Call`` wraps ``BackendWorker`` (the same worker the installer uses). It adds what the repair window needs: it remembers the
operation's ``job_id`` and last ``seq`` so a lost connection can be picked up again with ``rescue.events``; it tells a request
that was answered from one that lost its connection (an OSError *or* a stream that simply ended without ``completed``); it keeps
the last ``result`` even when an ``error`` follows it (``rescue.unmount`` reports the busy holders that way); and it can be
aborted, so closing the window never leaves a thread blocked on a socket.

Nothing here logs a request: it may carry a passphrase or a password.
"""

from __future__ import annotations

import socket
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal

from .. import daemon_client
from ..backend import BackendWorker
from . import messages
from .model import event_data


class _Stream(BackendWorker):
    """A BackendWorker whose blocking read can be ended from the GUI thread."""

    def abort(self) -> None:
        self.requestInterruption()
        sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class Call(QObject):
    """One request to the daemon. Connect the signals right after creating it; they are delivered from the event loop."""

    eventReceived = Signal(dict)  # every event of the stream, in order (not called ``event``: that would hide QObject.event)
    succeeded = Signal(dict)  # the last ``result`` payload ({} when there was none)
    failed = Signal(str, str, dict)  # machine code, text for the person, the error event's ``data``
    lost = Signal(str)  # no ``completed`` event arrived: the connection broke or the daemon closed it
    finished = Signal()  # always, once, after one of the three signals above

    def __init__(self, socket_path: str, action: str, params: dict[str, Any] | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.action = action
        self.socket_path = socket_path
        self.job_id = ""
        self.last_seq = 0
        self.session_id = str((params or {}).get("session_id", ""))
        self.result: dict[str, Any] = {}
        self.error: dict[str, Any] | None = None
        self.completed: dict[str, Any] | None = None
        self.received = 0
        self.aborted = False
        self.done = False
        self._failure = ""
        self._worker = _Stream(None, socket_path, self, request={"action": action, **(params or {})})
        self._worker.eventReceived.connect(self._on_event)
        self._worker.connectionFailed.connect(self._on_connection_failed)
        self._worker.finished.connect(self._on_worker_finished)

    # ---- starting and stopping -------------------------------------------------------------------------------------------------------
    def start(self) -> "Call":
        self._worker.start()
        return self

    def abort(self) -> None:
        """Close the connection (the operation itself goes on in the daemon). The signals stay silent."""
        self.aborted = True
        self._worker.abort()

    def wait(self, msecs: int = 3000) -> bool:
        try:
            return bool(self._worker.wait(msecs))
        except RuntimeError:  # the thread object was already deleted: the call is over
            return True

    @property
    def connected(self) -> bool:
        """False when not a single event arrived: the daemon could not be reached at all."""
        return self.received > 0

    def is_running(self) -> bool:
        return not self.done

    def then(self, ok: Callable[..., Any] | None = None, fail: Callable[..., Any] | None = None, lost: Callable[..., Any] | None = None) -> "Call":
        if ok is not None:
            self.succeeded.connect(ok)
        if fail is not None:
            self.failed.connect(fail)
        if lost is not None:
            self.lost.connect(lost)
        return self

    # ---- the stream ------------------------------------------------------------------------------------------------------------------
    def _on_event(self, event: dict[str, Any]) -> None:
        self.received += 1
        if event.get("job_id") and not self.job_id:
            self.job_id = str(event["job_id"])
        if event.get("seq"):
            self.last_seq = max(self.last_seq, int(event["seq"]))
        kind = event.get("type")
        data = event_data(event)
        if kind == "result":
            self.result = data
        elif kind == "error":
            self.error = event
        elif kind == "completed":
            self.completed = event
        self.eventReceived.emit(event)

    def _on_connection_failed(self, message: str) -> None:
        self._failure = message

    def _on_worker_finished(self) -> None:
        self.done = True
        self._worker.request = None  # a passphrase or password must not outlive the request
        if not self.aborted:
            self._report()
        self.finished.emit()
        # No deleteLater() here: deleting a finished Call from the event loop crashed PySide now and then (about 3 in 100 test runs, always
        # inside ~QObject). A finished call is a few hundred bytes; the client owns it and frees it when the window is gone.

    def _report(self) -> None:
        if self.completed is None:
            self.lost.emit(self._failure or messages.CONNECTION_LOST_TEXT)
            return
        if event_data(self.completed).get("success", True) and self.error is None:
            self.succeeded.emit(self.result)
            return
        failure = self.error or self.completed
        details = event_data(failure)
        code = str(details.get("code") or failure.get("error") or "rescue.internal")
        text = messages.friendly_error(code, str(failure.get("message") or failure.get("error") or ""), details)
        self.failed.emit(code, text, details)


class ProbeCall(QThread):
    """``daemon_client.probe`` (status and capabilities) off the GUI thread. Never raises."""

    done = Signal(object)  # a daemon_client.DaemonState

    def __init__(self, socket_path: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.socket_path = socket_path

    def run(self) -> None:
        self.done.emit(daemon_client.probe(self.socket_path))


_STRANDED: list[QObject] = []  # calls whose thread outlived the window: Qt aborts the program when a running QThread is deleted


class RescueClient(QObject):
    """Creates calls, keeps them alive until they end and ends them when the window closes."""

    def __init__(self, socket_path: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.socket_path = socket_path
        self.stopping = False  # set by abort_all(): nothing new is started after that
        self._calls: list[Call] = []
        self._probes: list[ProbeCall] = []

    def request(self, action: str, params: dict[str, Any] | None = None) -> Call:
        call = Call(self.socket_path, action, params, self)
        if self.stopping:  # the window is closing: answer nothing, start nothing
            call.aborted = call.done = True
            return call
        self._calls.append(call)
        call.finished.connect(lambda c=call: self._forget(c))
        return call.start()

    def probe(self) -> ProbeCall:
        probe = ProbeCall(self.socket_path, self)
        if not self.stopping:
            self._probes.append(probe)
            probe.start()
        return probe

    def _forget(self, call: Call) -> None:
        """A finished call is no longer active (it stays alive as a child of the client)."""
        if call in self._calls:
            self._calls.remove(call)

    @property
    def busy(self) -> bool:
        return any(call.is_running() for call in self._calls)

    def abort_all(self) -> None:
        self.stopping = True
        for call in list(self._calls):
            call.abort()

    def wait_all(self, msecs: int = 3000) -> bool:
        """Wait for every thread to end (after abort_all). True when none is left; a thread that does not end is kept alive, outside the window."""
        ended = True
        for call in list(self._calls):
            if not call.wait(msecs):
                ended = False
                call.setParent(None)
                _STRANDED.append(call)
        for probe in list(self._probes):
            if not probe.wait(msecs):
                ended = False
                probe.setParent(None)
                _STRANDED.append(probe)
        return ended
