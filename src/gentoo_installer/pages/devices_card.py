"""The "Devices on this computer" card of the Hardware tab.

It lists the optional device tools that fit the hardware the probe found (``gentoo_installer.devices.device_offers``): Razer, RGB lighting, game
controllers, Logitech, Thunderbolt, pens, ASUS ROG and TUF, ThinkPads. Guided shows at most five lines, each a checkbox and one sentence; whatever
is built on this PC, needs a testing version or comes from an unofficial repository starts unticked and says so. Advanced lists everything, with what was
found, the groups the user joins and the cost. The card is hidden when the probe found nothing worth offering.

Rule shared with the rest of the tab: a value loaded from a settings file is never rewritten by looking at the card. Only the lines the user clicked
are reported back (``touched``).
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QLabel, QVBoxLayout, QWidget

from ..devices import DeviceOffer
from ..widgets import Card, wrapped_label

GUIDED_LINES = 5
# The groups a tool adds the first user to (the plan does it after the packages are in).
GROUPS = {"openrazer": "plugdev", "openrgb": "i2c"}


class _Line(QWidget):
    """One offer: the checkbox, its sentence, the cost, and in Advanced what was found."""

    def __init__(self, offer: DeviceOffer, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = offer.key
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(2)
        self.check = QCheckBox(offer.title)
        self.check.setAccessibleName(offer.title)
        layout.addWidget(self.check)
        self.sentence = wrapped_label("", muted=True)
        self.sentence.setIndent(24)
        layout.addWidget(self.sentence)
        self.detail = wrapped_label("", muted=True)
        self.detail.setIndent(24)
        layout.addWidget(self.detail)
        self.offer = offer

    def show_offer(self, offer: DeviceOffer, checked: bool, advanced: bool) -> None:
        self.offer = offer
        self.check.setText(offer.title)
        blocked = bool(offer.blocked)
        self.check.setEnabled(not blocked)
        self.check.setToolTip(offer.blocked)
        if self.check.isChecked() != (checked and not blocked):
            self.check.blockSignals(True)
            self.check.setChecked(checked and not blocked)
            self.check.blockSignals(False)
        self.sentence.setText(offer.sentence if not blocked else f"{offer.sentence} {offer.blocked}")
        extra = [offer.note] if offer.note else []
        if advanced:
            extra.insert(0, f"Found: {offer.found}.")
            if offer.key in GROUPS:
                extra.append(f"You are added to the group {GROUPS[offer.key]}.")
        self.detail.setText(" ".join(extra))
        self.detail.setVisible(bool(extra))
        self.check.setAccessibleDescription(offer.sentence)


class DevicesCard(Card):
    """The card; ``toggledKey(key, checked)`` is sent when the user clicks a line."""

    toggledKey = Signal(str, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Devices on this computer", "", parent)
        self.intro = wrapped_label("The installer found hardware that has optional software. Tick what you want; nothing here is needed to start the computer.", muted=True)
        self.content.addWidget(self.intro)
        self.lines: dict[str, _Line] = {}
        self.more = wrapped_label("More devices and details are in the Advanced view.", muted=True)
        self.content.addWidget(self.more)
        self.more.hide()
        self.touched: dict[str, bool] = {}
        self.hide()

    def set_offers(self, offers: list[DeviceOffer], wanted: dict[str, bool], advanced: bool) -> None:
        """Show these offers. Unchanged lines are kept (so focus stays); a line the user clicked keeps the user's state."""
        keys = {offer.key for offer in offers}
        for key in list(self.lines):
            if key not in keys:
                line = self.lines.pop(key)
                self.content.removeWidget(line)
                line.deleteLater()
        shown = 0
        hidden_more = False
        for offer in offers:
            line = self.lines.get(offer.key)
            if line is None:
                line = _Line(offer)
                line.check.toggled.connect(lambda checked, name=offer.key: self._clicked(name, checked))
                self.lines[offer.key] = line
                self.content.insertWidget(self.content.indexOf(self.more), line)
            show = advanced or (not offer.advanced and shown < GUIDED_LINES)
            if not advanced and not offer.advanced and shown >= GUIDED_LINES:
                hidden_more = True
            elif offer.advanced and not advanced:
                hidden_more = True
            line.show_offer(offer, self.touched.get(offer.key, wanted.get(offer.key, offer.default)), advanced)
            line.setVisible(show)
            shown += 1 if show else 0
        self.more.setVisible(hidden_more and not advanced)
        self.setVisible(bool(offers) and (shown > 0))

    def _clicked(self, key: str, checked: bool) -> None:
        self.touched[key] = checked
        self.toggledKey.emit(key, checked)

    def visible_keys(self) -> list[str]:
        """The lines on show, top to bottom (``isVisibleTo`` works while the whole tab is hidden)."""
        return [key for key, line in self.lines.items() if line.isVisibleTo(self)]

    def clear_touched(self) -> None:
        self.touched.clear()
