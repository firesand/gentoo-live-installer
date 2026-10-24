"""docs/TESTING.md must keep up with the harness: a scenario or an option nobody wrote down is a trap."""
from __future__ import annotations

from pathlib import Path

import pytest

import vm_hw
from tests.vm.helpers import load_vm_module

vm = load_vm_module()
DOC = (Path(__file__).resolve().parents[2] / "docs" / "TESTING.md").read_text()
ALWAYS_DOCUMENTED = ("--luks-unlock", "--gui-detect", "--no-gui-detect", "--strict-probes", "--cleanup-stale", "--list-scenarios",
                     "--keep", "--check-resume", "--cancel-then-resume", "--gui-timeout", "--install-mode", "--memory",
                     "--boot-timeout", "--build-jobs", "--swap-mib", "--companions", "--live-only", "--live-expect")


def documented_options() -> list[str]:
    parser = vm.build_parser()
    options = [option for group in parser._action_groups if group.title in {"machine shape", "rescue"}
               for action in group._group_actions for option in action.option_strings if option.startswith("--")]
    return sorted(set(options) | set(ALWAYS_DOCUMENTED))


@pytest.mark.parametrize("name", sorted(vm.SCENARIO_MAP))
def test_every_scenario_is_described(name):
    assert f"`{name}`" in DOC, f"docs/TESTING.md does not describe the {name} scenario"


@pytest.mark.parametrize("option", documented_options())
def test_every_hardware_rescue_and_headline_option_is_described(option):
    assert option in DOC, f"docs/TESTING.md does not mention {option}"


@pytest.mark.parametrize("choice", [*vm_hw.DISK_BUSES, *vm_hw.NICS, *vm_hw.VGAS, *vm_hw.SMBIOS_PRESETS, *vm_hw.OVMF_VARS_MODES,
                                    *vm_hw.MACHINES, *vm_hw.RTC_MODES])
def test_every_hardware_choice_is_listed(choice):
    assert f"`{choice}`" in DOC


def test_the_rescue_break_modes_are_described():
    for mode in vm.RESCUE_BREAKS[1:]:
        assert mode in DOC


def test_the_documented_entry_points_exist():
    root = Path(__file__).resolve().parents[2]
    for script in ("scripts/test.sh", "scripts/check.sh", "scripts/test-vm.py", "scripts/vm-gui-install.py", "scripts/vm_guard.py"):
        assert script in DOC and (root / script).is_file(), script
