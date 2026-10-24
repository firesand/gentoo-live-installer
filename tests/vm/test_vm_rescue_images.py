"""Rescue scenarios on distribution cloud images (rescue-debian, -ubuntu, -fedora, -arch) through the fake lab.

Real QEMU, real images and the real live ISO are needed to prove that anything boots; what is tested here is the harness: which machine
starts when and how, what is asserted about the serial console, the overlay and the base image, what is kept, and that a failure says what failed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.vm.fake_env import Lab
from tests.vm.fake_rescue import BROKEN_GRUB, PANIC, cloud_image_serial_rules, console, rescue_rules
from tests.vm.helpers import load_vm_module

vm = load_vm_module()

pytestmark = pytest.mark.slow  # real processes and sockets: several seconds per scenario

FAST = ("--login-timeout", "5", "--broken-timeout", "2")


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_QEMU_IMG_VSIZE", str(3 * 2**30))
    return Lab(vm, tmp_path, monkeypatch)


@pytest.fixture
def images(tmp_path):
    folder = tmp_path / "images"
    folder.mkdir()
    for key, image in vm.vm_rescue.IMAGES.items():
        (folder / image.filename).write_bytes(key.encode() * (1 << 20))  # a few MiB: the first MiB is what gets checksummed
    return folder


def live_rules(os_id: str, **options) -> list[dict]:
    root = {"user": "^root$"}
    return [{**root, "match": "^true$", "rc": 0}, {**root, "match": "vm_disk_guard", "guard": True},
            {**root, "match": r"(?:rescue-break|disk-hash) '\{", "guest_tool": True}, {**root, "match": "^poweroff$", "shutdown_vm": "live"},
            *rescue_rules(os_id, **options)]


def prepare(lab, os_id: str, *, prompt: str | None = None, serial: list | None = None, **options) -> None:
    lab.rules = live_rules(os_id, **options)
    lab.qemu_config = {"exit_after": None, "serial_rules": serial if serial is not None else cloud_image_serial_rules(prompt or f"{os_id} login: ")}


def run(lab, images, scenario: str, *extra: str) -> int:
    return lab.run("--scenario", scenario, "--rescue-images", str(images), *FAST, *extra)


def argv_of(run_dir: Path, phase: str, number: int) -> list[str]:
    return json.loads((run_dir / f"fake-qemu-argv-{phase}-{number}.json").read_text())


def has(argv: list[str], fragment: str) -> bool:
    return any(fragment in part for part in argv)


# --- a repairable image: Debian ---------------------------------------------------------------------------------------

def test_debian_is_started_broken_repaired_and_started_again_with_uefi_and_bios(lab, images):
    prepare(lab, "debian", mode="grub-cfg")
    assert run(lab, images, "rescue-debian") == 0
    result, run_dir = lab.result(), lab.run_dir()
    rescue = result["rescue"]
    assert result["success"] is True and rescue["os_id"] == "debian" and rescue["break"] == "grub-cfg"
    assert rescue["baseline"]["state"] == "login" and rescue["baseline"]["prompt"] == "debian login:" and rescue["baseline"]["nvram"] == "fresh"
    assert rescue["details"]["mode"] == "grub-cfg" and rescue["broken"]["state"].startswith("fatal:grub rescue>") and "cannot start" in rescue["broken"]["proof"]
    repair = rescue["repair"]
    assert repair["system"]["device"] == "/dev/vda1" and [item["id"] for item in repair["diagnosis"]] == ["D05"]
    assert repair["plan"]["commands"][0].endswith("update-grub") and repair["outcome"]["outcome"] == "success"
    assert repair["boot_files"]["matches"] == [] and repair["cleanup"] == "nothing left behind" and repair["touches_nvram"] is False
    assert repair["efibootmgr"] == "rescue-grub-cfg-efibootmgr.log" and (lab.run_dir() / repair["efibootmgr"]).is_file()
    assert [boot["firmware"] for boot in rescue["final_boots"]] == ["uefi", "bios"] and all(boot["state"] == "login" for boot in rescue["final_boots"])
    assert rescue["base_image_unchanged"] is True and rescue["base_image"]["size"] > 1 << 20
    assert [boot["label"] for boot in result["boots"]] == ["baseline", "final-uefi", "final-bios"]
    assert (run_dir / "rescue-grub-cfg-audit.txt").read_text().startswith("confirm plan")


def test_whether_the_firmware_started_the_fallback_path_is_recorded_not_assumed(lab, images):
    registered = 'BdsDxe: starting Boot0002 "UEFI QEMU HARDDISK" from HD(1,GPT,AAAA,0x800,0x200000)/\\EFI\\BOOT\\BOOTX64.EFI\n'
    rules = cloud_image_serial_rules()
    rules.insert(2, {"phase": "target", "firmware": "uefi", "requires": "repaired", "text": registered + console()})  # only the UEFI boot after the repair
    prepare(lab, "debian", mode="grub-cfg", serial=rules)
    assert run(lab, images, "rescue-debian") == 0
    boots = {boot["label"]: boot for boot in lab.result()["boots"]}
    assert boots["final-uefi"]["started_through_fallback"] is True and boots["final-uefi"]["firmware_boot_choices"][0]["number"] == "0002"
    assert "started_through_fallback" not in boots["baseline"] and "started_through_fallback" not in boots["final-bios"], "no OVMF log, nothing to record"


def test_the_machines_start_in_the_order_the_proof_needs_and_never_see_the_base_image(lab, images):
    prepare(lab, "debian", mode="grub-cfg")
    assert run(lab, images, "rescue-debian") == 0
    run_dir = lab.run_dir()
    # target-only boots: the untouched image, the damaged one, the repaired one with UEFI and with BIOS
    boots = [argv_of(run_dir, "target", number) for number in range(1, 5)]
    assert not (run_dir / "fake-qemu-argv-target-5.json").exists()
    assert [has(argv, "if=pflash") for argv in boots] == [True, True, True, False]
    assert all(not has(argv, "media=cdrom") for argv in boots)
    # two live boots, both with the CD first so the disk's own boot loader cannot win
    assert has(argv_of(run_dir, "live", 1), "ide-cd,drive=cd0,bootindex=0") and has(argv_of(run_dir, "live", 2), "ide-cd,drive=cd0,bootindex=0")
    # the machine only ever has the overlay with the disposable serial; the base image is the overlay's backing file
    overlay = [json.loads(line) for line in lab.qemu_img_log.read_text().splitlines() if '"-b"' in line]
    assert len(overlay) == 1 and overlay[0][overlay[0].index("-b") + 1] == str((images / vm.vm_rescue.IMAGES["debian"].filename).resolve())
    for argv in boots:
        assert f"file={run_dir / 'disk.qcow2'},if=none,id=target,format=qcow2" in argv and f"virtio-blk-pci,drive=target,serial={vm.TARGET_SERIAL}" in argv
        assert not has(argv, str(images))
    assert (run_dir / "disk.qcow2").is_file(), "a rescue run keeps the disk it worked on"


def test_every_boot_has_its_own_console_log_and_pid_file_is_removed(lab, images):
    prepare(lab, "debian", mode="grub-cfg")
    assert run(lab, images, "rescue-debian") == 0
    run_dir = lab.run_dir()
    for name in ("serial-baseline.log", "serial-broken.log", "serial-final-uefi.log", "serial-final-bios.log", "broken-boot-serial.txt", "broken-boot.ppm"):
        assert (run_dir / name).is_file(), name
    assert "debian login:" in (run_dir / "serial-final-bios.log").read_text() and "grub rescue>" in (run_dir / "serial-broken.log").read_text()
    assert not list(run_dir.glob("qemu-*.pid"))


def test_empty_nvram_is_the_default_and_a_repair_that_changed_the_boot_entries_keeps_them(lab, images):
    prepare(lab, "debian", mode="grub-cfg")
    assert run(lab, images, "rescue-debian") == 0
    run_dir = lab.run_dir()
    assert (run_dir / "OVMF_VARS.before-final-uefi.fd").exists(), "the repaired image was started with fresh variables: only the fallback path can boot it"
    assert lab.result()["rescue"]["final_boots"][0]["nvram"] == "fresh"


def test_a_repair_that_touched_the_firmware_boot_entries_boots_with_the_variables_it_left(lab, images):
    prepare(lab, "debian", mode="grub-cfg", nvram=True)
    for rule in lab.rules:
        if "--json run" in rule.get("match", ""):
            rule["write"] = {"path": "OVMF_VARS.fd", "text": "Boot0009 debian"}  # what efibootmgr leaves in the NVRAM during the repair
    assert run(lab, images, "rescue-debian") == 0
    run_dir = lab.run_dir()
    assert (run_dir / "OVMF_VARS.fd").read_text() == "Boot0009 debian" and not (run_dir / "OVMF_VARS.before-final-uefi.fd").exists()
    final = lab.result()["rescue"]["final_boots"]
    assert final[0]["nvram"] == "keep" and lab.result()["rescue"]["repair"]["touches_nvram"] is True


def test_final_boots_can_be_limited_and_the_baseline_skipped(lab, images):
    prepare(lab, "debian", mode="grub-cfg")
    assert run(lab, images, "rescue-debian", "--final-boots", "bios", "--no-baseline-boot") == 0
    rescue = lab.result()["rescue"]
    assert "baseline" not in rescue and [boot["firmware"] for boot in rescue["final_boots"]] == ["bios"]
    assert not (lab.run_dir() / "fake-qemu-argv-target-4.json").exists()


def test_the_other_distributions_use_their_own_damage_and_prompt(lab, images):
    prepare(lab, "ubuntu", mode="initramfs", prompt="ubuntu login: ", serial=cloud_image_serial_rules("ubuntu login: ", broken=PANIC))
    assert run(lab, images, "rescue-ubuntu") == 0
    rescue = lab.result()["rescue"]
    assert rescue["break"] == "initramfs" and rescue["repair"]["system"]["os"] == "Ubuntu 24.04.1 LTS" and rescue["baseline"]["prompt"] == "ubuntu login:"
    assert rescue["broken"]["state"] == "fatal:Kernel panic - not syncing" and [item["id"] for item in rescue["repair"]["diagnosis"]] == ["D02"]
    assert [command["step"] for command in rescue["repair"]["commands"]] == ["scan", "mount", "diagnose", "plan", "run", "audit", "unmount"]


def test_fedora_restarts_once_to_relabel_before_it_shows_the_prompt(lab, images):
    prepare(lab, "fedora", mode="grub-cfg", prompt="localhost login: ", serial=cloud_image_serial_rules("localhost login: ", relabel=True))
    assert run(lab, images, "rescue-fedora") == 0
    final = lab.result()["rescue"]["final_boots"]
    assert final[0]["restarts"] == 1 and final[0]["state"] == "login" and final[1]["restarts"] == 0
    run_dir = lab.run_dir()
    assert (run_dir / "serial-final-uefi.log").is_file() and (run_dir / "serial-final-uefi-2.log").is_file()
    assert "reboot: Restarting system" in (run_dir / "serial-final-uefi.log").read_text() and "localhost login:" in (run_dir / "serial-final-uefi-2.log").read_text()


def test_a_machine_that_keeps_restarting_is_a_failure_after_two_restarts(lab, images):
    restarting = {"phase": "target", "requires": "repaired", "text": "reboot: Restarting system\n", "exit_after": 0.2, "exit_code": 0}
    prepare(lab, "debian", mode="grub-cfg", serial=[{"phase": "live", "text": ""}, {"phase": "target", "requires": "rescue-break.json", "absent": "repaired", "text": BROKEN_GRUB},
                                                    restarting, {"phase": "target", "text": console()}])
    assert run(lab, images, "rescue-debian") == 1
    assert "restarted 3 times without reaching a login prompt" in lab.result()["error"]


# --- what must fail -----------------------------------------------------------------------------------------------------

def test_an_image_that_does_not_boot_untouched_is_reported_before_anything_is_broken(lab, images):
    prepare(lab, "debian", serial=[{"phase": "target", "text": BROKEN_GRUB}, {"phase": "live", "text": ""}])
    assert run(lab, images, "rescue-debian") == 1
    error = lab.result()["error"]
    assert error.startswith("baseline (UEFI): the machine cannot start (grub rescue>)") and "serial-baseline.log" in error
    assert not (lab.run_dir() / "rescue-break.json").exists()
    assert (lab.run_dir() / "baseline.ppm").is_file() and (lab.run_dir() / "baseline-serial.txt").is_file()


def test_damage_that_does_not_stop_the_boot_is_a_failed_test_not_a_pass(lab, images):
    prepare(lab, "debian", serial=[{"phase": "live", "text": ""}, {"phase": "target", "text": console()}])
    assert run(lab, images, "rescue-debian") == 1
    assert "still boots after breaking grub-cfg (it showed 'debian login:')" in lab.result()["error"]
    assert "repair" not in lab.result()["rescue"], "nothing was repaired because there was nothing to prove"


def test_silence_without_a_login_counts_as_broken_after_the_time_is_up(lab, images):
    silent = [{"phase": "live", "text": ""}, {"phase": "target", "requires": "rescue-break.json", "absent": "repaired", "text": "booting...\n"},
              {"phase": "target", "text": console()}]
    prepare(lab, "debian", serial=silent)
    assert run(lab, images, "rescue-debian", "--no-baseline-boot") == 0  # without a baseline the wait is exactly --broken-timeout
    broken = lab.result()["rescue"]["broken"]
    assert broken["state"] == "waiting" and broken["proof"] == "no login prompt within 2s" and broken["waited"] >= 2


def test_a_repair_that_leaves_the_machine_unbootable_names_the_firmware_and_the_console(lab, images):
    prepare(lab, "debian", serial=cloud_image_serial_rules(after_repair=PANIC))
    assert run(lab, images, "rescue-debian") == 1
    result = lab.result()
    assert "final-uefi (UEFI): the machine cannot start (Kernel panic - not syncing)" in result["error"]
    assert [boot["label"] for boot in result["boots"]] == ["baseline", "final-uefi"] and (lab.run_dir() / "final-uefi.ppm").is_file()
    assert "Kernel panic - not syncing" in result["boots"][1]["markers"]


def test_a_repaired_system_that_still_boots_the_live_medium_arguments_is_caught_on_disk(lab, images):
    prepare(lab, "debian", live_arguments=True)
    assert run(lab, images, "rescue-debian") == 1
    error = lab.result()["error"]
    assert "would not boot from its disk" in error and "/boot/grub/grub.cfg:12 carries an argument of the live medium" in error
    assert not (lab.run_dir() / "fake-qemu-argv-target-3.json").exists(), "the final boots never started"
    assert lab.result()["rescue"]["repair"]["unmount"]["leftovers"] == [], "the session was still released"


def test_live_arguments_on_the_kernel_command_line_of_the_booted_system_are_caught_too(lab, images):
    leaking = console() + "\n[    0.04] Kernel command line: BOOT_IMAGE=/vmlinuz root=live:CDLABEL=GENTOO rd.live.image ro\n"
    prepare(lab, "debian", serial=cloud_image_serial_rules(after_repair=console("debian login: ") + leaking))
    assert run(lab, images, "rescue-debian", "--no-baseline-boot") == 1
    assert "arguments of the live medium (rd.live.image, root=live:CDLABEL=GENTOO)" in lab.result()["error"]


def test_a_firmware_boot_entry_with_live_arguments_is_caught_too(lab, images):
    prepare(lab, "debian", mode="grub-cfg")
    for rule in lab.rules:
        if rule.get("match") == "^efibootmgr -v$":
            rule["stdout"] = "Boot0003* Linux\tHD(1,GPT,AAAA)/File(\\vmlinuz)root=live:CDLABEL=GENTOO rd.live.image initrd=\\initrd\n"
    assert run(lab, images, "rescue-debian") == 1
    assert "A firmware boot entry carries arguments of the live medium: rd.live.image, root=live:CDLABEL=GENTOO" in lab.result()["error"]
    assert not (lab.run_dir() / "fake-qemu-argv-target-3.json").exists()


def test_a_menu_file_check_that_found_no_menu_proves_nothing(lab, images):
    prepare(lab, "debian", no_menu_files=True)
    assert run(lab, images, "rescue-debian") == 1
    assert "no boot menu file" in lab.result()["error"]


def test_a_repair_that_was_not_successful_fails_with_the_failed_step(lab, images):
    prepare(lab, "debian", outcome="failed")
    assert run(lab, images, "rescue-debian") == 1
    error = lab.result()["error"]
    assert "outcome 'failed'" in error and "step boot-config-c1 (exit 1) failed" in error
    assert lab.result()["rescue"]["repair"]["unmount"] is not None


def test_the_tool_that_does_not_see_the_damage_fails_the_scenario(lab, images):
    prepare(lab, "debian", sees_damage=False)
    assert run(lab, images, "rescue-debian") == 1
    assert "does not see or plan the repair" in lab.result()["error"] and "D05" in lab.result()["error"]


def test_the_wrong_system_on_the_disk_is_not_repaired(lab, images):
    other = [{"id": "c9", "device": "/dev/vda1", "kind": "root", "os": {"id": "ubuntu", "pretty_name": "Ubuntu"}}]
    prepare(lab, "debian", systems=other)
    assert run(lab, images, "rescue-debian") == 1
    assert "scan found no debian system on /dev/vda; it found: /dev/vda1 (Ubuntu, root)" in lab.result()["error"]


def test_anything_left_behind_in_the_live_system_fails_the_repair(lab, images):
    prepare(lab, "debian", left_behind=True)
    assert run(lab, images, "rescue-debian") == 1
    assert "still mounted: /mnt/rescue/r1a2b3" in lab.result()["error"]


def test_a_missing_image_says_where_to_put_it(lab, tmp_path):
    prepare(lab, "debian")
    assert lab.run("--scenario", "rescue-debian", "--rescue-images", str(tmp_path / "nowhere")) == 1
    error = lab.result()["error"]
    assert "debian-13-genericcloud-amd64.qcow2 was not found in" in error and "--rescue-images" in error
    assert not lab.qemu_img_log.exists(), "no overlay was made for an image that is not there"


def test_the_images_directory_can_come_from_the_environment(lab, images, monkeypatch):
    monkeypatch.setenv("GENTOO_RESCUE_IMAGES", str(images))
    prepare(lab, "debian", mode="grub-cfg")
    assert lab.run("--scenario", "rescue-debian", *FAST) == 0


def test_a_base_image_that_was_written_to_fails_the_run_even_if_everything_else_passed(lab, images, monkeypatch):
    prepare(lab, "debian", mode="grub-cfg")
    original = vm.ScenarioRunner.boot_live

    def write_to_the_base(self):
        with (images / vm.vm_rescue.IMAGES["debian"].filename).open("r+b") as stream:
            stream.write(b"WRITTEN")
        return original(self)

    monkeypatch.setattr(vm.ScenarioRunner, "boot_live", write_to_the_base)
    assert run(lab, images, "rescue-debian") == 1
    rescue = lab.result()["rescue"]
    assert rescue["base_image_unchanged"] is False and "the first MiB of the base image changed" in rescue["base_image_error"]
    assert "must only ever be a read-only backing file" in lab.result()["error"]


# --- an image that is only diagnosed: Arch -----------------------------------------------------------------------------------

def test_arch_is_detected_diagnosed_and_every_repair_is_refused_with_the_disk_untouched(lab, images):
    prepare(lab, "arch", refuse=True, prompt="archlinux login: ")
    assert run(lab, images, "rescue-arch") == 0
    result, run_dir = lab.result(), lab.run_dir()
    arch = result["rescue"]["diagnosis_only"]
    assert arch["system"]["os"] == "Arch Linux" and arch["family"] == "arch" and arch["actions_offered"] == [] and arch["unchanged"] is True
    assert [item["exit"] for item in arch["refusals"]] == [7, 7, 7, 7] and all(item["code"] == "rescue.unsupported_action" for item in arch["refusals"])
    assert arch["sha256_before"] == arch["sha256_after"]
    assert "baseline" not in result["rescue"] and "final_boots" not in result["rescue"], "nothing is repaired, so nothing is booted again"
    assert not list(run_dir.glob("fake-qemu-argv-target-*.json")), "the image is only ever seen from the live ISO"
    assert result["rescue"]["base_image_unchanged"] is True and not (run_dir / "rescue-break.json").exists()


def test_a_disk_that_changed_during_a_diagnosis_fails_the_arch_run(lab, images):
    prepare(lab, "arch", refuse=True)
    for rule in lab.rules:
        if "--json unmount" in rule.get("match", ""):
            rule["write"] = {"path": "fake-disk-state.txt", "text": "something wrote here"}
    assert run(lab, images, "rescue-arch") == 1
    assert "changed during a diagnosis that must not write anything" in lab.result()["error"]


def test_a_tool_that_offers_repairs_for_arch_fails_the_run(lab, images):
    prepare(lab, "arch", refuse=True)
    for rule in lab.rules:
        if "--json actions" in rule.get("match", ""):
            rule["stdout"] = rule["stdout"].replace('"available": false', '"available": true')
    assert run(lab, images, "rescue-arch") == 1
    assert "offers repairs for a system it must only diagnose" in lab.result()["error"]


def test_a_tool_that_plans_a_repair_for_arch_fails_the_run(lab, images):
    prepare(lab, "arch", refuse=True)
    plan_ok = [rule for rule in rescue_rules("debian") if "--json plan" in rule.get("match", "")][0]
    lab.rules.insert(0, {**plan_ok, "match": r"--json plan r1a2b3 repair\.recommended$"})
    assert run(lab, images, "rescue-arch") == 1
    assert "offered a plan for repair.recommended on a system it must only diagnose" in lab.result()["error"]


# --- the command line ------------------------------------------------------------------------------------------------------

def parse_error(lab, capsys, *arguments):
    with pytest.raises(SystemExit) as raised:
        vm.main(lab.argv(*arguments))
    assert raised.value.code == 2
    return capsys.readouterr().err


def test_an_image_scenario_is_not_installed_and_takes_no_installer_options(lab, capsys):
    assert "a cloud image is not installed" in parse_error(lab, capsys, "--scenario", "rescue-debian", "--install-mode", "gui")
    assert "is only diagnosed" in parse_error(lab, capsys, "--scenario", "rescue-arch", "--rescue-command", "true")
    assert "cannot be combined" in parse_error(lab, capsys, "--scenario", "rescue-debian", "--target-image", "x.qcow2")
    assert "need one --scenario" in parse_error(lab, capsys, "--all", "--rescue-repair", "--rescue-break", "grub-cfg")


def test_break_sequences_and_final_boot_lists_are_validated(lab, capsys):
    assert "comma separated list" in parse_error(lab, capsys, "--scenario", "bios", "--rescue-break", "grub-cfg,sabotage")
    assert "comma separated list" in parse_error(lab, capsys, "--scenario", "bios", "--rescue-break", "none,grub-cfg")
    assert "uefi, bios, or uefi,bios" in parse_error(lab, capsys, "--scenario", "rescue-debian", "--final-boots", "uefi,coreboot")
    assert "repairs something that was broken" in parse_error(lab, capsys, "--scenario", "bios", "--rescue-repair")
    assert "takes one --rescue-break at a time" in parse_error(lab, capsys, "--scenario", "bios", "--target-image", "x.qcow2", "--rescue-break", "grub-cfg,initramfs")


def test_listing_shows_the_image_scenarios(capsys):
    assert vm.main(["--list-scenarios"]) == 0
    out = capsys.readouterr().out
    for name in ("rescue-debian", "rescue-ubuntu", "rescue-fedora", "rescue-arch", "rescue-gentoo"):
        assert name in out
    assert "image debian" in out and "breaks grub-cfg,initramfs,fstab-uuid" in out


def test_uefi_is_needed_for_a_repaired_image_even_when_the_live_system_is_bios():
    import argparse
    import dataclasses

    debian = dataclasses.replace(vm.SCENARIO_MAP["rescue-debian"], firmware="bios")
    assert vm.needs_uefi(debian, argparse.Namespace(final_boots=None)) and vm.needs_uefi(debian, argparse.Namespace(final_boots="bios,uefi"))
    assert not vm.needs_uefi(debian, argparse.Namespace(final_boots="bios"))
    assert vm.needs_uefi(vm.SCENARIO_MAP["rescue-arch"], argparse.Namespace()) and not vm.needs_uefi(vm.SCENARIO_MAP["bios"], argparse.Namespace())


def test_the_base_images_are_the_real_names_of_the_downloaded_files():
    assert {image.filename for image in vm.vm_rescue.IMAGES.values()} == {
        "debian-13-genericcloud-amd64.qcow2", "noble-server-cloudimg-amd64.img", "Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2", "Arch-Linux-x86_64-cloudimg.qcow2"}
    assert os.sep not in "".join(image.filename for image in vm.vm_rescue.IMAGES.values())
