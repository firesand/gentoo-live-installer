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
import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable

import pytest
import shiboken6

from gentoo_installer import keyboard
from gentoo_installer.rescue_app import RescueWindow
from gentoo_installer.rescue_ui import find_page, launcher
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
        try:
            os.unlink(self.path)  # a new proxy can take the same address (the daemon "came back")
        except OSError:
            pass


class Gate:
    """Holds one command until the test lets it go. ``entered`` is set when the command is waiting; arm() starts the holding."""

    def __init__(self, armed: bool = True) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.armed = threading.Event()
        if armed:
            self.armed.set()

    def arm(self) -> None:
        self.armed.set()

    def hold(self, what: Any) -> None:
        if self.armed.is_set() and not self.entered.is_set():
            self.entered.set()
            assert self.release.wait(30), f"the test never released {what}"


def gate_host_command(lab: Lab, *prefix: str) -> Gate:
    """Hold the first host command that starts with ``prefix`` (for example blockdev --setrw) once the gate is armed."""
    gate = Gate(armed=False)
    original = lab.system.run

    def gated(argv: Any, **kwargs: Any) -> Any:
        inner = tuple(argv)[3:] if tuple(argv)[:1] == ("timeout",) else tuple(argv)
        if inner[: len(prefix)] == prefix:
            gate.hold(prefix)
        return original(argv, **kwargs)

    lab.system.run = gated  # type: ignore[method-assign]
    return gate


def gate_command(lab: Lab, name: str) -> Gate:
    """Hold the fake program ``name``, run inside the system: it only ever runs as a step of a repair, so the gate is armed at once."""
    gate = Gate()
    original = lab.system.chroot_effects[name]

    def gated(system: Any, root: Path, command: tuple[str, ...], extra: dict[str, str]) -> tuple[int, str]:
        gate.hold(name)
        return original(system, root, command, extra)

    lab.system.chroot_effects[name] = gated
    return gate


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
        kwargs.setdefault("name", f"d{len(made)}.sock")
        daemon = RescueDaemon(lab, sock_dir, **kwargs)
        made.append(daemon)
        return daemon

    yield make
    for daemon in made:
        daemon.close()


@pytest.fixture
def proxy_factory(sock_dir):
    made: list[CuttingProxy] = []

    def make(daemon: RescueDaemon | str, name: str = "") -> CuttingProxy:
        proxy = CuttingProxy(daemon if isinstance(daemon, str) else daemon.path, sock_dir, name=name or f"p{len(made)}.sock")
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


class RepairKit:
    """Opens repair windows with every side effect replaced, and closes them afterwards."""

    def __init__(self, qtbot: Any) -> None:
        self.qtbot = qtbot
        self.windows: list[RescueWindow] = []
        self.launched: list[tuple[str, list[str]]] = []  # programs the window tried to start: (program, arguments)
        self.keyboard_calls: list[tuple[str, list[str]]] = []  # what the keyboard chooser would have started (setxkbmap)
        self.terminal_available = True

    def open(self, socket_path: str, *, width: int = 1024, height: int = 600) -> RescueWindow:
        window = RescueWindow(socket_path)
        self.windows.append(window)
        window.resize(width, height)
        window.show()
        return window

    def start_detached(self, program: str, arguments: list[str]) -> bool:
        self.launched.append((program, list(arguments)))
        return True

    def start_keyboard(self, program: str, arguments: list[str]) -> bool:
        self.keyboard_calls.append((program, list(arguments)))
        return True

    def close(self) -> None:
        for window in self.windows:
            if shiboken6.isValid(window):
                window.force_close()
        self.qtbot.wait(20)


@pytest.fixture
def repair_kit(qtbot, monkeypatch):
    kit = RepairKit(qtbot)
    monkeypatch.setattr(launcher, "start_detached", kit.start_detached)
    monkeypatch.setattr(launcher, "find_terminal", lambda which=None: "xfce4-terminal" if kit.terminal_available else None)
    monkeypatch.setattr(keyboard, "start_detached", kit.start_keyboard)  # the keyboard chooser must not start setxkbmap
    monkeypatch.setattr(find_page, "read_layout", lambda: ("us", ""))
    yield kit
    kit.close()


def system_card(window: RescueWindow, device: str) -> str:
    """The id of the system the scan found on ``device``."""
    return next(item["id"] for item in window.find_page.candidates if item["device"] == device)


def open_system(qtbot: Any, window: RescueWindow, device: str = "/dev/nvme0n1p5") -> Any:
    """Drive the window from the first scan to the diagnosis of the system on ``device``."""
    from gentoo_installer.rescue_app import DIAGNOSE

    qtbot.waitUntil(lambda: any(item["device"] == device for item in window.find_page.candidates) and not window.find_page._busy, timeout=20000)
    window.find_page.group.set_value(system_card(window, device))
    window.find_page.continue_button.click()
    qtbot.waitUntil(lambda: window.mount_page.open_button.isEnabled(), timeout=20000)
    window.mount_page.open_button.click()
    qtbot.waitUntil(lambda: window.stack.currentIndex() == DIAGNOSE and window.system is not None and not window.diagnose_page.busy.isVisibleTo(window.diagnose_page), timeout=20000)
    return window.system


def request_action(qtbot: Any, window: RescueWindow, action_id: str, state: str = "review") -> None:
    """Click the diagnosis page's button for an action and wait until the plan page is in ``state``."""
    button = window.diagnose_page.findChild(object, f"rescueAction:{action_id}")
    assert button is not None and button.isEnabled(), action_id
    button.click()
    qtbot.waitUntil(lambda: window.plan_page.state == state, timeout=20000)


def with_live_mounts(lab: Lab) -> Lab:
    """Make the fake lsblk show what the fake kernel has mounted, as the real one does (the fixtures print a fixed list)."""
    static = lab.system._lsblk
    assert isinstance(static, str)

    def current(system: Any) -> str:
        document = json.loads(static)
        mounts: dict[str, list[str]] = {}
        for target, record in system.mounts.items():
            if not record.bind:
                mounts.setdefault(record.device, []).append(target)

        def walk(nodes: list[dict[str, Any]]) -> None:
            for node in nodes:
                node["mountpoints"] = [*node.get("mountpoints", []), *mounts.get(node["path"], [])]
                walk(node.get("children", []))

        walk(document["blockdevices"])
        return json.dumps(document)

    lab.system._lsblk = current
    return lab
