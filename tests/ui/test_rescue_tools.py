"""The tools around a repair: the disk list with its file differences, a moved disk, the logs, a terminal, the record, and letting go of a busy system."""

from __future__ import annotations

import shlex

from gentoo_installer.rescue_app import DIAGNOSE, FIND, FINISH, LOGS
from gentoo_installer.rescue_ui import messages
from tests.rescue import fakeroot as fr
from tests.rescue.flow import uefi_gentoo_lab
from tests.ui.rescue_fakes import (  # noqa: F401  (fixtures)
    daemon_factory,
    gentoo_machine,
    open_system,
    proxy_factory,
    repair_kit,
    request_action,
    rescue_lab_factory,
)


def finish(qtbot, window) -> None:
    window.diagnose_page.finish_button.click()
    assert window.stack.currentIndex() == FINISH


# ---- the disk list --------------------------------------------------------------------------------------------------------------
def test_a_disk_list_that_names_a_missing_disk_is_shown_as_a_difference_and_fixed(qtbot, rescue_lab_factory, daemon_factory, repair_kit) -> None:
    broken = fr.fstab_text(esp=fr.ESP_UUID, swap=None, home=fr.DEAD_UUID)  # /home is on a disk that is not in this computer
    lab, tree = uefi_gentoo_lab(rescue_lab_factory, fstab=broken)
    window = repair_kit.open(daemon_factory(lab).path)
    open_system(qtbot, window)
    assert any(f["id"] == "D07" for f in window.system.findings)
    finding = window.diagnose_page.findChild(object, "rescueFinding:D07:fstab.fix")
    assert finding is not None and "Fix the disk list" in finding.text()
    finding.click()
    qtbot.waitUntil(lambda: window.plan_page.state == "review", timeout=20000)
    page = window.plan_page
    assert page.risk_label.text().startswith("High risk") and page.diff.isVisibleTo(page) and page.diff.is_open
    shown = page.diff_view.toPlainText()
    assert "-UUID=" + fr.DEAD_UUID in shown.replace("\t", "") or fr.DEAD_UUID in shown
    assert "/home" in shown and "nofail" in shown and page.diff_choices.isVisibleTo(page) and "do not wait for it" in page.diff_choices.text()
    page.reviewed.setChecked(True)
    page.typed.setText("nvme0n1p5")
    page.run_button.click()
    qtbot.waitUntil(lambda: page.state == "result", timeout=30000)
    assert page.tracker.outcome == "success" and "every fstab entry now points at a disk that exists" in page.result_text.text()
    finish(qtbot, window)
    window.finish_page.unmount_button.click()
    qtbot.waitUntil(lambda: window.finish_page.state == "done", timeout=20000)
    home = next(line for line in (tree / "etc/fstab").read_text().splitlines() if "/home" in line and not line.startswith("#"))
    assert "nofail" in home  # the entry stays, but the system no longer waits for the missing disk
    assert len([path for path in (tree / "etc").iterdir() if path.name.startswith("fstab")]) >= 2  # and a copy of the old file is kept beside it


# ---- a moved disk ---------------------------------------------------------------------------------------------------------------------
def test_a_disk_that_was_moved_to_another_computer_gets_the_matching_advice(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, _lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    open_system(qtbot, window)
    page = window.diagnose_page
    assert not any(f["id"] == "D20" for f in window.system.findings)
    page.moved_box.setChecked(True)
    qtbot.waitUntil(lambda: any(f["id"] == "D20" for f in window.system.findings) and not page.busy.isVisibleTo(page), timeout=20000)
    assert page.findChild(object, "rescueFinding:D20:boot.initramfs") is not None and page.findChild(object, "rescueFinding:D20:boot.bootloader") is not None
    request_action(qtbot, window, "boot.initramfs", "options")  # the options come first, with drivers for many kinds of hardware already chosen
    form = window.plan_page._form
    assert form.fields["generic"].isChecked() and form.values()["generic"] is True
    window.plan_page.continue_button.click()
    qtbot.waitUntil(lambda: window.plan_page.state == "review", timeout=20000)
    assert window.plan_page.plan["params"]["generic"] is True
    window.plan_page.cancel_button.click()
    assert window.stack.currentIndex() == DIAGNOSE
    page.moved_box.setChecked(False)
    qtbot.waitUntil(lambda: not any(f["id"] == "D20" for f in window.system.findings) and not page.busy.isVisibleTo(page), timeout=20000)


# ---- the logs ----------------------------------------------------------------------------------------------------------------------------
def test_the_logs_of_the_system_can_be_read_without_changing_anything(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    open_system(qtbot, window)
    window.diagnose_page.logs_button.click()
    assert window.stack.currentIndex() == LOGS
    page = window.logs_page
    qtbot.waitUntil(lambda: page.sources.count() >= 2 and "Started emerge" in page.text.toPlainText(), timeout=20000)  # the first file is shown at once
    paths = [page.sources.itemData(index) for index in range(page.sources.count())]
    assert paths == ["/var/log/emerge.log", "/var/log/messages"] and messages.LOGS_NOTICE in "".join(label.text() for label in page.findChildren(type(page.journal_note)))
    page.sources.setCurrentIndex(paths.index("/var/log/messages"))
    page.show_button.click()
    qtbot.waitUntil(lambda: "kernel: boot" in page.text.toPlainText(), timeout=20000)
    assert not lab.system.chroot_calls and window.system.mode == "ro"
    page.back_button.click()
    assert window.stack.currentIndex() == DIAGNOSE


# ---- the terminal ----------------------------------------------------------------------------------------------------------------------
def terminal_line(repair_kit) -> list[str]:
    program, arguments = repair_kit.launched[-1]
    assert program == "xfce4-terminal" and arguments[0] == "--title" and arguments[2:5] == ["-x", "sh", "-c"]
    return shlex.split(arguments[-1].split("; echo;")[0])


def test_a_terminal_in_the_system_asks_for_the_disk_name_and_runs_the_terminal_tool_on_the_windows_socket(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, _lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    system = open_system(qtbot, window)
    page = window.diagnose_page
    page.terminal_button.click()
    assert page.confirm.isVisibleTo(page) and not page.confirm.confirm_button.isEnabled()
    assert not repair_kit.launched  # nothing opens before the name is typed
    page.confirm.typed.setText("nvme0n1")
    assert not page.confirm.confirm_button.isEnabled()
    page.confirm.typed.setText("nvme0n1p5")
    page.confirm.confirm_button.click()
    assert terminal_line(repair_kit) == ["gentoo-rescue", "--socket", daemon.path, "shell", system.session_id, "--typed", "nvme0n1p5"]
    assert "A terminal window was opened" in page.notice.text.text()
    assert "Repair: Gentoo Linux (read-only)" == repair_kit.launched[-1][1][1]
    assert not page.writable_terminal_button.isVisibleTo(page)  # a terminal that can change files needs repairs to be enabled first


def test_a_terminal_that_can_change_files_also_needs_the_backup_phrase(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, lab, tree = gentoo_machine
    (tree / "boot/grub/grub.cfg").unlink()
    window = repair_kit.open(daemon.path)
    system = open_system(qtbot, window)
    page = window.diagnose_page
    page.recommended_button.click()
    qtbot.waitUntil(lambda: window.plan_page.state == "review", timeout=20000)
    window.plan_page.reviewed.setChecked(True)
    window.plan_page.run_button.click()
    qtbot.waitUntil(lambda: window.plan_page.state == "result", timeout=30000)
    window.plan_page.done_button.click()
    qtbot.waitUntil(lambda: page.writable_terminal_button.isVisibleTo(page) and not page.busy.isVisibleTo(page), timeout=20000)
    page.writable_terminal_button.click()
    assert page.confirm.phrase.isVisibleTo(page)
    page.confirm.typed.setText("nvme0n1p5")
    assert not page.confirm.confirm_button.isEnabled()
    page.confirm.phrase.setText("I HAVE A BACKUP")
    page.confirm.confirm_button.click()
    argv = terminal_line(repair_kit)
    assert argv == ["gentoo-rescue", "--socket", daemon.path, "shell", system.session_id, "--typed", "nvme0n1p5", "--writable", "--i-have-a-backup"]
    assert "Repair: Gentoo Linux (read-write)" == repair_kit.launched[-1][1][1]


def test_a_terminal_for_a_look_only_session_is_a_shell_of_the_live_system(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, _lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    qtbot.waitUntil(lambda: len(window.find_page.candidates) == 2 and not window.find_page._busy, timeout=20000)
    window.find_page.group.set_value(next(c["id"] for c in window.find_page.candidates if c["device"] == "/dev/nvme0n1p5"))
    window.find_page.continue_button.click()
    qtbot.waitUntil(lambda: window.mount_page.open_button.isEnabled(), timeout=20000)
    window.mount_page.inspect_only.setChecked(True)
    qtbot.waitUntil(lambda: window.mount_page.open_button.isEnabled() and window.mount_page.choice.allow_write is False, timeout=20000)
    window.mount_page.open_button.click()
    qtbot.waitUntil(lambda: window.stack.currentIndex() == DIAGNOSE, timeout=20000)
    page = window.diagnose_page
    page.terminal_button.click()
    page.confirm.typed.setText("nvme0n1p5")
    page.confirm.confirm_button.click()
    assert terminal_line(repair_kit)[-1] == "--host" and "(look only)" in repair_kit.launched[-1][1][1]


def test_without_a_terminal_program_the_window_says_so(qtbot, gentoo_machine, repair_kit) -> None:
    repair_kit.terminal_available = False
    daemon, _lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    open_system(qtbot, window)
    page = window.diagnose_page
    page.terminal_button.click()
    page.confirm.typed.setText("nvme0n1p5")
    page.confirm.confirm_button.click()
    assert not repair_kit.launched and "No terminal program" in page.notice.text.text() and page.notice.kind == "error"


def test_sudo_is_only_used_when_the_user_cannot_open_the_socket_itself(qtbot, gentoo_machine, repair_kit, monkeypatch) -> None:
    from gentoo_installer.rescue_ui import launcher

    daemon, _lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    open_system(qtbot, window)
    window.diagnose_page.terminal_button.click()
    window.diagnose_page.confirm.typed.setText("nvme0n1p5")
    window.diagnose_page.confirm.confirm_button.click()
    assert terminal_line(repair_kit)[0] == "gentoo-rescue"
    monkeypatch.setattr(launcher.os, "access", lambda path, mode: False)
    started, _problem = launcher.open_terminal(daemon.path, "r1", "/dev/sda2", "t", access=lambda path, mode: False)
    assert started and terminal_line(repair_kit)[:2] == ["sudo", "gentoo-rescue"]


# ---- letting go ----------------------------------------------------------------------------------------------------------------------------
def test_a_system_that_is_still_in_use_is_closed_after_the_programs_are_closed_or_when_forced(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    system = open_system(qtbot, window)
    session = daemon.manager.session(system.session_id)
    efi = session.root + "/efi"
    lab.system.fail_umount[efi] = (32, "umount: target is busy.")
    lab.system.holders[efi] = "                     USER        PID ACCESS COMMAND\n" + f"{efi}:      gentoo     4321 ..c.. thunar\n"
    finish(qtbot, window)
    page = window.finish_page
    page.unmount_button.click()
    qtbot.waitUntil(lambda: page.state == "busy", timeout=20000)
    assert "thunar" in page.holders.text() and "Close them" in page.holders.text() and page.force_button.isVisibleTo(page)
    assert page.unmount_button.text().replace("&", "") == "Try again" and lab.system.mounted_targets()
    page.unmount_button.click()  # the program is still there
    qtbot.waitUntil(lambda: page.state == "busy" and not page.busy.isVisibleTo(page), timeout=20000)
    page.force_button.click()
    assert page.confirm.isVisibleTo(page) and "may be damaged" in page.confirm.body.text()
    page.confirm.confirm_button.click()
    qtbot.waitUntil(lambda: page.state == "done", timeout=20000)
    assert lab.system.mounted_targets() == []


def test_a_terminal_that_is_still_open_is_named_when_the_system_cannot_be_closed(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    system = open_system(qtbot, window)
    session = daemon.manager.session(system.session_id)
    session.shells.append({"pid": 999999, "writable": False, "mode": "chroot-ro", "transcript": "x"})  # a terminal of this session that is still open
    finish(qtbot, window)
    page = window.finish_page
    page.unmount_button.click()
    qtbot.waitUntil(lambda: page.state == "busy", timeout=20000)
    assert "terminal" in page.notice.text.text().lower()
    session.shells.clear()
    page.unmount_button.click()
    qtbot.waitUntil(lambda: page.state == "done", timeout=20000)


def test_the_record_can_be_saved_to_a_file_before_the_system_is_closed(qtbot, gentoo_machine, repair_kit, tmp_path, monkeypatch) -> None:
    daemon, _lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    system = open_system(qtbot, window)
    finish(qtbot, window)
    page = window.finish_page
    target = tmp_path / "record.txt"
    monkeypatch.setattr(page, "choose_report_path", lambda suggestion: str(target))
    assert system.audit_path in page.record_where.text().replace("​", "")
    page.save_button.click()
    qtbot.waitUntil(target.exists, timeout=20000)
    record = target.read_text()
    assert "session started" in record and "blockdev --setro /dev/nvme0n1p5" in record  # what the tool did, line by line
    qtbot.waitUntil(lambda: page.notice.isVisibleTo(page) and "was saved" in page.notice.text.text(), timeout=5000)


def test_a_second_system_can_be_repaired_after_the_first_was_closed(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    open_system(qtbot, window)
    finish(qtbot, window)
    window.finish_page.unmount_button.click()
    qtbot.waitUntil(lambda: window.finish_page.state == "done", timeout=20000)
    window.finish_page.again_button.click()
    assert window.stack.currentIndex() == FIND and window.system is None
    qtbot.waitUntil(lambda: len(window.find_page.candidates) == 2 and not window.find_page._busy, timeout=20000)
    open_system(qtbot, window, "/dev/sdb1")
    assert window.system.title.startswith("Debian")


# ---- read-only and read-write ---------------------------------------------------------------------------------------------------------
def test_repairs_can_be_switched_on_and_off_without_repairing_anything(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    open_system(qtbot, window)
    page = window.diagnose_page
    assert page.mode_label.text() == "READ-ONLY"
    request_action(qtbot, window, "session.enter_write")
    plan = window.plan_page
    assert plan.risk_label.text().startswith("High risk") and "read-only to read-write" in plan.prep_note.text() + plan.commands.title
    plan.reviewed.setChecked(True)
    plan.typed.setText("nvme0n1p5")
    plan.run_button.click()
    qtbot.waitUntil(lambda: plan.state == "result", timeout=30000)
    assert plan.tracker.outcome == "success" and "now open with permission to write" in plan.result_text.text()
    plan.done_button.click()
    qtbot.waitUntil(lambda: page.mode_label.text() == "REPAIRS ENABLED" and not page.busy.isVisibleTo(page), timeout=20000)
    assert not lab.system.chroot_calls or all(call[2][0] == "true" for call in lab.system.chroot_calls)  # nothing was repaired
    assert not page.findChild(object, "rescueAction:session.enter_write").isEnabled() and page.findChild(object, "rescueAction:session.leave_write").isEnabled()
    request_action(qtbot, window, "session.leave_write")
    assert not window.plan_page.reviewed.isVisibleTo(window.plan_page) and window.plan_page.run_button.isEnabled()  # a low-risk step: the click is the confirmation
    window.plan_page.run_button.click()
    qtbot.waitUntil(lambda: window.plan_page.state == "result", timeout=30000)
    window.plan_page.done_button.click()
    qtbot.waitUntil(lambda: page.mode_label.text() == "READ-ONLY" and not page.busy.isVisibleTo(page), timeout=20000)


def test_systems_that_an_earlier_window_left_open_are_offered_when_the_tool_starts(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, _lab, _tree = gentoo_machine
    first = repair_kit.open(daemon.path)
    system = open_system(qtbot, first)
    first.force_close()
    second = repair_kit.open(daemon.path)
    qtbot.waitUntil(lambda: second.find_page.open_card.isVisibleTo(second.find_page), timeout=20000)
    button = second.find_page.findChild(object, f"rescueResume:{system.session_id}")
    assert button is not None
    qtbot.waitUntil(lambda: not second.find_page._busy and bool(second.find_page.candidates), timeout=20000)
    button.click()
    qtbot.waitUntil(lambda: second.stack.currentIndex() == DIAGNOSE and second.system is not None and not second.diagnose_page.busy.isVisibleTo(second.diagnose_page), timeout=20000)
    assert second.system.session_id == system.session_id and second.system.actions
    finish(qtbot, second)
    second.finish_page.unmount_button.click()
    qtbot.waitUntil(lambda: second.finish_page.state == "done", timeout=20000)
    assert daemon.manager.session_summary("")["sessions"] == []
