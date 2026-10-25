"""lsblk of util-linux 2.42 prints a flat list when the columns have no NAME: the partitions are then no "children".

Found by the first Wave 6 VM matrix: the harness saw a disk with three partitions as one with none. The harness asks for the
tree explicitly; the product's own calls keep NAME first, which gives the tree on every util-linux.
"""
from __future__ import annotations

from pathlib import Path
import re

from gentoo_installer import blockdev

ROOT = Path(__file__).resolve().parents[2]


def test_the_harness_asks_lsblk_for_the_tree() -> None:
    for script in ("scripts/vm_guard.py", "scripts/vm_guest.py"):
        calls = re.findall(r'\["lsblk", "--json"[^\]]*\]', (ROOT / script).read_text())
        assert calls, script
        assert all('"--tree"' in call for call in calls), f"{script}: {calls}"


def test_the_products_lsblk_columns_start_with_name() -> None:
    assert blockdev.LSBLK_COLUMNS[0] == "NAME"
    assert blockdev.lsblk_argv()[:2] == ("lsblk", "--json") and "NAME" in blockdev.lsblk_argv()[-1].split(",")
