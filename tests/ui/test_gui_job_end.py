"""The end of an install job: the daemon says what went wrong, then cleans up, and only then is the job over.

A VM run with the real Cancel and Resume buttons found what no test here held open: the page offered Resume on the error event, and the
old job's cleanup and completed event then landed in the resumed run. The workers below can hold that gap; the helpers of the older tests
send error and completed back to back.
"""

import json
import socket
import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QPushButton

from gentoo_installer.manifest import InstallManifest
from gentoo_installer.pages import progress as progress_module
from gentoo_installer.pages.progress import CANCEL_WARNING, CANCEL_WARNING_BOOTABLE, ERASE_AGAIN, ProgressPage


class Worker(QObject):
    eventReceived = Signal(dict)
    connectionFailed = Signal(str)
    cancelAcknowledged = Signal(bool, str)
    finished = Signal()
    instances: list["Worker"] = []

    def __init__(self, manifest, socket_path="", parent=None, **kwargs):
        super().__init__(parent)
        self.kwargs, self.cancels = kwargs, 0
        Worker.instances.append(self)

    def start(self):
        pass

    def isRunning(self):
        return True

    def cancel(self):
        self.cancels += 1

    def send(self, kind, message="", **fields):
        data = fields.pop("data", {})
        self.eventReceived.emit({"type": kind, "message": message, "data": data, **fields})

    def stage(self, name, status="running", **data):
        self.send("stage", f"{status} {name}", stage=name, data={"status": status, **data})


class Spy:
    def __init__(self, page):
        self.finished, self.stopped = [], []
        page.finished.connect(self.finished.append)
        page.workerStopped.connect(lambda: self.stopped.append(True))


@pytest.fixture
def page(qtbot, monkeypatch):
    Worker.instances = []
    monkeypatch.setattr(progress_module, "BackendWorker", Worker)
    widget = ProgressPage()
    qtbot.addWidget(widget)
    widget.show()
    return widget


def running(page, *stages):
    page.start(InstallManifest(), "/run/none.sock")
    worker = Worker.instances[-1]
    worker.stage("storage", "completed")
    for name in stages:
        worker.stage(name, "completed")
    return worker


def starts_a_run(page):
    return {command.text() for command in (page.resume_button, page.retry_button, page.settings_button, page.repair_button) if command.available}


def always_there(page):
    return {command.text() for command in (page.save_log_button, page.diagnostics_button, page.quit_button) if command.available}


def cancelled(worker, stage="stage3", **data):
    worker.send("error", "Installation cancelled", error="cancelled", stage=stage, data={"code": "install.cancelled", "resumable": True, "retryable": True, **data})


def cleanup(worker):
    worker.stage("cleanup")
    worker.send("log", "$ umount -R /mnt/gentoo")
    worker.stage("cleanup", "completed")


def ended(worker, *, message="Installation cancelled", **data):
    worker.send("completed", message, data={"success": False, "resumable": True, **data})


# --- the gap between the error and the end of the job ------------------------------------------------------------


def test_nothing_that_starts_a_run_is_offered_until_the_job_is_over(page):
    spy = Spy(page)
    worker = running(page)
    worker.stage("stage3")
    cancelled(worker)
    assert page.stage.text() == "Installation cancelled"
    assert page.detail.text() == "Finishing and unmounting…"
    assert starts_a_run(page) == set() and not page.reboot.isVisible()
    assert "releasing the disks" in page.result_hint.text()
    assert always_there(page) == {"Save log…", "Save diagnostics…", "Quit"}  # a cleanup that hangs must not take these away
    assert spy.finished == []

    cleanup(worker)
    assert starts_a_run(page) == set()

    ended(worker)
    assert page.resume_button.available
    assert ERASE_AGAIN in starts_a_run(page) and "Back to settings" in starts_a_run(page) and "Open the repair tool" in starts_a_run(page)
    assert page.detail.text() == "The disk is partially installed; choose Resume or Back to settings."
    assert spy.finished == [False]


def test_a_failed_stage_waits_the_same_way_and_keeps_saying_why(page):
    spy = Spy(page)
    worker = running(page)
    worker.stage("base")
    worker.send("error", "emerge failed: dev-libs/foo", error="emerge failed: dev-libs/foo", stage="base", data={"retryable": True, "resumable": True})
    assert "emerge failed" in page.detail.text() and starts_a_run(page) == set() and spy.finished == []
    cleanup(worker)
    ended(worker, message="Installation failed")
    assert "emerge failed" in page.detail.text()
    assert page.resume_button.available and spy.finished == [False]


def test_a_system_that_can_start_is_offered_only_when_the_disks_are_released(page):
    worker = running(page, "bootloader")
    worker.stage("desktop")
    worker.send("error", "the desktop failed", error="the desktop failed", stage="desktop", data={"bootable": True, "retryable": True, "resumable": True})
    assert page.stage.text() == "Your new system is installed and can start"
    assert not page.reboot.isVisible() and not page.resume_button.available
    ended(worker, message="Installation failed", bootable=True)
    assert page.reboot.isVisible() and page.resume_button.text() == "Retry the desktop step" and page.resume_button.available


def test_what_only_the_completed_event_knows_still_reaches_the_page(page):
    worker = running(page)
    worker.send("error", "the last check failed", error="the last check failed", stage="validation", data={"retryable": True})
    ended(worker, message="Installation failed", bootable=True, degraded=["KDE games"])
    assert page.stage.text() == "Your new system is installed and can start" and page.reboot.isVisible()
    assert "KDE games" in page.outcome_details.text()


@pytest.mark.parametrize("sequence", ["finished", "connection-failed-then-finished", "failed-event"])
def test_a_stream_that_ends_without_completed_still_offers_what_comes_next(page, sequence):
    spy = Spy(page)
    worker = running(page)
    if sequence == "failed-event":
        worker.send("failed", "boom", error="boom", stage="base", data={"retryable": True})  # an old service: BackendWorker stops reading here
    else:
        cancelled(worker)
    assert starts_a_run(page) == set()
    if sequence == "connection-failed-then-finished":
        worker.connectionFailed.emit("the socket went away")  # the service is gone too, so this already ends it
    worker.finished.emit()
    assert page.resume_button.available and spy.finished == [False] and spy.stopped == [True]
    assert "boom" in page.detail.text() if sequence == "failed-event" else page.stage.text() == "Installation cancelled"  # the cause, not "the backend stopped"


def test_a_stream_lost_while_the_job_still_cleans_up_offers_only_to_reconnect(page, monkeypatch):
    spy = Spy(page)
    state = SimpleNamespace(reachable=True, job_running=True, status={}, supports=lambda _: True)
    monkeypatch.setattr(progress_module.daemon_client, "probe", lambda *a, **k: state)
    worker = running(page)
    worker.send("log", "a line of the job", seq=40)
    cancelled(worker)
    worker.finished.emit()
    assert starts_a_run(page) == set() and page.reconnect_button.available
    assert page.reconnect_button.presentation == "primary"
    assert spy.finished == []  # the job is not over: the window stays locked

    page.reconnect_button.button.click()
    again = Worker.instances[-1]
    assert again.kwargs == {"attach": True, "after_seq": 0}  # the replay brings back the error that said why
    again.stage("storage", "completed")
    cancelled(again)
    ended(again)
    again.finished.emit()
    assert page.resume_button.available and spy.finished == [False]


def test_nothing_is_the_primary_button_while_the_page_asks_for_patience(page):
    worker = running(page)
    cancelled(worker)
    assert [command.text() for command in page.commands if command.presentation == "primary"] == []
    ended(worker)
    assert [command.text() for command in page.commands if command.presentation == "primary"] == ["Resume installation"]


def test_a_cancel_before_the_disk_changed_names_its_buttons_only_when_they_are_there(page):
    page.start(InstallManifest(), "/run/none.sock")
    worker = Worker.instances[-1]
    cancelled(worker, stage="preflight")
    assert page.detail.text() == "No disk has been changed."
    ended(worker, resumable=False)
    assert page.detail.text() == "No disk has been changed. Choose Try again or Back to settings."
    assert "Try again" in starts_a_run(page)


def test_a_connection_that_never_came_up_reports_at_once(page):
    spy = Spy(page)
    page.start(InstallManifest(), "/run/none.sock")
    Worker.instances[-1].connectionFailed.emit("no such socket")
    assert page.stage.text().startswith("Installation needs attention")
    assert "Try again" in starts_a_run(page) and spy.finished == [False]


@pytest.mark.parametrize("data", [{"code": "install.busy", "retryable": False}, {"code": "install.busy", "blocked": True, "retryable": False}])
@pytest.mark.parametrize("with_completed", [True, False])
def test_a_refused_start_offers_its_commands_however_the_stream_ends(page, data, with_completed):
    spy = Spy(page)
    page.start(InstallManifest(), "/run/none.sock")
    worker = Worker.instances[-1]
    worker.send("error", "refused", error="refused", data=data)
    if with_completed:
        ended(worker, message="Installation failed", resumable=False)
    worker.finished.emit()
    assert spy.finished == [False]
    if data.get("blocked"):
        assert "Try again" in starts_a_run(page)
    else:
        assert page.reconnect_button.available


def test_attaching_in_the_gap_waits_for_the_same_end(page):
    page.attach("/run/none.sock")
    worker = Worker.instances[-1]
    worker.stage("storage", "completed")
    cancelled(worker)  # the replay ends here: the job is still cleaning up
    assert starts_a_run(page) == set()
    ended(worker)
    assert "Back to settings" in starts_a_run(page)


# --- a worker that was replaced -----------------------------------------------------------------------------------


def test_the_old_jobs_late_events_are_not_the_result_of_the_resumed_run(page):
    spy = Spy(page)
    old = running(page)
    old.stage("stage3")
    cancelled(old)
    page.resume()  # what a queued click can still reach; the button itself is not offered here
    new = Worker.instances[-1]
    assert new is not old and new.kwargs == {"resume": True}

    old.stage("cleanup")
    ended(old)
    old.send("log", "seq", seq=1439)
    old.finished.emit()
    assert page.stage.text() == "Resuming installation…"
    assert page.cancel.isVisible() and not page.result_panel.isVisible()
    assert page.last_seq == 0  # a later Reconnect must not skip the new job's first events
    assert spy.finished == [] and spy.stopped == []

    new.stage("stage3")
    new.send("progress", "Unpacking", stage="stage3", progress=30)
    assert page.detail.text() == "Unpacking"

    new.send("completed", "Installation completed successfully", progress=100.0, data={"success": True, "bootable": True})
    assert page.stage.text() == "Installation finished: remove the USB stick, then restart."
    assert spy.finished == [True]
    new.finished.emit()
    assert spy.stopped == [True]


def test_a_retired_workers_failure_and_refused_cancel_reach_nothing(page):
    old = running(page)
    cancelled(old)
    ended(old)
    page.resume()
    old.connectionFailed.emit("gone")
    old.cancelAcknowledged.emit(False, "not running")
    assert page.stage.text() == "Resuming installation…"
    assert page.cancel.isEnabled() and "not accepted" not in page.detail.text()


def test_the_wizard_stays_locked_when_the_old_worker_stops_during_the_resumed_run(make_window, monkeypatch):
    Worker.instances = []
    monkeypatch.setattr(progress_module, "BackendWorker", Worker)
    window = make_window()
    window.manifest.storage.device = "/dev/sda"
    window._show_page(7)
    window.progress.start(InstallManifest(), "/run/none.sock")
    old = Worker.instances[-1]
    assert window.install_started
    old.stage("storage", "completed")
    cancelled(old)
    window.progress.resume()
    ended(old)
    old.finished.emit()
    assert window.install_started  # closing the window still asks, and the sidebar stays locked
    assert not any(button.isEnabled() for button in window.step_buttons)
    new = Worker.instances[-1]
    ended(new, message="Installation failed")
    new.finished.emit()
    assert not window.install_started


# --- real threads and a real socket -------------------------------------------------------------------------------


class ScriptedDaemon:
    """Every connection gets the next script. A script holds at a threading.Event, as a job holds between its error and its end."""

    def __init__(self, path, scripts):
        self.requests, self.scripts = [], list(scripts)
        self.holds = [step for script in self.scripts for step in script if isinstance(step, threading.Event)]
        self.path = str(path)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(4)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(connection, self.scripts.pop(0)), daemon=True).start()

    def handle(self, connection, script):
        with connection, connection.makefile("rwb") as stream:
            self.requests.append(json.loads(stream.readline()))
            for step in script:
                if isinstance(step, threading.Event):
                    step.wait(10)
                else:
                    stream.write(json.dumps(step).encode() + b"\n")
                    stream.flush()


def stage_event(name, status, seq=1):
    return {"type": "stage", "stage": name, "seq": seq, "message": name, "data": {"status": status}}


ERROR = {"type": "error", "seq": 2, "stage": "stage3", "error": "cancelled", "message": "Installation cancelled",
         "data": {"code": "install.cancelled", "resumable": True, "retryable": True}}
END = {"type": "completed", "seq": 9, "message": "Installation cancelled", "data": {"success": False, "resumable": True}}
SUCCESS = {"type": "completed", "seq": 3, "message": "Installation completed successfully", "progress": 100.0, "data": {"success": True, "bootable": True}}


@pytest.fixture
def scripted(tmp_path):
    def make(scripts):
        make.daemons.append(ScriptedDaemon(tmp_path / f"s{len(make.daemons)}", scripts))
        return make.daemons[-1]

    make.daemons = []
    return make


@pytest.fixture
def real_page(qtbot, scripted):
    """Lets every script end and waits for every worker: a page destroyed under a running QThread would abort pytest and hide the test that failed."""
    widget = ProgressPage()
    qtbot.addWidget(widget)
    widget.show()
    yield widget
    for daemon in scripted.daemons:
        for hold in daemon.holds:
            hold.set()
    for worker in widget.findChildren(progress_module.BackendWorker):
        worker.wait(15000)


def test_resume_is_offered_after_the_real_stream_ended_and_starts_one_resume(qtbot, scripted, real_page):
    hold, finish = threading.Event(), threading.Event()
    daemon = scripted([[stage_event("storage", "completed"), ERROR, hold, END], [{"type": "stage", "stage": "stage3", "seq": 1, "data": {"status": "running"}}, finish, SUCCESS]])
    spy = Spy(real_page)
    real_page.start(InstallManifest(), daemon.path)
    qtbot.waitUntil(lambda: real_page.stage.text() == "Installation cancelled", timeout=5000)
    assert starts_a_run(real_page) == set()
    hold.set()
    qtbot.waitUntil(lambda: real_page.resume_button.available, timeout=5000)
    qtbot.waitUntil(lambda: spy.stopped == [True], timeout=5000)

    real_page.resume_button.button.click()  # the real widget, as it is on the panel
    qtbot.waitUntil(lambda: len(daemon.requests) == 2, timeout=5000)
    assert daemon.requests[1]["action"] == "start" and daemon.requests[1]["resume"] is True
    finish.set()
    qtbot.waitUntil(lambda: spy.finished == [False, True], timeout=5000)
    assert page_says_finished(real_page)


def page_says_finished(page):
    return page.stage.text().startswith("Installation finished")


def test_signals_a_real_worker_already_queued_are_dropped_after_a_resume(qtbot, scripted, real_page):
    """Everything of the first stream sits in the event queue while the page starts the next run: queued delivery, not a direct call."""
    finish = threading.Event()
    daemon = scripted([[stage_event("storage", "completed"), ERROR, END], [finish, SUCCESS]])
    spy = Spy(real_page)
    real_page.start(InstallManifest(), daemon.path)
    first = real_page.worker
    first.wait(5000)  # the thread ended, and nothing it sent was delivered: this thread never went back to the event loop
    assert real_page.stage.text() == "Starting installation…"
    real_page._manifest, real_page._socket_path = InstallManifest(), daemon.path
    real_page.resume()
    qtbot.wait(200)
    assert real_page.stage.text() == "Resuming installation…"
    assert spy.finished == [] and spy.stopped == [] and real_page.last_seq == 0
    finish.set()
    qtbot.waitUntil(lambda: spy.finished == [True], timeout=5000)
    assert page_says_finished(real_page)


# --- what the end of a job looks like -----------------------------------------------------------------------------


def test_a_cleanup_failure_is_a_note_beside_the_cause(page):
    worker = running(page)
    worker.stage("base")
    worker.send("error", "emerge failed: dev-libs/foo", error="emerge failed: dev-libs/foo", stage="base", data={"retryable": True, "resumable": True})
    worker.send("error", "Cleanup failed: target is busy", error="Cleanup failed: target is busy", stage="cleanup", data={"code": "install.cleanup_failed", "retryable": True})
    ended(worker, message="Installation failed")
    assert "emerge failed" in page.detail.text() and "Installing the base system" in page.stage.text()
    assert "Note: Cleanup failed: target is busy" in page.outcome_details.text() and page.outcome_details.isVisible()
    assert page.resume_button.available


def test_a_cleanup_failure_after_a_cancel_is_shown_too(page):
    worker = running(page)
    cancelled(worker)
    worker.send("error", "Cleanup failed: target is busy", error="Cleanup failed: target is busy", stage="cleanup", data={"code": "install.cleanup_failed", "retryable": True})
    ended(worker)
    assert page.stage.text() == "Installation cancelled"
    assert "Cleanup failed: target is busy" in page.outcome_details.text()


def test_cancelling_after_the_bootloader_says_the_system_can_start(page):
    worker = running(page, "bootloader")
    worker.stage("desktop")
    question = page.cancel_dialog().text()
    assert "can already start" in question and "cannot boot" not in question
    assert "can already start" in page.cancel_note.text() and "cannot start" not in page.cancel.toolTip()
    cancelled(worker, stage="desktop", bootable=True)
    ended(worker, bootable=True)
    assert page.stage.text() == "Your new system is installed and can start"
    assert page.reboot.isVisible()
    assert page.resume_button.text() == "Retry the desktop step" and page.resume_button.available
    assert "incomplete" not in page.result_hint.text() and page.settings_button.available is False


def test_the_hint_names_a_retry_only_when_there_is_one(page):
    page.attach("/run/none.sock")  # a page that attached has no manifest to resume with
    worker = Worker.instances[-1]
    worker.send("error", "the desktop failed", error="the desktop failed", stage="desktop", data={"bootable": True, "retryable": True, "resumable": True})
    ended(worker, message="Installation failed", bootable=True)
    assert page.reboot.isVisible() and not page.resume_button.available
    assert "retry" not in page.result_hint.text()


def test_the_cancel_note_starts_over_with_every_run(page):
    worker = running(page, "bootloader")
    assert page.cancel_note.text() == CANCEL_WARNING_BOOTABLE and page.cancel.toolTip() == CANCEL_WARNING_BOOTABLE
    worker.send("error", "a final check failed", error="a final check failed", stage="validation", data={"bootable": False, "retryable": True})
    ended(worker, message="Installation failed", bootable=False)
    page.start(InstallManifest(), "/run/none.sock")
    assert page.cancel_note.text() == CANCEL_WARNING and page.cancel.toolTip() == CANCEL_WARNING


def test_a_resumed_run_promises_nothing_until_it_reaches_the_bootloader_again(page):
    worker = running(page, "bootloader")
    worker.send("error", "the desktop failed", error="the desktop failed", stage="desktop", data={"bootable": True, "retryable": True, "resumable": True})
    ended(worker, message="Installation failed", bootable=True)
    page.resume()
    resumed = Worker.instances[-1]
    resumed.stage("kernel", "skipped")
    assert "cannot boot" in page.cancel_dialog().text() and page.cancel_note.text() == CANCEL_WARNING
    resumed.stage("bootloader", "skipped")
    assert "can already start" in page.cancel_dialog().text() and page.cancel_note.text() == CANCEL_WARNING_BOOTABLE


def test_cancelling_before_the_bootloader_still_warns(page):
    worker = running(page)
    worker.stage("kernel")
    assert "cannot boot" in page.cancel_dialog().text()
    cancelled(worker, stage="kernel")
    ended(worker)
    assert page.stage.text() == "Installation cancelled" and not page.reboot.isVisible()


@pytest.mark.parametrize("success", [True, False])
def test_a_cancel_confirmed_after_the_run_ended_leaves_the_result_alone(page, success):
    requests = []
    page.cancelRequested.connect(lambda: requests.append(True))
    worker = running(page)
    if success:
        worker.send("completed", "Installation completed successfully", progress=100.0, data={"success": True, "bootable": True})
    else:
        worker.send("error", "boom", error="boom", stage="base", data={"retryable": True})
    detail = page.detail.text()
    page.request_cancel()
    assert page.detail.text() == detail and worker.cancels == 0 and requests == []


def test_the_erase_command_is_red_in_the_menu(page):
    worker = running(page)
    cancelled(worker)
    ended(worker)
    assert page.retry_button.presentation == "menu" and page.retry_button.text() == ERASE_AGAIN
    assert isinstance(page.retry_button.entry_button, QPushButton)
    assert page.retry_button.entry_button.text() == ERASE_AGAIN and page.retry_button.entry_button.objectName() == "dangerButton"
    pressed = []
    page.retryRequested.connect(lambda: pressed.append(True))
    page.retry_button.entry_button.click()
    assert pressed == [True]
