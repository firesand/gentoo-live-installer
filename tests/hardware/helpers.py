"""Shared helpers for the hardware tests."""

from __future__ import annotations

import atexit
import json
import shutil
import tempfile
from pathlib import Path

from gentoo_installer import hardware

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
EXPECTED = HERE / "expected"
# Fixtures that are derived from a committed one when the tests start (see synthetic.py).
DERIVED = ("desktop-igpu-idle",)
_derived_root: Path | None = None

# Fixture name -> short description (the machines the planner is exercised on).
FIXTURE_NAMES = (
    "asus-desktop-rtx4090",
    "desktop-igpu-idle",
    "qemu-q35-virtio",
    "old-bios-sata-radeon",
    "thinkpad-intel-nvidia-hybrid",
    "amd-laptop-sof",
    "vmware-guest",
    "intel-rst-hidden-nvme",
    "asus-rog-laptop",
    "razer-blade-laptop",
    "arc-ultra-convertible",
    "thinkpad-amd-laptop",
)


def fixture_path(name: str) -> Path:
    """Directory of a fixture; derived ones are built once per test run in a temporary directory."""
    global _derived_root
    if name not in DERIVED:
        return FIXTURES / name
    if _derived_root is None:
        _derived_root = Path(tempfile.mkdtemp(prefix="hwfixtures-"))
        atexit.register(shutil.rmtree, _derived_root, True)
    target = _derived_root / name
    if not target.exists():
        from tests.hardware import synthetic

        synthetic.desktop_igpu_idle(target)
    return target


def fixture_now(name: str) -> float:
    return float(json.loads((fixture_path(name) / ".capture.json").read_text())["now"])


def probe_fixture(name: str) -> hardware.HardwareProfile:
    return hardware.probe(fixture_path(name), now=fixture_now(name))


def fixture_dict(name: str) -> dict:
    return probe_fixture(name).to_dict()
