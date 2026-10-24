"""PORT: the desktop environment cards of the System page, with the rule that the chosen stage3 may not support a desktop."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from .. import desktops
from ..manifest import System
from ..widgets import ChoiceGroup, wrapped_label

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


class DesktopSection(QWidget):
    """The desktop cards (KDE Plasma, GNOME, Xfce, Minimal) and the note about the stage3 family.

    ``selectionChanged(key)`` is emitted when the user, or a load, picks a card. The page that hosts the section
    decides what a stage3 family allows (``apply_family_constraints``) and reads the choice with ``collect``.
    """

    selectionChanged = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.desktop_group = ChoiceGroup([
            ("kde", "KDE Plasma", "A polished, flexible desktop with SDDM and a familiar application suite.", True),
            ("gnome", "GNOME", "A focused, modern desktop with GDM and GNOME applications.", False),
            ("xfce", "Xfce", "A lightweight desktop that works well on older or resource-limited hardware.", False),
            ("minimal", "Minimal system", "No graphical desktop. Install only the base system and selected tools.", False),
        ])
        layout.addWidget(QLabel("Desktop environment"))
        layout.addWidget(self.desktop_group)
        self.family_note = wrapped_label("", muted=True)
        self.family_note.hide()
        layout.addWidget(self.family_note)
        self.desktop_group.selectionChanged.connect(self.selectionChanged)

    def value(self) -> str:
        return self.desktop_group.value()

    def load(self, system: System) -> None:
        self.desktop_group.set_value(system.desktop)

    def collect(self, system: System) -> None:
        system.desktop = self.desktop_group.value() or "minimal"

    def set_advanced(self, advanced: bool) -> None:
        """The cards have nothing to hide: Guided and Advanced show the same choices."""

    def apply_family_constraints(self, family: str, user_change: bool, loading: bool) -> None:
        """Disable the desktops the chosen stage3 cannot build; when the user's
        own change made the current desktop impossible, fall back to Minimal.
        A manifest that is merely loaded is never changed here (the page then
        refuses it instead)."""
        supported = FAMILY_DESKTOP_SUPPORT[family]
        for key in ("kde", "gnome", "xfce"):
            reason = "" if key in supported else desktop_problem(f"amd64-{family}", key)
            self.desktop_group.set_card_enabled(key, key in supported, reason)
        chosen = self.desktop_group.value()
        if chosen and chosen != "minimal" and chosen not in supported and user_change and not loading:
            name = {"kde": "KDE Plasma", "gnome": "GNOME", "xfce": "Xfce"}[chosen]
            self.desktop_group.set_value("minimal")
            self.family_note.setText(f"{name} needs a Standard or Desktop stage3, so the selection was changed to the Minimal system. Choose a Standard stage3 to use {name}.")
            self.family_note.show()
        elif family in UNVERIFIED_FAMILIES and chosen in {"gnome", "xfce"}:
            self.family_note.setText("Not verified: GNOME and Xfce on this stage3 may need extra USE changes during the installation.")
            self.family_note.show()
        else:
            self.family_note.hide()
