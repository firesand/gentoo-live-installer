"""The manual layout editor of the Storage page.

Rows are picked from the partitions lsblk found on the chosen disk instead of
typed, so a typo can no longer reach the daemon. Existing data is kept unless
the row's Format box is ticked; the editor says what is on every partition and
which ones (Windows, EFI) it proposes to keep.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..keyboard import start_detached
from ..manifest import Partition
from ..system_info import DiskInfo, PartitionInfo, SystemInfo
from ..widgets import FlowLayout, wrapped_label

MOUNT_CHOICES = ("/", "/boot", "/efi", "/home", "swap")
FILESYSTEMS = (
    ("ext4", "ext4"), ("xfs", "XFS"), ("btrfs", "Btrfs"), ("vfat", "FAT32 (EFI)"), ("swap", "swap"), ("none", "none (do not mount)"),
)
# lsblk filesystem names that a declared filesystem may keep without formatting.
COMPATIBLE = {"ext4": {"ext2", "ext3", "ext4"}, "xfs": {"xfs"}, "btrfs": {"btrfs"}, "vfat": {"vfat", "fat32", "fat16"}, "swap": {"swap"}}
DETECTED = {"ext2": "ext4", "ext3": "ext4", "ext4": "ext4", "xfs": "xfs", "btrfs": "btrfs", "vfat": "vfat", "swap": "swap"}
EFI_MOUNTS = {"/efi", "/boot/efi"}
COLUMNS = ("Partition", "Use as", "Filesystem", "Format", "Label", "Currently contains")
STACKED = {"crypt", "lvm", "raid0", "raid1", "raid5", "raid6", "raid10", "md"}


class _Check(QWidget):
    """A centred checkbox for a table cell."""

    def __init__(self, checked: bool):
        super().__init__()
        box = QHBoxLayout(self)
        box.setContentsMargins(6, 0, 6, 0)
        box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.check = QCheckBox()
        self.check.setChecked(checked)
        box.addWidget(self.check)


class ManualLayoutEditor(QWidget):
    rescanRequested = Signal()
    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.info: SystemInfo | None = None
        self.disk_path = ""
        self._loading = False
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        for column, width in ((1, 120), (2, 150), (4, 100)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(column, width)
        self.table.setMinimumHeight(220)
        self.table.setAccessibleName("Manual partitions")
        box.addWidget(self.table)
        buttons = FlowLayout()
        self.add_button = QPushButton("Add partition")
        self.add_button.clicked.connect(lambda: self.add_row())
        self.remove_button = QPushButton("Remove selected row")
        self.remove_button.clicked.connect(self.remove_selected)
        self.suggest_button = QPushButton("Suggest a layout")
        self.suggest_button.setToolTip("Fill the table from the partitions found on the disk: the EFI partition is kept, the largest Linux partition becomes /.")
        self.suggest_button.clicked.connect(lambda: self.suggest_layout())
        self.rescan_button = QPushButton("Rescan disks")
        self.rescan_button.clicked.connect(self.rescanRequested)
        self.gparted_button = QPushButton("Open GParted")
        self.gparted_button.setToolTip("Create or resize partitions with GParted, then press Rescan disks.")
        self.gparted_button.clicked.connect(self.open_gparted)
        for button in (self.add_button, self.remove_button, self.suggest_button, self.rescan_button, self.gparted_button):
            buttons.addWidget(button)
        box.addLayout(buttons)
        self.note = wrapped_label("", muted=True, selectable=True)
        self.note.hide()
        box.addWidget(self.note)
        self.gparted_message = wrapped_label("", muted=True)
        self.gparted_message.hide()
        box.addWidget(self.gparted_message)

    # --- candidates -------------------------------------------------------------------------

    def set_info(self, info: SystemInfo) -> None:
        self.info = info
        self._refresh_all()

    def set_disk(self, path: str) -> None:
        self.disk_path = path
        self._refresh_all()

    def disk(self) -> DiskInfo | None:
        return self.info.find_disk(self.disk_path) if self.info and self.disk_path else None

    def other_disks(self) -> list[DiskInfo]:
        """The other disks whose partitions may be used too: any disk that can be a target itself."""
        selected = self.disk()
        if self.info is None or selected is None:
            return []
        return [disk for disk in self.info.disks if disk.selectable and disk.path != selected.path]

    def candidates(self) -> list[PartitionInfo]:
        disk = self.disk()
        if disk is None:
            return []
        found = [part for part in disk.partitions if part.type == "part" or part.type in STACKED]
        for other in self.other_disks():
            found += [part for part in other.partitions if part.type == "part" or part.type in STACKED]
        return found

    def extra_disks(self) -> list[DiskInfo]:
        """The other disks that the rows of the table live on (each one must be approved and confirmed)."""
        used: list[DiskInfo] = []
        selected = self.disk()
        for row in self.rows():
            part = self.info.find_partition(row.device) if self.info is not None and row.device else None
            owner = self.info.find_disk(part.parent) if self.info is not None and part is not None else None
            if owner is not None and selected is not None and owner.path != selected.path and owner not in used:
                used.append(owner)
        return used

    def _refresh_all(self) -> None:
        for row in range(self.table.rowCount()):
            self._fill_device_combo(row, keep=self._device(row))
            self._show_contents(row)
        self._update_note()

    def _fill_device_combo(self, row: int, keep: str) -> None:
        combo = self.table.cellWidget(row, 0)
        assert isinstance(combo, QComboBox)
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("— choose a partition —", "")
        selected = self.disk()
        names = {disk.path: disk for disk in self.other_disks()}
        for part in self.candidates():
            text = part.choice_text() + (f" · mounted at {', '.join(part.mountpoints)}" if part.mounted else "")
            if selected is not None and part.parent != selected.path and part.parent in names:
                text += f" · on {names[part.parent].model} ({part.parent})"
            combo.addItem(text, part.path)
        if keep and combo.findData(keep) < 0:
            combo.addItem(f"{keep} (not found on this disk)", keep)
        combo.setCurrentIndex(max(0, combo.findData(keep)) if keep else 0)
        combo.blockSignals(False)

    # --- rows ---------------------------------------------------------------------------------

    def row_count(self) -> int:
        return self.table.rowCount()

    def add_row(self, partition: Partition | None = None) -> int:
        row = self.table.rowCount()
        self.table.insertRow(row)
        device = QComboBox()
        device.setAccessibleName("Partition")
        self.table.setCellWidget(row, 0, device)
        mount = QComboBox()
        mount.setEditable(True)
        mount.addItems(MOUNT_CHOICES)
        mount.setAccessibleName("Use as")
        self.table.setCellWidget(row, 1, mount)
        fs = QComboBox()
        for value, label in FILESYSTEMS:
            fs.addItem(label, value)
        fs.setAccessibleName("Filesystem")
        self.table.setCellWidget(row, 2, fs)
        check = _Check(partition.format if partition else False)
        self.table.setCellWidget(row, 3, check)
        self.table.setItem(row, 4, QTableWidgetItem(partition.label if partition else ""))
        contents = QTableWidgetItem("")
        contents.setFlags(contents.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, 5, contents)
        self._fill_device_combo(row, keep=partition.device if partition else "")
        if partition is not None:
            self._set_from_partition(row, partition)
        elif not self.disk():
            mount.setCurrentText("/" if row == 0 else "")
        device.currentIndexChanged.connect(lambda _=0, w=device: self._device_picked(w))
        mount.currentTextChanged.connect(lambda _=None, w=mount: self._mount_edited(w))
        fs.currentIndexChanged.connect(lambda _=0: self.changed.emit())
        check.check.toggled.connect(lambda _=False: self.changed.emit())
        self._show_contents(row)
        self._update_note()
        self.changed.emit()
        return row

    def _set_from_partition(self, row: int, partition: Partition) -> None:
        mount = self.table.cellWidget(row, 1)
        fs = self.table.cellWidget(row, 2)
        assert isinstance(mount, QComboBox) and isinstance(fs, QComboBox)
        mount.blockSignals(True)
        mount.setCurrentText("swap" if partition.filesystem == "swap" else partition.mountpoint)
        mount.blockSignals(False)
        fs.blockSignals(True)
        fs.setCurrentIndex(max(0, fs.findData(partition.filesystem)))
        fs.blockSignals(False)

    def remove_selected(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)
            self._update_note()
            self.changed.emit()

    def clear(self) -> None:
        self.table.setRowCount(0)
        self._update_note()

    def _device(self, row: int) -> str:
        combo = self.table.cellWidget(row, 0)
        return str(combo.currentData() or "") if isinstance(combo, QComboBox) else ""

    def rows(self) -> list[Partition]:
        result: list[Partition] = []
        for row in range(self.table.rowCount()):
            mount = self.table.cellWidget(row, 1)
            fs = self.table.cellWidget(row, 2)
            check = self.table.cellWidget(row, 3)
            label = self.table.item(row, 4)
            assert isinstance(mount, QComboBox) and isinstance(fs, QComboBox) and isinstance(check, _Check)
            text = mount.currentText().strip()
            filesystem = str(fs.currentData())
            if text.lower() == "swap":
                filesystem, text = "swap", ""
            elif filesystem in {"swap", "none"}:
                text = ""
            result.append(Partition(
                device=self._device(row), mountpoint=text, filesystem=filesystem,
                format=check.check.isChecked(), label=label.text().strip() if label else "",
            ))
        return result

    def set_rows(self, partitions: list[Partition]) -> None:
        self._loading = True
        try:
            self.table.setRowCount(0)
            for partition in partitions:
                self.add_row(partition)
        finally:
            self._loading = False
        self._update_note()

    # --- user edits --------------------------------------------------------------------------------

    def _row_of(self, widget: QWidget, column: int) -> int:
        return next((row for row in range(self.table.rowCount()) if self.table.cellWidget(row, column) is widget), -1)

    def _device_picked(self, combo: QComboBox) -> None:
        row = self._row_of(combo, 0)
        part = self.info.find_partition(str(combo.currentData() or "")) if self.info and row >= 0 else None
        if part is None or row < 0 or self._loading:
            self._show_contents(row)
            self.changed.emit()
            return
        fs = self.table.cellWidget(row, 2)
        mount = self.table.cellWidget(row, 1)
        check = self.table.cellWidget(row, 3)
        assert isinstance(fs, QComboBox) and isinstance(mount, QComboBox) and isinstance(check, _Check)
        detected = DETECTED.get(part.fstype, "")
        fs.setCurrentIndex(max(0, fs.findData(detected or "ext4")))
        check.check.setChecked(not detected)  # keep data that is already in a usable filesystem
        taken = {self._mount_text(other) for other in range(self.table.rowCount()) if other != row}
        if part.is_esp:
            mount.setCurrentText("/efi")
            fs.setCurrentIndex(fs.findData("vfat"))
            check.check.setChecked(False)
        elif part.is_swap:
            mount.setCurrentText("swap")
        elif not mount.currentText().strip() or mount.currentText().strip() in taken:
            for choice in ("/", "/home", "/boot"):
                if choice not in taken:
                    mount.setCurrentText(choice)
                    break
        self._show_contents(row)
        self.changed.emit()

    def _mount_text(self, row: int) -> str:
        mount = self.table.cellWidget(row, 1)
        return mount.currentText().strip() if isinstance(mount, QComboBox) else ""

    def _mount_edited(self, mount: QComboBox) -> None:
        row = self._row_of(mount, 1)
        fs = self.table.cellWidget(row, 2) if row >= 0 else None
        if isinstance(fs, QComboBox) and mount.currentText().strip().lower() == "swap":
            fs.setCurrentIndex(fs.findData("swap"))
        self.changed.emit()

    def _show_contents(self, row: int) -> None:
        item = self.table.item(row, 5)
        if item is None:
            return
        part = self.info.find_partition(self._device(row)) if self.info and self._device(row) else None
        item.setText("" if part is None else part.describe() + (f" · mounted at {', '.join(part.mountpoints)}" if part.mounted else ""))

    def _update_note(self) -> None:
        disk = self.disk()
        if disk is None:
            self.note.hide()
            return
        used = {partition.device for partition in self.rows()}
        kept = [part for part in disk.partitions if part.type == "part" and part.path not in used]
        windows = [part for part in kept if part.is_windows and part.fstype]
        pieces = []
        if windows:
            pieces.append("Windows partitions stay untouched: " + "; ".join(f"{p.path} ({p.describe()})" for p in windows) + ".")
        if kept:
            pieces.append("Partitions that are not listed above are never changed.")
        self.note.setText(" ".join(pieces))
        self.note.setVisible(bool(pieces))

    # --- helpers ----------------------------------------------------------------------------------------

    def suggest_layout(self, confirm: bool = True) -> bool:
        """Fill the table from what is on the disk. Returns whether it did."""
        disk = self.disk()
        if disk is None:
            return False
        parts = [part for part in disk.partitions if part.type == "part" and not part.mounted]
        esp = next((part for part in parts if part.is_esp), None)
        linux = sorted((part for part in parts if part.fstype in {"ext4", "xfs", "btrfs", "ext3", "ext2"} and not part.is_esp), key=lambda part: -part.size)
        swap = next((part for part in parts if part.is_swap), None)
        rows: list[Partition] = []
        if linux:
            rows.append(Partition(linux[0].path, "/", DETECTED[linux[0].fstype], format=False))
        if esp is not None:
            rows.append(Partition(esp.path, "/efi", "vfat", format=False))
        if swap is not None:
            rows.append(Partition(swap.path, "", "swap", format=False))
        if not rows:
            return False
        if self.row_count() and confirm:
            answer = QMessageBox.question(self, "Replace the table?", "This replaces the rows you entered with a suggestion based on the disk.")
            if answer != QMessageBox.StandardButton.Yes:
                return False
        self.set_rows(rows)
        self.changed.emit()
        return True

    def open_gparted(self) -> bool:
        started = start_detached("gparted", [])
        self.gparted_message.setText("" if started else "GParted could not be started. Prepare the partitions from a terminal (gparted, cfdisk or fdisk) and press Rescan disks.")
        self.gparted_message.setVisible(not started)
        return started

    # --- validation ----------------------------------------------------------------------------------------

    def problems(self, firmware: str) -> list[str]:
        """Everything wrong with the table, in the order a user would fix it."""
        if self.info is None:
            return ["The disks have not been scanned yet."]
        rows = self.rows()
        disk = self.disk()
        if disk is None:
            return ["Select the disk that holds the partitions first."]
        problems: list[str] = []
        if not rows:
            return ["Add the partitions Gentoo should use: at least one for / (root)."]
        seen_devices: set[str] = set()
        seen_mounts: set[str] = set()
        for row in rows:
            if not row.device:
                problems.append("Choose a partition for every row of the table, or remove the empty row.")
                continue
            part = next((p for p in disk.partitions if p.path == row.device), None)
            if part is None:
                elsewhere = self.info.find_partition(row.device)
                if elsewhere is None:
                    problems.append(f"{row.device} was not found on this computer. Choose a partition from the list, or press Rescan disks.")
                    continue
                owner = self.info.find_disk(elsewhere.parent)
                if owner is None or not owner.selectable:
                    reason = owner.unavailable_reason if owner is not None and owner.unavailable_reason else "It cannot be used."
                    problems.append(f"{row.device} is on {elsewhere.parent}, which cannot be used. {reason}")
                    continue
                if not owner.has_identity and not owner.by_path:
                    problems.append(f"{row.device} is on {owner.path}, which has no serial number and no stable hardware path, so the installer cannot recognise it later. Use a disk that has one.")
                    continue
                part = elsewhere  # a partition of another disk is fine: that disk is approved and confirmed separately
            if part.mounted:
                problems.append(f"{row.device} is mounted at {', '.join(part.mountpoints)}. Unmount it before using it.")
            if row.device in seen_devices:
                problems.append(f"{row.device} is used twice.")
            seen_devices.add(row.device)
            if row.mountpoint:
                if row.mountpoint in seen_mounts:
                    problems.append(f"{row.mountpoint} is used by two rows.")
                seen_mounts.add(row.mountpoint)
            if not row.format and row.filesystem not in {"none"}:
                if not part.fstype:
                    problems.append(f"{row.device} has no filesystem yet. Tick Format to create one.")
                elif part.fstype not in COMPATIBLE.get(row.filesystem, set()):
                    problems.append(f"{row.device} is kept (not formatted) but contains {part.fstype}, not {row.filesystem}. Tick Format to replace it, or choose the matching filesystem.")
            if row.mountpoint in EFI_MOUNTS and row.filesystem != "vfat":
                problems.append("The EFI System Partition must be FAT32 (vfat).")
            if row.mountpoint in EFI_MOUNTS and not row.format and part.fstype and part.fstype not in COMPATIBLE["vfat"]:
                problems.append(f"{row.device} contains {part.fstype}, which cannot be used as the EFI System Partition.")
        roots = [row for row in rows if row.mountpoint == "/"]
        if len(roots) != 1:
            problems.append("Exactly one row must be used as / (root).")
        if firmware == "uefi" and not any(row.mountpoint in EFI_MOUNTS for row in rows):
            problems.append("A UEFI installation needs the EFI System Partition: add a row used as /efi.")
        if firmware == "bios" and disk.pttype == "gpt" and not any(part.is_bios_boot for part in disk.partitions):
            problems.append(f"Legacy BIOS boot from the GPT disk {disk.path} needs a BIOS boot partition (type EF02, about 1 MiB). Create one with GParted first, or choose UEFI.")
        return problems
