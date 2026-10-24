"""What a plan demands before it runs: only a click, a review, the root device's name, the backup phrase, a new password typed twice."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel

from gentoo_installer.rescue_ui import messages
from tests.ui.rescue_fakes import (  # noqa: F401  (fixtures)
    daemon_factory,
    gentoo_machine,
    open_system,
    repair_kit,
    request_action,
    rescue_lab_factory,
)

BACKUP = "I HAVE A BACKUP"


def finish_and_unmount(qtbot, window) -> None:
    window.diagnose_page.finish_button.click()
    window.finish_page.unmount_button.click()
    qtbot.waitUntil(lambda: window.finish_page.state == "done", timeout=20000)


def test_a_high_risk_plan_needs_the_review_tick_and_the_name_of_the_root_device(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    open_system(qtbot, window)
    request_action(qtbot, window, "boot.bootloader")
    page = window.plan_page
    assert page.risk_label.text().startswith("High risk") and "nvme0n1p5" in page.typed_label.text()
    assert page.reviewed.isVisibleTo(page) and page.typed.isVisibleTo(page) and not page.phrase.isVisibleTo(page)
    assert page.confirmation.typed == "nvme0n1p5" and not page.run_button.isEnabled()
    page.reviewed.setChecked(True)
    assert not page.run_button.isEnabled()  # the tick alone is not enough
    page.typed.setText("nvme0n1")
    assert not page.run_button.isEnabled()  # nor a different disk
    page.typed.setText("nvme0n1p5")
    assert page.run_button.isEnabled()
    page.reviewed.setChecked(False)
    assert not page.run_button.isEnabled()  # taking the tick back takes the permission back
    page.reviewed.setChecked(True)
    assert not lab.system.chroot_calls  # nothing ran while the confirmation was incomplete
    QTest.keyClick(page.typed, Qt.Key.Key_Return)  # Return in the field presses the button once everything is ready
    qtbot.waitUntil(lambda: page.state == "result", timeout=30000)
    assert page.tracker.outcome in {"success", "warning"} and lab.system.chroot_calls
    assert page.typed.text() == "nvme0n1p5"  # the request carried exactly what the plan demanded


def test_a_dangerous_plan_also_needs_the_backup_phrase_word_for_word(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    open_system(qtbot, window)
    request_action(qtbot, window, "fs.check", "options")  # the choice that matters is made on purpose
    page = window.plan_page
    mode = page._form.fields["mode"]
    assert mode.currentData() == "check"  # the harmless choice is the default
    mode.setCurrentIndex(mode.findData("repair_force"))
    page.continue_button.click()
    qtbot.waitUntil(lambda: page.state == "review", timeout=20000)
    assert page.risk_label.text().startswith("Dangerous") and page.phrase.isVisibleTo(page) and BACKUP in page.phrase_label.text()
    page.reviewed.setChecked(True)
    page.typed.setText("nvme0n1p5")
    assert not page.run_button.isEnabled()
    page.phrase.setText("i have a backup")
    assert not page.run_button.isEnabled()  # the words are checked as typed
    page.phrase.setText(BACKUP)
    assert page.run_button.isEnabled()
    assert page.confirmation.phrase == BACKUP
    assert not [c for c in lab.system.commands if c and c[0] == "e2fsck"]  # nothing ran yet


def test_a_check_that_changes_nothing_is_only_a_review_away(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, _lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    open_system(qtbot, window)
    request_action(qtbot, window, "fs.check", "options")
    page = window.plan_page
    page.continue_button.click()
    qtbot.waitUntil(lambda: page.state == "review", timeout=20000)
    assert page.risk_label.text().startswith("Medium risk") and not page.typed.isVisibleTo(page) and not page.phrase.isVisibleTo(page)
    page.reviewed.setChecked(True)
    assert page.run_button.isEnabled()


def test_a_low_risk_plan_needs_only_the_click_and_the_entry_is_picked_from_the_firmware_list(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, lab, _tree = gentoo_machine
    lab.system.nvram["entries"]["0001"] = {"label": "Gentoo", "loader": "\\EFI\\Gentoo\\grubx64.efi", "active": True, "disk": "/dev/nvme0n1", "part": "1"}
    lab.system.nvram["entries"]["0003"] = {"label": "Windows Boot Manager", "loader": "\\EFI\\Microsoft\\Boot\\bootmgfw.efi", "active": True, "disk": "/dev/nvme0n1", "part": "1"}
    lab.system.nvram["order"] = ["0003", "0001"]
    window = repair_kit.open(daemon.path)
    open_system(qtbot, window)
    request_action(qtbot, window, "efi.next", "options")
    page = window.plan_page
    entries = page._form.fields["bootnum"]
    qtbot.waitUntil(lambda: entries.count() == 2, timeout=20000)
    assert [entries.itemData(index) for index in range(2)] == ["0001", "0003"] and "Gentoo" in entries.itemText(0)
    page.continue_button.click()
    assert page.state in {"planning", "review"}
    qtbot.waitUntil(lambda: page.state == "review", timeout=20000)
    assert page.risk_label.text().startswith("Low risk")
    assert not page.reviewed.isVisibleTo(page) and not page.typed.isVisibleTo(page) and page.run_button.isEnabled()
    page.run_button.click()
    qtbot.waitUntil(lambda: page.state == "result", timeout=30000)
    assert page.tracker.outcome in {"success", "warning"} and lab.system.nvram["next"] == "0001"


def test_a_required_value_must_be_given_before_the_plan_is_made(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, _lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    open_system(qtbot, window)
    request_action(qtbot, window, "acct.unlock", "options")
    page = window.plan_page
    qtbot.waitUntil(lambda: page._form.fields["username"].count() > 0, timeout=20000)
    page._form.fields["username"].setEditText("")
    page.continue_button.click()
    assert page.state == "options" and "Please fill in" in page.notice.text.text()
    page._form.fields["username"].setEditText("user")
    page.continue_button.click()
    qtbot.waitUntil(lambda: page.state == "review", timeout=20000)
    assert page.plan["params"] == {"username": "user"}


def test_a_new_password_is_typed_twice_is_sent_once_and_is_not_kept(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, lab, tree = gentoo_machine
    secret = "s3cret! pass:word"
    window = repair_kit.open(daemon.path)
    open_system(qtbot, window)
    request_action(qtbot, window, "acct.password", "options")
    page = window.plan_page
    users = page._form.fields["username"]
    qtbot.waitUntil(lambda: users.count() >= 2, timeout=20000)
    assert {users.itemData(index) for index in range(users.count())} == {"root", "user"}  # the people who can log in
    users.setCurrentIndex(users.findData("user"))
    page.continue_button.click()
    qtbot.waitUntil(lambda: page.state == "review", timeout=20000)
    assert page.secrets_card.isVisibleTo(page) and "not stored or logged" in page.secrets_card.findChild(QLabel).text() + page.secrets_card.findChildren(QLabel)[1].text()
    first, second = page._secret_fields["password"]
    page.reviewed.setChecked(True)
    page.typed.setText("nvme0n1p5")
    assert not page.run_button.isEnabled()  # no password yet
    first.edit.setText(secret)
    second.edit.setText(secret + "x")
    assert not page.run_button.isEnabled()  # the two entries differ
    second.edit.setText(secret)
    assert page.run_button.isEnabled()
    assert first.edit.echoMode().name == "Password" and second.edit.echoMode().name == "Password"
    page.run_button.click()
    assert first.text() == "" and second.text() == ""  # gone from the fields as soon as it was sent
    qtbot.waitUntil(lambda: page.state == "result", timeout=30000)
    assert page.tracker.outcome in {"success", "warning"}
    seen = "\n".join([page.log.toPlainText(), page.result_text.text(), page.commands_text.toPlainText(), *[label.text() for label in window.findChildren(QLabel)]])
    assert secret not in seen and "s3cret" not in seen
    finish_and_unmount(qtbot, window)
    assert "$6$rescued" in (tree / "etc/shadow").read_text()  # the password reached the system


@pytest.mark.parametrize("bad", ["two\nlines", "carriage\rreturn"])
def test_a_password_with_a_line_break_is_not_accepted_by_the_window(qtbot, gentoo_machine, repair_kit, bad) -> None:
    daemon, lab, _tree = gentoo_machine
    window = repair_kit.open(daemon.path)
    open_system(qtbot, window)
    request_action(qtbot, window, "acct.password", "options")
    page = window.plan_page
    qtbot.waitUntil(lambda: page._form.fields["username"].count() > 0, timeout=20000)
    page.continue_button.click()
    qtbot.waitUntil(lambda: page.state == "review", timeout=20000)
    first, second = page._secret_fields["password"]
    page.reviewed.setChecked(True)
    page.typed.setText("nvme0n1p5")
    first.edit.setText(bad)
    second.edit.setText(bad)
    assert not page.run_button.isEnabled()
    assert not [call for call in lab.system.calls if call.secret]
