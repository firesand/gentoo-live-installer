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


# --- target-config: profile derivation and early validation -------------------------


def test_profile_is_derived_unless_the_manifest_names_one():
    from gentoo_installer.manifest import derive_profile

    manifest = valid_manifest()
    assert manifest.system.profile == ""
    assert manifest.effective_profile() == "default/linux/amd64/23.0/desktop/plasma"
    manifest.system.init, manifest.system.stage_variant, manifest.system.desktop = "systemd", "amd64-systemd", "minimal"
    assert manifest.effective_profile() == "default/linux/amd64/23.0/systemd"
    manifest.system.profile = "default/linux/amd64/23.0/hardened"
    assert manifest.effective_profile() == "default/linux/amd64/23.0/hardened"
    assert derive_profile("openrc", "gnome", multilib=True) == "default/linux/amd64/23.0/desktop/gnome"
    assert derive_profile("openrc", "xfce", multilib=True) == "default/linux/amd64/23.0/desktop"
    assert derive_profile("systemd", "kde", multilib=False) == "default/linux/amd64/23.0/no-multilib/systemd"
    assert derive_profile("openrc", "kde", multilib=True, hardened=True) == "default/linux/amd64/23.0/hardened"
    assert derive_profile("systemd", "kde", multilib=False, hardened=True) == "default/linux/amd64/23.0/no-multilib/hardened/systemd"


def test_hand_written_manifest_without_profile_gets_a_matching_one():
    raw = valid_manifest().to_dict()
    raw["system"].update(init="systemd", stage_variant="amd64-systemd", desktop="minimal")
    del raw["system"]["profile"]
    manifest = InstallManifest.from_dict(raw)
    assert manifest.validate() == []
    assert manifest.effective_profile() == "default/linux/amd64/23.0/systemd"


@pytest.mark.parametrize("profile", ["../../etc", "default/linux/arm64/23.0", "default/linux/amd64/23.0/desk top", "x\ny"])
def test_bad_profiles_are_rejected_before_installation(profile):
    manifest = valid_manifest()
    manifest.system.profile = profile
    assert any("Portage profile" in error for error in manifest.validate_sections()["system"])


def _secure_boot_manifest(**overrides):
    manifest = valid_manifest()
    manifest.system.secure_boot = True
    manifest.system.secure_boot_key = "/root/sb.key"
    manifest.system.secure_boot_certificate = "/root/sb.crt"
    manifest.system.bootloader = "systemd-boot"
    manifest.system.uki = True
    for name, value in overrides.items():
        section, field = name.split("__")
        setattr(getattr(manifest, section), field, value)
    return manifest


def test_secure_boot_needs_uefi_systemd_boot_or_efi_stub_and_a_unified_kernel_image():
    assert _secure_boot_manifest().validate() == []
    assert _secure_boot_manifest(system__bootloader="efi-stub").validate() == []
    cases = {
        "storage__firmware": ("bios", "requires UEFI"),
        "system__bootloader": ("grub", "not supported with GRUB"),
        "system__uki": (False, "requires a signed Unified Kernel Image"),
        "system__secure_boot_key": ("", "signing key and certificate"),
        "system__secure_boot_certificate": ("relative/cert.pem", "absolute file paths"),
    }
    for name, (value, fragment) in cases.items():
        manifest = _secure_boot_manifest(**{name: value})
        if name == "storage__firmware":
            manifest.system.bootloader = "grub"
        assert any(fragment in error for error in manifest.validate()), name


def test_uki_requires_uefi():
    manifest = valid_manifest()
    manifest.storage.firmware = "bios"
    manifest.system.uki = True
    assert "A Unified Kernel Image requires UEFI firmware" in manifest.validate_sections()["system"]


@pytest.mark.parametrize(("field", "value", "valid"), [
    ("keymap", "us", True), ("keymap", "de-latin1-nodeadkeys", True), ("keymap", "pt-latin9", True),
    ("keymap", 'us"; reboot', False), ("keymap", "-us", False), ("keymap", "x" * 41, False),
    ("keymap", "", True), ("keymap", "auto", True),
    ("locale", "en_US.UTF-8", True), ("locale", "de_DE@euro", True), ("locale", "C.UTF-8", True),
    ("locale", "de_DE.UTF-8@euro", True), ("locale", "sr_RS@latin", True), ("locale", "zh_CN.GB18030", True),
    ("locale", "en_US.UTF-8; rm", False), ("locale", "english", False),
    ("timezone", "Asia/Jakarta", True), ("timezone", "UTC", True),
    ("timezone", "Asia/Jakartaa", False), ("timezone", "../etc/passwd", False), ("timezone", "Europe/Berlin\n", False),
])
def test_locale_section_rejects_typos_and_injection(field, value, valid):
    import zoneinfo

    if field == "timezone" and not zoneinfo.available_timezones():
        pytest.skip("no time zone database on this machine")
    manifest = valid_manifest()
    setattr(manifest.system, field, value)
    assert (manifest.validate_sections()["locale"] == []) is valid


@pytest.mark.parametrize(("full_name", "valid"), [("Jane Doe", True), ("", True), ("Jane: Doe", False), ("Jane\nDoe", False), ("J" * 65, False)])
def test_full_name_must_survive_useradd(full_name, valid):
    manifest = valid_manifest()
    manifest.accounts.full_name = full_name
    assert (manifest.validate_sections()["accounts"] == []) is valid


def test_account_groups_and_ssh_keys_are_checked_up_front():
    manifest = valid_manifest()
    manifest.accounts.groups = ["wheel", "bad group"]
    manifest.accounts.ssh_authorized_keys = ["ssh-ed25519 AAAA\nssh-rsa BBBB"]
    errors = manifest.validate_sections()["accounts"]
    assert any("groups" in error for error in errors)
    assert any("SSH authorized keys" in error for error in errors)


@pytest.mark.parametrize(("atom", "valid"), [
    ("app-editors/vim", True), ("dev-libs/openssl:0/3", False), ("sys-apps/portage:0", True),
    ("-C/foo", False), ("app-editors", False), ("app editors/vim", False), ("=app-editors/vim-9", False),
])
def test_extra_package_atoms_must_start_with_a_letter_or_digit(atom, valid):
    manifest = valid_manifest()
    manifest.options.extra_packages = [atom]
    assert (manifest.validate_sections()["options"] == []) is valid


def test_kde_application_set_is_a_choice():
    manifest = valid_manifest()
    assert manifest.options.kde_apps == "core"
    manifest.options.kde_apps = "everything"
    assert manifest.validate_sections()["options"] == ["KDE applications must be core or full"]


@pytest.mark.parametrize(("make_opts", "valid"), [
    ("", True), ("-j4", True), ("-j4 -l8.5", True), ("--jobs=4 --load-average=8", True), ("-j 2 -l 4", True),
    ("-j2; reboot", False), ("-j2 $(id)", False), ("-O3", False), ("-j2\n-l3", False),
])
def test_make_opts_accepts_only_job_options(make_opts, valid):
    manifest = valid_manifest()
    manifest.portage.make_opts = make_opts
    assert (manifest.validate_sections()["portage"] == []) is valid


def test_default_make_opts_means_automatic():
    assert InstallManifest().portage.make_opts == ""


def test_package_use_and_free_text_are_validated_before_the_disk_is_touched():
    manifest = valid_manifest()
    manifest.portage.package_use = {"invalid;rm": ["X"], "app/valid": ["X;touch"], "*/*": ["VIDEO_CARDS:", "virgl"]}
    manifest.portage.cflags = "-O2\n-march=native"
    manifest.portage.features = ["ok", "bad feature"]
    errors = manifest.validate_sections()["portage"]
    assert sum("package.use entry" in error for error in errors) == 2
    assert any("CFLAGS" in error for error in errors)
    assert any("FEATURES" in error for error in errors)


def test_fallback_and_sync_mirrors_must_be_encrypted_or_rsync():
    manifest = valid_manifest()
    manifest.portage.mirror_fallbacks = ["http://mirror.example"]
    manifest.portage.rsync_mirror = "ftp://example"
    errors = manifest.validate_sections()["portage"]
    assert "Fallback stage3 mirrors must use HTTPS" in errors
    assert "The repository sync mirror must use rsync or HTTPS" in errors
    manifest.portage.mirror_fallbacks = ["https://mirror.example/gentoo"]
    manifest.portage.rsync_mirror = "rsync://rsync.example/gentoo"
    assert manifest.validate_sections()["portage"] == []


@pytest.mark.parametrize(("proxy", "valid"), [
    ("", True), ("http://proxy.example:3128", True), ("http://user:secret@127.0.0.1:3128", True),
    ("socks5h://127.0.0.1:9050", False),
    ("javascript:alert(1)", False), ("ftp://proxy.example", False), ("http://", False),
    ("http://proxy.example/with/path", False), ("http://pro xy.example", False),
])
def test_proxy_scheme_and_shape(proxy, valid):
    manifest = valid_manifest()
    manifest.network.proxy = proxy
    assert (manifest.validate_sections()["network"] == []) is valid


def test_wifi_only_with_networkmanager_and_sane_values():
    manifest = valid_manifest()
    manifest.network.wifi_ssid = "Home"
    manifest.network.wifi_password = "correct horse"
    assert manifest.validate_sections()["network"] == []
    manifest.network.manager = "dhcpcd"
    assert "Wi-Fi setup is available only with NetworkManager" in manifest.validate_sections()["network"]
    manifest.network.manager = "networkmanager"
    for ssid, password in (("bad\nssid", "correct horse"), ("x" * 33, "correct horse"), ("Home", "short"),
                           ("Home", "x" * 64), ("", "correct horse")):
        manifest.network.wifi_ssid, manifest.network.wifi_password = ssid, password
        assert manifest.validate_sections()["network"], (ssid, password)
    manifest.network.wifi_ssid, manifest.network.wifi_password = "Café", "g" * 64  # a non-ASCII name is fine, 64 characters must be hex
    assert manifest.validate_sections()["network"] == ["The Wi-Fi password must be 8 to 63 printable ASCII characters or 64 hexadecimal digits"]
    manifest.network.wifi_ssid, manifest.network.wifi_password = "Open cafe", ""
    assert manifest.validate_sections()["network"] == []
    manifest.network.wifi_ssid, manifest.network.wifi_password = "Home", "0123456789abcdef" * 4
    assert manifest.validate_sections()["network"] == []


def test_static_dhcpcd_needs_an_interface_and_live_profile_import_needs_networkmanager():
    manifest = valid_manifest()
    manifest.network.manager = "dhcpcd"
    manifest.network.dhcp = False
    manifest.network.address, manifest.network.gateway, manifest.network.dns = "192.0.2.10/24", "192.0.2.1", ["192.0.2.53"]
    assert "A static address with dhcpcd needs the network interface name" in manifest.validate_sections()["network"]
    manifest.network.interface = "enp1s0"
    assert manifest.validate_sections()["network"] == []
    manifest.network.import_live_profiles = True
    assert any("requires NetworkManager" in error for error in manifest.validate_sections()["network"])
    manifest.network.manager = "networkmanager"
    assert manifest.validate_sections()["network"] == []
    manifest.network.interface = "bad name"
    assert any("interface name" in error for error in manifest.validate_sections()["network"])


def test_proxy_credentials_are_redacted_in_saved_manifests():
    manifest = valid_manifest()
    manifest.network.proxy = "http://user:s3cr3t@proxy.example:3128"
    data = manifest.to_dict(redact=True)
    assert data["network"]["proxy"] == "http://user:<redacted>@proxy.example:3128"
    assert "s3cr3t" not in manifest.to_json(redact=True)
    assert manifest.to_dict()["network"]["proxy"] == "http://user:s3cr3t@proxy.example:3128"
    manifest.network.proxy = "http://proxy.example:3128"
    assert manifest.to_dict(redact=True)["network"]["proxy"] == "http://proxy.example:3128"
    # a redacted proxy must be completed before it can be used for an installation
    redacted = InstallManifest.from_dict(valid_manifest().to_dict())
    redacted.network.proxy = "http://user:<redacted>@proxy.example:3128"
    assert "Replace the redacted proxy password before installing" in redacted.validate_sections()["network"]
