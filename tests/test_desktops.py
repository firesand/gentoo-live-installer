"""The desktop registry: one entry per desktop, and every consumer reads it."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from gentoo_installer import desktops, layout, manifest as manifest_module
from gentoo_installer.manifest import ATOM_RE, DESKTOPS as MANIFEST_DESKTOPS, InstallManifest, derive_profile

TREE = Path(os.environ["GENTOO_PINNED_TREE"]) if os.environ.get("GENTOO_PINNED_TREE") else None
GRAPHICAL = [key for key in desktops.DESKTOPS if key != "minimal"]
LIGHTDM_KEYS = {"xfce", "cinnamon", "mate", "i3"}


def test_the_manifest_and_the_registry_agree_on_the_desktops():
    assert MANIFEST_DESKTOPS == desktops.DESKTOPS
    assert set(desktops.DESKTOPS) == {"kde", "gnome", "xfce", "minimal", "cinnamon", "mate", "lxqt", "gnome-light", "sway", "i3"}
    manifest = InstallManifest()
    manifest.storage.device = "/dev/nvme0n1"
    manifest.accounts.password = manifest.accounts.root_password = "pw"
    manifest.system.desktop = "budgie"
    assert any("desktop" in error.lower() for error in manifest.validate())


def test_the_cards_are_the_guided_four_and_the_six_more():
    assert desktops.GUIDED_DESKTOPS == ("kde", "gnome", "xfce", "minimal")
    assert desktops.MORE_DESKTOPS == ("cinnamon", "mate", "lxqt", "gnome-light", "sway", "i3")
    cards = desktops.card_choices(desktops.DESKTOPS)
    assert [card[0] for card in cards] == list(desktops.DESKTOPS)
    assert [card[0] for card in cards if card[3]] == ["kde"]
    by_key = {card[0]: card[2] for card in cards}
    assert "desktop/gnome" in by_key["cinnamon"]  # the card says which profile it needs
    assert "experienced" in by_key["sway"] and "experienced" in by_key["i3"]
    assert "estimated" in by_key["mate"] and "estimated" not in by_key["kde"]


@pytest.mark.parametrize("key", GRAPHICAL)
@pytest.mark.parametrize("init", ["openrc", "systemd"])
def test_every_desktop_has_valid_atoms_a_display_manager_and_an_x_server(key, init):
    atoms = desktops.required_atoms(key, init)
    assert atoms and len(atoms) == len(set(atoms))
    assert all(ATOM_RE.fullmatch(atom) for atom in atoms), atoms
    assert desktops.XORG in atoms
    manager = desktops.REGISTRY[key].dm
    assert manager and all(atom in atoms for atom in desktops.DISPLAY_MANAGERS[manager][0])
    openrc_name, unit = desktops.display_manager(key)
    assert openrc_name and unit
    assert ("app-admin/openrc-settingsd" in atoms) == (init == "openrc" and key in {"cinnamon", "mate"})
    assert bool(desktops.services(key, init)) == (init == "openrc" and key in {"cinnamon", "mate"})


def test_display_managers_follow_the_plan():
    sddm = {key for key in GRAPHICAL if desktops.REGISTRY[key].dm == "sddm"}
    gdm = {key for key in GRAPHICAL if desktops.REGISTRY[key].dm == "gdm"}
    lightdm = {key for key in GRAPHICAL if desktops.REGISTRY[key].dm == "lightdm"}
    assert sddm == {"kde", "lxqt", "sway"} and gdm == {"gnome", "gnome-light"} and lightdm == LIGHTDM_KEYS
    assert desktops.display_manager("kde") == ("sddm", "sddm") and desktops.display_manager("minimal") == ("", "")


def test_profiles():
    assert desktops.profile_suffix("kde") == "/desktop/plasma"
    for key in ("gnome", "gnome-light", "cinnamon"):
        assert desktops.profile_suffix(key) == "/desktop/gnome"
    for key in ("xfce", "mate", "lxqt", "sway", "i3"):
        assert desktops.profile_suffix(key) == "/desktop"
    assert desktops.profile_suffix("minimal") == ""
    assert derive_profile("systemd", "cinnamon").endswith("/desktop/gnome/systemd")
    assert derive_profile("openrc", "mate").endswith("/23.0/desktop")
    assert derive_profile("openrc", "minimal").endswith("/23.0")


def test_cinnamon_is_refused_on_the_families_without_the_gnome_profile():
    for family in ("hardened", "nomultilib"):
        assert not desktops.family_supports("cinnamon", family)
        assert "desktop/gnome" in desktops.family_problem("cinnamon", family)
    assert desktops.family_supports("cinnamon", "standard") and desktops.family_supports("cinnamon", "desktop")
    assert desktops.unverified("mate", "hardened") and not desktops.unverified("mate", "standard")


def test_nm_applet_only_with_networkmanager():
    for key in ("xfce", "cinnamon", "mate", "lxqt", "sway", "i3"):
        assert desktops.NM_APPLET in desktops.required_atoms(key, "openrc")
        assert desktops.NM_APPLET not in desktops.required_atoms(key, "openrc", network_manager="dhcpcd")


def test_the_four_original_desktops_keep_their_package_sets():
    assert desktops.required_atoms("kde", "openrc") == ["kde-plasma/plasma-meta", "kde-apps/kdecore-meta", "gui-apps/sddm", "x11-base/xorg-server"]
    assert desktops.required_atoms("kde", "openrc", "full")[1] == "kde-apps/kde-apps-meta"
    assert desktops.required_atoms("gnome", "systemd") == ["gnome-base/gnome", "gnome-base/gdm", "x11-base/xorg-server"]
    assert desktops.required_atoms("minimal", "openrc") == []


def test_plasma_gets_discover_flatpak_only_with_flatpak():
    assert "kde-plasma/plasma-meta" not in desktops.package_use("kde")
    assert desktops.package_use("kde", flatpak=True)["kde-plasma/plasma-meta"] == ("discover", "flatpak")


def test_sizes_are_in_every_table_and_unchanged_for_the_original_four():
    for key in desktops.DESKTOPS:
        assert key in layout.MIN_ROOT_MIB and key in layout.RECOMMENDED_ROOT_MIB and key in layout.SOURCE_BUILD_EXTRA_MIB
        assert layout.MIN_ROOT_MIB[key] <= layout.RECOMMENDED_ROOT_MIB[key]
    assert (layout.MIN_ROOT_MIB["kde"], layout.RECOMMENDED_ROOT_MIB["kde"], layout.SOURCE_BUILD_EXTRA_MIB["kde"]) == (24 * 1024, 40 * 1024, 16 * 1024)
    assert layout.MIN_ROOT_MIB["xfce"] == 14 * 1024 and layout.MIN_ROOT_MIB["gnome"] == 18 * 1024 and layout.MIN_ROOT_MIB["minimal"] == 8 * 1024


def test_a_loaded_card_list_is_checked_against_the_registry_not_a_copy():
    assert manifest_module.FAMILY_DESKTOP_SUPPORT == desktops.family_desktop_support()
    assert manifest_module.DESKTOPS is desktops.DESKTOPS or manifest_module.DESKTOPS == desktops.DESKTOPS


# --- evidence from the pinned tree (opt-in, like tests/hardware/test_tree_evidence.py) ---------------------------------------------

needs_tree = pytest.mark.skipif(TREE is None or not TREE.is_dir(), reason="set GENTOO_PINNED_TREE to the pinned ::gentoo snapshot")


def _stable_amd64(atom: str) -> bool:
    category, _, name = atom.partition("/")
    directory = TREE / category / name
    if not directory.is_dir():
        return False
    for ebuild in directory.glob("*.ebuild"):
        text = ebuild.read_text(errors="replace")
        match = re.search(r'^KEYWORDS="([^"]*)"', text, re.M)
        if match and "amd64" in match.group(1).split():
            return True
    return False


@needs_tree
@pytest.mark.parametrize("key", GRAPHICAL)
def test_every_registry_atom_has_a_stable_amd64_ebuild_in_the_tree(key):
    entry = desktops.REGISTRY[key]
    atoms = {*entry.required, *entry.portals, *(a for atoms_ in entry.init_atoms.values() for a in atoms_), desktops.FIREFOX, desktops.FIREFOX_BIN, desktops.FLATPAK}
    atoms.discard(desktops.KDE_APPS_CORE)
    missing = sorted(atom for atom in atoms if not _stable_amd64(atom))
    assert not missing, f"{key}: no stable amd64 ebuild for {missing}"
