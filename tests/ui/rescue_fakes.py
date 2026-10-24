"""A real daemon over a fake machine, for the repair window's tests.

``RescueDaemon`` is ``gentoo_installerd.server.UnixServer`` with ``rescue.api.register`` on top of a ``tests.rescue`` Lab: the wire
protocol, the event stream, the operation threads and the rescue engine are the production ones, only the computer is fake (a
recording runner, fake device trees and stateful stand-ins for cryptsetup, LVM, mount and the programs inside the system). The payloads
the window gets are therefore exactly the shapes the real daemon sends. Nothing here starts a real process or touches a device.

``CuttingProxy`` sits between the window and the daemon and can drop the window's side of one stream, which is what a lost connection
looks like; the daemon goes on with the operation, as it does for a real client that vanishes.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any, Callable

import pytest

from gentoo_installerd.rescue import api as rescue_api
from gentoo_installerd.server import ActionRegistry, InstallManager, UnixServer
from tests.rescue.flow import uefi_gentoo_lab
from tests.rescue.lab import Lab

PASSPHRASE = "correct horse battery staple"


class RescueDaemon:
    """UnixServer + the rescue actions over a Lab. ``rescue=False`` is a daemon that predates the repair engine."""

    def __init__(self, lab: Lab | None, sock_dir: Path, name: str = "daemon.sock", *, rescue: bool = True) -> None:
        self.lab = lab
        self.registry = ActionRegistry()
        base = (lab.tmp if lab is not None else sock_dir) / "installer"
        self.registry.install_manager = InstallManager(base / "target", base / "state", base / "cache")
        self.api = rescue_api.register(self.registry, manager=lab.manager(), enabled=True) if rescue and lab is not None else None
        self.path = str(sock_dir / name)
        self.server = UnixServer(self.path, self.registry.install_manager, self.registry)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, name="rescue-test-daemon", daemon=True)
        self.thread.start()

    @property
    def manager(self) -> Any:
        assert self.api is not None
        return self.api.manager

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class CuttingProxy:
    """Relays the window's requests to the daemon; ``cut(action, after_events)`` drops the window's side of the next such stream."""

    def __init__(self, upstream: str, sock_dir: Path, name: str = "proxy.sock") -> None:
        self.upstream = upstream
        self.path = str(sock_dir / name)
        self.requests: list[dict[str, Any]] = []
        self._rules: list[tuple[str, int]] = []
        self._lock = threading.Lock()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(16)
        self._server.settimeout(0.2)
        self._stop = False
        self._thread = threading.Thread(target=self._accept, name="rescue-test-proxy", daemon=True)
        self._thread.start()

    def cut(self, action: str, after_events: int) -> None:
        with self._lock:
            self._rules.append((action, after_events))

    def count(self, action: str) -> int:
        return sum(1 for request in self.requests if request.get("action") == action)

    def _take_rule(self, action: str) -> int | None:
        with self._lock:
            for index, (name, events) in enumerate(self._rules):
                if name == action:
                    del self._rules[index]
                    return events
        return None

    def _accept(self) -> None:
        while not self._stop:
            try:
                client, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._relay, args=(client,), daemon=True).start()

    def _relay(self, client: socket.socket) -> None:
        try:
            with client, client.makefile("rb") as reader:
                line = reader.readline()
                if not line:
                    return
                request = json.loads(line)
                self.requests.append(request)
                limit = self._take_rule(str(request.get("action", "")))
                upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                upstream.connect(self.upstream)
                with upstream:
                    upstream.sendall(line)
                    forwarded = 0
                    cut = False
                    with upstream.makefile("rb") as stream:
                        for event_line in stream:
                            if cut:
                                continue  # keep reading: the daemon's handler must not notice anything
                            if limit is not None and forwarded >= limit:
                                cut = True
                                try:
                                    client.shutdown(socket.SHUT_RDWR)
                                except OSError:
                                    pass
                                continue
                            client.sendall(event_line)
                            forwarded += 1
        except OSError:
            pass

    def close(self) -> None:
        self._stop = True
        try:
            self._server.close()
        except OSError:
            pass
        self._thread.join(2)


def gate_command(lab: Lab, name: str) -> tuple[threading.Event, threading.Event]:
    """Make the fake program ``name`` (run inside the system) wait: returns (entered, release). Nothing else changes about it."""
    entered, release = threading.Event(), threading.Event()
    original = lab.system.chroot_effects[name]

    def gated(system: Any, root: Path, command: tuple[str, ...], extra: dict[str, str]) -> tuple[int, str]:
        entered.set()
        assert release.wait(30), f"the test never released {name}"
        return original(system, root, command, extra)

    lab.system.chroot_effects[name] = gated
    return entered, release


@pytest.fixture
def rescue_lab_factory(tmp_path):
    """Every lab gets its own directory, like the factory in tests/rescue/conftest.py (which is not visible from tests/ui)."""
    counter = {"n": 0}

    def make(lsblk, **kwargs) -> Lab:
        counter["n"] += 1
        kwargs.setdefault("name", f"lab{counter['n']}")
        return Lab(tmp_path, lsblk, **kwargs)

    return make


@pytest.fixture
def daemon_factory(sock_dir):
    made: list[Any] = []

    def make(lab: Lab | None, **kwargs) -> RescueDaemon:
        daemon = RescueDaemon(lab, sock_dir, name=f"d{len(made)}.sock", **kwargs)
        made.append(daemon)
        return daemon

    yield make
    for daemon in made:
        daemon.close()


@pytest.fixture
def proxy_factory(sock_dir):
    made: list[CuttingProxy] = []

    def make(daemon: RescueDaemon) -> CuttingProxy:
        proxy = CuttingProxy(daemon.path, sock_dir, name=f"p{len(made)}.sock")
        made.append(proxy)
        return proxy

    yield make
    for proxy in made:
        proxy.close()


@pytest.fixture
def gentoo_machine(rescue_lab_factory, daemon_factory):
    """A UEFI computer with Gentoo on /dev/nvme0n1p5 (and a Debian on a second disk), behind a real daemon: (daemon, lab, root tree)."""
    lab, tree = uefi_gentoo_lab(rescue_lab_factory)
    return daemon_factory(lab), lab, tree


def wait_for(qtbot: Any, predicate: Callable[[], Any], timeout: int = 20000, message: str = "") -> None:
    """qtbot.waitUntil with a readable failure."""
    qtbot.waitUntil(predicate, timeout=timeout)
    if message:
        assert predicate(), message
