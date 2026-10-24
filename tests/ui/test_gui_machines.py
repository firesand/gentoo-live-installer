"""The whole wizard on the recorded machines: a desktop with six disks, two laptops, an old BIOS PC, two virtual machines
and one whose disks are hidden by the firmware."""

from __future__ import annotations

import copy
import dataclasses

import pytest

from gentoo_installer.manifest import Hardware
from gentoo_installer.system_info import DiskInfo, SystemInfo, mark_shared_identities
from tests.hardware.helpers import FIXTURE_NAMES, fixture_dict

GIB = 1024**3


def machine(name: str) -> SystemInfo:
    """What the window knows about a recorded machine: its hardware profile, its findings and its disks."""
    hardware = fixture_dict(name)
    disks = [
        DiskInfo(
            item["path"], item["model"] or "Block device", item["size"], serial=f"SER-{item['name']}" if item["serial"] else "", by_path=item["by_path"],
            removable=bool(item["removable"] or item["hotplug"]), live_medium=bool(item["live_medium"]), read_only=bool(item["ro"]), transport=item["tran"],
            pttype=item["pttype"],
        )
        for item in hardware["disks"]
    ]
    mark_shared_identities(disks)
    memory_gib = hardware["memory"]["total_kib"] / 1024 / 1024
    return SystemInfo(
        architecture="x86_64", memory_gib=memory_gib, uefi=hardware["firmware"]["mode"] == "uefi", disks=disks, network=True, synchronized_clock=True,
        missing_tools=[], hardware=hardware, warnings=copy.deepcopy(hardware["warnings"]))


def walk(window, disk):
    """Choose the disk like a user, fill in the accounts and press Continue to the review."""
    window.readiness.continue_anyway.setChecked(True)
    window.storage.disk_group.set_value(disk.path)
    if not disk.has_identity:
        window.storage.approve_path.setChecked(True)
    window.accounts.password.setText("a-long-password")
    window.accounts.password_confirm.setText("a-long-password")
    window.accounts.root_password.setText("another-long-password")
    window.accounts.root_confirm.setText("another-long-password")
    window.accounts.save_manifest()
    window._show_page(0)
    for expected in range(1, 7):
        window.next.click()
        assert window.current == expected, (expected, window.current)


@pytest.mark.parametrize("name", [name for name in FIXTURE_NAMES if name != "intel-rst-hidden-nvme"])
def test_the_wizard_reaches_the_review_on_every_machine(make_window, name):
    info = machine(name)
    window = make_window(info=info)
    assert not window.readiness.blocking  # nothing stops the wizard on these machines
    disk = next(item for item in info.disks if item.selectable)
    walk(window, disk)
    assert window.manifest.validate() == []
    assert window.manifest.hardware == Hardware()  # a visit changes none of the hardware choices
    review = window.review
    review.confirmation.setText(disk.path)
    assert review.validate_page() == (True, "")
    assert "CREATE" in review.storage_plan.text() and disk.path in review.storage_plan.text()
    assert window.manifest.storage.firmware == ("uefi" if info.uefi else "bios")


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_the_hardware_tab_works_in_both_views_on_every_machine(make_window, name):
    window = make_window(info=machine(name))
    window._show_page(4)
    window.customize.show_hardware()
    tab = window.customize.hardware
    for index in (1, 0):
        window.mode.setCurrentIndex(index)
        assert tab.summary.text() and tab.plan_card.isVisibleTo(tab)
        assert tab.values() == dataclasses.asdict(Hardware())  # nothing was touched
        assert tab.problems() == []


def test_the_machine_whose_disks_are_hidden_by_the_firmware_is_stopped_with_an_explanation(make_window):
    info = machine("intel-rst-hidden-nvme")
    window = make_window(info=info)
    valid, message = window.readiness.validate_page()
    assert not valid and "SATA Operation to AHCI" in message  # the dialog says what to change, not just that no disk was found
    assert window.readiness.rows["disk"].status == "error" and "AHCI" in window.readiness.rows["disk"].detail.text()
    texts = " ".join(window.readiness.notes_box.itemAt(i).widget().detail.text() for i in range(window.readiness.notes_box.count()))
    assert "SATA Operation to AHCI" in texts  # the finding of the probe says what to change in the firmware settings
    assert "Windows" in texts


def test_the_old_bios_computer_is_installed_for_legacy_boot(make_window):
    info = machine("old-bios-sata-radeon")
    window = make_window(info=info)
    assert window.manifest.storage.firmware == "bios"
    window._show_page(3)
    assert not window.system.bootloader.cards["systemd-boot"].isEnabled() and not window.system.secure_boot.isEnabled()
    window._show_page(4)
    tab = window.customize.hardware
    assert not tab.gpu_driver.cards["nvidia"].isVisibleTo(tab) and "AMD Radeon" in tab.summary.text()
    assert "open AMD driver" in tab.plan_text.text()
    assert not tab.row_shown("removable_fallback")  # no EFI boot partition on this machine


def test_the_virtual_machines_offer_guest_tools_and_skip_microcode(make_window):
    for name, hypervisor in (("qemu-q35-virtio", "qemu"), ("vmware-guest", "vmware")):
        window = make_window(info=machine(name))
        assert window.manifest.options.microcode is False and window.manifest.options.bluetooth is False
        window._show_page(4)
        tab = window.customize.hardware
        assert tab.row_shown("guest_tools") and hypervisor in tab.summary.text()
        assert "guest tools" in tab.plan_text.text().lower()


def test_what_the_detection_knows_about_the_disk_stops_the_storage_page(make_window):
    info = machine("old-bios-sata-radeon")
    info.hardware["disks"][0]["log_sec"] = 4096  # a disk with 4096-byte sectors: legacy BIOS cannot boot it
    window = make_window(info=info)
    disk = next(item for item in info.disks if item.selectable)
    window.storage.disk_group.set_value(disk.path)
    window.storage.approve_path.setChecked(not disk.has_identity)
    valid, message = window.storage.validate_page()
    assert not valid and "4096-byte sectors" in message
    # the same disk is fine for a UEFI install on a machine that has UEFI
    other = machine("asus-desktop-rtx4090")
    other.hardware["disks"][0]["log_sec"] = 4096
    window = make_window(info=other)
    window.storage.disk_group.set_value(next(item for item in other.disks if item.selectable).path)
    assert window.storage.validate_page() == (True, "")
