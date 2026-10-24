"""Finding systems on different computers, and opening them: nothing found, the live stick, several systems, a disk the file manager opened, optional data."""

from __future__ import annotations

import json

import pytest

from gentoo_installer.rescue_app import DIAGNOSE, FIND
from gentoo_installer.rescue_ui import messages
from tests.rescue import fakeroot as fr
from tests.rescue import lsblk_fixtures as lf
from tests.rescue.flow import uefi_gentoo_lab
from tests.ui.rescue_fakes import (  # noqa: F401  (fixtures)
    daemon_factory,
    open_system,
    proxy_factory,
    repair_kit,
    rescue_lab_factory,
    system_card,
)


def plain(text: str) -> str:
    """The text as a person reads it (long words carry invisible break points)."""
    return text.replace("\u200b", "")


def scanned(qtbot, window, count: int | None = None) -> None:
    qtbot.waitUntil(lambda: bool(window.find_page.scan) and not window.find_page._busy and (count is None or len(window.find_page.candidates) == count), timeout=20000)


def test_a_computer_with_only_windows_says_why_nothing_was_found(qtbot, rescue_lab_factory, daemon_factory, repair_kit) -> None:
    lab = rescue_lab_factory(lf.windows_only())
    window = repair_kit.open(daemon_factory(lab).path)
    scanned(qtbot, window, 0)
    page = window.find_page
    assert not page.systems_card.isVisibleTo(page) and page.empty_card.isVisibleTo(page) and not page.continue_button.isEnabled()
    assert page.empty_card.findChild(object, "cardTitle").text() == messages.NOTHING_FOUND_TITLE
    assert "Only Windows-style volumes were found" in page.hints.text()
    assert page.disks.isVisibleTo(page) and "/dev/sda" in page.disks_text.text() and "ntfs" in page.disks_text.text()
    assert not lab.system.mounted_targets() and not lab.system.commands_named("mount")  # ntfs is listed, never opened


def test_the_live_stick_is_never_offered_and_the_page_says_it_is_hidden(qtbot, rescue_lab_factory, daemon_factory, repair_kit) -> None:
    lab = rescue_lab_factory(lf.usb_live_hybrid())
    lab.system.live_mounts.append(("/dev/sdb1", "/run/initramfs/live"))
    lab.add_tree("/dev/sda2", "gentoo", firmware="uefi")
    lab.add_tree("/dev/sdb1", "debian")  # even if the stick held a system it must not be listed
    window = repair_kit.open(daemon_factory(lab).path)
    scanned(qtbot, window, 1)
    page = window.find_page
    assert [item["device"] for item in page.candidates] == ["/dev/sda2"]
    assert page.live_note.isVisibleTo(page) and messages.LIVE_MEDIUM_NOTICE in page.live_note.text() and "/dev/sdb" in page.live_note.text()
    assert "Cruzer" not in page.disks_text.text()  # the stick is not in the list of disks either
    assert page.group.value() == page.candidates[0]["id"] and page.continue_button.isEnabled()  # the one system is chosen already


def test_several_systems_are_listed_and_a_disk_that_cannot_be_read_is_reported(qtbot, rescue_lab_factory, daemon_factory, repair_kit) -> None:
    lab = rescue_lab_factory(lf.multiboot_three())
    lab.add_tree("/dev/nvme0n1p2", "gentoo", firmware="uefi")
    lab.add_tree("/dev/nvme0n1p3", "debian", firmware="uefi")  # the btrfs partition has nothing attached: opening it fails
    window = repair_kit.open(daemon_factory(lab).path)
    scanned(qtbot, window, 2)
    page = window.find_page
    assert [page.group.cards[item["id"]].title_label.text() for item in sorted(page.candidates, key=lambda c: c["device"])][0] == "Gentoo Linux"
    assert page.group.value() == "" and not page.continue_button.isEnabled()  # with a choice, nothing is chosen for the person
    assert "/dev/nvme0n1p4" in page.hints.text()  # the disk that could not be read is mentioned, the scan went on
    page.group.set_value(system_card(window, "/dev/nvme0n1p3"))
    assert page.continue_button.isEnabled()


def test_scanning_again_keeps_the_choice_and_changes_nothing(qtbot, gentoo_machine_for_find, repair_kit) -> None:
    daemon, _lab = gentoo_machine_for_find
    window = repair_kit.open(daemon.path)
    scanned(qtbot, window, 2)
    page = window.find_page
    chosen = system_card(window, "/dev/sdb1")
    page.group.set_value(chosen)
    page.scan_button.click()
    assert not page.scan_button.isEnabled() and not page.continue_button.isEnabled()  # nothing to press while the disks are read
    scanned(qtbot, window, 2)
    assert page.group.value() == chosen and page.continue_button.isEnabled()


@pytest.fixture
def gentoo_machine_for_find(rescue_lab_factory, daemon_factory):
    lab, _tree = uefi_gentoo_lab(rescue_lab_factory)
    return daemon_factory(lab), lab


# ---- opening ----------------------------------------------------------------------------------------------------------------------
def test_a_disk_the_file_manager_opened_must_be_closed_first_and_the_plan_is_made_again(qtbot, rescue_lab_factory, daemon_factory, repair_kit, tmp_path) -> None:
    media = tmp_path / "media-root"
    state = {"mounted": True}

    def lsblk(_system) -> str:
        data = json.loads(lf.desktop_automount())
        data["blockdevices"][0]["children"][0]["mountpoints"] = [str(media)] if state["mounted"] else []
        data["blockdevices"][0]["children"][1]["fstype"] = "swap"  # the second partition is not a Linux system here
        return json.dumps(data)

    lab = rescue_lab_factory(lsblk)
    lab.add_tree("/dev/sda1", "gentoo", firmware="bios")
    fr.make_root(media, "gentoo")  # what the file manager shows
    window = repair_kit.open(daemon_factory(lab).path)
    scanned(qtbot, window, 1)
    page = window.find_page
    assert "is mounted at" in page.group.cards[page.candidates[0]["id"]].description_label.text()  # the card warns before the person tries
    page.group.set_value(page.candidates[0]["id"])
    page.continue_button.click()
    mount = window.mount_page
    qtbot.waitUntil(lambda: mount.again_button.isVisibleTo(mount), timeout=20000)  # after the look for a session of the tool's own
    assert str(media) in plain(mount.notice.text.text()) and "eject" in mount.notice.text.text() and mount.again_button.isVisibleTo(mount) and not mount.open_button.isEnabled()
    state["mounted"] = False  # the person ejects the disk in the file manager
    mount.again_button.click()
    qtbot.waitUntil(lambda: mount.open_button.isEnabled(), timeout=20000)
    assert not mount.notice.isVisibleTo(mount) and not mount.again_button.isVisibleTo(mount)


def test_an_optional_data_volume_is_opened_only_when_asked_for(qtbot, rescue_lab_factory, daemon_factory, repair_kit) -> None:
    """An old BIOS computer with SATA disks: the root and a separate /home."""
    lab = rescue_lab_factory(lf.plain_sata_mbr(), uefi=False)
    lab.add_tree("/dev/sda1", "gentoo", firmware="bios", fstab=fr.fstab_text(esp=None, home=fr.HOME_UUID))
    lab.add_tree("/dev/sda6", "debian")
    window = repair_kit.open(daemon_factory(lab).path)
    scanned(qtbot, window)
    page = window.find_page
    assert window.find_page.firmware.text().startswith("Started in legacy BIOS mode")
    root = next(item for item in page.candidates if item["device"] == "/dev/sda1")
    page.group.set_value(root["id"])
    page.continue_button.click()
    mount = window.mount_page
    qtbot.waitUntil(lambda: mount.open_button.isEnabled(), timeout=20000)
    box = mount.findChild(object, "rescueMountRow:/home")
    assert box is not None and not box.isChecked() and "Your data" in "".join(label.text() for label in mount.table_card.findChildren(type(mount.summary)))
    first_digest = mount.mount_plan["digest"]
    box.setChecked(True)  # asking for the data changes the plan, so a new dry run is made and the digest changes
    qtbot.waitUntil(lambda: mount.open_button.isEnabled() and mount.mount_plan["digest"] != first_digest, timeout=20000)
    assert mount.choice.include_tier4 == ["/home"] and mount.findChild(object, "rescueMountRow:/home").isChecked()
    mount.open_button.click()
    qtbot.waitUntil(lambda: window.stack.currentIndex() == DIAGNOSE, timeout=20000)
    mounts = {item["mountpoint"] for item in window.system.info["mounts"] if item["kind"] == "mount"}
    assert "/home" in mounts


def test_look_only_opens_a_system_that_cannot_be_changed(qtbot, rescue_lab_factory, daemon_factory, repair_kit) -> None:
    lab, _tree = uefi_gentoo_lab(rescue_lab_factory)
    daemon = daemon_factory(lab)
    window = repair_kit.open(daemon.path)
    scanned(qtbot, window, 2)
    window.find_page.group.set_value(system_card(window, "/dev/nvme0n1p5"))
    window.find_page.continue_button.click()
    mount = window.mount_page
    qtbot.waitUntil(lambda: mount.open_button.isEnabled(), timeout=20000)
    digest = mount.mount_plan["digest"]
    mount.inspect_only.setChecked(True)
    qtbot.waitUntil(lambda: mount.open_button.isEnabled() and mount.mount_plan["digest"] != digest, timeout=20000)
    assert mount.choice.allow_write is False
    mount.open_button.click()
    qtbot.waitUntil(lambda: window.stack.currentIndex() == DIAGNOSE, timeout=20000)
    page = window.diagnose_page
    assert page.mode_label.text() == "LOOK ONLY" and not window.system.allow_write
    assert "do not run" in page.terminal_text.text() and not page.writable_terminal_button.isVisibleTo(page)
    repairs = [page.findChild(object, f"rescueAction:{name}") for name in ("boot.bootloader", "boot.initramfs", "fstab.fix")]
    assert all(button is not None and not button.isEnabled() for button in repairs)
    assert "inspection only" in "".join(label.text() for label in page.findChildren(type(page.summary)))  # the reason is written next to the button
    assert not lab.system.chroot_calls


def test_a_missing_file_list_suggests_the_boot_partition_and_it_can_be_added(qtbot, rescue_lab_factory, daemon_factory, repair_kit) -> None:
    lab, _tree = uefi_gentoo_lab(rescue_lab_factory, fstab="")  # the system has an empty /etc/fstab
    window = repair_kit.open(daemon_factory(lab).path)
    scanned(qtbot, window, 2)
    window.find_page.group.set_value(system_card(window, "/dev/nvme0n1p5"))
    window.find_page.continue_button.click()
    mount = window.mount_page
    qtbot.waitUntil(lambda: mount.open_button.isEnabled(), timeout=20000)
    assert mount.suggest_card.isVisibleTo(mount)
    button = mount.findChild(object, "rescueSuggest:/efi")
    assert button is not None
    before = mount.mount_plan["digest"]
    button.click()
    qtbot.waitUntil(lambda: mount.open_button.isEnabled() and mount.mount_plan["digest"] != before, timeout=20000)
    assert not mount.suggest_card.isVisibleTo(mount) and mount.findChild(object, "rescueMountRow:/efi") is not None
    assert mount.choice.overrides == [{"mountpoint": "/efi", "device": "/dev/nvme0n1p1"}]
    mount.open_button.click()
    qtbot.waitUntil(lambda: window.stack.currentIndex() == DIAGNOSE, timeout=20000)
    # the folder does not exist in the read-only system, so it is not opened yet: the diagnosis says so
    assert "/efi" not in {item["mountpoint"] for item in window.system.info["mounts"] if item["kind"] == "mount"}
    assert any(f["id"] == "M01" and "/efi was not mounted" in f["title"] for f in window.system.findings)


def test_back_from_the_mount_page_returns_to_the_list_without_opening_anything(qtbot, gentoo_machine_for_find, repair_kit) -> None:
    daemon, lab = gentoo_machine_for_find
    window = repair_kit.open(daemon.path)
    scanned(qtbot, window, 2)
    window.find_page.group.set_value(system_card(window, "/dev/nvme0n1p5"))
    window.find_page.continue_button.click()
    qtbot.waitUntil(lambda: window.mount_page.open_button.isEnabled(), timeout=20000)
    window.mount_page.back_button.click()
    assert window.stack.currentIndex() == FIND and lab.system.mounted_targets() == []
