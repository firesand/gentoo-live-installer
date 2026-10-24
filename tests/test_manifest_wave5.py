"""The settings added for Wave 5: defaults, round trips, validation, and the migration of settings files saved before them."""

from __future__ import annotations

import json

import pytest

from gentoo_installer.manifest import InstallManifest, ManifestError
from gentoo_installerd import hwplan

UUID = "0b3e2f10-1c5a-4d3b-9a77-5f0c1d2e3f40"


def valid_manifest() -> InstallManifest:
    manifest = InstallManifest()
    manifest.storage.device = "/dev/nvme0n1"
    manifest.accounts.password = "user-password"
    manifest.accounts.root_password = "root-password"
    return manifest


def reload(manifest: InstallManifest) -> InstallManifest:
    return InstallManifest.from_json(manifest.to_json())


# The old settings file layout: no Wave 5 key, bool settings, the old spellings.
LEGACY = {
    "storage": {"device": "/dev/sda", "filesystem": "ext4", "swap_mib": 2048},
    "system": {"init": "openrc", "stage_variant": "amd64-openrc", "desktop": "kde", "kernel": "gentoo-kernel-bin", "initramfs": "dracut",
               "bootloader": "grub", "world_update": True},
    "portage": {"binhost": True},
    "network": {"manager": "networkmanager"},
    "accounts": {"username": "gentoo", "password": "user-password", "root_password": "root-password", "disable_root_login": False},
    "options": {"ssh": False, "chrony": True, "cron": True, "logger": True, "kde_apps": "core"},
    "hardware": {"gpu_driver": "open", "nvk": "yes"},
    "version": 1,
}


def test_new_settings_have_the_documented_defaults():
    m = InstallManifest()
    assert (m.storage.separate_boot, m.storage.swap_kind, m.storage.swap_choice, m.storage.fstab_ident, m.storage.atime, m.storage.boot_noauto) \
        == (False, "auto", "custom", "uuid", "noatime", False)
    assert (m.network.keep_proxy, m.network.live_static.is_set(), m.network.wireless_tools, m.network.ppp, m.network.copy_live_wifi) \
        == (True, False, False, False, "")
    assert (m.portage.optimize, m.portage.binhost_variant, m.portage.mirrors, m.portage.rsync_mode, m.portage.license_groups,
            m.portage.depclean, m.portage.profile_override) == ("generic", "auto", [], "snapshot", [], False, "")
    assert (m.system.world_update, m.system.stage_auto, m.system.kernel_config_snippet, m.system.hostonly, m.system.generic_uki,
            m.system.secure_boot_mode, m.system.serial_console, m.system.domain, m.system.hosts_extra, m.system.extra_locales) \
        == ("later", True, "", False, False, "off", "", "", [], [])
    assert (m.system.desktop, m.system.initramfs, m.system.bootloader) == ("kde", "dracut", "grub")
    assert (m.accounts.root_mode, m.accounts.privilege, m.accounts.ssh_password_login, m.accounts.ssh_root_login) == ("separate", "sudo", True, False)
    o = m.options
    assert (o.ssh, o.logger, o.cron, o.timesync, o.gentoolkit, o.eclean_kernel, o.mlocate, o.journal_persistent, o.browser, o.flathub, o.input_method,
            o.accessibility, o.gaming, o.gaming_tweaks, o.gaming_windows_tools, o.gaming_accept_external) \
        == (True, "syslog-ng", "cronie", "chrony", False, False, False, False, "firefox", False, "auto", False, "off", True, False, False)
    h = m.hardware
    assert (h.gpu_driver, h.nvidia_modules, h.nvk, h.safe_graphics_entry, h.nvidia_rtd3, h.nvidia_extras, h.nvidia_cuda_license_accepted,
            h.signed_modules, h.devices, h.battery_charge_limit, h.lid_action, h.vendor_workarounds) \
        == ("auto", "auto", "auto", True, "auto", [], False, "off", {}, 0, "suspend", False)
    assert valid_manifest().validate() == []


def test_ssh_is_on_by_default_and_password_login_is_allowed():
    m = valid_manifest()
    assert m.options.ssh is True and m.accounts.ssh_password_login is True and m.accounts.ssh_root_login is False
    assert reload(m).options.ssh is True


# --- round trip: every new setting survives to_json / from_json with a non-default value -----------------------------------------------

CHANGES = [
    ("storage.separate_boot", True), ("storage.swap_kind", "zram"), ("storage.swap_choice", "hibernate"), ("storage.filesystem", "f2fs"),
    ("storage.fstab_ident", "label"), ("storage.atime", "lazytime"), ("storage.boot_noauto", True),
    ("network.keep_proxy", False), ("network.wireless_tools", True), ("network.ppp", True), ("network.copy_live_wifi", UUID),
    ("portage.optimize", "native"), ("portage.binhost_variant", "v3"), ("portage.mirrors", ["https://mirror.example.org/gentoo"]),
    ("portage.rsync_mode", "sync"), ("portage.license_groups", ["@FREE", "@BINARY-REDISTRIBUTABLE"]), ("portage.depclean", True),
    ("portage.profile_override", "default/linux/amd64/23.0/desktop"),
    ("system.world_update", "auto"), ("system.stage_auto", False), ("system.kernel", "custom"), ("system.kernel_config_snippet", "CONFIG_FOO=y\n# CONFIG_BAR is not set\n"),
    ("system.initramfs", "ugrd"), ("system.hostonly", True), ("system.generic_uki", True), ("system.bootloader", "none"),
    ("system.secure_boot_mode", "shim"), ("system.serial_console", "ttyS0,115200"), ("system.desktop", "sway"), ("system.domain", "example.org"),
    ("system.hosts_extra", ["192.168.1.10 nas nas.example.org"]), ("system.extra_locales", ["de_DE.UTF-8", "fr_FR.UTF-8"]),
    ("accounts.root_mode", "same"), ("accounts.privilege", "su"), ("accounts.ssh_password_login", False), ("accounts.ssh_root_login", True),
    ("options.ssh", False), ("options.logger", "metalog"), ("options.cron", "dcron"), ("options.timesync", "timesyncd"), ("options.gentoolkit", True),
    ("options.eclean_kernel", True), ("options.mlocate", True), ("options.journal_persistent", True), ("options.browser", "none"),
    ("options.flathub", True), ("options.input_method", "fcitx5"), ("options.accessibility", True), ("options.gaming", "flatpak"),
    ("options.gaming_tweaks", False), ("options.gaming_windows_tools", True), ("options.gaming_accept_external", True),
    ("hardware.gpu_driver", "basic"), ("hardware.nvidia_modules", "closed"), ("hardware.nvk", "on"), ("hardware.safe_graphics_entry", False),
    ("hardware.nvidia_rtd3", "off"), ("hardware.nvidia_extras", ["vaapi", "nvtop"]), ("hardware.nvidia_cuda_license_accepted", True),
    ("hardware.signed_modules", "kernel_key"), ("hardware.devices", {"openrazer": True, "solaar": False}), ("hardware.battery_charge_limit", 80),
    ("hardware.lid_action", "lock"), ("hardware.vendor_workarounds", True),
]


# Choices that are accepted in a saved file but have no effect yet: the validation refuses them (BOOT items 8, 10, 11).
NOT_AVAILABLE_YET = {"system.kernel", "system.initramfs", "system.hostonly", "system.generic_uki", "system.secure_boot_mode"}


def configured(path: str, value: object) -> InstallManifest:
    m = valid_manifest()
    section, name = path.split(".")
    if path == "storage.filesystem":
        m.storage.separate_boot = True  # GRUB cannot read /boot from F2FS
    if path == "system.initramfs":
        m.system.kernel = "custom"  # a distribution kernel needs dracut
    if path == "accounts.ssh_password_login":
        m.accounts.ssh_authorized_keys = ["ssh-ed25519 AAAA test"]
    if path == "options.timesync":
        m.system.init, m.system.stage_variant = "systemd", "amd64-systemd"
    setattr(getattr(m, section), name, value)
    return m


@pytest.mark.parametrize(("path", "value"), CHANGES, ids=[path for path, _ in CHANGES])
def test_every_new_setting_round_trips(path, value):
    m = configured(path, value)
    if path in NOT_AVAILABLE_YET:  # still saved and loaded, but the validation says so instead of ignoring it
        assert any("not available yet" in error for error in m.validate()), m.validate()
        section, name = path.split(".")
        restored = InstallManifest.from_dict(m.to_dict())
        assert getattr(getattr(restored, section), name) == value and restored.to_dict() == m.to_dict()
        return
    else:
        assert m.validate() == [], m.validate()
    section, name = path.split(".")
    assert getattr(getattr(reload(m), section), name) == value
    assert reload(m).to_dict() == m.to_dict()


def test_live_static_round_trips():
    m = valid_manifest()
    m.network.live_static.address, m.network.live_static.gateway, m.network.live_static.dns = "10.0.0.5/24", "10.0.0.1", ["10.0.0.1"]
    assert m.validate() == []
    restored = reload(m)
    assert (restored.network.live_static.address, restored.network.live_static.gateway, restored.network.live_static.dns) == ("10.0.0.5/24", "10.0.0.1", ["10.0.0.1"])


def test_every_new_setting_is_covered_by_the_round_trip_list():
    covered = {path for path, _ in CHANGES} | {"network.live_static"}
    raw = InstallManifest().to_dict()
    for section, name in (("storage", "separate_boot"), ("network", "live_static"), ("hardware", "devices"), ("options", "timesync")):
        assert name in raw[section]
    assert "options.chrony" not in {f"options.{key}" for key in raw["options"]}  # chrony is only an alias that loads
    assert {"portage.optimize", "options.gaming", "hardware.devices", "accounts.root_mode"} <= covered


# --- validation: each rule has a plain message --------------------------------------------------------------------------------------

BAD = [
    ("storage.swap_kind", "swapfile", "Swap kind"), ("storage.swap_choice", "big", "Swap choice"), ("storage.fstab_ident", "name", "fstab"),
    ("storage.atime", "always", "access-time"), ("storage.filesystem", "zfs", "Root filesystem"),
    ("network.copy_live_wifi", "not-a-uuid", "uuid"),
    ("portage.optimize", "fast", "Optimisation"), ("portage.binhost_variant", "v9", "binary package variant"), ("portage.rsync_mode", "git", "sync mode"),
    ("portage.mirrors", ["http://insecure.example.org"], "HTTPS"), ("portage.license_groups", ["bad group"], "Licence groups"),
    ("portage.profile_override", "default/linux/arm64/23.0", "profile override"),
    ("system.world_update", "sometimes", "@world update"), ("system.kernel", "linux-next", "kernel"), ("system.initramfs", "mkinitcpio", "initramfs"),
    ("system.secure_boot_mode", "magic", "Secure Boot mode"), ("system.serial_console", "COM1", "serial console"),
    ("system.kernel_config_snippet", "rm -rf /", "kernel configuration"), ("system.domain", "bad domain!", "domain"),
    ("system.hosts_extra", ["not-an-address name"], "Extra hosts"), ("system.hosts_extra", ["10.0.0.1"], "Extra hosts"),
    ("system.extra_locales", ["english"], "extra locales"), ("system.desktop", "enlightenment", "desktop"), ("system.bootloader", "lilo", "bootloader"),
    ("accounts.root_mode", "sometimes", "root account"), ("accounts.privilege", "pkexec", "Privilege"),
    ("options.logger", "journald", "logger"), ("options.cron", "anacron", "cron"), ("options.timesync", "ntpd", "Time synchronisation"),
    ("options.browser", "chromium", "browser"), ("options.input_method", "anthy", "input method"), ("options.gaming", "wine", "Gaming"),
    ("hardware.gpu_driver", "radeon", "gpu driver"), ("hardware.nvidia_modules", "both", "nvidia modules"), ("hardware.nvk", "yes-please", "nvk"),
    ("hardware.nvidia_rtd3", "maybe", "rtd3"), ("hardware.nvidia_extras", ["fan-control"], "NVIDIA add-on"),
    ("hardware.signed_modules", "yes", "signed modules"), ("hardware.devices", {"toaster": True}, "Unknown device"),
    ("hardware.battery_charge_limit", 70, "charge limit"), ("hardware.lid_action", "explode", "lid action"),
]


@pytest.mark.parametrize(("path", "value", "fragment"), BAD, ids=[f"{path}={value}" for path, value, _ in BAD])
def test_bad_values_are_refused_with_a_plain_message(path, value, fragment):
    m = valid_manifest()
    section, name = path.split(".")
    setattr(getattr(m, section), name, value)
    assert any(fragment.lower() in error.lower() for error in m.validate()), m.validate()


def test_genkernel_is_refused_with_a_plain_message():
    m = valid_manifest()
    m.system.initramfs = "genkernel"
    errors = m.validate()
    assert any("genkernel is no longer offered" in error and "dracut" in error for error in errors)
    with pytest.raises(ManifestError, match="genkernel is no longer offered"):
        InstallManifest.from_json(m.to_json())


def test_f2fs_is_not_allowed_for_boot_with_grub():
    m = valid_manifest()
    m.storage.filesystem = "f2fs"
    assert any("GRUB cannot reliably read /boot" in error for error in m.validate())
    m.storage.separate_boot = True
    assert m.validate() == []
    m.storage.separate_boot = False
    m.system.bootloader = "systemd-boot"
    assert m.validate() == []


def test_f2fs_boot_row_is_refused_with_grub_in_manual_mode():
    from gentoo_installer.manifest import Partition

    m = valid_manifest()
    m.storage.mode = "manual"
    m.storage.partitions = [Partition(device="/dev/nvme0n1p1", mountpoint="/efi", filesystem="vfat"),
                            Partition(device="/dev/nvme0n1p2", mountpoint="/boot", filesystem="f2fs"),
                            Partition(device="/dev/nvme0n1p3", mountpoint="/", filesystem="f2fs")]
    assert sum("GRUB cannot reliably read /boot" in error for error in m.validate()) == 1


def test_bootloader_none_needs_no_uefi_and_ugrd_needs_a_manual_or_custom_kernel():
    m = valid_manifest()
    m.storage.firmware = "bios"
    m.storage.table = "dos"
    m.system.bootloader = "none"
    assert m.validate() == []
    m.system.initramfs = "ugrd"
    assert any("require Dracut" in error for error in m.validate())
    m.system.kernel = "custom"
    assert any("custom kernel is not available yet" in error for error in m.validate())  # accepted values that do nothing are refused
    assert any("ugrd initramfs is not available yet" in error for error in m.validate())


@pytest.mark.parametrize("desktop", ["cinnamon", "mate", "lxqt", "gnome-light", "sway", "i3"])
@pytest.mark.parametrize("stage", ["amd64-openrc", "amd64-desktop-openrc", "amd64-nomultilib-openrc", "amd64-hardened-openrc"])
def test_new_desktops_are_accepted_on_every_family(desktop, stage):
    m = valid_manifest()
    m.system.stage_variant, m.system.desktop = stage, desktop
    m.system.multilib = "nomultilib" not in stage
    assert m.validate() == []


def test_gaming_rules():
    m = valid_manifest()
    m.options.gaming = "native"
    assert m.validate() == []
    m.system.multilib, m.system.stage_variant, m.system.desktop = False, "amd64-nomultilib-openrc", "gnome"
    assert any("32-bit" in error for error in m.validate())
    m.options.gaming = "flatpak"
    assert m.validate() == []
    m.system.desktop = "minimal"
    assert any("needs a desktop" in error for error in m.validate())
    m.options.gaming = "off"
    assert m.validate() == []


def test_cuda_needs_its_licence_and_a_live_static_address_must_be_complete():
    m = valid_manifest()
    m.hardware.nvidia_extras = ["cuda"]
    assert any("CUDA" in error and "licence" in error for error in m.validate())
    m.hardware.nvidia_cuda_license_accepted = True
    assert m.validate() == []
    m.network.live_static.address = "10.0.0.5/24"
    assert any("fixed address for this session" in error for error in m.validate())


def test_copying_a_wifi_connection_needs_network_manager_and_timesyncd_and_run0_need_systemd():
    m = valid_manifest()
    m.network.manager, m.network.copy_live_wifi = "dhcpcd", UUID
    assert any("requires NetworkManager" in error for error in m.validate())
    m = valid_manifest()
    m.options.timesync = "timesyncd"
    assert any("needs systemd" in error for error in m.validate())
    m = valid_manifest()
    m.accounts.privilege = "run0"
    assert any("run0 is part of systemd" in error for error in m.validate())


def test_ssh_without_any_way_to_log_in_is_refused():
    m = valid_manifest()
    m.accounts.ssh_password_login = False
    assert any("Nobody could log in over SSH" in error for error in m.validate())
    m.accounts.ssh_authorized_keys = ["ssh-ed25519 AAAA test"]
    assert m.validate() == []
    m.accounts.ssh_authorized_keys = []
    m.options.ssh = False
    assert m.validate() == []


# --- migrations ---------------------------------------------------------------------------------------------------------------------

def test_a_settings_file_from_before_wave_5_loads_and_means_the_same():
    m = InstallManifest.from_dict(json.loads(json.dumps(LEGACY)))
    assert m.validate() == []
    assert (m.system.world_update, m.options.logger, m.options.cron, m.options.timesync, m.options.chrony) == ("now", "syslog-ng", "cronie", "chrony", True)
    assert (m.hardware.gpu_driver, m.hardware.nvk) == ("nouveau", "on")
    assert m.options.ssh is False  # an explicit saved choice stays
    assert m.accounts.root_mode == "separate" and m.system.secure_boot_mode == "off"
    assert reload(m).to_dict() == m.to_dict()


@pytest.mark.parametrize(("section", "key", "old", "new"), [
    ("system", "world_update", True, "now"), ("system", "world_update", False, "later"),
    ("options", "logger", True, "syslog-ng"), ("options", "logger", False, "none"),
    ("options", "cron", True, "cronie"), ("options", "cron", False, "none"),
    ("hardware", "gpu_driver", "open", "nouveau"), ("hardware", "nvk", "yes", "on"), ("hardware", "nvk", "no", "off"),
])
def test_widened_settings_migrate_when_a_file_is_loaded_and_when_code_assigns_them(section, key, old, new):
    raw = json.loads(json.dumps(LEGACY))
    raw[section][key] = old
    assert getattr(getattr(InstallManifest.from_dict(raw), section), key) == new
    m = valid_manifest()
    setattr(getattr(m, section), key, old)
    assert getattr(getattr(m, section), key) == new


def test_the_old_chrony_flag_is_an_alias_of_timesync():
    raw = json.loads(json.dumps(LEGACY))
    raw["options"]["chrony"] = False
    m = InstallManifest.from_dict(raw)
    assert m.options.timesync == "none" and m.options.chrony is False and "chrony" not in m.to_dict()["options"]
    m.options.chrony = True
    assert m.options.timesync == "chrony"
    raw["options"]["timesync"] = "timesyncd"  # the new setting wins when both are in the file
    assert InstallManifest.from_dict(raw).options.timesync == "timesyncd"


def test_secure_boot_true_loads_as_the_own_key_mode_and_root_login_follows_root_mode():
    raw = json.loads(json.dumps(LEGACY))
    raw["system"].update(secure_boot=True, uki=True, bootloader="systemd-boot", secure_boot_key="/k.key", secure_boot_certificate="/k.crt")
    raw["accounts"]["disable_root_login"] = True
    m = InstallManifest.from_dict(raw)
    assert m.system.secure_boot_mode == "own-key" and m.accounts.root_mode == "locked"
    assert reload(m).to_dict() == m.to_dict()
    m.accounts.disable_root_login = False
    assert m.accounts.root_mode == "separate"
    m.accounts.root_mode = "locked"
    assert m.accounts.disable_root_login is True and m.accounts.root_locked()
    m.system.secure_boot = False
    m.system.secure_boot = True
    assert m.system.secure_boot_mode == "own-key"


def test_a_locked_root_needs_no_root_password():
    m = valid_manifest()
    m.accounts.root_password = ""
    assert any("root password" in error for error in m.validate())
    m.accounts.root_mode = "locked"
    assert m.validate() == []


# --- every manifest the repository builds still loads --------------------------------------------------------------------------------

def test_every_vm_scenario_manifest_loads_and_round_trips():
    from tests.vm.helpers import PUBLIC_KEY, load_vm_module

    vm = load_vm_module()
    for name, scenario in vm.SCENARIO_MAP.items():
        manifest = vm.build_manifest(scenario, PUBLIC_KEY)
        assert manifest.validate() == [], name
        assert InstallManifest.from_json(manifest.to_json()).to_dict() == manifest.to_dict(), name


def test_every_golden_scenario_manifest_loads_and_round_trips():
    from tests.hardware.helpers import FIXTURE_NAMES, fixture_dict
    from tests.hardware.test_plan_golden import SCENARIOS, scenario_manifest

    for fixture in FIXTURE_NAMES:
        machine = fixture_dict(fixture)
        for scenario in SCENARIOS:
            manifest = scenario_manifest(scenario, machine)
            assert InstallManifest.from_dict(manifest.to_dict()).to_dict() == manifest.to_dict(), (fixture, scenario)


def test_an_old_saved_settings_file_round_trips_through_save_and_load(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(LEGACY))
    m = InstallManifest.load_redacted(path)
    saved = tmp_path / "saved.json"
    m.save(saved, redact=False)
    assert InstallManifest.load_redacted(saved).to_dict() == m.to_dict()


# --- the plan output -----------------------------------------------------------------------------------------------------------------

def test_plan_schema_2_adds_empty_fields_and_still_reads_schema_1():
    assert hwplan.PLAN_SCHEMA == 2 and hwplan.READABLE_PLAN_SCHEMAS == (1, 2)
    plan = hwplan.HardwarePlan()
    for name in ("groups", "package_keywords", "package_unmask", "user_services", "repos", "first_steps"):
        assert not getattr(plan, name)
        assert name not in plan.to_dict()  # empty fields are left out, so a plain plan reads like a schema 1 plan
    old = plan.to_dict()
    old["schema"] = 1
    restored = hwplan.HardwarePlan.from_dict(old)  # a saved schema 1 plan has none of the new fields
    assert restored.groups == [] and restored.repos == [] and restored.first_steps == []


def test_plan_schema_2_fields_round_trip():
    plan = hwplan.HardwarePlan(groups=["plugdev"], package_keywords={"x11-drivers/nvidia-drivers": ["~amd64"]}, package_unmask=["=sys-kernel/foo-1"],
                               user_services=[("pipewire", "user")], repos=["guru"], first_steps=["Run solaar once."])
    restored = hwplan.HardwarePlan.from_dict(json.loads(json.dumps(plan.to_dict())))
    assert (restored.groups, restored.package_keywords, restored.package_unmask, restored.user_services, restored.repos, restored.first_steps) \
        == (["plugdev"], {"x11-drivers/nvidia-drivers": ["~amd64"]}, ["=sys-kernel/foo-1"], [("pipewire", "user")], ["guru"], ["Run solaar once."])
    assert restored.to_dict() == plan.to_dict()
