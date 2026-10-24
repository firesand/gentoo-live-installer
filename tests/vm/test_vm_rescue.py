"""TD-N4: damage an installed system from the live ISO, show it no longer boots, repair it, boot it again.

The repair itself is the rescue tool's business (another component); the harness runs whatever
``--rescue-command`` says and judges the result. Everything here runs against the fake lab.
"""
from __future__ import annotations

import json
import os

import pytest

from tests.vm.fake_env import EVENT_STREAM, Lab, guest_rules
from tests.vm.helpers import PUBLIC_KEY, load_vm_module

vm = load_vm_module()

pytestmark = pytest.mark.slow  # real processes and sockets: about a second per scenario


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return Lab(vm, tmp_path, monkeypatch)


def break_rule(mode: str, *, broken_boot: bool = True) -> list[dict]:
    """The guest breaks fine; afterwards the installed disk does (not) boot."""
    rules = [{"user": "^root$", "match": r"rescue-break '\{", "guest_tool": True}]
    if broken_boot:
        rules.insert(0, {"user": "^vmtest$", "match": "^true$", "rc": 255, "requires": "rescue-break.json", "absent": "repaired"})
    return rules


def rules_for(scenario="bios", mode="grub-cfg", *, repaired=False, broken_boot=True, **kwargs):
    rules = break_rule(mode, broken_boot=broken_boot)
    if repaired:  # the repair command marks the disk repaired; from then on the system boots again
        rules.insert(0, {"user": "^root$", "match": r"^gentoo-rescue ", "touch": ["repaired"]})
    return rules + guest_rules(vm, scenario, **kwargs)


def test_break_after_a_verified_install_proves_the_system_no_longer_boots(lab):
    lab.rules = rules_for()
    assert lab.run("--scenario", "bios", "--rescue-break", "grub-cfg", "--broken-timeout", "1") == 0
    run = lab.run_dir()
    result = lab.result()
    assert result["success"] is True
    assert all(probe["success"] for probe in result["probes_before_break"] if not probe["advisory"])  # it worked before the break
    rescue = result["rescue"]
    assert rescue["break"] == "grub-cfg" and rescue["broken"]["proof"] == "no SSH"
    assert rescue["details"]["removed"] == ["boot/grub/grub.cfg"]
    assert (run / "rescue-break.json").is_file() and (run / "broken-boot.ppm").is_file()
    assert (run / "disk.qcow2").is_file(), "a rescue run exists to leave the broken disk behind"
    assert json.loads((run / "result.json").read_text())["rescue"]["disk"] == str(run / "disk.qcow2")


def test_the_second_live_boot_wins_the_boot_order_over_the_installed_disk(lab):
    lab.rules = rules_for()
    assert lab.run("--scenario", "bios", "--rescue-break", "initramfs", "--broken-timeout", "1") == 0
    run = lab.run_dir()
    live = json.loads((run / "fake-qemu-argv-live.json").read_text())  # the last live boot
    assert "ide-cd,drive=cd0,bootindex=0" in live
    qemu_log = (run / "qemu-live.log").read_text()
    assert qemu_log.count("$ ") == 2 and qemu_log.count("bootindex=0") == 1  # the first boot, on a blank disk, is the original shape


def test_the_break_is_guarded_like_an_install(lab):
    lab.rules = rules_for()
    assert lab.run("--scenario", "bios", "--rescue-break", "grub-cfg", "--broken-timeout", "1") == 0
    guards = [json.loads(call["remote"].rsplit("'", 2)[-2]) for call in lab.ssh_calls() if "vm_disk_guard" in call["remote"]]
    assert [g["disks"][0]["blank"] for g in guards] == [True, False]  # blank before the install, installed before the break


def test_a_system_that_still_boots_after_the_break_is_a_failed_test_not_a_pass(lab):
    lab.rules = rules_for(broken_boot=False)
    assert lab.run("--scenario", "bios", "--rescue-break", "initramfs", "--broken-timeout", "1") == 1
    assert "still boots after breaking initramfs" in lab.result()["error"]


def test_the_fstab_defect_is_proven_by_findmnt_not_by_a_failed_boot(lab):
    def verify_failed(command, payload):
        return None
    lab.rules = [{"user": "^root$", "match": r"rescue-break '\{", "stdout": json.dumps(
        {"type": "vm_guest", "ok": True, "mode": "fstab-uuid", "replaced": ["UUID=x"], "findmnt_exit": 1,
         "findmnt_output": "unreachable on boot required source: UUID=00000000-dead-beef-0000-000000000000"}) + "\n"}] + guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios", "--rescue-break", "fstab-uuid") == 0
    rescue = lab.result()["rescue"]
    assert rescue["broken"]["proof"] == "findmnt --verify" and rescue["broken"]["exit"] == 1
    boots = sorted(path.name for path in lab.run_dir().glob("fake-qemu-argv-target-[0-9]*.json"))
    assert boots == ["fake-qemu-argv-target-1.json"]  # the installed disk booted once (before the break): the fstab damage needed no boot to prove it


def test_a_fstab_break_that_findmnt_accepts_is_rejected(lab):
    lab.rules = [{"user": "^root$", "match": r"rescue-break '\{", "stdout": json.dumps(
        {"type": "vm_guest", "ok": True, "mode": "fstab-uuid", "replaced": ["UUID=x"], "findmnt_exit": 0}) + "\n"}] + guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios", "--rescue-break", "fstab-uuid") == 1
    assert "accepts the damaged fstab" in lab.result()["error"]


def test_a_repair_command_runs_as_root_with_the_guarded_device_and_the_system_is_verified_again(lab):
    lab.rules = rules_for(repaired=True)
    assert lab.run("--scenario", "bios", "--rescue-break", "grub-cfg", "--broken-timeout", "1",
                   "--rescue-command", "gentoo-rescue --local repair --device {device}") == 0
    result = lab.result()
    repair = result["rescue"]["repair"]
    assert repair["command"] == "gentoo-rescue --local repair --device /dev/vda" and repair["exit"] == 0
    assert (lab.run_dir() / "rescue-command.log").is_file()
    assert result["probes_before_break"] and result["probes"]  # verified before the break and again after the repair
    assert [call["user"] for call in lab.ssh_calls() if call["remote"].startswith("gentoo-rescue")] == ["root"]


def test_a_failing_repair_command_fails_the_run_and_keeps_the_disk(lab):
    lab.rules = [{"user": "^root$", "match": r"^gentoo-rescue ", "rc": 3}] + rules_for()
    assert lab.run("--scenario", "bios", "--rescue-break", "grub-cfg", "--broken-timeout", "1",
                   "--rescue-command", "gentoo-rescue --local repair") == 1
    assert "rescue command failed with exit status 3" in lab.result()["error"]
    assert (lab.run_dir() / "disk.qcow2").is_file()


def test_a_repair_that_does_not_fix_the_boot_is_a_failure(lab):
    lab.rules = [{"user": "^root$", "match": r"^gentoo-rescue ", "rc": 0}] + rules_for()  # exits 0 but repairs nothing
    assert lab.run("--scenario", "bios", "--rescue-break", "grub-cfg", "--broken-timeout", "1",
                   "--rescue-command", "gentoo-rescue --local repair", "--boot-timeout", "2") == 1
    assert "SSH did not become ready" in lab.result()["error"]


# --- an existing image (a kept run, or another distribution's) ---------------------------------------

@pytest.fixture
def image(tmp_path, monkeypatch):
    folder = tmp_path / "golden"
    folder.mkdir()
    disk = folder / "disk.qcow2"
    disk.write_bytes(b"golden image")
    vm.build_manifest(vm.SCENARIO_MAP["bios"], PUBLIC_KEY).save(folder / "manifest.json", redact=False)
    monkeypatch.setenv("FAKE_QEMU_IMG_VSIZE", str(21 * 2**30 + 5 * 2**20))
    return disk


def test_an_existing_image_is_attached_through_an_overlay_and_never_written(lab, image):
    lab.rules = rules_for(repaired=True)
    before = (image.stat().st_size, image.stat().st_mtime_ns)
    assert lab.run("--scenario", "bios", "--target-image", str(image), "--rescue-break", "grub-cfg", "--broken-timeout", "1",
                   "--rescue-command", "gentoo-rescue --local repair") == 0
    run = lab.run_dir()
    assert (run / "disk.qcow2.backing").read_text() == str(image.resolve())
    [overlay] = [json.loads(line) for line in lab.qemu_img_log.read_text().splitlines() if '"-b"' in line]
    assert overlay[overlay.index("-b") + 1] == str(image.resolve()) and overlay[overlay.index("-F") + 1] == "qcow2"
    assert overlay[-1] == str(run / "disk.qcow2")
    assert (image.stat().st_size, image.stat().st_mtime_ns) == before
    result = lab.result()
    assert result["rescue"]["base_image_unchanged"] is True and result["success"] is True
    assert result["disk"]["bytes"] == 21 * 2**30 + 5 * 2**20  # the guard expects the image's own size
    assert not [c for c in lab.ssh_calls() if c["remote"].startswith("gentoo-installerd")]  # nothing was installed
    guard = json.loads([c["remote"] for c in lab.ssh_calls() if "vm_disk_guard" in c["remote"]][0].rsplit("'", 2)[-2])
    assert guard["disks"][0]["size"] == 21 * 2**30 + 5 * 2**20 and guard["disks"][0]["blank"] is False


def test_booting_an_existing_image_without_break_or_repair_is_a_smoke_test(lab, image):
    lab.rules = guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios", "--target-image", str(image)) == 0
    assert "only booted and verified" in lab.result()["rescue"]["note"]


def test_the_probes_need_the_manifest_the_image_was_installed_with(lab, image):
    (image.parent / "manifest.json").unlink()
    lab.rules = guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios", "--target-image", str(image)) == 1
    assert "--target-manifest" in lab.result()["error"]


def test_a_changed_base_image_is_detected(lab, image):
    lab.rules = [{"user": "^vmtest$", "match": r"^hostname$", "stdout": "gentoo-vm-bios\n"}] + guest_rules(vm, "bios")
    original = vm.ScenarioRunner.check_base_image_untouched

    def tamper(self):
        os.utime(image, ns=(1, 1))
        return original(self)

    vm.ScenarioRunner.check_base_image_untouched = tamper
    try:
        assert lab.run("--scenario", "bios", "--target-image", str(image)) == 1
    finally:
        vm.ScenarioRunner.check_base_image_untouched = original
    assert "must only ever be a read-only backing file" in lab.result()["error"]


@pytest.mark.parametrize("name", ["disk.iso", "disk.vmdk", "disk"])
def test_only_disk_images_are_accepted_as_a_target(lab, tmp_path, name):
    odd = tmp_path / name
    odd.write_bytes(b"x")
    lab.rules = guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios", "--target-image", str(odd)) == 1
    assert "must name a regular .qcow2, .img or .raw file" in lab.result()["error"]


# --- the command line --------------------------------------------------------------------------------

def parse_error(lab, capsys, *arguments):
    with pytest.raises(SystemExit) as raised:
        vm.main(lab.argv(*arguments))
    assert raised.value.code == 2
    return capsys.readouterr().err


def test_rescue_options_need_one_scenario(lab, capsys):
    assert "need one --scenario, not --all" in parse_error(lab, capsys, "--all", "--rescue-break", "grub-cfg")


def test_encrypted_installs_are_not_supported_by_the_breaker_yet(lab, capsys):
    assert "unencrypted installs only" in parse_error(lab, capsys, "--scenario", "uefi-luks", "--rescue-break", "initramfs")


def test_target_image_does_not_install(lab, capsys, tmp_path):
    error = parse_error(lab, capsys, "--scenario", "bios", "--target-image", str(tmp_path / "x.qcow2"), "--install-mode", "gui")
    assert "has nothing to drive" in error
    assert "cannot be combined with a decoy" in parse_error(lab, capsys, "--scenario", "decoy-disk", "--target-image", str(tmp_path / "x.qcow2"))
    assert "only applies to --target-image" in parse_error(lab, capsys, "--scenario", "bios", "--rescue-break", "grub-cfg",
                                                           "--target-manifest", str(tmp_path / "m.json"))


def test_a_rescue_run_keeps_its_disk_even_without_keep(lab):
    lab.rules = rules_for()
    assert lab.run("--scenario", "bios", "--rescue-break", "grub-cfg", "--broken-timeout", "1") == 0
    assert (lab.run_dir() / "disk.qcow2").is_file()
