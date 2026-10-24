"""scripts/resolve-gate.py: the parts that run without Docker."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def load_gate():
    spec = importlib.util.spec_from_file_location("resolve_gate", ROOT / "scripts" / "resolve-gate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["resolve_gate"] = module
    spec.loader.exec_module(module)
    return module


gate = load_gate()

FAILED_EMERGE = """\
Calculating dependencies... done!
!!! All ebuilds that could satisfy "x11-drivers/nvidia-drivers" have been masked.
!!! One of the following masked packages is required to complete your request:
- x11-drivers/nvidia-drivers-595.104.02::gentoo (masked by: NVIDIA-2025 license(s))
(dependency required by "app-misc/hwmonux-1.0.0-r1::gentoo-live-installer[nvidia]" [ebuild])
[ebuild  N     ] app-misc/hwmonux-1.0.0-r1
"""


def test_the_reasons_are_the_lines_that_explain_a_failure():
    reasons = [line for line in FAILED_EMERGE.splitlines() if gate.REASON_RE.match(line)]
    assert reasons[0].startswith("!!! All ebuilds")
    assert any("masked by: NVIDIA-2025" in line for line in reasons)
    assert any(line.startswith("(dependency required by") for line in reasons)
    assert not any(line.startswith("[ebuild") or line.startswith("Calculating") for line in reasons)


def test_the_container_keeps_portage_defaults_for_lint_scratch_paths(tmp_path):
    portage = tmp_path / "etc" / "portage"
    portage.mkdir(parents=True)
    (portage / "make.conf").write_text('COMMON_FLAGS="-O2 -pipe"\nPORTAGE_TMPDIR="/tmp/x/tmp"\nDISTDIR="/tmp/x/d"\nPKGDIR="/tmp/x/p"\n'
                                       'PORTAGE_DEPCACHEDIR="/tmp/x/c"\nFEATURES="getbinpkg"\n')
    assert gate.container_config(SimpleNamespace(config=tmp_path)) == portage
    assert (portage / "make.conf").read_text() == 'COMMON_FLAGS="-O2 -pipe"\nFEATURES="getbinpkg"\n'


def test_a_cell_result_is_ok_only_when_emerge_exited_zero_without_an_error():
    assert gate.CellResult("c", "img", returncode=0).ok
    assert not gate.CellResult("c", "img", returncode=1).ok
    assert not gate.CellResult("c", "img", returncode=0, error="timeout").ok
    assert not gate.CellResult("c", "img").ok


def test_settings_become_typed_cell_overrides():
    assert gate.parse_setting("hardware.gpu_driver=nvidia") == ("hardware.gpu_driver", "nvidia")
    assert gate.parse_setting("hardware.nvidia_license_accepted=true") == ("hardware.nvidia_license_accepted", True)
    assert gate.parse_setting("hardware.zram_percent=25") == ("hardware.zram_percent", 25)
    for bad in ("gpu_driver=nvidia", "hardware.gpu_driver", "a.b.c=1"):
        try:
            gate.parse_setting(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
