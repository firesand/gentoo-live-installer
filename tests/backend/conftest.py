"""Hermetic defaults for the backend tests: nothing asks the network, and the machine that runs them is not probed."""

from __future__ import annotations

import pytest

from gentoo_installerd import inspect as inspect_module
from gentoo_installerd import proxy, stage3

from tests.hardware.helpers import FIXTURES

# The machine every Discovery.inspect() in these tests describes unless a test chooses another one.
DEFAULT_MACHINE = "qemu-q35-virtio"


@pytest.fixture(autouse=True)
def hermetic_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENTOO_HW_ROOT", str(FIXTURES / DEFAULT_MACHINE))
    monkeypatch.setattr(inspect_module, "mirror_clock_probe", lambda *args, **kwargs: proxy.ClockProbe(None, None))
    monkeypatch.setattr(stage3, "free_bytes", lambda path: 1 << 50)  # the disk of the machine running the tests is not the subject
