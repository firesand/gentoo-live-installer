"""TD-N5: names and signatures that tools outside this repository's tracked files depend on.

build/*.py (untracked, ignored by git) loads scripts/test-vm.py and calls into it:
boot-live-smoke.py, boot-normal-smoke.py, target-resolver-review.py, virtio-vga-boot-probe.py,
run-r19-openrc.py, watch-r19-openrc-safety.py and the acceptance generators. A refactor of the
harness must keep these working or fail here first.
"""
from __future__ import annotations

import argparse
import inspect
import runpy
from pathlib import Path

import pytest

from tests.vm.helpers import PUBLIC_KEY, load_vm_module

vm = load_vm_module()
ROOT = Path(__file__).resolve().parents[2]


def parameters(function) -> list[str]:
    return list(inspect.signature(function).parameters)


@pytest.mark.parametrize("name", [
    "build_parser", "SCENARIO_MAP", "preflight", "public_key_from_private", "ScenarioRunner", "VMProcess", "HarnessError",
    "OvmfFirmware", "detect_ovmf", "positive_int", "validate_command_paths", "run_logged", "qemu_command", "build_manifest",
    "TARGET_SERIAL", "TARGET_DEVICE", "TEST_USERNAME", "TEST_PASSWORD", "TEST_ROOT_PASSWORD", "qmp_screendump", "main",
    "ALL_SCENARIOS", "SCENARIOS", "Scenario", "poll_gui_environment", "firmware_arguments",
    "parse_install_result", "parse_probe_result", "TEST_LUKS_PASSPHRASE", "VM_ROOT", "SerialChannel", "ssh_command",
])
def test_the_names_other_tools_import_still_exist(name):
    assert hasattr(vm, name), name


def test_signatures_used_by_the_smoke_tools():
    assert parameters(vm.ScenarioRunner.__init__)[:5] == ["self", "args", "scenario", "public_key", "firmware"]
    assert parameters(vm.ScenarioRunner.start_vm) == ["self", "live"]
    assert parameters(vm.ScenarioRunner.wait_for_ssh)[:4] == ["self", "user", "phase", "vm"]
    assert parameters(vm.ScenarioRunner.ssh)[:5] == ["self", "user", "remote", "log_name", "timeout"]
    assert parameters(vm.ScenarioRunner.gui_environment) == ["self"]
    assert parameters(vm.VMProcess.__init__) == ["self", "argv", "run_dir", "label"]
    assert parameters(vm.VMProcess.screendump) == ["self", "output"]
    assert parameters(vm.run_logged)[:3] == ["argv", "log", "timeout"] and "stdin" in parameters(vm.run_logged)
    assert parameters(vm.validate_command_paths) == ["iso", "disk", "run_dir"]
    assert parameters(vm.preflight) == ["args", "scenarios"]
    assert parameters(vm.public_key_from_private)[:2] == ["ssh_keygen", "private_key"]
    assert parameters(vm.build_manifest)[:3] == ["scenario", "ssh_public_key", "companions"]
    assert parameters(vm.OvmfFirmware) == ["code", "variables"]
    required = [name for name, p in inspect.signature(vm.qemu_command).parameters.items() if p.default is inspect.Parameter.empty]
    assert required == ["scenario", "iso", "disk", "run_dir", "live", "ssh_host", "ssh_port", "memory_mib", "cpus"]
    assert "qemu_binary" in parameters(vm.qemu_command) and "firmware" in parameters(vm.qemu_command)


def test_scenario_names_the_tools_use_are_still_scenarios():
    assert {"openrc", "systemd", "uefi-luks", "bios", "defaults"} <= set(vm.SCENARIO_MAP)
    assert vm.TARGET_SERIAL == "GENTOO-VM-DISPOSABLE" and vm.TARGET_DEVICE == "/dev/vda"


def test_the_script_can_be_run_as_a_file_the_way_the_review_tools_do():
    namespace = runpy.run_path(str(ROOT / "scripts" / "test-vm.py"))  # target-resolver-review.py, virtio-vga-boot-probe.py
    manifest = namespace["build_manifest"](namespace["SCENARIO_MAP"]["openrc"], PUBLIC_KEY)
    assert manifest.storage.device == "/dev/vda" and namespace["qmp_screendump"]


def test_a_runner_can_be_built_from_a_bare_namespace_like_boot_normal_smoke_does(tmp_path, monkeypatch):
    monkeypatch.setattr(vm, "VM_ROOT", tmp_path / "build" / "vm")
    iso = tmp_path / "installer.iso"
    iso.write_bytes(b"iso")
    runner = vm.ScenarioRunner(argparse.Namespace(iso=iso), vm.SCENARIO_MAP["openrc"], "", None)  # only args.iso may be read
    assert runner.run_dir.parent == tmp_path / "build" / "vm" and runner.disk == runner.run_dir / "disk.qcow2"
    assert runner.vms == [] and runner.logs == []


def test_the_command_line_the_smoke_tools_check_is_unchanged(tmp_path, monkeypatch):
    """boot-normal-smoke.py matches drive strings; watch-r19-openrc-safety.py wants the absolute QMP socket in the argv."""
    root = tmp_path / "build" / "vm"
    run = root / "run"
    run.mkdir(parents=True)
    iso = tmp_path / "installer.iso"
    iso.write_bytes(b"iso")
    code, variables = tmp_path / "code.fd", tmp_path / "vars.fd"
    code.write_bytes(b"c")
    variables.write_bytes(b"v")
    monkeypatch.setattr(vm, "VM_ROOT", root)
    argv = vm.qemu_command(vm.SCENARIO_MAP["openrc"], iso=iso, disk=run / "disk.qcow2", run_dir=run, live=True,
                           ssh_host="127.0.0.1", ssh_port=1, memory_mib=4096, cpus=2, qemu_binary="q", firmware=vm.OvmfFirmware(code, variables))
    drives = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "-drive"]
    for fragment in (f"file={run / 'disk.qcow2'},if=none,id=target,format=qcow2", f"file={iso},media=cdrom,readonly=on,format=raw",
                     f"if=pflash,format=raw,readonly=on,file={code}", f"if=pflash,format=raw,file={run / 'OVMF_VARS.fd'}"):
        assert any(drive.startswith(fragment) for drive in drives), fragment
    assert str(run / "qmp.sock") in " ".join(argv)
    index = argv.index("-netdev") + 1
    assert argv[index].startswith("user,id=net0,hostfwd=")
    assert "-virtfs" not in argv and "-fsdev" not in argv


def test_the_qmp_helpers_keep_their_two_entry_points():
    import vm_qmp
    assert parameters(vm_qmp.execute) == ["socket_path", "command", "arguments"]
    assert parameters(vm_qmp.screendump) == ["socket_path", "output"]
    assert parameters(vm_qmp.type_luks_passphrase) == ["socket_path", "passphrase"]
    assert (ROOT / "scripts" / "vm-qmp.py").is_file()  # postboot-gui-geometry-probe.py runs it


@pytest.mark.slow
def test_the_untracked_acceptance_gate_files_are_still_produced(tmp_path, monkeypatch):
    """build/generate-r19-vm-acceptance.py reads these keys of result.json and these artifacts."""
    result_keys = {"scenario", "success", "run_dir", "companions", "companion_probes", "disabled_companion_probes",
                   "probes", "resume", "cancel"}
    from tests.vm.fake_env import Lab, guest_rules
    lab = Lab(vm, tmp_path, monkeypatch)
    lab.rules = guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios", "--check-resume", "--keep") == 0
    assert result_keys <= set(lab.result())
    assert {"success", "storage_skipped", "destructive_commands", "identity_mismatches"} <= set(lab.result()["resume"])
    for artifact in ("manifest.json", "install-result.json", "resume-result.json", "serial-target.log", "qemu-live.log"):
        assert (lab.run_dir() / artifact).is_file(), artifact
