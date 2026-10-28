from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ..keyboard import KeyboardChooser
from ..manifest import InstallManifest
from ..widgets import Card, FlowLayout, Page, wrapped_label


WILL_DO_TEXT = ("It takes about 1 hour with binary packages (longer if you choose to compile), it needs an internet connection, "
                "and it erases only the disk you choose, after you confirm it at the end.")


class WelcomePage(Page):
    tryLiveRequested = Signal()
    applicationRequested = Signal(str)
    handbookRequested = Signal()
    repairRequested = Signal()
    importRequested = Signal()
    keyboardChanged = Signal(str, str, str)  # XKB layout, variant, console keymap

    def __init__(self, manifest: InstallManifest | None = None):
        super().__init__("")
        self.manifest = manifest
        hero = QVBoxLayout()
        title = QLabel("Your Gentoo system, your way.")
        title.setObjectName("heroTitle")
        title.setWordWrap(True)
        hero.addWidget(title)
        text = QLabel("Install Gentoo with a clear, guided wizard built around the official Gentoo Handbook, or repair a Gentoo system that no longer starts.")
        text.setObjectName("heroText")
        text.setWordWrap(True)
        text.setMaximumWidth(760)
        hero.addWidget(text)
        self.content.addLayout(hero)
        self.content.addSpacing(8)

        will_do = Card("What this will do", "")
        self.will_do = wrapped_label(WILL_DO_TEXT, selectable=True)
        self.will_do.setAccessibleName("What this will do")
        will_do.content.addWidget(self.will_do)
        self.content.addWidget(will_do)

        choices = QHBoxLayout()
        choices.setSpacing(14)
        install = Card("Install Gentoo", "Put Gentoo on this computer's disk. The installer checks your hardware, explains every choice and only changes a disk after your final confirmation. Works on desktops, laptops and virtual machines. Press Continue to begin.")
        choices.addWidget(install, 1)
        repair = Card("Fix a Gentoo system that will not boot", "Reinstall the bootloader, rebuild the initramfs, repair /etc/fstab, reset a password or look at the logs. This tool works on Gentoo systems only.")
        self.repair_button = QPushButton("Repair a Gentoo system")
        self.repair_button.setAccessibleDescription("Open the repair tools for a Gentoo system that does not start")
        self.repair_button.clicked.connect(self.repairRequested)
        repair.content.addWidget(self.repair_button)
        choices.addWidget(repair, 1)
        self.content.addLayout(choices)

        keyboard_card = Card("Keyboard", "It is applied to this live session at once, so passwords and disk passphrases are typed the way you expect, and it becomes the layout of the installed system.")
        line = QHBoxLayout()
        self.keyboard_line = QLabel()
        self.keyboard_line.setAccessibleName("Current keyboard")
        line.addWidget(self.keyboard_line, 1)
        self.keyboard_button = QPushButton("Change")
        self.keyboard_button.setCheckable(True)
        self.keyboard_button.setAccessibleDescription("Choose another keyboard layout")
        self.keyboard_button.toggled.connect(self._keyboard_chooser_toggled)
        line.addWidget(self.keyboard_button)
        keyboard_card.content.addLayout(line)
        self.keyboard = KeyboardChooser()
        self.keyboard.layoutChanged.connect(self.keyboardChanged)
        self.keyboard.layoutChanged.connect(lambda *_: self._update_keyboard_line())
        self.keyboard.hide()
        keyboard_card.content.addWidget(self.keyboard)
        self.content.addWidget(keyboard_card)

        grid = QGridLayout()
        grid.setSpacing(14)
        cards = [
            ("Guided by the Handbook", "Verified stage3 archives, sensible defaults, and checks at every important step."),
            ("Hardware at a glance", "HWMonux shows temperatures, clocks, fans, power, memory, GPUs, and drives before you install."),
            ("Portage made approachable", "PortageHub lets you browse packages, understand USE flags, preview changes, and follow builds."),
            ("Room for every choice", "OpenRC or systemd, source or binary packages, encryption, desktops, and more."),
        ]
        for index, (heading, description) in enumerate(cards):
            grid.addWidget(Card(heading, description), index // 2, index % 2)
        self.content.addLayout(grid)
        self.content.addSpacing(8)

        actions = FlowLayout()
        live = QPushButton("Continue trying the live desktop")
        live.clicked.connect(self.tryLiveRequested)
        actions.addWidget(live)
        handbook = QPushButton("Open the AMD64 &Handbook")
        handbook.clicked.connect(self.handbookRequested)
        actions.addWidget(handbook)
        self.import_button = QPushButton("&Import settings…")
        self.import_button.setToolTip("Load a settings file saved from an earlier run. Passwords are never saved and must be typed again.")
        self.import_button.clicked.connect(self.importRequested)
        actions.addWidget(self.import_button)
        self.content.addLayout(actions)

        companions = FlowLayout()
        explore = QLabel("Explore the included tools:")
        explore.setProperty("muted", True)
        companions.addWidget(explore)
        for tool, caption in (("hwmonux", "HWMonux"), ("portagehub", "PortageHub"), ("gentoo-settings", "Gentoo Settings"), ("kernelsmith", "KernelSmith")):
            button = QPushButton(caption)
            button.setObjectName("linkButton")
            button.clicked.connect(lambda _=False, name=tool: self.applicationRequested.emit(name))
            companions.addWidget(button)
        self.content.addLayout(companions)
        self.load_manifest()
        self.finish_layout()

    def _keyboard_chooser_toggled(self, shown: bool) -> None:
        self.keyboard.setVisible(shown)
        self.keyboard_button.setText("Done" if shown else "Change")

    def _update_keyboard_line(self) -> None:
        name = self.keyboard.layout_box.currentText().rsplit("  (", 1)[0].strip() or "English (US)"
        self.keyboard_line.setText(f"Keyboard: {name}")

    def load_manifest(self) -> None:
        if self.manifest is not None:
            self.keyboard.set_selection(self.manifest.system.keyboard_layout, self.manifest.system.keyboard_variant)
        self._update_keyboard_line()

    def summary_rows(self) -> list[tuple[str, str]]:
        return []

    def focus_first(self) -> None:
        self.keyboard_button.setFocus()
