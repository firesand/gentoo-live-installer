"""An encrypted laptop: unlock with a wrong and then the right passphrase, activate LVM, repair, and release everything again."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QLineEdit

from gentoo_installer.rescue_app import DIAGNOSE, FINISH, MOUNT
from gentoo_installer.rescue_ui import find_page, messages
from tests.rescue.test_flow_encrypted import MAPPER, PASSPHRASE, encrypted_lab
from tests.ui.rescue_fakes import (  # noqa: F401  (fixtures)
    daemon_factory,
    proxy_factory,
    repair_kit,
    rescue_lab_factory,
)

DEVICE = "/dev/nvme0n1p3"


@pytest.fixture
def laptop(rescue_lab_factory, daemon_factory):
    lab, tree, boot = encrypted_lab(rescue_lab_factory, initramfs=False)
    return daemon_factory(lab), lab, tree, boot


def unlock_button(window):
    return window.find_page.findChild(object, f"rescueUnlock:{DEVICE}")


def type_passphrase(panel, text: str) -> None:
    panel.passphrase.edit.setText(text)
    panel.go_button.click()


def test_a_locked_disk_is_listed_with_what_to_do_and_nothing_is_guessed(qtbot, laptop, repair_kit) -> None:
    daemon, lab, _tree, _boot = laptop
    window = repair_kit.open(daemon.path)
    qtbot.waitUntil(lambda: unlock_button(window) is not None, timeout=20000)
    page = window.find_page
    assert not page.systems_card.isVisibleTo(page) and page.empty_card.isVisibleTo(page) and page.locked_card.isVisibleTo(page)
    assert page.empty_title.text() == messages.NOTHING_YET_TITLE and "Unlock or activate them first" in page.empty_text.text()  # not the list of common reasons
    assert not page.hints.isVisibleTo(page)  # the card above already says it
    assert not page.continue_button.isEnabled() and not page.unlock_panel.isVisibleTo(page)
    assert lab.system.mapped == {}  # looking at the disks opened nothing


def test_a_wrong_passphrase_shows_the_engines_keyboard_hint_and_the_right_one_opens_the_disk(qtbot, laptop, repair_kit) -> None:
    daemon, lab, _tree, _boot = laptop
    window = repair_kit.open(daemon.path)
    qtbot.waitUntil(lambda: unlock_button(window) is not None, timeout=20000)
    page = window.find_page
    panel = page.unlock_panel
    unlock_button(window).click()
    assert panel.isVisibleTo(page) and panel.device == DEVICE and "Unlock /dev/nvme0n1p3" in panel.title.text()
    # the passphrase is masked, can be shown, and the layout in use is named
    assert panel.passphrase.edit.echoMode() == QLineEdit.EchoMode.Password
    panel.passphrase.toggle.setChecked(True)
    assert panel.passphrase.edit.echoMode() == QLineEdit.EchoMode.Normal
    panel.passphrase.toggle.setChecked(False)
    assert "English (US)" in panel.layout_label.text() and not panel.keyboard_section.is_open
    assert not panel.go_button.isEnabled()

    type_passphrase(panel, "wrong-one")
    assert not panel.passphrase.isEnabled()  # busy while the disk is tried
    qtbot.waitUntil(lambda: panel.status.isVisibleTo(panel), timeout=20000)
    assert "keyboard layout" in panel.status.text.text() and panel.isVisibleTo(page) and panel.failures == 1 and panel.passphrase.text() == "wrong-one"
    assert lab.system.mapped == {} and not panel.keyboard_section.is_open
    type_passphrase(panel, "wrong-two")
    qtbot.waitUntil(lambda: panel.failures == 2, timeout=20000)
    assert panel.keyboard_section.is_open  # after the second failure the layout chooser opens by itself

    type_passphrase(panel, PASSPHRASE)
    qtbot.waitUntil(lambda: not panel.isVisibleTo(page), timeout=20000)
    assert panel.passphrase.text() == "" and lab.system.mapped == {MAPPER: DEVICE}
    # the volume group inside is not active yet: the page offers to activate it
    activate = lambda: page.findChild(object, "rescueActivate:vg0")  # noqa: E731
    qtbot.waitUntil(lambda: activate() is not None and not page._busy, timeout=20000)
    assert page.storage_card.isVisibleTo(page) and window.storage_opened


def test_the_keyboard_layout_can_be_changed_while_typing_the_passphrase(qtbot, laptop, repair_kit, monkeypatch) -> None:
    daemon, _lab, _tree, _boot = laptop
    monkeypatch.setattr(find_page, "read_layout", lambda: ("de", "nodeadkeys"))
    window = repair_kit.open(daemon.path)
    qtbot.waitUntil(lambda: unlock_button(window) is not None, timeout=20000)
    panel = window.find_page.unlock_panel
    unlock_button(window).click()
    assert "German" in panel.layout_label.text() and not panel.keyboard.summary.isVisibleTo(panel)
    panel.keyboard_section.button.setChecked(True)
    assert panel.keyboard.isVisibleTo(panel) and panel.keyboard.value() == ("de", "nodeadkeys")
    index = panel.keyboard.layout_box.findData("fr")
    assert index >= 0
    panel.keyboard.layout_box.setCurrentIndex(index)
    panel.keyboard._layout_activated(index)  # what choosing an entry does
    assert repair_kit.keyboard_calls and repair_kit.keyboard_calls[-1][0] == "setxkbmap" and "fr" in repair_kit.keyboard_calls[-1][1]
    assert "French" in panel.layout_label.text()


def test_characters_a_us_boot_prompt_cannot_type_are_pointed_out(qtbot, laptop, repair_kit) -> None:
    daemon, _lab, _tree, _boot = laptop
    window = repair_kit.open(daemon.path)
    qtbot.waitUntil(lambda: unlock_button(window) is not None, timeout=20000)
    panel = window.find_page.unlock_panel
    unlock_button(window).click()
    panel.passphrase.edit.setText("plain ascii")
    assert not panel.typing_note.text()
    panel.passphrase.edit.setText("pässwörd")
    assert "not on a US keyboard" in panel.typing_note.text()


def test_cancel_closes_the_panel_and_forgets_the_passphrase(qtbot, laptop, repair_kit) -> None:
    daemon, lab, _tree, _boot = laptop
    window = repair_kit.open(daemon.path)
    qtbot.waitUntil(lambda: unlock_button(window) is not None, timeout=20000)
    panel = window.find_page.unlock_panel
    unlock_button(window).click()
    panel.passphrase.edit.setText("half typed")
    panel.cancel_button.click()
    assert not panel.isVisibleTo(window.find_page) and panel.passphrase.text() == ""
    assert lab.system.mapped == {}


def test_from_a_locked_disk_to_a_repaired_system_and_back_to_a_locked_disk(qtbot, laptop, repair_kit) -> None:
    daemon, lab, tree, boot = laptop
    window = repair_kit.open(daemon.path)
    qtbot.waitUntil(lambda: unlock_button(window) is not None, timeout=20000)
    page = window.find_page
    unlock_button(window).click()
    type_passphrase(page.unlock_panel, PASSPHRASE)
    qtbot.waitUntil(lambda: page.findChild(object, "rescueActivate:vg0") is not None and not page._busy, timeout=20000)

    # activating a volume group names its side effect first, word for word
    page.findChild(object, "rescueActivate:vg0").click()
    assert page.confirm.isVisibleTo(page) and page.confirm.body.text() == messages.LVM_ACTIVATION_NOTICE
    page.confirm.confirm_button.click()
    qtbot.waitUntil(lambda: len(page.candidates) == 1 and not page._busy, timeout=30000)
    row = page.group.cards[page.candidates[0]["id"]]
    assert row.title_label.text() == "Gentoo Linux" and "Encrypted (LUKS)" in row.description_label.text() and "LVM volume" in row.description_label.text()
    assert page.group.value() == page.candidates[0]["id"]  # the only system is chosen already
    page.continue_button.click()
    assert window.stack.currentIndex() == MOUNT
    qtbot.waitUntil(lambda: window.mount_page.open_button.isEnabled(), timeout=20000)
    window.mount_page.open_button.click()
    qtbot.waitUntil(lambda: window.stack.currentIndex() == DIAGNOSE, timeout=20000)
    assert {f["id"] for f in window.system.findings if f["severity"] == "error"} == {"D02"}  # the kernel in the separate /boot has no initramfs

    window.diagnose_page.recommended_button.click()
    qtbot.waitUntil(lambda: window.plan_page.state == "review", timeout=20000)
    window.plan_page.reviewed.setChecked(True)
    window.plan_page.run_button.click()
    qtbot.waitUntil(lambda: window.plan_page.state == "result", timeout=30000)
    assert window.plan_page.tracker.outcome == "success"
    window.plan_page.done_button.click()
    qtbot.waitUntil(lambda: window.diagnose_page.mode_label.text() == "REPAIRS ENABLED" and not window.diagnose_page.busy.isVisibleTo(window.diagnose_page), timeout=20000)
    assert not any(f["severity"] == "error" for f in window.system.findings)

    window.diagnose_page.finish_button.click()
    assert window.stack.currentIndex() == FINISH
    window.finish_page.unmount_button.click()
    qtbot.waitUntil(lambda: window.finish_page.state == "done", timeout=20000)
    assert lab.system.mapped == {} and lab.system.vgs["vg0"].active is False and lab.system.mounted_targets() == []  # back to a locked disk
    assert (boot / "initramfs-6.18.57-gentoo-dist-bin.img").stat().st_size > 1 << 20
    assert not window.storage_opened
