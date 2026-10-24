"""The graphical branch of the harness (install --install-mode gui) through the fake lab: what is uploaded,
how the driver is started, and what detect mode asks of it."""
from __future__ import annotations

import json

import pytest

from tests.vm.fake_env import Lab, gui_rules, guest_rules
from tests.vm.helpers import load_vm_module

vm = load_vm_module()

pytestmark = pytest.mark.slow  # real processes and sockets: about a second per scenario


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return Lab(vm, tmp_path, monkeypatch)


def plan_of(lab) -> dict:
    return json.loads((lab.run_dir() / "fake-guest" / "vm-gui-plan.json").read_text())


def driver_command(lab) -> str:
    [command] = [call["remote"] for call in lab.ssh_calls() if call["remote"].startswith("runuser ")]
    return command


def test_a_graphical_install_uploads_the_driver_the_guard_and_the_plan_and_collects_the_artifacts(lab):
    lab.rules = gui_rules() + guest_rules(vm, "openrc")
    assert lab.run("--scenario", "openrc", "--install-mode", "gui", "--keep") == 0
    run = lab.run_dir()
    guest = run / "fake-guest"
    assert (guest / "vm-gui-install.py").read_bytes() == (vm.PROJECT_ROOT / "scripts" / "vm-gui-install.py").read_bytes()
    assert (guest / "vm_guard.py").read_bytes() == (vm.PROJECT_ROOT / "scripts" / "vm_guard.py").read_bytes()
    command = driver_command(lab)
    assert "python3 /run/vm-gui-install.py /run/gentoo-installer-vm-manifest.json --output /tmp/gentoo-vm-gui --plan /run/vm-gui-plan.json" in command
    assert "DISPLAY=:0" in command and "--cancel-at-stage3" not in command
    for artifact in ("gui-review.png", "gui-result.png", "gui-gui.log"):
        assert (run / artifact).stat().st_size > 0
    plan = plan_of(lab)
    assert plan["detect"] is False and plan["overrides"] == {} and plan["detected"] == {}  # pre-filled, as before
    assert plan["target"]["path"] == "/dev/vda" and plan["guard"]["disks"][0]["serial"] == vm.TARGET_SERIAL


def test_the_defaults_scenario_leaves_firmware_table_and_disk_to_the_gui(lab):
    detected = {"storage.device": "/dev/vda"}
    lab.rules = gui_rules(detected=detected) + guest_rules(vm, "defaults", swap_mib=4096)
    assert lab.run("--scenario", "defaults", "--install-mode", "gui", "--keep") == 0
    plan = plan_of(lab)
    assert plan["detect"] is True
    assert set(plan["overrides"]) == {"accounts", "options"}  # credentials and the SSH probe access, nothing else
    assert plan["detected"] == {"storage.device": "/dev/vda", "storage.device_id": vm.TARGET_SERIAL, "storage.device_id_kind": "",
                                "storage.firmware": "uefi", "storage.table": "gpt",
                                # seeded from the hardware report: recorded, not asserted
                                "storage.swap_mib": None, "options.bluetooth": None, "options.microcode": None,
                                "options.install_hwmonux": None, "options.install_portagehub": None}
    assert json.loads((lab.run_dir() / "gui-detected.json").read_text()) == detected  # the GUI's own choices are kept as evidence


def test_what_the_wizard_seeded_from_the_hardware_is_what_the_probes_judge(lab):
    """In detect mode the swap, Bluetooth, microcode and the companions are the wizard's choice for this machine: recorded, and then checked as chosen."""
    chosen = {"storage.device": "/dev/vda", "storage.swap_mib": 8192, "options.bluetooth": False, "options.microcode": False,
              "options.install_hwmonux": False, "options.install_portagehub": True}
    lab.rules = gui_rules(detected=chosen) + guest_rules(vm, "defaults", swap_mib=8192, companions="portagehub")
    assert lab.run("--scenario", "defaults", "--install-mode", "gui", "--keep") == 0
    run, result = lab.run_dir(), lab.result()
    saved = json.loads((run / "manifest.json").read_text())
    assert saved["storage"]["swap_mib"] == 8192 and saved["options"]["install_hwmonux"] is False and saved["options"]["install_portagehub"] is True
    assert saved["options"]["bluetooth"] is False and saved["options"]["microcode"] is False
    harness = json.loads((run / "manifest-expected.json").read_text())  # what the harness asked for stays on record
    assert harness["storage"]["swap_mib"] == 4096 and harness["options"]["install_hwmonux"] is True and harness["options"]["microcode"] is True
    assert result["companions"] == {"hwmonux": False, "portagehub": True}
    names = {probe["name"] for probe in result["probes"]}
    assert "disabled-hwmonux-absent" in names and "hwmonux" not in names and "portagehub" in names
    assert result["install"]["wizard_choices"] == {
        "storage.swap_mib": {"harness": 4096, "wizard": 8192}, "options.bluetooth": {"harness": True, "wizard": False},
        "options.microcode": {"harness": True, "wizard": False}, "options.install_hwmonux": {"harness": True, "wizard": False}}


def test_a_run_whose_wizard_chose_what_the_harness_expected_changes_nothing(lab):
    chosen = {"storage.device": "/dev/vda", "storage.swap_mib": 4096, "options.install_hwmonux": True, "options.install_portagehub": True}
    lab.rules = gui_rules(detected=chosen) + guest_rules(vm, "defaults", swap_mib=4096)
    assert lab.run("--scenario", "defaults", "--install-mode", "gui", "--keep") == 0
    run = lab.run_dir()
    assert lab.result()["install"]["wizard_choices"] == {} and not (run / "manifest-expected.json").exists()
    assert lab.result()["companions"] == {"hwmonux": True, "portagehub": True}


def test_only_detect_mode_takes_over_what_the_wizard_chose(lab):
    lab.rules = gui_rules(detected={"storage.swap_mib": 8192}) + guest_rules(vm, "openrc")
    assert lab.run("--scenario", "openrc", "--install-mode", "gui", "--keep") == 0
    assert json.loads((lab.run_dir() / "manifest.json").read_text())["storage"]["swap_mib"] == 512 and lab.result()["install"]["wizard_choices"] is None


def test_detection_can_be_switched_per_run(lab):
    lab.rules = gui_rules() + guest_rules(vm, "defaults", swap_mib=4096)
    assert lab.run("--scenario", "defaults", "--install-mode", "gui", "--no-gui-detect") == 0
    assert plan_of(lab)["detect"] is False


def test_detection_of_a_bios_machine_asserts_the_firmware_but_not_the_table(lab):
    lab.rules = gui_rules() + guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios", "--install-mode", "gui", "--gui-detect") == 0
    plan = plan_of(lab)
    assert plan["detect"] is True
    assert plan["detected"]["storage.firmware"] == "bios" and plan["detected"]["storage.table"] is None
    assert "firmware" not in plan["overrides"].get("storage", {}) and "table" not in plan["overrides"].get("storage", {})


def test_a_failed_graphical_install_is_reported_with_the_backend_error(lab):
    lab.rules = gui_rules(success=False) + guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios", "--install-mode", "gui") == 1
    assert "Installation failed" in lab.result()["error"]


def test_the_cancel_variant_passes_the_flag_and_reads_the_cancel_output_directory(lab):
    cancel = [{"type": "stage", "stage": "storage", "data": {"status": "completed"}},
              {"type": "stage", "stage": "stage3", "data": {"status": "running"}},
              {"type": "error", "error": "cancelled", "message": "Installation cancelled"},
              {"type": "stage", "stage": "cleanup", "data": {"status": "completed"}},
              {"type": "completed", "data": {"success": False}},
              {"type": "gui_result", "success": False, "cancel_requested": True, "reboot_enabled": False, "stage3_started": 1}]
    rules = gui_rules(cancel=True) + gui_rules() + guest_rules(vm, "bios")
    rules.insert(0, {"user": "^root$", "match": r"^runuser -u gentoo -- env .*--cancel-at-stage3", "events": cancel})
    rules.insert(1, {"user": "^root$", "match": r"/proc/self/mountinfo", "stdout": json.dumps(
        {"target_mounts": [], "leftover_root_stack": [], "target_swaps": []}) + "\n"})
    lab.rules = rules
    assert lab.run("--scenario", "bios", "--install-mode", "gui", "--cancel-then-resume") == 0
    result = lab.result()
    assert result["cancel"]["success"] is True and result["resume"]["storage_skipped"] is True
    run = lab.run_dir()
    assert "events_file" not in result["resume"] or (run / "resume-events.jsonl").is_file()
    assert (run / "cancel-events.jsonl").is_file() and (run / "cancel-gui-review.png").is_file()
    assert any("--cancel-at-stage3" in call["remote"] for call in lab.ssh_calls())
    checks = [call["remote"] for call in lab.ssh_calls() if "/proc/self/mountinfo" in call["remote"]]
    assert len(checks) == 1 and "/dev/mapper/gentoo-crypt" in checks[0]  # the cleanup check also looks for LUKS under LVM
