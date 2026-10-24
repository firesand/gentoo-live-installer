"""Shared pytest configuration for the whole suite.

* Qt always runs on the offscreen platform, so a plain ``pytest`` (IDE, CI, a
  Wayland or X11 session) never opens windows. Set ``GENTOO_TEST_QT_PLATFORM``
  to watch the GUI tests on a real platform on purpose.
* Every run gets its own short, symlink-free temporary directory. Tests that use
  ``tempfile`` directly (the product refuses symlinked target roots) therefore
  never cross a symlinked ``TMPDIR``, and anything a test leaks is removed at
  the end of the session.
* ``sock_dir`` and ``unix_listener`` give AF_UNIX tests a directory short enough
  for ``sun_path`` (107 bytes) and a listener whose ``accept()`` cannot block
  forever.
* A hung test must end the run: a per-test timeout (the ``pytest-timeout``
  plugin when it is installed, otherwise a small SIGALRM fallback with the same
  ``--timeout`` option, ``timeout`` ini key and ``@pytest.mark.timeout``) and a
  last-resort guard against non-daemon threads that would keep the interpreter
  alive after the run.
"""
from __future__ import annotations

import contextlib
import faulthandler
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
from pathlib import Path
from typing import Iterator

import pytest

# Must happen before anything imports Qt, which is why it is module level.
os.environ["QT_QPA_PLATFORM"] = os.environ.get("GENTOO_TEST_QT_PLATFORM") or "offscreen"

SUN_PATH_MAX = 107
# Seconds the backstop waits after the in-process timeout before it gives up on
# a test that is stuck in native code (for example a Qt event loop).
BACKSTOP_GRACE = 30.0

_session_tmp: Path | None = None
_exit_status = 1


def pytest_addoption(parser: pytest.Parser, pluginmanager: pytest.PytestPluginManager) -> None:
    """Offer the pytest-timeout interface when that plugin is not installed."""
    if pluginmanager.hasplugin("timeout"):
        return
    parser.getgroup("timeout").addoption(
        "--timeout", type=float, default=None, metavar="SECONDS",
        help="Fail a test that runs longer than this many seconds (0 disables).")
    parser.addini("timeout", "Per-test timeout in seconds (fallback for pytest-timeout).", default=None)


def pytest_configure(config: pytest.Config) -> None:
    global _session_tmp
    config.addinivalue_line("markers", "timeout(seconds): fail the test after this many seconds (0 disables).")
    config.addinivalue_line("markers", "slow: runs real subprocesses or sleeps for a few seconds.")
    base = os.path.realpath(tempfile.gettempdir())
    # pytest keeps the temp dirs of failed tests for post-mortems: leave them where they were.
    os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", base)
    _session_tmp = Path(tempfile.mkdtemp(prefix="gi-t-", dir=base))
    tempfile.tempdir = str(_session_tmp)
    os.environ["TMPDIR"] = str(_session_tmp)
    # Never let a test write the real user's Qt settings.
    os.environ.setdefault("XDG_CONFIG_HOME", str(_session_tmp / "xdg-config"))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    global _exit_status
    _exit_status = int(exitstatus)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    _stop_stray_threads()
    if _session_tmp is not None:
        shutil.rmtree(_session_tmp, ignore_errors=True)


def _stop_stray_threads() -> None:
    """Exit instead of hanging when a test left a non-daemon thread running.

    A listener thread whose ``accept()`` never returns (a failing test that
    never connected) is joined by the interpreter at shutdown, so the failing
    run would never finish. Give such threads a moment, then say so and exit
    with the status pytest computed.
    """
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    main = threading.main_thread()
    stray = [t for t in threading.enumerate() if t is not main and not t.daemon and t.is_alive()]
    for thread in stray:
        thread.join(timeout=1.0)
    stray = [t for t in stray if t.is_alive()]
    if not stray:
        return
    names = ", ".join(sorted(t.name for t in stray))
    sys.stderr.write(f"\nconftest: leaving {len(stray)} non-daemon thread(s) running ({names}); "
                     "exiting without waiting for them. Make test threads daemon=True with a socket timeout.\n")
    sys.stdout.flush()
    sys.stderr.flush()
    if _session_tmp is not None:
        shutil.rmtree(_session_tmp, ignore_errors=True)
    os._exit(_exit_status)


def _timeout_for(item: pytest.Item) -> float:
    marker = item.get_closest_marker("timeout")
    if marker is not None:
        return float(marker.args[0] if marker.args else marker.kwargs.get("timeout", 0) or 0)
    option = item.config.getoption("timeout", default=None)
    if option is None:
        option = item.config.getini("timeout")
    try:
        return float(option or 0)
    except (TypeError, ValueError):
        return 0.0


@contextlib.contextmanager
def _deadline(seconds: float, item: pytest.Item) -> Iterator[None]:
    if (seconds <= 0 or not hasattr(signal, "setitimer")
            or threading.current_thread() is not threading.main_thread()):
        yield
        return

    def on_alarm(_signum: int, _frame: object) -> None:
        pytest.fail(f"Timeout: {item.nodeid} ran longer than {seconds:g}s", pytrace=False)

    def backstop() -> None:  # the alarm could not interrupt native code
        sys.stderr.write(f"\nconftest: {item.nodeid} is stuck after {seconds + BACKSTOP_GRACE:g}s; "
                         "dumping all thread stacks and exiting.\n")
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        sys.stderr.flush()
        os._exit(1)

    previous = signal.signal(signal.SIGALRM, on_alarm)
    guard = threading.Timer(seconds + BACKSTOP_GRACE, backstop)
    guard.daemon = True
    guard.start()
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        guard.cancel()
        signal.signal(signal.SIGALRM, previous)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item):
    if item.config.pluginmanager.hasplugin("timeout"):
        return (yield)
    with _deadline(_timeout_for(item), item):
        return (yield)


@pytest.fixture
def sock_dir(tmp_path: Path) -> Iterator[Path]:
    """A directory for Unix sockets: tmp_path, or a short one when tmp_path is too deep."""
    if len(os.fsencode(tmp_path / "installer-test.sock")) < SUN_PATH_MAX - 8:
        yield tmp_path
        return
    base = os.environ.get("GENTOO_INSTALLER_TEST_SOCKET_DIR", "/tmp")
    path = Path(tempfile.mkdtemp(prefix="gi-", dir=os.path.realpath(base)))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def unix_listener(sock_dir: Path):
    """Factory for listening Unix sockets whose accept() gives up after five seconds.

    ``path, server = unix_listener("installer.sock", backlog=2)``; the server
    socket is closed at teardown. Serve threads built on it should be
    ``daemon=True``.
    """
    created: list[socket.socket] = []

    def make(name: str = "installer.sock", backlog: int = 1) -> tuple[str, socket.socket]:
        path = str(sock_dir / name)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(backlog)
        server.settimeout(5)
        created.append(server)
        return path, server

    yield make
    for server in created:
        with contextlib.suppress(OSError):
            server.close()
