"""docs/TESTING.md must keep up with the harness: a scenario or an option nobody wrote down is a trap."""
from __future__ import annotations

from pathlib import Path

import pytest

import vm_hw
import vm_matrix
import vm_rescue
from tests.vm.helpers import load_vm_module

vm = load_vm_module()
DOC = (Path(__file__).resolve().parents[2] / "docs" / "TESTING.md").read_text()
ALWAYS_DOCUMENTED = ("--luks-unlock", "--gui-detect", "--no-gui-detect", "--strict-probes", "--cleanup-stale", "--list-scenarios",
                     "--keep", "--check-resume", "--cancel-then-resume", "--gui-timeout", "--install-mode", "--memory",
                     "--boot-timeout", "--build-jobs", "--swap-mib", "--companions", "--live-only", "--live-expect",
                     "--matrix", "--list-matrix", "--matrix-only", "--matrix-parallel", "--matrix-memory-budget", "--boot-entry",
                     "--list-boot-entries", "--fresh-nvram-check", "--no-world-probe", "--world-timeout")


def documented_options() -> list[str]:
    parser = vm.build_parser()
    options = [option for group in parser._action_groups if group.title in {"machine shape", "rescue", "matrix"}
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
    for script in ("scripts/test.sh", "scripts/check.sh", "scripts/test-vm.py", "scripts/vm-gui-install.py", "scripts/vm_guard.py", "scripts/vm_probes.py",
                   "scripts/vm_rescue.py", "scripts/vm_iso.py", "scripts/vm_matrix.py"):
        assert script in DOC and (root / script).is_file(), script


def matrix_rows() -> dict[str, list[str]]:
    section = DOC.split("## The Wave 3 matrix", 1)[1].split("```sh", 1)[0]
    rows = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("| `") and len(cells) == 6:
            rows[cells[0].strip("`")] = cells
    return rows


def documented_companions(entry) -> str:
    """The companions an entry installs, as the table writes them: '-' for a cloud image (nothing is installed)."""
    if vm.SCENARIO_MAP[entry.scenario].image:
        return "-"
    return "PortageHub" if "portagehub" in entry.options else "HWMonux and PortageHub"


@pytest.mark.parametrize("entry", vm_matrix.WAVE3.entries, ids=lambda entry: entry.name)
def test_every_matrix_entry_is_in_the_table_with_the_sizing_the_matrix_really_uses(entry):
    rows = matrix_rows()
    assert entry.name in rows, f"docs/TESTING.md does not list the {entry.name} entry of the matrix"
    _name, scenario, companions, memory, cpus, minutes = rows[entry.name]
    assert f"`{entry.scenario}`" in scenario
    assert memory == f"{entry.memory_mib // 1024} GiB" and cpus == str(entry.cpus) and minutes == str(entry.minutes)
    assert companions == documented_companions(entry), "the Companions column of the table must say what the entry's --companions option does"


def test_the_documented_matrix_rules_are_the_ones_the_scheduler_applies():
    assert f"**At most {['zero', 'one', 'two'][vm_matrix.PARALLEL_MAX]} VMs run at the same time**" in DOC
    assert f"default {vm_matrix.DEFAULT_BUDGET_MIB} MiB" in DOC and f"plus {vm_matrix.MARGIN_MIB // 1024} GiB" in DOC


def test_the_cloud_image_names_in_the_requirements_are_the_ones_the_harness_looks_for():
    for image in vm_rescue.IMAGES.values():
        assert image.filename in DOC, image.filename


def test_the_memory_rule_and_the_choice_of_companions_are_written_down():
    for words in ("PySide", "no binary package", "`--companions portagehub`", "6 GiB"):
        assert words in DOC, words
