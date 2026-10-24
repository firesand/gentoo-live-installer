"""BOOT: the Secure Boot modes and the boot choices that are refused until they have an effect."""

from __future__ import annotations

from gentoo_installer.manifest import InstallManifest

from tests.test_manifest_wave5 import valid_manifest


def secure(mode: str, **changes: object) -> InstallManifest:
    m = valid_manifest()
    m.system.bootloader, m.system.uki = "systemd-boot", True
    m.system.secure_boot_mode = mode
    for name, value in changes.items():
        setattr(m.system, name, value)
    return m


def test_a_key_made_by_the_installer_needs_no_files():
    m = secure("create")
    assert m.system.secure_boot is True and m.validate() == []


def test_own_key_needs_both_files():
    m = secure("own-key")
    assert any("signing key and certificate" in error for error in m.validate())
    m.system.secure_boot_key, m.system.secure_boot_certificate = "/root/db.key", "/root/db.crt"
    assert m.validate() == []


def test_the_mode_and_the_flag_stay_together():
    m = secure("create")
    m.system.secure_boot = False
    assert m.system.secure_boot_mode == "off" and m.validate() == []
    m.system.secure_boot_mode = "own-key"
    assert m.system.secure_boot is True
    assert InstallManifest.from_dict(m.to_dict()).system.secure_boot_mode == "own-key"


def test_shim_grub_and_no_bootloader_are_refused_with_plain_messages():
    assert any("shim and MOK is not available yet" in error for error in secure("shim").validate())
    assert any("not supported with GRUB" in error for error in secure("create", bootloader="grub").validate())
    assert any("loader that the installer signs" in error for error in secure("create", bootloader="none").validate())


def test_choices_without_an_effect_are_refused_not_ignored():
    for name, value in (("kernel", "custom"), ("initramfs", "ugrd"), ("hostonly", True), ("generic_uki", True)):
        m = valid_manifest()
        setattr(m.system, name, value)
        assert any("not available yet" in error for error in m.validate()), name
