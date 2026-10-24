"""Stage2 must find the stage1 archive even with the reuse receipt and the digests next to it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXTENSIONS = {"gzip": ".tar.gz", "xz": ".tar.xz", "pixz": ".tar.xz", "bzip2": ".tar.bz2", "zstd": ".tar.zst"}


def spec_value(name: str, key: str) -> str:
    match = re.search(rf"^{key}: (\S+)$", (ROOT / "iso/specs" / name).read_text(), re.M)
    assert match, f"{name} has no {key}"
    return match.group(1)


def test_stage2_names_the_stage1_archive_with_its_extension() -> None:
    subpath = spec_value("livecd-stage2.spec", "source_subpath")
    assert subpath.endswith(EXTENSIONS[spec_value("livecd-stage1.spec", "compression_mode")])


def test_catalyst_resolves_that_path_with_the_receipt_beside_it(tmp_path: Path) -> None:
    support = pytest.importorskip("catalyst.support")
    stem = tmp_path / "livecd-stage1-amd64-installer-20261018T124555Z"
    for suffix in (".tar.gz", ".tar.gz.receipt.json", ".tar.gz.DIGESTS", ".tar.gz.sha256", ".tar.gz.CONTENTS.gz"):
        Path(f"{stem}{suffix}").write_text("x")
    with pytest.raises(support.CatalystError):  # the receipt makes the bare name ambiguous ...
        support.file_check(str(stem), ["tar.gz"])
    assert support.file_check(f"{stem}.tar.gz", ["tar.gz"]) == f"{stem}.tar.gz"  # ... the full name never is
