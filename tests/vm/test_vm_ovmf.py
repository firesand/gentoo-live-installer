from pathlib import Path

import pytest

from tests.vm.helpers import load_vm_module


vm = load_vm_module()


def test_ovmf_detection_requires_both_code_and_variables(tmp_path):
    code = tmp_path / "OVMF_CODE.fd"
    variables = tmp_path / "OVMF_VARS.fd"
    code.write_bytes(b"code")
    assert vm.detect_ovmf([(code, variables)]) is None
    variables.write_bytes(b"variables")
    selected = vm.detect_ovmf([(code, variables)])
    assert selected == vm.OvmfFirmware(code.resolve(), variables.resolve())


def test_ovmf_selection_uses_first_complete_pair(tmp_path):
    first = (tmp_path / "missing-code", tmp_path / "missing-vars")
    second = (tmp_path / "code2", tmp_path / "vars2")
    third = (tmp_path / "code3", tmp_path / "vars3")
    for path in (*second, *third):
        path.write_bytes(b"x")
    assert vm.detect_ovmf([first, second, third]).code == second[0].resolve()


def test_uefi_firmware_arguments_copy_only_writable_vars(tmp_path, monkeypatch):
    root = tmp_path / "build" / "vm"
    run_dir = root / "run"
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(vm, "VM_ROOT", root)
    code = tmp_path / "code"
    variables = tmp_path / "vars"
    code.write_bytes(b"code")
    variables.write_bytes(b"original")
    selected = vm.OvmfFirmware(code, variables)

    args = vm.firmware_arguments(vm.SCENARIO_MAP["openrc"], selected, run_dir)
    copied = run_dir / "OVMF_VARS.fd"
    assert copied.read_bytes() == b"original"
    assert variables.read_bytes() == b"original"
    assert f"if=pflash,format=raw,readonly=on,file={code}" in args
    assert f"if=pflash,format=raw,file={copied}" in args
    copied.write_bytes(b"installed target boot entry")
    vm.firmware_arguments(vm.SCENARIO_MAP["openrc"], selected, run_dir)
    assert copied.read_bytes() == b"installed target boot entry"
    assert vm.firmware_arguments(vm.SCENARIO_MAP["bios"], None, run_dir) == []


def test_uefi_without_ovmf_is_an_explicit_error(tmp_path, monkeypatch):
    monkeypatch.setattr(vm, "VM_ROOT", tmp_path / "build" / "vm")
    with pytest.raises(vm.HarnessError, match="requires OVMF"):
        vm.firmware_arguments(vm.SCENARIO_MAP["uefi-luks"], None, tmp_path)
