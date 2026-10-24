"""CORE-09: a failing step shows its problem inline and marks the sidebar; no dialog opens."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox

from gentoo_installer.daemon_client import DaemonState
from tests.ui.conftest import sample_info


@pytest.fixture
def no_dialogs(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a QMessageBox was opened")

    for name in ("warning", "information", "question", "critical"):
        monkeypatch.setattr(QMessageBox, name, refuse)
    monkeypatch.setattr(QMessageBox, "exec", refuse)


def test_a_failing_page_shows_an_inline_message_and_a_sidebar_marker(make_window, no_dialogs):
    window = make_window()
    window.apply_info(sample_info(daemon=DaemonState("not-running", "The installer service is not running. Start it first.")))
    window._show_page(1)
    window.next.click()
    page = window.readiness
    assert page.problem_label.isVisibleTo(page) and "not running" in page.problem_label.text()
    button = window.step_buttons[1]
    assert button.property("problem") is True and "●" in button.text()
    assert "needs attention" in button.accessibleName()
    assert window.current == 1


def test_the_marker_and_message_clear_when_the_page_passes(make_window, no_dialogs):
    window = make_window()
    page = window.pages[0]
    page.set_problem("KDE needs the Desktop kind of base system. Details: ...")
    assert window.step_buttons[0].property("problem") is True
    window.next.click()  # the welcome page is always valid
    assert not page.has_problem()
    assert window.step_buttons[0].property("problem") is False and "●" not in window.step_buttons[0].text()


def test_welcome_has_one_forward_action_and_a_keyboard_line(make_window):
    window = make_window()
    welcome = window.welcome
    assert welcome.keyboard_line.text().startswith("Keyboard: ")
    assert not welcome.keyboard.isVisibleTo(welcome)
    welcome.keyboard_button.click()
    assert welcome.keyboard.isVisibleTo(welcome)
