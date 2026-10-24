"""CORE: shared widgets for explaining a choice and for hiding the options only an expert needs.

HelpNote      a small "i" control: one sentence and, online, a link to the Gentoo Handbook
Badge         a small label that says what kind of choice something is (Default, Alternative, Optional, Recommended)
AdvancedSection  a collapsible "More options" group; the window's Detail level opens or collapses it
HANDBOOK_LINKS   topic -> Handbook URL, used by HelpNote and by the pages that link to the Handbook
"""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLayout, QToolButton, QVBoxLayout, QWidget

from .. import theme

HANDBOOK = "https://wiki.gentoo.org/wiki/Handbook:AMD64/"

# One entry per Handbook chapter of the installation, plus the working chapters that explain Portage. The text after "#" is
# the section of that chapter; a link to a section that was renamed still opens the right chapter.
HANDBOOK_LINKS: dict[str, str] = {
    "Media": HANDBOOK + "Installation/Media",
    "Networking": HANDBOOK + "Installation/Networking",
    "Disks": HANDBOOK + "Installation/Disks",
    "Partitioning": HANDBOOK + "Installation/Disks#Designing_a_partitioning_scheme",
    "Filesystems": HANDBOOK + "Installation/Disks#Creating_file_systems",
    "Stage": HANDBOOK + "Installation/Stage",
    "Base": HANDBOOK + "Installation/Base",
    "Mirrors": HANDBOOK + "Installation/Base#Optional:_Selecting_mirrors",
    "Profile": HANDBOOK + "Installation/Base#Choosing_the_right_profile",
    "UseFlags": HANDBOOK + "Installation/Base#Configuring_the_USE_variable",
    "Kernel": HANDBOOK + "Installation/Kernel",
    "System": HANDBOOK + "Installation/System",
    "Fstab": HANDBOOK + "Installation/System#About_fstab",
    "Hostname": HANDBOOK + "Installation/System#Hostname",
    "Locale": HANDBOOK + "Installation/Base#Locale_selection",
    "Timezone": HANDBOOK + "Installation/Base#Timezone",
    "Tools": HANDBOOK + "Installation/Tools",
    "Bootloader": HANDBOOK + "Installation/Bootloader",
    "Finalizing": HANDBOOK + "Installation/Finalizing",
    "Users": HANDBOOK + "Installation/Finalizing#User_administration",
    "Portage": HANDBOOK + "Working/Portage",
    "Features": HANDBOOK + "Working/Features",
    "InitSystem": HANDBOOK + "Working/Initscripts",
}


def handbook_url(anchor: str) -> str:
    """The Handbook URL for a topic of HANDBOOK_LINKS, or for a path below Handbook:AMD64/ such as "Installation/Disks#Partitioning"."""
    if anchor in HANDBOOK_LINKS:
        return HANDBOOK_LINKS[anchor]
    return HANDBOOK + anchor.lstrip("/")


class _Popup(QFrame):
    """The window that opens next to the "i": the sentence, and a link when there is a Handbook page."""

    def __init__(self, text: str, url: str, parent: QWidget) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("card")
        self.setMaximumWidth(380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.label)
        self.link: QLabel | None = None
        if url:
            self.link = QLabel(f'<a href="{url}">Read more in the Gentoo Handbook</a>')
            self.link.setTextFormat(Qt.TextFormat.RichText)
            self.link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            self.link.setToolTip(url)
            self.link.linkActivated.connect(open_url)
            layout.addWidget(self.link)


def open_url(url: str) -> bool:
    """Open a link in the system browser; offline (or without a browser) nothing happens and the sentence is still on screen."""
    try:
        return bool(QDesktopServices.openUrl(QUrl(url)))
    except Exception:  # noqa: BLE001 - a missing browser must never break a page
        return False


class HelpNote(QToolButton):
    """A small "i" button. Clicking it shows ``text`` and, when ``handbook_anchor`` is given, a link to the Handbook.

    ``handbook_anchor`` is a key of HANDBOOK_LINKS ("Disks") or a path below Handbook:AMD64/. The button can be reached with
    the keyboard (Space or Enter) and announces itself as "More information".
    """

    def __init__(self, text: str, handbook_anchor: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.text = text
        self.url = handbook_url(handbook_anchor) if handbook_anchor else ""
        self.setText("i")
        self.setObjectName("helpNote")
        self.setAutoRaise(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("More information")
        self.setAccessibleDescription(text)
        self.setToolTip(text)
        self.setFixedSize(22, 22)
        self.setStyleSheet(
            f"QToolButton#helpNote {{ color: {theme.ACCENT_DARK if not theme.is_dark() else theme.TEXT}; border: 1px solid {theme.BORDER}; "
            f"border-radius: 11px; font-weight: 700; padding: 0; }}"
            f"QToolButton#helpNote:focus {{ border: 2px solid {theme.ACCENT}; }}")
        self.popup: _Popup | None = None
        self.clicked.connect(self.show_note)

    def show_note(self) -> None:
        """Open the note below the button (also what a click does)."""
        if self.popup is not None:
            self.popup.close()
        self.popup = _Popup(self.text, self.url, self)
        self.popup.adjustSize()
        self.popup.move(self.mapToGlobal(self.rect().bottomLeft()))
        self.popup.show()

    def open_handbook(self) -> bool:
        """Open the Handbook page of this note in the system browser (False when there is none or no browser)."""
        return open_url(self.url) if self.url else False


BADGE_KINDS = ("Default", "Alternative", "Optional", "Recommended")


class Badge(QLabel):
    """A small pill that says what kind of choice something is: Default, Alternative, Optional or Recommended."""

    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        if kind not in BADGE_KINDS:
            raise ValueError(f"unknown badge kind {kind!r}; use one of {', '.join(BADGE_KINDS)}")
        super().__init__(kind, parent)
        self.kind = kind
        self.setObjectName("badge")
        self.setAccessibleName(kind)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.apply_theme()

    def apply_theme(self) -> None:
        """Colour the badge from the theme tokens of the current colour scheme."""
        foreground, background = theme.badge_colors(self.kind)
        self.setStyleSheet(
            f"QLabel#badge {{ color: {foreground}; background: {background}; border-radius: 9px; padding: 2px 9px; font-size: 12px; font-weight: 600; }}")


class AdvancedSection(QWidget):
    """A collapsible group "More options". The window's Detail level drives it through ``set_advanced``:

    Advanced opens it; Guided collapses it to the state the user left it in (collapsed at first), and the header button still
    opens it by hand. Nothing in it is disabled or cleared, so values entered survive switching. The state the user chose in
    Guided is remembered per page in the application settings.
    """

    toggled = Signal(bool)

    def __init__(self, page: str, title: str = "More options", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.page = page
        self.title = title
        self._advanced = False
        self._guided_open = self._load()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        self.header = QToolButton()
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setAutoRaise(True)
        self.header.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.header.setAccessibleName(title)
        header_row = QHBoxLayout()
        header_row.addWidget(self.header)
        header_row.addStretch()
        outer.addLayout(header_row)
        self.body = QFrame()
        self.body.setObjectName("advancedBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.body)
        self.header.toggled.connect(self._header_toggled)
        self._show(self._guided_open)

    # -- content ------------------------------------------------------------------------------------------------------

    def add_widget(self, widget: QWidget) -> None:
        self.body_layout.addWidget(widget)

    def add_layout(self, layout: QLayout) -> None:
        self.body_layout.addLayout(layout)

    # -- state --------------------------------------------------------------------------------------------------------

    def is_open(self) -> bool:
        return not self.body.isHidden()

    def set_open(self, opened: bool) -> None:
        """Open or collapse by hand, as the header button does."""
        self.header.setChecked(opened)

    def set_advanced(self, advanced: bool) -> None:
        self._advanced = advanced
        self._show(True if advanced else self._guided_open)

    def _show(self, opened: bool) -> None:
        self.header.blockSignals(True)
        self.header.setChecked(opened)
        self.header.blockSignals(False)
        self.header.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)
        self.header.setAccessibleDescription("expanded" if opened else "collapsed")
        self.body.setVisible(opened)

    def _header_toggled(self, opened: bool) -> None:
        if not self._advanced:
            self._guided_open = opened
            self._save(opened)
        self._show(opened)
        self.toggled.emit(opened)

    # -- memory per page ----------------------------------------------------------------------------------------------

    def _key(self) -> str:
        return f"advanced_sections/{self.page}/{self.title}"

    def _load(self) -> bool:
        try:
            return str(QSettings("GentooLive", "Installer").value(self._key(), "false")).lower() == "true"
        except Exception:  # noqa: BLE001 - settings are a convenience
            return False

    def _save(self, opened: bool) -> None:
        try:
            QSettings("GentooLive", "Installer").setValue(self._key(), "true" if opened else "false")
        except Exception:  # noqa: BLE001
            pass
