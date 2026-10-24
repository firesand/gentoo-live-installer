"""The window: a failing or stuck poll never freezes it, and it fits small laptop screens."""

import threading
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QRect, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import hwmonux  # noqa: E402
from sensors import Device  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication(["hwmonux-test"])


@pytest.fixture(autouse=True)
def private_settings(tmp_path, monkeypatch, qapp):
    """MainWindow stores its geometry with QSettings: keep the user's own file out of it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, str(tmp_path / "qsettings"))
    yield


class FakeMonitor:
    hostname = "testhost"

    def __init__(self, poll=None):
        self.calls = 0
        self._poll = poll
        dev = Device("Fake device", "chip")
        dev.add("Utilization", "Load", "percent", lambda: 42.0)
        self.devices = [dev]
        for sensor in dev.sensors():
            sensor.update()

    def poll(self):
        self.calls += 1
        if self._poll:
            self._poll(self)

    def reset(self):
        pass


class FakeScreen:
    def __init__(self, width, height):
        self._area = QRect(0, 0, width, height)

    def availableGeometry(self):
        return self._area


def finish_poll(window):
    window._poller.join(timeout=5)
    assert not window._poller.is_alive()
    QApplication.processEvents()


def test_clamp_size_for_laptop_screens():
    assert hwmonux.clamp_size(800, 900, 1366, 728) == (800, 655)   # 1366x768 panel minus the taskbar
    assert hwmonux.clamp_size(800, 900, 1280, 720) == (800, 648)
    assert hwmonux.clamp_size(800, 900, 640, 480) == (576, 432)
    assert hwmonux.clamp_size(800, 900, 3840, 2160) == (800, 900)  # a big screen leaves the default alone


def test_window_fits_a_1366x768_screen(monkeypatch):
    monkeypatch.setattr(hwmonux.MainWindow, "screen", lambda self: FakeScreen(1366, 728))
    w = hwmonux.MainWindow(FakeMonitor())
    try:
        assert w.width() <= int(1366 * 0.9) and w.height() <= int(728 * 0.9)
        assert (w.width(), w.height()) == (800, 655)
    finally:
        w.close()


def test_a_remembered_oversized_geometry_is_clamped_too(monkeypatch):
    big = hwmonux.MainWindow(FakeMonitor())
    big.resize(1800, 1000)
    big.close()  # stores the geometry
    monkeypatch.setattr(hwmonux.MainWindow, "screen", lambda self: FakeScreen(1280, 720))
    small = hwmonux.MainWindow(FakeMonitor())
    try:
        assert small.width() <= 1152 and small.height() <= 648
    finally:
        small.close()


def test_without_any_screen_the_default_size_stays(monkeypatch):
    monkeypatch.setattr(hwmonux.MainWindow, "screen", lambda self: None)
    monkeypatch.setattr(QApplication, "primaryScreen", staticmethod(lambda: None))
    w = hwmonux.MainWindow(FakeMonitor())
    try:
        assert (w.width(), w.height()) == hwmonux.WINDOW_SIZE
    finally:
        w.close()


def test_every_poll_reports_back_even_when_it_fails():
    def flaky(monitor):
        if monitor.calls == 2:
            raise RuntimeError("sensor chip vanished")

    w = hwmonux.MainWindow(FakeMonitor(flaky))
    seen = []
    w.polled.connect(lambda: seen.append(1))
    try:
        for expected in (1, 2, 3):
            w.poll()
            finish_poll(w)
            assert len(seen) == expected, f"poll {expected} did not emit polled"
    finally:
        w.close()


def test_a_stuck_poll_does_not_keep_the_window_open():
    release = threading.Event()
    w = hwmonux.MainWindow(FakeMonitor(lambda monitor: release.wait(30)))
    late = []
    w.polled.connect(lambda: late.append(1))
    errors = []
    old_hook = threading.excepthook
    threading.excepthook = lambda args: errors.append(args.exc_value)
    try:
        w.poll()
        deadline = time.monotonic() + 5
        while w.monitor.calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        started = time.monotonic()
        w.close()
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f"close() blocked for {elapsed:.1f} s"
        release.set()  # the stuck read finally returns, after the window is gone
        w._poller.join(timeout=5)
        QApplication.processEvents()
        assert not w._poller.is_alive()
        assert errors == [], errors
        assert late == [], "a poll that finished after closing must not signal the dead window"
    finally:
        threading.excepthook = old_hook
        release.set()


def test_values_show_up_in_the_tree():
    w = hwmonux.MainWindow(FakeMonitor())
    try:
        w.poll()
        finish_poll(w)
        texts = [row[0].text(1) for row in w._rows]
        assert texts == ["42.0 %"]
    finally:
        w.close()
