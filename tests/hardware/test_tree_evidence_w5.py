"""Tree evidence for the Wave 5 hardware plan (opt-in, like test_tree_evidence.py: set GENTOO_PINNED_TREE to the ::gentoo snapshot).

Static reads of the ebuilds: every atom the new plan fields name exists, every exact version that gets a ~amd64 keyword exists and is
testing, every USE flag the plan sets is in the package's IUSE, every service has its init script, and the NVIDIA branches accept the
stable distribution kernel. Overlay packages (edorp, GURU) cannot be checked against ::gentoo: they are checked against a checkout when
GENTOO_EDORP_TREE / GENTOO_GURU_TREE point at one, and skipped (not passed) otherwise.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from gentoo_installer import hardware_data as data
from gentoo_installerd import hwplan

from tests.hardware.builders import gpu, manifest, profile, systemd
from tests.hardware.helpers import FIXTURE_NAMES, fixture_dict
from tests.hardware.test_tree_evidence import TREE, needs_tree

EDORP_TREE = Path(os.environ["GENTOO_EDORP_TREE"]) if os.environ.get("GENTOO_EDORP_TREE") else None
GURU_TREE = Path(os.environ["GENTOO_GURU_TREE"]) if os.environ.get("GENTOO_GURU_TREE") else None
OVERLAY_ATOMS = {"sys-power/asusctl": "edorp", "sys-power/supergfxctl": "edorp", "gnome-extra/iio-sensor-proxy": "guru"}
ECLASS_FLAGS = {"dist-kernel"}      # IUSE that comes from linux-mod-r1.eclass, not from the ebuild

ALL_DEVICES = {"openrazer": True, "razer_frontend": True, "openrgb": True, "game_rules": True, "solaar": True, "iio_sensors": True, "bolt": True,
               "wacom": True, "fingerprint": True, "asus_tools": True, "thinkfan": True}


def plans():
    """Plans that between them use every atom, keyword, flag and service of the Wave 5 additions."""
    out = []
    for name in FIXTURE_NAMES:
        machine = fixture_dict(name)
        for desktop in ("kde", "gnome", "xfce"):
            for maker in (manifest, systemd):
                m = maker(system={"desktop": desktop}, hardware={"devices": ALL_DEVICES, "fwupd": True, "gpu_driver": "nvidia", "nvidia_license_accepted": True,
                                                                  "nvidia_extras": ["vaapi", "cuda", "nvtop", "persistenced"], "nvidia_cuda_license_accepted": True},
                          options={"accessibility": True, "input_method": "fcitx5"})
                out.append(hwplan.plan(m, machine))
    ja = manifest(system={"locale": "ja_JP.UTF-8"}, options={"accessibility": True})
    out.append(hwplan.plan(ja, profile(gpus=(gpu("nvidia", "ampere_plus"),))))
    return out


def ebuilds(atom: str) -> list[Path]:
    category, name = atom.split("/")
    return sorted((TREE / category / name).glob(f"{name}-[0-9]*.ebuild"))


def keywords(path: Path) -> list[str]:
    match = re.search(r'^\s*KEYWORDS="([^"]*)"', path.read_text(), re.M)
    return match.group(1).split() if match else []


def iuse(path: Path) -> set[str]:
    text = path.read_text()
    flags: set[str] = set()
    for match in re.finditer(r'^\s*IUSE\+?="([^"]*)"', text, re.M | re.S):
        flags |= {flag.lstrip("+-") for flag in match.group(1).split()}
    for match in re.finditer(r'IUSE="([^"]*)"', text, re.S):
        flags |= {flag.lstrip("+-") for flag in match.group(1).split()}
    return flags


@needs_tree
def test_every_atom_of_the_new_plan_fields_exists_in_the_tree_or_is_an_overlay_package():
    missing: list[str] = []
    for result in plans():
        for atom in result.all_atoms():
            if atom in OVERLAY_ATOMS or atom.split(":")[0] in OVERLAY_ATOMS:
                continue
            if not ebuilds(atom.split(":")[0]):
                missing.append(atom)
    assert not sorted(set(missing)), sorted(set(missing))


@needs_tree
def test_every_exact_version_that_gets_a_testing_keyword_exists_and_is_testing():
    seen: set[str] = set()
    for result in plans():
        for atom, words in result.package_keywords.items():
            assert words == ["~amd64"], atom
            seen.add(atom)
            match = re.fullmatch(r"=([a-z0-9-]+/[A-Za-z0-9_+-]+)-([0-9][A-Za-z0-9_.+]*)", atom)
            assert match, atom
            package, version = match.groups()
            if package in OVERLAY_ATOMS:
                continue
            path = TREE / package / f"{package.split('/')[1]}-{version}.ebuild"
            assert path.exists(), f"{atom}: that version is not in the tree"
            found = keywords(path)
            assert "~amd64" in found and "amd64" not in found, f"{atom} is {found}: the keyword is needless or the version is not keyworded for amd64"
    assert {"=games-util/game-device-udev-rules-20260625", "=media-libs/nvidia-vaapi-driver-0.0.18", "=dev-util/nvidia-cuda-toolkit-12.9.2"} <= seen


@needs_tree
def test_everything_without_a_keyword_is_stable_on_amd64():
    unstable: set[str] = set()
    for result in plans():
        for atom in result.all_atoms():
            package = atom.split(":")[0]
            if package in OVERLAY_ATOMS or any(key.endswith(f"/{package.split('/')[1]}") or f"{package}-" in key for key in result.package_keywords):
                continue
            stable = [path for path in ebuilds(package) if "amd64" in keywords(path)]
            if not stable:
                unstable.add(package)
    assert not sorted(unstable), sorted(unstable)


@needs_tree
def test_every_use_flag_the_new_plan_sets_exists_in_the_packages_iuse():
    problems: set[str] = set()
    for result in plans():
        for atom, flags in result.package_use.items():
            package = atom.split(":")[0]
            if package in OVERLAY_ATOMS or package == "*/*":
                continue
            paths = ebuilds(package)
            if not paths:
                continue
            known = set().union(*(iuse(path) for path in paths)) | ECLASS_FLAGS
            # the pinned stable version decides, but a flag that exists in some version is the most a static read can prove
            for flag in flags:
                if flag.lstrip("-") not in known:
                    problems.add(f"{atom}: no USE flag {flag}")
    assert not sorted(problems), sorted(problems)


@needs_tree
def test_the_nvidia_use_flags_follow_the_branches():
    text_595 = (TREE / "x11-drivers/nvidia-drivers/nvidia-drivers-595.104.02.ebuild").read_text()
    text_580 = (TREE / "x11-drivers/nvidia-drivers/nvidia-drivers-580.178.04.ebuild").read_text()
    assert "kernel-open" not in re.search(r'IUSE="([^"]*)"', text_595).group(1)       # 595 has no such flag: its modules are always the open ones
    assert "kernel-open" in re.search(r'IUSE="([^"]*)"', text_580).group(1)
    for text in (text_595, text_580):
        assert {"wayland", "persistenced"} <= {flag.lstrip("+") for flag in re.search(r'IUSE="([^"]*)"', text).group(1).split()}
    # the wayland flag pulls the EGL libraries the plan says it does
    assert all(name in text_595 for name in ("egl-gbm", "egl-wayland", "egl-wayland2", "egl-x11"))
    assert "newinitd" in text_595 and "nvidia-persistenced" in text_595


@needs_tree
def test_the_stable_distribution_kernel_is_within_what_the_nvidia_branches_accept():
    def version(text: str) -> tuple[int, ...]:
        return tuple(int(part) for part in text.split("."))

    stable = [version(re.fullmatch(r"gentoo-kernel-bin-(.+)\.ebuild", path.name).group(1)) for path in (TREE / "sys-kernel/gentoo-kernel-bin").glob("*.ebuild")
              if "amd64" in keywords(path)]
    assert stable
    newest = max(stable)
    for branch in data.NVIDIA_BRANCHES:
        stable_drivers = [path for path in ebuilds(data.NVIDIA_ATOM) if path.name.startswith(f"nvidia-drivers-{branch}.") and "amd64" in keywords(path)]
        assert stable_drivers, f"no stable ebuild for the {branch} branch: the plan would fall back to nouveau"
        limit = version(re.search(r"^MODULES_KERNEL_MAX=(\S+)", stable_drivers[-1].read_text(), re.M).group(1))
        assert newest[:len(limit)] <= limit, f"the stable kernel {newest} is newer than driver {branch} accepts ({limit})"


SERVICE_EVIDENCE = {
    "boltd": ("sys-apps/bolt", r"newinitd\s+\S+\s+boltd"),
    "fwupd": ("sys-apps/fwupd", r"newinitd\s+.*\$\{PN\}"),
    "nvidia-persistenced": ("x11-drivers/nvidia-drivers", r"nvidia-persistenced"),
}


@needs_tree
@pytest.mark.parametrize("service", sorted(SERVICE_EVIDENCE))
def test_the_new_openrc_services_exist_in_their_packages(service):
    atom, pattern = SERVICE_EVIDENCE[service]
    assert any(re.search(pattern, path.read_text()) for path in ebuilds(atom)), service


@needs_tree
def test_the_planned_dist_kernel_flag_belongs_to_a_linux_mod_package():
    openrazer = ebuilds("sys-apps/openrazer")[-1].read_text()
    assert "linux-mod-r1" in openrazer
    eclass = (TREE / "eclass/linux-mod-r1.eclass").read_text()
    assert re.search(r'IUSE="[^"]*dist-kernel', eclass)


def overlay_ebuilds(tree: Path | None, atom: str) -> list[Path]:
    if tree is None or not tree.is_dir():
        pytest.skip("set GENTOO_EDORP_TREE / GENTOO_GURU_TREE to a checkout of the overlay: its packages cannot be checked against ::gentoo")
    category, name = atom.split("/")
    return sorted((tree / category / name).glob(f"{name}-[0-9]*.ebuild"))


def test_the_asus_tools_in_the_edorp_overlay_match_the_plan():
    for atom, version in (("sys-power/asusctl", "6.5.0"), ("sys-power/supergfxctl", "5.2.7")):
        found = overlay_ebuilds(EDORP_TREE, atom)
        assert any(path.name == f"{atom.split('/')[1]}-{version}.ebuild" for path in found), f"{atom}-{version} is not in the overlay"
        pinned = next(path for path in found if path.name.endswith(f"-{version}.ebuild")).read_text()
        assert "systemd" in re.search(r'IUSE="([^"]*)"', pinned).group(1)


def test_the_gnome_iio_sensor_proxy_in_guru_is_the_planned_version():
    assert any(path.name == "iio-sensor-proxy-3.9.ebuild" for path in overlay_ebuilds(GURU_TREE, "gnome-extra/iio-sensor-proxy"))


def test_the_plan_names_the_two_overlay_repositories_for_the_asus_tools_with_https_urls():
    result = hwplan.plan(manifest(hardware={"devices": {"asus_tools": True}}), fixture_dict("asus-rog-laptop"))
    assert result.repos == ["guru git https://github.com/gentoo/guru.git", "edorp git https://github.com/firesand/edorp-overlay.git"]
