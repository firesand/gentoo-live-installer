from __future__ import annotations

from pathlib import Path

import pytest

from .lab import Lab


@pytest.fixture
def lab_factory(tmp_path: Path):
    def make(lsblk, **kwargs) -> Lab:
        return Lab(tmp_path, lsblk, **kwargs)

    return make
