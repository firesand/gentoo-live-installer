from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ..keyboard import KeyboardChooser
from ..manifest import InstallManifest
from ..widgets import Card, Page


class WelcomePage(Page):
    tryLiveRequested = Signal()
    applicationRequested = Signal(str)
    handbookRequested = Signal()
    installRequested = Signal()
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
        text = QLabel("Install Gentoo with a clear, guided wizard built around the official Gentoo Handbook, or repair a Linux system that no longer starts.")
        text.setObjectName("heroText")
        text.setWordWrap(True)
        text.setMaximumWidth(760)
        hero.addWidget(text)
        self.content.addLayout(hero)
        self.content.addSpacing(8)

        choices = QHBoxLayout()
        choices.setSpacing(14)
        install = Card("Install Gentoo", "Put Gentoo on this computer's disk. The installer checks your hardware, explains every choice and only changes a disk after your final confirmation. Works on desktops, laptops and virtual machines.")
        self.install_button = QPushButton("Install Gentoo")
        self.install_button.setObjectName("primaryButton")
        self.install_button.setAccessibleDescription("Start the installation wizard")
        self.install_button.clicked.connect(self.installRequested)
        install.content.addWidget(self.install_button)
        choices.addWidget(install, 1)
        repair = Card("Repair an existing system", "Fix a Linux system that will not boot: reinstall the bootloader, rebuild the initramfs, repair /etc/fstab, reset a password or look at the logs. Works with Gentoo and with other Linux distributions.")
        self.repair_button = QPushButton("Repair an existing system")
        self.repair_button.setAccessibleDescription("Open the repair tools")
        self.repair_button.clicked.connect(self.repairRequested)
        repair.content.addWidget(self.repair_button)
        choices.addWidget(repair, 1)
        self.content.addLayout(choices)

        keyboard_card = Card("Language and keyboard", "Pick your keyboard layout first: it is applied to this live session immediately, so passwords and disk passphrases are typed the way you expect, and it becomes the layout of the installed system.")
        self.keyboard = KeyboardChooser()
        self.keyboard.layoutChanged.connect(self.keyboardChanged)
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

        actions = QHBoxLayout()
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
        actions.addStretch()
        self.content.addLayout(actions)

        companions = QHBoxLayout()
        explore = QLabel("Explore the included tools:")
        explore.setProperty("muted", True)
        companions.addWidget(explore)
        for tool, caption in (("hwmonux", "HWMonux"), ("portagehub", "PortageHub")):
            button = QPushButton(caption)
            button.setObjectName("linkButton")
            button.clicked.connect(lambda _=False, name=tool: self.applicationRequested.emit(name))
            companions.addWidget(button)
        companions.addStretch()
        self.content.addLayout(companions)
        self.load_manifest()

    def load_manifest(self) -> None:
        if self.manifest is not None:
            self.keyboard.set_selection(self.manifest.system.keyboard_layout, self.manifest.system.keyboard_variant)

    def focus_first(self) -> None:
        self.install_button.setFocus()
