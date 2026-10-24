"""Wave 3 review fixes in the installer window: the clock button, the cancel wording, and the result of a refused start."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal

from gentoo_installer import app as app_module
from gentoo_installer.daemon_client import DaemonState
from gentoo_installer.pages import progress as progress_module
from gentoo_installer.pages.progress import ProgressPage
from tests.ui.conftest import sample_info
from tests.ui.test_gui_recovery import FakeWorker, started, stage


@pytest.fixture
def page(qtbot, monkeypatch):
    FakeWorker.instances = []
    monkeypatch.setattr(progress_module, "BackendWorker", FakeWorker)
    widget = ProgressPage()
    qtbot.addWidget(widget)
    widget.show()
    return widget


# --- CLOCK-2: the clock button works again after an attempt ---------------------------------------------------------------------------


class ActionWorker(QObject):
    """A BackendWorker for a one-shot daemon action: it answers at once, in the calling thread."""

    eventReceived = Signal(dict)
    connectionFailed = Signal(str)
    finished = Signal()
    answer = {"ok": False, "message": "The network was asked for the time, but the clock is still off."}

    def __init__(self, manifest, socket_path="", parent=None, *, request=None, **kwargs):
        super().__init__(parent)
        self.request = request

    def start(self):
        self.eventReceived.emit({"type": "result", "data": dict(self.answer)})
        self.finished.emit()


def clock_window(make_window, monkeypatch):
    state = DaemonState("running", features=frozenset({"hardware.v1"}), actions=frozenset({"sync_time", "set_time"}))
    window = make_window()
    window.apply_info(sample_info(clock_skew=-3 * 86400, daemon=state))
    monkeypatch.setattr(app_module, "BackendWorker", ActionWorker)
    monkeypatch.setattr(window, "_collect_info", lambda: sample_info(clock_skew=-3 * 86400, daemon=state))  # still wrong afterwards
    return window


def test_the_clock_button_is_usable_again_when_the_attempt_changed_nothing(make_window, monkeypatch):
    window = clock_window(make_window, monkeypatch)
    button = window.readiness.sync_button
    assert button.isVisibleTo(window.readiness) and button.isEnabled()
    button.click()
    assert "still off" in window.readiness.action_note.text()
    assert button.isVisibleTo(window.readiness), "the clock row is still red, so the button stays"
    assert button.isEnabled(), "and it can be pressed again"


def test_the_manual_time_row_leaves_the_sync_button_usable(make_window, monkeypatch):
    window = clock_window(make_window, monkeypatch)
    monkeypatch.setattr(app_module, "ask_utc_time", lambda parent: "2026-10-24 12:00:00")
    window.run_hardware_action("set_time")
    assert window.readiness.sync_button.isEnabled()


# --- CANCEL-1: the cancel dialog and the result know that storage is running ---------------------------------------------------------


def test_the_cancel_dialog_does_not_say_nothing_changed_while_storage_runs(page):
    started(page)
    stage(page, "preflight")
    assert "No disk has been changed yet" in page.cancel_dialog().text()
    stage(page, "storage", "running")
    running = page.cancel_dialog().text()
    assert "No disk has been changed yet" not in running
    assert "being changed right now" in running and "cannot boot" in running
    stage(page, "storage", "completed")
    assert "already been modified" in page.cancel_dialog().text()


def cancelled_in(page, stage_name, **data):
    page.handle_event({"type": "error", "stage": stage_name, "message": "Installation cancelled", "error": "cancelled",
                       "data": {"code": "install.cancelled", "retryable": True, **data}})
    page.handle_event({"type": "completed", "message": "Installation cancelled", "data": {"success": False, **data}})


def test_a_cancel_inside_storage_does_not_send_the_user_to_a_resume_button_that_is_not_there(page):
    started(page)
    stage(page, "storage", "running")
    cancelled_in(page, "storage", resumable=False)
    assert not page.resume_button.isVisible()
    assert page.retry_button.text() == "Start over" and page.retry_button.isVisible() and page.settings_button.isVisible()
    shown = page.detail.text() + "\n" + page.result_hint.text()
    assert "Resume" not in shown, shown
    assert "partially installed" in page.detail.text() and "Start over" in page.detail.text()
    assert "Start over" in page.result_hint.text() and "modified" in page.result_hint.text()


def test_a_cancel_after_storage_still_points_to_resume(page):
    started(page)
    stage(page, "storage", "completed")
    cancelled_in(page, "kernel", resumable=True)
    assert page.resume_button.isVisible()
    assert "choose Resume" in page.detail.text() and "Resume continues" in page.result_hint.text()


# --- BUSY-1: a start that a repair session blocked is not "an installation is already running" ---------------------------------------------


REASON = "A repair session holds /dev/sda3; unmount it first"


def refused_start(page, **data):
    page.handle_event({"type": "error", "message": REASON, "error": REASON, "data": {"code": "install.busy", "retryable": False, **data}})
    page.handle_event({"type": "completed", "message": REASON, "data": {"success": False}})


def test_a_start_blocked_by_a_repair_session_shows_the_reason_and_no_reconnect(page):
    started(page)
    refused_start(page, blocked=True)
    assert page.stage.text() == "The installation cannot start yet"
    assert REASON in page.detail.text()
    assert "already running" not in page.detail.text() and "already running" not in page.result_hint.text()
    assert not page.reconnect_button.isVisible()
    assert page.retry_button.isVisible() and page.retry_button.text() == "Try again" and page.settings_button.isVisible()
    assert "repair" in page.result_hint.text() and "Nothing was started" in page.result_hint.text()
    assert not page.resume_button.isVisible()


def test_a_second_start_while_a_job_runs_still_offers_to_follow_it(page):
    started(page)
    refused_start(page)  # no `blocked` key: another installation
    assert page.stage.text() == "An installation is already running"
    assert page.reconnect_button.isVisible() and not page.retry_button.isVisible()
