"""Tree evidence for the Wave 5 atoms (hardware backlog H02): every atom the plans can name exists in the pinned ::gentoo snapshot with the keyword,
the USE flags and the licence the planner relies on. Opt-in like test_tree_evidence.py: set GENTOO_PINNED_TREE to the snapshot.

The tests fail when a stable atom becomes ~amd64 or disappears, when an atom the planner treats as testing-only becomes stable (its keyword line is then
needless), when a USE flag the plans switch on is renamed, and when an out-of-tree atom is neither in ::gentoo nor in the shipped overlay.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from gentoo_installer import hardware_data as data
from gentoo_installerd import hwplan

from tests.hardware.builders import profile
from tests.hardware.helpers import fixture_dict
from tests.hardware.test_plan_golden import scenario_manifest
from tests.hardware.test_tree_evidence import case_for, has_portage

TREE = Path(os.environ["GENTOO_PINNED_TREE"]) if os.environ.get("GENTOO_PINNED_TREE") else None
needs_tree = pytest.mark.skipif(TREE is None or not TREE.is_dir(), reason="set GENTOO_PINNED_TREE to the pinned ::gentoo snapshot")
ROOT = Path(__file__).resolve().parents[2]

# Atoms the Wave 5 plans install, with the state the planner assumes. "stable": some ebuild is stable on amd64. "testing": only ~amd64 exists, so the plan
# must carry a keyword line (the check tells you when that line can go).
STABLE = (
    "sys-apps/bolt", "sys-power/tlp", "gnome-extra/nm-applet", "gnome-extra/cinnamon", "www-client/firefox", "x11-misc/prime-run",
    "sys-power/switcheroo-control", "sys-apps/openrazer", "sys-apps/polychromatic", "sys-apps/razergenie", "app-misc/solaar", "app-misc/openrgb",
    "sys-apps/fwupd", "app-laptop/thinkfan", "sys-apps/flatpak", "media-video/pipewire", "media-video/wireplumber", "sys-power/power-profiles-daemon",
    "gui-libs/egl-gbm", "gui-libs/egl-wayland", "gui-libs/egl-wayland2", "gui-libs/egl-x11", "x11-drivers/nvidia-drivers", "kde-plasma/plasma-meta",
    "gnome-base/gdm", "sys-kernel/gentoo-kernel-bin", "sys-kernel/linux-firmware", "sys-auth/fprintd", "mate-base/mate", "lxqt-base/lxqt-meta",
    "gnome-base/gnome-light", "gui-wm/sway", "x11-wm/i3", "xfce-base/xfce4-meta", "gnome-base/gnome", "games-util/gamemode",
)
TESTING_ONLY = ("games-util/game-device-udev-rules", "media-libs/nvidia-vaapi-driver", "gui-wm/gamescope")
KEYWORD_LINE = re.compile(r'^\s*KEYWORDS="([^"]*)"', re.M)
IUSE_LINE = re.compile(r'^\s*IUSE="([^"]*)"', re.M)


def ebuilds(atom: str) -> list[Path]:
    assert TREE is not None
    category, package = atom.split("/")
    return sorted((TREE / category / package).glob("*.ebuild"))


def stable(path: Path) -> bool:
    """Some KEYWORDS line of the ebuild (they may sit inside an if block) lists amd64 without a tilde."""
    return any("amd64" in match.split() for match in KEYWORD_LINE.findall(path.read_text()))


def iuse(path: Path) -> set[str]:
    return {flag.lstrip("+-") for match in IUSE_LINE.findall(path.read_text()) for flag in match.split()}


def version_of(path: Path, atom: str) -> tuple[int, ...]:
    text = path.name[len(atom.split("/")[1]) + 1:-len(".ebuild")]
    return tuple(int(part) for part in re.findall(r"\d+", text.split("-r")[0]))


@needs_tree
@pytest.mark.parametrize("atom", STABLE)
def test_a_stable_atom_the_plans_install_has_a_stable_ebuild(atom):
    found = ebuilds(atom)
    assert found, f"{atom} disappeared from the tree"
    assert any(stable(path) for path in found), f"{atom} has no stable amd64 ebuild any more ({[p.name for p in found]}): the plans need a keyword line or another atom"


@needs_tree
@pytest.mark.parametrize("atom", TESTING_ONLY)
def test_a_testing_only_atom_is_still_testing_only(atom):
    found = ebuilds(atom)
    assert found, f"{atom} disappeared from the tree"
    assert not any(stable(path) for path in found), f"{atom} is stable now: drop its keyword line from the plan and move it to STABLE here"


@needs_tree
def test_kernel_open_is_a_flag_of_580_and_gone_from_595():
    stable_580 = [p for p in ebuilds(data.NVIDIA_ATOM) if p.name.startswith("nvidia-drivers-580.") and stable(p)]
    stable_595 = [p for p in ebuilds(data.NVIDIA_ATOM) if p.name.startswith("nvidia-drivers-595.") and stable(p)]
    assert stable_580 and stable_595
    assert all("kernel-open" in iuse(p) for p in stable_580), "580 lost USE=kernel-open: the Turing RTD3 decision and the closed-module path change"
    assert all("kernel-open" not in iuse(p) for p in stable_595), "595 has USE=kernel-open again: the planner must not assume the open modules are forced"
    assert all("wayland" in iuse(p) for p in stable_580 + stable_595), "the wayland flag pulls egl-wayland and egl-x11 for NVIDIA on Wayland"


@needs_tree
@pytest.mark.parametrize(("atom", "flags"), [
    ("kde-plasma/plasma-meta", {"thunderbolt", "wacom", "virtualkeyboard"}),
    ("gnome-base/gdm", {"fprint"}),
    ("sys-apps/fwupd", {"gnutls"}),
    ("x11-drivers/nvidia-drivers", {"wayland", "persistenced", "tools", "X"}),
])
def test_the_use_flags_the_plans_switch_on_exist(atom, flags):
    candidates = [p for p in ebuilds(atom) if stable(p)]
    if atom == data.NVIDIA_ATOM:  # the branches the planner knows (390 and 470 are masked in the profile)
        candidates = [p for p in candidates if p.name.startswith(tuple(f"nvidia-drivers-{branch}." for branch in data.NVIDIA_BRANCHES))]
    assert candidates, atom
    for path in candidates:
        assert flags <= iuse(path), f"{path.name} lacks {sorted(flags - iuse(path))}"


@needs_tree
def test_the_nvidia_licence_is_real_and_not_free():
    licences = {}
    for line in (TREE / "profiles/license_groups").read_text().splitlines():  # type: ignore[operator]
        if line.strip() and not line.startswith("#"):
            name, _, members = line.partition(" ")
            licences[name] = members.split()
    free: set[str] = set()

    def expand(name: str) -> None:
        for member in licences.get(name, []):
            expand(member[1:]) if member.startswith("@") else free.add(member)

    expand("FREE")
    branches = tuple(f"nvidia-drivers-{branch}." for branch in data.NVIDIA_BRANCHES)
    for path in (p for p in ebuilds(data.NVIDIA_ATOM) if stable(p) and p.name.startswith(branches)):
        text = path.read_text()
        match = re.search(r'LICENSE="\s*([^"]*)"', text, re.S)
        tokens = {token for token in re.findall(r"[A-Za-z][A-Za-z0-9._+-]*", match.group(1)) if (TREE / "licenses" / token).exists()}  # type: ignore[operator]
        assert any(token in tokens for token in {str(info["license"]) for info in data.NVIDIA_BRANCHES.values()}), f"{path.name}: licences {sorted(tokens)}"
        assert str(data.NVIDIA_BRANCHES["595"]["license"]) not in free


@needs_tree
def test_the_stable_kernel_is_not_newer_than_the_nvidia_modules_can_build_for():
    """MODULES_KERNEL_MAX of each stable branch must cover the newest stable gentoo-kernel-bin, or the driver fails to build after the kernel step."""
    kernels = [version_of(p, "sys-kernel/gentoo-kernel-bin") for p in ebuilds("sys-kernel/gentoo-kernel-bin") if stable(p)]
    assert kernels
    newest = max(kernels)[:2]
    for branch in data.NVIDIA_BRANCHES:
        for path in (p for p in ebuilds(data.NVIDIA_ATOM) if p.name.startswith(f"nvidia-drivers-{branch}.") and stable(p)):
            match = re.search(r"^MODULES_KERNEL_MAX=(\d+)\.(\d+)", path.read_text(), re.M)
            assert match, f"{path.name} has no MODULES_KERNEL_MAX"
            assert newest <= (int(match.group(1)), int(match.group(2))), \
                f"{path.name} builds for kernels up to {match.group(1)}.{match.group(2)}, the stable gentoo-kernel-bin is {'.'.join(map(str, newest))}"


@needs_tree
def test_an_atom_the_planner_names_is_in_the_tree_or_in_the_shipped_overlay():
    overlay = ROOT / "packaging" / "gentoo-overlay"
    for name, atom in sorted((n, v) for n, v in vars(hwplan).items() if n.startswith("A_") and isinstance(v, str)):
        in_tree = bool(ebuilds(atom)) or atom == "app-misc/hwmonux"
        in_overlay = bool(list((overlay / atom).glob("*.ebuild"))) if (overlay / atom).is_dir() else False
        assert in_tree or in_overlay, f"{name} = {atom} is neither in ::gentoo nor in packaging/gentoo-overlay"
        if atom not in ("app-misc/hwmonux",):
            assert not (in_tree and in_overlay), f"{atom} is in both ::gentoo and the shipped overlay: the overlay copy is redundant"


@needs_tree
def test_overlay_only_atoms_are_not_in_gentoo_yet():
    """These come from GURU or the user's overlay (edorp). If ::gentoo gets them, the overlay entry in the plan and the notice for newcomers go."""
    for atom in ("gnome-extra/iio-sensor-proxy", "sys-power/rog-daemon", "sys-power/asusctl", "sys-power/supergfxctl", "games-util/steam-launcher"):
        assert not ebuilds(atom), f"{atom} is in ::gentoo now: the plan no longer needs an overlay for it"


@needs_tree
@pytest.mark.skipif(not has_portage(), reason="the Portage Python API is not importable")
def test_the_pascal_mask_leaves_the_stable_580_visible_and_hides_the_newer_branches(tmp_path):
    """Hardware critique 1: '>=nvidia-drivers-581' must not hide the only stable branch the card can use."""
    machine = fixture_dict("pascal-desktop-gtx1060")
    request = case_for("pascal/nvidia-this", scenario_manifest("nvidia-this", machine), machine)
    assert any("nvidia-drivers" in line for line in request["package_mask"]), "the plan for Pascal carries no NVIDIA mask"
    process = subprocess.run([sys.executable, str(Path(__file__).with_name("tree_check.py")), str(TREE), str(tmp_path / "portage")], input=json.dumps([request]),
                             capture_output=True, text=True, timeout=1800)
    assert process.returncode == 0, process.stderr[-2000:]
    outcome = json.loads(process.stdout)[0]["atoms"][data.NVIDIA_ATOM]
    assert outcome["visible"] and outcome["visible"].startswith("nvidia-drivers-580."), f"Pascal would get {outcome['visible']}: {outcome['why']}"


@needs_tree
@pytest.mark.skipif(not has_portage(), reason="the Portage Python API is not importable")
def test_the_blackwell_plan_gets_the_open_595(tmp_path):
    machine = fixture_dict("blackwell-desktop-rtx5090")
    request = case_for("blackwell/nvidia-this", scenario_manifest("nvidia-this", machine), machine)
    process = subprocess.run([sys.executable, str(Path(__file__).with_name("tree_check.py")), str(TREE), str(tmp_path / "portage")], input=json.dumps([request]),
                             capture_output=True, text=True, timeout=1800)
    assert process.returncode == 0, process.stderr[-2000:]
    assert json.loads(process.stdout)[0]["atoms"][data.NVIDIA_ATOM]["visible"].startswith("nvidia-drivers-595.")
    assert profile  # the builders stay imported for the cases that need a made-up machine


# --- the checks themselves, on a made-up tree (they run without GENTOO_PINNED_TREE) -------------------------------------------

def fake_tree(base: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return base


def test_stable_keywords_are_read_even_inside_a_version_block(tmp_path, monkeypatch):
    tree = fake_tree(tmp_path, {
        "app-misc/stable/stable-1.ebuild": 'EAPI=8\nKEYWORDS="amd64 ~arm64"\n',
        "app-misc/block/block-1.ebuild": 'if [[ ${PV} == 9999 ]]; then\n\tinherit git-r3\nelse\n\tKEYWORDS="amd64 x86"\nfi\n',
        "app-misc/testing/testing-1.ebuild": 'KEYWORDS="~amd64 ~arm64"\n',
        "app-misc/minus/minus-1.ebuild": 'KEYWORDS="-* ~amd64"\n',
        "app-misc/live/live-9999.ebuild": 'KEYWORDS=""\n',
    })
    monkeypatch.setattr(sys.modules[__name__], "TREE", tree)
    results = {name: [stable(path) for path in ebuilds(f"app-misc/{name}")] for name in ("stable", "block", "testing", "minus", "live")}
    assert results == {"stable": [True], "block": [True], "testing": [False], "minus": [False], "live": [False]}
    assert ebuilds("app-misc/gone") == [], "an atom that disappeared has no ebuilds, which the stable check reports"


def test_iuse_flags_lose_their_default_markers_across_lines(tmp_path):
    ebuild = tmp_path / "x.ebuild"
    ebuild.write_text('IUSE="\n\t+X abi_x86_32\n\tkernel-open -wayland\n"\n')
    assert iuse(ebuild) == {"X", "abi_x86_32", "kernel-open", "wayland"}


def test_versions_compare_as_numbers_not_text():
    assert version_of(Path("gentoo-kernel-bin-6.18.58-r1.ebuild"), "sys-kernel/gentoo-kernel-bin") == (6, 18, 58)
    assert max([(6, 9, 1), (6, 18, 57)]) == (6, 18, 57)
    assert version_of(Path("nvidia-drivers-595.104.02.ebuild"), "x11-drivers/nvidia-drivers")[:2] == (595, 104)
