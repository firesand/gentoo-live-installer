from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ..manifest import InstallManifest, Partition
from ..system_info import SystemInfo
from ..widgets import Card, ChoiceGroup, FormGrid, Page


class StoragePage(Page):
    def __init__(self, manifest: InstallManifest, info: SystemInfo):
        super().__init__("Where should Gentoo live?", "Guided mode creates a complete layout. Manual mode lets you assign existing partitions without changing them unless you explicitly enable formatting.")
        self.manifest = manifest
        self.info = info
        self.disks = info.disks
        self.disk_group = ChoiceGroup([], columns=1)
        choices = []
        for disk in self.disks:
            flags = []
            if disk.mounted:
                flags.append("mounted")
            if disk.removable:
                flags.append("removable")
            if disk.read_only:
                flags.append("read-only")
            suffix = f" · {', '.join(flags)}" if flags else ""
            choices.append((disk.path, f"{disk.path} · {disk.model}", f"{disk.size_text}{suffix}", False))
        self.disk_group.deleteLater()
        self.disk_group = ChoiceGroup(choices, columns=1)
        self.content.addWidget(self.disk_group)

        self.mode = QComboBox()
        self.mode.addItem("Guided — erase the selected disk", "erase")
        self.mode.addItem("Manual — assign partitions and mountpoints", "manual")
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.content.addWidget(self.mode)

        self.guided = Card("Guided layout", "The installer creates an EFI or BIOS boot partition, optional swap, and a Gentoo root partition. Existing data on the selected disk will be erased only after your final confirmation.")
        form = QFormLayout()
        self.firmware = QComboBox()
        self.firmware.addItem("UEFI", "uefi")
        self.firmware.addItem("Legacy BIOS", "bios")
        self.firmware.setCurrentIndex(0 if info.uefi else 1)
        form.addRow("Firmware", self.firmware)
        self.table = QComboBox()
        self.table.addItem("GPT (recommended)", "gpt")
        self.table.addItem("DOS / MBR", "dos")
        form.addRow("Partition table", self.table)
        self.filesystem = QComboBox()
        for name, value in (("ext4 — reliable default", "ext4"), ("XFS — large filesystems", "xfs"), ("Btrfs — snapshots and subvolumes", "btrfs")):
            self.filesystem.addItem(name, value)
        form.addRow("Root filesystem", self.filesystem)
        self.swap = QSpinBox()
        self.swap.setRange(0, 131072)
        self.swap.setSuffix(" MiB")
        self.swap.setValue(manifest.storage.swap_mib)
        form.addRow("Swap size (0 disables)", self.swap)
        self.home = QCheckBox("Create a separate /home partition")
        self.home.toggled.connect(self._home_changed)
        form.addRow(self.home)
        self.home_size = QSpinBox()
        self.home_size.setRange(4096, 4194304)
        self.home_size.setSuffix(" MiB")
        self.home_size.setValue(max(16384, manifest.storage.home_mib))
        self.home_size.setEnabled(False)
        form.addRow("Home partition size", self.home_size)
        self.guided.content.addLayout(form)
        self.content.addWidget(self.guided)

        self.encryption = Card("Encryption and advanced storage")
        self.luks = QCheckBox("Encrypt the Gentoo root with LUKS2")
        self.encryption.content.addWidget(self.luks)
        passphrase_form = FormGrid()
        self.passphrase = QLineEdit()
        self.passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.passphrase.setPlaceholderText("At least 8 characters")
        passphrase_form.add_field(0, "LUKS passphrase", self.passphrase)
        self.passphrase_confirm = QLineEdit()
        self.passphrase_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        passphrase_form.add_field(2, "Repeat passphrase", self.passphrase_confirm)
        self.encryption.content.addLayout(passphrase_form)
        self.lvm = QCheckBox("Use LVM inside the encrypted/root storage")
        self.encryption.content.addWidget(self.lvm)
        raid_row = QHBoxLayout()
        raid_row.addWidget(QLabel("mdadm RAID (advanced)"))
        self.raid = QComboBox()
        for text, value in (("No RAID", ""), ("RAID 0", "0"), ("RAID 1", "1"), ("RAID 5", "5"), ("RAID 6", "6"), ("RAID 10", "10")):
            self.raid.addItem(text, value)
        raid_row.addWidget(self.raid)
        raid_row.addStretch()
        self.encryption.content.addLayout(raid_row)
        self.content.addWidget(self.encryption)

        self.manual = Card("Manual partitions", "Use one row per partition. Set the EFI mountpoint to /efi on UEFI. Filesystems marked ‘no’ for formatting are preserved.")
        self.partitions = QTableWidget(0, 5)
        self.partitions.setHorizontalHeaderLabels(["Device", "Mountpoint", "Filesystem", "Format (yes/no)", "Label"])
        self.partitions.horizontalHeader().setStretchLastSection(True)
        self.partitions.setMinimumHeight(220)
        self.manual.content.addWidget(self.partitions)
        row = QHBoxLayout()
        add = QPushButton("Add partition")
        add.clicked.connect(self.add_partition)
        remove = QPushButton("Remove selected row")
        remove.clicked.connect(self._remove_partition)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch()
        self.manual.content.addLayout(row)
        self.content.addWidget(self.manual)
        self._mode_changed()
        self.load_manifest()

    def _mode_changed(self) -> None:
        manual = self.mode.currentData() == "manual"
        self.manual.setVisible(manual)
        self.guided.setVisible(not manual)

    def _home_changed(self, checked: bool) -> None:
        self.home_size.setEnabled(checked)

    def add_partition(self) -> None:
        row = self.partitions.rowCount()
        self.partitions.insertRow(row)
        values = ["/dev/", "/" if row == 0 else "", "ext4", "no", ""]
        for column, value in enumerate(values):
            self.partitions.setItem(row, column, QTableWidgetItem(value))

    def _remove_partition(self) -> None:
        row = self.partitions.currentRow()
        if row >= 0:
            self.partitions.removeRow(row)

    def load_manifest(self) -> None:
        storage = self.manifest.storage
        if storage.device:
            self.disk_group.set_value(storage.device)
        self.mode.setCurrentIndex(max(0, self.mode.findData(storage.mode)))
        self.firmware.setCurrentIndex(max(0, self.firmware.findData(storage.firmware)))
        self.table.setCurrentIndex(max(0, self.table.findData(storage.table)))
        self.filesystem.setCurrentIndex(max(0, self.filesystem.findData(storage.filesystem)))
        self.swap.setValue(storage.swap_mib)
        self.home.setChecked(storage.separate_home)
        self.home_size.setValue(storage.home_mib or 16384)
        self.luks.setChecked(storage.luks)
        self.passphrase.setText(storage.luks_passphrase)
        self.passphrase_confirm.setText(storage.luks_passphrase)
        self.lvm.setChecked(storage.lvm)
        self.raid.setCurrentIndex(max(0, self.raid.findData(storage.raid_level)))
        self.partitions.setRowCount(0)
        for partition in storage.partitions:
            self.add_partition()
            row = self.partitions.rowCount() - 1
            for column, value in enumerate((partition.device, partition.mountpoint, partition.filesystem, "yes" if partition.format else "no", partition.label)):
                self.partitions.setItem(row, column, QTableWidgetItem(value))
        self._mode_changed()

    def save_manifest(self) -> None:
        storage = self.manifest.storage
        storage.device = self.disk_group.value()
        disk = next((item for item in self.disks if item.path == storage.device), None)
        storage.device_id = (disk.wwid or disk.serial) if disk else ""
        storage.mode = self.mode.currentData()
        storage.firmware = self.firmware.currentData()
        storage.table = self.table.currentData()
        storage.filesystem = self.filesystem.currentData()
        storage.swap_mib = self.swap.value()
        storage.separate_home = self.home.isChecked()
        storage.home_mib = self.home_size.value() if self.home.isChecked() else 0
        storage.luks = self.luks.isChecked()
        storage.luks_passphrase = self.passphrase.text() if storage.luks else ""
        storage.lvm = self.lvm.isChecked()
        storage.raid_level = self.raid.currentData()
        storage.partitions = []
        for row in range(self.partitions.rowCount()):
            def text(column: int) -> str:
                item = self.partitions.item(row, column)
                return item.text().strip() if item else ""
            storage.partitions.append(Partition(
                device=text(0), mountpoint=text(1), filesystem=text(2),
                format=text(3).lower() in {"yes", "true", "1"}, label=text(4),
            ))

    def validate_page(self) -> tuple[bool, str]:
        self.save_manifest()
        storage = self.manifest.storage
        if not storage.device:
            return False, "Select the disk you want to use."
        disk = next((item for item in self.disks if item.path == storage.device), None)
        if disk and disk.read_only:
            return False, "The selected disk is read-only."
        if storage.mode == "erase" and disk and disk.mounted:
            return False, "The selected disk has mounted partitions. Unmount them before erasing it."
        if storage.luks and self.passphrase.text() != self.passphrase_confirm.text():
            return False, "The LUKS passphrases do not match."
        errors = [error for error in self.manifest.validate() if any(word in error.lower() for word in ("disk", "storage", "partition", "luks", "raid", "filesystem", "efi", "initramfs"))]
        return (False, errors[0]) if errors else (True, "")
