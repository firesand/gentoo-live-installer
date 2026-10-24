"""Running a plan: a lost connection that is picked up again, the cancel rules, stale plans and failed conditions."""

from __future__ import annotations

import pytest
import shiboken6

from gentoo_installer.rescue_app import DIAGNOSE
from gentoo_installer.rescue_ui import messages
from gentoo_installer.rescue_ui.plan_page import PlanPage
from tests.rescue.flow import plan_and_run
from tests.ui.rescue_fakes import (  # noqa: F401  (fixtures)
    CuttingProxy,
    daemon_factory,
    gate_command,
    gate_host_command,
    gentoo_machine,
    open_system,
    proxy_factory,
    repair_kit,
    request_action,
    rescue_lab_factory,
)


@pytest.fixture(autouse=True)
def quick_reconnects(monkeypatch):
    monkeypatch.setattr(PlanPage, "RECONNECT_MS", 40)


def run_plan(qtbot, window, action: str = "boot.config") -> None:
    request_action(qtbot, window, action)
    window.plan_page.reviewed.setChecked(True)
    window.plan_page.run_button.click()


# ---- a lost connection -------------------------------------------------------------------------------------------------------------
def test_a_run_whose_connection_breaks_is_picked_up_again_and_runs_once(qtbot, gentoo_machine, repair_kit, proxy_factory) -> None:
    daemon, lab, tree = gentoo_machine
    (tree / "boot/grub/grub.cfg").unlink()
    proxy = proxy_factory(daemon)
    proxy.cut("rescue.run", 12)  # the window loses its connection in the middle of the stream
    window = repair_kit.open(proxy.path)
    open_system(qtbot, window)
    run_plan(qtbot, window)
    qtbot.waitUntil(lambda: window.plan_page.state == "result", timeout=30000)
    page = window.plan_page
    assert page.tracker.outcome == "success" and page.result_title.text() == messages.OUTCOME_TITLES["success"]
    assert proxy.count("rescue.run") == 1 and proxy.count("rescue.events") >= 1
    events = [request for request in proxy.requests if request["action"] == "rescue.events"]
    assert events[0]["after_seq"] > 0 and events[0]["job_id"] == page.tracker.job_id  # it asked for what came after the last event it had
    assert [call for _root, _env, call in lab.system.chroot_calls if call[0] == "grub-mkconfig"] == [("grub-mkconfig", "-o", "/boot/grub/grub.cfg")]  # the repair ran once
    texts = [page.steps_list.item(index).text() for index in range(page.steps_list.count())]
    assert len(texts) == len(set(texts)) and all(text.startswith("✓") for text in texts)  # every step once, none repeated by the replay
    assert page.log.toPlainText().count("Generating grub configuration file") == 1
    assert not page.notice.isVisibleTo(page)


def test_when_the_daemon_stays_away_the_window_says_so_and_can_try_again(qtbot, gentoo_machine, repair_kit, proxy_factory, monkeypatch) -> None:
    daemon, lab, tree = gentoo_machine
    (tree / "boot/grub/grub.cfg").unlink()
    monkeypatch.setattr(PlanPage, "RECONNECT_TRIES", 3)
    proxy = proxy_factory(daemon, "away.sock")
    proxy.cut("rescue.run", 12)
    window = repair_kit.open(proxy.path)
    open_system(qtbot, window)
    run_plan(qtbot, window)
    page = window.plan_page
    qtbot.waitUntil(lambda: proxy.count("rescue.run") == 1 and page.notice.isVisibleTo(page), timeout=30000)
    assert page.close_blocker() == messages.CANNOT_CLOSE_TEXT  # the last step it heard of cannot be stopped, and it cannot tell whether that step still runs
    proxy.close()  # the connection is gone for good
    qtbot.waitUntil(lambda: page.reconnect_button.isVisibleTo(page), timeout=30000)
    assert page.state == "running" and messages.RECONNECT_FAILED_TEXT in page.notice.text.text() and not page.done_button.isVisibleTo(page)
    assert page.close_blocker() == ""  # having given up, the window may be closed
    again = proxy_factory(daemon, "away.sock")  # the daemon is back
    page.reconnect_button.click()
    qtbot.waitUntil(lambda: page.state == "result", timeout=30000)
    assert page.tracker.outcome == "success" and again.count("rescue.events") >= 1 and len(lab.system.chroot_calls) >= 1


def test_a_connection_that_breaks_before_anything_arrived_asks_the_session_what_it_is_doing(qtbot, gentoo_machine, repair_kit, proxy_factory) -> None:
    daemon, lab, tree = gentoo_machine
    (tree / "boot/grub/grub.cfg").unlink()
    proxy = proxy_factory(daemon)
    proxy.cut("rescue.run", 0)  # not a single event reaches the window
    window = repair_kit.open(proxy.path)
    open_system(qtbot, window)
    run_plan(qtbot, window)
    qtbot.waitUntil(lambda: window.plan_page.state == "result", timeout=30000)
    page = window.plan_page
    assert proxy.count("rescue.session") >= 1  # it did not guess which operation was its own
    # the run had already finished when the window looked: the session is idle, so the result is reported as not known
    # (never the result of some other operation)
    assert page.tracker.result is None or page.tracker.outcome in {"success", ""}
    assert page.result_title.text() in {"The result is not known", messages.OUTCOME_TITLES["success"]}


# ---- cancel and close ----------------------------------------------------------------------------------------------------------
def test_cancel_and_close_are_refused_while_a_step_runs_that_cannot_be_stopped_safely(qtbot, gentoo_machine, repair_kit, proxy_factory, monkeypatch) -> None:
    daemon, lab, tree = gentoo_machine
    (tree / "boot/grub/grub.cfg").unlink()
    gate = gate_command(lab, "grub-mkconfig")
    proxy = proxy_factory(daemon)
    window = repair_kit.open(proxy.path)
    shown: list[str] = []
    monkeypatch.setattr(window, "show_info", lambda title, text: shown.append(text))
    open_system(qtbot, window)
    run_plan(qtbot, window)
    page = window.plan_page
    qtbot.waitUntil(gate.entered.is_set, timeout=20000)
    qtbot.waitUntil(lambda: page.tracker.non_atomic_running and page.stop_button.isVisibleTo(page), timeout=20000)
    try:
        assert not page.cancel_button.isEnabled() and "cannot be stopped safely" in page.step_warning.text()
        assert page.request_cancel() is False and proxy.count("rescue.cancel") == 0
        window.close()  # the window refuses to close
        assert shown == [messages.CANNOT_CLOSE_TEXT] and shiboken6.isValid(window) and window.isVisible()
        # "Stop now" asks first, with the strong warning, and keeps waiting when the person changes their mind
        page.stop_button.click()
        assert page.stop_confirm.isVisibleTo(page) and "half-written" in page.stop_confirm.body.text()
        page.stop_confirm.cancel_button.click()
        assert not page.stop_confirm.isVisibleTo(page) and proxy.count("rescue.cancel") == 0
        page.stop_button.click()
        page.stop_confirm.confirm_button.click()
        qtbot.waitUntil(lambda: proxy.count("rescue.cancel") == 1, timeout=10000)
        cancel = next(request for request in proxy.requests if request["action"] == "rescue.cancel")
        assert cancel["force"] is True and cancel["session_id"] == window.system.session_id
    finally:
        gate.release.set()
    qtbot.waitUntil(lambda: page.state == "result", timeout=30000)
    assert page.tracker.outcome == "cancelled" and page.result_title.text() == messages.OUTCOME_TITLES["cancelled"]
    [stopped] = page.tracker.failed_steps()  # the command that was killed is shown as the step that did not finish
    assert "grub-mkconfig" in stopped.text and "This step failed" in page.result_text.text()
    page.done_button.click()
    assert window.stack.currentIndex() == DIAGNOSE


def test_a_step_that_can_be_stopped_safely_can_be_cancelled_and_the_plan_stops_after_it(qtbot, gentoo_machine, repair_kit, proxy_factory) -> None:
    daemon, lab, tree = gentoo_machine
    (tree / "boot/grub/grub.cfg").unlink()
    gate = gate_host_command(lab, "blockdev", "--setrw")
    proxy = proxy_factory(daemon)
    window = repair_kit.open(proxy.path)
    open_system(qtbot, window)
    gate.arm()
    run_plan(qtbot, window)
    page = window.plan_page
    qtbot.waitUntil(gate.entered.is_set, timeout=20000)
    qtbot.waitUntil(lambda: page.tracker.running is not None and page.tracker.running.id.startswith("w-setrw"), timeout=20000)
    try:
        assert page.tracker.running.atomic and page.cancel_button.isEnabled() and not page.stop_button.isVisibleTo(page)
        page.cancel_button.click()
        qtbot.waitUntil(lambda: proxy.count("rescue.cancel") == 1, timeout=10000)
        assert next(r for r in proxy.requests if r["action"] == "rescue.cancel").get("force") in (None, False)
        assert not page.cancel_button.isEnabled()
    finally:
        gate.release.set()
    qtbot.waitUntil(lambda: page.state == "result", timeout=30000)
    assert page.tracker.outcome == "cancelled" and not lab.system.chroot_calls  # the plan stopped after the step that was running; grub-mkconfig never ran


def test_closing_the_window_during_a_safe_step_stops_the_repair_first_and_then_closes(qtbot, gentoo_machine, repair_kit, monkeypatch) -> None:
    daemon, lab, tree = gentoo_machine
    (tree / "boot/grub/grub.cfg").unlink()
    gate = gate_host_command(lab, "blockdev", "--setrw")
    window = repair_kit.open(daemon.path)
    monkeypatch.setattr(window, "ask_stop_and_close", lambda: True)
    monkeypatch.setattr(window, "ask_close_choice", lambda what: "leave")
    open_system(qtbot, window)
    gate.arm()
    run_plan(qtbot, window)
    qtbot.waitUntil(gate.entered.is_set, timeout=20000)
    qtbot.waitUntil(lambda: window.plan_page.tracker.running is not None, timeout=20000)
    window.close()
    assert shiboken6.isValid(window) and window.isVisible()  # it waits for the repair to stop
    gate.release.set()
    qtbot.waitUntil(lambda: not shiboken6.isValid(window) or not window.isVisible(), timeout=30000)
    assert not lab.system.chroot_calls


def test_the_window_asks_before_it_closes_over_a_running_repair_and_keeps_running_when_told_to(qtbot, gentoo_machine, repair_kit, monkeypatch) -> None:
    daemon, lab, tree = gentoo_machine
    (tree / "boot/grub/grub.cfg").unlink()
    gate = gate_host_command(lab, "blockdev", "--setrw")
    window = repair_kit.open(daemon.path)
    monkeypatch.setattr(window, "ask_stop_and_close", lambda: False)
    open_system(qtbot, window)
    gate.arm()
    run_plan(qtbot, window)
    qtbot.waitUntil(gate.entered.is_set, timeout=20000)
    qtbot.waitUntil(lambda: window.plan_page.tracker.running is not None, timeout=20000)
    try:
        window.close()
        assert shiboken6.isValid(window) and window.isVisible() and window.plan_page.state == "running"
    finally:
        gate.release.set()
    qtbot.waitUntil(lambda: window.plan_page.state == "result", timeout=30000)
    assert window.plan_page.tracker.outcome == "success"


# ---- plans that cannot run as shown ---------------------------------------------------------------------------------------------------
def test_a_plan_that_went_stale_is_made_again_and_asks_for_the_confirmation_again(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, lab, tree = gentoo_machine
    (tree / "boot/grub/grub.cfg").unlink()
    window = repair_kit.open(daemon.path)
    open_system(qtbot, window)
    request_action(qtbot, window, "boot.config")
    page = window.plan_page
    first = page.plan["plan_id"]
    page.reviewed.setChecked(True)
    assert page.run_button.isEnabled()
    plan_and_run(daemon.manager, window.system.session_id, "boot.initramfs")  # somebody changed the system meanwhile (it is read-write now)
    chroot_before = len(lab.system.chroot_calls)
    page.run_button.click()
    qtbot.waitUntil(lambda: page.state == "review" and page.plan.get("plan_id") != first, timeout=30000)
    assert "changed" in page.notice.text.text() and not page.reviewed.isChecked() and not page.run_button.isEnabled()
    assert len(lab.system.chroot_calls) == chroot_before  # nothing ran on the stale plan
    page.reviewed.setChecked(True)
    page.run_button.click()
    qtbot.waitUntil(lambda: page.state == "result", timeout=30000)
    assert page.tracker.outcome == "success"


def test_a_plan_whose_condition_is_not_met_cannot_be_run(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, lab, tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    open_system(qtbot, window)
    request_action(qtbot, window, "efi.add")  # every option has a default, so the plan comes first
    page = window.plan_page
    assert page.state == "review" and "was not found" not in page.checks.text() and page.options_button.isVisibleTo(page)
    page.options_button.click()  # ... and the options can still be changed
    assert page.state == "options"
    page._form.fields["loader"].setText("\\EFI\\nothing\\missing.efi")
    page.continue_button.click()
    qtbot.waitUntil(lambda: page.state == "review", timeout=20000)
    assert "was not found on the EFI partition" in page.checks.text() and "×" in page.checks.text()
    page.reviewed.setChecked(True)
    assert not page.run_button.isEnabled() and "cannot run yet" in page.notice.text.text()
    assert not lab.system.chroot_calls and lab.system.nvram["entries"] == {}


def test_the_late_answer_to_an_earlier_repair_does_not_land_in_a_later_one(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, _lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    system = open_system(qtbot, window)
    page = window.plan_page
    seen: list[str] = []
    page.begin(system, next(spec for spec in system.actions if spec["id"] == "boot.config"))
    handler = page._current(lambda plan: seen.append(plan["action"]))  # what the answer to that first request would do
    qtbot.waitUntil(lambda: page.state == "review", timeout=20000)
    page.begin(system, next(spec for spec in system.actions if spec["id"] == "boot.initramfs"))  # the person went on to another repair
    handler({"action": "boot.config"})  # the first answer arrives now
    assert seen == []
    qtbot.waitUntil(lambda: page.state == "review", timeout=20000)
    assert page.plan["action"] == "boot.initramfs" and page.heading.text() == "Rebuild the initramfs"
