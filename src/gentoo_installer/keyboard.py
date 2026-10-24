"""Keyboard layouts: the list, the console keymap that matches, and a chooser.

The layout picked on the Welcome page is applied to the live X session at once
(so the passphrases typed later are typed with the layout the user expects) and
seeds System.keyboard_layout / keyboard_variant / keymap of the new system.
The repair window can embed the same KeyboardChooser to type a LUKS passphrase.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QProcess, Signal
from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit, QVBoxLayout, QWidget

EVDEV_XML = Path("/usr/share/X11/xkb/rules/evdev.xml")
KEYMAP_DIR = Path("/usr/share/keymaps")


@dataclass(frozen=True)
class Layout:
    name: str
    description: str
    variants: tuple[tuple[str, str], ...] = ()  # (name, description)


FALLBACK_LAYOUTS = (
    Layout("us", "English (US)", (("dvorak", "English (Dvorak)"), ("intl", "English (US, intl., with dead keys)"), ("colemak", "English (Colemak)"))),
    Layout("gb", "English (UK)"),
    Layout("de", "German", (("nodeadkeys", "German (no dead keys)"),)),
    Layout("fr", "French", (("latin9", "French (Latin-9 only)"),)),
    Layout("es", "Spanish"),
    Layout("it", "Italian"),
    Layout("pt", "Portuguese"),
    Layout("br", "Portuguese (Brazil)"),
    Layout("nl", "Dutch"),
    Layout("pl", "Polish"),
    Layout("se", "Swedish"),
    Layout("no", "Norwegian"),
    Layout("dk", "Danish"),
    Layout("fi", "Finnish"),
    Layout("cz", "Czech"),
    Layout("tr", "Turkish"),
    Layout("jp", "Japanese"),
    Layout("ru", "Russian"),
    Layout("ua", "Ukrainian"),
)

# XKB layout (and variant) -> console keymap of sys-apps/kbd. The console prompt
# for a LUKS passphrase has no font for non-Latin scripts, so those fall back to
# "us". Every name here exists in /usr/share/keymaps (a test checks that).
CONSOLE_KEYMAPS: dict[tuple[str, str], str] = {
    ("us", ""): "us", ("us", "dvorak"): "dvorak",
    ("gb", ""): "uk", ("ie", ""): "uk",
    ("de", ""): "de-latin1", ("de", "nodeadkeys"): "de-latin1-nodeadkeys", ("at", ""): "de-latin1",
    ("ch", ""): "de_CH-latin1", ("ch", "de"): "de_CH-latin1", ("ch", "fr"): "fr_CH-latin1",
    ("fr", ""): "fr", ("fr", "latin9"): "fr-latin9", ("be", ""): "be-latin1",
    ("es", ""): "es", ("it", ""): "it", ("pt", ""): "pt-latin9",
    ("se", ""): "sv-latin1", ("dk", ""): "dk-latin1", ("is", ""): "is-latin1", ("no", ""): "no-latin1", ("fi", ""): "fi",
    ("cz", ""): "cz-lat2", ("hu", ""): "hu", ("ro", ""): "ro", ("hr", ""): "croat", ("si", ""): "slovene",
    ("bg", ""): "bg_bds-utf8", ("ee", ""): "et", ("lt", ""): "lt", ("lv", ""): "lv", ("tr", ""): "trq",
    ("jp", ""): "jp106", ("ca", ""): "ca", ("ca", "fr"): "cf", ("latam", ""): "la-latin1", ("br", ""): "br-abnt2",
    ("nl", ""): "nl2", ("pl", ""): "pl2", ("sk", ""): "sk-qwertz",
}
# Console keymaps offered in the Accounts page (each is in CONSOLE_KEYMAPS).
CONSOLE_KEYMAP_CHOICES = sorted(set(CONSOLE_KEYMAPS.values()))


def console_keymap(layout: str, variant: str = "") -> str:
    """Console keymap that best matches the first XKB layout (and variant)."""
    first = layout.split(",")[0].strip()
    first_variant = variant.split(",")[0].strip()
    return CONSOLE_KEYMAPS.get((first, first_variant)) or CONSOLE_KEYMAPS.get((first, "")) or "us"


def keymap_exists(name: str, directory: Path = KEYMAP_DIR) -> bool:
    """Whether the live system has this console keymap (True when it cannot be told)."""
    if not directory.is_dir():
        return True
    return any(directory.rglob(f"{name}.map*"))


def load_layouts(path: Path = EVDEV_XML) -> list[Layout]:
    """Layouts and variants from xkeyboard-config; a short built-in list if unreadable."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return list(FALLBACK_LAYOUTS)
    layouts: list[Layout] = []
    for element in root.findall("./layoutList/layout"):
        name = (element.findtext("./configItem/name") or "").strip()
        description = (element.findtext("./configItem/description") or name).strip()
        if not name:
            continue
        variants = tuple(
            ((variant.findtext("./configItem/name") or "").strip(), (variant.findtext("./configItem/description") or "").strip())
            for variant in element.findall("./variantList/variant")
            if (variant.findtext("./configItem/name") or "").strip()
        )
        layouts.append(Layout(name, description, variants))
    if not layouts:
        return list(FALLBACK_LAYOUTS)
    layouts.sort(key=lambda item: (item.name != "us", item.description.lower()))
    return layouts


def start_detached(program: str, arguments: list[str]) -> bool:
    return bool(QProcess.startDetached(program, arguments))


def apply_layout(layout: str, variant: str = "", options: str = "", model: str = "") -> bool:
    """Apply the layout to the running X session with setxkbmap (no root needed).

    Returns whether the helper could be started. Failures are not errors: a
    Wayland session or a machine without X simply keeps its layout."""
    arguments = ["-layout", layout, "-variant", variant, "-option", options]
    if model:
        arguments += ["-model", model]
    return start_detached("setxkbmap", arguments)


class KeyboardChooser(QWidget):
    """Search box, layout, variant and a field to try the keyboard in.

    layoutChanged(layout, variant, console_keymap) is emitted only when the
    user picks something; set_selection() just displays a value."""

    layoutChanged = Signal(str, str, str)

    def __init__(self, layouts: list[Layout] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.layouts = layouts if layouts is not None else load_layouts()
        self._by_name = {item.name: item for item in self.layouts}
        self._custom = ""
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search keyboard layouts…")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search keyboard layouts")
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(lambda: self._layout_activated(0))
        box.addWidget(self.search)
        self.layout_box = QComboBox()
        self.layout_box.setAccessibleName("Keyboard layout")
        self.layout_box.setMaxVisibleItems(18)
        self.layout_box.activated.connect(self._layout_activated)
        box.addWidget(self.layout_box)
        self.variant_box = QComboBox()
        self.variant_box.setAccessibleName("Keyboard layout variant")
        self.variant_box.activated.connect(self._variant_activated)
        box.addWidget(self.variant_box)
        self.test_field = QLineEdit()
        self.test_field.setPlaceholderText("Type here to test your keyboard")
        self.test_field.setAccessibleName("Keyboard test field")
        box.addWidget(self.test_field)
        self.summary = QLabel()
        self.summary.setProperty("muted", True)
        self.summary.setWordWrap(True)
        box.addWidget(self.summary)
        self._fill_layouts()
        self._fill_variants("")

    # --- display -------------------------------------------------------------------------

    def _fill_layouts(self, keep: str = "", text: str = "") -> None:
        needle = text.strip().lower()
        self.layout_box.blockSignals(True)
        self.layout_box.clear()
        if self._custom:
            self.layout_box.addItem(f"Custom: {self._custom}", self._custom)
        for item in self.layouts:
            if not needle or needle in item.description.lower() or needle in item.name.lower():
                self.layout_box.addItem(f"{item.description}  ({item.name})", item.name)
        index = self.layout_box.findData(keep) if keep else 0
        self.layout_box.setCurrentIndex(max(0, index))
        self.layout_box.blockSignals(False)

    def _fill_variants(self, layout: str, keep: str = "") -> None:
        self.variant_box.blockSignals(True)
        self.variant_box.clear()
        self.variant_box.addItem("Default variant", "")
        for name, description in self._by_name.get(layout, Layout(layout, layout)).variants:
            self.variant_box.addItem(description or name, name)
        self.variant_box.setCurrentIndex(max(0, self.variant_box.findData(keep)))
        self.variant_box.blockSignals(False)
        self._update_summary()

    def _update_summary(self) -> None:
        layout, variant = self.value()
        self.summary.setText(f"Console keymap for the installed system: {console_keymap(layout, variant)}")

    def _filter(self, text: str) -> None:
        layout, variant = self.value()
        self._fill_layouts(layout, text)
        self._fill_variants(self.value()[0], variant)

    def set_selection(self, layout: str, variant: str = "") -> None:
        """Show this selection without applying it (a multi-layout value like
        'us,ru' is kept as a custom entry)."""
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._custom = layout if layout not in self._by_name and layout else ""
        self._fill_layouts(layout)
        self._fill_variants(layout, variant)

    # --- user actions ------------------------------------------------------------------------

    def value(self) -> tuple[str, str]:
        return str(self.layout_box.currentData() or "us"), str(self.variant_box.currentData() or "")

    def _layout_activated(self, _index: int) -> None:
        self._fill_variants(self.value()[0])
        self._publish()

    def _variant_activated(self, _index: int) -> None:
        self._update_summary()
        self._publish()

    def _publish(self) -> None:
        layout, variant = self.value()
        apply_layout(layout, variant)
        self.layoutChanged.emit(layout, variant, console_keymap(layout, variant))
