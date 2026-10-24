"""--live-only: does the live ISO work on this machine shape? Boot it, ask the installer what it sees, install nothing."""
from __future__ import annotations

import json

import pytest

from tests.vm.fake_env import Lab, gui_rules, guest_rules
from tests.vm.helpers import load_vm_module

vm = load_vm_module()
pytestmark = pytest.mark.slow


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return Lab(vm, tmp_path, monkeypatch)


def rules(scenario="openrc", **inspection):
    return ([{"user": "^root$", "match": "live_inspection", "inspection": inspection}] + gui_rules()
            + guest_rules(vm, scenario))


def test_a_healthy_live_system_passes_without_installing_anything(lab):
    lab.rules = rules()
    assert lab.run("--scenario", "openrc", "--live-only") == 0
    run = lab.run_dir()
    result = lab.result()
    live = result["live"]
    assert result["success"] is True and live["problems"] == [] and live["inspection"]["uefi"] is True
    assert "environment" in live["gui"] and live["guard"]["device"] == "/dev/vda"
    assert json.loads((run / "live-inspection.json").read_text())["architecture"] == "x86_64"
    assert not any(call["remote"].startswith("gentoo-installerd") for call in lab.ssh_calls())  # nothing was installed
    assert not (run / "fake-qemu-argv-target.json").exists() and not (run / "disk.qcow2").exists()


@pytest.mark.parametrize(("inspection", "scenario", "fragment"), [
    ({"uefi": False}, "openrc", "detects BIOS firmware but the VM runs UEFI"),
    ({"uefi": True}, "bios", "detects UEFI firmware but the VM runs BIOS"),
    ({"network": False}, "openrc", "no working network on this machine's NIC"),
    ({"drop_disk": True}, "openrc", "does not see the VM's disk"),
    ({"memory_mib": 1024}, "openrc", "MiB of memory in a 4096 MiB VM"),
    ({"tools": {"lsblk": True, "sgdisk": False}}, "openrc", "required tools are missing from the live system: sgdisk"),
])
def test_what_the_installer_sees_must_be_what_the_machine_is(lab, inspection, scenario, fragment):
    lab.rules = rules(scenario, **inspection)
    assert lab.run("--scenario", scenario, "--live-only") == 1
    assert fragment in lab.result()["error"]


def test_a_live_session_that_never_starts_on_this_display_adapter_is_reported(lab):
    lab.rules = [rule for rule in rules() if "xfce4-session" not in rule.get("match", "")]
    assert lab.run("--scenario", "openrc", "--live-only", "--vga", "qxl", "--gui-timeout", "1") == 1
    assert "did not start on this display adapter" in lab.result()["error"]


def test_a_machine_without_a_display_skips_the_graphical_check(lab):
    lab.rules = [rule for rule in rules() if "xfce4-session" not in rule.get("match", "")]
    assert lab.run("--scenario", "openrc", "--live-only", "--vga", "none") == 0
    assert "gui" not in lab.result()["live"]


def test_expectations_about_the_hardware_report_are_checked(lab, tmp_path, capsys):
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps({"architecture": "x86_64", "disks.0.name": "re:^vd[a-z]$", "network": True}))
    lab.rules = rules()
    assert lab.run("--scenario", "openrc", "--live-only", "--live-expect", str(expected)) == 0
    capsys.readouterr()
    expected.write_text(json.dumps({"disks.0.model": "Samsung SSD", "hardware.machine.chassis": "notebook", "uefi": False}))
    lab.rules = rules()
    assert lab.run("--scenario", "openrc", "--live-only", "--live-expect", str(expected)) == 1
    error = capsys.readouterr().err
    assert "disks.0.model: expected 'Samsung SSD', the installer reports ''" in error
    assert "hardware.machine.chassis: not reported by the installer" in error and "uefi: expected False" in error


def test_live_only_runs_on_other_hardware_shapes(lab):
    lab.rules = [{"user": "^root$", "match": "live_inspection", "inspection": {}}] + gui_rules() + guest_rules(
        vm, "nvme-uefi", guard_options={"bus": "nvme"})
    assert lab.run("--scenario", "nvme-uefi", "--live-only") == 0
    assert lab.result()["live"]["guard"]["device"] == "/dev/nvme0n1"


def test_the_option_cannot_be_combined_with_installs(lab, capsys):
    for extra in (["--check-resume"], ["--rescue-break", "grub-cfg"], ["--cancel-then-resume", "--install-mode", "gui"]):
        with pytest.raises(SystemExit):
            vm.main(lab.argv("--scenario", "openrc", "--live-only", *extra))
        assert "installs nothing" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        vm.main(lab.argv("--scenario", "openrc", "--live-expect", "x.json"))
    assert "only applies to --live-only" in capsys.readouterr().err


def test_pure_helpers():
    assert vm.lookup_path({"a": [{"b": 5}]}, "a.0.b") == 5
    with pytest.raises(KeyError):
        vm.lookup_path({"a": []}, "a.0")
    assert vm.check_expectations({"x": "abc"}, {"x": "re:^a"}) == []
    assert vm.check_expectations({"x": "abc"}, {"x": "re:^z"}) == ["x: 'abc' does not match '^z'"]
    with pytest.raises(vm.HarnessError, match="returned no inspection"):
        vm.parse_live_inspection(1, "Traceback ...")
    with pytest.raises(vm.HarnessError, match="could not inspect"):
        vm.parse_live_inspection(0, json.dumps({"type": "live_inspection_error", "error": "boom"}))
