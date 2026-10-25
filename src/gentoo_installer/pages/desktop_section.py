"""PORT: the desktop environment cards of the System page, with the rule that the chosen stage3 may not support a desktop."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .. import desktops
from ..manifest import System
from ..widgets import ChoiceGroup, wrapped_label
from .help import AdvancedSection, HelpNote

# Stage3 families and what the installer can build on each come from the desktop registry (desktops.py).
FAMILY_PREFIX = desktops.FAMILY_PREFIX
FAMILY_LABELS = desktops.FAMILY_LABELS
FAMILY_DESKTOP_SUPPORT = desktops.family_desktop_support()
UNVERIFIED_FAMILIES = set(desktops.UNVERIFIED_FAMILIES)
KDE_NEEDS_DESKTOP_STAGE = desktops.KDE_NEEDS_DESKTOP_STAGE


def stage_family(stage_variant: str) -> str:
    if "hardened" in stage_variant:
        return "hardened"
    if "nomultilib" in stage_variant:
        return "nomultilib"
    if "desktop" in stage_variant:
        return "desktop"
    return "standard"


def desktop_problem(stage_variant: str, desktop: str) -> str:
    """Why this desktop cannot be installed on this stage3 ('' when it can)."""
    return desktops.family_problem(desktop, stage_family(stage_variant))


class DesktopCards(QObject):
    """The Guided cards and the "More desktops" cards as one radio group.

    It offers the part of ChoiceGroup that the System page and the tests use (``cards``, ``value``, ``set_value``,
    ``set_card_enabled``, ``selectionChanged``) over two groups, of which only one card is selected at any time.
    """

    selectionChanged = Signal(str)

    def __init__(self, guided: ChoiceGroup, more: ChoiceGroup) -> None:
        super().__init__()
        self.guided, self.more = guided, more
        guided.selectionChanged.connect(lambda key: self._chosen(key, guided, more))
        more.selectionChanged.connect(lambda key: self._chosen(key, more, guided))

    @property
    def cards(self) -> dict:
        return {**self.guided.cards, **self.more.cards}

    def _chosen(self, key: str, group: ChoiceGroup, other: ChoiceGroup) -> None:
        other.clear_value()
        self.selectionChanged.emit(key)

    def value(self) -> str:
        return self.guided.value() or self.more.value()

    def set_value(self, key: str) -> None:
        if key in self.guided.cards:
            self.guided.set_value(key)
        elif key in self.more.cards:
            self.more.set_value(key)

    def set_card_enabled(self, key: str, enabled: bool, reason: str = "") -> None:
        for group in (self.guided, self.more):
            group.set_card_enabled(key, enabled, reason)


class DesktopSection(QWidget):
    """The desktop cards (KDE Plasma, GNOME, Xfce, Minimal; the others under "More desktops") and the note about the stage3 family.

    The cards come from the desktop registry (``desktops.py``). ``selectionChanged(key)`` is emitted when the user, or a
    load, picks a card. The page that hosts the section decides what a stage3 family allows (``apply_family_constraints``)
    and reads the choice with ``collect``.
    """

    selectionChanged = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        guided = ChoiceGroup(desktops.card_choices(desktops.GUIDED_DESKTOPS))
        more = ChoiceGroup(desktops.card_choices(desktops.MORE_DESKTOPS))
        self.desktop_group = DesktopCards(guided, more)
        heading = QHBoxLayout()
        heading.addWidget(QLabel("Desktop environment"))
        heading.addWidget(HelpNote(
            "The desktop is the graphical environment with windows, menus and a panel. Every desktop includes Firefox (you can turn it off "
            "under Software). Minimal installs no desktop at all."))
        heading.addStretch()
        layout.addLayout(heading)
        layout.addWidget(guided)
        self.more_section = AdvancedSection("system", "More desktops")
        self.more_section.add_widget(more)
        layout.addWidget(self.more_section)
        self.family_note = wrapped_label("", muted=True)
        self.family_note.hide()
        layout.addWidget(self.family_note)
        self.desktop_group.selectionChanged.connect(self._selected)

    def _selected(self, key: str) -> None:
        if key in desktops.MORE_DESKTOPS:
            self.more_section.set_open(True)  # a chosen card is never hidden
        self.selectionChanged.emit(key)

    def value(self) -> str:
        return self.desktop_group.value()

    def load(self, system: System) -> None:
        self.desktop_group.set_value(system.desktop)

    def collect(self, system: System) -> None:
        system.desktop = self.desktop_group.value() or "minimal"

    def set_advanced(self, advanced: bool) -> None:
        """Advanced opens "More desktops"; Guided leaves it as the user left it, and a selected card keeps it open."""
        self.more_section.set_advanced(advanced)
        if self.desktop_group.value() in desktops.MORE_DESKTOPS:
            self.more_section.set_open(True)

    def apply_family_constraints(self, family: str, user_change: bool, loading: bool) -> None:
        """Disable the desktops the chosen stage3 cannot build; when the user's
        own change made the current desktop impossible, fall back to Minimal.
        A manifest that is merely loaded is never changed here (the page then
        refuses it instead)."""
        for key in desktops.DESKTOPS:
            if key == "minimal":
                continue
            supported = desktops.family_supports(key, family)
            self.desktop_group.set_card_enabled(key, supported, "" if supported else desktops.family_problem(key, family))
        chosen = self.desktop_group.value()
        if chosen and chosen != "minimal" and not desktops.family_supports(chosen, family) and user_change and not loading:
            problem = desktops.family_problem(chosen, family)
            self.desktop_group.set_value("minimal")
            self.family_note.setText(f"{problem} The selection was changed to the Minimal system.")
            self.family_note.show()
        elif desktops.unverified(chosen, family):
            self.family_note.setText(f"Not verified: {desktops.label(chosen)} on this stage3 may need extra USE changes during the installation.")
            self.family_note.show()
        else:
            self.family_note.hide()
