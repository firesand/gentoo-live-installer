"""The install paths and probes added for the hardware-aware installer, through the fake lab: artifacts, the new probes, the second boot with an
empty NVRAM, the console installer with a boot menu entry, an install across two disks, and Gentoo broken and repaired several times."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.vm.fake_env import EVENT_STREAM, FAILED_STREAM, Lab, guest_rules, hardware_plan_for, plan_record
from tests.vm.fake_rescue import rescue_rules
from tests.vm.helpers import PUBLIC_KEY, load_vm_module
from tests.vm.iso_fixture import write_iso

vm = load_vm_module()
DATA = Path(__file__).resolve().parent / "data"  # files of the first real VM runs

pytestmark = pytest.mark.slow  # real processes and sockets: about a second per scenario


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return Lab(vm, tmp_path, monkeypatch)


def findings(lab) -> dict[str, dict]:
    return {probe["name"]: probe for probe in lab.result()["advisory_findings"]}


def failed(lab) -> dict[str, dict]:
    return {probe["name"]: probe for probe in lab.result()["probes"] if not probe["success"]}


# --- what is copied out of the live system -----------------------------------------------------------------------------

def test_the_daemons_stderr_log_and_the_hardware_plan_are_copied_out_of_the_live_system(lab):
    lab.rules = guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios", "--keep") == 0
    run = lab.run_dir()
    assert (run / "daemon-stderr.log").read_text() == "daemon stderr\n" and json.loads((run / "hardware-plan.json").read_text())["plan"]["schema"] == 1
    artifacts = json.loads((run / "install-artifacts.json").read_text())
    assert artifacts["daemon-stderr.log"]["from"] == "/var/log/gentoo-installerd.log" and artifacts["hardware-plan.json"]["from"] == vm.HARDWARE_PLAN_PATH
    assert lab.result()["install"]["artifacts"] == artifacts and lab.result()["install"]["mode"] == "cli"


def test_the_logs_are_fetched_even_when_the_install_failed(lab):
    lab.rules = guest_rules(vm, "bios", install=FAILED_STREAM)
    assert lab.run("--scenario", "bios") == 1
    run = lab.run_dir()
    assert (run / "daemon-stderr.log").read_text() == "daemon stderr\n" and "emerge failed: simulated" in lab.result()["error"]


def test_a_missing_log_or_plan_is_recorded_not_an_error_of_its_own(lab):
    lab.rules = [{"user": "^root$", "match": r"^cat /var/(?:log/gentoo-installerd\.log|lib/gentoo-installer/hardware-plan\.json)$", "rc": 1,
                  "stdout": "cat: No such file or directory\n"}] + guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios") == 0
    run = lab.run_dir()
    assert not (run / "daemon-stderr.log").exists()
    assert json.loads((run / "install-artifacts.json").read_text())["hardware-plan.json"] == {"missing": vm.HARDWARE_PLAN_PATH, "exit": 1}
    assert "hardware-plan-file" in findings(lab) and "hardware-plan.json" in findings(lab)["hardware-plan-file"]["error"]


def test_the_plan_file_the_daemon_really_writes_passes_the_plan_checks_in_a_strict_run(lab):
    """H1: the state file is {version, fingerprint, created, plan}; the first real runs called it 'not a plan'."""
    real = (DATA / "hardware-plan-uefi-nvme-luks-openrc.json").read_text()
    lab.rules = guest_rules(vm, "bios", sudo=True, extra=[{"user": "^root$", "match": r"^cat /var/lib/gentoo-installer/hardware-plan\.json$", "stdout": real}])
    assert lab.run("--scenario", "bios", "--strict-probes") == 0
    probes = {probe["name"]: probe for probe in lab.result()["probes"]}
    assert probes["hardware-plan-file"]["success"] and probes["hardware-plan-file"]["output"].startswith('["CPU: QEMU Virtual CPU')
    assert lab.result()["advisory_findings"] == []


def test_a_plan_file_without_the_record_around_it_is_still_accepted(lab):
    bare = hardware_plan_for(vm, "bios")
    lab.rules = guest_rules(vm, "bios", sudo=True, extra=[{"user": "^root$", "match": r"^cat /var/lib/gentoo-installer/hardware-plan\.json$", "stdout": json.dumps(bare)}])
    assert lab.run("--scenario", "bios", "--strict-probes") == 0
    assert {probe["name"]: probe for probe in lab.result()["probes"]}["hardware-plan-file"]["success"]


def test_a_plan_with_intel_microcode_inside_the_daemons_record_fails_the_run(lab):
    plan = hardware_plan_for(vm, "bios")
    plan["early_atoms"] = ["sys-firmware/intel-microcode"]
    lab.rules = guest_rules(vm, "bios", extra=[{"user": "^root$", "match": r"^cat /var/lib/gentoo-installer/hardware-plan\.json$", "stdout": json.dumps(plan_record(plan))}])
    assert lab.run("--scenario", "bios") == 1
    assert "hardware-plan-file: the plan installs Intel microcode in a virtual machine" in lab.result()["error"]


def test_a_missing_hardware_plan_fails_a_strict_run(lab):
    lab.rules = [{"user": "^root$", "match": r"^cat /var/lib/gentoo-installer/hardware-plan\.json$", "rc": 1}] + guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios", "--strict-probes") == 1
    assert "hardware-plan-file" in lab.result()["error"]


# --- what the installer says about itself -------------------------------------------------------------------------------------

def test_a_success_that_is_not_bootable_is_not_a_success(lab):
    stream = [{"type": "stage", "stage": "storage", "data": {"status": "completed"}}, {"type": "completed", "data": {"success": True, "bootable": False}, "message": "done"}]
    lab.rules = guest_rules(vm, "bios", install=stream)
    assert lab.run("--scenario", "bios") == 1
    assert "success but not a bootable system" in lab.result()["error"]


def last_result(lab) -> dict:
    return json.loads((lab.run_dirs()[-1] / "result.json").read_text())


def test_the_installers_reason_for_a_degraded_part_is_shown_as_it_sent_it_and_a_strict_run_fails(lab):
    """H3: the completed event of the first real run (a part that could not be installed) is the one the harness judges."""
    completed = json.loads((DATA / "completed-uefi-nvme-luks-openrc.json").read_text())
    reason = completed["data"]["degraded"][0]
    stream = EVENT_STREAM[:-1] + [completed]
    lab.rules = guest_rules(vm, "bios", sudo=True, install=stream)
    assert lab.run("--scenario", "bios") == 0  # a finding: the system installed and boots
    finding = findings(lab)["install-degraded"]
    assert reason in finding["error"] and json.loads(finding["output"])["degraded"] == [reason]
    assert lab.result()["install"]["completed"]["degraded"] == [reason]
    lab.rules = guest_rules(vm, "bios", sudo=True, install=stream)
    assert lab.run("--scenario", "bios", "--strict-probes") == 1  # an optional part that is missing is a real problem
    # nothing else fails: the one problem is what the installer said
    assert last_result(lab)["error"] == f"Target verification failed: install-degraded: the installer finished with problems it did not treat as fatal: {reason}"


def test_a_long_reason_of_the_installer_is_not_cut_short(lab):
    reason = ("app-misc/hwmonux could not be installed: dev-python/pyside-6.11.2 was stopped because the machine ran out of memory while it compiled "
              "QtOpenGL (the compiler was killed). " + "The build log inside the new system has the details. " * 12 + "End of the reason.")
    stream = EVENT_STREAM[:-1] + [{"type": "completed", "message": "done", "data": {"success": True, "bootable": True, "degraded": [reason], "warnings": []}}]
    lab.rules = guest_rules(vm, "bios", install=stream)
    assert lab.run("--scenario", "bios") == 0
    finding = findings(lab)["install-degraded"]
    assert len(reason) > 500 and json.loads(finding["output"])["degraded"] == [reason] and finding["error"].endswith("End of the reason.")


def test_optional_parts_that_failed_and_cleanup_warnings_are_findings(lab):
    stream = EVENT_STREAM[:-1] + [{"type": "completed", "message": "done", "data": {
        "success": True, "bootable": True, "degraded": ["NVIDIA driver failed to build"], "warnings": ["Could not unmount /mnt/gentoo"]}}]
    lab.rules = guest_rules(vm, "bios", install=stream)
    assert lab.run("--scenario", "bios") == 0
    finding = findings(lab)["install-degraded"]
    assert "NVIDIA driver failed to build" in finding["error"] and "warning: Could not unmount /mnt/gentoo" in finding["error"]
    assert lab.result()["install"]["completed"]["degraded"] == ["NVIDIA driver failed to build"]
    lab.rules = guest_rules(vm, "bios", install=stream)
    assert lab.run("--scenario", "bios", "--strict-probes") == 1


# --- the new probes -----------------------------------------------------------------------------------------------------------

def test_every_new_probe_runs_on_a_uefi_install_and_passes_on_a_good_guest(lab):
    lab.rules = guest_rules(vm, "openrc")
    assert lab.run("--scenario", "openrc") == 0
    names = {probe["name"] for probe in lab.result()["probes"]}
    assert {"receipt", "efi-fallback", "world-resolve", "world-drift", "hardware-plan", "hardware-plan-file", "install-degraded"} <= names
    assert all(probe["success"] for probe in lab.result()["probes"] if probe["name"] in {"receipt", "efi-fallback", "world-resolve", "hardware-plan", "hardware-plan-file"})
    world = [call["remote"] for call in lab.ssh_calls() if "@world" in call["remote"]]
    assert world == ["sudo -S -p '' emerge --color=n -pvuDN @world"]


def test_the_world_probe_can_be_skipped_and_has_its_own_timeout(lab):
    lab.rules = guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios", "--no-world-probe") == 0
    assert not any("@world" in call["remote"] for call in lab.ssh_calls())
    assert "world-resolve" not in {probe["name"] for probe in lab.result()["probes"]}


def test_a_world_that_does_not_resolve_fails_the_run(lab):
    lab.rules = guest_rules(vm, "bios", extra=[{"user": "^vmtest$", "match": r"emerge --color=n", "rc": 1,
                                                 "stdout": "The following USE changes are necessary to proceed:\n>=media-libs/libpng-1.6 -apng\n"}])
    assert lab.run("--scenario", "bios") == 1
    assert "world-resolve: the configuration cannot satisfy the world" in lab.result()["error"]


def test_drift_is_information_that_never_fails_a_job_not_even_a_strict_one(lab):
    """H2: the first real run (4 upgrades, 3 new, 5 reinstalls) came from a tree newer than the stage3, not from the installer."""
    real = (DATA / "emerge-world-uefi-nvme-luks-openrc.log").read_text()
    lab.rules = guest_rules(vm, "bios", sudo=True, extra=[{"user": "^vmtest$", "match": r"emerge --color=n", "stdout": real}])
    assert lab.run("--scenario", "bios", "--strict-probes") == 0
    result = lab.result()
    assert result["advisory_findings"] == []
    drift = result["world_drift"]
    assert drift["counts"] == {"upgrades": 4, "new": 3, "reinstalls": 5}
    assert "net-misc/curl-8.23.0-1" in drift["upgrades"] and "app-arch/zstd-1.5.7-r1-10" in drift["reinstalls"] and "net-libs/nghttp2-1.70.0-3" in drift["new"]
    probes = {probe["name"]: probe for probe in result["probes"]}
    assert probes["world-resolve"]["success"] and probes["world-drift"]["success"] and probes["world-drift"]["data"] == drift
    assert "information only" in probes["world-drift"]["output"] and "data" not in probes["world-resolve"]  # only a probe that collected something has data


def test_a_world_without_drift_says_so_in_the_result(lab):
    lab.rules = guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios") == 0
    assert lab.result()["world_drift"]["counts"] == {"upgrades": 0, "new": 0, "reinstalls": 0}


def test_no_world_probe_means_no_drift_record(lab):
    lab.rules = guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios", "--no-world-probe") == 0
    assert lab.result()["world_drift"] is None


def test_a_receipt_that_contradicts_the_manifest_fails_the_run(lab):
    wrong = json.loads(guest_receipt(lab, "bios"))
    wrong["bootloader"] = "systemd-boot"
    lab.rules = guest_rules(vm, "bios", extra=[{"user": "^vmtest$", "match": r"^cat /etc/gentoo-live-installer/install\.json$", "stdout": json.dumps(wrong)}])
    assert lab.run("--scenario", "bios") == 1
    assert "receipt: bootloader is 'systemd-boot'" in lab.result()["error"]


def guest_receipt(lab, name: str) -> str:
    from tests.vm.fake_env import receipt_for

    return receipt_for(vm, name)


def test_a_plan_with_intel_microcode_for_a_virtual_machine_fails_the_run(lab):
    plan = hardware_plan_for(vm, "bios")
    plan["early_atoms"] = ["sys-firmware/intel-microcode"]
    lab.rules = guest_rules(vm, "bios", extra=[{"user": "^root$", "match": r"^cat /var/lib/gentoo-installer/hardware-plan\.json$", "stdout": json.dumps(plan)}])
    assert lab.run("--scenario", "bios") == 1
    assert "hardware-plan-file: the plan installs Intel microcode in a virtual machine" in lab.result()["error"]


def test_microcode_in_the_installed_system_fails_the_run(lab):
    lab.rules = guest_rules(vm, "bios", extra=[{"user": "^vmtest$", "match": r"^sh -c .* sh sys-firmware/intel-microcode",
                                                 "stdout": "installed sys-firmware/intel-microcode\nabsent app-emulation/qemu-guest-agent\n"}])
    assert lab.run("--scenario", "bios") == 1
    assert "hardware-plan: Intel microcode is installed in a virtual machine" in lab.result()["error"]


NO_GUEST_AGENT = {"user": "^vmtest$", "match": r"^sh -c .* sh sys-firmware/intel-microcode", "stdout": "absent sys-firmware/intel-microcode\nabsent app-emulation/qemu-guest-agent\n"}


def test_a_guest_tool_the_plan_wants_must_be_installed(lab):
    lab.rules = guest_rules(vm, "laptop-smbios", extra=[NO_GUEST_AGENT])
    assert lab.run("--scenario", "laptop-smbios") == 1
    assert "the plan asked for app-emulation/qemu-guest-agent" in lab.result()["error"]


def test_a_guest_tool_that_failed_is_fine_when_the_installer_said_so_itself(lab):
    stream = EVENT_STREAM[:-1] + [{"type": "completed", "message": "done", "data": {"success": True, "degraded": ["app-emulation/qemu-guest-agent failed to build"]}}]
    lab.rules = guest_rules(vm, "laptop-smbios", install=stream, extra=[NO_GUEST_AGENT])
    assert lab.run("--scenario", "laptop-smbios") == 0  # the degraded install is a finding; the missing package is explained by it
    assert "install-degraded" in findings(lab)


def test_an_encrypted_install_with_a_swap_partition_fails_the_run(lab):
    lab.rules = guest_rules(vm, "uefi-luks", extra=[{"user": "^vmtest$", "match": r"^swapon --noheadings", "stdout": "/dev/vda3 partition 536870912\n"}])
    lab.qemu_config.update(serial_text_target=PROMPT, unlock_line=vm.TEST_LUKS_PASSPHRASE)
    assert lab.run("--scenario", "uefi-luks") == 1
    assert "swap: an encrypted install has a raw swap partition (/dev/vda3)" in lab.result()["error"]


def test_a_missing_fallback_boot_file_fails_the_run(lab):
    lab.rules = guest_rules(vm, "openrc", extra=[{"user": "^vmtest$", "match": r"BOOTX64\.EFI$", "rc": 1, "stdout": json.dumps({"path": "/efi/EFI/BOOT/BOOTX64.EFI", "error": "No such file"})}])
    assert lab.run("--scenario", "openrc") == 1
    assert "efi-fallback: /efi/EFI/BOOT/BOOTX64.EFI is not readable on the EFI partition" in lab.result()["error"]


def test_a_broken_sudo_makes_the_fallback_check_a_finding_that_only_a_strict_run_fails(lab):
    lab.rules = guest_rules(vm, "openrc", extra=[{"user": "^vmtest$", "match": r"BOOTX64\.EFI$", "rc": 1, "stdout": "sudo: a password is required\n"}])
    assert lab.run("--scenario", "openrc") == 0
    assert "efi-fallback" in findings(lab) and "cannot use sudo" in findings(lab)["efi-fallback"]["error"]


def test_a_bios_install_has_no_fallback_probe(lab):
    lab.rules = guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios") == 0
    assert "efi-fallback" not in {probe["name"] for probe in lab.result()["probes"]}


# --- the second boot with an empty NVRAM -----------------------------------------------------------------------------------------

PROMPT = "Please enter passphrase for disk root (gentoo-root): (press TAB for no echo)"
REGISTERED = 'BdsDxe: starting Boot0008 "Gentoo" from HD(1,GPT,AAAA,0x800,0x200000)/\\EFI\\Gentoo\\grubx64.efi\n'
FALLBACK = 'BdsDxe: starting Boot0002 "UEFI QEMU HARDDISK" from HD(1,GPT,AAAA,0x800,0x200000)/\\EFI\\BOOT\\BOOTX64.EFI\n'


def test_the_installed_disk_boots_a_second_time_with_an_empty_nvram_when_asked(lab):
    lab.rules = guest_rules(vm, "openrc")
    lab.qemu_config = {"exit_after": None, "serial_rules": [{"phase": "target", "boot": 1, "text": REGISTERED}, {"phase": "target", "boot": 2, "text": FALLBACK}]}
    assert lab.run("--scenario", "openrc", "--fresh-nvram-check", "--keep") == 0
    run = lab.run_dir()
    fresh = lab.result()["fresh_nvram"]
    assert fresh["nvram"] == "empty" and [probe["success"] for probe in fresh["probes"]] == [True, True]
    assert fresh["firmware_boot_choices"][-1]["path"].endswith("\\EFI\\BOOT\\BOOTX64.EFI")
    assert (run / "OVMF_VARS.before-target-fresh.fd").exists() and (run / "serial-target-fresh.log").is_file()
    assert "if=pflash,format=raw,file=" + str(run / "OVMF_VARS.fd") in json.loads((run / "fake-qemu-argv-target-2.json").read_text())


def test_a_second_boot_that_started_the_registered_entry_is_not_a_fallback_proof(lab):
    lab.rules = guest_rules(vm, "openrc")
    lab.qemu_config = {"exit_after": None, "serial_text_target": REGISTERED}
    assert lab.run("--scenario", "openrc", "--fresh-nvram-check") == 1
    assert "not the removable fallback" in lab.result()["error"] and "\\EFI\\Gentoo\\grubx64.efi" in lab.result()["error"]


def test_the_empty_nvram_check_unlocks_an_encrypted_disk_again(lab):
    lab.rules = guest_rules(vm, "uefi-luks")
    lab.qemu_config.update(serial_text_target=PROMPT, unlock_line=vm.TEST_LUKS_PASSPHRASE)
    assert lab.run("--scenario", "uefi-luks", "--fresh-nvram-check", "--keep") == 0
    run = lab.run_dir()
    assert (run / "luks-unlock-receipt.json").is_file() and (run / "luks-unlock-receipt-fresh.json").is_file()
    assert lab.result()["fresh_nvram"]["luks_unlock"]["method"] == "serial"


def test_a_bios_install_has_no_empty_nvram_to_check(lab):
    lab.rules = guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios", "--fresh-nvram-check") == 0 and lab.result()["fresh_nvram"] is None


def test_a_scenario_that_already_boots_with_an_empty_nvram_needs_no_second_boot(lab):
    lab.rules = guest_rules(vm, "uefi-fresh-nvram")
    assert lab.run("--scenario", "uefi-fresh-nvram", "--fresh-nvram-check") == 0 and lab.result()["fresh_nvram"] is None


# --- the console installer and the boot menu ---------------------------------------------------------------------------------------

MENU = """\
set default=0
menuentry 'Gentoo installer and rescue desktop' {
\tlinux /boot/gentoo quiet root=live:CDLABEL=LIVE cdroot
\tinitrd /boot/gentoo.igz
}
menuentry 'Text console only (repair, headless)' {
\tlinux /boot/gentoo quiet root=live:CDLABEL=LIVE cdroot nox dokeymap
\tinitrd /boot/gentoo.igz
}
submenu 'More options' {
\tmenuentry 'Serial console ttyS0 115200 (text only)' {
\t\tlinux /boot/gentoo root=live:CDLABEL=LIVE cdroot nox console=tty0 console=ttyS0,115200n8
\t\tinitrd /boot/gentoo.igz
\t}
}
"""
TEXT_ARGS = "BOOT_IMAGE=/boot/gentoo quiet root=live:CDLABEL=LIVE cdroot nox dokeymap"


def text_lab(lab, *, extra: list | None = None, scenario: str = "text-install", cmdline: str = TEXT_ARGS, install_rc: int = 0, install=None) -> None:
    write_iso(lab.iso, {"/boot/grub/grub.cfg": MENU.encode(), "/boot/gentoo": b"KERNEL", "/boot/gentoo.igz": b"INITRD"})
    root = {"user": "^root$"}
    text = r"^gentoo-installer-text --manifest \S+"
    lab.rules = [
        {**root, "match": r"^cat /proc/cmdline$", "stdout": cmdline + "\n"},
        *(extra or []),
        {**root, "match": text + r" --dry-run$", "stdout": "Plan: erase /dev/vda\n"},
        {**root, "match": text + r" --yes --json-events$", "rc": 3, "stdout": json.dumps({"type": "error", "error": "install.invalid_manifest", "message": "confirmation missing"}) + "\n"},
        {**root, "match": text + r" --yes --confirm-device /dev/harness-wrong-device --json-events$", "rc": 3,
         "stdout": json.dumps({"type": "error", "error": "refused", "message": "wrong device"}) + "\n"},
        {**root, "match": text + r" --yes --confirm-device /dev/vda --json-events$", "rc": install_rc, "events": install or EVENT_STREAM},
    ] + guest_rules(vm, scenario)


def test_the_text_installer_is_started_on_the_text_entry_with_a_device_confirmation(lab):
    text_lab(lab)
    assert lab.run("--scenario", "text-install", "--keep") == 0
    run, result = lab.run_dir(), lab.result()
    live = json.loads((run / "fake-qemu-argv-live-1.json").read_text())
    assert live[live.index("-kernel") + 1] == str(run / "boot-entry" / "kernel") and (run / "boot-entry" / "kernel").read_bytes() == b"KERNEL"
    assert (run / "boot-entry" / "initrd").read_bytes() == b"INITRD" and live[live.index("-initrd") + 1] == str(run / "boot-entry" / "initrd")
    assert live[live.index("-append") + 1] == "quiet root=live:CDLABEL=LIVE cdroot nox dokeymap"
    assert "media=cdrom" in " ".join(live), "the ISO stays attached: the entry's root=live:CDLABEL=... needs it"
    assert result["boot_entry"]["entry"] == "Text console only (repair, headless)" and result["install"]["mode"] == "text"
    assert result["install"]["text_installer"]["wrong-confirmation"]["exit"] == 3 and result["install"]["text_installer"]["dry_run"]["exit"] == 0
    remote = [call["remote"] for call in lab.ssh_calls() if call["user"] == "root"]
    assert not any(text.startswith("gentoo-installerd") for text in remote), "the daemon's own command line is not used in text mode"
    assert remote.index(next(r for r in remote if r.endswith("--dry-run"))) < remote.index(next(r for r in remote if "--confirm-device /dev/vda" in r))


def test_a_device_that_is_not_the_manifests_disk_is_never_confirmed(lab):
    text_lab(lab)
    assert lab.run("--scenario", "text-install") == 0
    confirmations = [call["remote"].split("--confirm-device ")[1].split()[0] for call in lab.ssh_calls() if "--confirm-device" in call["remote"]]
    assert confirmations == ["/dev/harness-wrong-device", "/dev/vda"]


def test_a_text_installer_that_does_not_refuse_a_missing_confirmation_fails_the_run(lab):
    text_lab(lab, extra=[{"user": "^root$", "match": r"^gentoo-installer-text --manifest \S+ --yes --json-events$", "rc": 0, "stdout": ""}])
    assert lab.run("--scenario", "text-install") == 1
    error = lab.result()["error"]
    assert "with a missing confirmation exited with status 0, not 3 (refused)" in error and "text-installer-missing-confirmation.log" in error
    assert not any("--confirm-device /dev/vda" in call["remote"] for call in lab.ssh_calls()), "nothing was installed"


def test_a_text_installer_that_runs_commands_before_refusing_fails_the_run(lab):
    refusing = json.dumps({"type": "log", "message": "$ mkfs.ext4 -F /dev/vda2"}) + "\n"
    text_lab(lab, extra=[{"user": "^root$", "match": r"--confirm-device /dev/harness-wrong-device", "rc": 3, "stdout": refusing}])
    assert lab.run("--scenario", "text-install") == 1
    assert "ran mkfs.ext4 -F /dev/vda2" in lab.result()["error"]


@pytest.mark.parametrize(("code", "words"), [(4, "could not reach the installer daemon"), (1, "reported that the installation failed"), (2, "rejected the command line or the manifest"),
                                             (130, "was interrupted")])
def test_the_exit_status_of_the_text_installer_is_explained(lab, code, words):
    text_lab(lab, install_rc=code, install=[])
    assert lab.run("--scenario", "text-install") == 1
    assert f"gentoo-installer-text {words} (exit {code})" in lab.result()["error"]


def test_a_dry_run_that_fails_stops_before_anything_is_confirmed(lab):
    text_lab(lab, extra=[{"user": "^root$", "match": r"--dry-run$", "rc": 2, "stdout": "manifest rejected\n"}])
    assert lab.run("--scenario", "text-install") == 1
    assert "gentoo-installer-text --dry-run rejected the command line or the manifest (exit 2)" in lab.result()["error"]


def test_a_live_system_that_was_not_started_with_the_entrys_arguments_fails_the_run(lab):
    text_lab(lab, cmdline="BOOT_IMAGE=/boot/gentoo quiet root=live:CDLABEL=LIVE cdroot")
    assert lab.run("--scenario", "text-install") == 1
    assert "was not started with the boot menu entry" in lab.result()["error"] and "nox, dokeymap" in lab.result()["error"]


def test_a_boot_entry_can_be_chosen_for_any_scenario_and_listed(lab, capsys):
    text_lab(lab, cmdline="BOOT_IMAGE=/boot/gentoo root=live:CDLABEL=LIVE cdroot nox console=tty0 console=ttyS0,115200n8", scenario="bios")
    lab.rules = [rule for rule in lab.rules if "gentoo-installer-text" not in rule.get("match", "")]
    assert lab.run("--scenario", "bios", "--boot-entry", "serial") == 0
    live = json.loads((lab.run_dir() / "fake-qemu-argv-live-1.json").read_text())
    assert live[live.index("-append") + 1].endswith("console=tty0 console=ttyS0,115200n8")
    assert lab.result()["boot_entry"]["entry"] == "More options > Serial console ttyS0 115200 (text only)"
    assert vm.main(["--list-boot-entries", "--iso", str(lab.iso)]) == 0
    out = capsys.readouterr().out
    assert "Gentoo installer and rescue desktop" in out and "More options > Serial console ttyS0 115200 (text only)" in out and "nox dokeymap" in out


def test_an_unknown_boot_entry_stops_before_a_disk_is_created(lab):
    text_lab(lab)
    assert lab.run("--scenario", "bios", "--boot-entry", "nonsense") == 1
    assert "--boot-entry nonsense: no boot menu entry matches" in lab.result()["error"]
    assert not lab.qemu_img_log.exists() and not list(lab.run_dir().glob("qemu-*.log"))


def test_an_entry_without_a_desktop_cannot_serve_a_graphical_install(lab):
    text_lab(lab, scenario="openrc")
    assert lab.run("--scenario", "openrc", "--boot-entry", "text", "--install-mode", "gui") == 1
    assert "starts no desktop (nox), but this run needs the graphical session" in lab.result()["error"]


def test_an_iso_that_is_not_an_iso_cannot_be_asked_for_entries(lab, capsys):
    with pytest.raises(SystemExit) as raised:
        vm.main(["--list-boot-entries", "--iso", str(lab.iso)])
    assert raised.value.code == 2 and "not an ISO 9660" in capsys.readouterr().err


# --- an install across two disks, next to a disk that must survive --------------------------------------------------------------------

def test_a_manual_layout_across_two_disks_is_partitioned_installed_and_checked_where_it_landed(lab):
    lab.rules = guest_rules(vm, "uefi-manual-two-disks")
    assert lab.run("--scenario", "uefi-manual-two-disks", "--keep") == 0
    run, result = lab.run_dir(), lab.result()
    storage = json.loads((run / "fake-guest" / "manifest.json").read_text())["storage"]
    assert storage["mode"] == "manual" and storage["device"] == "/dev/vda" and storage["additional_device_ids"] == ["GENTOO-VM-EXTRA-1"]
    assert [(row["device"], row["mountpoint"], row["filesystem"]) for row in storage["partitions"]] == [
        ("/dev/vda1", "/efi", "vfat"), ("/dev/vda2", "/", "ext4"), ("/dev/vdb2", "/home", "ext4"), ("/dev/vdb1", "", "swap")]
    guard = [json.loads(call["remote"].rsplit("'", 2)[-2]) for call in lab.ssh_calls() if "vm_disk_guard" in call["remote"]]
    assert [disk["role"] for disk in guard[0]["disks"]] == ["target", "data-1", "decoy-2"]
    tools = [call["remote"] for call in lab.ssh_calls() if "partition-manual '{" in call["remote"]]
    assert [json.loads(text.rsplit("'", 2)[-2])["role"] for text in tools] == ["target", "data-1"] and set(result["decoys"]) == {"decoy-2"}
    assert "layout" in {probe["name"] for probe in result["probes"]} and all(probe["success"] for probe in result["probes"] if probe["name"] == "layout")
    argv = json.loads((run / "fake-qemu-argv-target-1.json").read_text())
    assert any("extra-1.img" in part for part in argv) and any("extra-2.img" in part for part in argv)


def test_a_layout_that_landed_on_the_wrong_disk_fails_the_run(lab):
    wrong = "/ /dev/vda2\n/efi /dev/vda1\n/home /dev/vda3\n---\n/dev/vdb1\n"
    lab.rules = guest_rules(vm, "uefi-manual-two-disks", extra=[{"user": "^vmtest$", "match": r"^findmnt -rno TARGET,SOURCE", "stdout": wrong}])
    assert lab.run("--scenario", "uefi-manual-two-disks") == 1
    assert "layout: /home is mounted from /dev/vda3, the layout says /dev/vdb2" in lab.result()["error"]


def test_the_decoy_of_an_install_across_two_disks_is_checked_by_its_own_image(lab):
    lab.rules = guest_rules(vm, "uefi-manual-two-disks", extra=[
        {"user": "^vmtest$", "match": r"^hostname$", "stdout": "gentoo-vm-uefi-manual-two-disks\n", "write": {"path": "extra-2.img", "text": "scribbled"}}])
    assert lab.run("--scenario", "uefi-manual-two-disks") == 1
    assert "decoy image extra-2.img changed" in lab.result()["error"]


def test_the_data_disk_may_change_but_the_decoy_may_not_be_confused_with_it(lab):
    lab.rules = guest_rules(vm, "uefi-manual-two-disks", extra=[
        {"user": "^vmtest$", "match": r"^hostname$", "stdout": "gentoo-vm-uefi-manual-two-disks\n", "write": {"path": "extra-1.img", "text": "the install wrote /home here"}}])
    assert lab.run("--scenario", "uefi-manual-two-disks") == 0


def test_a_manifest_across_two_disks_is_valid_and_names_the_approval_of_the_second_disk():
    manifest = vm.build_manifest(vm.SCENARIO_MAP["uefi-manual-two-disks"], PUBLIC_KEY, data_disks=[("/dev/nvme1n1", "GENTOO-VM-EXTRA-1")])
    assert manifest.validate() == []
    assert [(row.device, row.mountpoint) for row in manifest.storage.partitions][-2:] == [("/dev/nvme1n1p2", "/home"), ("/dev/nvme1n1p1", "")]
    assert manifest.storage.additional_device_ids == ["GENTOO-VM-EXTRA-1"]
    alone = vm.build_manifest(vm.SCENARIO_MAP["uefi-manual-two-disks"], PUBLIC_KEY)
    assert alone.validate() == [] and alone.storage.additional_device_ids == [] and len(alone.storage.partitions) == 2


# --- Gentoo, broken and repaired three times ---------------------------------------------------------------------------------------------

def gentoo_rescue_rules(lab, **options) -> None:
    root = {"user": "^root$"}
    break_rule = {**root, "match": r"rescue-break '\{", "guest_tool": True, "remove": ["repaired"]}
    broken = {"user": "^vmtest$", "match": "^true$", "rc": 255, "requires": "rescue-break.json", "absent": "repaired"}
    lab.rules = [break_rule, broken, *rescue_rules("gentoo", mode="all", typed="vda2", **options), *guest_rules(vm, "rescue-gentoo")]


def test_gentoo_is_broken_and_repaired_once_per_damage_and_verified_after_each_repair(lab):
    gentoo_rescue_rules(lab)
    assert lab.run("--scenario", "rescue-gentoo", "--broken-timeout", "1") == 0
    result = lab.result()
    cycles = result["rescue"]["cycles"]
    assert [cycle["break"] for cycle in cycles] == ["grub-cfg", "initramfs", "fstab-uuid"] and result["rescue"]["break"] == "fstab-uuid"
    assert all(cycle["repair"]["outcome"]["outcome"] == "success" for cycle in cycles)
    assert cycles[0]["broken"]["proof"] == "no SSH" and cycles[1]["broken"]["proof"] == "no SSH" and cycles[2]["broken"]["proof"] == "findmnt --verify"
    assert {"probes_before_break", "probes_after_grub-cfg", "probes_after_initramfs", "probes"} <= set(result)
    assert all(probe["success"] for probe in result["probes"] if not probe["advisory"])
    steps = [call["remote"] for call in lab.ssh_calls() if call["user"] == "root" and "gentoo-rescue" in call["remote"]]
    assert sum(1 for step in steps if " run r1a2b3 " in step) == 3 and all("--typed vda2" in step for step in steps if " run r1a2b3 " in step)
    assert all((lab.run_dir() / f"rescue-{mode}-audit.txt").is_file() for mode in ("grub-cfg", "initramfs", "fstab-uuid"))
    assert result["rescue"]["repair"]["system"]["os"] == "Gentoo Linux"


def test_the_repair_of_one_damage_is_not_taken_for_the_repair_of_the_next(lab):
    gentoo_rescue_rules(lab)
    assert lab.run("--scenario", "rescue-gentoo", "--broken-timeout", "1", "--rescue-break", "grub-cfg,initramfs") == 0
    assert [cycle["break"] for cycle in lab.result()["rescue"]["cycles"]] == ["grub-cfg", "initramfs"]
    assert [cycle["broken"]["proof"] for cycle in lab.result()["rescue"]["cycles"]] == ["no SSH", "no SSH"], "each damage was proven again after the earlier repair"


def test_a_repair_with_the_command_line_can_be_asked_for_on_any_scenario(lab):
    root = {"user": "^root$"}
    lab.rules = [{**root, "match": r"rescue-break '\{", "guest_tool": True},
                 {"user": "^vmtest$", "match": "^true$", "rc": 255, "requires": "rescue-break.json", "absent": "repaired"},
                 *rescue_rules("gentoo", mode="grub-cfg"), *guest_rules(vm, "bios")]
    assert lab.run("--scenario", "bios", "--rescue-break", "grub-cfg", "--rescue-repair", "--broken-timeout", "1") == 0
    repair = lab.result()["rescue"]["repair"]
    assert repair["tool"] == "gentoo-rescue" and repair["action"] == "repair.recommended" and repair["transport"] == "auto"
    assert not any("--rescue-command" in call["remote"] for call in lab.ssh_calls())


def test_the_transport_and_the_action_can_be_chosen(lab):
    root = {"user": "^root$"}
    lab.rules = [{**root, "match": r"rescue-break '\{", "guest_tool": True},
                 {"user": "^vmtest$", "match": "^true$", "rc": 255, "requires": "rescue-break.json", "absent": "repaired"},
                 *rescue_rules("gentoo", mode="grub-cfg"), *guest_rules(vm, "bios")]
    assert lab.run("--scenario", "bios", "--rescue-break", "grub-cfg", "--rescue-repair", "--rescue-transport", "local", "--broken-timeout", "1") == 0
    scans = [call["remote"] for call in lab.ssh_calls() if call["remote"].endswith("--json scan")]
    assert scans == ["gentoo-rescue --local --json scan"]
