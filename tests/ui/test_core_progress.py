"""CORE-07 (progress page), CORE-08 / G04 (finish card) and CORE-11 (warnings on the finish page), offscreen."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from gentoo_installer import first_steps
from gentoo_installer.manifest import InstallManifest
from gentoo_installer.pages import progress as progress_module
from gentoo_installer.pages.progress import CHECKLIST_NAMES, CHECKLIST_STAGES, ProgressPage
from tests.ui.test_gui_recovery import FakeWorker, error, stage, started, window_with_fake_workers

SIZES = [(1366, 768), (1024, 600)]


@pytest.fixture
def page(qtbot, monkeypatch):
    FakeWorker.instances = []
    monkeypatch.setattr(progress_module, "BackendWorker", FakeWorker)
    widget = ProgressPage()
    qtbot.addWidget(widget)
    widget.show()
    return widget


def success(page, **data):
    page.handle_event({"type": "completed", "message": "Installation completed successfully", "data": {"success": True, **data}})


def primaries(page) -> list[QPushButton]:
    return [b for b in page.result_panel.findChildren(QPushButton) if b.objectName() == "primaryButton" and b.isVisibleTo(page.result_panel)]


def inside_viewport(window, widget: QWidget) -> bool:
    QApplication.processEvents()
    area = window.progress.scroll_area
    top_left = widget.mapTo(area.viewport(), QPoint(0, 0))
    return top_left.y() >= 0 and top_left.y() + widget.height() <= area.viewport().height() and widget.isVisible()


# --- the checklist ---------------------------------------------------------------------------------------------


def test_the_checklist_lists_the_twelve_stages_with_handbook_names(page):
    assert len(CHECKLIST_STAGES) == 12 and "cleanup" not in CHECKLIST_STAGES
    shown = [label.text() for label in page.checklist.names.values()]
    assert shown == [CHECKLIST_NAMES[name] for name in CHECKLIST_STAGES]
    for needle in ("Unpacking the stage file", "Configuring Portage", "Setting up binary package signatures", "Installing the kernel"):
        assert any(needle in text for text in shown), needle
    assert "kernel and the desktop" in page.checklist.hint.text()


def test_the_checklist_follows_the_stage_events(page):
    started(page)
    stage(page, "preflight", "completed")
    stage(page, "storage", "running")
    states = page.checklist.states
    assert states["preflight"] == "done" and states["storage"] == "running" and states["stage3"] == "pending"
    stage(page, "kernel", "running")  # a resumed run skips ahead: everything before is done
    assert states["storage"] == "done" and states["portage"] == "done" and states["kernel"] == "running"
    error(page, "boom", data={"retryable": True})
    assert states["kernel"] == "failed"


def test_success_checks_every_stage_and_names_the_genuine_check(page):
    assert "checking that the download is genuine" in progress_module.STAGE_LABELS["stage3"]
    started(page)
    success(page)
    assert set(page.checklist.states.values()) == {"done"}


# --- the details toggle, severity, full stop ----------------------------------------------------------------


def test_the_log_is_behind_a_details_toggle_and_stays_complete(page):
    started(page)
    page.handle_event({"type": "log", "message": "hello from emerge"})
    page.flush_log()
    assert not page.log.isVisibleTo(page) and "hello from emerge" in page.visible_text()
    page.details_button.click()
    assert page.log.isVisibleTo(page)
    page.details_button.click()
    assert not page.log.isVisibleTo(page)


def test_the_headline_has_a_severity_and_the_detail_ends_with_a_full_stop(page):
    started(page)
    success(page)
    assert page.stage.property("severity") == "ok"
    assert "Installation completed successfully. Remove the installation medium" in page.detail.text()
    started(page)
    error(page, "boom", data={"retryable": True})
    assert page.stage.property("severity") == "error"
    started(page)
    stage(page, "storage", "completed")
    page.handle_event({"type": "error", "stage": "desktop", "message": "emerge failed", "error": "x", "data": {"retryable": True, "resumable": True, "bootable": True}})
    page.handle_event({"type": "completed", "message": "Installation failed", "data": {"success": False, "bootable": True}})
    assert page.stage.property("severity") == "warning"


def test_cancel_is_a_quiet_link_with_the_partial_system_warning(page):
    assert page.cancel.objectName() == "linkButton"
    assert "partly installed" in page.cancel.toolTip() and "partly installed" in page.cancel_note.text()


# --- one primary button ------------------------------------------------------------------------------------------


def test_a_failure_panel_has_exactly_one_primary_button_and_the_rest_in_more(page):
    started(page)
    stage(page, "storage", "completed")
    stage(page, "kernel")
    error(page, "boom", data={"retryable": True})
    assert [b.text() for b in primaries(page)] == ["Resume installation"]
    assert page.resume_button.isVisible()
    menu_entries = [c.text() for c in page.commands if c.presentation == "menu"]
    assert "Erase again and restart installation" in menu_entries and "Back to settings" in menu_entries and "Quit" in menu_entries
    assert "Open the repair tool" in menu_entries  # the disk was changed and the system is not bootable
    assert page.more_button.isVisibleTo(page.result_panel)
    assert page.retry_button.danger and page.retry_button.text() == "Erase again and restart installation"


def test_without_a_changed_disk_try_again_is_the_primary_button(page):
    started(page)
    stage(page, "preflight")
    error(page, "No network", data={"retryable": True})
    assert [b.text() for b in primaries(page)] == ["Try again"]
    assert not page.repair_button.isVisible()


def test_the_repair_tool_request_is_a_signal(page):
    seen = []
    page.repairRequested.connect(lambda: seen.append(True))
    page.repair_button.click()
    assert seen == [True]


@pytest.mark.parametrize("size", SIZES)
def test_restart_now_is_inside_the_viewport_when_the_install_succeeded_or_can_boot(make_window, monkeypatch, size):
    window = window_with_fake_workers(make_window, monkeypatch)
    window.resize(*size)
    window.install_started = True
    window._show_page(7)
    progress = window.progress
    progress.start(InstallManifest(), "/run/none.sock")
    for name in progress_module.STAGE_ORDER:
        stage(progress, name, "completed")
    success(progress)
    assert progress.reboot.isVisible() and [b.text() for b in primaries(progress)] == ["Restart now"]
    assert inside_viewport(window, progress.reboot)
    # a bootable failure
    progress.start(InstallManifest(), "/run/none.sock")
    stage(progress, "storage", "completed")
    progress.handle_event({"type": "error", "stage": "desktop", "message": "emerge failed", "error": "x", "data": {"retryable": True, "resumable": True, "bootable": True}})
    progress.handle_event({"type": "completed", "message": "Installation failed", "data": {"success": False, "bootable": True}})
    assert progress.reboot.isVisible() and progress.resume_button.isVisible()
    assert [b.text() for b in primaries(progress)] == ["Restart now"]
    assert inside_viewport(window, progress.reboot)
    assert inside_viewport(window, progress.resume_button.button)


# --- CORE-08 / G04: the finish card -------------------------------------------------------------------------------


def test_the_finish_card_has_every_command_the_login_and_the_checks(page):
    started(page)
    login = {"username": "ana", "hostname": "box", "privilege": "sudo", "ssh": True, "minimal": True, "bootloader": "grub"}
    success(page, boot_check="Boot check passed: GRUB found the kernel and initramfs", news_unread=3, first_steps=["Press F8 for Safe graphics"], login=login)
    assert page.first_steps_card.isVisibleTo(page)
    text = page.first_steps_text.text() + page.first_steps_checks.text() + page.first_steps_hardware.text()
    for needle in ("Log in as ana", "emerge --sync", "--update --deep --newuse @world", "Working with Gentoo", "/etc/portage/make.conf", "package.use",
                   "eclean-dist -d", "@module-rebuild", "eclean-kernel", "grub-mkconfig", "Libera.Chat", "wiki.gentoo.org", "ssh ana@box", "nmtui",
                   "Boot check passed: GRUB found the kernel and initramfs", "3 unread Gentoo news", "Press F8 for Safe graphics"):
        assert needle in text, needle
    assert page.news_button.isVisibleTo(page)
    assert "/home/ana/FIRST-STEPS.txt" in page.first_steps_note.text()


def test_missing_news_and_boot_check_hide_only_their_lines(page):
    started(page)
    success(page, login={"username": "ana", "hostname": "box", "privilege": "doas", "ssh": False, "minimal": False, "bootloader": "grub"})
    assert page.first_steps_card.isVisibleTo(page)
    assert not page.first_steps_checks.isVisibleTo(page) and not page.news_button.isVisibleTo(page)
    assert not page.first_steps_hardware.isVisibleTo(page)
    assert "doas emerge --sync" in page.first_steps_text.text() and "ssh ana@box" not in page.first_steps_text.text()


def test_the_finish_card_matches_the_text_in_the_file(page):
    manifest = InstallManifest()
    started(page, manifest)
    success(page, boot_check="Boot check passed: GRUB found the kernel and initramfs")
    steps = page.first_steps_model()
    wanted = first_steps.first_steps_text(steps)
    for line in first_steps.steps_lines(steps):
        assert line.split(": ")[0][:20] in wanted


def test_no_first_steps_card_after_a_failure(page):
    started(page)
    error(page, "boom", data={"retryable": True})
    assert not page.first_steps_card.isVisibleTo(page)


# --- CORE-11 on the finish page ----------------------------------------------------------------------------------


def test_validation_warnings_reach_the_finish_page(page):
    started(page)
    warning = "The new system has no machine ID (/etc/machine-id is empty). Desktop programs may misbehave until you run 'dbus-uuidgen --ensure=/etc/machine-id' as root."
    success(page, warnings=[warning])
    assert warning in page.outcome_details.text() and page.outcome_details.isVisibleTo(page)
