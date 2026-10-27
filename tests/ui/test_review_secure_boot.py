"""With Secure Boot on, the disc starts (shim and its own key, iso/sign-secure-boot.py), but the system it installs starts only when it is
signed. Readiness says so first; Review says it again right before the installation, when the Readiness page is long gone."""
from __future__ import annotations

from gentoo_installer.manifest import InstallManifest
from gentoo_installer.pages.review import SECURE_BOOT_UNSIGNED, manifest_warnings
from tests.hardware.helpers import fixture_dict
from tests.ui.conftest import sample_info


def info_with(secure_boot: str):
    hardware = fixture_dict("qemu-q35-virtio")
    hardware["firmware"]["secure_boot"] = secure_boot
    return sample_info(hardware=hardware)


def unsigned_notes(manifest: InstallManifest, secure_boot: str) -> list[str]:
    return [note for note in manifest_warnings(manifest, info_with(secure_boot)) if note.startswith("Secure Boot is on in this PC")]


def test_secure_boot_on_and_grub_gets_the_reminder_with_both_ways_out() -> None:
    manifest = InstallManifest()
    assert manifest.system.bootloader == "grub" and not manifest.system.secure_boot
    (note,) = unsigned_notes(manifest, "enabled")
    assert note == SECURE_BOOT_UNSIGNED.format(loader="GRUB")
    assert "Turn Secure Boot off in the firmware setup before the first start" in note and "systemd-boot with Secure Boot signing" in note


def test_an_unsigned_systemd_boot_gets_it_too_and_a_signed_one_does_not() -> None:
    manifest = InstallManifest()
    manifest.system.bootloader = "systemd-boot"
    (note,) = unsigned_notes(manifest, "enabled")
    assert "starts with systemd-boot, which is not signed" in note
    manifest.system.secure_boot, manifest.system.secure_boot_mode = True, "create"
    assert unsigned_notes(manifest, "enabled") == []


def test_no_reminder_when_secure_boot_is_off_unknown_or_the_install_is_for_bios() -> None:
    manifest = InstallManifest()
    for state in ("disabled", "setup", "unknown", ""):
        assert unsigned_notes(manifest, state) == [], state
    manifest.storage.firmware = "bios"
    assert unsigned_notes(manifest, "enabled") == []
    assert [n for n in manifest_warnings(InstallManifest(), None) if n.startswith("Secure Boot")] == []  # no probe, nothing claimed
