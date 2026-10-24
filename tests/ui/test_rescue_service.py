"""A daemon that cannot repair: no repair engine, no service at all, a running installation, and how the window recovers."""

from __future__ import annotations

import pytest

from gentoo_installer.rescue_app import FIND, SERVICE
from gentoo_installer.rescue_ui import messages
from tests.rescue.flow import uefi_gentoo_lab
from tests.ui.rescue_fakes import (  # noqa: F401  (fixtures)
    daemon_factory,
    proxy_factory,
    repair_kit,
    rescue_lab_factory,
)


def test_a_daemon_without_the_repair_engine_explains_how_to_use_the_terminal_tool(qtbot, daemon_factory, repair_kit) -> None:
    daemon = daemon_factory(None, rescue=False)  # what an older installer service looks like: it knows install, attach, log and hardware actions only
    window = repair_kit.open(daemon.path)
    qtbot.waitUntil(lambda: window.stack.currentIndex() == SERVICE, timeout=20000)
    page = window.service_page
    assert page.heading.text() == messages.NO_RESCUE_TITLE and "sudo gentoo-rescue" in page.text.text()
    assert window.header_title.text() == "The repair tool cannot start"
    assert not window.find_page.busy.isVisibleTo(window.find_page)  # the "connecting" bar is gone
    assert page.tool_button.isVisibleTo(page) and page.retry_button.isVisibleTo(page)
    page.tool_button.click()
    program, arguments = repair_kit.launched[-1]
    assert program == "xfce4-terminal" and arguments[:3] == ["--title", "Repair tool", "-x"] and "sudo gentoo-rescue" in arguments[-1]
    assert "A terminal window was opened" in page.notice.text.text()


def test_without_any_terminal_program_the_window_says_how_to_start_the_tool_by_hand(qtbot, daemon_factory, repair_kit) -> None:
    repair_kit.terminal_available = False
    daemon = daemon_factory(None, rescue=False)
    window = repair_kit.open(daemon.path)
    qtbot.waitUntil(lambda: window.stack.currentIndex() == SERVICE, timeout=20000)
    window.service_page.tool_button.click()
    assert not repair_kit.launched and "sudo gentoo-rescue" in window.service_page.notice.text.text()


def test_a_service_that_is_not_running_is_explained_and_the_window_starts_when_it_comes_up(qtbot, rescue_lab_factory, daemon_factory, repair_kit, sock_dir) -> None:
    window = repair_kit.open(str(sock_dir / "late.sock"))
    qtbot.waitUntil(lambda: window.stack.currentIndex() == SERVICE, timeout=20000)
    text = window.service_page.text.text()
    assert "not running" in text and "rc-service gentoo-installerd restart" in text and "sudo gentoo-rescue" in text
    lab, _tree = uefi_gentoo_lab(rescue_lab_factory)
    daemon_factory(lab, name="late.sock")  # the service starts
    window.service_page.retry_button.click()
    qtbot.waitUntil(lambda: window.stack.currentIndex() == FIND and len(window.find_page.candidates) == 2, timeout=20000)


def test_a_user_without_access_to_the_socket_is_told_what_to_do(qtbot, repair_kit, sock_dir) -> None:
    import socket

    path = str(sock_dir / "stale.sock")
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(path)  # nobody listens: a stale socket is left behind
    stale.close()
    window = repair_kit.open(path)
    qtbot.waitUntil(lambda: window.stack.currentIndex() == SERVICE, timeout=20000)
    assert "stale socket" in window.service_page.text.text()


def test_an_installation_that_is_running_blocks_repairs_with_a_plain_explanation(qtbot, rescue_lab_factory, daemon_factory, repair_kit) -> None:
    lab, _tree = uefi_gentoo_lab(rescue_lab_factory)
    daemon = daemon_factory(lab)
    daemon.registry.install_manager.status = "running"  # what the status reply of a daemon with a running installation says
    window = repair_kit.open(daemon.path)
    qtbot.waitUntil(lambda: window.stack.currentIndex() == SERVICE, timeout=20000)
    assert window.service_page.heading.text() == "An installation is running"
    assert window.service_page.text.text() == messages.INSTALL_RUNNING_TEXT and not window.find_page.candidates
    daemon.registry.install_manager.status = "idle"
    window.service_page.retry_button.click()
    qtbot.waitUntil(lambda: window.stack.currentIndex() == FIND and len(window.find_page.candidates) == 2, timeout=20000)


def test_a_scan_that_the_engine_refuses_shows_its_message_and_can_be_repeated(qtbot, rescue_lab_factory, daemon_factory, repair_kit) -> None:
    lab, _tree = uefi_gentoo_lab(rescue_lab_factory)
    daemon = daemon_factory(lab)
    daemon.api.manager.install_busy = lambda: "an installation is running"  # the engine's own check at the moment of the scan
    window = repair_kit.open(daemon.path)
    qtbot.waitUntil(lambda: window.find_page.notice.isVisibleTo(window.find_page), timeout=20000)
    assert "installation is running" in window.find_page.notice.text.text() and not window.find_page.candidates
    daemon.api.manager.install_busy = lambda: None
    window.find_page.scan_button.click()
    qtbot.waitUntil(lambda: len(window.find_page.candidates) == 2, timeout=20000)
    assert not window.find_page.notice.isVisibleTo(window.find_page)
