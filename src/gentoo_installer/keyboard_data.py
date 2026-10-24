"""XKB layout to console keymap, and locale to L10N code (pure data).

The text console and the initramfs passphrase prompt use kbd keymaps, the
desktops use XKB layouts. This table maps the second to the first, with
names verified to exist under /usr/share/keymaps of kbd (tests re-check them).
It does not decide anything: writing keymaps, X11 or desktop keyboard files
belongs to the configure step.
"""

from __future__ import annotations

# (XKB layout, XKB variant) -> kbd keymap. An empty variant is the layout's default.
CONSOLE_KEYMAPS: dict[tuple[str, str], str] = {
    ("us", ""): "us",
    ("us", "dvorak"): "dvorak",
    ("gb", ""): "uk",
    ("ie", ""): "uk",
    ("de", ""): "de-latin1",
    ("de", "nodeadkeys"): "de-latin1-nodeadkeys",
    ("at", ""): "de-latin1",
    ("ch", "de"): "de_CH-latin1",
    ("ch", "fr"): "fr_CH-latin1",
    ("ch", ""): "de_CH-latin1",
    ("fr", ""): "fr",
    ("fr", "latin9"): "fr-latin9",
    ("be", ""): "be-latin1",
    ("es", ""): "es",
    ("it", ""): "it",
    ("pt", ""): "pt-latin9",
    ("se", ""): "sv-latin1",
    ("dk", ""): "dk-latin1",
    ("is", ""): "is-latin1",
    ("cz", ""): "cz-lat2",
    ("hu", ""): "hu",
    ("ro", ""): "ro",
    ("hr", ""): "croat",
    ("si", ""): "slovene",
    ("bg", ""): "bg_bds-utf8",
    ("ee", ""): "et",
    ("lt", ""): "lt",
    ("lv", ""): "lv",
    ("tr", ""): "trq",
    ("jp", ""): "jp106",
    ("ca", ""): "ca",
    ("ca", "fr"): "cf",
    ("latam", ""): "la-latin1",
    ("br", ""): "br-abnt2",
    ("nl", ""): "nl2",
    ("no", ""): "no-latin1",
    ("fi", ""): "fi",
    ("pl", ""): "pl2",
    ("sk", ""): "sk-qwertz",
}
# Layouts whose letters are not Latin: the text console has no suitable font by default,
# so the passphrase prompt stays on the US map (type the passphrase with Latin letters).
NON_LATIN_LAYOUTS = frozenset({"ru", "ua", "by", "kz", "gr", "il", "ara", "ir", "th", "in", "cn", "tw", "kr", "vn", "ge", "am", "mk", "rs", "mn", "np", "bd", "lk", "mm", "kh", "la"})
FALLBACK_KEYMAP = "us"


def console_keymap(layout: str = "us", variant: str = "") -> str:
    """kbd keymap for the first XKB layout (and its variant) of comma-separated lists; 'us' when unknown."""
    first = layout.split(",")[0].strip().lower() or "us"
    first_variant = variant.split(",")[0].strip().lower() if variant else ""
    if first in NON_LATIN_LAYOUTS:
        return FALLBACK_KEYMAP
    return CONSOLE_KEYMAPS.get((first, first_variant)) or CONSOLE_KEYMAPS.get((first, "")) or FALLBACK_KEYMAP


def is_non_latin(layout: str) -> bool:
    return layout.split(",")[0].strip().lower() in NON_LATIN_LAYOUTS


# Valid L10N values of the pinned Portage tree (profiles/desc/l10n.desc).
L10N_CODES = frozenset("""
ach af am an ar as ast az be bea bg bn bn-IN bo br brx bs ca ca-valencia cak ceb ch chr ckb cop co cs csb cu cy da de de-1901 de-AT de-CH de-DE dgo doi dsb
dv dz el en en-AU en-CA en-GB en-US en-ZA eo es es-419 es-AR es-CL es-ES es-MX es-PE et eu fa ff fi fil fo fr fr-CA fur fy ga gd gez gl gn got grc gu gug gv
he hi hil hr hsb ht hu hus hy ia id io is it iu ja jbo jv ka kab kek kk km kmr kmr-Latn kn ko kok kpg ks ktu ku ky la lb lij ln lo lt lv mai mg mi mk ml mn
mni mr ms mt my nan nb nd nds ne nl nn no nr nso ny oc om or pa pl pon pot ppk prs ps pt pt-BR pt-PT qu rm rmq ro ru rw sa sat sc sco sd si sid sk skr sl sml
sn so son sq sr sr-Latn ss st su sv sw sw-TZ syc syr szl ta te tet tg th ti tk tl tlh tn to tr trs ts tsg tt ug uk ur uz ve vec vi vls wa xh yi yo zh zh-CN
zh-HK zh-TW zu
""".split())


def l10n_code(locale: str) -> str:
    """L10N value for a locale such as 'pt_BR.UTF-8' -> 'pt-BR', 'de_DE.UTF-8' -> 'de-DE', 'nl_NL' -> 'nl'; '' when Portage has none."""
    base = locale.split(".")[0].split("@")[0]
    language, _, region = base.partition("_")
    language = language.lower()
    if region and f"{language}-{region.upper()}" in L10N_CODES:
        return f"{language}-{region.upper()}"
    return language if language in L10N_CODES else ""
