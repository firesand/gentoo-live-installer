"""hwplan.validate_plan: cross-checks of the manifest against the probed machine."""

from __future__ import annotations

import pytest

from gentoo_installer import hardware as hw
from gentoo_installerd import hwplan

from tests.hardware.builders import GIB, disk, gpu, manifest, profile, systemd
from tests.hardware.helpers import FIXTURE_NAMES, fixture_dict


def codes(m, machine, severity=None):
    return {issue.code for issue in hwplan.validate_plan(m, machine) if severity in (None, issue.severity)}


def test_issues_have_hw_codes_and_known_severities():
    for name in FIXTURE_NAMES:
        for issue in hwplan.validate_plan(manifest(), fixture_dict(name)):
            assert issue.code.startswith("hw.") and issue.severity in {"info", "warning", "error"} and issue.message
            assert set(issue.to_dict()) == {"code", "severity", "message"}


def test_a_sane_default_install_has_no_errors_on_the_authors_pc():
    m = manifest()
    m.storage.device = "/dev/nvme0n1"
    assert [i for i in hwplan.validate_plan(m, fixture_dict("asus-desktop-rtx4090")) if i.severity == "error"] == []


# -- storage rules (the storage owner enforces them; the GUI shows them early) -------------------------

@pytest.mark.parametrize(("device", "storage", "code", "severity"), [
    (disk(live_medium=True), {}, "hw.st_live_medium", "error"),
    (disk(ro=True), {}, "hw.st_read_only", "error"),
    (disk("/dev/sda", bus="sata", log_sec=4096), {"firmware": "bios", "device": "/dev/sda"}, "hw.bios_4kn", "error"),
    (disk(), {"firmware": "bios"}, "hw.nvme_bios", "warning"),
    (disk(size=3 * 1024**4), {"table": "dos"}, "hw.dos_over_2tib", "error"),
    (disk(size=5 * 1024**4, log_sec=4096), {"table": "dos"}, "hw.dos_over_2tib", "error"),
    (disk(signatures=["linux_raid_member"]), {}, "hw.st_raid_member", "warning"),
    (disk(signatures=["isw_raid_member"]), {}, "hw.st_raid_member", "warning"),
    (disk(signatures=["LVM2_member"]), {}, "hw.st_lvm_member", "warning"),
    (disk("/dev/sdb", bus="usb", hotplug=True), {"device": "/dev/sdb"}, "hw.st_removable_target", "info"),
    (disk("/dev/mmcblk0", bus="sd", hotplug=True), {"device": "/dev/mmcblk0"}, "hw.st_removable_target", "info"),
    (disk(serial=""), {}, "hw.st_no_serial", "info"),
])
def test_storage_findings(device, storage, code, severity):
    m = manifest(storage={"device": device.path, **storage})
    findings = {issue.code: issue.severity for issue in hwplan.validate_plan(m, profile(disks=(device,)))}
    assert findings.get(code) == severity


@pytest.mark.parametrize(("device", "storage", "code"), [
    (disk(size=2 * 1024**4), {"table": "dos"}, "hw.dos_over_2tib"),                 # exactly 2 TiB is addressable
    (disk(size=4 * 1024**4, log_sec=4096), {"table": "dos"}, "hw.dos_over_2tib"),   # 4 TiB with 4Kn
    (disk(log_sec=4096), {}, "hw.bios_4kn"),                                          # 4Kn is fine with UEFI
    (disk(), {}, "hw.nvme_bios"),
    (disk(signatures=["ext4"]), {}, "hw.st_raid_member"),
    (disk(serial="", wwn="x"), {}, "hw.st_no_serial"),
    (disk("/dev/mmcblk0", bus="sd"), {"device": "/dev/mmcblk0"}, "hw.st_removable_target"),      # soldered eMMC is not removable
    (disk(serial=""), {"device_id_kind": "path", "device_id": "pci-0000:00:1d.0|1|x"}, "hw.st_no_serial"),   # already approved
])
def test_storage_findings_that_must_not_fire(device, storage, code):
    m = manifest(storage={"device": device.path, **storage})
    assert code not in codes(m, profile(disks=(device,)))


@pytest.mark.parametrize(("desktop", "binhost", "disk_gib", "swap", "severity"), [
    ("kde", True, 30, 4096, "warning"),     # 30 GiB - 1 GiB ESP - 4 GiB swap = 25 GiB: enough for KDE (24) but below the 40 GiB recommendation
    ("kde", True, 24, 4096, "error"),       # 19 GiB root
    ("kde", True, 60, 4096, None),          # 55 GiB root
    ("kde", False, 60, 4096, "warning"),    # building from source needs 16 GiB more: 40 minimum, 56 recommended, 55 available
    ("minimal", True, 10, 0, "warning"),    # 9 GiB root: minimum 8, recommended 15
    ("minimal", True, 8, 0, "error"),       # 7 GiB root
    ("xfce", True, 40, 4096, None),
    ("gnome", True, 20, 0, "warning"),      # 19 GiB root: minimum 18, recommended 28
])
def test_root_size_rules(desktop, binhost, disk_gib, swap, severity):
    m = manifest(system={"desktop": desktop}, portage={"binhost": binhost}, storage={"swap_mib": swap})
    found = {i.code: i.severity for i in hwplan.validate_plan(m, profile(disks=(disk(size=disk_gib * GIB),)))}
    assert found.get("hw.disk_too_small") == ("error" if severity == "error" else None)
    assert found.get("hw.disk_small") == ("warning" if severity == "warning" else None)


def test_manual_layouts_are_not_judged_on_size():
    m = manifest(storage={"mode": "manual"})
    assert not {"hw.disk_too_small", "hw.disk_small"} & codes(m, profile(disks=(disk(size=2 * GIB),)))


def test_encryption_and_home_reduce_the_root():
    base = manifest(system={"desktop": "xfce"}, storage={"swap_mib": 0})
    luks = manifest(system={"desktop": "xfce"}, storage={"swap_mib": 0, "luks": True, "luks_passphrase": "long-enough-pass"})
    home = manifest(system={"desktop": "xfce"}, storage={"swap_mib": 0, "separate_home": True, "home_mib": 28 * 1024})
    machine = profile(disks=(disk(size=40 * GIB),))
    assert "hw.disk_too_small" not in codes(base, machine) and "hw.disk_too_small" not in codes(luks, machine)
    assert "hw.disk_too_small" in codes(home, machine)


def test_a_disk_that_is_not_there_is_an_error_and_hidden_disks_are_explained():
    m = manifest(storage={"device": "/dev/sdz"})
    issues = {i.code: i for i in hwplan.validate_plan(m, profile())}
    assert issues["hw.st_disk_missing"].severity == "error" and "Choose one of the detected disks" in issues["hw.st_disk_missing"].message
    hidden = profile(disks=(disk("/dev/sdb", bus="usb", hotplug=True, live_medium=True, installable=False),))
    assert "firmware settings" in {i.code: i for i in hwplan.validate_plan(m, hidden)}["hw.st_disk_missing"].message


def test_unknown_disks_are_not_judged_without_a_disk_list_or_for_odd_paths():
    assert "hw.st_disk_missing" not in codes(manifest(storage={"device": "/dev/sdz"}), profile(disks=()))          # no probe data
    assert "hw.st_disk_missing" not in codes(manifest(storage={"device": "/dev/disk/by-id/x"}), profile())          # symlinks are the storage code's business
    assert "hw.st_disk_missing" not in codes(manifest(storage={"device": "/dev/mapper/vg-root"}), profile())


# -- firmware type ------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("live", "requested", "bootloader", "code", "severity"), [
    ("uefi", "bios", "grub", "hw.fw_other_type", "warning"),
    ("bios", "uefi", "grub", "hw.fw_uefi_from_bios", "warning"),
    ("bios", "uefi", "systemd-boot", "hw.fw_uefi_from_bios", "error"),
    ("bios", "uefi", "efi-stub", "hw.fw_uefi_from_bios", "error"),
])
def test_firmware_mismatch_rules(live, requested, bootloader, code, severity):
    m = manifest(storage={"firmware": requested}, system={"bootloader": bootloader})
    found = {i.code: i.severity for i in hwplan.validate_plan(m, profile(firmware=live))}
    assert found.get(code) == severity


def test_matching_firmware_has_no_finding_and_32_bit_uefi_warns():
    assert not {"hw.fw_other_type", "hw.fw_uefi_from_bios"} & codes(manifest(), profile())
    assert not {"hw.fw_other_type", "hw.fw_uefi_from_bios"} & codes(manifest(storage={"firmware": "bios"}), profile(firmware="bios"))
    machine = profile()
    machine["firmware"]["efi_bits"] = 32
    assert "hw.fw_uefi32" in codes(manifest(), machine)


# -- memory, hibernation ---------------------------------------------------------------------------------------

def test_hibernation_rules():
    machine = profile(ram_gib=16)
    ok = manifest(hardware={"hibernate": True}, storage={"swap_mib": 20 * 1024})
    assert not {"hw.hibernate_luks", "hw.hibernate_swap_small", "hw.hibernate_lockdown"} & codes(ok, machine)
    small = manifest(hardware={"hibernate": True}, storage={"swap_mib": 8 * 1024})
    assert "hw.hibernate_swap_small" in codes(small, machine, "error")
    none = manifest(hardware={"hibernate": True}, storage={"swap_mib": 0})
    assert "hw.hibernate_swap_small" in codes(none, machine, "error")
    luks = manifest(hardware={"hibernate": True}, storage={"swap_mib": 20 * 1024, "luks": True, "luks_passphrase": "long-enough-pass"})
    assert "hw.hibernate_luks" in codes(luks, machine, "error")
    assert "hw.hibernate_lockdown" in codes(ok, profile(ram_gib=16, lockdown="integrity"), "warning")
    assert not {"hw.hibernate_luks", "hw.hibernate_swap_small"} & codes(manifest(), machine)


@pytest.mark.parametrize(("ram", "desktop", "expected"), [(3.0, "kde", True), (3.0, "gnome", True), (3.0, "xfce", False), (6.0, "kde", False), (1.5, "kde", False)])
def test_memory_tight_applies_to_heavy_desktops_between_2_and_4_gib(ram, desktop, expected):
    assert ("hw.memory_tight" in codes(manifest(system={"desktop": desktop}), profile(ram_gib=ram))) is expected


# -- power, network ------------------------------------------------------------------------------------------------

def test_two_power_daemons_cannot_be_combined():
    assert "hw.power_conflict" in codes(manifest(hardware={"power": "ppd"}, options={"extra_packages": ["sys-power/tlp"]}), profile(), "error")
    assert "hw.power_conflict" in codes(manifest(hardware={"power": "tlp"}, options={"extra_packages": ["sys-power/power-profiles-daemon"]}), profile(), "error")
    assert "hw.power_conflict" not in codes(manifest(hardware={"power": "auto"}, options={"extra_packages": ["sys-power/tlp"]}), profile())


def test_thermald_is_for_intel():
    assert "hw.thermald_not_intel" in codes(manifest(hardware={"thermald": "yes"}), profile(vendor="amd"))
    assert "hw.thermald_not_intel" not in codes(manifest(hardware={"thermald": "yes"}), profile(vendor="intel"))


def test_wifi_only_machines_need_networkmanager_and_portable_disks_do_not_carry_wifi_passwords():
    wifi_only = profile(wifi=True)
    assert "hw.net_wifi_needs_nm" in codes(manifest(network={"manager": "dhcpcd"}), wifi_only)
    assert "hw.net_wifi_needs_nm" not in codes(manifest(), wifi_only)
    assert "hw.import_profiles_portable" in codes(manifest(hardware={"target": "portable"}, network={"import_live_profiles": True}), profile())
    assert "hw.import_profiles_portable" not in codes(manifest(network={"import_live_profiles": True}), profile())
    assert "hw.net_names_hardware_specific" in codes(manifest(hardware={"target": "portable"}, network={"manager": "netifrc", "interface": "eth0"}), profile())


def test_virtualbox_is_best_effort_and_empty_manual_flags_warn():
    assert "hw.virtualbox_best_effort" in codes(manifest(), profile(hypervisor="virtualbox"))
    assert "hw.cpu_flags_empty" in codes(manifest(hardware={"cpu_flags_mode": "manual"}), profile())


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_every_fixture_validates_without_raising(name):
    m = manifest(storage={"device": next((d["path"] for d in fixture_dict(name)["disks"] if d["installable"]), "/dev/null")})
    assert isinstance(hwplan.validate_plan(m, fixture_dict(name)), list)


# -- blocking issues -------------------------------------------------------------------------------------------

def test_blocking_issues_combine_validation_errors_and_probe_errors():
    broken = fixture_dict("intel-rst-hidden-nvme")
    m = manifest()
    blocking = {issue.code for issue in hwplan.blocking_issues(m, broken)}
    assert {"hw.st_disk_missing", "hw.st_rst_no_disks", "hw.st_no_disk"} <= blocking
    assert all(issue.severity == "error" for issue in hwplan.blocking_issues(m, broken))
    healthy = fixture_dict("thinkpad-intel-nvidia-hybrid")
    m.storage.device = "/dev/nvme0n1"
    assert hwplan.blocking_issues(m, healthy) == []
    m.hardware.hibernate = True
    m.storage.swap_mib = 1024
    assert {issue.code for issue in hwplan.blocking_issues(m, healthy)} == {"hw.hibernate_swap_small"}


def test_probe_errors_that_are_not_installation_blockers_do_not_block():
    machine = profile()
    machine["warnings"] = [{"code": "hw.fw_sb_enabled", "severity": "warning", "message": "x", "action": ""},
                           {"code": "hw.clock_skew", "severity": "error", "message": "The clock is wrong.", "action": "sync_time"}]
    assert [issue.code for issue in hwplan.blocking_issues(manifest(), machine)] == ["hw.clock_skew"]
    assert hwplan.blocking_issues(manifest(), {}) == []
