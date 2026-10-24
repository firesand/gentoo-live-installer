from __future__ import annotations

import argparse
import errno
import fcntl
import grp
import importlib
import importlib.util
import itertools
import json
import logging
import os
import queue
import re
import select
import signal
import socket
import socketserver
import stat
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from gentoo_installer.manifest import InstallManifest, ManifestError

from . import __version__
from .events import PROTECTED_EVENT_TYPES, PROTOCOL_VERSION, ErrorCode, Event, completed_event, error_event
from .inspect import Discovery
from .install import STAGE_WEIGHTS, STAGES, Installer, tools_for_selection
from .runner import SubprocessRunner, redact_text

LOG = logging.getLogger(__name__)

DEFAULT_SOCKET = "/run/gentoo-installer/server.sock"
MAX_REQUEST_BYTES = 1024 * 1024
SOCKET_GROUP = "gentoo-installer"
# Applies only while the request line is read. Streams must survive a client that stops reading.
REQUEST_TIMEOUT = 30.0
# A client that has not read a single byte for this long is dropped (the install keeps running
# and the client can attach again).
SEND_TIMEOUT = 600.0
# How often a quiet stream checks that its client and its job are still there.
STREAM_POLL_SECONDS = 15.0
# Per-job event history for attach: at least this many recent events; stage, error and
# completed events are kept for the whole job.
HISTORY_EVENTS = 5000
PROTECTED_HISTORY_EVENTS = 20000
# Events a slow client may fall behind before the oldest log lines are dropped for it.
LISTENER_CAPACITY = 20000
LOG_LINES_PER_EVENT = 1000
# How long a shutdown waits for a cancelled installation to clean up.
SHUTDOWN_GRACE_SECONDS = 60.0
SHUTDOWN_HOOK_SECONDS = 30.0
# Socket parents that must never be taken over (chown/chmod) as a private directory.
FORBIDDEN_SOCKET_PARENTS = frozenset({"/", "/run", "/var/run", "/tmp", "/var/tmp", "/etc", "/usr", "/var", "/home", "/root", "/dev", "/dev/shm", "/mnt", "/opt", "/srv"})
# Optional modules that add actions: (module, function). Each is called with the registry.
OPTIONAL_PLUGINS = (("gentoo_installerd.hwactions", "register"), ("gentoo_installerd.rescue.api", "register"))


# ------------------------------------------------------------------ errors


class ActionError(RuntimeError):
    """A request failed in an expected way; ``code`` is the namespaced machine-readable reason."""

    code = ErrorCode.INTERNAL

    def __init__(self, message: str, code: str | None = None, **data: Any) -> None:
        super().__init__(message)
        if code:
            self.code = code
        self.data = data


class InstallBusy(ActionError):
    code = ErrorCode.BUSY


class InstallNotRunning(ActionError):
    code = ErrorCode.NOT_RUNNING


class ChannelClosed(ConnectionError):
    """The peer left (or stopped reading for too long): nothing more can be sent."""


# ------------------------------------------------------------------ channel


@dataclass(frozen=True)
class PeerCredentials:
    pid: int
    uid: int
    gid: int


class Channel:
    """One client connection, as seen by an action handler."""

    def __init__(self, connection: socket.socket, *, server: Any = None, request: dict | None = None) -> None:
        self._sock = connection
        self._lock = threading.Lock()
        self._closed = False
        self._peer: PeerCredentials | None = None
        self.server = server
        self.request: dict = request if request is not None else {}
        self.request_id: str | int | None = None
        self.completed_sent = False

    # -- sending

    def _prepare(self, event: Event) -> bytes:
        if self.request_id not in (None, ""):
            event = event.with_data(request_id=self.request_id)
        return (event.to_json() + "\n").encode("utf-8")

    def send(self, event: Event) -> None:
        """Send one event. Raises ChannelClosed when the peer left or stopped reading."""
        data = self._prepare(event)
        with self._lock:
            if self._closed:
                raise ChannelClosed("The client disconnected")
            try:
                self._sock.sendall(data)
            except (OSError, TimeoutError) as exc:  # EPIPE, ECONNRESET, a send timeout, a closed socket
                self._closed = True
                raise ChannelClosed(f"The client disconnected: {exc}") from exc
        if event.type == "completed":
            self.completed_sent = True

    def send_with_fds(self, event: Event, fds: Sequence[int]) -> None:
        """Send one event with file descriptors attached (SCM_RIGHTS)."""
        data = self._prepare(event)
        with self._lock:
            if self._closed:
                raise ChannelClosed("The client disconnected")
            try:
                sent = socket.send_fds(self._sock, [data], list(fds))
                if sent < len(data):
                    self._sock.sendall(data[sent:])
            except (OSError, TimeoutError) as exc:
                self._closed = True
                raise ChannelClosed(f"The client disconnected: {exc}") from exc
        if event.type == "completed":
            self.completed_sent = True

    # -- state

    @property
    def peer(self) -> PeerCredentials:
        """pid, uid and gid of the client process (SO_PEERCRED)."""
        if self._peer is None:
            raw = self._sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            self._peer = PeerCredentials(*struct.unpack("3i", raw))
        return self._peer

    @property
    def closed(self) -> bool:
        """True once a send failed or the client closed the connection.

        A client that only shut down its writing side (it sent its request and waits for events)
        is not closed: that is reported by POLLHUP only when both directions are gone.
        """
        if self._closed:
            return True
        try:
            fd = self._sock.fileno()
            if fd < 0:
                self._closed = True
                return True
            poller = select.poll()
            poller.register(fd, select.POLLHUP | select.POLLERR)
            if any(events & (select.POLLHUP | select.POLLERR | select.POLLNVAL) for _fd, events in poller.poll(0)):
                self._closed = True
        except (OSError, ValueError):
            self._closed = True
        return self._closed

    def close(self) -> None:
        self._closed = True

    @property
    def install_manager(self) -> InstallManager | None:
        return getattr(self.server, "manager", None)


Handler = Callable[[dict, Channel], None]


# ------------------------------------------------------------------ registry


class ActionRegistry:
    """Actions, features, shutdown hooks and status extensions of one daemon."""

    def __init__(self, *, builtins: bool = True) -> None:
        self._lock = threading.RLock()
        self._actions: dict[str, Handler] = {}
        self._action_features: dict[str, str] = {}
        self._features: dict[str, None] = {}
        self._hooks: list[Callable[[], None]] = []
        self._status_providers: dict[str, Callable[[], Any]] = {}
        self._capability_providers: dict[str, Callable[[], Any]] = {}
        self.install_manager: InstallManager | None = None
        self.options: Any = None  # the parsed command line, for plugins that have flags of their own
        if builtins:
            register_builtin_actions(self)

    def register_action(self, name: str, handler: Handler, *, feature: str | None = None, replace: bool = False) -> None:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", name):
            raise ValueError(f"Invalid action name: {name!r}")
        if not callable(handler):
            raise TypeError("An action handler must be callable")
        with self._lock:
            if name in self._actions and self._actions[name] != handler and not replace:
                raise ValueError(f"Action {name!r} is already registered")
            self._actions[name] = handler
            if feature:
                self._action_features[name] = feature
                self._features.setdefault(feature, None)

    def unregister_action(self, name: str) -> None:
        with self._lock:
            self._actions.pop(name, None)
            self._action_features.pop(name, None)

    def add_feature(self, feature: str) -> None:
        with self._lock:
            self._features.setdefault(feature, None)

    def lookup(self, name: str) -> Handler | None:
        with self._lock:
            return self._actions.get(name)

    def actions(self) -> list[str]:
        with self._lock:
            return sorted(self._actions)

    def features(self) -> list[str]:
        with self._lock:
            return list(self._features)

    def add_shutdown_hook(self, hook: Callable[[], None]) -> None:
        with self._lock:
            self._hooks.append(hook)

    def run_shutdown_hooks(self, timeout: float = SHUTDOWN_HOOK_SECONDS) -> None:
        """Run every hook (last registered first). A failing or hanging hook never blocks the others."""
        with self._lock:
            hooks = list(reversed(self._hooks))
        for hook in hooks:
            failure: list[BaseException] = []

            def call(hook: Callable[[], None] = hook) -> None:
                try:
                    hook()
                except BaseException as exc:  # noqa: BLE001 - a hook must not stop the shutdown
                    failure.append(exc)

            worker = threading.Thread(target=call, name="gentoo-shutdown-hook", daemon=True)
            worker.start()
            worker.join(timeout)
            if worker.is_alive():
                LOG.error("Shutdown hook %r did not finish within %.0f s", hook, timeout)
            elif failure:
                LOG.error("Shutdown hook %r failed: %r", hook, failure[0])

    def add_status_provider(self, key: str, provider: Callable[[], Any]) -> None:
        """Add ``key: provider()`` to every ``status`` reply (the rescue manager's summary, for one)."""
        with self._lock:
            self._status_providers[key] = provider

    def add_capability_provider(self, key: str, provider: Callable[[], Any]) -> None:
        """Add ``key: provider()`` to the ``capabilities`` reply (what the rescue module can repair, for one)."""
        with self._lock:
            self._capability_providers[key] = provider

    @staticmethod
    def _collect(providers: dict[str, Callable[[], Any]], what: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, provider in providers.items():
            try:
                value = provider()
            except Exception:
                LOG.exception("%s provider %r failed", what, key)
                continue
            if value is not None:
                result[key] = value
        return result

    def status_extensions(self) -> dict[str, Any]:
        with self._lock:
            providers = dict(self._status_providers)
        return self._collect(providers, "Status")

    def capabilities(self) -> dict[str, Any]:
        features = self.features()
        if module_available("gentoo_installer.hardware") and "hardware.v1" not in features:
            features.append("hardware.v1")
        with self._lock:
            providers = dict(self._capability_providers)
        return {
            **self._collect(providers, "Capability"),
            "protocol": PROTOCOL_VERSION,
            "version": __version__,
            "features": features,
            "actions": self.actions(),
            "stages": stage_table(),
        }


def register_action(name: str, handler: Handler, *, feature: str | None = None) -> None:
    """Register ``handler`` for ``name`` (``feature`` is advertised by ``capabilities``)."""
    REGISTRY.register_action(name, handler, feature=feature)


def add_shutdown_hook(hook: Callable[[], None]) -> None:
    """Call ``hook`` when the daemon shuts down, after a running installation was cancelled."""
    REGISTRY.add_shutdown_hook(hook)


def module_available(name: str) -> bool:
    """Whether an optional module can be imported (without importing it)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def stage_table(weights: dict[str, int] | None = None) -> list[dict[str, Any]]:
    weights = weights or STAGE_WEIGHTS
    return [{"name": name, "weight": weights.get(name, 0)} for name in STAGES]


def load_plugins(registry: ActionRegistry, *, rescue: bool = True) -> list[str]:
    """Let optional modules (hardware actions, rescue) register their actions. Absent modules are skipped."""
    loaded: list[str] = []
    for module_name, function in OPTIONAL_PLUGINS:
        if not rescue and ".rescue" in module_name:
            continue
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            # The plugin (or its package) itself is absent: normal. A missing dependency of it is a bug.
            absent = bool(exc.name) and (module_name == exc.name or module_name.startswith(f"{exc.name}."))
            if absent:
                LOG.debug("Optional module %s is not installed", module_name)
            else:
                LOG.error("Optional module %s cannot be imported: %s", module_name, exc)
            continue
        except Exception:
            LOG.exception("Optional module %s failed to import", module_name)
            continue
        register = getattr(module, function, None)
        if not callable(register):
            continue
        try:
            api = register(registry)
        except Exception:
            LOG.exception("Optional module %s failed to register its actions", module_name)
            continue
        # A module that hands back an object with capabilities() (rescue does) shows up in the capabilities reply under its package name.
        if callable(getattr(api, "capabilities", None)):
            registry.add_capability_provider(module_name.split(".")[1], api.capabilities)
        loaded.append(module_name)
    return loaded


# ------------------------------------------------------------------ event delivery


class Listener:
    """The events of one job as one client sees them: bounded, and never blocking the job.

    ``None`` from ``get()`` means the stream is over; ``queue.Empty`` means "nothing yet".
    When the client falls ``capacity`` events behind, the oldest log lines (then progress
    updates) are dropped; stage, error and completed events are never dropped, and the client
    is told once how many lines it missed.
    """

    def __init__(self, capacity: int = LISTENER_CAPACITY, job_id: str = "") -> None:
        self.capacity = capacity
        self.job_id = job_id
        self._items: deque[Event] = deque()
        self._condition = threading.Condition()
        self._finished = False
        self._closed = False
        self._skipped = 0
        self.skipped_total = 0

    def prefill(self, events: Iterable[Event]) -> None:
        with self._condition:
            self._items.extend(events)
            self._condition.notify()

    def offer(self, event: Event) -> None:
        with self._condition:
            if self._closed:
                return
            self._items.append(event)
            if len(self._items) > self.capacity:
                self._shed()
            self._condition.notify()

    def _shed(self) -> None:
        for kind in ("log", "progress"):
            for index, item in enumerate(self._items):
                if item.type == kind:
                    del self._items[index]
                    if kind == "log":
                        self._skipped += 1
                        self.skipped_total += 1
                    return
        # Only events that must be kept: let the queue grow rather than lose one.

    def get(self, timeout: float | None = None) -> Event | None:
        with self._condition:
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                if self._skipped:
                    count, self._skipped = self._skipped, 0
                    return Event("log", job_id=self.job_id, message=f"{count} log lines were skipped because this client was reading too slowly; the full log is kept by the installer")
                if self._items:
                    return self._items.popleft()
                if self._finished or self._closed:
                    return None
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)

    def get_nowait(self) -> Event | None:
        return self.get(timeout=0)

    def finish(self) -> None:
        """No more events will be offered; ``get()`` ends the stream once the rest is read."""
        with self._condition:
            self._finished = True
            self._condition.notify_all()

    def close(self) -> None:
        """The consumer left. Always succeeds."""
        with self._condition:
            self._closed = True
            self._items.clear()
            self._condition.notify_all()

    def qsize(self) -> int:
        with self._condition:
            return len(self._items)


class EventHistory:
    """The events of the current (or last) job, for attach.

    The last ``limit`` events are kept whatever they are; older stage, error and completed
    events are kept as well, so a reconnecting client can always rebuild its progress page.
    """

    def __init__(self, limit: int = HISTORY_EVENTS, protected_limit: int = PROTECTED_HISTORY_EVENTS) -> None:
        self.limit = limit
        self._recent: deque[Event] = deque()
        self._protected: deque[Event] = deque(maxlen=protected_limit)

    def append(self, event: Event) -> None:
        self._recent.append(event)
        if len(self._recent) > self.limit:
            old = self._recent.popleft()
            if old.type in PROTECTED_EVENT_TYPES:
                self._protected.append(old)

    def replay(self, after_seq: int = 0) -> list[Event]:
        return [event for event in itertools.chain(self._protected, self._recent) if event.seq > after_seq]

    def __len__(self) -> int:
        return len(self._recent) + len(self._protected)


# ------------------------------------------------------------------ install manager


GENERIC_FAILURE = "Installation failed; inspect the streamed error and persistent log"


class InstallManager:
    """Runs at most one installation at a time and lets any number of clients follow it."""

    def __init__(
        self,
        target_root: Path,
        state_dir: Path,
        cache_dir: Path,
        *,
        busy_check: Callable[[], str | None] | None = None,
        history_events: int = HISTORY_EVENTS,
        listener_capacity: int = LISTENER_CAPACITY,
    ) -> None:
        self.target_root = target_root
        self.state_dir = state_dir
        self.cache_dir = cache_dir
        # Returns the reason an installation must not start now (for example a repair session
        # holding the disks), or a falsy value. The rescue manager installs it.
        self.busy_check = busy_check
        self.history_events = history_events
        self.listener_capacity = listener_capacity
        self.cancel = threading.Event()
        self.installer: Installer | None = None
        self.thread: threading.Thread | None = None
        self.status = "idle"
        self.listeners: list[Listener] = []
        self.lock = threading.RLock()
        self.history = EventHistory(history_events)
        self.stage_weights: dict[str, int] = dict(STAGE_WEIGHTS)
        self._reset_job_state()

    def _reset_job_state(self) -> None:
        self.last_error = ""
        self.last_error_stage = ""
        self.last_error_code = ""
        self.last_retryable: bool | None = None
        self.errors: list[dict[str, Any]] = []
        self.stage = ""
        self.message = ""
        self.progress: float | None = None
        self.seq = 0
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.cancel_requested = False
        self.job_id = ""
        self._completed: Event | None = None

    # -- starting and following

    def is_running(self) -> bool:
        thread = self.thread
        return thread is not None and thread.is_alive()

    def job_finished(self) -> bool:
        """True when no job thread is alive any more (a stream waiting for one should stop waiting)."""
        return not self.is_running()

    def start(self, manifest: InstallManifest, resume: bool = False) -> Listener:
        reason = None
        if self.busy_check is not None:
            try:
                reason = self.busy_check()
            except Exception as exc:
                LOG.exception("The busy check failed")
                reason = f"Cannot tell whether another operation holds the disks: {exc}"
        if reason:
            # `blocked`: this is a refusal (a repair session holds the disks), not another installation that could be followed.
            raise InstallBusy(str(reason), blocked=True)
        with self.lock:
            if self.is_running():
                raise InstallBusy("An installation is already running")
            cancel = threading.Event()
            # Built first: if it fails (state directory unusable), nothing about the manager changes.
            installer = Installer(
                manifest,
                target_root=self.target_root,
                state_dir=self.state_dir,
                cache_dir=self.cache_dir,
                cancel=cancel,
                resume=resume,
                emit=self._emit,
            )
            self._reset_job_state()
            self.job_id = getattr(installer, "job_id", "")
            self.history = EventHistory(self.history_events)
            self.stage_weights = dict(getattr(installer, "stage_weights", STAGE_WEIGHTS))
            listener = Listener(self.listener_capacity, self.job_id)
            self.cancel = cancel
            self.listeners = [listener]
            self.status = "running"
            self.started_at = time.time()
            self.installer = installer
            self.thread = threading.Thread(target=self._run, args=(installer, cancel), name="gentoo-install", daemon=True)
            self.thread.start()
            return listener

    def attach(self, after_seq: int = 0) -> Listener:
        """A listener primed with the buffered events after ``after_seq``, then live ones until the job ends."""
        with self.lock:
            if self.installer is None:
                raise InstallNotRunning("No installation has run in this session")
            listener = Listener(self.listener_capacity, self.job_id)
            events = self.history.replay(after_seq)
            if self.status != "running":
                # The job is over; the stream must still end with its completed event.
                if self._completed is not None and not any(event.type == "completed" for event in events):
                    events.append(self._completed)
                listener.prefill(events)
                listener.finish()
            else:
                listener.prefill(events)
                self.listeners.append(listener)
            return listener

    def unsubscribe(self, listener: Any) -> None:
        close = getattr(listener, "close", None)
        if close is not None:
            close()
        with self.lock:
            if listener in self.listeners:
                self.listeners.remove(listener)

    def _run(self, installer: Installer, cancel: threading.Event) -> None:
        result = False
        try:
            result = bool(installer.run())
        except BaseException as exc:  # noqa: BLE001 - whatever happened, the job must end cleanly for its clients
            LOG.exception("Installation job %s crashed", self.job_id)
            text = redact_text(f"Installer crashed: {exc}", getattr(installer, "secrets", ()))
            self._emit(error_event(ErrorCode.FAILED, text, job_id=self.job_id, stage=self.stage or "preflight", retryable=True, resumable=False,
                                   bootable=bool(getattr(installer, "_bootable", False))))
        finally:
            with self.lock:
                if self._completed is None:
                    self._emit(completed_event(result, "Installation completed successfully" if result else "Installation failed", job_id=self.job_id,
                                               bootable=bool(getattr(installer, "_bootable", False)), degraded=[], warnings=[]))
                self.status = "completed" if result else "cancelled" if cancel.is_set() else "failed"
                if self.status == "failed" and not self.last_error:
                    self.last_error = GENERIC_FAILURE
                self.finished_at = time.time()
                for listener in self.listeners:
                    listener.finish()
                LOG.info("Installation job %s ended: %s", self.job_id, self.status)

    def _emit(self, event: Event) -> None:
        with self.lock:
            if event.seq == 0:
                event = event.replace(seq=self.seq + 1)
            self.seq = max(self.seq, event.seq)
            if not event.job_id and self.job_id:
                event = event.replace(job_id=self.job_id)
            self.history.append(event)
            self._track(event)
            # Offering never blocks (bounded, sheds old log lines), so a slow client cannot slow the job.
            for listener in self.listeners:
                listener.offer(event)

    def _track(self, event: Event) -> None:
        if event.stage and event.type != "completed":
            self.stage = event.stage
        if event.type in ("stage", "progress") and event.progress is not None:
            self.progress = event.progress
        if event.type == "stage" and event.message:
            self.message = event.message
        if event.type == "error":
            self.last_error = event.error or event.message
            self.last_error_stage = event.stage
            self.last_error_code = str(event.data.get("code", ""))
            retryable = event.data.get("retryable")
            self.last_retryable = retryable if isinstance(retryable, bool) else None
            self.errors = [*self.errors[-9:], {"stage": event.stage, "message": event.error or event.message, "code": self.last_error_code, "seq": event.seq}]
        elif event.type == "completed":
            self._completed = event
            if event.data.get("success") is True:
                self.progress = 100.0

    # -- control

    def request_cancel(self) -> None:
        with self.lock:
            if not self.is_running():
                raise InstallNotRunning("No installation is running")
            self.cancel_requested = True
            self.cancel.set()

    def shutdown(self, grace: float = SHUTDOWN_GRACE_SECONDS) -> bool:
        """Cancel a running installation and wait for it (and its cleanup) to finish."""
        thread = self.thread
        if thread is None or not thread.is_alive():
            return True
        with self.lock:
            self.cancel_requested = True
            self.cancel.set()
        thread.join(grace)
        if thread.is_alive():
            LOG.error("The installation is still cleaning up after %.0f s", grace)
            return False
        return True

    # -- reports

    def resumable(self) -> bool:
        """Storage was prepared and checkpointed, so a run with resume=true can continue."""
        if self.is_running() or self.status == "completed":
            return False
        try:
            raw = json.loads((self.state_dir / "checkpoints.json").read_text())
            return "storage" in raw.get("completed", []) and (self.state_dir / "layout.json").is_file()
        except (OSError, ValueError, AttributeError, TypeError):
            return False

    def log_path(self) -> Path:
        return self.state_dir / "install.log"

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            running = self.status == "running"
            data: dict[str, Any] = {
                "status": self.status,
                "job_id": self.job_id,
                "error": self.last_error,
                "stage": self.stage,
                "progress": self.progress,
                "message": self.message,
                "seq": self.seq,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "cancelling": running and self.cancel_requested,
                "retryable": self.last_retryable,
                "error_stage": self.last_error_stage,
                "error_code": self.last_error_code,
                "errors": list(self.errors),
                "stages": stage_table(self.stage_weights),
            }
        data["resumable"] = self.resumable()
        data["log_path"] = str(self.log_path()) if self.log_path().exists() else ""
        data["leftovers"] = [] if running else find_leftovers(self.target_root)
        return data

    def read_log(self, tail: int = 0) -> Iterator[str]:
        """Lines of the install log, optionally only the last ``tail``. The file is written redacted."""
        try:
            handle = self.log_path().open("r", encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return
        with handle:
            source: Iterable[str] = deque(handle, maxlen=tail) if tail > 0 else handle
            for line in source:
                line = line.rstrip("\n")
                if line:
                    yield line

    def known_secrets(self) -> tuple[str, ...]:
        """Secret values of the current job, so exports can scrub them once more."""
        return tuple(getattr(self.installer, "secrets", ()) or ()) if self.installer else ()


# ------------------------------------------------------------------ leftovers


def _unescape_mountinfo(path: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), path)


def find_leftovers(target_root: Path, *, mountinfo: str | Path = "/proc/self/mountinfo", mapper_dir: str | Path = "/dev/mapper") -> list[str]:
    """What an earlier installation left behind: mounts under the target root and its device-mapper devices."""
    found: list[str] = []
    root = str(target_root).rstrip("/") or "/"
    try:
        with open(mountinfo, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) < 5:
                    continue
                mountpoint = _unescape_mountinfo(fields[4])
                if mountpoint == root or mountpoint.startswith(root + "/"):
                    found.append(f"mounted: {mountpoint}")
    except OSError:
        pass
    try:
        for name in sorted(os.listdir(mapper_dir)):
            if name.startswith("gentoo-"):
                found.append(f"device-mapper device: {Path(mapper_dir, name)}")
    except OSError:
        pass
    return found


# ------------------------------------------------------------------ built-in actions


def _manager(channel: Channel) -> InstallManager:
    manager = channel.install_manager
    if manager is None:
        raise ActionError("This daemon has no installation manager", ErrorCode.INTERNAL)
    return manager  # type: ignore[no-any-return]


def _registry(channel: Channel) -> ActionRegistry:
    return getattr(channel.server, "registry", None) or REGISTRY  # type: ignore[no-any-return]


def _non_negative_int(request: dict, key: str, default: int = 0) -> int:
    value = request.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ActionError(f"{key!r} must be a non-negative integer", ErrorCode.BAD_REQUEST)
    return value


def stream_job(channel: Channel, manager: Any, listener: Any, opening: Event | None = None) -> None:
    """Forward a job's events to the client until its ``completed`` event, whatever the client does.

    ``opening`` is sent first, inside the same protected block: a client that is already gone
    must not leave its listener registered.
    """
    finished = getattr(manager, "job_finished", None)
    try:
        if opening is not None:
            channel.send(opening)
        while True:
            try:
                event = listener.get(timeout=STREAM_POLL_SECONDS)
            except queue.Empty:
                if channel.closed:
                    return
                if finished is not None and finished() and hasattr(listener, "finish"):
                    listener.finish()  # the job's thread is gone without a sentinel: drain what is left and stop
                continue
            if event is None:
                break
            channel.send(event)
        if not channel.completed_sent:
            channel.send(completed_event(False, "The installation ended without reporting a result"))
    finally:
        unsubscribe = getattr(manager, "unsubscribe", None)
        if unsubscribe is not None:
            unsubscribe(listener)


def handle_capabilities(request: dict, channel: Channel) -> None:
    channel.send(Event("result", message="capabilities", data=_registry(channel).capabilities()))


def handle_status(request: dict, channel: Channel) -> None:
    data = _manager(channel).snapshot()
    data.update(_registry(channel).status_extensions())
    channel.send(Event("status", job_id=str(data.get("job_id", "")), data=data))


def handle_cancel(request: dict, channel: Channel) -> None:
    _manager(channel).request_cancel()
    LOG.info("Cancellation requested by uid %s", _peer_uid(channel))
    channel.send(Event("status", message="Cancellation requested", data={"status": "cancelling"}))


def handle_start(request: dict, channel: Channel) -> None:
    manager = _manager(channel)
    raw = request.get("manifest")
    if not isinstance(raw, dict):
        raise ActionError("start needs a 'manifest' object", ErrorCode.BAD_REQUEST)
    try:
        manifest = InstallManifest.from_dict(raw)
        manifest.require_valid()
    except ManifestError as exc:
        raise ActionError(str(exc), ErrorCode.INVALID_MANIFEST) from exc
    resume = bool(request.get("resume", False))
    listener = manager.start(manifest, resume)
    job_id = getattr(getattr(manager, "installer", None), "job_id", "") or ""
    LOG.info("Installation started by uid %s (job %s, resume=%s)", _peer_uid(channel), job_id, resume)
    stream_job(channel, manager, listener, Event("status", job_id=job_id, message="Installation started", data={"status": "running", "job_id": job_id}))


def handle_attach(request: dict, channel: Channel) -> None:
    manager = _manager(channel)
    after_seq = _non_negative_int(request, "after_seq")
    listener = manager.attach(after_seq)
    LOG.info("Client uid %s attached to job %s after seq %d", _peer_uid(channel), manager.job_id, after_seq)
    stream_job(channel, manager, listener)


def format_log_record(line: str) -> str:
    """One install.log JSON record as a readable line; anything else is passed through."""
    try:
        record = json.loads(line)
    except ValueError:
        return line
    if not isinstance(record, dict) or not (record.get("ts") or record.get("message")):
        return line
    return _format_record(record)


def _format_record(record: dict[str, Any]) -> str:
    text = str(record.get("message") or record.get("error") or "")
    error = str(record.get("error") or "")
    if error and error != text:
        text += f" ({error})"
    if record.get("code"):
        text += f" [{record['code']}]"
    head = [str(record.get("ts", "")), f"{record.get('type', ''):<9}"]
    if record.get("stage"):
        head.append(f"[{record['stage']}]")
    return " ".join(part for part in [*head, text] if part).rstrip()


def export_log_line(raw: str, secrets: Sequence[str], form: str) -> str:
    """A log line for export: parsed first, so secrets inside JSON-escaped text are scrubbed too."""
    try:
        record = json.loads(raw)
    except ValueError:
        record = None
    if not isinstance(record, dict):
        return redact_text(raw, secrets)
    for key in ("message", "error"):
        if isinstance(record.get(key), str):
            record[key] = redact_text(record[key], secrets)
    if form == "jsonl":
        return json.dumps(record, ensure_ascii=False)
    return _format_record(record) if (record.get("ts") or record.get("message")) else redact_text(raw, secrets)


def handle_log(request: dict, channel: Channel) -> None:
    manager = _manager(channel)
    tail = _non_negative_int(request, "tail")
    form = request.get("format", "text")
    if form not in ("text", "jsonl"):
        raise ActionError("'format' must be 'text' or 'jsonl'", ErrorCode.BAD_REQUEST)
    secrets = manager.known_secrets()
    total = 0
    chunk: list[str] = []
    for line in manager.read_log(tail):
        chunk.append(export_log_line(line, secrets, form))
        if len(chunk) >= LOG_LINES_PER_EVENT:
            channel.send(Event("result", message="install log", data={"lines": chunk}))
            total += len(chunk)
            chunk = []
    if chunk:
        channel.send(Event("result", message="install log", data={"lines": chunk}))
        total += len(chunk)
    channel.send(completed_event(True, "Install log sent" if total else "There is no install log yet", lines=total))


def _merge_warnings(existing: Any, extra: Iterable[Any]) -> list[dict[str, Any]]:
    """inspect warnings are {code, severity, message, action}; plain strings from older checks are wrapped."""
    merged: list[dict[str, Any]] = []
    for item in [*(existing or []), *extra]:
        if isinstance(item, str):
            item = {"code": "inspect.warning", "severity": "warning", "message": item, "action": ""}
        if isinstance(item, dict):
            merged.append(item)
    return merged


def _selection(raw: Any) -> InstallManifest | None:
    """The settings an ``inspect`` request carries (no accounts, no secrets), or None when there are none or they make no sense."""
    if not isinstance(raw, dict):
        return None
    try:
        return InstallManifest.from_dict(raw)
    except ManifestError as exc:
        LOG.debug("The inspect request carries settings that cannot be read: %s", exc)
        return None


def handle_inspect(request: dict, channel: Channel) -> None:
    runner = SubprocessRunner()
    selection = _selection(request.get("manifest"))
    # With the user's settings the network check goes to the mirror the install will use, through the proxy it will use.
    discovery = Discovery(runner, mirror=selection.portage.mirror, proxy=selection.network.proxy) if selection else Discovery(runner)
    inspection = discovery.inspect()
    data = inspection.to_dict()
    if selection is not None:
        data["missing_for_selection"] = sorted(tool for tool in tools_for_selection(selection) if inspection.tools.get(tool) is False)
    # Discovery probes the hardware itself; this is for an inspector that does not.
    if "hardware" not in data and "hardware_error" not in data and module_available("gentoo_installer.hardware"):
        try:
            hardware = importlib.import_module("gentoo_installer.hardware")
            profile = hardware.probe(Path("/"), runner).to_dict()
            data["hardware"] = profile
            data["warnings"] = _merge_warnings(data.get("warnings"), profile.get("warnings") or [])
        except Exception as exc:
            # Inspection must still work when one probe is broken on unusual hardware.
            LOG.exception("The hardware probe failed")
            data["hardware_error"] = f"{type(exc).__name__}: {exc}"
    elif "warnings" in data:
        data["warnings"] = _merge_warnings(data["warnings"], [])
    channel.send(Event("status", message="Inspection completed", data=data))
    channel.send(completed_event(True, "Inspection completed"))


def _peer_uid(channel: Channel) -> Any:
    try:
        return channel.peer.uid
    except (OSError, struct.error):
        return "?"


def register_builtin_actions(registry: ActionRegistry) -> None:
    registry.register_action("inspect", handle_inspect, feature="install.v1")
    registry.register_action("status", handle_status, feature="install.v1")
    registry.register_action("cancel", handle_cancel, feature="install.v1")
    registry.register_action("start", handle_start, feature="install.v1")
    registry.register_action("capabilities", handle_capabilities, feature="attach.v1")
    registry.register_action("attach", handle_attach, feature="attach.v1")
    registry.register_action("log", handle_log, feature="attach.v1")


# The process-wide registry: modules register their actions here (see register_action) and
# the daemon serves it. Tests that need isolation build their own ActionRegistry.
REGISTRY = ActionRegistry()


# ------------------------------------------------------------------ the socket server


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, path: str, manager: InstallManager, registry: ActionRegistry | None = None) -> None:
        self.manager = manager
        self.registry = registry if registry is not None else REGISTRY
        if self.registry.install_manager is None:
            self.registry.install_manager = manager
        super().__init__(path, RequestHandler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        LOG.exception("Unhandled error while serving a client")


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        channel = Channel(self.connection, server=self.server)
        try:
            request = self._read_request(channel)
            if request is not None:
                self._dispatch(request, channel)
        except ChannelClosed:
            LOG.debug("The client left")
        except Exception as exc:
            self._fail(channel, exc)
        finally:
            channel.close()

    def _read_request(self, channel: Channel) -> dict | None:
        self.connection.settimeout(REQUEST_TIMEOUT)
        try:
            line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        except (TimeoutError, socket.timeout):
            raise ActionError(f"No request was received within {REQUEST_TIMEOUT:g} seconds", ErrorCode.TIMEOUT) from None
        except OSError:
            return None
        finally:
            try:
                self.connection.settimeout(SEND_TIMEOUT)
            except OSError:
                pass
        if not line:
            return None
        if len(line) > MAX_REQUEST_BYTES:
            raise ActionError("Request is too large", ErrorCode.BAD_REQUEST)
        try:
            request = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            raise ActionError("The request is not valid JSON", ErrorCode.BAD_REQUEST) from None
        if not isinstance(request, dict):
            raise ActionError("The request must be a JSON object", ErrorCode.BAD_REQUEST)
        if request.get("version") != PROTOCOL_VERSION:
            raise ActionError(f"Protocol version must be {PROTOCOL_VERSION}", ErrorCode.BAD_REQUEST)
        request_id = request.get("request_id")
        if request_id is not None and (isinstance(request_id, bool) or not isinstance(request_id, (str, int)) or len(str(request_id)) > 200):
            raise ActionError("'request_id' must be a short string or integer", ErrorCode.BAD_REQUEST)
        channel.request = request
        channel.request_id = request_id
        return request

    def _dispatch(self, request: dict, channel: Channel) -> None:
        action = request.get("action")
        registry: ActionRegistry = getattr(self.server, "registry", REGISTRY)
        handler = registry.lookup(action) if isinstance(action, str) else None
        if handler is None:
            raise ActionError(f"Unsupported action {action!r}. This daemon supports: {', '.join(registry.actions())}", ErrorCode.UNSUPPORTED_ACTION)
        (LOG.debug if action in ("status", "capabilities") else LOG.info)("Request %s from pid %s", action, _pid_of(channel))
        handler(request, channel)

    def _fail(self, channel: Channel, exc: Exception) -> None:
        """The error boundary: every failure becomes one error event and one completed event."""
        if isinstance(exc, ActionError):
            code, message, extra = exc.code, str(exc), exc.data
            LOG.info("Request refused (%s): %s", code, message)
        elif isinstance(exc, ManifestError):
            code, message, extra = ErrorCode.INVALID_MANIFEST, str(exc), {}
            LOG.info("Request refused (%s): %s", code, message)
        elif isinstance(exc, ValueError):
            code, message, extra = ErrorCode.BAD_REQUEST, str(exc), {}
            LOG.warning("Bad request: %s", message)
        else:
            LOG.exception("Request failed with an unexpected error")
            code, message, extra = ErrorCode.INTERNAL, f"Installer backend error: {redact_text(str(exc))}", {}
        try:
            channel.send(error_event(code, message, **extra))
            if not channel.completed_sent:
                channel.send(completed_event(False, message))
        except ChannelClosed:
            pass


def _pid_of(channel: Channel) -> Any:
    try:
        return channel.peer.pid
    except (OSError, struct.error):
        return "?"


# ------------------------------------------------------------------ shutdown and signals


class ShutdownController:
    """Turns SIGTERM/SIGINT into an orderly stop: cancel the install, run hooks, stop the server.

    A signal handler may only set a flag and start a thread; ``server.shutdown()`` would
    deadlock if it ran on the thread that is inside ``serve_forever()``.
    """

    def __init__(self, server: Any, manager: InstallManager, registry: ActionRegistry, *, grace: float = SHUTDOWN_GRACE_SECONDS) -> None:
        self.server = server
        self.manager = manager
        self.registry = registry
        self.grace = grace
        self.requested = threading.Event()
        self.finished = threading.Event()
        # Claimed with a single non-blocking acquire: atomic even when a second signal interrupts
        # the first handler on the same thread (a lock held by the interrupted code would deadlock).
        self._claim = threading.Lock()

    def request(self, reason: str = "requested") -> None:
        self.requested.set()
        if not self._claim.acquire(blocking=False):
            return  # a shutdown is already under way
        threading.Thread(target=self._run, args=(reason,), name="gentoo-shutdown", daemon=True).start()

    def _run(self, reason: str) -> None:
        LOG.info("Shutting down (%s)", reason)
        try:
            if self.manager.is_running():
                LOG.info("Cancelling the running installation; its cleanup still runs")
            self.manager.shutdown(self.grace)
        except Exception:
            LOG.exception("Stopping the installation failed")
        try:
            self.registry.run_shutdown_hooks()
        except Exception:
            LOG.exception("Running the shutdown hooks failed")
        try:
            self.server.shutdown()
        finally:
            self.finished.set()


def install_signal_handlers(controller: ShutdownController) -> Callable[[], None]:
    """Route SIGTERM and SIGINT to ``controller``. Returns a function that restores the old handlers."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous: dict[int, Any] = {}

    def handler(signum: int, _frame: Any) -> None:
        controller.request(signal.Signals(signum).name)

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, handler)

    def restore() -> None:
        for signum, old in previous.items():
            signal.signal(signum, old if old is not None else signal.SIG_DFL)

    return restore


# ------------------------------------------------------------------ start-up


def validate_socket_path(socket_path: str | Path) -> Path:
    """Refuse socket paths whose directory the daemon would take over (chown/chmod) or whose file it would replace."""
    path = Path(socket_path)
    if not path.is_absolute():
        raise ValueError(f"The socket path must be absolute: {socket_path}")
    parents = {os.path.normpath(path.parent), os.path.realpath(path.parent)}
    if parents & FORBIDDEN_SOCKET_PARENTS:
        raise ValueError(f"Refusing to use {path.parent} as the socket directory: it is shared with other programs. Use a dedicated directory such as {DEFAULT_SOCKET}")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return path
    if not stat.S_ISSOCK(mode):
        raise RuntimeError(f"Refusing to replace non-socket path: {path}")
    return path


def prepare_socket_directory(parent: Path) -> int:
    parent.mkdir(parents=True, exist_ok=True)
    try:
        group_id = grp.getgrnam(SOCKET_GROUP).gr_gid
    except KeyError as exc:
        raise RuntimeError(f"Required socket access group {SOCKET_GROUP!r} does not exist") from exc
    os.chown(parent, 0, group_id)
    parent.chmod(0o750)
    return group_id


def secure_socket(path: Path, group_id: int) -> None:
    os.chown(path, 0, group_id)
    path.chmod(0o660)


def acquire_server_lock(directory: Path) -> int:
    """One daemon per socket directory. The lock lives as long as the returned descriptor is open."""
    fd = os.open(directory / "server.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            raise RuntimeError("Another gentoo-installerd is already serving this socket") from exc
        raise
    return fd


def serve(
    socket_path: str | Path = DEFAULT_SOCKET,
    *,
    target_root: str | Path = "/mnt/gentoo",
    state_dir: str | Path = "/var/lib/gentoo-installer",
    cache_dir: str | Path = "/var/tmp/gentoo-installer",
    require_root: bool = True,
    rescue: bool = True,
    install_signals: bool = True,
    registry: ActionRegistry | None = None,
    options: Any = None,
) -> None:
    if require_root and hasattr(os, "geteuid") and os.geteuid() != 0:
        raise PermissionError("gentoo-installerd must run as root")
    path = validate_socket_path(socket_path)
    group_id = prepare_socket_directory(path.parent)
    lock_fd = acquire_server_lock(path.parent)
    bound = False
    restore_signals: Callable[[], None] = lambda: None
    manager = InstallManager(Path(target_root), Path(state_dir), Path(cache_dir))
    try:
        active = registry if registry is not None else REGISTRY
        active.install_manager = manager
        active.options = options
        leftovers = find_leftovers(manager.target_root)
        if leftovers:
            LOG.warning("Found what an earlier installation left behind: %s", "; ".join(leftovers))
        plugins = load_plugins(active, rescue=rescue)
        path.unlink(missing_ok=True)  # a stale socket; the lock proves no other daemon is using it
        with UnixServer(str(path), manager) as server:
            bound = True
            secure_socket(path, group_id)  # right away: until now only root could connect
            server.registry = active
            controller = ShutdownController(server, manager, active)
            if install_signals:
                restore_signals = install_signal_handlers(controller)
            LOG.info("Listening on %s (actions: %s; plugins: %s)", path, ", ".join(active.actions()), ", ".join(plugins) or "none")
            server.serve_forever(poll_interval=0.2)
            if controller.requested.is_set():
                controller.finished.wait(SHUTDOWN_GRACE_SECONDS + SHUTDOWN_HOOK_SECONDS)
    finally:
        restore_signals()
        manager.shutdown(SHUTDOWN_GRACE_SECONDS)
        if bound:
            path.unlink(missing_ok=True)
        os.close(lock_fd)
        LOG.info("gentoo-installerd stopped")


def run_cli(manifest_path: Path, *, dry_run: bool, target_root: Path, state_dir: Path, cache_dir: Path, resume: bool,
            allow_unverified_disk: bool = False) -> int:
    manifest = InstallManifest.load(manifest_path)
    installer = Installer(manifest, target_root=target_root, state_dir=state_dir, cache_dir=cache_dir, dry_run=dry_run, resume=resume,
                          allow_unverified_disk=allow_unverified_disk, emit=lambda event: print(event.to_json(), flush=True))
    restore = install_cancel_signals(installer.cancel)
    try:
        return 0 if installer.run() else 1
    finally:
        restore()


def install_cancel_signals(cancel: threading.Event) -> Callable[[], None]:
    """CLI runs: SIGTERM/SIGINT cancel the installation (its cleanup still runs) instead of killing it mid-way."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous = {signum: signal.signal(signum, lambda _signum, _frame: cancel.set()) for signum in (signal.SIGTERM, signal.SIGINT)}

    def restore() -> None:
        for signum, old in previous.items():
            signal.signal(signum, old if old is not None else signal.SIG_DFL)

    return restore


# ------------------------------------------------------------------ logging


class JournalFormatter(logging.Formatter):
    """systemd-journald understands a "<N>" syslog priority prefix on each line of stderr."""

    PRIORITY = {logging.CRITICAL: 2, logging.ERROR: 3, logging.WARNING: 4, logging.INFO: 6, logging.DEBUG: 7}

    def format(self, record: logging.LogRecord) -> str:
        priority = self.PRIORITY.get(record.levelno, 6)
        return "\n".join(f"<{priority}>{line}" for line in super().format(record).splitlines() or [""])


def configure_logging(log_file: str | Path | None = None, level: str = "INFO") -> Callable[[], None]:
    """Log to stderr (journal-friendly under systemd) and optionally to a file. Returns an undo function."""
    package_logger = logging.getLogger("gentoo_installerd")
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(JournalFormatter("%(name)s: %(message)s") if os.environ.get("JOURNAL_STREAM") else logging.Formatter(fmt))
    handlers: list[logging.Handler] = [stream]
    problem = ""
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter(fmt))
            handlers.append(file_handler)
        except OSError as exc:
            problem = f"Cannot open the log file {log_file}: {exc}"
    for handler in handlers:
        package_logger.addHandler(handler)
    previous_level = package_logger.level
    package_logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    if problem:
        LOG.warning(problem)

    def undo() -> None:
        for handler in handlers:
            package_logger.removeHandler(handler)
            handler.close()
        package_logger.setLevel(previous_level)

    return undo


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gentoo-installerd")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--target-root", default="/mnt/gentoo")
    parser.add_argument("--state-dir", default="/var/lib/gentoo-installer")
    parser.add_argument("--cache-dir", default="/var/tmp/gentoo-installer")
    parser.add_argument("--manifest", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--yes", action="store_true", help="Confirm destructive installation without interactive prompts")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--allow-unverified-disk", action="store_true",
                        help="With --manifest: install even though the manifest does not pin the target disk by serial number, WWN or bus path. "
                             "Not available to clients of the socket.")
    parser.add_argument("--log-file", type=Path, help="Also write the daemon log to this file (default: stderr only)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], type=str.upper)
    parser.add_argument("--no-rescue", action="store_true", help="Do not register the rescue actions")
    parser.add_argument("--rescue-root", default="/mnt/rescue", help="Mount directory of repair sessions")
    parser.add_argument("--rescue-state-dir", default="/run/gentoo-rescue", help="Ledger of repair sessions (tmpfs)")
    parser.add_argument("--rescue-audit-dir", default="/var/lib/gentoo-rescue", help="Audit log of repair sessions")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    undo_logging = configure_logging(args.log_file, args.log_level)
    try:
        if hasattr(os, "geteuid") and os.geteuid() != 0 and not args.dry_run:
            LOG.error("gentoo-installerd must run as root (except for --dry-run)")
            return 1
        try:
            if args.manifest:
                if not args.dry_run and not args.yes:
                    LOG.error("--manifest requires either --dry-run or explicit --yes")
                    return 2
                LOG.info("gentoo-installerd %s manifest=%s dry_run=%s resume=%s state=%s target=%s pid=%d", __version__, args.manifest, args.dry_run, args.resume, args.state_dir, args.target_root, os.getpid())
                return run_cli(args.manifest, dry_run=args.dry_run, target_root=Path(args.target_root), state_dir=Path(args.state_dir), cache_dir=Path(args.cache_dir),
                               resume=args.resume, allow_unverified_disk=args.allow_unverified_disk)
            if args.dry_run or args.yes or args.allow_unverified_disk:
                LOG.error("--dry-run, --yes and --allow-unverified-disk require --manifest")
                return 2
            LOG.info("gentoo-installerd %s socket=%s group=%s state=%s cache=%s target=%s pid=%d", __version__, args.socket, SOCKET_GROUP, args.state_dir, args.cache_dir, args.target_root, os.getpid())
            serve(args.socket, target_root=args.target_root, state_dir=args.state_dir, cache_dir=args.cache_dir, rescue=not args.no_rescue, options=args)
            return 0
        except (OSError, ValueError, RuntimeError, ManifestError) as exc:
            LOG.error("gentoo-installerd: %s", exc)
            return 1
    finally:
        undo_logging()


if __name__ == "__main__":
    raise SystemExit(main())
