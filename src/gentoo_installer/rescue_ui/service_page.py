"""The page that explains why the repair tool cannot work yet: no service, no repair engine, or an installation that is running."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel

from ..widgets import Card, wrapped_label
from . import messages
from .base import Notice, RescuePage


class ServicePage(RescuePage):
    title = "The repair tool cannot start"
    subtitle = "The installer service is needed to repair a system."
    step = 0
    retryRequested = Signal()
    terminalToolRequested = Signal()
    quitRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.card = Card(messages.NO_RESCUE_TITLE)
        self.heading = self.card.findChild(QLabel, "cardTitle")
        self.text = wrapped_label("", selectable=True)
        self.card.content.addWidget(self.text)
        self.content.addWidget(self.card)
        self.notice = Notice()
        self.content.addWidget(self.notice)
        self.content.addStretch()
        self.quit_button = self.add_button("&Quit", "rescueQuit", right=False)
        self.quit_button.clicked.connect(self.quitRequested)
        self.tool_button = self.add_button("Open the &terminal tool", "rescueTerminalTool", tip="Opens a terminal that runs: " + messages.TERMINAL_COMMAND)
        self.tool_button.clicked.connect(self.terminalToolRequested)
        self.retry_button = self.add_button("&Try again", "rescueRetry", primary=True)
        self.retry_button.clicked.connect(self.retryRequested)

    def show_problem(self, title: str, text: str, *, can_retry: bool = True) -> None:
        if self.heading is not None:
            self.heading.setText(title)
        self.text.setText(text)
        self.notice.clear()
        self.retry_button.setVisible(can_retry)
        self.card.setAccessibleName(title)

    def focus_first(self) -> None:
        (self.retry_button if self.retry_button.isVisibleTo(self) else self.tool_button).setFocus()
