"""B19: input methods chosen from the locale, the screen reader, symbol fonts."""

from __future__ import annotations

import pytest

from gentoo_installerd import hwplan

from tests.hardware.builders import manifest, profile


def plan_for(**sections):
    return hwplan.plan(manifest(**sections), profile())


def env_of(result):
    return {path: content for path, content, _mode in result.files}.get("/etc/env.d/90installer-input-method", "")


@pytest.mark.parametrize(("locale", "desktop", "method", "engine"), [
    ("ja_JP.UTF-8", "kde", "fcitx5", "app-i18n/fcitx-anthy:5"),
    ("zh_CN.UTF-8", "xfce", "fcitx5", "app-i18n/fcitx-chinese-addons:5"),
    ("ko_KR.UTF-8", "kde", "fcitx5", "app-i18n/fcitx-hangul:5"),
    ("ja_JP.UTF-8", "gnome", "ibus", "app-i18n/ibus-anthy"),
    ("zh_CN.UTF-8", "gnome-light", "ibus", "app-i18n/ibus-libpinyin"),
])
def test_a_cjk_locale_can_type_after_login(locale, desktop, method, engine):
    result = plan_for(system={"locale": locale, "desktop": desktop})
    assert result.scalars["input_method"] == method and engine in result.optional_atoms
    assert ("app-i18n/fcitx:5" if method == "fcitx5" else "app-i18n/ibus") in result.optional_atoms
    assert "XMODIFIERS=@im=" in env_of(result)
    assert any("input method" in note for note in result.notes) and any(locale in line for line in result.first_steps)


def test_the_environment_follows_the_session_type():
    kde = env_of(plan_for(system={"locale": "ja_JP.UTF-8"}))
    assert "QT_IM_MODULE=fcitx" in kde and "GTK_IM_MODULE" not in kde               # Plasma on Wayland talks to fcitx5 directly
    assert "GTK_IM_MODULE=fcitx" in env_of(plan_for(system={"locale": "ja_JP.UTF-8", "desktop": "xfce"}))
    gnome = env_of(plan_for(system={"locale": "ja_JP.UTF-8", "desktop": "gnome"}))
    assert "XMODIFIERS=@im=ibus" in gnome and "GTK_IM_MODULE" not in gnome         # GNOME has ibus built in


@pytest.mark.parametrize("sections", [
    {"system": {"locale": "en_US.UTF-8"}},
    {"system": {"locale": "de_DE.UTF-8"}},
    {"system": {"locale": "ar_EG.UTF-8"}},
    {"system": {"locale": "ja_JP.UTF-8", "desktop": "minimal", "profile": "default/linux/amd64/23.0"}},
    {"system": {"locale": "ja_JP.UTF-8"}, "options": {"input_method": "none"}},
])
def test_no_input_method_where_it_is_not_needed_or_not_wanted(sections):
    result = plan_for(**sections)
    assert result.scalars.get("input_method", "none") == "none" and env_of(result) == ""
    assert not [atom for atom in result.optional_atoms if atom.startswith(("app-i18n/fcitx", "app-i18n/ibus"))]


def test_an_explicit_choice_beats_the_locale():
    result = plan_for(options={"input_method": "ibus"})
    assert result.scalars["input_method"] == "ibus" and "app-i18n/ibus" in result.optional_atoms
    assert "app-i18n/ibus-anthy" not in result.optional_atoms                       # an English locale has no engine to add
    assert plan_for(system={"locale": "ja_JP.UTF-8"}, options={"input_method": "fcitx5", "browser": "none"}).scalars["input_method"] == "fcitx5"


def test_orca_and_the_speech_voice_are_installed_when_ticked_and_not_otherwise():
    on = plan_for(options={"accessibility": True})
    assert {"app-accessibility/orca", "app-accessibility/speech-dispatcher", "app-accessibility/espeak-ng", "app-accessibility/at-spi2-core"} <= set(on.optional_atoms)
    assert on.package_use["app-accessibility/speech-dispatcher"] == ["pipewire"] and any("Orca" in line for line in on.first_steps)
    assert "*/*" not in on.package_use and "USE" not in on.make_conf                  # never the global accessibility flag
    off = plan_for()
    assert "app-accessibility/orca" not in off.optional_atoms
    alsa = plan_for(options={"accessibility": True, "pipewire": False})
    assert alsa.package_use["app-accessibility/speech-dispatcher"] == ["alsa"]
    assert "app-accessibility/orca" not in plan_for(options={"accessibility": True}, system={"desktop": "minimal", "profile": "default/linux/amd64/23.0"}).optional_atoms


def test_sway_gets_the_icon_font_of_its_bar():
    assert "media-fonts/fontawesome" in plan_for(system={"desktop": "sway"}).optional_atoms
    assert "media-fonts/fontawesome" not in plan_for().optional_atoms
