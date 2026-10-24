"""Keyboard presets shared by the installer window and the installer service.

A preset ties together the three names a keyboard has on Linux:

* the console keymap (a ``kbd`` map such as ``de-latin1``, used by ``loadkeys``,
  OpenRC's ``keymaps`` service, ``vconsole.conf`` and the LUKS prompt),
* the XKB layout and variant (used by X11 and Wayland sessions and by display managers).

Only console keymaps that exist in sys-apps/kbd are listed; ``tests/test_keymaps.py``
verifies this against ``/usr/share/keymaps`` where that directory exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class KeyboardPreset:
    console: str  # name of a kbd keymap
    label: str
    layout: str  # XKB layout
    variant: str = ""  # XKB variant


# Text consoles have no font for non-Latin scripts, and the LUKS prompt must stay typable,
# so those layouts keep the US console map and only change the graphical session.
_NON_LATIN_NOTE = " (console stays US)"

PRESETS: tuple[KeyboardPreset, ...] = (
    KeyboardPreset("us", "English (US)", "us"),
    KeyboardPreset("dvorak", "English (US, Dvorak)", "us", "dvorak"),
    KeyboardPreset("uk", "English (UK)", "gb"),
    KeyboardPreset("de-latin1", "German", "de"),
    KeyboardPreset("de-latin1-nodeadkeys", "German (no dead keys)", "de", "nodeadkeys"),
    KeyboardPreset("de_CH-latin1", "German (Switzerland)", "ch"),
    KeyboardPreset("fr_CH-latin1", "French (Switzerland)", "ch", "fr"),
    KeyboardPreset("fr", "French", "fr"),
    KeyboardPreset("fr-latin9", "French (Latin-9)", "fr", "latin9"),
    KeyboardPreset("be-latin1", "Belgian", "be"),
    KeyboardPreset("es", "Spanish", "es"),
    KeyboardPreset("it", "Italian", "it"),
    KeyboardPreset("pt-latin9", "Portuguese", "pt"),
    KeyboardPreset("br-abnt2", "Portuguese (Brazil)", "br"),
    KeyboardPreset("la-latin1", "Spanish (Latin America)", "latam"),
    KeyboardPreset("nl2", "Dutch", "nl"),
    KeyboardPreset("sv-latin1", "Swedish", "se"),
    KeyboardPreset("dk-latin1", "Danish", "dk"),
    KeyboardPreset("no-latin1", "Norwegian", "no"),
    KeyboardPreset("fi", "Finnish", "fi"),
    KeyboardPreset("is-latin1", "Icelandic", "is"),
    KeyboardPreset("pl2", "Polish", "pl"),
    KeyboardPreset("cz-lat2", "Czech", "cz"),
    KeyboardPreset("sk-qwertz", "Slovak", "sk"),
    KeyboardPreset("hu", "Hungarian", "hu"),
    KeyboardPreset("ro", "Romanian", "ro"),
    KeyboardPreset("croat", "Croatian", "hr"),
    KeyboardPreset("slovene", "Slovenian", "si"),
    KeyboardPreset("bg_bds-utf8", "Bulgarian", "bg"),
    KeyboardPreset("et", "Estonian", "ee"),
    KeyboardPreset("lt", "Lithuanian", "lt"),
    KeyboardPreset("lv", "Latvian", "lv"),
    KeyboardPreset("trq", "Turkish (Q)", "tr"),
    KeyboardPreset("jp106", "Japanese", "jp"),
    KeyboardPreset("ca", "Canadian (multilingual)", "ca", "multix"),
    KeyboardPreset("cf", "French (Canada)", "ca"),
    KeyboardPreset("us", "Russian" + _NON_LATIN_NOTE, "ru"),
    KeyboardPreset("us", "Ukrainian" + _NON_LATIN_NOTE, "ua"),
    KeyboardPreset("us", "Greek" + _NON_LATIN_NOTE, "gr"),
    KeyboardPreset("us", "Hebrew" + _NON_LATIN_NOTE, "il"),
    KeyboardPreset("us", "Arabic" + _NON_LATIN_NOTE, "ara"),
    KeyboardPreset("us", "Persian" + _NON_LATIN_NOTE, "ir"),
    KeyboardPreset("us", "Thai" + _NON_LATIN_NOTE, "th"),
    KeyboardPreset("us", "Korean" + _NON_LATIN_NOTE, "kr"),
    KeyboardPreset("us", "Chinese" + _NON_LATIN_NOTE, "cn"),
    KeyboardPreset("us", "Hindi" + _NON_LATIN_NOTE, "in"),
)

# Layouts that have no preset of their own but share the console map of a neighbour.
_LAYOUT_CONSOLE_ALIASES = {("ie", ""): "uk", ("at", ""): "de-latin1", ("at", "nodeadkeys"): "de-latin1-nodeadkeys"}

KEYMAP_ROOTS: tuple[Path, ...] = (Path("/usr/share/keymaps"), Path("/usr/share/kbd/keymaps"))


def console_choices() -> list[tuple[str, str]]:
    """(console keymap, label) pairs for a combo box; each console map appears once."""
    seen: set[str] = set()
    choices: list[tuple[str, str]] = []
    for preset in PRESETS:
        if preset.console not in seen:
            seen.add(preset.console)
            choices.append((preset.console, preset.label))
    return choices


def preset_for_layout(layout: str, variant: str = "") -> KeyboardPreset | None:
    """The preset for an XKB layout, preferring an exact variant match."""
    first = layout.split(",")[0].strip()
    first_variant = variant.split(",")[0].strip()
    exact = [p for p in PRESETS if p.layout == first and p.variant == first_variant]
    if exact:
        return exact[0]
    plain = [p for p in PRESETS if p.layout == first and not p.variant]
    return plain[0] if plain else None


def preset_for_console(console: str) -> KeyboardPreset | None:
    return next((p for p in PRESETS if p.console == console), None)


def console_keymap_for(layout: str, variant: str = "") -> str:
    """The console keymap that matches an XKB layout (``us`` when none is known)."""
    preset = preset_for_layout(layout, variant)
    if preset:
        return preset.console
    first, first_variant = layout.split(",")[0].strip(), variant.split(",")[0].strip()
    return _LAYOUT_CONSOLE_ALIASES.get((first, first_variant)) or _LAYOUT_CONSOLE_ALIASES.get((first, "")) or "us"


def xkb_for_console(console: str) -> tuple[str, str]:
    """The XKB (layout, variant) that matches a console keymap; ("us", "") when unknown."""
    preset = preset_for_console(console)
    return (preset.layout, preset.variant) if preset else ("us", "")


def list_console_keymaps(roots: Iterable[Path] = KEYMAP_ROOTS) -> set[str]:
    """Names of the kbd keymaps installed under ``roots`` (empty when kbd is absent)."""
    names: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.map*"):
            name = path.name
            for suffix in (".gz", ".bz2", ".xz", ".zst"):
                if name.endswith(suffix):
                    name = name[: -len(suffix)]
            if name.endswith(".map"):
                names.add(name[: -len(".map")])
    return names


def near_matches(name: str, available: Sequence[str], limit: int = 5) -> list[str]:
    import difflib

    return difflib.get_close_matches(name, list(available), n=limit, cutoff=0.6)
