"""The install result as the daemon reports it (interface I1): bootable, degraded parts, warnings after a good install."""

from __future__ import annotations

import pytest

from gentoo_installer.manifest import InstallManifest
from gentoo_installer.pages import progress as progress_module
from gentoo_installer.pages.progress import STAGE_LABELS, ProgressPage
from tests.ui.test_gui_recovery import FakeWorker, error, stage, started, window_with_fake_workers


@pytest.fixture
def page(qtbot, monkeypatch):
    FakeWorker.instances = []
    monkeypatch.setattr(progress_module, "BackendWorker", FakeWorker)
    widget = ProgressPage()
    qtbot.addWidget(widget)
    widget.show()
    return widget


def desktop_failure(page, **data):
    """The desktop step fails after the bootloader step: error event, then the final completed event."""
    base = {"code": "install.failed", "retryable": True, "resumable": True, "bootable": True}
    base.update(data)
    page.handle_event({"type": "error", "stage": "desktop", "message": "emerge failed for kde-plasma/plasma-meta", "error": "emerge failed", "data": base})
    page.handle_event({"type": "completed", "message": "Installation failed", "data": {"success": False, "bootable": base["bootable"], "degraded": ["KDE applications"]}})


def test_the_new_stages_have_plain_labels():
    assert STAGE_LABELS["base"] == "Installing the base system" and STAGE_LABELS["desktop"] == "Installing the desktop and applications"


# --- a system that can start although a later step failed -----------------------------------------------------------------------------


def test_a_failed_desktop_step_on_a_bootable_system_says_so_and_offers_to_restart_or_retry(page):
    started(page)
    stage(page, "storage", "completed", progress=20)
    stage(page, "bootloader", "completed", progress=70)
    desktop_failure(page)
    assert page.stage.text() == "Your new system is installed and can start"
    assert "did not finish" in page.detail.text() and "kde-plasma" in page.detail.text()
    assert page.bootable and page.reboot.isVisible() and page.reboot.text() == "Restart now"
    assert page.resume_button.isVisible() and page.resume_button.text() == "Retry the desktop step"
    assert page.save_log_button.isVisible() and page.diagnostics_button.isVisible() and page.quit_button.isVisible()
    assert not page.retry_button.isVisible() and not page.settings_button.isVisible()  # nothing offers to erase a system that works
    assert "restart now" in page.result_hint.text().lower() and "desktop step" in page.result_hint.text()
    assert "KDE applications" in page.outcome_details.text() and page.outcome_details.isVisibleTo(page)


def test_retrying_the_desktop_step_resumes_with_the_same_manifest(page):
    manifest = InstallManifest()
    worker = started(page, manifest)
    stage(page, "storage", "completed", progress=20)
    desktop_failure(page)
    page.resume_button.click()
    assert FakeWorker.instances[-1] is not worker and FakeWorker.instances[-1].kwargs == {"resume": True}
    assert FakeWorker.instances[-1].manifest is manifest
    assert not page.result_panel.isVisible() and not page.reboot.isVisible() and not page.bootable  # a new run starts clean


def test_the_retry_button_is_hidden_when_the_daemon_says_it_cannot_resume(page):
    started(page)
    stage(page, "storage", "completed", progress=20)
    desktop_failure(page, resumable=False)
    assert page.reboot.isVisible() and not page.resume_button.isVisible()


def test_another_failing_step_is_named_the_last_step(page):
    started(page)
    stage(page, "storage", "completed", progress=20)
    page.handle_event({"type": "error", "stage": "validation", "message": "check failed", "data": {"bootable": True, "resumable": True}})
    page.handle_event({"type": "completed", "data": {"success": False}})
    assert page.resume_button.text() == "Retry the last step" and "validation" not in page.stage.text().lower()


def test_bootable_may_arrive_only_with_the_completed_event(page):
    started(page)
    stage(page, "storage", "completed", progress=20)
    page.handle_event({"type": "error", "stage": "desktop", "message": "emerge failed", "data": {"retryable": True, "resumable": True}})
    assert page.stage.text().startswith("Installation needs attention") and not page.reboot.isVisible()
    page.handle_event({"type": "completed", "data": {"success": False, "bootable": True}})
    assert page.stage.text() == "Your new system is installed and can start" and page.reboot.isVisible()


def test_a_failure_before_the_bootloader_never_offers_a_restart(page):
    started(page)
    stage(page, "storage", "completed", progress=20)
    error(page, "The kernel could not be built", stage="kernel", data={"code": "install.failed", "retryable": True, "resumable": True, "bootable": False})
    assert page.stage.text().startswith("Installation needs attention") and not page.reboot.isVisible() and not page.bootable
    assert page.retry_button.isVisible() and page.resume_button.isVisible()


def test_a_bootable_system_keeps_the_wizard_locked(make_window, monkeypatch):
    window = window_with_fake_workers(make_window, monkeypatch)
    window._show_page(7)
    window.progress.started.emit()
    window.progress.start(window.manifest, window.socket_path)
    window.progress.handle_event({"type": "stage", "stage": "storage", "data": {"status": "completed"}})
    desktop_failure(window.progress)
    window._worker_stopped()
    assert not window.step_buttons[2].isEnabled() and not window.step_buttons[6].isEnabled()


# --- success with something to tell -------------------------------------------------------------------------------------------------------


def test_the_finish_page_tells_the_user_to_remove_the_stick_and_restart(page):
    started(page)
    stage(page, "storage", "completed", progress=20)
    page.handle_event({"type": "completed", "message": "Installation completed successfully", "data": {"success": True}})
    assert "remove the USB stick" in page.stage.text() and "restart" in page.stage.text()
    assert "Remove the installation medium" in page.detail.text()
    assert page.reboot.isVisible() and page.reboot.text() == "Restart now" and not page.outcome_details.isVisibleTo(page)


def test_parts_that_failed_are_listed_after_a_successful_install(page):
    started(page)
    stage(page, "storage", "completed", progress=20)
    page.handle_event({"type": "completed", "message": "Installed", "data": {"success": True, "bootable": True, "degraded": ["NVIDIA driver", "HP printer drivers"]}})
    assert page.reboot.isVisible() and not page.resume_button.isVisible()
    text = page.outcome_details.text()
    assert "NVIDIA driver; HP printer drivers" in text and "optional parts" in text
    assert "optional parts are missing" in page.result_hint.text()


def test_a_warning_after_a_good_install_is_shown_and_logged(page):
    started(page)
    stage(page, "storage", "completed", progress=20)
    note = "Unmounting failed. The installation itself is complete; before restarting run: umount -R /mnt/gentoo"
    page.handle_event({"type": "status", "stage": "cleanup", "message": note, "data": {"warning": True}})
    page.handle_event({"type": "completed", "message": "Installed", "data": {"success": True, "warnings": [note]}})
    assert page.outcome_details.text().count("umount -R /mnt/gentoo") == 1  # the same text from both events appears once
    assert "Note: Unmounting failed" in page.outcome_details.text() and page.reboot.isVisible()
    assert "NOTE: Unmounting failed" in page.visible_text()


def test_degraded_parts_may_be_announced_while_the_desktop_step_runs(page):
    started(page)
    page.handle_event({"type": "progress", "stage": "desktop", "message": "Installing the printer drivers", "data": {"degraded": ["Gutenprint"], "bootable": True}})
    assert "Gutenprint" in page.outcome_details.text() and page.bootable


def test_the_outcome_of_one_run_does_not_leak_into_the_next(page):
    started(page)
    stage(page, "storage", "completed", progress=20)
    desktop_failure(page)
    started(page)
    assert not page.bootable and not page.outcome_details.isVisibleTo(page) and page.outcome_details.text() == ""
