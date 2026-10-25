"""PORT: the pure modules behind the Portage choices (stage and profile choice, USE flags, mirrors, MAKEOPTS, licences)."""

from __future__ import annotations

import re

import pytest

from gentoo_installer import licenses, makeopts, mirrors, optimize, stagechoice, usedesc
from gentoo_installer.manifest import (
    DESKTOPS, PROFILE_BASE, PUBLISHED_PROFILES, STAGE3_VARIANTS, InstallManifest, derive_profile, desktop_problem, profile_problem,
)
from gentoo_installer.profiles_data import AMD64_PROFILES
from gentoo_installer.usedesc_data import GLOBAL_USE, VIDEO_CARDS

# --- PORT-08: stage and profile ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(("desktop", "variant"), [
    ("kde", "amd64-desktop-openrc"), ("gnome", "amd64-desktop-openrc"), ("xfce", "amd64-desktop-openrc"),
    ("sway", "amd64-desktop-openrc"), ("minimal", "amd64-openrc"),
])
def test_the_desktop_decides_between_the_desktop_and_the_standard_stage3(desktop, variant):
    assert stagechoice.derive_stage_variant(desktop, "openrc") == variant
    assert stagechoice.derive_stage_variant(desktop, "systemd") == variant.replace("openrc", "systemd")


def test_every_desktop_and_init_gives_a_published_stage3():
    for desktop in DESKTOPS:
        for init in ("openrc", "systemd"):
            for multilib in (True, False):
                assert stagechoice.derive_stage_variant(desktop, init, multilib) in STAGE3_VARIANTS
    assert stagechoice.derive_stage_variant("kde", "openrc", multilib=False) == "amd64-nomultilib-openrc"


def test_the_derived_profile_of_the_desktop_stage_matches_the_handbook_profiles():
    assert stagechoice.derive_stage("kde", "openrc").profile == PROFILE_BASE + "/desktop/plasma"
    assert stagechoice.derive_stage("gnome", "systemd").profile == PROFILE_BASE + "/desktop/gnome/systemd"
    assert stagechoice.derive_stage("xfce", "openrc").profile == PROFILE_BASE + "/desktop"
    assert stagechoice.derive_stage("minimal", "openrc").profile == PROFILE_BASE
    for desktop in DESKTOPS:
        for init in ("openrc", "systemd"):
            choice = stagechoice.derive_stage(desktop, init)
            assert choice.profile in PUBLISHED_PROFILES
            assert profile_problem(choice.profile, init, choice.variant) == ""


def test_stage_auto_follows_the_desktop_but_never_replaces_an_explicit_family():
    assert stagechoice.effective_stage_variant("amd64-openrc", "kde", "openrc", True, True) == "amd64-desktop-openrc"
    assert stagechoice.effective_stage_variant("amd64-desktop-openrc", "minimal", "openrc", True, True) == "amd64-openrc"
    assert stagechoice.effective_stage_variant("amd64-hardened-openrc", "gnome", "openrc", True, True) == "amd64-hardened-openrc"
    assert stagechoice.effective_stage_variant("amd64-nomultilib-systemd", "xfce", "systemd", False, True) == "amd64-nomultilib-systemd"
    assert stagechoice.effective_stage_variant("amd64-openrc", "kde", "openrc", True, False) == "amd64-openrc"


def test_hardened_blocks_kde_with_a_plain_message():
    message = stagechoice.stage_problem("amd64-hardened-openrc", "kde")
    assert message == desktop_problem("amd64-hardened-openrc", "kde")
    assert "Standard or Desktop stage3" in message and "Minimal" in message
    assert stagechoice.stage_problem("amd64-desktop-openrc", "kde") == ""


def test_the_manifest_still_enforces_the_profile_rules():
    manifest = InstallManifest()
    manifest.system.profile = PROFILE_BASE + "/desktop/plasma/systemd"  # systemd profile on an OpenRC system
    assert any("init system" in error for error in manifest.validate())
    manifest.system.profile = PROFILE_BASE + "/hardened"  # hardened profile on a standard stage3
    assert any("hardened" in error for error in manifest.validate())


def test_the_bundled_profile_list_is_the_amd64_23_list_of_the_tree():
    paths = [path for path, _ in AMD64_PROFILES]
    assert paths and all(path.startswith(PROFILE_BASE) for path in paths)
    assert PROFILE_BASE + "/desktop/plasma" in paths and PROFILE_BASE + "/hardened/systemd" in paths
    assert all(stability in {"stable", "dev", "exp"} for _, stability in AMD64_PROFILES)
    # every profile the installer can install is in the tree
    assert PUBLISHED_PROFILES <= set(paths)


def test_the_profile_list_is_stable_only_and_filtered_to_init_and_multilib():
    openrc = [choice.path for choice in stagechoice.list_profiles("openrc")]
    assert PROFILE_BASE in openrc and PROFILE_BASE + "/desktop/plasma" in openrc
    assert all("/systemd" not in path and "no-multilib" not in path for path in openrc)
    systemd = [choice.path for choice in stagechoice.list_profiles("systemd")]
    assert PROFILE_BASE + "/desktop/gnome/systemd" in systemd and all(path.endswith("/systemd") for path in systemd)
    nomultilib = [choice.path for choice in stagechoice.list_profiles("openrc", multilib=False)]
    assert nomultilib and all("no-multilib" in path for path in nomultilib)
    assert PROFILE_BASE + "/no-multilib/hardened" in nomultilib


def test_the_list_never_contains_developer_prefix_x32_or_split_usr_profiles():
    for init in ("openrc", "systemd"):
        for multilib in (True, False):
            for experimental in (False, True):
                for choice in stagechoice.list_profiles(init, multilib, experimental):
                    assert not {"developer", "prefix", "x32", "split-usr"} & set(choice.path.split("/"))


def test_experimental_profiles_appear_only_on_request_and_are_marked_unsupported_when_the_installer_cannot_install_them():
    plain = {choice.path for choice in stagechoice.list_profiles("openrc")}
    more = {choice.path: choice for choice in stagechoice.list_profiles("openrc", show_experimental=True)}
    assert plain < set(more)
    selinux = more[PROFILE_BASE + "/hardened/selinux"]
    assert selinux.experimental and not selinux.supported
    llvm = more[PROFILE_BASE + "/llvm"]
    assert llvm.experimental and not llvm.supported
    assert all(not choice.experimental and choice.supported for choice in stagechoice.list_profiles("openrc"))


def test_hardened_filter():
    assert all("hardened" in c.path for c in stagechoice.list_profiles("openrc", hardened=True))
    assert all("hardened" not in c.path for c in stagechoice.list_profiles("openrc", hardened=False))


def test_the_stages_own_eselect_list_is_used_when_it_is_given():
    text = ("Available profile symlink targets:\n  [1]   default/linux/amd64/23.0 (stable)\n  [2]   default/linux/amd64/23.0/desktop (stable)\n"
            "  [3]   default/linux/amd64/23.0/desktop/plasma (stable) *\n  [4]   default/linux/amd64/23.0/hardened (dev)\n"
            "  [5]   default/linux/amd64/23.0/developer (dev)\n  [6]   default/linux/amd64/23.0/systemd (stable)\n")
    assert stagechoice.parse_eselect_profile_list(text)[2] == (PROFILE_BASE + "/desktop/plasma", "stable")
    assert [c.path for c in stagechoice.list_profiles("openrc", eselect_list=text)] == [PROFILE_BASE, PROFILE_BASE + "/desktop", PROFILE_BASE + "/desktop/plasma"]
    assert [c.path for c in stagechoice.list_profiles("openrc", eselect_list=text, show_experimental=True)][-1] == PROFILE_BASE + "/hardened"


def test_profile_names_are_plain_english():
    assert stagechoice.describe_profile(PROFILE_BASE + "/desktop/plasma") == "Desktop for KDE Plasma (OpenRC)"
    assert stagechoice.describe_profile(PROFILE_BASE + "/systemd") == "Standard (systemd)"
    assert derive_profile("openrc", "kde") == PROFILE_BASE + "/desktop/plasma"


# --- PORT-09: USE flags -------------------------------------------------------------------------------------------------

USE_DESC_FIXTURE = """# Copyright
# Keep them sorted

X - Add support for X11
dvd - Add support for DVDs
gtk - Add support for x11-libs/gtk+ (The GIMP Toolkit)
kde - Add support for software made by KDE, a free software community
"""

USE_LOCAL_FIXTURE = """# comment
app-editors/vim:python - Build support for the Python scripting
media-video/mpv:dvd - Enable DVD support
media-video/vlc:dvd - Enable DVD support
"""


def test_use_desc_parser_on_a_fixture():
    parsed = usedesc.parse_use_desc(USE_DESC_FIXTURE)
    assert parsed == {"X": "Add support for X11", "dvd": "Add support for DVDs", "gtk": "Add support for x11-libs/gtk+ (The GIMP Toolkit)",
                      "kde": "Add support for software made by KDE, a free software community"}


def test_use_local_desc_parser_on_a_fixture():
    parsed = usedesc.parse_use_local_desc(USE_LOCAL_FIXTURE)
    assert parsed["dvd"] == [("media-video/mpv", "Enable DVD support"), ("media-video/vlc", "Enable DVD support")]
    assert parsed["python"] == [("app-editors/vim", "Build support for the Python scripting")]


def test_the_local_flags_of_a_missing_tree_are_empty(tmp_path):
    assert usedesc.local_flags_from_tree(tmp_path / "nothing") == {}
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles/use.local.desc").write_text(USE_LOCAL_FIXTURE)
    assert "dvd" in usedesc.local_flags_from_tree(tmp_path)


def test_the_bundled_index_has_the_common_flags():
    assert len(GLOBAL_USE) > 300
    for flag in ("kde", "gtk", "dvd", "wayland", "pulseaudio", "X"):
        assert GLOBAL_USE[flag]


def test_search_finds_by_name_and_description_and_ranks_name_matches_first():
    found = usedesc.search("dvd", {"dvdnav": "DVD navigation", "dvd": "Add support for DVDs", "bluray": "Blu-ray, not a DVD"})
    assert [name for name, _ in found] == ["dvd", "dvdnav", "bluray"]
    assert usedesc.search("support kde", dict(parse := usedesc.parse_use_desc(USE_DESC_FIXTURE)))[0][0] == "kde"
    assert usedesc.search("nothing like this", dict(parse)) == []


def test_star_is_refused_with_the_handbooks_reason():
    for token in ("-*", "*", "-*foo"):
        assert "switch off every default feature" in usedesc.use_problem(token)
    manifest = InstallManifest()
    manifest.portage.use_flags = ["-*", "kde"]
    assert any("switch off every default feature" in error for error in manifest.validate())
    assert usedesc.use_problem("-gtk") == "" and usedesc.use_problem("kde") == ""
    assert "not a USE flag" in usedesc.use_problem("kde;rm")


def test_tri_state_maps_to_use_tokens():
    assert usedesc.tokens_from_states({"kde": "on", "gtk": "off", "dvd": "default", "alsa": "on"}) == ["alsa", "-gtk", "kde"]
    assert usedesc.states_from_tokens(["kde", "-gtk", "gtk", "-qt5"]) == {"kde": "on", "gtk": "on", "qt5": "off"}
    with pytest.raises(ValueError):
        usedesc.tokens_from_states({"kde": "maybe"})


def test_the_browser_keeps_entries_it_did_not_touch():
    assert usedesc.merge_tokens(["-gtk", "kde", "foo"], {"kde": "off", "dvd": "on"}) == ["-gtk", "foo", "dvd", "-kde"]
    assert usedesc.merge_tokens(["-gtk", "kde"], {"gtk": "default"}) == ["kde"]


# --- PORT-13: VIDEO_CARDS -----------------------------------------------------------------------------------------------


def test_video_cards_values_come_from_the_tree():
    for value in ("amdgpu", "radeonsi", "intel", "nouveau", "nvidia", "nvk", "virgl", "vmware"):
        assert value in VIDEO_CARDS
    assert "exynos" not in VIDEO_CARDS  # an ARM board driver


def test_unknown_video_card_tokens_are_rejected_known_ones_and_auto_pass():
    assert usedesc.video_cards_problem("") == "" and usedesc.video_cards_problem("auto") == ""
    assert usedesc.video_cards_problem("amdgpu radeonsi") == ""
    assert "nvidiaa" in usedesc.video_cards_problem("amdgpu nvidiaa")
    manifest = InstallManifest()
    manifest.portage.video_cards = "madeup"
    assert any("madeup" in error for error in manifest.validate())


# --- PORT-06: mirrors --------------------------------------------------------------------------------------------------


def test_the_bundled_mirror_table_is_https_only_and_well_formed():
    assert mirrors.MIRRORS
    seen = set()
    for mirror in mirrors.MIRRORS:
        assert mirror.country in mirrors.COUNTRIES and mirror.name
        assert mirror.url.startswith("https://") and not mirror.url.endswith("/")
        assert mirrors.url_problem(mirror.url) == ""
        assert mirror.url not in seen
        seen.add(mirror.url)
    assert mirrors.countries()[0][1] <= mirrors.countries()[-1][1]
    assert {m.url for m in mirrors.mirrors_for_country("JP")} >= {"https://ftp.jaist.ac.jp/pub/Linux/Gentoo"}


@pytest.mark.parametrize("url", ["http://mirror.example/gentoo", "ftp://mirror.example/gentoo", "mirror.example", "https://", "https://a b.example",
                                 "https://mirror.example/gentoo?x=1", "https://mirror.example/gen\ntoo"])
def test_a_custom_mirror_must_be_a_clean_https_address(url):
    assert mirrors.url_problem(url)


def test_custom_http_is_named_in_the_message():
    assert "http://" in mirrors.url_problem("http://mirror.example/gentoo")
    assert mirrors.url_problem("https://mirror.example:8443/gentoo/") == ""


def test_mirror_order_and_duplicates():
    assert mirrors.mirror_list("https://p.example", ["https://a.example", "https://b.example/"], ["https://b.example", "https://c.example"]) == [
        "https://a.example", "https://b.example", "https://c.example"]
    assert mirrors.mirror_list("https://p.example") == ["https://p.example"]


# --- PORT-10: MAKEOPTS --------------------------------------------------------------------------------------------------

GIB = 1024**3


@pytest.mark.parametrize(("cpus", "memory_gib", "swap_mib", "expected"), [
    (32, 16, 0, "-j8 -l32"), (4, 4, 0, "-j2 -l4"), (2, 2, 0, "-j1 -l2"), (64, 128, 0, "-j32 -l64"), (8, 3, 4096, "-j2 -l8"), (1, 0.5, 0, "-j1 -l1"),
])
def test_auto_make_opts(cpus, memory_gib, swap_mib, expected):
    assert makeopts.auto_make_opts(cpus, int(memory_gib * GIB), swap_mib) == expected


def test_parse_and_format_keep_both_parts():
    assert makeopts.parse_make_opts("-j6 -l8") == (6, 8.0)
    assert makeopts.parse_make_opts("-j 6 --load-average=7.5") == (6, 7.5)
    assert makeopts.parse_make_opts("--jobs=4") == (4, None)
    assert makeopts.parse_make_opts("") == (None, None) and makeopts.parse_make_opts("-j") == (None, None)
    assert makeopts.format_make_opts(6, 8.0) == "-j6 -l8" and makeopts.format_make_opts(3, None) == "-j3" and makeopts.format_make_opts(3, 7.5) == "-j3 -l7.5"


# --- PORT-05 and PORT-03 helpers -----------------------------------------------------------------------------------------


def test_licence_choices():
    manifest = InstallManifest()
    assert licenses.choice_of(manifest.portage) == "free"
    manifest.portage.accept_license = licenses.RECOMMENDED
    assert licenses.choice_of(manifest.portage) == "redistributable"
    manifest.portage.accept_license = licenses.FREE_ONLY
    assert licenses.choice_of(manifest.portage) == "free"
    manifest.portage.accept_license = "-* @FREE @EULA"
    assert licenses.choice_of(manifest.portage) == "custom"
    assert licenses.groups_of("-* @FREE @EULA") == ["@FREE", "@EULA"]
    assert [group for group, *_ in licenses.GROUPS] == ["@FREE", "@BINARY-REDISTRIBUTABLE", "@GPL-COMPATIBLE", "@EULA"]


def test_flag_warnings_name_the_handbook_cautions():
    assert optimize.flag_warnings("-O2 -pipe") == []
    assert "-O3" in optimize.flag_warnings("-O3 -pipe")[0]
    assert len(optimize.flag_warnings("-O3 -fomit-frame-pointer")) == 2
    assert optimize.flag_warnings("-O2 -pipe -march=native") == []


def test_the_generated_data_modules_are_plain_and_complete():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", __import__("gentoo_installer.usedesc_data", fromlist=["TREE_DATE"]).TREE_DATE)


def test_probe_reports_none_for_a_mirror_that_does_not_answer():
    assert mirrors.probe_seconds("https://127.0.0.1:9/gentoo", timeout=0.5) is None
    assert mirrors.probe_all([], timeout=0.5) == {}
