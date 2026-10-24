import json

import pytest

from gentoo_installer.manifest import InstallManifest, ManifestError, Partition


def valid_manifest():
    manifest = InstallManifest()
    manifest.storage.device = "/dev/nvme0n1"
    manifest.accounts.password = "user-password"
    manifest.accounts.root_password = "root-password"
    return manifest


def test_round_trip_preserves_choices_and_redacts_secrets():
    manifest = valid_manifest()
    manifest.system.init = "systemd"
    manifest.system.stage_variant = "amd64-systemd"
    manifest.storage.luks = True
    manifest.storage.luks_passphrase = "disk-secret"
    restored = InstallManifest.from_json(manifest.to_json())
    assert restored.storage.device == "/dev/nvme0n1"
    assert restored.system.stage_variant == "amd64-systemd"
    assert restored.validate() == []
    redacted = restored.to_json(redact=True)
    assert "disk-secret" not in redacted
    assert "user-password" not in redacted
    assert "root-password" not in redacted
    with pytest.raises(ManifestError, match="redacted secret"):
        InstallManifest.from_json(redacted)


def test_unknown_fields_are_rejected():
    with pytest.raises(ManifestError, match="Unknown manifest fields"):
        InstallManifest.from_dict({"unexpected": True})


@pytest.mark.parametrize("value,message", [
    ({"storage": "/dev/sda"}, "storage must be an object"),
    ({"storage": {"device": 42}}, "storage.device must be a string"),
    ({"storage": {"luks": 1}}, "storage.luks must be true or false"),
    ({"options": {"extra_packages": "app-editors/vim"}}, "must be a list"),
])
def test_wrong_json_types_have_clear_errors(value, message):
    with pytest.raises(ManifestError, match=message):
        InstallManifest.from_dict(value)


def test_systemd_network_requires_systemd():
    manifest = valid_manifest()
    manifest.network.manager = "systemd-networkd"
    assert "systemd-networkd requires systemd" in manifest.validate()


def test_netifrc_requires_shell_safe_interface_name():
    manifest = valid_manifest()
    manifest.network.manager = "netifrc"
    assert any("network interface" in error for error in manifest.validate())
    manifest.network.interface = "enp1s0"
    assert not any("network interface" in error for error in manifest.validate())
    manifest.network.interface = "enp1s0;touch"
    assert any("network interface" in error for error in manifest.validate())


def test_guided_dos_layout_rejects_more_than_four_required_partitions():
    manifest = valid_manifest()
    manifest.storage.table = "dos"
    manifest.storage.firmware = "uefi"
    manifest.storage.separate_home = True
    manifest.storage.luks = True
    manifest.storage.luks_passphrase = "disk-passphrase"
    assert any("guided DOS layout needs more than four" in error for error in manifest.validate())


def test_manual_uefi_layout_requires_esp():
    manifest = valid_manifest()
    manifest.storage.mode = "manual"
    manifest.storage.partitions = [Partition(device="/dev/nvme0n1p2", mountpoint="/")]
    assert any("EFI System Partition" in error for error in manifest.validate())
    manifest.storage.partitions.append(Partition(device="/dev/nvme0n1p1", mountpoint="/efi", filesystem="vfat"))
    assert manifest.validate() == []


def test_manual_luks_requires_unencrypted_boot_mount():
    manifest = valid_manifest()
    manifest.storage.mode = "manual"
    manifest.storage.luks = True
    manifest.storage.luks_passphrase = "disk-passphrase"
    manifest.storage.partitions = [
        Partition(device="/dev/nvme0n1p1", mountpoint="/efi", filesystem="vfat"),
        Partition(device="/dev/nvme0n1p2", mountpoint="/"),
    ]
    assert any("separate /boot partition" in error for error in manifest.validate())
    manifest.storage.partitions.append(Partition(device="/dev/nvme0n1p3", mountpoint="/boot", filesystem="ext4"))
    assert not any("separate /boot partition" in error for error in manifest.validate())


@pytest.mark.parametrize("stack", ["luks", "lvm"])
def test_manual_unformatted_root_rejects_new_storage_stack(stack):
    manifest = valid_manifest()
    manifest.storage.mode = "manual"
    manifest.storage.partitions = [
        Partition(device="/dev/nvme0n1p1", mountpoint="/efi", filesystem="vfat"),
        Partition(device="/dev/nvme0n1p2", mountpoint="/", format=False),
    ]
    setattr(manifest.storage, stack, True)
    assert any("Preserved root cannot create" in error for error in manifest.validate())
    setattr(manifest.storage, stack, False)
    assert not any("Preserved root cannot create" in error for error in manifest.validate())


def test_encrypted_root_requires_passphrase_and_initramfs():
    manifest = valid_manifest()
    manifest.storage.luks = True
    manifest.system.initramfs = "none"
    errors = manifest.validate()
    assert any("LUKS passphrase" in error for error in errors)
    assert any("need an initramfs" in error for error in errors)


@pytest.mark.parametrize("kernel", ["gentoo-kernel-bin", "gentoo-kernel", "manual"])
@pytest.mark.parametrize("initramfs", ["dracut", "genkernel", "none"])
def test_automatic_kernels_require_dracut(kernel, initramfs):
    manifest = valid_manifest()
    manifest.system.kernel = kernel
    manifest.system.initramfs = initramfs
    unsupported = kernel != "manual" and initramfs != "dracut"
    assert any("Automatic distribution kernels require Dracut" in error for error in manifest.validate()) == unsupported


def test_validate_sections_group_every_error_and_validate_flattens_them():
    from gentoo_installer.manifest import VALIDATION_SECTIONS

    manifest = valid_manifest()
    manifest.storage.device = "nonsense"
    manifest.network.manager = "systemd-networkd"
    manifest.system.timezone = "../etc"
    sections = manifest.validate_sections()
    assert tuple(sections) == VALIDATION_SECTIONS
    assert "Select a real target disk" in sections["storage"]
    assert "systemd-networkd requires systemd" in sections["network"]
    assert "Invalid timezone" in sections["locale"]
    assert manifest.validate() == [error for errors in sections.values() for error in errors]


def test_manifest_without_new_sections_still_loads_with_defaults():
    raw = valid_manifest().to_dict()
    del raw["hardware"]
    for key in ("device_id_kind",):
        del raw["storage"][key]
    for key in ("keyboard_layout", "keyboard_variant", "keyboard_model", "keyboard_options",
                "rtc_local", "console_font", "kernel_cmdline_extra"):
        del raw["system"][key]
    del raw["network"]["import_live_profiles"]
    manifest = InstallManifest.from_dict(raw)
    assert manifest.validate() == []
    assert manifest.hardware.gpu_driver == "auto"
    assert manifest.system.keyboard_layout == "us"
    assert manifest.network.import_live_profiles is False


@pytest.mark.parametrize(("field", "value", "fragment"), [
    ("gpu_driver", "nouveau", "gpu driver"),
    ("power", "laptop-mode", "power"),
    ("zram_percent", 5, "zram"),
    ("hidpi", "dpi:400", "Display scaling"),
    ("nvidia_branch", "latest", "NVIDIA driver branch"),
    ("printer_drivers", ["gutenprint", "foo"], "printer driver"),
])
def test_hardware_choices_are_validated(field, value, fragment):
    manifest = valid_manifest()
    setattr(manifest.hardware, field, value)
    assert any(fragment in error for error in manifest.validate_sections()["hardware"])


def test_proprietary_nvidia_driver_requires_licence_acceptance():
    manifest = valid_manifest()
    manifest.hardware.gpu_driver = "nvidia"
    assert any("licence" in error for error in manifest.validate())
    manifest.hardware.nvidia_license_accepted = True
    assert manifest.validate() == []


@pytest.mark.parametrize(("device_id", "valid"), [
    ("pci-0000:00:04.0|85899345920|QEMU HARDDISK", True),
    ("pci-0000:00:04.0|85899345920|", True),
    ("pci-0000:00:04.0|big|QEMU", False),
    ("/dev/disk/by-path/pci-0|1|x", False),
    ("pci-0000:00:04.0|1", False),
])
def test_path_identity_for_disks_without_serial(device_id, valid):
    manifest = valid_manifest()
    manifest.storage.device_id_kind = "path"
    manifest.storage.device_id = device_id
    assert (manifest.validate() == []) is valid


@pytest.mark.parametrize(("extra", "valid"), [
    ("nvidia-drm.modeset=1 quiet", True),
    ("root=/dev/sda1", False),
    ("init=/bin/sh", False),
    ("a  b", False),
    ("x;reboot", False),
])
def test_extra_kernel_arguments_are_plain_tokens(extra, valid):
    manifest = valid_manifest()
    manifest.system.kernel_cmdline_extra = extra
    assert (manifest.validate() == []) is valid


def test_passwords_with_line_breaks_are_rejected():
    manifest = valid_manifest()
    manifest.accounts.password = "user-password\nroot:owned"
    assert "Passwords cannot contain line breaks or NUL characters" in manifest.validate_sections()["accounts"]


@pytest.mark.parametrize(("field", "value"), [
    ("keyboard_layout", "us,ru"),
    ("keyboard_variant", "dvorak"),
    ("keyboard_options", "grp:alt_shift_toggle,caps:escape"),
])
def test_keyboard_fields_accept_xkb_values(field, value):
    manifest = valid_manifest()
    setattr(manifest.system, field, value)
    assert manifest.validate() == []


def test_keyboard_layout_rejects_shell_text():
    manifest = valid_manifest()
    manifest.system.keyboard_layout = "us; rm -rf /"
    assert "Invalid keyboard layout" in manifest.validate_sections()["locale"]
