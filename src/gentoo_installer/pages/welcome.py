from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..widgets import Card, Page


class WelcomePage(Page):
    tryLiveRequested = Signal()
    applicationRequested = Signal(str)
    handbookRequested = Signal()

    def __init__(self):
        super().__init__("")
        hero = QVBoxLayout()
        title = QLabel("Your Gentoo system, your way.")
        title.setObjectName("heroTitle")
        title.setWordWrap(True)
        hero.addWidget(title)
        text = QLabel("A clear, guided installation built around the official Gentoo Handbook. Choose the simple path or customize every detail.")
        text.setObjectName("heroText")
        text.setWordWrap(True)
        text.setMaximumWidth(760)
        hero.addWidget(text)
        self.layout.addLayout(hero)
        self.layout.addSpacing(12)

        grid = QGridLayout()
        grid.setSpacing(14)
        cards = [
            ("Guided by the Handbook", "Verified stage3 archives, sensible defaults, and checks at every important step."),
            ("Hardware at a glance", "HWMonux shows temperatures, clocks, fans, power, memory, GPUs, and drives before you install."),
            ("Portage made approachable", "PortageHub lets you browse packages, understand USE flags, preview changes, and follow builds."),
            ("Room for every choice", "OpenRC or systemd, source or binary packages, custom kernels, encryption, desktops, and more."),
        ]
        for index, (heading, description) in enumerate(cards):
            grid.addWidget(Card(heading, description), index // 2, index % 2)
        self.layout.addLayout(grid)
        self.layout.addSpacing(8)

        actions = QHBoxLayout()
        live = QPushButton("Continue trying the live desktop")
        live.clicked.connect(self.tryLiveRequested)
        actions.addWidget(live)
        handbook = QPushButton("Open the AMD64 Handbook")
        handbook.clicked.connect(self.handbookRequested)
        actions.addWidget(handbook)
        actions.addStretch()
        self.layout.addLayout(actions)

        companions = QHBoxLayout()
        label = QLabel("Explore the included tools:")
        label.setProperty("muted", True)
        companions.addWidget(label)
        for app, title in (("hwmonux", "HWMonux"), ("portagehub", "PortageHub")):
            button = QPushButton(title)
            button.setObjectName("linkButton")
            button.clicked.connect(lambda _=False, name=app: self.applicationRequested.emit(name))
            companions.addWidget(button)
        companions.addStretch()
        self.layout.addLayout(companions)
