from __future__ import annotations

from pathlib import Path

import pytest

from .lab import Lab


@pytest.fixture
def lab_factory(tmp_path: Path):
    """Every lab gets its own directory, so a test can build several machines."""
    counter = {"n": 0}

    def make(lsblk, **kwargs) -> Lab:
        counter["n"] += 1
        kwargs.setdefault("name", f"lab{counter['n']}")
        return Lab(tmp_path, lsblk, **kwargs)

    return make
