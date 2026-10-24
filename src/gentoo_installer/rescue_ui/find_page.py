"""Step 1: find the Gentoo systems (any other Linux is named, not offered), unlock encrypted disks and activate LVM or RAID volumes."""

from __future__ import annotations

import subprocess
from typing import Any

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..keyboard import KeyboardChooser
from ..widgets import Card, ChoiceGroup, wrapped_label
from . import messages, model
from .base import Busy, Expander, InlineConfirm, Notice, RescuePage, SecretField, primary_style, soft_breaks, tone
from .client import Call, RescueClient

STAGE_TEXTS = {
    "devices": "Listing the disks…",
    "stacks": "Looking at encryption, LVM and RAID…",
    "probe": "Reading the disks…",
    "environment": "Checking the firmware…",
}
KEYBOARD_FAILURES_BEFORE_OPENING = 2
REPEATED_HINTS = frozenset({"locked_volumes", "inactive_vg", "md_members", "no_gentoo"})  # the cards already say it


def read_layout() -> tuple[str, str] | None:
    """The keyboard layout of this live session (``setxkbmap -query``), or None when it cannot be told (no X session, no program)."""
    try:
        result = subprocess.run(["setxkbmap", "-query"], capture_output=True, text=True, timeout=1.5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    fields = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    layout = fields.get("layout", "")
    return (layout, fields.get("variant", "")) if layout else None


def clear_layout(layout: Any) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


class UnlockPanel(QFrame):
    """Type the passphrase of an encrypted disk. The keyboard layout can be changed here, because the passphrase is typed with this session's layout."""

    unlockRequested = Signal(str, str)  # device, passphrase
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("rescueUnlock")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        column = QVBoxLayout(self)
        column.setContentsMargins(14, 12, 14, 12)
        column.setSpacing(8)
        self.title = QLabel()
        self.title.setObjectName("cardTitle")
        self.title.setWordWrap(True)
        column.addWidget(self.title)
        self.explain = wrapped_label("Type the passphrase you use to start this system. It is sent to the disk and not stored.", muted=True)
        column.addWidget(self.explain)
        self.hints = wrapped_label("", muted=True)
        column.addWidget(self.hints)
        self.passphrase = SecretField("Passphrase")
        self.passphrase.edit.setObjectName("rescuePassphrase")
        column.addWidget(self.passphrase)
        self.typing_note = wrapped_label("")
        self.typing_note.setStyleSheet(f"color: {tone('warning')};")
        column.addWidget(self.typing_note)
        self.layout_label = wrapped_label("", muted=True)
        column.addWidget(self.layout_label)
        self.keyboard = KeyboardChooser()
        self.keyboard.summary.hide()  # its sentence is about the installed system, which is not what this is
        self.keyboard.layoutChanged.connect(self._layout_changed)
        self.keyboard_section = Expander("Change the keyboard layout", self.keyboard)
        column.addWidget(self.keyboard_section)
        self.status = Notice()
        column.addWidget(self.status)
        self.busy = Busy()
        column.addWidget(self.busy)
        row = QHBoxLayout()
        row.addStretch()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("rescueUnlockCancel")
        self.cancel_button.clicked.connect(self._cancel)
        row.addWidget(self.cancel_button)
        self.go_button = QPushButton("&Unlock")
        self.go_button.setObjectName("rescueUnlockGo")
        self.go_button.setStyleSheet(primary_style())
        self.go_button.clicked.connect(self._go)
        row.addWidget(self.go_button)
        column.addLayout(row)
        self.passphrase.returnPressed.connect(self._go)
        self.passphrase.textChanged.connect(self._typed)
        self.device = ""
        self.failures = 0
        self.hide()

    def open_for(self, device: str, size: str, hints: list[str]) -> None:
        self.device = device
        self.failures = 0
        self.title.setText(f"Unlock {device} ({size})")
        self.hints.setText(soft_breaks(" ".join(hints)))
        self.hints.setVisible(bool(hints))
        self.passphrase.clear()
        self.status.clear()
        self.busy.stop()
        self.typing_note.clear()
        self._show_layout()
        self.keyboard_section.button.setChecked(False)
        self.set_busy(False)
        self.show()
        self.passphrase.setFocus()

    def _show_layout(self) -> None:
        current = read_layout()
        if current is None:
            self.layout_label.setText("The keyboard layout of this session could not be read. If the passphrase is not accepted, check the layout below.")
            return
        layout, variant = current
        self.keyboard.set_selection(layout, variant)
        self.layout_label.setText(f"Keyboard layout in use: {self._layout_name(layout, variant)}. The passphrase is typed with this layout.")

    def _layout_name(self, layout: str, variant: str) -> str:
        found = next((item for item in self.keyboard.layouts if item.name == layout), None)
        name = found.description if found is not None else layout
        return f"{name}, {variant}" if variant else name

    def _layout_changed(self, layout: str, variant: str, _console: str) -> None:
        self.layout_label.setText(f"Keyboard layout in use: {self._layout_name(layout, variant)}. The passphrase is typed with this layout.")

    def _typed(self, text: str) -> None:
        if any(ord(char) > 127 for char in text):
            self.typing_note.setText("This passphrase has characters that are not on a US keyboard. The prompt that asks for it when the computer starts often uses a US "
                                     "layout, so the passphrase may have been set with other keys. Check the keyboard layout if it is not accepted.")
        else:
            self.typing_note.clear()
        self.go_button.setEnabled(bool(text) and not self.busy.isVisibleTo(self))

    def set_busy(self, busy: bool) -> None:
        for widget in (self.passphrase, self.keyboard_section, self.cancel_button):
            widget.setEnabled(not busy)
        self.go_button.setEnabled(not busy and bool(self.passphrase.text()))
        if busy:
            self.busy.start("Unlocking… this can take a few seconds.")
        else:
            self.busy.stop()

    def failed(self, text: str) -> None:
        """The disk did not open: show the engine's explanation, which already mentions the keyboard layout, and let the person try again."""
        self.failures += 1
        self.set_busy(False)
        self.status.show_message("error", text)
        if self.failures >= KEYBOARD_FAILURES_BEFORE_OPENING:
            self.keyboard_section.button.setChecked(True)
        self.passphrase.setFocus()

    def close_panel(self) -> None:
        self.passphrase.clear()  # the text must not stay in the window
        self.hide()

    def _go(self) -> None:
        if self.go_button.isEnabled() and self.passphrase.text():
            self.set_busy(True)
            self.status.clear()
            self.unlockRequested.emit(self.device, self.passphrase.text())

    def _cancel(self) -> None:
        self.close_panel()
        self.cancelled.emit()

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Escape and self.cancel_button.isEnabled():
            self._cancel()
            return
        super().keyPressEvent(event)


class FindPage(RescuePage):
    title = "Find the Gentoo system to repair"
    subtitle = messages.REPAIR_TITLE + ". The tool looks at the disks of this computer. Nothing is changed."
    step = 0
    systemChosen = Signal(dict, dict)  # the system's scan entry, the whole scan
    serviceProblem = Signal(str)
    storageChanged = Signal()  # a disk was unlocked or a volume activated: it has to be released again
    resumeRequested = Signal(dict)  # a session that is still open in the daemon (the reply of rescue.session)
    refreshRequested = Signal()  # leftovers were released: ask the daemon again what is open
    quitRequested = Signal()

    def __init__(self, client: RescueClient) -> None:
        super().__init__()
        self.client = client
        self.scan: dict[str, Any] = {}
        self.candidates: list[dict[str, Any]] = []  # the Gentoo systems: the ones that can be opened
        self.others: list[dict[str, Any]] = []  # every other Linux the scan found: named, never opened
        self._call: Call | None = None
        self._busy = False
        self._selected = ""

        self.firmware = wrapped_label("", muted=True)
        self.content.addWidget(self.firmware)
        self.notice = Notice()
        self.content.addWidget(self.notice)
        self.busy = Busy()
        self.content.addWidget(self.busy)

        self.open_card = Card("Still open", "These systems were opened earlier and are still open. Continue with one, or close it on the last page.")
        self.open_rows = QVBoxLayout()
        self.open_card.content.addLayout(self.open_rows)
        self.open_card.hide()
        self.content.addWidget(self.open_card)

        self.left_card = Card("Left open by an earlier repair", messages.LEFTOVERS_TEXT)
        self.left_rows = QVBoxLayout()
        self.left_card.content.addLayout(self.left_rows)
        release_row = QHBoxLayout()
        release_row.addStretch()
        self.release_button = QPushButton("&Release them")
        self.release_button.setObjectName("rescueReleaseLeftovers")
        self.release_button.setAccessibleName("Release what the earlier repair left open")
        self.release_button.clicked.connect(self._release_leftovers)
        release_row.addWidget(self.release_button)
        self.left_card.content.addLayout(release_row)
        self.left_card.hide()
        self.content.addWidget(self.left_card)
        self._leftover_calls: list[dict[str, Any]] = []
        self._note_after_scan = ""

        self.systems_card = Card("Gentoo systems", "Choose the system you want to repair.")
        self.group = ChoiceGroup([], columns=1)
        self.group.selectionChanged.connect(self._selection_changed)
        self.systems_card.content.addWidget(self.group)
        self.content.addWidget(self.systems_card)

        self.locked_card = Card("Encrypted disks", "These disks are locked. Unlock a disk to look for a system inside it.")
        self.locked_rows = QVBoxLayout()
        self.locked_card.content.addLayout(self.locked_rows)
        self.unlock_panel = UnlockPanel()
        self.unlock_panel.unlockRequested.connect(self._unlock)
        self.unlock_panel.cancelled.connect(self._unlock_cancelled)
        self.locked_card.content.addWidget(self.unlock_panel)
        self.content.addWidget(self.locked_card)

        self.storage_card = Card("Volumes that are not active", "A system can be on an LVM volume or a RAID array that must be activated first.")
        self.storage_rows = QVBoxLayout()
        self.storage_card.content.addLayout(self.storage_rows)
        self.content.addWidget(self.storage_card)

        self.empty_card = Card(messages.NOTHING_FOUND_TITLE, messages.NOTHING_FOUND_TEXT)
        self.empty_title = self.empty_card.findChild(QLabel, "cardTitle")
        self.empty_text = self.empty_card.findChildren(QLabel)[-1]
        self.content.addWidget(self.empty_card)

        self.others_card = Card(messages.OTHER_SYSTEMS_TITLE, messages.NOT_GENTOO_TEXT)  # after the cards above: with one Gentoo it sits right below it
        self.others_rows = QVBoxLayout()
        self.others_card.content.addLayout(self.others_rows)
        self.others_card.hide()
        self.content.addWidget(self.others_card)

        self.confirm = InlineConfirm()
        self.confirm.confirmed.connect(self._activation_confirmed)
        self.confirm.cancelled.connect(self._activation_cancelled)
        self.content.addWidget(self.confirm)

        self.hints = wrapped_label("")
        self.content.addWidget(self.hints)
        self.live_note = wrapped_label("", muted=True)
        self.content.addWidget(self.live_note)
        self.disks_text = wrapped_label("", selectable=True)
        self.disks_text.setStyleSheet("font-family: monospace;")
        self.disks = Expander("Show all disks of this computer", self.disks_text)
        self.content.addWidget(self.disks)
        self.content.addStretch()

        self.quit_button = self.add_button("&Quit", "rescueQuit", right=False)
        self.quit_button.clicked.connect(self.quitRequested)
        self.scan_button = self.add_button("&Scan again", "rescueScan", right=False, tip="Look at the disks again")
        self.scan_button.clicked.connect(self.start_scan)
        self.continue_button = self.add_button("&Open this system", "rescueContinue", primary=True, tip="Open the selected system for looking and repairing")
        self.continue_button.clicked.connect(self._continue)
        self._pending: dict[str, Any] = {}
        self._render()

    def show_connecting(self) -> None:
        self.notice.clear()
        self.busy.start(messages.CONNECTING_TEXT)
        self.scan_button.setEnabled(False)

    def show_open_sessions(self, sessions: list[dict[str, Any]]) -> None:
        """Systems that an earlier window (or a crashed one) left open: they can be taken over instead of opened again."""
        clear_layout(self.open_rows)
        for session in sessions:
            row = QHBoxLayout()
            title = model.os_name(session.get("os") or {})
            mode = "repairs enabled" if session.get("mode") == "rw" else "read-only"
            row.addWidget(wrapped_label(soft_breaks(f"{title} on {(session.get('root') or {}).get('device', '')} ({mode})")), 1)
            button = QPushButton("&Continue with it")
            button.setObjectName(f"rescueResume:{session.get('session_id')}")
            button.setAccessibleName(f"Continue with {title}")
            button.clicked.connect(lambda _=False, item=session: self.resumeRequested.emit(item))
            row.addWidget(button)
            self.open_rows.addLayout(row)
        self.open_card.setVisible(bool(sessions))

    def show_leftovers(self, orphans: list[dict[str, Any]], pool: list[dict[str, Any]]) -> None:
        """What an earlier repair left that no window can continue with: sessions whose disks are still flagged, mounts of unknown origin, unlocked or activated volumes."""
        clear_layout(self.left_rows)
        calls: list[dict[str, Any]] = []
        for item in orphans:
            sid = str(item.get("sid", ""))
            if item.get("state") == "unknown":
                text = f"Something is mounted at {item.get('path', sid)} and the tool cannot tell whether it opened it. Unmount it yourself in a terminal: sudo umount -R {item.get('path', sid)}"
            elif sid and item.get("state") in {"stale", "alive"}:
                calls.append({"do": "cleanup", "sid": sid})
                text = (f"The repair {sid} was cut short; its disks may still be locked against writing." if item.get("state") == "stale"
                        else f"The repair {sid} is still mounted, but this window cannot continue with it.")
            else:
                continue
            self.left_rows.addWidget(wrapped_label(soft_breaks(text)))
        if pool:
            calls.append({"do": "cleanup"})
            names = ", ".join(sorted({str(entry.get("name") or entry.get("device") or entry.get("kind", "")) for entry in pool}))
            self.left_rows.addWidget(wrapped_label(soft_breaks(f"Unlocked disks or active volumes are still open: {names}.")))
        self._leftover_calls = calls
        self.release_button.setEnabled(bool(calls) and not self._busy)
        self.release_button.setVisible(bool(calls))
        self.left_card.setVisible(bool(self.left_rows.count()))

    def _release_leftovers(self) -> None:
        if self._busy or not self._leftover_calls:
            return
        self.notice.clear()
        self._release_next(list(self._leftover_calls))

    def _release_next(self, calls: list[dict[str, Any]]) -> None:
        if not calls:
            self._set_busy(False)
            self._note_after_scan = "Everything the earlier repair left open was released."  # shown when the scan that follows has finished (a scan starts with a clean page)
            self.refreshRequested.emit()
            return
        self._set_busy(True, "Releasing what the earlier repair left open…")

        def released(result: dict[str, Any]) -> None:
            problems = [str(error) for error in result.get("errors", [])] + [f"{path} is still in use" for path in result.get("leftovers", []) if isinstance(path, str)]
            if problems:
                self._set_busy(False)
                self.notice.show_message("error", "Not everything could be released: " + "; ".join(problems) + ". Close the programs that use it and try again.")
                return
            self._release_next(calls[1:])

        def failed(_code: str, text: str, _data: dict[str, Any]) -> None:
            self._set_busy(False)
            self.notice.show_message("error", text)

        def lost(text: str) -> None:
            self._set_busy(False)
            self.notice.show_message("error", text + " Use “Scan again” to see what is still open.")

        call = self.client.request("rescue.recover", calls[0])
        self._call = call
        call.then(released, failed, lost)

    # ---- scanning -------------------------------------------------------------------------------------------------------------------
    def start_scan(self) -> None:
        if self._busy:
            return
        self._set_busy(True, "Looking for installed systems…")
        self.notice.clear()
        self._call = self.client.request("rescue.scan", {"probe": True})
        self._call.eventReceived.connect(self._scan_event)
        self._call.then(self._scanned, self._scan_failed, self._scan_lost)

    def _scan_event(self, event: dict[str, Any]) -> None:
        data = model.event_data(event)
        if event.get("type") == "stage":
            self.busy.progress(STAGE_TEXTS.get(str(event.get("stage")), str(event.get("message", ""))))
        elif event.get("type") == "progress":
            total = int(data.get("total") or 0)
            self.busy.progress(f"Reading {data['device']} ({data.get('index')} of {total})…" if data.get("device") else str(event.get("message", "")), event.get("progress"))

    def _scanned(self, scan: dict[str, Any]) -> None:
        self._set_busy(False)
        self.scan = scan
        self.candidates, self.others = model.split_candidates(scan.get("candidates", []))
        self._render()
        if self._note_after_scan:
            self.notice.show_message("ok", self._note_after_scan)
            self._note_after_scan = ""

    def _scan_failed(self, _code: str, text: str, _data: dict[str, Any]) -> None:
        self._set_busy(False)
        self.notice.show_message("error", text)

    def _scan_lost(self, text: str) -> None:
        self._set_busy(False)
        if self._call is not None and not self._call.connected:
            self.serviceProblem.emit(text)
            return
        self.notice.show_message("error", text + " Use “Scan again” to try once more.")

    def _set_busy(self, busy: bool, text: str = "") -> None:
        """Lock the page while the daemon works. No text: the panel that started the work shows its own progress."""
        self._busy = busy
        if busy and text:
            self.busy.start(text)
        else:
            self.busy.stop()
        for button in (self.scan_button,):
            button.setEnabled(not busy)
        self.release_button.setEnabled(not busy and bool(self._leftover_calls))
        self._update_continue()

    # ---- drawing the result ---------------------------------------------------------------------------------------------------------
    def _render(self) -> None:
        scan = self.scan
        devices = scan.get("devices", [])
        rows = [model.describe_system(item, devices) for item in sorted(self.candidates, key=lambda item: (item.get("kind") == "snapshot", str(item.get("device"))))]
        self.firmware.setText(model.environment_text(scan["environment"]) if scan.get("environment") else "")
        self.firmware.setVisible(bool(scan))
        choices = []
        for row in rows:
            text = "\n".join([*row.lines, *(f"! {warning}" for warning in row.warnings)])
            choices.append((row.id, row.title, text, False))
        previous = self.group.value()
        self.group.set_choices(choices)
        for row in rows:
            card = self.group.cards.get(row.id)
            if card is not None:
                card.setProperty("rescueSystem", row.id)
                card.setAccessibleName(f"{row.title}, {row.device}")
        if previous in self.group.cards:
            self.group.set_value(previous)
        elif len(rows) == 1 and not rows[0].snapshot:
            self.group.set_value(rows[0].id)
        self.systems_card.setVisible(bool(rows))
        self._render_others([model.describe_system(item, devices) for item in sorted(self.others, key=lambda item: str(item.get("device")))])
        locked = model.locked_volumes(scan)
        inactive = model.inactive_groups(scan)
        arrays = model.unassembled_arrays(scan)
        self.empty_card.setVisible(bool(scan) and not rows)
        waiting = bool(locked or inactive or arrays)  # the volumes below have to be opened first: the usual list of reasons would only distract
        if self.empty_title is not None:
            self.empty_title.setText(messages.NOTHING_YET_TITLE if waiting else messages.NOTHING_FOUND_TITLE)
        self.empty_text.setText(messages.NOTHING_YET_TEXT if waiting else messages.NOTHING_FOUND_INTRO + (messages.NOTHING_FOUND_OTHERS if self.others else "") + messages.NOTHING_FOUND_REASONS)
        self._render_locked(locked)
        self._render_storage(inactive, arrays)
        # what the cards above already say (an encrypted disk, an inactive volume group, a RAID array) is not said twice
        hints = [item["text"] for item in scan.get("hints", []) if item.get("id") not in REPEATED_HINTS] + [str(item) for item in scan.get("warnings", [])]
        self.hints.setText(soft_breaks("\n".join(f"• {text}" for text in hints)))
        self.hints.setVisible(bool(hints))
        live = scan.get("live_medium", [])
        self.live_note.setText(messages.LIVE_MEDIUM_NOTICE + f" ({', '.join(live)})" if live else "")
        self.live_note.setVisible(bool(live))
        overview = model.disk_overview(devices)
        self.disks_text.setText(soft_breaks("\n".join(overview)))
        self.disks.setVisible(bool(overview))
        self._update_continue()

    def _render_others(self, rows: list[model.SystemRow]) -> None:
        """The systems that are not Gentoo, by the name they give themselves and where they are; the card says once why they cannot be opened."""
        clear_layout(self.others_rows)
        named: set[tuple[str, str]] = set()
        for row in rows:
            if (row.title, row.device) in named:  # two roots of one disk read the same
                continue
            named.add((row.title, row.device))
            label = wrapped_label(soft_breaks(f"{row.title} on {row.device}"))
            label.setObjectName(f"rescueOther:{row.device}")
            self.others_rows.addWidget(label)
        self.others_card.setVisible(bool(rows))

    def _render_locked(self, locked: list[model.LockedVolume]) -> None:
        clear_layout(self.locked_rows)
        for volume in locked:
            row = QHBoxLayout()
            label = wrapped_label(f"{volume.device} — {volume.size}, encrypted (LUKS{volume.version})")
            row.addWidget(label, 1)
            button = QPushButton("&Unlock…")
            button.setObjectName(f"rescueUnlock:{volume.device}")
            button.setAccessibleName(f"Unlock {volume.device}")
            button.clicked.connect(lambda _=False, v=volume: self._open_unlock(v))
            row.addWidget(button)
            self.locked_rows.addLayout(row)
        self.locked_card.setVisible(bool(locked))
        if not locked:
            self.unlock_panel.close_panel()

    def _render_storage(self, groups: list[dict[str, Any]], arrays: list[dict[str, Any]]) -> None:
        clear_layout(self.storage_rows)
        for group in groups:
            row = QHBoxLayout()
            row.addWidget(wrapped_label(f"LVM volume group “{group['name']}” is not active."), 1)
            button = QPushButton("&Activate…")
            button.setObjectName(f"rescueActivate:{group['name']}")
            button.setAccessibleName(f"Activate volume group {group['name']}")
            button.clicked.connect(lambda _=False, g=group: self._ask_activate_lvm(g))
            row.addWidget(button)
            self.storage_rows.addLayout(row)
        for array in arrays:
            row = QHBoxLayout()
            name = array.get("name") or array.get("uuid")
            row.addWidget(wrapped_label(f"RAID array {name} ({array.get('level', 'raid')}) is not assembled."), 1)
            button = QPushButton("&Assemble…")
            button.setObjectName(f"rescueAssemble:{array['uuid']}")
            button.setAccessibleName(f"Assemble RAID array {name}")
            button.clicked.connect(lambda _=False, a=array: self._ask_assemble(a))
            row.addWidget(button)
            self.storage_rows.addLayout(row)
        self.storage_card.setVisible(bool(groups or arrays))

    # ---- choosing -------------------------------------------------------------------------------------------------------------------
    def _selection_changed(self, key: str) -> None:
        self._selected = key
        self._update_continue()

    def selected_candidate(self) -> dict[str, Any] | None:
        key = self.group.value()
        return next((item for item in self.candidates if item.get("id") == key), None)

    def _update_continue(self) -> None:
        self.continue_button.setEnabled(not self._busy and self.selected_candidate() is not None)

    def _continue(self) -> None:
        candidate = self.selected_candidate()
        if candidate is not None and not self._busy:
            self.systemChosen.emit(candidate, self.scan)

    # ---- unlocking ------------------------------------------------------------------------------------------------------------------
    def _open_unlock(self, volume: model.LockedVolume) -> None:
        self.confirm.hide()
        self.unlock_panel.open_for(volume.device, volume.size, volume.hints)
        self._show_panel()

    def _show_panel(self) -> None:
        """Scroll the passphrase panel (and its buttons) into view once it has its size."""
        QTimer.singleShot(0, lambda: self.scroll_area.ensureWidgetVisible(self.unlock_panel, 0, 24) if self.unlock_panel.isVisible() else None)

    def _unlock(self, device: str, passphrase: str) -> None:
        self._set_busy(True)
        call = self.client.request("rescue.unlock", {"device": device, "passphrase": passphrase})
        self._call = call
        call.then(self._unlocked, self._unlock_failed, self._unlock_lost)

    def _unlocked(self, _result: dict[str, Any]) -> None:
        self.storageChanged.emit()
        self.unlock_panel.close_panel()
        self._set_busy(False)
        self.notice.show_message("ok", "The disk is unlocked. Looking again for systems…")
        self.start_scan()

    def _unlock_failed(self, _code: str, text: str, _data: dict[str, Any]) -> None:
        self._set_busy(False)
        self.unlock_panel.failed(text)
        self._show_panel()

    def _unlock_lost(self, text: str) -> None:
        self._set_busy(False)
        self.unlock_panel.failed(text + " Check with “Scan again” whether the disk was unlocked.")

    def _unlock_cancelled(self) -> None:
        self.scan_button.setFocus()

    # ---- LVM and RAID ---------------------------------------------------------------------------------------------------------------
    def _ask_activate_lvm(self, group: dict[str, Any]) -> None:
        self._pending = {"kind": "lvm", "vg": group["name"]}
        self.unlock_panel.close_panel()
        self.confirm.ask(f"Activate the volume group “{group['name']}”?", messages.LVM_ACTIVATION_NOTICE, "&Activate")

    def _ask_assemble(self, array: dict[str, Any]) -> None:
        self._pending = {"kind": "md", "uuids": [array["uuid"]]}
        self.unlock_panel.close_panel()
        tick = "Start the array even though a disk is missing (data may be at risk)" if array.get("degraded") else ""
        self.confirm.ask(f"Assemble the RAID array {array.get('name') or array['uuid']}?", "The array is started so its file systems can be read. The disks are not changed.",
                         "&Assemble", tick=tick)

    def _activation_confirmed(self, _typed: str, _phrase: str, degraded: bool) -> None:
        params = dict(self._pending)
        if params.get("kind") == "md" and degraded:
            params["degraded_ok"] = True
        self._set_busy(True, "Activating…")
        call = self.client.request("rescue.activate", params)
        self._call = call
        call.then(self._activated, self._activation_failed, self._activation_lost)

    def _activated(self, _result: dict[str, Any]) -> None:
        self.storageChanged.emit()
        self._set_busy(False)
        self.start_scan()

    def _activation_failed(self, _code: str, text: str, _data: dict[str, Any]) -> None:
        self._set_busy(False)
        self.notice.show_message("error", text)

    def _activation_lost(self, text: str) -> None:
        self._set_busy(False)
        self.notice.show_message("error", text + " Use “Scan again” to see what was done.")

    def _activation_cancelled(self) -> None:
        self._pending = {}

    # ---- window hooks ---------------------------------------------------------------------------------------------------------------
    def close_blocker(self) -> str:
        return ""

    def focus_first(self) -> None:
        card = self.group.cards.get(self.group.value())
        if card is not None:
            card.setFocus()
        elif self.continue_button.isEnabled():
            self.continue_button.setFocus()
        else:
            self.scan_button.setFocus()
