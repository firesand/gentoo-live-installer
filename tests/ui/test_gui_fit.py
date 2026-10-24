"""Small screens: every page fits the width of a 800x560, 1024x600 and 1366x768 window, so no button or field is cut off."""

from __future__ import annotations

import copy

import pytest
from PySide6.QtWidgets import QApplication, QFormLayout, QPushButton

from gentoo_installer.manifest import InstallManifest, Partition
from gentoo_installer.system_info import DiskInfo, PartitionInfo, SystemInfo
from gentoo_installer.widgets import FlowLayout
from tests.hardware.helpers import fixture_dict

GIB = 1024**3
SIZES = [(800, 560), (1024, 600), (1366, 768)]
ESP = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"


def laptop_info(name: str) -> SystemInfo:
    hardware = fixture_dict(name)
    disk = DiskInfo("/dev/nvme0n1", "Samsung SSD 870 EVO", 500 * GIB, serial="S5Y1NX0T123456", pttype="gpt", transport="nvme", partitions=[
        PartitionInfo("/dev/nvme0n1p1", 260 * 1024**2, "vfat", label="SYSTEM", parttype=ESP, parent="/dev/nvme0n1"),
        PartitionInfo("/dev/nvme0n1p2", 450 * GIB, "ntfs", label="Windows", parent="/dev/nvme0n1"),
        PartitionInfo("/dev/nvme0n1p3", 40 * GIB, "ext4", label="old-gentoo", parent="/dev/nvme0n1")])
    other = DiskInfo("/dev/sdb", "Data disk with a rather long model name", 2000 * GIB, serial="D1", partitions=[PartitionInfo("/dev/sdb1", 1900 * GIB, "ext4", parent="/dev/sdb")])
    return SystemInfo("x86_64", 16, True, [disk, other], True, True, [], hardware=hardware, warnings=copy.deepcopy(hardware["warnings"]))


def measure(window, index: int) -> tuple[int, int]:
    """(width the page needs, width it has)."""
    QApplication.processEvents()
    QApplication.processEvents()
    page = window.pages[index]
    body = page.scroll_area.widget()
    body.layout().activate()
    return body.minimumSizeHint().width(), page.scroll_area.viewport().width()


@pytest.mark.parametrize("size", SIZES, ids=lambda size: f"{size[0]}x{size[1]}")
@pytest.mark.parametrize("name", ["thinkpad-intel-nvidia-hybrid", "qemu-q35-virtio"])
def test_every_page_and_tab_fits_the_window_width(make_window, size, name):
    manifest = InstallManifest()
    manifest.storage.device = "/dev/nvme0n1"
    window = make_window(info=laptop_info(name), manifest=manifest)
    window.resize(*size)
    cut_off = []
    for advanced in (False, True):
        window.mode.setCurrentIndex(1 if advanced else 0)
        for index in range(7):
            window._show_page(index)
            variants = list(range(4)) if index == 4 else ["guided", "manual"] if index == 2 else [None]
            for variant in variants:
                if index == 4:
                    window.customize.tabs.setCurrentIndex(variant)
                if index == 2:
                    window.storage.mode.setCurrentIndex(0 if variant == "guided" else 1)
                    if variant == "manual":
                        window.storage.manual_editor.set_rows([Partition("/dev/nvme0n1p1", "/efi", "vfat", format=False), Partition("/dev/sdb1", "/home", "ext4", format=False)])
                needs, has = measure(window, index)
                if needs > has:
                    cut_off.append(f"page {index} {variant or ''} {'advanced' if advanced else 'guided'}: needs {needs}, has {has}")
    assert cut_off == []


def test_the_result_panel_of_a_failed_run_fits_too(make_window):
    window = make_window()
    window.resize(800, 560)
    window._show_page(7)
    window.progress.handle_event({"type": "error", "message": "boom", "data": {"retryable": True, "resumable": True, "bootable": True}})
    window.progress.handle_event({"type": "completed", "data": {"success": False, "bootable": True}})
    needs, has = measure(window, 7)
    assert needs <= has


def test_a_row_of_buttons_wraps_instead_of_forcing_the_page_wider(qtbot):
    from PySide6.QtWidgets import QWidget

    host = QWidget()
    qtbot.addWidget(host)
    flow = FlowLayout(host)
    buttons = [QPushButton(f"A button with a long caption {number}") for number in range(5)]
    for button in buttons:
        flow.addWidget(button)
    wide = flow.heightForWidth(2000)
    narrow = flow.heightForWidth(420)
    assert flow.minimumSize().width() == max(button.sizeHint().width() for button in buttons) or flow.minimumSize().width() >= buttons[0].minimumSizeHint().width()
    assert narrow > wide  # more lines when there is less room
    host.resize(420, narrow)
    host.show()
    QApplication.processEvents()
    assert all(button.geometry().right() <= 420 for button in buttons)


def test_forms_and_combo_boxes_give_way_on_a_narrow_window(make_window):
    window = make_window()
    window.resize(800, 560)
    window._show_page(3)
    forms = window.system.findChildren(QFormLayout)
    assert forms and all(form.rowWrapPolicy() == QFormLayout.RowWrapPolicy.WrapLongRows for form in forms)
    assert window.system.stage.minimumWidth() == 120
