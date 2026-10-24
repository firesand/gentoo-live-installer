"""Progress page and window: recovery after a failure, cancel, attach, log volume, progress model."""

from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from gentoo_installer.app import InstallerWindow
from gentoo_installer.daemon_client import DaemonState
from gentoo_installer.manifest import InstallManifest
from gentoo_installer.pages import progress as progress_module
from gentoo_installer.pages.progress import (
    LOG_MAX_BLOCKS,
    STAGE_LABELS,
    ProgressModel,
    ProgressPage,
    format_elapsed,
    summarize,
)
from tests.ui.conftest import sample_info


class FakeWorker(QObject):
    eventReceived = Signal(dict)
    connectionFailed = Signal(str)
    cancelAcknowledged = Signal(bool, str)
    finished = Signal()
    instances: list["FakeWorker"] = []

    def __init__(self, manifest, socket_path="", parent=None, **kwargs):
        super().__init__(parent)
        self.manifest, self.socket_path, self.kwargs = manifest, socket_path, kwargs
        self.cancelled = 0
        self.running = False
        FakeWorker.instances.append(self)

    def start(self):
        self.running = True

    def isRunning(self):
        return self.running

    def cancel(self):
        self.cancelled += 1

    def stop(self):
        self.running = False
        self.finished.emit()


@pytest.fixture
def page(qtbot, monkeypatch):
    FakeWorker.instances = []
    monkeypatch.setattr(progress_module, "BackendWorker", FakeWorker)
    widget = ProgressPage()
    qtbot.addWidget(widget)
    widget.show()
    return widget


def started(page, manifest=None, **kwargs):
    page.start(manifest or InstallManifest(), "/run/none.sock", **kwargs)
    return FakeWorker.instances[-1]


def stage(page, name, status="running", progress=None, **data):
    event = {"type": "stage", "stage": name, "message": f"{status} {name}", "data": {"status": status, **data}}
    if progress is not None:
        event["progress"] = progress
    page.handle_event(event)


def error(page, message, **extra):
    data = extra.pop("data", {})
    page.handle_event({"type": "error", "message": message, "error": extra.pop("error", message), "data": data, **extra})
    page.handle_event({"type": "completed", "message": "Installation failed", "data": {"success": False}})


# --- D15: labels, elapsed time, monotonic bar ---------------------------------------------------


def test_every_install_stage_has_a_progress_label():
    from gentoo_installerd.install import STAGES

    assert set(STAGES) <= set(STAGE_LABELS)


def test_elapsed_time_is_hh_mm_ss_from_zero(page):
    assert format_elapsed(0) == "00:00:00"
    assert format_elapsed(3725) == "01:02:05"
    assert page.elapsed.text() == "00:00:00"
    started(page)
    page._tick()
    assert page.elapsed.text() == "00:00:01"


def test_bar_never_moves_back_and_stage3_download_does_not_hijack_it(page):
    started(page)
    values = []
    # an older daemon: stage events carry the overall percentage, download events a stage-local one
    for kind, name, status, progress in (
        ("stage", "preflight", "running", 0.0), ("stage", "preflight", "completed", 8.3),
        ("stage", "storage", "running", 8.3), ("stage", "storage", "completed", 16.6),
        ("stage", "stage3", "running", 16.6), ("progress", "stage3", None, 5.0), ("progress", "stage3", None, 50.0),
        ("progress", "stage3", None, 100.0), ("stage", "stage3", "completed", 25.0),
        ("stage", "mounts", "running", 25.0), ("stage", "base", "running", 66.6), ("stage", "validation", "completed", 100 * 12 / 12.0),
        ("stage", "cleanup", "running", 99.0),
    ):
        data = {"status": status} if status else {}
        page.handle_event({"type": kind, "stage": name, "progress": progress, "message": name, "data": data})
        values.append(page.progress.value())
    assert values == sorted(values)
    assert values[5] < 30 and values[7] < 30  # the download reaching 100% is not 100% overall
    assert values[-1] == 100  # cleanup repeats the last overall value; only "completed" ends it


def test_daemon_overall_progress_is_trusted_when_stage_progress_is_present(page):
    started(page)
    page.handle_event({"type": "progress", "stage": "stage3", "progress": 17.0, "data": {"stage_progress": 40.0, "downloaded": 4, "total": 10}})
    assert page.progress.value() == 17
    assert "40% of this step" in page.secondary.text()


def test_emerging_lines_update_the_package_counter(page):
    started(page)
    stage(page, "base")
    before = page.progress.value()
    page.handle_event({"type": "log", "message": ">>> Emerging (3 of 10) dev-libs/foo-1.2::gentoo"})
    assert "Package 3 of 10: dev-libs/foo-1.2::gentoo" == page.secondary.text()
    assert page.progress.value() > before
    page.flush_log()
    assert ">>> Emerging (3 of 10)" in page.log.toPlainText()
    stage(page, "bootloader")
    assert "Step 10 of 12" in page.secondary.text()


def test_success_ends_at_100_and_asks_to_remove_the_medium(page):
    started(page)
    stage(page, "storage", "completed", progress=20)
    page.handle_event({"type": "completed", "message": "Installation completed successfully", "data": {"success": True}})
    assert page.progress.value() == 100
    assert "Remove the installation medium" in page.detail.text()
    assert page.reboot.isVisible()
    assert page.result_panel.isVisible() and page.save_log_button.isVisible()
    assert not page.retry_button.isVisible()


# --- D2: log volume ------------------------------------------------------------------------------


def test_log_is_bounded_and_says_so(page):
    started(page)
    for index in range(20_000):
        page.handle_event({"type": "log", "message": f"line {index}"})
    page.flush_log()
    assert page.log.blockCount() <= LOG_MAX_BLOCKS
    assert page.truncated_label.isVisible()
    assert "older lines are kept in the installer log" in page.log.toPlainText().splitlines()[-1]
    assert "line 19999" in page.log.toPlainText()


def test_merge_listings_are_counted_not_shown(page):
    started(page)
    stage(page, "base")
    for path in ("/usr/bin/x", "/usr/bin/y", "/usr/lib/z"):
        page.handle_event({"type": "log", "message": f">>> {path}"})
    page.handle_event({"type": "log", "message": "--- /usr/bin"})
    page.handle_event({"type": "log", "message": ">>> Installing (1 of 3) sys-apps/foo-1::gentoo"})
    page.handle_event({"type": "log", "message": " * a warning"})
    page.flush_log()
    text = page.log.toPlainText()
    assert ">>> /usr/bin/x" not in text and "--- /usr/bin" not in text
    assert ">>> Installing (1 of 3)" in text and "* a warning" in text
    assert "merged files: 3" in page.detail.text()


def test_log_appends_are_batched(page):
    started(page)
    appended = []
    original = page.log.appendPlainText
    page.log.appendPlainText = lambda text: (appended.append(text), original(text))[1]
    for index in range(500):
        page.handle_event({"type": "log", "message": f"line {index}"})
    assert len(appended) < 10  # the first line at once, the burst in one append
    page.flush_log()
    assert page.log.blockCount() == 500


# --- D9: failure text -----------------------------------------------------------------------------


def test_long_error_text_wraps_and_is_summarised(page):
    started(page)
    message = "Command failed: emerge --ask=n --verbose=y " + "sys-apps/very-long-package-name-1.2.3 " * 12 + "/var/tmp/" + "x" * 300 + "\nline two\nline three"
    error(page, message)
    assert page.detail.wordWrap()
    assert page.detail.minimumSizeHint().width() < page.width()  # can shrink with the window
    assert page.detail.width() <= page.width()
    assert page.detail.text().endswith("(full output in the log below)")
    assert "line three" not in page.detail.text()
    assert "line three" in page.visible_text()  # the log keeps the full text
    assert summarize("short") == "short"
    assert "\u200b" in summarize("/var/tmp/" + "x" * 300)  # an unbreakable token can still wrap


def test_failure_keeps_the_last_bar_value_and_names_the_stage(page):
    started(page)
    stage(page, "kernel", "running", progress=58)
    error(page, "dracut failed", stage="kernel")
    assert page.progress.value() == 58
    assert page.stage.text() == "Installation needs attention · Installing firmware and the kernel"


# --- D1: the result panel --------------------------------------------------------------------------


def test_failure_before_the_disk_is_touched_offers_try_again_only(page):
    started(page)
    stage(page, "preflight")
    error(page, "No network", data={"retryable": True})
    assert page.result_panel.isVisible()
    assert page.retry_button.text() == "Try again" and page.retry_button.isVisible()
    assert not page.resume_button.isVisible()
    assert page.settings_button.isVisible() and page.save_log_button.isVisible() and page.quit_button.isVisible()
    assert "Nothing on your disks was changed" in page.result_hint.text()


def test_failure_after_storage_offers_resume_and_start_over(page):
    started(page)
    stage(page, "storage", "completed")
    stage(page, "kernel")
    error(page, "boom", data={"retryable": True})
    assert page.resume_button.isVisible()
    assert page.retry_button.text() == "Start over"
    assert "without formatting again" in page.result_hint.text()


def test_resume_is_hidden_when_the_daemon_says_the_failure_is_not_retryable(page):
    started(page)
    stage(page, "storage", "completed")
    error(page, "storage identity changed", data={"retryable": False})
    assert not page.resume_button.isVisible()


def test_user_cancel_is_neutral_and_resumable_after_storage(page):
    started(page)
    stage(page, "storage", "completed")
    page.handle_event({"type": "error", "message": "Installation cancelled", "error": "cancelled", "data": {"retryable": False}})
    page.handle_event({"type": "completed", "message": "Installation cancelled", "data": {"success": False}})
    assert page.stage.text() == "Installation cancelled"
    assert "partially installed" in page.detail.text() and "Resume" in page.detail.text()
    assert page.resume_button.isVisible()


def test_cancel_before_storage_changes_nothing(page):
    started(page)
    page.handle_event({"type": "error", "message": "Installation cancelled", "error": "cancelled", "data": {"code": "install.cancelled"}})
    assert page.stage.text() == "Installation cancelled"
    assert "No disk has been changed" in page.detail.text()
    assert not page.resume_button.isVisible()


def test_resume_starts_a_resume_run_with_the_same_manifest_and_resets_the_latch(page):
    manifest = InstallManifest()
    first = started(page, manifest)
    stage(page, "storage", "completed")
    error(page, "boom", data={"retryable": True})
    finished = []
    page.finished.connect(finished.append)
    page.resume_button.click()
    second = FakeWorker.instances[-1]
    assert second is not first
    assert second.manifest is manifest and second.kwargs == {"resume": True}
    assert not page.result_panel.isVisible() and page.cancel.isVisible()
    assert "--- resuming ---" in page.visible_text()
    assert "Resuming" in page.stage.text()
    stage(page, "storage", "skipped")
    page.handle_event({"type": "completed", "message": "done", "data": {"success": True}})
    assert finished == [True]  # the failure latch was reset: a new outcome is reported


def test_panel_buttons_emit_their_signals(page, qtbot):
    started(page)
    error(page, "boom")
    with qtbot.waitSignal(page.retryRequested, timeout=500):
        page.retry_button.click()
    with qtbot.waitSignal(page.settingsRequested, timeout=500):
        page.settings_button.click()
    with qtbot.waitSignal(page.quitRequested, timeout=500):
        page.quit_button.click()
    with qtbot.waitSignal(page.diagnosticsRequested, timeout=500):
        page.diagnostics_button.click()


def test_start_signals_a_new_run_each_time(page, qtbot):
    with qtbot.waitSignal(page.started, timeout=500):
        started(page)


# --- save log -----------------------------------------------------------------------------------------


def test_save_log_writes_the_daemon_log(page, qtbot, fake_daemon, tmp_path, monkeypatch):
    daemon = fake_daemon(lambda request: [
        {"type": "result", "data": {"lines": ["alpha", "beta"]}}, {"type": "result", "data": {"lines": ["gamma"]}},
        {"type": "completed", "data": {"success": True}}])
    page.start(InstallManifest(), daemon.path)
    target = tmp_path / "saved.log"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(target), ""))
    with qtbot.waitSignal(page.logSaved, timeout=5000) as blocker:
        page.save_log()
    assert blocker.args[0] is True
    assert target.read_text().split() == ["alpha", "beta", "gamma"]
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert "3 lines" in page.notice.text()


def test_save_log_falls_back_to_the_visible_text(page, qtbot, tmp_path, monkeypatch):
    page.start(InstallManifest(), str(tmp_path / "missing.sock"))
    page.handle_event({"type": "log", "message": "only the visible line"})
    target = tmp_path / "fallback.log"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(target), ""))
    with qtbot.waitSignal(page.logSaved, timeout=5000) as blocker:
        page.save_log()
    assert blocker.args[0] is True
    assert "only the visible line" in target.read_text()
    assert "lines shown in this window" in page.notice.text()


def test_save_log_to_an_unwritable_place_reports_it(page, qtbot, tmp_path, monkeypatch):
    page.start(InstallManifest(), str(tmp_path / "missing.sock"))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(tmp_path / "no-such-dir" / "x.log"), ""))
    with qtbot.waitSignal(page.logSaved, timeout=5000) as blocker:
        page.save_log()
    assert blocker.args[0] is False
    assert "Could not write" in blocker.args[1]


# --- D10: cancel -----------------------------------------------------------------------------------------


def click_dialog_button(text):
    def click():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QMessageBox)
        button = next(b for b in dialog.buttons() if b.text() == text)
        button.click()

    QTimer.singleShot(0, click)


def test_cancel_dialog_has_explicit_buttons_and_state_dependent_text(page):
    started(page)
    box = page.cancel_dialog()
    assert [b.text() for b in box.buttons()] == ["Keep installing", "Cancel installation"]
    assert box.defaultButton().text() == "Keep installing"
    assert "No disk has been changed yet" in box.text()
    stage(page, "storage", "completed")
    assert "already been modified" in page.cancel_dialog().text()


def test_keep_installing_sends_nothing(page):
    worker = started(page)
    click_dialog_button("Keep installing")
    page.cancel.click()
    assert worker.cancelled == 0 and page.cancel.isEnabled()


def test_cancel_installation_sends_exactly_one_cancel(page):
    worker = started(page)
    emitted = []
    page.cancelRequested.connect(lambda: emitted.append(True))
    click_dialog_button("Cancel installation")
    page.cancel.click()
    assert worker.cancelled == 1 and emitted == [True]
    assert not page.cancel.isEnabled()
    assert "waiting for a safe stopping point" in page.detail.text()


def test_refused_cancel_is_shown_and_the_button_comes_back(page):
    worker = started(page)
    page.request_cancel()
    worker.cancelAcknowledged.emit(False, "No installation is running")
    assert page.cancel.isEnabled()
    assert "not accepted" in page.detail.text() and "No installation is running" in page.detail.text()


def test_accepted_cancel_keeps_waiting(page):
    worker = started(page)
    page.request_cancel()
    worker.cancelAcknowledged.emit(True, "Cancellation requested")
    assert not page.cancel.isEnabled()


# --- lost stream and attach ---------------------------------------------------------------------------------


def running_daemon(fake_daemon, **status):
    def handler(request):
        if request["action"] == "capabilities":
            return [{"type": "result", "data": {"protocol": 1, "features": ["install.v1", "attach.v1"], "actions": ["attach", "log"]}}]
        return [{"type": "status", "data": {"status": "running", "stage": "base", "seq": 40, **status}}]

    return fake_daemon(handler)


def test_lost_stream_of_a_running_job_offers_reconnect(page, fake_daemon):
    daemon = running_daemon(fake_daemon)
    worker = page.start(InstallManifest(), daemon.path) or FakeWorker.instances[-1]
    page.handle_event({"type": "stage", "stage": "base", "seq": 33, "data": {"status": "running"}})
    worker.finished.emit()  # the stream ended without a "completed" event
    assert page.stage.text() == "Lost the connection to the installer"
    assert page.reconnect_button.isVisible() and not page.retry_button.isVisible()
    assert "still running" in page.detail.text()
    page.reconnect_button.click()
    attach = FakeWorker.instances[-1]
    assert attach.kwargs == {"attach": True, "after_seq": 33}
    assert attach.manifest is None


def test_lost_stream_of_a_job_that_ended_reports_the_daemons_specific_error(page, fake_daemon):
    def handler(request):
        return [{"type": "status", "data": {"status": "failed", "error": "dracut: no space left on /boot"}}]

    daemon = fake_daemon(handler)
    worker = started(page)
    page._socket_path = daemon.path
    worker.finished.emit()
    assert page.stage.text().startswith("Installation needs attention")
    assert "no space left on /boot" in page.detail.text()


def test_lost_stream_with_a_dead_service_explains_how_to_restart_it(page, tmp_path):
    worker = started(page)
    page._socket_path = str(tmp_path / "gone.sock")
    worker.connectionFailed.emit("The installer service is not running (rc-service gentoo-installerd restart)")
    assert "rc-service gentoo-installerd" in page.detail.text() or "rc-service" in page.visible_text()


def test_busy_start_offers_to_follow_the_running_installation(page):
    started(page)
    page.handle_event({"type": "error", "message": "An installation is already running", "error": "An installation is already running", "data": {"code": "install.busy"}})
    page.handle_event({"type": "completed", "data": {"success": False}})
    assert page.stage.text() == "An installation is already running"
    assert page.reconnect_button.isVisible() and not page.retry_button.isVisible()
    page.reconnect_button.click()
    assert FakeWorker.instances[-1].kwargs == {"attach": True, "after_seq": 0}


def test_attach_replays_events_and_ends_in_the_recorded_outcome(page):
    page.attach("/run/none.sock")
    worker = FakeWorker.instances[-1]
    assert worker.kwargs == {"attach": True, "after_seq": 0}
    for seq, event in enumerate((
        {"type": "status", "data": {"status": "failed"}},
        {"type": "stage", "stage": "storage", "data": {"status": "completed"}},
        {"type": "log", "message": "replayed line"},
        {"type": "error", "message": "boom", "error": "boom", "stage": "kernel", "data": {"retryable": True}},
        {"type": "completed", "data": {"success": False}},
    ), start=1):
        page.handle_event({**event, "seq": seq})
    assert page.last_seq == 5
    assert page.resume_button.isVisible() or page._manifest is None  # no manifest after a restart: Start over
    assert page.stage.text().startswith("Installation needs attention")


# --- the window around the page -------------------------------------------------------------------------------


def window_with_fake_workers(make_window, monkeypatch, **kwargs):
    FakeWorker.instances = []
    monkeypatch.setattr(progress_module, "BackendWorker", FakeWorker)
    return make_window(**kwargs)


def test_sidebar_and_buttons_come_back_after_a_failed_run(make_window, monkeypatch):
    window = window_with_fake_workers(make_window, monkeypatch)
    window.manifest.storage.device = "/dev/sda"
    window.install_started = True
    window._show_page(7)
    window.progress.started.emit()
    assert not window.step_buttons[2].isEnabled()
    window.progress.handle_event({"type": "error", "message": "boom", "error": "boom"})
    window.progress.handle_event({"type": "completed", "data": {"success": False}})
    window._worker_stopped()
    assert window.step_buttons[2].isEnabled() and window.step_buttons[6].isEnabled()
    window.progress.settings_button.click()
    assert window.current == 6 and window.back.isVisible() and window.next.isVisible()
    assert not window.install_started


def test_success_keeps_the_sidebar_locked(make_window, monkeypatch):
    window = window_with_fake_workers(make_window, monkeypatch)
    window._show_page(7)
    window.progress.started.emit()
    window.progress.handle_event({"type": "completed", "data": {"success": True}})
    window._worker_stopped()
    assert not window.step_buttons[2].isEnabled()


def test_try_again_goes_through_the_usual_confirmation(make_window, monkeypatch):
    window = window_with_fake_workers(make_window, monkeypatch)
    window.manifest.storage.device = "/dev/sda"
    window.manifest.storage.device_id = "test-disk"
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text, *a, **k: (shown.append(title), QMessageBox.StandardButton.Cancel)[1])
    window.progress.start(window.manifest, window.socket_path)
    window.progress.handle_event({"type": "error", "message": "boom", "error": "boom"})
    window.progress.handle_event({"type": "completed", "data": {"success": False}})
    window._worker_stopped()
    window.progress.retry_button.click()
    assert shown == ["Erase the selected disk?"]


def test_erase_confirmation_names_the_disk_and_what_is_on_it(make_window, monkeypatch):
    info = sample_info()
    info.disks[0].partitions = []
    window = window_with_fake_workers(make_window, monkeypatch, info=info)
    window.manifest.storage.device = "/dev/sda"
    seen = {}

    def warning(parent, title, text, buttons, default):
        seen.update(title=title, text=text, buttons=buttons, default=default)
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "warning", warning)
    window.start_installation()
    assert seen["title"] == "Erase the selected disk?"
    assert "/dev/sda (Virtual disk, 64.0 GiB, serial …t-disk)" in seen["text"]
    assert seen["buttons"] & QMessageBox.StandardButton.Yes
    assert not FakeWorker.instances


def test_confirmed_start_runs_the_job_and_locks_the_wizard(make_window, monkeypatch):
    window = window_with_fake_workers(make_window, monkeypatch)
    window.manifest.storage.device = "/dev/sda"
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    window.start_installation()
    assert window.install_started and window.current == 7
    assert FakeWorker.instances[-1].kwargs == {}
    assert not window.back.isVisible() and not window.step_buttons[2].isEnabled()


def test_a_dead_service_blocks_the_start_with_instructions(make_window, monkeypatch):
    window = window_with_fake_workers(make_window, monkeypatch)
    monkeypatch.delenv("GENTOO_INSTALLER_DEV")
    window.manifest.storage.device = "/dev/sda"
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text, *a, **k: shown.append((title, text)))
    window.start_installation()
    assert not FakeWorker.instances and not window.install_started
    assert shown[0][0] == "Installer service unavailable" and "rc-service gentoo-installerd" in shown[0][1]


def test_a_running_job_is_offered_instead_of_a_second_start(make_window, monkeypatch, fake_daemon):
    daemon = fake_daemon(lambda request: [{"type": "status", "data": {"status": "running"}}])
    window = window_with_fake_workers(make_window, monkeypatch)
    monkeypatch.delenv("GENTOO_INSTALLER_DEV")
    window.socket_path = daemon.path
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    window.start_installation()
    assert FakeWorker.instances[-1].kwargs == {"attach": True, "after_seq": 0}
    assert window.current == 7


def test_banner_offers_to_attach_to_a_running_installation(make_window, monkeypatch):
    window = window_with_fake_workers(make_window, monkeypatch)
    started_at = time.time() - 600
    state = DaemonState("running", status={"status": "running", "stage": "base", "started_at": started_at}, features=frozenset({"attach.v1"}))
    window.apply_info(sample_info(daemon=state))
    assert window.banner.isVisible()
    assert "still running" in window.banner.label.text() and time.strftime("%H:%M", time.localtime(started_at)) in window.banner.label.text()
    assert set(window.banner.buttons) == {"attach", "cancel", "ignore"}
    window.banner.buttons["attach"].click()
    assert FakeWorker.instances[-1].kwargs == {"attach": True, "after_seq": 0}
    assert window.current == 7 and not window.banner.isVisible()


def test_banner_ignore_dismisses_it(make_window, monkeypatch):
    window = window_with_fake_workers(make_window, monkeypatch)
    window.apply_info(sample_info(daemon=DaemonState("running", status={"status": "running"})))
    window.banner.buttons["ignore"].click()
    assert not window.banner.isVisible() and window.current == 0


def test_banner_cancel_asks_first_and_then_cancels(make_window, monkeypatch, fake_daemon):
    daemon = fake_daemon(lambda request: [{"type": "status", "message": "Cancellation requested", "data": {"status": "cancelling"}}])
    window = window_with_fake_workers(make_window, monkeypatch)
    window.socket_path = daemon.path
    window.apply_info(sample_info(daemon=DaemonState("running", status={"status": "running"})))
    click_dialog_button("Keep it running")
    window.banner.buttons["cancel"].click()
    assert daemon.requests == []
    window.apply_info(sample_info(daemon=DaemonState("running", status={"status": "running"})))
    click_dialog_button("Cancel installation")
    window.banner.buttons["cancel"].click()
    assert [r["action"] for r in daemon.requests] == ["cancel"]
    assert "Cancellation requested" in window.banner.label.text()


def test_a_resumable_failed_job_is_explained_and_review_offers_resume(make_window, monkeypatch):
    window = window_with_fake_workers(make_window, monkeypatch)
    state = DaemonState("running", status={"status": "failed", "resumable": True, "stage": "kernel"})
    window.apply_info(sample_info(daemon=state))
    assert window.banner.isVisible() and "stopped" in window.banner.label.text() and "Resume" in window.banner.label.text()
    window.manifest.storage.device = "/dev/sda"
    window._show_page(6)
    assert window.review.resume_check.isVisible()
    window.review.resume_check.setChecked(True)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail("resuming must not ask to erase the disk again"))
    window.start_installation()
    assert FakeWorker.instances[-1].kwargs == {"resume": True}


REAL_CLOSE_DIALOG = InstallerWindow._confirm_close_during_install


def test_close_during_an_installation_uses_explicit_buttons(make_window, monkeypatch):
    window = make_window()
    monkeypatch.setattr(InstallerWindow, "_confirm_close_during_install", REAL_CLOSE_DIALOG)
    window.install_started = True
    seen = {}

    def exec_(self):
        seen["buttons"] = [b.text() for b in self.buttons()]
        seen["default"] = self.defaultButton().text()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    window.close()
    assert seen == {"buttons": ["Keep installer open", "Cancel installation and close"], "default": "Keep installer open"}
    assert window.isVisible()
    window.install_started = False


def test_progress_model_unit():
    model = ProgressModel()
    model.stage_event("preflight", "completed", None, None)
    assert model.overall == 2
    model.progress_event("stage3", 50.0, None)  # old daemon: stage-local
    assert model.overall == 2 + 8 + 12 * 0.5
    model.progress_event("stage3", 10.0, None)
    assert model.overall == 16  # never backwards
    model.progress_event("kernel", 33.0, 40.0)  # new daemon: overall + local
    assert model.overall == 33 and model.local == 40
    model.finish()
    assert model.overall == 100
