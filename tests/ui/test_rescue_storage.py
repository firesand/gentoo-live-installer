"""RAID arrays that are not assembled yet: the tool names what it does, asks before it starts a degraded array, and finds the system inside."""

from __future__ import annotations

from gentoo_installer.rescue_ui import messages
from tests.rescue import lsblk_fixtures as lf
from tests.rescue.fakes import MdSpec
from tests.ui.rescue_fakes import (  # noqa: F401  (fixtures)
    daemon_factory,
    repair_kit,
    rescue_lab_factory,
)

UUID = "aaaa1111bbbb2222cccc333344445555"  # as the scan names the array: hex digits only


def raid_lab(factory, **spec):
    lab = factory(lambda s: lf.md_raid1_under_lvm() if s.md["x"].assembled else lf.md_raid1_unassembled())
    lab.system.md["x"] = MdSpec("aaaa1111:bbbb2222:cccc3333:44445555", ["/dev/sdb1", "/dev/sdc1"], name="host:data", device="/dev/md127", **spec)
    lab.add_tree("/dev/mapper/vgdata-data", "debian")
    return lab


def test_an_array_that_is_not_assembled_is_assembled_on_request_and_the_system_inside_is_found(qtbot, rescue_lab_factory, daemon_factory, repair_kit) -> None:
    lab = raid_lab(rescue_lab_factory)
    window = repair_kit.open(daemon_factory(lab).path)
    page = window.find_page
    qtbot.waitUntil(lambda: page.findChild(object, f"rescueAssemble:{UUID}") is not None, timeout=20000)
    assert page.storage_card.isVisibleTo(page) and "RAID array" in page.storage_rows.itemAt(0).layout().itemAt(0).widget().text()
    assert not page.candidates and page.empty_title.text() == messages.NOTHING_YET_TITLE
    page.findChild(object, f"rescueAssemble:{UUID}").click()
    assert page.confirm.isVisibleTo(page) and not page.confirm.check.isVisibleTo(page.confirm)  # a complete array needs no extra warning
    assert not lab.system.commands_with("mdadm", "--assemble")  # nothing happens before the answer
    page.confirm.confirm_button.click()
    qtbot.waitUntil(lambda: len(page.candidates) == 1 and not page._busy, timeout=30000)
    assert page.candidates[0]["os"]["id"] == "debian" and "RAID array" in page.group.cards[page.candidates[0]["id"]].description_label.text()
    assert window.storage_opened and lab.system.commands_with("mdadm", "--assemble")[0][:4] == ("mdadm", "--assemble", "--scan", "--readonly")  # assembled read-only


def test_an_array_with_a_missing_disk_is_only_started_after_a_deliberate_tick(qtbot, rescue_lab_factory, daemon_factory, repair_kit) -> None:
    lab = raid_lab(rescue_lab_factory, devices=3)  # three disks expected, two present
    window = repair_kit.open(daemon_factory(lab).path)
    page = window.find_page
    qtbot.waitUntil(lambda: page.findChild(object, f"rescueAssemble:{UUID}") is not None, timeout=20000)
    page.findChild(object, f"rescueAssemble:{UUID}").click()
    assert page.confirm.check.isVisibleTo(page.confirm) and "disk is missing" in page.confirm.check.text()
    assert not page.confirm.confirm_button.isEnabled()
    page.confirm.check.setChecked(True)
    assert page.confirm.confirm_button.isEnabled()
    page.confirm.confirm_button.click()
    qtbot.waitUntil(lambda: len(page.candidates) == 1 and not page._busy, timeout=30000)
    assert lab.system.commands_with("mdadm", "--assemble")
