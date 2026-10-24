"""One version per package: the ebuild file name. Everything else must agree with it."""
from __future__ import annotations

import re

import pytest

from .conftest import ROOT, load_script

reuse = load_script("iso/stage_reuse.py", "stage_reuse")


def version(prefix: str) -> str:
    return reuse.package_version(reuse.ebuild_file(ROOT, prefix))


def test_project_versions_match_the_ebuilds() -> None:
    assert re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)[1] == version("gentoo-live-installer")
    assert re.search(r'^version = "([^"]+)"', (ROOT / "apps/hwmonux/pyproject.toml").read_text(), re.M)[1] == version("hwmonux")
    assert re.search(r'^version = "([^"]+)"', (ROOT / "apps/portagehub/Cargo.toml").read_text(), re.M)[1] == version("portagehub")


def test_scripts_do_not_repeat_versions_or_revisions() -> None:
    """build-iso.sh and the builder read the ebuild names; a literal version would drift."""
    for relative in ("scripts/build-iso.sh", "iso/builder-entrypoint.sh", "iso/stage_reuse.py"):
        text = (ROOT / relative).read_text()
        assert not re.search(r"(gentoo-live-installer|hwmonux|portagehub)-\d+\.\d+\.\d+", text), relative
        assert not re.search(r"-r[0-9]\.ebuild", text), relative


def test_installer_companion_install_uses_the_same_archive_names() -> None:
    """configure.py copies the distfiles into the target by name; the names must be the ebuilds' ${P}."""
    text = (ROOT / "src/gentoo_installerd/configure.py").read_text()
    found = dict(re.findall(r'"(hwmonux|portagehub)-(\d+\.\d+\.\d+)\.tar\.zst"', text))
    if not found:
        pytest.skip("configure.py no longer names the archives literally")
    for prefix, number in found.items():
        assert number == version(prefix), f"configure.py expects {prefix}-{number}.tar.zst but the ebuild is {version(prefix)}"


def test_the_installer_reports_the_packaged_version() -> None:
    text = (ROOT / "src/gentoo_installerd/configure.py").read_text()
    receipt = re.search(r'"installer":\s*"(\d+\.\d+\.\d+)"', text)
    if receipt:
        assert receipt[1] == version("gentoo-live-installer")
