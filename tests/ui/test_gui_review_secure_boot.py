"""The Review page says that this PC will refuse the new system where it is read, and only when that is so."""

import pytest

from gentoo_installer.manifest import InstallManifest
from gentoo_installer.pages.review import ReviewPage, manifest_warnings
from tests.hardware.helpers import fixture_dict
from tests.ui.conftest import sample_info


def review_with_secure_boot(qtbot, state="enabled"):
    hardware = fixture_dict("qemu-q35-virtio")
    hardware["firmware"]["secure_boot"] = state
    manifest = InstallManifest()
    manifest.storage.device = "/dev/sda"
    manifest.storage.device_id = "test-disk"
    manifest.storage.firmware = "uefi"
    info = sample_info(hardware=hardware)
    page = ReviewPage(manifest, info)
    qtbot.addWidget(page)
    page.load_manifest()
    return page, manifest, info


def test_the_review_says_so_directly_under_the_disk_warning(qtbot):
    page, manifest, info = review_with_secure_boot(qtbot)
    assert "the PC will refuse to start it" in page.warning.text()
    assert page.warning.text().index("erased") < page.warning.text().index("Secure Boot is on")
    assert "Secure Boot is on" not in page.notes.text()  # said once, where it is read
    assert any("Secure Boot is on" in note for note in manifest_warnings(manifest, info))  # the other readers of the notes keep it


def test_the_review_says_nothing_with_secure_boot_off(qtbot):
    page, _, _ = review_with_secure_boot(qtbot, "disabled")
    assert "Secure Boot" not in page.warning.text()


@pytest.mark.parametrize("change", ["signed", "own loader", "portable", "bios"])
def test_the_review_does_not_say_what_it_cannot_know(qtbot, change):
    page, manifest, _ = review_with_secure_boot(qtbot)
    if change == "signed":
        manifest.system.secure_boot = True
    elif change == "own loader":
        manifest.system.bootloader = "none"
    elif change == "portable":
        manifest.hardware.target = "portable"
    else:
        manifest.storage.firmware = "bios"
    page.load_manifest()
    assert "Secure Boot" not in page.warning.text()
