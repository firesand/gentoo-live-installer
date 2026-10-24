from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QWidget,
)

from .. import theme
from ..manifest import InstallManifest
from ..system_info import MIN_DISK_BYTES, DiskInfo, SystemInfo
from ..widgets import Card, ChoiceGroup, FormGrid, Page, wrapped_label
from .manual_storage import ManualLayoutEditor

UEFI_ON_BIOS = (
    "This session was booted in legacy BIOS mode, so a UEFI install cannot register its boot entry. "
    "Choose Legacy BIOS, or reboot the installer in UEFI mode."
)
BIOS_ON_UEFI = "Legacy BIOS on a computer that was started in UEFI mode boots only through the firmware's CSM/legacy mode."
LIVE_MEDIUM_REASON = "This is the medium the installer was started from. It cannot be used as the installation target."
NO_IDENTITY_HINT = "QEMU: -device virtio-blk-pci,drive=d0,serial=MYDISK; libvirt: <serial>MYDISK</serial>"


def card_flags(disk: DiskInfo) -> list[str]:
    flags = []
    if disk.live_medium:
        flags.append("live installer medium")
    elif disk.mounted:
        flags.append("mounted")
    if disk.removable:
        flags.append("removable")
    if disk.read_only:
        flags.append("read-only")
    if not disk.has_identity:
        flags.append("approval needed")
    return flags


def disk_choice(disk: DiskInfo) -> tuple[str, str, str, bool]:
    """(key, title, description, recommended) of a disk card: what the disk is
    and what is on it today."""
    bus = f" · {disk.bus_text}" if disk.bus_text else ""
    flags = card_flags(disk)
    suffix = f" · {', '.join(flags)}" if flags else ""
    return disk.path, f"{disk.path} · {disk.model}", f"{disk.size_text} · {disk.identity_text}{bus}\n{disk.contents_text}{suffix}", False


class StoragePage(Page):
    def __init__(self, manifest: InstallManifest, info: SystemInfo):
        super().__init__("Where should Gentoo live?", "Guided mode creates a complete layout. Manual mode lets you assign existing partitions without changing them unless you explicitly enable formatting.")
        self.manifest = manifest
        self.info = info
        self.disks = info.disks
        self.firmware_touched = False
        self._vanished = ""
        self.disk_group = ChoiceGroup([disk_choice(disk) for disk in self.disks], columns=1)
        self.content.addWidget(self.disk_group)
        self.disk_notice = wrapped_label("", muted=True)
        self.disk_notice.hide()
        self.content.addWidget(self.disk_notice)

        self.approval = Card("This disk has no serial number")
        self.approval_text = wrapped_label("", muted=True)
        self.approval.content.addWidget(self.approval_text)
        self.approve_path = QCheckBox()
        self.approve_path.setAccessibleName("Approve a disk without a serial number")
        self.approval.content.addWidget(self.approve_path)
        self.approval.hide()
        self.content.addWidget(self.approval)

        self.mode = QComboBox()
        self.mode.addItem("Guided — erase the selected disk", "erase")
        self.mode.addItem("Manual — assign partitions and mountpoints", "manual")
        self.mode.setAccessibleName("Storage mode")
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.content.addWidget(self.mode)

        self.guided = Card("Guided layout", "The installer creates an EFI or BIOS boot partition, optional swap, and a Gentoo root partition. Existing data on the selected disk will be erased only after your final confirmation.")
        form = QFormLayout()
        self.firmware = QComboBox()
        self.firmware.addItem("UEFI", "uefi")
        self.firmware.addItem("Legacy BIOS", "bios")
        self.firmware.setCurrentIndex(0 if info.uefi else 1)
        self.firmware.activated.connect(self._firmware_picked)
        form.addRow("Firmware", self.firmware)
        self.firmware_note = wrapped_label("", muted=True)
        self.firmware_note.hide()
        form.addRow("", self.firmware_note)
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
        self.show_passphrase = QCheckBox("Show the passphrase")
        self.show_passphrase.toggled.connect(self._show_passphrase)
        self.encryption.content.addWidget(self.show_passphrase)
        self.encryption.content.addWidget(wrapped_label(
            "You will type this passphrase at every start, before the desktop is loaded. Use characters that are the same on your keyboard layout and on a US layout.", muted=True))
        self.lvm = QCheckBox("Use LVM inside the encrypted/root storage")
        self.encryption.content.addWidget(self.lvm)
        # Software RAID cannot be created by the installer yet: the combo stays
        # (a loaded manifest keeps its value) but is not offered.
        self.raid_widget = QWidget()
        raid_row = QHBoxLayout(self.raid_widget)
        raid_row.addWidget(QLabel("mdadm RAID (advanced)"))
        self.raid = QComboBox()
        for text, value in (("No RAID", ""), ("RAID 0", "0"), ("RAID 1", "1"), ("RAID 5", "5"), ("RAID 6", "6"), ("RAID 10", "10")):
            self.raid.addItem(text, value)
        raid_row.addWidget(self.raid)
        raid_row.addStretch()
        self.raid_widget.hide()
        self.encryption.content.addWidget(self.raid_widget)
        self.content.addWidget(self.encryption)

        self.manual = Card("Manual partitions", "Pick the partitions Gentoo should use from the disk selected above. Existing data is kept unless Format is ticked. Set the EFI partition to /efi on UEFI. For other layouts, prepare the partitions with GParted first.")
        self.manual_editor = ManualLayoutEditor()
        self.manual_editor.set_info(info)
        self.partitions = self.manual_editor.table  # the table the page always exposed
        self.manual.content.addWidget(self.manual_editor)
        self.content.addWidget(self.manual)
        self.disk_group.selectionChanged.connect(self._disk_changed)
        self.approve_path.toggled.connect(lambda _=False: self._update_approval_text())
        self._mode_changed()
        self.load_manifest()

    # --- the disk list ---------------------------------------------------------------------------

    def set_info(self, info: SystemInfo) -> None:
        """A new scan (Check again, hot-plugged disk): rebuild the cards and keep
        the selection when the disk is still there."""
        previous = self.disk_group.value()
        self.info = info
        self.disks = info.disks
        survived = self.disk_group.set_choices([disk_choice(disk) for disk in info.disks])
        self._apply_card_states()
        wanted = self.manifest.storage.device
        if not previous and wanted in self.disk_group.cards and not self.disk_group.value():
            self.disk_group.set_value(wanted)  # the first scan finished: show the manifest's disk
        elif previous and not survived and info.disks:
            self._vanished = previous
            self.disk_notice.setText(f"The selected disk {previous} is no longer present. Select a disk again.")
            self.disk_notice.show()
        else:
            self._vanished = ""
            self.disk_notice.hide()
        self.manual_editor.set_info(info)
        if not self.firmware_touched:
            self.firmware.setCurrentIndex(max(0, self.firmware.findData(self.manifest.storage.firmware)))
        self._disk_changed()
        self._update_firmware_note()

    def _apply_card_states(self) -> None:
        for disk in self.disks:
            if disk.live_medium:
                self.disk_group.set_card_enabled(disk.path, False, LIVE_MEDIUM_REASON)
            elif disk.read_only:
                self.disk_group.set_card_enabled(disk.path, False, "This disk is read-only and cannot be installed to.")
            elif not disk.has_identity:
                card = self.disk_group.cards.get(disk.path)
                if card is not None:
                    card.setToolTip("This disk has no serial number or WWN. You can still use it after approving it explicitly below.")

    def selected_disk(self) -> DiskInfo | None:
        path = self.disk_group.value()
        return self.info.find_disk(path) if path else None

    def _disk_changed(self) -> None:
        disk = self.selected_disk()
        self.manual_editor.set_disk(disk.path if disk else "")
        if disk is not None:
            self._vanished = ""
            self.disk_notice.hide()
        self._update_approval()

    # --- disks without serial or WWN ---------------------------------------------------------------

    def _update_approval(self) -> None:
        disk = self.selected_disk()
        if disk is None or disk.has_identity:
            self.approval.hide()
            self.approve_path.blockSignals(True)
            self.approve_path.setChecked(False)
            self.approve_path.blockSignals(False)
            return
        self.approval.show()
        if disk.by_path:
            self.approve_path.show()
            self.approve_path.setText(f"I confirm that {disk.path} ({disk.model}, {disk.size_text}) is the disk to use")
            self._update_approval_text()
        else:
            self.approve_path.hide()
            self.approval_text.setText(
                f"{disk.path} has no serial number and no stable hardware path, so the installer cannot make sure it is the same disk later. {NO_IDENTITY_HINT}")

    def _update_approval_text(self) -> None:
        disk = self.selected_disk()
        if disk is None or disk.has_identity or not disk.by_path:
            return
        self.approval_text.setText(
            f"{disk.path} reports neither a serial number nor a WWN (a virtual disk or a USB adapter, for example). "
            f"It will be recognised by its hardware path {disk.by_path}, its size ({disk.size} bytes) and its model, "
            "and the installer refuses to continue if any of them changes. Check that this is really the disk you want.")

    # --- firmware ------------------------------------------------------------------------------------

    def _firmware_picked(self, _index: int = 0) -> None:
        self.firmware_touched = True
        self._update_firmware_note()

    def _update_firmware_note(self) -> None:
        chosen = self.firmware.currentData()
        if chosen == "uefi" and not self.info.uefi:
            self.firmware_note.setText(UEFI_ON_BIOS)
            self.firmware_note.setStyleSheet(f"color: {theme.status_color('error')};")
        elif chosen == "bios" and self.info.uefi:
            self.firmware_note.setText(BIOS_ON_UEFI)
            self.firmware_note.setStyleSheet("")
        else:
            self.firmware_note.setText("")
        self.firmware_note.setVisible(bool(self.firmware_note.text()))

    def _mode_changed(self) -> None:
        manual = self.mode.currentData() == "manual"
        self.manual.setVisible(manual)
        self.guided.setVisible(not manual)

    def _home_changed(self, checked: bool) -> None:
        self.home_size.setEnabled(checked)

    def _show_passphrase(self, shown: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        self.passphrase.setEchoMode(mode)
        self.passphrase_confirm.setEchoMode(mode)

    def add_partition(self) -> None:
        self.manual_editor.add_row()

    # --- manifest -----------------------------------------------------------------------------------------

    def load_manifest(self) -> None:
        storage = self.manifest.storage
        if storage.device and storage.device in self.disk_group.cards:
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
        self.manual_editor.set_rows(storage.partitions)
        self._mode_changed()
        self._update_approval()
        disk = self.selected_disk()
        if disk is not None and not disk.has_identity and storage.device_id_kind == "path":
            self.approve_path.setChecked(bool(storage.device_id) and storage.device_id == disk.path_identity())
        self._update_firmware_note()

    def save_manifest(self) -> None:
        storage = self.manifest.storage
        selected = self.disk_group.value()
        if selected:
            storage.device = selected
            disk = self.info.find_disk(selected)
            if disk is not None:
                if disk.has_identity:
                    storage.device_id, storage.device_id_kind = disk.stable_id, ""
                elif self.approve_path.isChecked() and disk.path_identity():
                    storage.device_id, storage.device_id_kind = disk.path_identity(), "path"
                else:
                    storage.device_id, storage.device_id_kind = "", ""
            else:
                storage.device_id, storage.device_id_kind = "", ""
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
        storage.partitions = self.manual_editor.rows()

    def validate_page(self) -> tuple[bool, str]:
        self.save_manifest()
        storage = self.manifest.storage
        if not self.disk_group.value():
            if self._vanished:
                return False, f"The disk you selected ({self._vanished}) is no longer present. Select a disk again."
            return False, "Select the disk you want to use."
        disk = self.selected_disk()
        if disk is None:
            return False, f"The selected disk {self.disk_group.value()} is no longer present. Press “Check again” on the readiness page and select a disk."
        if disk.live_medium:
            return False, LIVE_MEDIUM_REASON
        if disk.read_only:
            return False, "The selected disk is read-only."
        if storage.mode == "erase" and disk.mounted:
            return False, "The selected disk has mounted partitions. Unmount them before erasing it."
        if storage.mode == "erase" and disk.size < MIN_DISK_BYTES:
            return False, f"{disk.path} is only {disk.size_text}. A Gentoo installation needs a disk of at least 16 GiB."
        if not disk.has_identity:
            if not disk.by_path:
                return False, f"{disk.path} has no serial number and no stable hardware path, so it cannot be identified safely. {NO_IDENTITY_HINT}"
            if not self.approve_path.isChecked():
                return False, f"{disk.path} has no serial number or WWN. Tick the confirmation box to approve it explicitly, or choose another disk."
        if storage.firmware == "uefi" and not self.info.uefi:
            return False, UEFI_ON_BIOS
        if storage.luks and self.passphrase.text() != self.passphrase_confirm.text():
            return False, "The LUKS passphrases do not match."
        if storage.raid_level:
            return False, "Software RAID cannot be created by the installer yet. Prepare the array yourself and use manual mode with partitions of the assembled /dev/md device."
        if storage.mode == "manual":
            problems = self.manual_editor.problems(storage.firmware)
            if problems:
                return False, problems[0]
        errors = self.manifest.validate_sections()["storage"]
        return (False, errors[0]) if errors else (True, "")
