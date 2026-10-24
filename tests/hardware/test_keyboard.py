from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from gentoo_installer import keyboard_data as kd


@pytest.mark.parametrize(("layout", "variant", "expected"), [
    ("us", "", "us"), ("us", "dvorak", "dvorak"), ("gb", "", "uk"), ("ie", "", "uk"), ("de", "", "de-latin1"), ("de", "nodeadkeys", "de-latin1-nodeadkeys"),
    ("at", "", "de-latin1"), ("ch", "de", "de_CH-latin1"), ("ch", "fr", "fr_CH-latin1"), ("fr", "", "fr"), ("fr", "latin9", "fr-latin9"),
    ("pt", "", "pt-latin9"), ("se", "", "sv-latin1"), ("tr", "", "trq"), ("ca", "fr", "cf"), ("latam", "", "la-latin1"), ("br", "", "br-abnt2"),
    ("us,ru", "", "us"), ("de,us", "nodeadkeys,", "de-latin1-nodeadkeys"), ("DE", "", "de-latin1"),
    ("ru", "", "us"), ("ua", "", "us"), ("il", "", "us"),            # non-Latin: the passphrase prompt stays Latin
    ("us", "colemak", "us"), ("de", "unknown", "de-latin1"),           # unknown variant falls back to the layout
    ("xx", "", "us"), ("", "", "us"),
])
def test_console_keymap(layout, variant, expected):
    assert kd.console_keymap(layout, variant) == expected


def test_non_latin_detection():
    assert kd.is_non_latin("ru,us") and kd.is_non_latin("ara") and not kd.is_non_latin("de") and not kd.is_non_latin("")


def test_keymap_names_exist_in_kbd():
    """Names must exist under /usr/share/keymaps (skipped where kbd is not installed)."""
    root = Path("/usr/share/keymaps")
    if not root.is_dir():
        pytest.skip("kbd keymaps are not installed")
    available = {re.sub(r"\.map(\.gz)?$", "", path.name) for path in root.rglob("*.map*")}
    missing = sorted({name for name in kd.CONSOLE_KEYMAPS.values() if name not in available})
    assert not missing, f"console keymaps that do not exist: {missing}"


@pytest.mark.parametrize(("locale", "code"), [
    ("de_DE.UTF-8", "de-DE"), ("pt_BR.UTF-8", "pt-BR"), ("pt_PT.UTF-8", "pt-PT"), ("zh_CN.UTF-8", "zh-CN"), ("zh_TW", "zh-TW"), ("en_US.UTF-8", "en-US"),
    ("en_GB.UTF-8", "en-GB"), ("nl_NL.UTF-8", "nl"), ("fr_FR.UTF-8", "fr"), ("fr_CA.UTF-8", "fr-CA"), ("es_MX.UTF-8", "es-MX"), ("sr_RS@latin", "sr"),
    ("ja_JP.eucJP", "ja"), ("C", ""), ("POSIX", ""), ("", ""), ("xx_XX.UTF-8", ""),
])
def test_l10n_code(locale, code):
    assert kd.l10n_code(locale) == code


def test_l10n_codes_match_the_pinned_tree_when_available():
    tree = os.environ.get("GENTOO_PINNED_TREE")
    desc = Path(tree) / "profiles/desc/l10n.desc" if tree else None
    if desc is None or not desc.is_file():
        pytest.skip("set GENTOO_PINNED_TREE")
    names = {line.split()[0] for line in desc.read_text().splitlines() if line.strip() and not line.startswith("#")}
    assert names == set(kd.L10N_CODES)
