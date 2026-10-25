"""Hardware I01: ebuilds that the installer needs and ::gentoo does not have, vendored into the companion overlay."""
from __future__ import annotations

import configparser
from pathlib import Path
import re
from xml.etree import ElementTree

import pytest

from .conftest import ROOT

OVERLAY = ROOT / "packaging/gentoo-overlay"
# category/name of every vendored package (the project's own packages are in test_ebuilds.py)
VENDORED = ["gnome-extra/iio-sensor-proxy"]
GURU = Path("/var/db/repos/guru")


def package_dir(atom: str) -> Path:
    return OVERLAY / atom


def ebuild(atom: str) -> Path:
    found = sorted(package_dir(atom).glob("*.ebuild"))
    assert len(found) == 1, f"{atom} needs exactly one vendored version, found {[f.name for f in found]}"
    return found[0]


def dist_lines(atom: str) -> list[list[str]]:
    return [line.split() for line in (package_dir(atom) / "Manifest").read_text().splitlines() if line.strip()]


@pytest.mark.parametrize("atom", VENDORED)
def test_the_manifest_pins_every_distfile_with_all_hashes_of_the_overlay(atom: str) -> None:
    layout = configparser.ConfigParser(interpolation=None)
    layout.read_string("[x]\n" + (OVERLAY / "metadata/layout.conf").read_text())
    wanted = layout["x"]["manifest-hashes"].split()
    text = ebuild(atom).read_text()
    names = re.search(r'SRC_URI="[^"]*/([^"/]*)"', text).group(1).replace("${P}", ebuild(atom).stem).replace("${PV}", ebuild(atom).stem.rsplit("-", 1)[1])
    lines = dist_lines(atom)
    assert [l[1] for l in lines] == [names]
    for line in lines:
        assert line[0] == "DIST" and line[2].isdigit() and int(line[2]) < 1_000_000, "a small tarball: the ISO ships it"
        hashes = dict(zip(line[3::2], line[4::2]))
        assert set(hashes) == set(wanted), (set(hashes), wanted)
        assert len(hashes["BLAKE2B"]) == 128 and len(hashes["SHA512"]) == 128 and len(hashes["SHA256"]) == 64
        assert all(re.fullmatch(r"[0-9a-f]+", v) for v in hashes.values())


@pytest.mark.skipif(not (GURU / "gnome-extra/iio-sensor-proxy").is_dir(), reason="a local ::guru checkout is needed")
def test_the_vendored_copy_is_the_guru_ebuild_with_the_documented_changes() -> None:
    upstream = GURU / "gnome-extra/iio-sensor-proxy"
    ours = package_dir("gnome-extra/iio-sensor-proxy")
    for name in ("files/iio-sensor-proxy", "files/no-systemd.patch", "metadata.xml"):
        assert (ours / name).read_bytes() == (upstream / name).read_bytes(), name
    # same tarball: BLAKE2B and SHA512 of ::guru's Manifest
    guru_line = next(l.split() for l in (upstream / "Manifest").read_text().splitlines() if " iio-sensor-proxy-3.9.tar.bz2 " in l)
    mine = dist_lines("gnome-extra/iio-sensor-proxy")[0]
    assert dict(zip(guru_line[3::2], guru_line[4::2]))["BLAKE2B"] == dict(zip(mine[3::2], mine[4::2]))["BLAKE2B"]
    assert dict(zip(guru_line[3::2], guru_line[4::2]))["SHA512"] == dict(zip(mine[3::2], mine[4::2]))["SHA512"]
    assert guru_line[2] == mine[2]
    # the code of the ebuild is the same apart from the comment, KEYWORDS and the notice
    def code(path: Path) -> list[str]:
        body = path.read_text().splitlines()
        return [l for l in body if l.strip() and not l.lstrip().startswith("#") and "elog" not in l and not l.startswith("KEYWORDS=")]

    assert code(ebuild("gnome-extra/iio-sensor-proxy")) == code(upstream / "iio-sensor-proxy-3.9.ebuild")


@pytest.mark.parametrize("atom", VENDORED)
def test_the_ebuild_says_where_it_comes_from_and_why(atom: str) -> None:
    text = ebuild(atom).read_text()
    assert re.search(r"github\.com/gentoo-mirror/guru, master at [0-9a-f]{40}", text), "the ::guru commit is the pin"
    assert "never `eselect repository enable guru`" in text
    assert text.startswith("# Copyright") and "\nEAPI=8\n" in text
    assert re.search(r'^KEYWORDS="amd64"$', text, re.M), "the copy is keyworded stable: its dependencies are (checked below)"
    assert 'SRC_URI="https://' in text
    root = ElementTree.parse(package_dir(atom) / "metadata.xml").getroot()
    assert root.tag == "pkgmetadata"


def test_nothing_enables_or_syncs_guru() -> None:
    """Pinned ebuilds are copied into the overlay; the installed system never gets ::guru."""
    for base in ("iso", "packaging", "scripts", "src"):
        for path in (ROOT / base).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".sh", ".ebuild", ".spec", ".conf", ".json", ""} and "__pycache__" not in path.parts:
                text = "\n".join(l for l in path.read_text(errors="replace").splitlines() if not l.lstrip().startswith("#"))
                assert not re.search(r"eselect\s+repository\s+(enable|add)\s+guru|layman.*guru|emerge --sync\s+guru", text), path


def test_the_live_image_does_not_install_the_vendored_packages() -> None:
    """They are for the installed system, and only when a plan atom selects them."""
    spec = (ROOT / "iso/specs/livecd-stage1.spec").read_text()
    for atom in VENDORED:
        assert atom not in spec


def atoms(depend: str) -> list[str]:
    result = []
    for token in re.findall(r"[^\s()]+", depend):
        if token.endswith("?") or token in ("||", "!"):
            continue
        token = re.sub(r"\[[^\]]*\]$", "", token)
        token = re.sub(r"^[<>=~!]+", "", token)
        token = re.sub(r":.*$", "", token)
        token = re.sub(r"-[0-9][0-9A-Za-z._]*(?:-r[0-9]+)?$", "", token)
        if re.fullmatch(r"[a-z0-9-]+/[A-Za-z0-9_.+-]+", token):
            result.append(token)
    return result


@pytest.mark.parametrize("atom", VENDORED)
def test_dependencies_are_stable_amd64_in_the_pinned_tree(atom: str, pinned_tree: Path) -> None:
    text = ebuild(atom).read_text()
    wanted = set()
    for variable in ("RDEPEND", "DEPEND"):
        wanted |= set(atoms(re.search(rf'^{variable}="(.*?)"', text, re.M | re.S).group(1)))
    wanted -= {"${RDEPEND}"}
    assert {"dev-libs/libgudev", "sys-auth/polkit", "virtual/udev"} <= wanted
    # the systemd branch is off (USE=-systemd) on the OpenRC systems of the installer
    wanted -= {"sys-apps/systemd", "sys-apps/openrc"}
    for dependency in sorted(wanted):
        ebuilds = sorted((pinned_tree / dependency).glob("*.ebuild"))
        assert ebuilds, f"{dependency} is not in the pinned tree"
        assert any(re.search(r'^\s*KEYWORDS="[^"]*(?<![~-])\bamd64\b', e.read_text(), re.M) for e in ebuilds if "9999" not in e.name), f"{dependency} has no stable amd64 version"
    for eclass in re.search(r"^inherit (.*)$", text, re.M).group(1).split():
        assert (pinned_tree / "eclass" / f"{eclass}.eclass").is_file(), eclass


@pytest.mark.parametrize("atom", VENDORED)
def test_the_package_is_not_in_the_pinned_tree_so_the_copy_is_needed(atom: str, pinned_tree: Path) -> None:
    """When ::gentoo gets the package, the pin-bump checklist (docs/ISO.md) says: delete the copy."""
    assert not (pinned_tree / atom).is_dir(), f"{atom} is in ::gentoo now: remove the vendored copy and its Manifest"
