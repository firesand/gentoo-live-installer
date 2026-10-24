"""Golden plans: what the planner decides for each captured or modelled machine.

Every fixture has one file in tests/hardware/expected with the plan and the
cross-check issues for four settings. A change in the planner shows up as a
reviewable diff. After an intended change run

    UPDATE_GOLDENS=1 python -m pytest tests/hardware/test_plan_golden.py

and read the diff of tests/hardware/expected before committing it.
"""

from __future__ import annotations

import difflib
import json
import os

import pytest

from gentoo_installer import recommend
from gentoo_installer.manifest import InstallManifest
from gentoo_installerd import hwplan

from tests.hardware.helpers import EXPECTED, FIXTURE_NAMES, fixture_dict

SCENARIOS = {
    "kde-openrc": "the GUI defaults (KDE, OpenRC, binary packages)",
    "xfce-systemd-source": "Xfce on systemd, everything built from source",
    "portable-nvidia": "a portable disk with the proprietary NVIDIA driver",
    "nvidia-this": "this PC, proprietary NVIDIA driver accepted",
}


def scenario_manifest(name: str, machine: dict) -> InstallManifest:
    m = InstallManifest()
    installable = [d["path"] for d in machine["disks"] if d["installable"]]
    m.storage.device = installable[0] if installable else "/dev/nvme0n1"
    m.accounts.password = m.accounts.root_password = "secret-pass"
    if name == "xfce-systemd-source":
        m.system.init, m.system.stage_variant, m.system.desktop = "systemd", "amd64-desktop-systemd", "xfce"
        m.system.profile = "default/linux/amd64/23.0/desktop/systemd"
        m.portage.binhost = False
    elif name == "portable-nvidia":
        m.hardware.target = "portable"
    if name in ("portable-nvidia", "nvidia-this"):
        m.hardware.gpu_driver, m.hardware.nvidia_license_accepted = "nvidia", True
    recommend.apply_defaults(m, machine)  # what the wizard does on start
    return m


def render(name: str) -> str:
    machine = fixture_dict(name)
    scenarios = {}
    for scenario in SCENARIOS:
        m = scenario_manifest(scenario, machine)
        scenarios[scenario] = {
            "description": SCENARIOS[scenario],
            "issues": [issue.to_dict() for issue in hwplan.validate_plan(m, machine)],
            "plan": hwplan.plan(m, machine).to_dict(),
        }
    return json.dumps({"fixture": name, "machine": machine["machine"]["product_name"], "scenarios": scenarios}, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_plan_matches_golden(name):
    path = EXPECTED / f"{name}.json"
    actual = render(name)
    if os.environ.get("UPDATE_GOLDENS"):
        EXPECTED.mkdir(exist_ok=True)
        path.write_text(actual)
    assert path.exists(), f"missing golden {path}; run with UPDATE_GOLDENS=1"
    expected = path.read_text()
    if actual != expected:
        diff = "".join(difflib.unified_diff(expected.splitlines(True), actual.splitlines(True), "expected", "actual", n=2))
        pytest.fail(f"plan for {name} changed:\n{diff[:6000]}")


def test_every_fixture_has_a_golden_and_nothing_else_is_left_behind():
    assert {p.stem for p in EXPECTED.glob("*.json")} == set(FIXTURE_NAMES)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_golden_default_plans_leave_make_conf_to_the_profile(name):
    golden = json.loads((EXPECTED / f"{name}.json").read_text())["scenarios"]["kde-openrc"]["plan"]
    assert "CPU_FLAGS_X86" not in golden["make_conf"] and "VIDEO_CARDS" not in golden["make_conf"]
