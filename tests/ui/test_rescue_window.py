"""The window as a whole: how it is opened and closed, the small screen, the keyboard, and what must be released at the end."""

from __future__ import annotations

import pytest
import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QAbstractButton, QApplication

from gentoo_installer import app as app_module
from gentoo_installer.rescue_app import DIAGNOSE, FINISH, RescueWindow, bounded_size
from tests.rescue.test_flow_encrypted import PASSPHRASE, encrypted_lab
from tests.ui.rescue_fakes import (  # noqa: F401  (fixtures)
    daemon_factory,
    gentoo_machine,
    open_system,
    proxy_factory,
    repair_kit,
    request_action,
    rescue_lab_factory,
    system_card,
    with_live_mounts,
)


def gone(window) -> bool:
    return not shiboken6.isValid(window) or not window.isVisible()


# ---- the installer opens it ------------------------------------------------------------------------------------------------------------
def test_the_installer_opens_the_repair_window_and_comes_back_when_it_is_closed(make_window, qtbot, tmp_path) -> None:
    installer = make_window()
    assert app_module.create_repair_window(str(tmp_path / "backend.sock")).__class__ is RescueWindow
    installer.open_repair()  # the welcome page's button
    repair = installer._repair_window
    assert isinstance(repair, RescueWindow) and not installer.isVisible() and repair.isVisible()
    assert repair.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose) and repair.socket_path == installer.socket_path
    qtbot.waitUntil(lambda: repair.stack.currentIndex() == 0, timeout=20000)  # no daemon in this test: the explanation page
    repair.force_close()
    qtbot.waitUntil(lambda: installer.isVisible() and installer._repair_window is None, timeout=10000)


def test_a_window_opened_by_itself_has_the_documented_constructor(qtbot) -> None:
    import inspect

    parameters = list(inspect.signature(RescueWindow.__init__).parameters)
    assert parameters[:3] == ["self", "socket_path", "parent"]


# ---- the small screen ------------------------------------------------------------------------------------------------------------------
def fits(window) -> None:
    """Nothing of the current page is wider than the space it has: the pages cannot scroll sideways, so a wide piece would be cut off."""
    page = window.current_page
    body = page.scroll_area.widget()
    viewport = page.scroll_area.viewport().width()
    assert body.minimumSizeHint().width() <= viewport, (type(page).__name__, body.minimumSizeHint().width(), viewport)
    assert page.footer.isVisible() and page.footer.geometry().bottom() <= window.height()  # the buttons are always in reach
    assert page.scroll_area.viewport().height() >= 150


@pytest.mark.parametrize("size", [(1024, 600), (800, 520)])
def test_every_page_fits_a_small_screen(qtbot, gentoo_machine, repair_kit, size) -> None:
    daemon, lab, tree = gentoo_machine
    (tree / "boot/grub/grub.cfg").unlink()
    window = repair_kit.open(daemon.path, width=size[0], height=size[1])
    qtbot.waitUntil(lambda: len(window.find_page.candidates) == 1 and not window.find_page._busy, timeout=20000)
    fits(window)
    window.find_page.disks.button.setChecked(True)
    fits(window)
    open_system(qtbot, window)
    fits(window)
    request_action(qtbot, window, "boot.bootloader")  # a high-risk plan: every confirmation control is on the page
    fits(window)
    window.plan_page.options_button.click()
    fits(window)
    window.plan_page.continue_button.click()
    qtbot.waitUntil(lambda: window.plan_page.state == "review", timeout=20000)
    window.plan_page.prep.button.setChecked(True)
    window.plan_page.reviewed.setChecked(True)
    window.plan_page.typed.setText("nvme0n1p5")
    fits(window)
    window.plan_page.run_button.click()
    qtbot.waitUntil(lambda: window.plan_page.state == "result", timeout=30000)
    fits(window)
    window.plan_page.done_button.click()
    qtbot.waitUntil(lambda: not window.diagnose_page.busy.isVisibleTo(window.diagnose_page), timeout=20000)
    fits(window)
    window.diagnose_page.terminal_button.click()
    fits(window)
    window.diagnose_page.finish_button.click()
    fits(window)


def test_the_window_never_asks_for_more_than_the_screen_has() -> None:
    assert bounded_size((1100, 740), (1024, 600)) == (1024, 552)
    assert bounded_size((1100, 740), (800, 480)) == (800, 520)  # the pages scroll, so this is the least that works
    assert bounded_size((1100, 740), (3840, 2160)) == (1100, 740)


# ---- the keyboard and assistive technology ----------------------------------------------------------------------------------------------
def visible_buttons(page):
    return [button for button in page.findChildren(QAbstractButton) if button.isVisibleTo(page) and button.text()]


def test_the_buttons_of_every_page_have_unique_mnemonics_and_the_page_names_a_starting_point(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, _lab, tree = gentoo_machine
    (tree / "boot/grub/grub.cfg").unlink()
    window = repair_kit.open(daemon.path)
    qtbot.waitUntil(lambda: len(window.find_page.candidates) == 1 and not window.find_page._busy, timeout=20000)
    seen: list[tuple[str, list[str]]] = []

    def check() -> None:
        page = window.current_page
        keys = [QKeySequence.mnemonic(button.text()).toString() for button in visible_buttons(page) if "&" in button.text()]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        seen.append((type(page).__name__, duplicates))
        for button in visible_buttons(page):
            assert button.text().strip() or button.accessibleName(), type(page).__name__

    check()
    open_system(qtbot, window)
    check()
    request_action(qtbot, window, "boot.bootloader")
    check()
    window.plan_page.cancel_button.click()
    window.diagnose_page.finish_button.click()
    check()
    assert all(not duplicates for _name, duplicates in seen), seen


def test_with_several_gentoo_systems_nothing_is_chosen_for_the_person(qtbot, rescue_lab_factory, daemon_factory, repair_kit) -> None:
    from tests.rescue import fakeroot as fr
    from tests.rescue import lsblk_fixtures as lf

    lab = rescue_lab_factory(lf.multiboot_three())
    lab.add_tree("/dev/nvme0n1p2", "gentoo", firmware="uefi")
    lab.add_tree("/dev/nvme0n1p3", "gentoo", firmware="uefi", root_uuid=fr.HOME_UUID)
    window = repair_kit.open(daemon_factory(lab).path)
    window.activateWindow()
    qtbot.waitUntil(lambda: len(window.find_page.candidates) == 2 and not window.find_page._busy, timeout=20000)
    window.find_page.focus_first()
    assert window.focusWidget() is window.find_page.scan_button  # nothing is chosen yet: the person is not pushed towards a system
    window.find_page.group.set_value(system_card(window, "/dev/nvme0n1p3"))
    window.find_page.focus_first()
    assert window.focusWidget() is window.find_page.group.cards[window.find_page.group.value()]


def test_the_first_focus_of_each_page_is_the_main_thing_to_do(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, _lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    window.activateWindow()
    qtbot.waitUntil(lambda: len(window.find_page.candidates) == 1 and not window.find_page._busy, timeout=20000)
    window.find_page.focus_first()
    assert window.focusWidget() is window.find_page.group.cards[window.find_page.group.value()]  # the one Gentoo is chosen already: the focus is on it
    open_system(qtbot, window)
    window.diagnose_page.focus_first()
    assert window.focusWidget() is window.diagnose_page.logs_button  # nothing to fix: the way on is to look at the logs


def test_the_steps_of_the_sidebar_follow_the_page_and_the_header_names_it(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, _lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    qtbot.waitUntil(lambda: len(window.find_page.candidates) == 1 and not window.find_page._busy, timeout=20000)

    def current_step() -> int:
        return [index for index, button in enumerate(window.step_buttons) if button.property("current")][0]

    assert current_step() == 0 and window.header_title.text() == "Find the Gentoo system to repair"
    assert window.header_subtitle.text().startswith("Repair a Gentoo system that does not start") and window.windowTitle() == "Repair a Gentoo system"
    open_system(qtbot, window)
    assert current_step() == 2 and window.header_title.text() == "What is wrong"
    request_action(qtbot, window, "boot.config")
    assert current_step() == 3 and window.header_title.text() == "Review and repair"
    window.plan_page.cancel_button.click()
    window.diagnose_page.finish_button.click()
    assert current_step() == 4 and window.header_title.text() == "Finish"
    assert not any(button.isEnabled() for button in window.step_buttons)  # they show where you are; they are not a way to jump around


# ---- closing -------------------------------------------------------------------------------------------------------------------------------
def test_closing_with_a_system_open_asks_and_staying_changes_nothing(qtbot, gentoo_machine, repair_kit, monkeypatch) -> None:
    daemon, lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    open_system(qtbot, window)
    asked: list[str] = []
    monkeypatch.setattr(window, "ask_close_choice", lambda what: asked.append(what) or "stay")
    window.close()
    assert shiboken6.isValid(window) and window.isVisible() and lab.system.mounted_targets()
    assert "Gentoo Linux on /dev/nvme0n1p5 is still open" in asked[0]


def test_closing_and_leaving_the_system_open_keeps_the_session_for_the_daemon(qtbot, gentoo_machine, repair_kit, monkeypatch) -> None:
    daemon, lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    system = open_system(qtbot, window)
    monkeypatch.setattr(window, "ask_close_choice", lambda what: "leave")
    window.close()
    qtbot.waitUntil(lambda: gone(window), timeout=10000)
    assert lab.system.mounted_targets() and daemon.manager.session(system.session_id) is not None


def test_closing_and_closing_the_system_releases_everything_before_the_window_goes(qtbot, gentoo_machine, repair_kit, monkeypatch) -> None:
    daemon, lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    open_system(qtbot, window)
    monkeypatch.setattr(window, "ask_close_choice", lambda what: "close")
    window.close()
    assert shiboken6.isValid(window) and window.isVisible()  # still there: the system is being closed first
    qtbot.waitUntil(lambda: gone(window), timeout=20000)
    assert lab.system.mounted_targets() == [] and daemon.manager.session_summary("")["sessions"] == []


def test_unlocked_disks_without_an_open_system_are_released_too(qtbot, rescue_lab_factory, daemon_factory, repair_kit, monkeypatch) -> None:
    lab, _tree, _boot = encrypted_lab(rescue_lab_factory, initramfs=False)
    window = repair_kit.open(daemon_factory(lab).path)
    device = "/dev/nvme0n1p3"
    qtbot.waitUntil(lambda: window.find_page.findChild(object, f"rescueUnlock:{device}") is not None, timeout=20000)
    window.find_page.findChild(object, f"rescueUnlock:{device}").click()
    window.find_page.unlock_panel.passphrase.edit.setText(PASSPHRASE)
    window.find_page.unlock_panel.go_button.click()
    qtbot.waitUntil(lambda: window.storage_opened and not window.find_page._busy, timeout=20000)
    assert lab.system.mapped
    asked: list[str] = []
    monkeypatch.setattr(window, "ask_close_choice", lambda what: asked.append(what) or "close")
    window.close()
    qtbot.waitUntil(lambda: gone(window), timeout=20000)
    assert "Encrypted disks" in asked[0] and lab.system.mapped == {} and lab.system.vgs["vg0"].active is False


def test_nothing_is_asked_when_nothing_is_open(qtbot, gentoo_machine, repair_kit, monkeypatch) -> None:
    daemon, _lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    qtbot.waitUntil(lambda: len(window.find_page.candidates) == 1 and not window.find_page._busy, timeout=20000)
    monkeypatch.setattr(window, "ask_close_choice", lambda what: pytest.fail("nothing is open, nothing should be asked"))
    window.close()
    qtbot.waitUntil(lambda: gone(window), timeout=10000)


def test_closing_the_window_while_the_daemon_is_scanning_never_leaves_a_thread_behind(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, _lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    window.close()  # the scan has just started
    qtbot.waitUntil(lambda: gone(window), timeout=10000)
    qtbot.wait(200)
    assert QApplication.instance() is not None  # and the process is still alive: no QThread was destroyed while it ran


# ---- a daemon that already holds a session ---------------------------------------------------------------------------------------------
def test_a_session_left_open_by_an_earlier_window_can_be_taken_over_when_the_mount_finds_it(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, lab, _tree = gentoo_machine
    with_live_mounts(lab)  # the real lsblk shows the session's mount, so the engine refuses to mount the same system a second time
    first = repair_kit.open(daemon.path)
    system = open_system(qtbot, first)
    first.force_close()  # the first window is gone; the session stays open in the daemon
    second = repair_kit.open(daemon.path)
    qtbot.waitUntil(lambda: len(second.find_page.candidates) >= 1 and not second.find_page._busy, timeout=20000)
    second.find_page.group.set_value(system_card(second, "/dev/nvme0n1p5"))
    second.find_page.continue_button.click()
    qtbot.waitUntil(lambda: second.stack.currentIndex() == DIAGNOSE and second.system is not None and not second.diagnose_page.busy.isVisibleTo(second.diagnose_page), timeout=20000)
    assert second.system.session_id == system.session_id  # it continued with the open session instead of failing
    assert second.system.actions and second.diagnose_page.system_label.text().startswith("Gentoo Linux")



def test_after_the_service_restarted_the_open_system_is_listed_and_can_be_continued_and_released(qtbot, rescue_lab_factory, daemon_factory, repair_kit) -> None:
    """The service died or was restarted while a system was open. Its mounts and flags survive; the new service takes the session over and the window offers it."""
    from tests.rescue.flow import mount_through_the_manager, uefi_gentoo_lab

    lab, _tree = uefi_gentoo_lab(rescue_lab_factory)
    _old_manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")  # the old service; it is gone and only its state files are left
    window = repair_kit.open(daemon_factory(lab).path)
    page = window.find_page
    qtbot.waitUntil(lambda: page.findChild(object, f"rescueResume:{sid}") is not None, timeout=20000)
    assert page.open_card.isVisibleTo(page) and not page.left_card.isVisibleTo(page)
    page.findChild(object, f"rescueResume:{sid}").click()
    qtbot.waitUntil(lambda: window.stack.currentIndex() == DIAGNOSE and window.system is not None and not window.diagnose_page.busy.isVisibleTo(window.diagnose_page), timeout=20000)
    assert window.system.session_id == sid and window.system.actions
    window.diagnose_page.finish_button.click()
    window.finish_page.unmount_button.click()
    qtbot.waitUntil(lambda: window.finish_page.state == "done", timeout=20000)

    def nothing_is_left() -> None:
        assert lab.system.mounted_targets() == [] and not any(lab.system.ro.values())

    # The window looks at the disks again after the unmount, and its probes flag a disk read-only for a moment: wait for what stays.
    qtbot.waitUntil(nothing_is_left, timeout=20000)


def test_what_a_cut_short_repair_left_behind_is_shown_on_the_first_page_and_released_there(qtbot, rescue_lab_factory, daemon_factory, repair_kit) -> None:
    """Disks flagged read-only by a session that no longer has any mount: nothing to continue, but the disks must be freed."""
    from tests.rescue.flow import mount_through_the_manager, uefi_gentoo_lab

    lab, _tree = uefi_gentoo_lab(rescue_lab_factory)
    _old_manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    for target in sorted(lab.system.mounted_targets(), reverse=True):  # somebody unmounted everything by hand
        lab.system.mounts.pop(target)
    assert lab.system.ro["/dev/nvme0n1p5"] is True
    window = repair_kit.open(daemon_factory(lab).path)
    page = window.find_page
    qtbot.waitUntil(lambda: page.left_card.isVisibleTo(page), timeout=20000)
    shown = " ".join(label.text() for label in page.left_card.findChildren(type(page.hints)))
    assert sid in shown and "cut short" in shown and page.release_button.isVisibleTo(page) and not page.open_card.isVisibleTo(page)
    qtbot.waitUntil(lambda: not page._busy, timeout=20000)
    page.release_button.click()
    qtbot.waitUntil(lambda: not page.left_card.isVisibleTo(page) and not page._busy and page.notice.isVisibleTo(page), timeout=20000)
    assert not any(lab.system.ro.values()) and not list((lab.state_dir / "sessions").glob("*.json"))
    assert "released" in page.notice.text.text()  # and it is still said after the page has looked at the disks again


def test_an_unlocked_disk_from_an_earlier_window_is_offered_for_release(qtbot, rescue_lab_factory, daemon_factory, repair_kit) -> None:
    from gentoo_installerd.rescue.scan import ScanOptions
    from tests.rescue.test_flow_encrypted import MAPPER, encrypted_lab

    lab, _tree, _boot = encrypted_lab(rescue_lab_factory)
    old = lab.manager()
    old.scan(None, ScanOptions())
    old.unlock(None, {"device": "/dev/nvme0n1p3", "passphrase": PASSPHRASE})
    old.activate(None, {"kind": "lvm", "vg": "vg0"})
    window = repair_kit.open(daemon_factory(lab).path)
    page = window.find_page
    qtbot.waitUntil(lambda: page.left_card.isVisibleTo(page), timeout=20000)
    assert "vg0" in " ".join(label.text() for label in page.left_card.findChildren(type(page.hints))) or MAPPER in " ".join(label.text() for label in page.left_card.findChildren(type(page.hints)))
    qtbot.waitUntil(lambda: not page._busy, timeout=20000)
    page.release_button.click()
    qtbot.waitUntil(lambda: not page.left_card.isVisibleTo(page) and not page._busy, timeout=20000)
    assert lab.system.mapped == {} and lab.system.vgs["vg0"].active is False


def test_a_mount_that_the_tool_itself_left_is_not_blamed_on_the_file_manager(qtbot, rescue_lab_factory, daemon_factory, repair_kit) -> None:
    """The state file of the old session is lost, so nothing can be taken over; the mount page must still say whose mount it is."""
    from tests.rescue.flow import mount_through_the_manager, uefi_gentoo_lab, with_live_mounts

    lab, _tree = uefi_gentoo_lab(rescue_lab_factory)
    with_live_mounts(lab)
    old_manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    old_manager.session(sid).state_path.unlink()
    window = repair_kit.open(daemon_factory(lab).path)
    qtbot.waitUntil(lambda: len(window.find_page.candidates) >= 1 and not window.find_page._busy, timeout=20000)
    window.find_page.group.set_value(system_card(window, "/dev/nvme0n1p5"))
    window.find_page.continue_button.click()
    mount = window.mount_page
    qtbot.waitUntil(lambda: mount.again_button.isVisibleTo(mount), timeout=20000)
    said = mount.notice.text.text()
    assert "earlier repair" in said and "Release them" in said and "file manager" not in said


def test_a_system_that_cannot_be_released_keeps_the_window_open_until_it_is_forced_or_the_person_goes_back(qtbot, gentoo_machine, repair_kit, monkeypatch) -> None:
    daemon, lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    system = open_system(qtbot, window)
    efi = daemon.manager.session(system.session_id).root + "/efi"
    lab.system.fail_umount[efi] = (32, "umount: target is busy.")
    lab.system.holders[efi] = "                     USER        PID ACCESS COMMAND\n" + f"{efi}:      gentoo     4321 ..c.. thunar\n"
    monkeypatch.setattr(window, "ask_close_choice", lambda what: "close")
    window.close()
    qtbot.waitUntil(lambda: window.finish_page.state == "busy", timeout=20000)
    assert shiboken6.isValid(window) and window.isVisible() and window.stack.currentIndex() == FINISH and "thunar" in window.finish_page.holders.text()
    window.finish_page.back_button.click()  # the person decides to stay: the window must not close by itself later
    assert window.stack.currentIndex() == DIAGNOSE and not window._close_after_release
    window.diagnose_page.finish_button.click()
    window._close_after_release = True  # ... and closing again, this time forcing the release
    window.finish_page.force_button.click()
    window.finish_page.confirm.confirm_button.click()
    qtbot.waitUntil(lambda: gone(window), timeout=20000)
    assert lab.system.mounted_targets() == []
