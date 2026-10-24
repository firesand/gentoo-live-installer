from __future__ import annotations

import argparse
import grp
import json
import os
import queue
import socket
import socketserver
import stat
import sys
import threading
from pathlib import Path
from typing import Any

from gentoo_installer.manifest import InstallManifest, ManifestError

from .events import Event, PROTOCOL_VERSION
from .inspect import Discovery
from .install import Installer
from .runner import SubprocessRunner

DEFAULT_SOCKET = "/run/gentoo-installer/server.sock"
MAX_REQUEST_BYTES = 1024 * 1024
SOCKET_GROUP = "gentoo-installer"


class InstallManager:
    def __init__(self, target_root: Path, state_dir: Path, cache_dir: Path) -> None:
        self.target_root = target_root
        self.state_dir = state_dir
        self.cache_dir = cache_dir
        self.cancel = threading.Event()
        self.installer: Installer | None = None
        self.thread: threading.Thread | None = None
        self.status = "idle"
        self.last_error = ""
        self.listeners: list[queue.Queue[Event | None]] = []
        self.lock = threading.Lock()

    def start(self, manifest: InstallManifest, resume: bool = False) -> queue.Queue[Event | None]:
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise RuntimeError("An installation is already running")
            cancel = threading.Event()
            installer = Installer(
                manifest,
                target_root=self.target_root,
                state_dir=self.state_dir,
                cache_dir=self.cache_dir,
                cancel=cancel,
                resume=resume,
                emit=self._emit,
            )
            listener: queue.Queue[Event | None] = queue.Queue()
            self.cancel = cancel
            self.listeners = [listener]
            self.status = "running"
            self.last_error = ""
            self.installer = installer
            self.thread = threading.Thread(target=self._run, args=(installer,), name="gentoo-install", daemon=True)
            self.thread.start()
            return listener

    def _run(self, installer: Installer) -> None:
        result = installer.run()
        with self.lock:
            self.status = "completed" if result and not self.cancel.is_set() else "cancelled" if self.cancel.is_set() else "failed"
            if self.status == "failed":
                self.last_error = "Installation failed; inspect the streamed error and persistent log"
            listeners = list(self.listeners)
        for listener in listeners:
            listener.put(None)

    def _emit(self, event: Event) -> None:
        with self.lock:
            listeners = list(self.listeners)
            if event.type == "error":
                self.last_error = event.error or event.message
        for listener in listeners:
            listener.put(event)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"status": self.status, "job_id": self.installer.job_id if self.installer else "", "error": self.last_error}

    def request_cancel(self) -> None:
        with self.lock:
            if not self.thread or not self.thread.is_alive():
                raise RuntimeError("No installation is running")
            self.cancel.set()


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, manager: InstallManager) -> None:
        self.manager = manager
        super().__init__(path, RequestHandler)


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(30)
        line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_REQUEST_BYTES:
            self.send(Event("error", error="Request is too large", message="Request is too large"))
            return
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get("version") != PROTOCOL_VERSION:
                raise ValueError(f"Protocol version must be {PROTOCOL_VERSION}")
            action = request.get("action")
            if action == "inspect":
                inspection = Discovery(SubprocessRunner()).inspect()
                self.send(Event("status", message="Inspection completed", data=inspection.to_dict()))
                self.send(Event("completed", message="Inspection completed", data={"success": True}))
            elif action == "status":
                self.send(Event("status", data=self.server.manager.snapshot()))  # type: ignore[attr-defined]
            elif action == "cancel":
                self.server.manager.request_cancel()  # type: ignore[attr-defined]
                self.send(Event("status", message="Cancellation requested", data={"status": "cancelling"}))
            elif action == "start":
                raw = request.get("manifest")
                manifest = InstallManifest.from_dict(raw)
                manifest.require_valid()
                listener = self.server.manager.start(manifest, bool(request.get("resume", False)))  # type: ignore[attr-defined]
                self.send(Event("status", message="Installation started", data={"status": "running"}))
                while True:
                    event = listener.get()
                    if event is None:
                        break
                    self.send(event)
            else:
                raise ValueError("Action must be inspect, start, status, or cancel")
        except (ValueError, ManifestError, RuntimeError, json.JSONDecodeError) as exc:
            self.send(Event("error", message=str(exc), error=str(exc)))

    def send(self, event: Event) -> None:
        try:
            self.wfile.write((event.to_json() + "\n").encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            if isinstance(event, Event):
                self.connection.close()


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


def serve(socket_path: str | Path = DEFAULT_SOCKET, *, target_root: str | Path = "/mnt/gentoo", state_dir: str | Path = "/var/lib/gentoo-installer", cache_dir: str | Path = "/var/tmp/gentoo-installer", require_root: bool = True) -> None:
    if require_root and hasattr(os, "geteuid") and os.geteuid() != 0:
        raise PermissionError("gentoo-installerd must run as root")
    path = Path(socket_path)
    group_id = prepare_socket_directory(path.parent)
    try:
        if path.exists() and not stat.S_ISSOCK(path.lstat().st_mode):
            raise RuntimeError(f"Refusing to replace non-socket path: {path}")
        path.unlink(missing_ok=True)
        manager = InstallManager(Path(target_root), Path(state_dir), Path(cache_dir))
        with UnixServer(str(path), manager) as server:
            secure_socket(path, group_id)
            server.serve_forever(poll_interval=0.2)
    finally:
        path.unlink(missing_ok=True)


def run_cli(manifest_path: Path, *, dry_run: bool, target_root: Path, state_dir: Path, cache_dir: Path, resume: bool) -> int:
    manifest = InstallManifest.load(manifest_path)
    installer = Installer(manifest, target_root=target_root, state_dir=state_dir, cache_dir=cache_dir, dry_run=dry_run, resume=resume, emit=lambda event: print(event.to_json(), flush=True))
    return 0 if installer.run() else 1


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(os, "geteuid") and os.geteuid() != 0 and not args.dry_run:
        print("gentoo-installerd must run as root (except for --dry-run)", file=sys.stderr)
        return 1
    try:
        if args.manifest:
            if not args.dry_run and not args.yes:
                print("--manifest requires either --dry-run or explicit --yes", file=sys.stderr)
                return 2
            return run_cli(args.manifest, dry_run=args.dry_run, target_root=Path(args.target_root), state_dir=Path(args.state_dir), cache_dir=Path(args.cache_dir), resume=args.resume)
        if args.dry_run or args.yes:
            print("--dry-run and --yes require --manifest", file=sys.stderr)
            return 2
        serve(args.socket, target_root=args.target_root, state_dir=args.state_dir, cache_dir=args.cache_dir)
        return 0
    except (OSError, ValueError, ManifestError, PermissionError) as exc:
        print(f"gentoo-installerd: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
