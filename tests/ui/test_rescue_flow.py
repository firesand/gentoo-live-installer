"""The repair window from the first scan to the restart, against the real daemon over a fake computer."""

from __future__ import annotations

from gentoo_installer.rescue_app import DIAGNOSE, FIND, FINISH, MOUNT, PLAN
from tests.ui.rescue_fakes import (  # noqa: F401  (fixtures)
    daemon_factory,
    gentoo_machine,
    proxy_factory,
    repair_kit,
    rescue_lab_factory,
    system_card,
)


def current(window) -> int:
    return window.stack.currentIndex()


def test_a_gentoo_without_a_boot_menu_is_found_opened_diagnosed_repaired_and_closed(qtbot, gentoo_machine, repair_kit) -> None:
    daemon, lab, tree = gentoo_machine
    (tree / "boot/grub/grub.cfg").unlink()
    window = repair_kit.open(daemon.path)

    # 1. the scan starts by itself and lists the Gentoo with its disk; the Debian on the other disk is named, but it is not a choice
    qtbot.waitUntil(lambda: len(window.find_page.candidates) == 1 and bool(window.find_page.others), timeout=20000)
    assert current(window) == FIND
    cards = window.find_page.group.cards
    gentoo = system_card(window, "/dev/nvme0n1p5")
    assert list(cards) == [gentoo] and window.find_page.continue_button.isEnabled()  # the only Gentoo is chosen already
    text = cards[gentoo].description_label.text()
    assert "Samsung SSD 990 PRO 1TB" in text and "ext4" in text and "Not encrypted" in text and "1 kernel" in text
    debian = window.find_page.findChild(object, "rescueOther:/dev/sdb1")
    assert debian is not None and debian.text().startswith("Debian") and "Not a Gentoo system" in " ".join(label.text() for label in window.find_page.others_card.findChildren(type(debian)))
    window.find_page.group.set_value(gentoo)
    assert window.find_page.continue_button.isEnabled()
    window.find_page.continue_button.click()

    # 2. the mount plan is shown (a dry run) before anything is opened
    assert current(window) == MOUNT
    qtbot.waitUntil(lambda: window.mount_page.open_button.isEnabled(), timeout=20000)
    assert window.mount_page.table_card.isVisibleTo(window.mount_page) and window.mount_page.findChild(object, "rescueMountRow:/efi") is not None
    assert lab.system.mounted_targets() == []  # a dry run opens nothing
    digest = window.mount_page.mount_plan["digest"]
    window.mount_page.open_button.click()

    # 3. the diagnosis: the missing boot menu is the problem, and the recommended repair comes first
    qtbot.waitUntil(lambda: current(window) == DIAGNOSE, timeout=20000)
    page = window.diagnose_page
    assert window.system is not None and window.system.mode == "ro" and page.mode_label.text() == "READ-ONLY"
    assert any(f["id"] == "D05" for f in window.system.findings) and page.recommended_card.isVisibleTo(page)
    assert "Regenerate the boot menu" in page.recommended_text.text()
    assert lab.system.mounted_targets() and daemon.manager.session(window.system.session_id).plan.digest == digest

    # 4. the plan: exact commands, nothing runs before the review box is ticked
    page.recommended_button.click()
    qtbot.waitUntil(lambda: current(window) == PLAN and window.plan_page.state == "review", timeout=20000)
    plan_page = window.plan_page
    assert "grub-mkconfig" in plan_page.commands_text.toPlainText() and plan_page.risk_label.text().startswith("Medium risk")
    assert not plan_page.run_button.isEnabled() and plan_page.reviewed.isVisibleTo(plan_page) and not plan_page.typed.isVisibleTo(plan_page)
    assert not lab.system.chroot_calls
    plan_page.reviewed.setChecked(True)
    assert plan_page.run_button.isEnabled()
    plan_page.run_button.click()

    # 5. the run and its result
    qtbot.waitUntil(lambda: plan_page.state == "result", timeout=30000)
    assert plan_page.tracker is not None and plan_page.tracker.outcome == "success"
    assert "The repair finished." == plan_page.result_title.text() and "/boot/grub/grub.cfg is" in plan_page.result_text.text()
    assert "Generating grub configuration file" in plan_page.log.toPlainText()
    assert lab.system.chroot_calls[-1][2] == ("grub-mkconfig", "-o", "/boot/grub/grub.cfg")
    plan_page.done_button.click()

    # 6. back on the diagnosis with the new state: repairs are enabled and the problem is gone
    assert current(window) == DIAGNOSE
    qtbot.waitUntil(lambda: page.mode_label.text() == "REPAIRS ENABLED" and not page.busy.isVisibleTo(page), timeout=20000)
    assert window.system is not None and not any(f["severity"] == "error" for f in window.system.findings) and not page.recommended_card.isVisibleTo(page)

    # 7. finish: everything is released and the repair reached the disk
    page.finish_button.click()
    assert current(window) == FINISH
    finish = window.finish_page
    assert "Regenerate the boot menu" in finish.did.text()
    finish.unmount_button.click()
    qtbot.waitUntil(lambda: finish.state == "done", timeout=20000)
    assert lab.system.mounted_targets() == [] and daemon.manager.session_summary("")["sessions"] == []
    assert (tree / "boot/grub/grub.cfg").read_text().count("menuentry") == 1
    assert "/var/log/gentoo-rescue/" in finish.where_report.text()
    finish.reboot_button.click()
    assert repair_kit.launched == [("loginctl", ["reboot"])]
