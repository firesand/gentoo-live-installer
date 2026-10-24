"""Contract tests for scripts/vm-gui-install.py, which otherwise only runs inside a live VM.

TD-N1 (detect mode), DELTA-6 (readiness), NOBS-14 (watchdog) and the guard that the driver repeats as
the unprivileged user.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import py_compile
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gentoo_installer.app import InstallerWindow
from gentoo_installer.manifest import InstallManifest
from gentoo_installer.system_info import DiskInfo, SystemInfo
from tests.vm.helpers import PUBLIC_KEY, load_vm_module

ROOT = Path(__file__).resolve().parents[2]
vm = load_vm_module()


def load_driver():
    spec = importlib.util.spec_from_file_location("vm_gui_install", ROOT / "scripts" / "vm-gui-install.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


driver = load_driver()


def info(**changes):
    values = dict(architecture="x86_64", memory_gib=8, uefi=True, network=True, synchronized_clock=True, missing_tools=[],
                  disks=[DiskInfo("/dev/vda", "QEMU", 40 * 1024**3, serial="GENTOO-VM-DISPOSABLE")])
    values.update(changes)
    return SystemInfo(**values)


# --- readiness (DELTA-6) -----------------------------------------------------------------------------

def test_clean_scan_has_no_problems():
    assert driver.readiness_problems(info(), "uefi") == ([], [])


@pytest.mark.parametrize("changes, kind", [
    ({"network": False}, "transient"),
    ({"missing_tools": ["sgdisk"]}, "transient"),
    ({"architecture": "aarch64"}, "fatal"),
    ({"uefi": False}, "fatal"),  # the scenario expects UEFI, the live VM booted BIOS
    ({"disks": []}, "fatal"),
    ({"disks": [DiskInfo("/dev/vda", "QEMU", 1, serial="SOME-REAL-DISK")]}, "fatal"),
    ({"disks": [DiskInfo("/dev/vda", "QEMU", 1, serial="GENTOO-VM-DISPOSABLE", mounted=True)]}, "fatal"),
    ({"disks": [DiskInfo("/dev/vda", "QEMU", 1, serial="GENTOO-VM-DISPOSABLE", read_only=True)]}, "fatal"),
    ({"disks": [DiskInfo("/dev/vda", "QEMU", 1)]}, "fatal"),  # without the plan, a serial-less disk is not the harness disk
])
def test_problems_are_classified_so_identity_checks_are_never_retried(changes, kind):
    fatal, transient = driver.readiness_problems(info(**changes), "uefi")
    assert bool(fatal) == (kind == "fatal")
    assert bool(transient) == (kind == "transient")


def test_the_target_comes_from_the_plan_and_may_have_no_serial():
    target = {"path": "/dev/nvme0n1", "serial": "", "size": 42_000_000_000}
    disk = DiskInfo("/dev/nvme0n1", "QEMU NVMe Ctrl", 42_000_000_000)
    assert driver.readiness_problems(info(disks=[disk]), "uefi", target) == ([], [])
    wrong_size = DiskInfo("/dev/nvme0n1", "QEMU NVMe Ctrl", 42_000_000_001)
    assert driver.readiness_problems(info(disks=[wrong_size]), "uefi", target)[0]
    assert driver.readiness_problems(info(disks=[]), "uefi", target)[0]


# --- the plan and the manifest comparison (TD-N1) ---------------------------------------------------------

def test_plans_with_unknown_keys_are_refused(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"detect": True, "detectt": True}))
    with pytest.raises(ValueError, match="Unknown plan keys: detectt"):
        driver.load_plan(plan)
    plan.write_text(json.dumps({"detect": True, "overrides": {"accounts": {"hostname": "x"}}}))
    assert driver.load_plan(plan)["detect"] is True
    assert driver.load_plan(None) == {"guard": None, "detect": False, "target": None, "overrides": {}, "detected": {}}


def test_overrides_are_merged_into_the_defaults_and_typos_are_rejected():
    defaults = InstallManifest().to_dict(redact=False)
    merged = driver.merge_overrides(defaults, {"accounts": {"hostname": "vm"}, "storage": {"swap_mib": 512}})
    assert merged["accounts"]["hostname"] == "vm" and merged["storage"]["swap_mib"] == 512
    assert merged["system"] == defaults["system"] and defaults["accounts"]["hostname"] == "gentoo"  # defaults untouched
    with pytest.raises(ValueError, match="Unknown manifest section"):
        driver.merge_overrides(defaults, {"acounts": {"hostname": "x"}})
    with pytest.raises(ValueError, match="Unknown accounts fields in the overrides: hostnme"):
        driver.merge_overrides(defaults, {"accounts": {"hostnme": "x"}})


def expected_for(scenario="openrc", **kwargs):
    return vm.build_manifest(vm.SCENARIO_MAP[scenario], PUBLIC_KEY, **kwargs).to_dict(redact=False)


def test_matching_manifests_have_no_differences():
    expected = expected_for()
    assert driver.compare_manifests(expected, json.loads(json.dumps(expected))) == []


def test_differences_name_the_field_and_hide_secrets():
    expected = expected_for()
    actual = json.loads(json.dumps(expected))
    actual["system"]["desktop"] = "kde"
    actual["accounts"]["password"] = "something else entirely"
    problems = driver.compare_manifests(expected, actual)
    assert "system.desktop: expected 'minimal', the wizard has 'kde'" in problems
    assert any(p.startswith("accounts.password:") and "<secret>" in p and "something else" not in p for p in problems)


def test_detected_fields_must_equal_the_expectation_when_one_is_given():
    expected = expected_for("bios")
    detected = {"storage.firmware": "bios", "storage.table": None, "storage.device": "/dev/vda"}
    actual = json.loads(json.dumps(expected))
    actual["storage"]["table"] = "gpt"  # the GUI's own choice: recorded, not asserted (table is None)
    assert driver.compare_manifests(expected, actual, detected) == []
    actual["storage"]["firmware"] = "uefi"  # G2: a BIOS live system whose GUI offers UEFI
    assert driver.compare_manifests(expected, actual, detected) == ["storage.firmware: the GUI chose 'uefi', expected 'bios'"]


def test_everything_that_is_not_detected_is_still_compared_exactly():
    expected = expected_for()
    detected = {"storage.device": "/dev/vda"}
    actual = json.loads(json.dumps(expected))
    actual["storage"]["swap_mib"] = 4096
    assert driver.compare_manifests(expected, actual, detected) == ["storage.swap_mib: expected 512, the wizard has 4096"]


def test_fields_the_wizard_seeds_from_the_hardware_are_recorded_never_compared():
    """The plan of a detect-mode run names them with None: a small machine may get other swap, no Bluetooth, no microcode and fewer companions."""
    expected = expected_for("defaults")
    recorded = {"storage.swap_mib": None, "options.bluetooth": None, "options.microcode": None, "options.install_hwmonux": None, "options.install_portagehub": None}
    actual = json.loads(json.dumps(expected))
    actual["storage"]["swap_mib"] = 8192
    actual["options"].update(bluetooth=False, microcode=False, install_hwmonux=False, install_portagehub=False)
    assert driver.compare_manifests(expected, actual, recorded) == []
    assert len(driver.compare_manifests(expected, actual, {})) == 5  # without the plan every one of them is a difference
    actual["system"]["desktop"] = "xfce"  # but nothing else may move
    assert driver.compare_manifests(expected, actual, recorded) == ["system.desktop: expected 'kde', the wizard has 'xfce'"]


@pytest.mark.parametrize("fixture", ["qemu-q35-virtio", "vmware-guest"])
def test_on_a_virtual_machine_the_wizard_seeds_only_what_the_plan_records(fixture):
    """If the wizard starts to seed another field from the hardware report (a console font on a big screen is one), a detect-mode run would
    fail at the review page with 'expected X, the wizard has Y': the harness has to learn the field first."""
    from gentoo_installer import recommend
    from tests.hardware.helpers import fixture_dict

    seeded = set(recommend.defaults_for(fixture_dict(fixture), InstallManifest()))
    assert seeded <= set(vm.WIZARD_SEEDED) | {"storage.firmware"}, sorted(seeded - set(vm.WIZARD_SEEDED))


def test_the_plan_the_harness_writes_names_exactly_the_fields_the_driver_may_not_compare(tmp_path):
    runner = vm.ScenarioRunner.__new__(vm.ScenarioRunner)
    runner.args = SimpleNamespace(gui_detect=None, memory=12288)
    runner.scenario = vm.SCENARIO_MAP["defaults"]
    runner.guard_report = None
    manifest = vm.build_manifest(runner.scenario, PUBLIC_KEY)
    plan = runner.gui_plan(manifest, vm.vm_guard.GuardSpec(disks=[], nonce="n"))
    assert {path for path, value in plan["detected"].items() if value is None} == set(vm.WIZARD_SEEDED)
    actual = manifest.to_dict(redact=False)
    for path in vm.WIZARD_SEEDED:
        section, _, key = path.partition(".")
        actual[section][key] = False if isinstance(actual[section][key], bool) else 1024
    actual["storage"]["firmware"], actual["storage"]["table"] = "uefi", "gpt"
    assert driver.compare_manifests(manifest.to_dict(redact=False), actual, plan["detected"]) == []


def test_serial_identities_tolerate_the_legacy_empty_kind():
    expected = expected_for()
    actual = json.loads(json.dumps(expected))
    actual["storage"]["device_id_kind"] = "serial"
    detected = {"storage.device_id": expected["storage"]["device_id"], "storage.device_id_kind": ""}
    assert driver.compare_manifests(expected, actual, detected) == []
    actual["storage"]["device_id"] = "ANOTHER-DISK"
    assert driver.compare_manifests(expected, actual, detected)


def test_a_path_identity_accepts_any_by_path_link_of_the_disk():
    expected = expected_for("serial-less", device_id="pci-0000:00:07.0|42000000000|", device_id_kind="path")
    detected = {"storage.device_id": expected["storage"]["device_id"], "storage.device_id_kind": "path"}
    target = {"by_path": ["pci-0000:00:07.0", "virtio-pci-0000:00:07.0"]}
    actual = json.loads(json.dumps(expected))
    actual["storage"]["device_id"] = "virtio-pci-0000:00:07.0|42000000000|"
    assert driver.compare_manifests(expected, actual, detected, target) == []
    actual["storage"]["device_id"] = "pci-0000:00:09.0|42000000000|"  # a link of some other disk
    assert driver.compare_manifests(expected, actual, detected, target)
    actual["storage"]["device_id"] = "pci-0000:00:07.0|41999999999|"  # same link, other size
    assert driver.compare_manifests(expected, actual, detected, target)


def test_overrides_are_exactly_what_the_wizard_cannot_detect():
    manifest = vm.build_manifest(vm.SCENARIO_MAP["bios"], PUBLIC_KEY)
    expected = manifest.to_dict(redact=False)
    overrides = vm.manifest_overrides(expected, InstallManifest().to_dict(redact=False))
    for field in vm.DETECTED_FIELDS:
        section, key = field.split(".")
        assert key not in overrides.get(section, {}), field
    assert overrides["system"]["desktop"] == "minimal" and overrides["accounts"]["username"] == vm.TEST_USERNAME
    assert overrides["network"]["dhcp"] is False
    # merging them into the defaults and adding the detections gives back the expected manifest
    merged = driver.merge_overrides(InstallManifest().to_dict(redact=False), overrides)
    for field in vm.DETECTED_FIELDS:
        section, key = field.split(".")
        merged[section][key] = expected[section][key]
    assert merged == expected


def test_the_defaults_scenario_needs_almost_no_overrides():
    expected = vm.build_manifest(vm.SCENARIO_MAP["defaults"], PUBLIC_KEY).to_dict(redact=False)
    overrides = vm.manifest_overrides(expected, InstallManifest().to_dict(redact=False))
    assert set(overrides) == {"accounts", "options"}  # credentials and the SSH probe access: nothing else
    assert overrides["options"] == {"ssh": True}


# --- watchdog (NOBS-14) ---------------------------------------------------------------------------------

def test_the_watchdog_fires_once_with_its_reason(qtbot):
    fired = []
    watchdog = driver.Watchdog(fired.append)
    watchdog.arm(0.05, "page 3 did not advance")
    qtbot.waitUntil(lambda: bool(fired), timeout=2000)
    assert fired == ["Watchdog: page 3 did not advance for 0.05s"]


def test_rearming_or_stopping_the_watchdog_cancels_the_old_deadline(qtbot):
    fired = []
    watchdog = driver.Watchdog(fired.append)
    watchdog.arm(0.05, "first")
    watchdog.arm(0.3, "second")
    qtbot.wait(150)
    assert fired == []
    qtbot.waitUntil(lambda: bool(fired), timeout=2000)
    assert fired == ["Watchdog: second for 0.3s"]
    watchdog.arm(0.05, "third")
    watchdog.stop()
    qtbot.wait(200)
    assert fired == ["Watchdog: second for 0.3s"]


def test_driver_options_have_safe_defaults():
    args = driver.build_parser().parse_args(["manifest.json"])
    assert (args.readiness_timeout, args.navigation_timeout, args.idle_timeout) == (60, 120, 1800)
    assert args.plan is None and args.cancel_at_stage3 is False


def test_driver_help_lists_every_option(capsys):
    with pytest.raises(SystemExit) as exit_info:
        driver.main(["--help"])
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    for option in ("--plan", "--readiness-timeout", "--navigation-timeout", "--idle-timeout", "--cancel-at-stage3"):
        assert option in help_text


def test_the_driver_refuses_to_run_as_root_and_outside_the_vm(tmp_path, monkeypatch):
    monkeypatch.setattr(driver.os, "geteuid", lambda: 0)
    with pytest.raises(SystemExit, match="outside the non-root disposable VM"):
        driver.main([str(tmp_path / "manifest.json")])
    monkeypatch.setattr(driver.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit, match="outside the non-root disposable VM"):  # no sysfs serial of a harness disk here
        driver.main([str(tmp_path / "manifest.json")])


def test_the_driver_refuses_a_plan_whose_guard_finds_a_problem(tmp_path, monkeypatch):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"guard": {"disks": []}, "detect": False}))
    monkeypatch.setattr(driver.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("DISPLAY", ":0")
    xauth = tmp_path / "xauth"
    xauth.write_text("x")
    monkeypatch.setenv("XAUTHORITY", str(xauth))
    manifest = tmp_path / "manifest.json"
    vm.build_manifest(vm.SCENARIO_MAP["openrc"], PUBLIC_KEY).save(manifest, redact=False)
    monkeypatch.setattr(driver, "guard_problems", lambda spec: ["Unexpected disk(s) present besides the harness disks: /dev/sdb"])
    with pytest.raises(SystemExit, match="Unexpected disk"):
        driver.main([str(manifest), "--plan", str(plan)])


# --- the real window ---------------------------------------------------------------------------------------

def window_attribute_chains(source: str) -> set[tuple[str, ...]]:
    """Every ``window.a.b.c`` attribute chain the guest-only driver touches, taken from its AST."""
    chains: set[tuple[str, ...]] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            parts, current = [], node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name) and current.id == "window":
                chains.add(tuple(reversed(parts)))
    return chains


def make_window(qtbot, monkeypatch, tmp_path, manifest, system_info):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("GENTOO_INSTALLER_DEV", "1")  # system_info is the whole machine: no daemon is asked
    monkeypatch.setattr("gentoo_installer.app.inspect_system", lambda: system_info)
    window = InstallerWindow(manifest, str(tmp_path / "backend.sock"), background_inspection=False)
    qtbot.addWidget(window)
    return window


def test_driver_only_uses_window_attributes_that_exist(qtbot, monkeypatch, tmp_path):
    """Fails when a GUI refactor renames something the guest-only driver clicks or reads."""
    window = make_window(qtbot, monkeypatch, tmp_path, InstallManifest(), info())
    chains = window_attribute_chains((ROOT / "scripts" / "vm-gui-install.py").read_text())
    assert ("progress", "request_cancel") in chains and ("storage", "disk_group") in chains  # the parser found the real uses
    assert ("readiness", "rows") in chains and ("refresh_readiness",) in chains
    for chain in sorted(chains):
        target = window
        for part in chain:
            assert hasattr(target, part), f"window.{'.'.join(chain)} is missing ({part}); update scripts/vm-gui-install.py"
            target = getattr(target, part)


def walk_wizard(window):
    """What the driver does between the plan and the Review page: choose the disk, press Continue."""
    for _ in range(6):
        if window.current == 2:
            window.storage.disk_group.set_value("/dev/vda")
        window.next.click()
    return window.manifest.to_dict(redact=False)


def detect_run(qtbot, monkeypatch, tmp_path, scenario, *, uefi):
    expected_manifest = vm.build_manifest(vm.SCENARIO_MAP[scenario], PUBLIC_KEY)
    expected = expected_manifest.to_dict(redact=False)
    overrides = vm.manifest_overrides(expected, InstallManifest().to_dict(redact=False))
    initial = InstallManifest.from_dict(driver.merge_overrides(InstallManifest().to_dict(redact=False), overrides))
    window = make_window(qtbot, monkeypatch, tmp_path, initial, info(uefi=uefi))
    window.show()
    actual = walk_wizard(window)
    assert window.current == 6, "the wizard stopped before the Review page"
    detected = {"storage.device": "/dev/vda", "storage.device_id": vm.TARGET_SERIAL, "storage.firmware": expected_manifest.storage.firmware,
                "storage.table": "gpt" if expected_manifest.storage.firmware == "uefi" else None}
    return driver.compare_manifests(expected, actual, detected)


def test_a_uefi_live_system_is_detected_and_the_wizard_matches_the_expected_manifest(qtbot, monkeypatch, tmp_path):
    assert detect_run(qtbot, monkeypatch, tmp_path, "openrc", uefi=True) == []


def test_a_bios_live_system_is_detected_and_the_wizard_matches_the_expected_manifest(qtbot, monkeypatch, tmp_path):
    assert detect_run(qtbot, monkeypatch, tmp_path, "bios", uefi=False) == []


def test_the_defaults_scenario_survives_the_wizard_when_the_gui_detects_everything(qtbot, monkeypatch, tmp_path):
    assert detect_run(qtbot, monkeypatch, tmp_path, "defaults", uefi=True) == []


@pytest.mark.parametrize("script", sorted(p.name for p in (ROOT / "scripts").glob("*.py")))
def test_every_script_compiles(script, tmp_path):
    py_compile.compile(str(ROOT / "scripts" / script), cfile=str(tmp_path / "out.pyc"), doraise=True)
