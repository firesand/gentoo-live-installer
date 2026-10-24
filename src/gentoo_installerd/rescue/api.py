"""The ``rescue.*`` daemon actions (docs/protocol.md): registration, dispatch, streaming and the shell hand-over.

``register(registry)`` adds every action and the ``rescue.v1`` feature to the server's action registry, so server.py needs no
rescue-specific code. Long work runs on the manager's operation threads and the connection only follows it: a client that
stalls, crashes or reconnects (``rescue.events``) never stops a repair.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from gentoo_installerd.events import Event
from gentoo_installerd.runner import InstallCancelled, SubprocessRunner

from . import DEFAULT_AUDIT_DIR, DEFAULT_SESSION_BASE, DEFAULT_STATE_DIR, FEATURE, RESCUE_API_VERSION
from .audit import scrub
from .errors import RescueError
from .hostenv import Host
from .manager import RescueManager
from .ops import Operation, RescueEvent
from .scan import ScanOptions
from .session import SessionEnv

LOG = logging.getLogger(__name__)

ACTIONS = (
    "rescue.scan", "rescue.unlock", "rescue.activate", "rescue.mount", "rescue.session", "rescue.events", "rescue.query", "rescue.plan", "rescue.run", "rescue.cancel",
    "rescue.shell", "rescue.unmount", "rescue.audit", "rescue.recover",
)
FAMILIES = ("gentoo", "debian", "fedora", "arch", "suse", "generic")
REPAIR_FAMILIES = ("gentoo", "debian", "fedora")
LIVE_MARKERS = ("/run/initramfs/live", "/run/live/medium")


def on_live_medium(host: Host | None = None) -> bool:
    """Rescue is meant for the live medium (review R21): membership of gentoo-installer would otherwise grant rescue.shell."""
    host = host or Host()
    return any(host.exists(marker) for marker in LIVE_MARKERS) or "cdroot" in host.cmdline().split() or "rd.live.image" in host.cmdline().split()


def is_channel_gone(exc: BaseException) -> bool:
    return isinstance(exc, (OSError, TimeoutError, ConnectionError)) or type(exc).__name__ == "ChannelClosed"


def build_env(base_dir: str | Path = DEFAULT_SESSION_BASE, state_dir: str | Path = DEFAULT_STATE_DIR, audit_dir: str | Path = DEFAULT_AUDIT_DIR, host: Host | None = None) -> SessionEnv:
    state = Path(state_dir)
    return SessionEnv(base_dir=Path(base_dir), state_dir=state, audit_dir=Path(audit_dir), probe_root=state / "probe", host=host or Host(), make_runner=lambda cancel: SubprocessRunner(cancel=cancel))


class RescueApi:
    def __init__(self, manager: RescueManager) -> None:
        self.manager = manager
        self.startup_orphans: list[dict[str, Any]] = []

    # ---- information for server.py ---------------------------------------------------------------------------------------------------------
    def capabilities(self) -> dict[str, Any]:
        """The ``rescue`` entry of the capabilities reply."""
        return {"api": RESCUE_API_VERSION, "root": str(self.manager.env.base_dir), "families": list(FAMILIES), "repairs": list(REPAIR_FAMILIES), "actions": list(ACTIONS)}

    def status(self) -> dict[str, Any]:
        return {**self.manager.status(), "orphans": len(self.startup_orphans)}

    def install_blocker(self) -> str | None:
        return self.manager.install_blocker()

    def recover_orphans(self) -> list[dict[str, Any]]:
        """What a daemon that died left behind. Systems that are still mounted become sessions again, so a window lists them (continue, or release); the rest stays a leftover."""
        for item in self.manager.orphans():
            if item["sid"] and item["state"] == "alive":
                self.manager.adopt(item["sid"])
        self.startup_orphans = [item for item in self.manager.orphans() if item["state"] in {"alive", "stale", "pool", "unknown"}]
        return self.startup_orphans

    # ---- dispatch --------------------------------------------------------------------------------------------------------------------------------
    def handler(self, name: str) -> Callable[[dict[str, Any], Any], None]:
        def handle(request: dict[str, Any], channel: Any) -> None:
            self.dispatch(name, request, channel)

        handle.__name__ = name.replace(".", "_")
        return handle

    def dispatch(self, name: str, request: dict[str, Any], channel: Any) -> None:
        request_id = str(request.get("request_id", ""))
        try:
            if name == "rescue.shell":
                self._shell(request, channel, request_id)
            elif name in {"rescue.session", "rescue.query", "rescue.plan", "rescue.audit", "rescue.cancel"}:
                self._quick(name, request, channel, request_id)
            elif name == "rescue.events":
                self._events(request, channel)
            else:
                self._streamed(name, request, channel, request_id)
        except RescueError as exc:
            self._reject(channel, exc, request_id)
        except Exception as exc:  # the handler must never die silently: one error event, then completed(false)
            if is_channel_gone(exc):
                return
            self._reject(channel, exc, request_id)

    # ---- plumbing ---------------------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _send(channel: Any, event: Event) -> bool:
        try:
            channel.send(event)
        except Exception as exc:
            if is_channel_gone(exc):
                return False
            raise
        return True

    def _reject(self, channel: Any, exc: BaseException, request_id: str) -> None:
        data: dict[str, Any] = {"request_id": request_id} if request_id else {}
        if isinstance(exc, RescueError):
            message, code = exc.message, exc.code
            data.update(exc.to_data())
        else:
            message, code = "Internal error in the rescue engine", "proto.internal"
            data["code"] = code
        if self._send(channel, RescueEvent("error", message=message, error=code, data=data)):
            self._send(channel, RescueEvent("completed", message=message, data={"success": False, **({"request_id": request_id} if request_id else {})}))

    def _quick(self, name: str, request: dict[str, Any], channel: Any, request_id: str) -> None:
        manager = self.manager
        if name == "rescue.session":
            payload = manager.session_summary(str(request.get("session_id", "")))
        elif name == "rescue.query":
            payload = manager.query(request)
        elif name == "rescue.plan":
            payload = manager.plan(request)
        elif name == "rescue.audit":
            payload = manager.audit_text(request)
        else:
            payload = manager.cancel(request)
        data = {**payload, **({"request_id": request_id} if request_id else {})}
        if self._send(channel, RescueEvent("result", message=name, data=data)):
            self._send(channel, RescueEvent("completed", message=name, data={"success": True, **({"request_id": request_id} if request_id else {})}))

    def _events(self, request: dict[str, Any], channel: Any) -> None:
        op = self.manager.operation_for(request)
        try:
            op.stream(lambda event: channel.send(event), int(request.get("after_seq", 0)))
        except Exception as exc:
            if not is_channel_gone(exc):
                raise

    def _streamed(self, name: str, request: dict[str, Any], channel: Any, request_id: str) -> None:
        manager = self.manager
        if name in {"rescue.scan", "rescue.unlock", "rescue.activate", "rescue.mount"}:
            manager.check_not_busy()
        peer = getattr(channel, "peer", None)
        if peer is not None:
            request = {**request, "_peer": {"uid": getattr(peer, "uid", None), "pid": getattr(peer, "pid", None)}}
        op = manager.new_op(name.split(".", 1)[1], str(request.get("session_id", "")), request_id)
        op.start(self._work(name, request))
        try:
            op.stream(lambda event: channel.send(event))
        except Exception as exc:  # the operation goes on without us (review R6)
            if not is_channel_gone(exc):
                raise

    def _work(self, name: str, request: dict[str, Any]) -> Callable[[Operation], None]:
        manager = self.manager

        def publish(op: Operation, payload: dict[str, Any]) -> None:
            if not op.result:
                op.publish(payload, name)

        def work(op: Operation) -> None:
            try:
                if name == "rescue.scan":
                    publish(op, manager.scan(op, ScanOptions(probe=request.get("probe", True) is not False, include_loop=bool(request.get("include_loop")), network=bool(request.get("network")))))
                elif name == "rescue.unlock":
                    publish(op, manager.unlock(op, request))
                elif name == "rescue.activate":
                    publish(op, manager.activate(op, request))
                elif name == "rescue.mount":
                    result = manager.mount(op, request)
                    if "session_id" in result:
                        op.session_id = result["session_id"]
                    publish(op, result)
                elif name == "rescue.unmount":
                    payload = manager.unmount(op, request)
                    publish(op, payload)
                    if payload.get("leftovers"):
                        raise RescueError("busy", "Some mounts are still in use", leftovers=payload["leftovers"], busy_holders=payload.get("busy_holders", []), errors=payload.get("errors", []))
                elif name == "rescue.recover":
                    publish(op, manager.recover(op, request))
                elif name == "rescue.run":
                    manager.run_plan(op, request)
                else:
                    raise RescueError("bad_request", f"Unknown action {name}")
            except InstallCancelled as exc:
                raise RescueError("cancelled", "The operation was cancelled") from exc

        return work

    def _shell(self, request: dict[str, Any], channel: Any, request_id: str) -> None:
        master, info = self.manager.open_shell(request)
        data = {**{key: value for key, value in info.items() if key not in {"_inline"}}, **({"request_id": request_id} if request_id else {})}
        event = RescueEvent("result", message="Terminal started", data=data)
        try:
            if hasattr(channel, "send_with_fds"):
                channel.send_with_fds(event, [master])
            else:
                raise RescueError("unsupported_action", "This connection cannot pass a terminal")
        finally:
            os.close(master)  # the client now owns its own copy; the reaper thread watches the child


def register(
    registry: Any,
    *,
    manager: RescueManager | None = None,
    install_busy: Callable[[], str | None] | None = None,
    enabled: bool | Callable[[], bool] | None = None,
    base_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    audit_dir: str | Path | None = None,
    host: Host | None = None,
) -> RescueApi | None:
    """Add every ``rescue.*`` action and the ``rescue.v1`` feature to the server's registry.

    ``registry`` offers ``register_action(name, handler, *, feature=None)``. The daemon's ActionRegistry also offers
    ``add_shutdown_hook``, ``add_status_provider``, ``install_manager`` and ``options`` (the --rescue-* flags); when they
    exist both directions of the install/rescue exclusivity, the ``status.rescue`` summary and the directories are wired
    here, so server.py needs no rescue-specific code. ``install_busy`` returns a reason while an installation runs. Rescue
    is only enabled on the live medium unless ``enabled`` says otherwise (review R21); ``None`` is returned when it is
    disabled. Sessions an earlier daemon left behind are recovered before the first request.
    """
    host = host or Host()
    allowed = enabled() if callable(enabled) else enabled
    if allowed is None:
        allowed = on_live_medium(host)
    if not allowed:
        LOG.info("Repair actions are disabled: this system was not started from the live medium")
        return None
    options = getattr(registry, "options", None)
    install_manager = getattr(registry, "install_manager", None)
    if install_busy is None and install_manager is not None:
        def install_busy() -> str | None:
            return "An installation is running; repairs can start when it has finished." if install_manager.is_running() else None
    if manager is None:
        env = build_env(base_dir or getattr(options, "rescue_root", None) or DEFAULT_SESSION_BASE,
                        state_dir or getattr(options, "rescue_state_dir", None) or DEFAULT_STATE_DIR,
                        audit_dir or getattr(options, "rescue_audit_dir", None) or DEFAULT_AUDIT_DIR, host)
        manager = RescueManager(env, install_busy=install_busy)
    elif install_busy is not None:
        manager.install_busy = install_busy
    api = RescueApi(manager)
    for name in ACTIONS:
        registry.register_action(name, api.handler(name), feature=FEATURE)
    if hasattr(registry, "add_shutdown_hook"):
        registry.add_shutdown_hook(lambda: manager.shutdown(25.0))
    if hasattr(registry, "add_status_provider"):
        registry.add_status_provider("rescue", api.status)
    if install_manager is not None:
        install_manager.busy_check = api.install_blocker
    api.recover_orphans()
    return api
