"""Versions of the companion applications must agree wherever they are spelled out.

The installer finds the companion source archives by pattern (hwmonux-*.tar.zst), so a version bump
only has to be made in the application and its ebuild; these tests catch the places that still name a
version literally (the ISO build scripts) drifting away from them.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "packaging/gentoo-overlay"

COMPANIONS = {
    "hwmonux": ("app-misc", ROOT / "apps/hwmonux/pyproject.toml", ("project", "version")),
    "portagehub": ("app-portage", ROOT / "apps/portagehub/Cargo.toml", ("package", "version")),
}


def application_version(name: str) -> str:
    _, manifest, keys = COMPANIONS[name]
    data = tomllib.loads(manifest.read_text())
    return data[keys[0]][keys[1]]


def ebuild_versions(category: str, name: str) -> list[str]:
    """Upstream versions of the overlay ebuilds; the Gentoo revision (-rN) is packaging-only."""
    stems = (path.stem.removeprefix(f"{name}-") for path in (OVERLAY / category / name).glob(f"{name}-*.ebuild"))
    return [re.sub(r"-r[0-9]+$", "", stem) for stem in stems]


@pytest.mark.parametrize("name", sorted(COMPANIONS))
def test_overlay_ebuild_matches_the_application(name):
    category = COMPANIONS[name][0]
    assert ebuild_versions(category, name) == [application_version(name)]


@pytest.mark.parametrize("name", sorted(COMPANIONS))
def test_iso_build_scripts_name_the_same_version(name):
    version = application_version(name)
    category = COMPANIONS[name][0]
    found = False
    build = (ROOT / "scripts/build-iso.sh").read_text()
    for match in re.finditer(rf"{category}/{name}/{name}-([0-9][^ \n\t/]*)\.ebuild", build):
        found = True
        assert match.group(1) == version
    entrypoint = (ROOT / "iso/builder-entrypoint.sh").read_text()
    for match in re.finditer(rf"\b{name}={name}-([0-9][^ \n\t\"']*)", entrypoint):
        found = True
        assert match.group(1) == version
    if not found:
        pytest.skip(f"the build scripts no longer name a {name} version literally")


def test_installer_versions_agree():
    import gentoo_installer
    import gentoo_installerd

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert gentoo_installer.__version__ == gentoo_installerd.__version__ == project
    ebuilds = [path.stem for path in (OVERLAY / "app-admin/gentoo-live-installer").glob("gentoo-live-installer-*.ebuild")]
    assert ebuilds
    for stem in ebuilds:
        assert stem.removeprefix("gentoo-live-installer-").split("-r")[0] == project
