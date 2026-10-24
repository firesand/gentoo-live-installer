from __future__ import annotations

import dataclasses
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import blockdev, keymaps, layout, recommend, theme
from ..keyboard import start_detached
from ..manifest import InstallManifest
from ..system_info import DiskInfo, SystemInfo, device_from_disk, format_size, holder_reasons_of, smallest_disk_bytes
from ..widgets import Card, ChoiceGroup, FlowLayout, FormGrid, Page, wrapped_label
from .hardware import CAPACITY_CODES, blocking_findings
from .help import AdvancedSection, HelpNote
from .manual_storage import ManualLayoutEditor
from .storage_widgets import ExistingTable, Note, PlanPreview

UEFI_ON_BIOS = (
    "This session was booted in legacy BIOS mode, so a UEFI install cannot register its boot entry. "
    "Choose Legacy BIOS, or reboot the installer in UEFI mode."
)
BIOS_ON_UEFI = "Legacy BIOS on a computer that was started in UEFI mode boots only through the firmware's CSM/legacy mode."
LIVE_MEDIUM_REASON = "This is the medium the installer was started from. It cannot be used as the installation target."
NO_IDENTITY_HINT = "QEMU: -device virtio-blk-pci,drive=d0,serial=MYDISK; libvirt: <serial>MYDISK</serial>"
RAID_HINT = (
    "The installer does not create RAID arrays. Assemble the array first (with mdadm in a terminal), then install onto its /dev/md device "
    "with manual partitions."
)
BTRFS_LABEL = "Btrfs with subvolumes @ and @home"
FILESYSTEMS = (
    ("ext4", "ext4 (default): the long-time Linux standard"),
    ("xfs", "XFS: fast, the Handbook's recommendation"),
    ("btrfs", "Btrfs: snapshots and compression (subvolumes @ and @home)"),
    ("f2fs", "F2FS: made for flash media such as SSDs, SD cards and eMMC"),
)
FILESYSTEM_HELP = {
    "ext4": "ext4: the long-time Linux standard; can be grown and shrunk. It is the default here because it is the simplest to repair. The Handbook recommends XFS for most computers.",
    "xfs": "XFS: fast and the Handbook's recommendation; cannot be made smaller later.",
    "btrfs": "Btrfs: adds snapshots and compression; good if you want to roll back. Separate subvolumes @ and @home are created.",
    "f2fs": "F2FS: designed for flash storage. GRUB cannot read an F2FS root, so a separate /boot partition is created for it.",
}
SWAP_HELP = "Swap is disk space the computer uses when memory runs short; the recommended size follows the Gentoo Handbook for your amount of RAM."
WINDOWS_NOTE = "Windows is installed on this disk. Guided mode will ERASE it, including Windows and its recovery partition."
REMOVABLE_NOTE = (
    "SD cards and USB sticks wear out quickly under a full Linux system; an internal SSD or hard disk is a much better home for Gentoo."
)
XFS_NVME_NOTE = "Some early Intel NVMe drives can corrupt XFS; update the drive firmware or choose ext4."


def with_help(widget: QWidget, text: str, anchor: str | None = None) -> QWidget:
    """``widget`` with a small information button (HelpNote) next to it."""
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(widget, 1)
    row.addWidget(HelpNote(text, anchor))
    return holder
SWAPPED_DISK = "The disk at {path} is not the same disk you selected before (its serial number or hardware path is different). Select a disk again."


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
    reason = f"\n{disk.unavailable_reason}" if disk.unavailable_reason else ""
    return disk.path, f"{disk.path} · {disk.model}", f"{disk.size_text} · {disk.identity_text}{bus}\n{disk.contents_text}{suffix}{reason}", False


def holder_reasons(info: SystemInfo, devices: list[str]) -> list[str]:
    """What is alive on these devices and would be switched off: volumes, swap, volume-group and RAID membership,
    and Btrfs or ZFS pools that go on beyond them (erasing the member destroys data on the other disks too)."""
    roots = info.block_devices()
    reasons: list[str] = []
    for device in devices:
        try:
            node = blockdev.find(roots, device)
        except (LookupError, ValueError):
            continue
        reasons += holder_reasons_of(node)
        reasons += [pool.reason() for pool in blockdev.filesystem_pools(roots, [node])]
    return list(dict.fromkeys(reasons))


class StoragePage(Page):
    rescanRequested = Signal()

    def __init__(self, manifest: InstallManifest, info: SystemInfo):
        super().__init__("Where should Gentoo live?", "Guided mode creates a complete layout. Manual mode lets you assign existing partitions without changing them unless you explicitly enable formatting.")
        self.manifest = manifest
        self.info = info
        self.disks = info.disks
        self.firmware_touched = False
        self.advanced = False
        self._vanished = ""
        self._swapped = False  # the disk that is gone was replaced by another one under the same name
        self._consented_disk = ""  # the disk the consent box below was shown for
        self._extras_written = False
        self._custom_origin = 0  # a loaded swap size that is not a whole number of GiB
        self._loaded_swap: tuple = ()
        self.disk_group = ChoiceGroup([disk_choice(disk) for disk in self.disks], columns=1)
        self.disk_group.setToolTip("Nothing is selected for you: choose the disk Gentoo should be installed on.")
        self.content.addWidget(self.disk_group)
        self.disk_notice = wrapped_label("", muted=True)
        self.disk_notice.hide()
        self.content.addWidget(self.disk_notice)

        self.empty_card = Card("No disk can be used")
        self.empty_text = wrapped_label("", muted=True, selectable=True)
        self.empty_card.content.addWidget(self.empty_text)
        empty_buttons = FlowLayout()
        self.rescan_button = QPushButton("Rescan disks")
        self.rescan_button.clicked.connect(self.rescanRequested)
        empty_buttons.addWidget(self.rescan_button)
        self.gparted_button = QPushButton("Open GParted")
        self.gparted_button.setToolTip("Look at the disks and partitions of this computer, then press Rescan disks.")
        self.gparted_button.clicked.connect(self.open_gparted)
        empty_buttons.addWidget(self.gparted_button)
        self.empty_card.content.addLayout(empty_buttons)
        self.empty_message = wrapped_label("", muted=True)
        self.empty_message.hide()
        self.empty_card.content.addWidget(self.empty_message)
        self.empty_card.hide()
        self.content.addWidget(self.empty_card)

        self.approval = Card("This disk has no serial number")
        self.approval_text = wrapped_label("", muted=True)
        self.approval.content.addWidget(self.approval_text)
        self.approve_path = QCheckBox()
        self.approve_path.setAccessibleName("Approve a disk without a serial number")
        self.approval.content.addWidget(self.approve_path)
        self.approval.hide()
        self.content.addWidget(self.approval)

        self.holders_card = Card("This disk is in use")
        self.holders_text = wrapped_label("", muted=True, selectable=True)
        self.holders_card.content.addWidget(self.holders_text)
        self.release_holders = QCheckBox("Switch these volumes off and erase them together with the disk")
        self.release_holders.setAccessibleName("Switch off and erase the volumes in use")
        self.release_holders.setToolTip("The volumes are deactivated first. Data stored on them, and on other disks of the same volume group, RAID array, Btrfs or ZFS pool, is lost.")
        self.holders_card.content.addWidget(self.release_holders)
        self.holders_card.hide()
        self.content.addWidget(self.holders_card)

        self.choose_hint = wrapped_label("Choose a disk above. Nothing is selected for you, so nothing can be erased by accident.", muted=True)
        self.content.addWidget(self.choose_hint)
        self.existing = ExistingTable()
        self.existing.hide()
        self.content.addWidget(self.existing)
        self.windows_note = Note("error")
        self.content.addWidget(self.windows_note)
        self.removable_note = Note("warning")
        self.content.addWidget(self.removable_note)

        self.mode = QComboBox()
        self.mode.addItem("Erase the whole disk and install Gentoo (all data lost)", "erase")
        self.mode.addItem("Choose partitions myself", "manual")
        self.mode.setAccessibleName("Storage mode")
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.content.addWidget(self.mode)

        self.guided = Card("Guided layout", "Gentoo gets a complete layout on the disk you chose. Existing data on it is erased only after your final confirmation.")
        self.boot_mode_text = wrapped_label("")
        self.guided.content.addWidget(self.boot_mode_text)
        form = QFormLayout()
        self.filesystem = QComboBox()
        self.filesystem.setAccessibleName("Root filesystem")
        self._rebuild_filesystems("ext4")
        self.filesystem.currentIndexChanged.connect(self._filesystem_changed)
        form.addRow("Root filesystem", with_help(self.filesystem, "Pick the filesystem for the system partition. ext4 is the safe default; the Handbook explains the differences.", "Filesystems"))
        self.fs_note = wrapped_label("", muted=True)
        form.addRow("", self.fs_note)
        self.xfs_note = Note("warning")
        form.addRow("", self.xfs_note)
        self.swap_mode = QComboBox()
        self.swap_mode.addItem("Recommended for this computer", "recommended")
        self.swap_mode.addItem("None", "none")
        self.swap_mode.addItem("Custom size", "custom")
        self.swap_mode.setAccessibleName("Swap")
        self.swap_mode.currentIndexChanged.connect(self._swap_mode_changed)
        form.addRow("Swap", with_help(self.swap_mode, SWAP_HELP, "Disks"))
        self.swap = QSpinBox()
        self.swap.setRange(1, 128)
        self.swap.setSuffix(" GiB")
        self.swap.setValue(max(1, round(manifest.storage.swap_mib / 1024)))
        self.swap.setAccessibleName("Custom swap size in GiB")
        self.swap.valueChanged.connect(self._swap_size_edited)
        self.swap_label = QLabel("Custom swap size")
        form.addRow(self.swap_label, self.swap)
        self.swap_note = wrapped_label("", muted=True)
        form.addRow("", self.swap_note)
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
        self.capacity_label = wrapped_label("", selectable=True)
        self.capacity_label.hide()
        self.guided.content.addWidget(self.capacity_label)
        self.content.addWidget(self.guided)

        self.encryption = Card("Encryption")
        self.luks = QCheckBox("Encrypt my disk (asks for a passphrase at every start)")
        self.encryption.content.addWidget(self.luks)
        self.luks_details = QWidget()
        details = QVBoxLayout(self.luks_details)
        details.setContentsMargins(0, 0, 0, 0)
        passphrase_form = FormGrid()
        self.passphrase = QLineEdit()
        self.passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.passphrase.setPlaceholderText("At least 8 characters")
        passphrase_form.add_field(0, "LUKS passphrase", self.passphrase)
        self.passphrase_confirm = QLineEdit()
        self.passphrase_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        passphrase_form.add_field(2, "Repeat passphrase", self.passphrase_confirm)
        details.addLayout(passphrase_form)
        self.show_passphrase = QCheckBox("Show the passphrase")
        self.show_passphrase.toggled.connect(self._show_passphrase)
        details.addWidget(self.show_passphrase)
        self.luks_note = wrapped_label("", muted=True)
        details.addWidget(self.luks_note)
        self.luks_details.hide()
        self.encryption.content.addWidget(self.luks_details)
        self.content.addWidget(self.encryption)

        self.manual = Card("Manual partitions", "Pick the partitions Gentoo should use. Existing data is kept unless Format is ticked. Set the EFI partition to /efi on UEFI. Partitions of other disks can be used too; each such disk has to be confirmed again at the end.")
        self.manual_editor = ManualLayoutEditor()
        self.manual_editor.set_info(info)
        self.partitions = self.manual_editor.table  # the table the page always exposed
        self.manual.content.addWidget(self.manual_editor)
        self.extra_box = QVBoxLayout()
        self.extra_checks: dict[str, QCheckBox] = {}
        self.manual.content.addLayout(self.extra_box)
        self.content.addWidget(self.manual)

        self.preview_card = Card()
        self.preview = PlanPreview()
        self.preview_card.content.addWidget(self.preview)
        self.preview_card.hide()
        self.content.addWidget(self.preview_card)

        # The expert choices: Advanced opens the group, Guided keeps it collapsed. Nothing in it is cleared when the view changes.
        self.advanced_section = AdvancedSection("storage")
        self.firmware = QComboBox()
        self.firmware.addItem("UEFI", "uefi")
        self.firmware.addItem("Legacy BIOS", "bios")
        self.firmware.setCurrentIndex(0 if info.uefi else 1)
        self.firmware.activated.connect(self._firmware_picked)
        self.firmware.setAccessibleName("Firmware")
        self.firmware_note = wrapped_label("", muted=True)
        self.firmware_note.hide()
        shared = QFormLayout()
        shared.addRow("Firmware", with_help(self.firmware, "Almost every computer made after 2012 uses UEFI; the installer detects this for you.", "Disks"))
        shared.addRow("", self.firmware_note)
        self.erase_options = QWidget()
        erase_form = QFormLayout(self.erase_options)
        erase_form.setContentsMargins(0, 0, 0, 0)
        self.table = QComboBox()
        self.table.addItem("GPT (recommended)", "gpt")
        self.table.addItem("DOS / MBR", "dos")
        self.table.setAccessibleName("Partition table")
        erase_form.addRow("Partition table", with_help(self.table, "GPT is the modern way to divide a disk. Choose MBR only for a very old computer whose firmware cannot start from a GPT disk.", "Partitioning"))
        self.swap_kind = QComboBox()
        self.swap_kind.setAccessibleName("Swap type")
        for value in ("auto", "partition", "file", "zram"):
            self.swap_kind.addItem("", value)
        self._label_swap_kinds()
        erase_form.addRow("Swap type", with_help(self.swap_kind, "A small compressed-memory swap (zram) is used first and is much faster than disk swap. A swap file lives inside the system partition and can be resized later.", "Disks"))
        self.hibernate = QCheckBox("Large enough for hibernation")
        self.hibernate.setToolTip("Hibernation writes all memory to swap, so the swap has to be bigger. Switch hibernation itself on under Customize, Hardware.")
        erase_form.addRow(self.hibernate)
        self.separate_boot = QCheckBox("Separate /boot partition (1 GiB)")
        self.separate_boot.setToolTip("Always created for encrypted, LVM and RAID installs. Needed for F2FS with GRUB.")
        erase_form.addRow(self.separate_boot)
        self.boot_noauto = QCheckBox("Mount /boot only when needed (noauto)")
        erase_form.addRow(self.boot_noauto)
        self.boot_noauto_note = wrapped_label("Kernel updates need /boot to be mounted: mount it first (mount /boot), or the new kernel lands in the wrong place.", muted=True)
        erase_form.addRow("", self.boot_noauto_note)
        self.lvm = QCheckBox("Use LVM (volumes that can be resized later)")
        self.lvm.setToolTip("LVM lets you resize or add storage later without repartitioning; most home users do not need it.")
        self.lvm_widget = with_help(self.lvm, "LVM lets you resize or add storage later without repartitioning; most home users do not need it.", "Disks")
        shared.addRow(self.lvm_widget)
        self.fstab_ident = QComboBox()
        self.fstab_ident.setAccessibleName("Refer to filesystems by")
        for text, value in (("Filesystem UUID (default)", "uuid"), ("Label", "label"), ("Partition UUID (GPT only)", "partuuid")):
            self.fstab_ident.addItem(text, value)
        shared.addRow("Refer to filesystems by", with_help(self.fstab_ident, "How /etc/fstab names your disks. The UUID never changes when disks are plugged in a different order. Volumes on LVM or encryption keep the UUID.", "Fstab"))
        self.atime = QComboBox()
        self.atime.setAccessibleName("Access-time updates")
        for text, value in (("noatime (default)", "noatime"), ("lazytime", "lazytime"), ("relatime", "relatime")):
            self.atime.addItem(text, value)
        shared.addRow("Access-time updates", with_help(self.atime, "noatime stops Linux writing a timestamp every time a file is read, which is faster and gentler on SSDs.", "Fstab"))
        advanced_form = QVBoxLayout()
        advanced_form.setContentsMargins(0, 0, 0, 0)
        advanced_form.addLayout(shared)
        advanced_form.addWidget(self.erase_options)
        self.advanced_section.add_layout(advanced_form)
        # The installer never creates software RAID: the combo is for installing onto an array that was assembled
        # beforehand, so it only shows with manual partitions (a loaded value keeps it visible).
        self.raid_widget = QWidget()
        raid_box = QVBoxLayout(self.raid_widget)
        raid_box.setContentsMargins(0, 0, 0, 0)
        raid_row = QHBoxLayout()
        raid_row.addWidget(QLabel("mdadm RAID (advanced)"))
        self.raid = QComboBox()
        for text, value in (("No RAID", ""), ("RAID 0", "0"), ("RAID 1", "1"), ("RAID 5", "5"), ("RAID 6", "6"), ("RAID 10", "10")):
            self.raid.addItem(text, value)
        self.raid.setAccessibleName("RAID level of the assembled array")
        self.raid.currentIndexChanged.connect(self._update_visibility)
        raid_row.addWidget(self.raid)
        raid_row.addStretch()
        raid_box.addLayout(raid_row)
        self.raid_note = wrapped_label(RAID_HINT, muted=True)
        raid_box.addWidget(self.raid_note)
        self.raid_widget.hide()
        self.advanced_section.add_widget(self.raid_widget)
        self.content.addWidget(self.advanced_section)

        self.disk_group.selectionChanged.connect(self._disk_changed)
        self.approve_path.toggled.connect(lambda _=False: self._update_approval_text())
        self.manual_editor.changed.connect(self._manual_changed)
        for signal in (self.swap.valueChanged, self.home_size.valueChanged):
            signal.connect(lambda _=0: self._update_capacity())
        for toggle in (self.home.toggled, self.luks.toggled, self.hibernate.toggled, self.separate_boot.toggled, self.boot_noauto.toggled, self.lvm.toggled):
            toggle.connect(lambda _=False: self._refresh())
        for choice in (self.firmware.currentIndexChanged, self.table.currentIndexChanged, self.mode.currentIndexChanged, self.swap_mode.currentIndexChanged,
                       self.swap_kind.currentIndexChanged, self.filesystem.currentIndexChanged, self.fstab_ident.currentIndexChanged, self.atime.currentIndexChanged):
            choice.connect(lambda _=0: self._refresh())
        self.boot_noauto.toggled.connect(lambda checked: self.separate_boot.setChecked(True) if checked else None)
        self.luks.toggled.connect(lambda _=False: self._update_luks_note())
        self.luks.toggled.connect(lambda _=False: self._luks_changed())
        self.luks.toggled.connect(lambda _=False: self._update_visibility())
        self.release_holders.toggled.connect(lambda _=False: self._update_holders())
        self._mode_changed()
        self.load_manifest()
        self.finish_layout()

    # --- the disk list ---------------------------------------------------------------------------

    def set_info(self, info: SystemInfo) -> None:
        """A new scan (Check again, hot-plugged disk): rebuild the cards and keep
        the selection when the disk is still there."""
        previous = self.disk_group.value()
        self.info = info
        self.disks = info.disks
        survived = self.disk_group.set_choices([disk_choice(disk) for disk in info.disks])
        self._apply_card_states()
        swapped = bool(previous and survived and self._pin_mismatch(info.find_disk(previous)))
        if swapped:
            self._forget_swapped_disk()  # the name is the same, the disk is not: the choice was made for another disk
            survived = False
        wanted = self.manifest.storage.device
        if not previous and wanted in self.disk_group.cards and not self.disk_group.value():
            self.disk_group.set_value(wanted)  # the first scan finished: show the manifest's disk
        elif previous and not survived and info.disks:
            self._vanished, self._swapped = previous, swapped
            self.disk_notice.setText(SWAPPED_DISK.format(path=previous) if swapped else f"The selected disk {previous} is no longer present. Select a disk again.")
            self.disk_notice.show()
        else:
            self._vanished, self._swapped = "", False
            self.disk_notice.hide()
        self.manual_editor.set_info(info)
        if not self.firmware_touched:
            self.firmware.setCurrentIndex(max(0, self.firmware.findData(self.manifest.storage.firmware)))
        if self._swap_state() == self._loaded_swap:
            self._load_swap(self.manifest.storage)  # the swap the detection suggested; a swap the user chose is left alone
        self._disk_changed()
        self._update_firmware_note()
        self._update_empty_state()

    def _apply_card_states(self) -> None:
        for disk in self.disks:
            if disk.live_medium:
                self.disk_group.set_card_enabled(disk.path, False, LIVE_MEDIUM_REASON)
            elif disk.read_only or disk.problem:
                self.disk_group.set_card_enabled(disk.path, False, disk.unavailable_reason or "This disk is read-only and cannot be installed to.")
            elif not disk.has_identity:
                card = self.disk_group.cards.get(disk.path)
                if card is not None:
                    card.setToolTip("This disk has no serial number or WWN. You can still use it after approving it explicitly below.")

    def _update_empty_state(self) -> None:
        """No disk to offer: say what was found instead, and how to look again."""
        usable = [disk for disk in self.disks if disk.selectable]
        waiting = not self.info.inspected
        self.empty_card.setVisible(not usable and not waiting)
        if usable or waiting:
            return
        lines = ["The installer found no disk it can install to."]
        lines += [f"{disk.path} ({disk.model}, {disk.size_text}): {disk.unavailable_reason}" for disk in self.disks if disk.unavailable_reason]
        lines += [f"{item.path} ({item.model}) {item.reason}." for item in self.info.excluded]
        lines.append("If the disk is missing completely, the storage controller may be in a RAID mode (Intel RST) in the firmware settings: switch it to AHCI and start the installer again.")
        self.empty_text.setText("\n".join(lines))

    def open_gparted(self) -> bool:
        started = start_detached("gparted", [])
        self.empty_message.setText("" if started else "GParted could not be started. Look at the disks from a terminal (lsblk, fdisk -l) and press Rescan disks.")
        self.empty_message.setVisible(not started)
        return started

    def selected_disk(self) -> DiskInfo | None:
        path = self.disk_group.value()
        return self.info.find_disk(path) if path else None

    def _pin_mismatch(self, disk: DiskInfo | None) -> bool:
        """Whether the manifest pinned this name to a disk that is not the one there now (the service checks the same pin before it writes)."""
        storage = self.manifest.storage
        if disk is None or not storage.device_id or disk.path != storage.device:
            return False  # nothing was pinned for this disk yet: it is a choice that has not been saved
        return not blockdev.identity_matches(device_from_disk(disk), storage.device_id_kind, storage.device_id, disk.by_path)

    def _forget_swapped_disk(self) -> None:
        """A different disk answers to the name the user chose: drop the choice, its pin and the consents given for the old disk."""
        storage = self.manifest.storage
        storage.device, storage.device_id, storage.device_id_kind = "", "", ""
        self.disk_group.clear_value()
        self._drop_holders_consent()
        self._consented_disk = ""

    def _drop_holders_consent(self) -> None:
        """The consent to switch volumes off was given for one disk; it does not pass to another."""
        self.release_holders.setChecked(False)
        self.manifest.storage.release_holders = False

    def _disk_changed(self) -> None:
        disk = self.selected_disk()
        self.manual_editor.set_disk(disk.path if disk else "")
        if disk is not None:
            self._vanished, self._swapped = "", False
            self.disk_notice.hide()
            if self._consented_disk and disk.path != self._consented_disk:
                self._drop_holders_consent()
            self._consented_disk = disk.path
        self._update_approval()
        self._update_holders()
        self._refresh()
        self._update_visibility()

    def _manual_changed(self) -> None:
        self._update_extra_approvals()
        self._update_holders()

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
        why = ("shares its serial number or WWN with another disk" if disk.identity_shared else "reports neither a serial number nor a WWN (a virtual disk or a USB adapter, for example)")
        self.approval_text.setText(
            f"{disk.path} {why}. "
            f"It will be recognised by its hardware path {disk.by_path}, its size ({disk.size} bytes) and its model, "
            "and the installer refuses to continue if any of them changes. Check that this is really the disk you want.")

    # --- other disks used by manual rows ---------------------------------------------------------------

    def _update_extra_approvals(self) -> None:
        """Every other disk a manual row lives on has to be recognisable later; one without a serial number needs a tick."""
        wanted = [disk for disk in self.manual_editor.extra_disks() if not disk.has_identity and disk.by_path]
        for path in [path for path in self.extra_checks if path not in {disk.path for disk in wanted}]:
            check = self.extra_checks.pop(path)
            self.extra_box.removeWidget(check)
            check.hide()
            check.deleteLater()
        stored = set(self.manifest.storage.additional_device_ids)
        for disk in wanted:
            if disk.path in self.extra_checks:
                continue
            check = QCheckBox(f"I confirm that {disk.path} ({disk.model}, {disk.size_text}) is the disk to use. It has no serial number and is recognised by its hardware path.")
            check.setAccessibleName(f"Approve {disk.path}")
            check.setChecked(disk.path_identity() in stored)
            self.extra_box.addWidget(check)
            self.extra_checks[disk.path] = check

    def additional_approvals(self) -> list[str]:
        """Serial number, WWN or by-path identity of every other disk the manual rows use (the daemon checks each one)."""
        found: list[str] = []
        for disk in self.manual_editor.extra_disks():
            if disk.has_identity:
                value = disk.stable_id
            elif self.extra_checks.get(disk.path) is not None and self.extra_checks[disk.path].isChecked():
                value = disk.path_identity()
            else:
                continue
            if value and value not in found:
                found.append(value)
        return found

    # --- what is in use ------------------------------------------------------------------------------

    def _devices_to_rewrite(self) -> list[str]:
        storage_mode = self.mode.currentData()
        if storage_mode == "erase":
            disk = self.selected_disk()
            return [disk.path] if disk is not None else []
        return [row.device for row in self.manual_editor.rows() if row.device and row.format and row.filesystem != "none"]

    def holders(self) -> list[str]:
        return holder_reasons(self.info, self._devices_to_rewrite())

    def _update_holders(self) -> None:
        reasons = self.holders()
        shown = bool(reasons)
        self.holders_card.setVisible(shown)
        if shown:
            self.holders_text.setText("Active at the moment: " + "; ".join(reasons) + ".\nThe installer can switch them off, but their data is lost.")

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
        self._update_visibility()
        self._update_holders()

    def set_advanced(self, advanced: bool) -> None:
        self.advanced = advanced
        self.advanced_section.set_advanced(advanced)
        self._rebuild_filesystems(self.filesystem.currentData() or "ext4")
        self._update_visibility()

    def _update_visibility(self) -> None:
        """What each mode shows: the RAID setting is for experts installing onto an assembled array (manual partitions only; a loaded value
        keeps it visible); the swap, partition table and /boot options belong to the guided layout."""
        manual = self.mode.currentData() == "manual"
        self.raid_widget.setVisible(manual or bool(self.raid.currentData()))
        self.erase_options.setVisible(not manual)
        self._update_disk_state()

    def _has_advanced_values(self) -> bool:
        """Whether the loaded settings use something the Advanced group holds, so it opens by itself and the user sees what is in use."""
        storage = self.manifest.storage
        return bool(
            storage.separate_boot or storage.boot_noauto or storage.lvm or storage.raid_level or storage.swap_kind not in {"auto", "partition"}
            or storage.swap_choice == "hibernate" or storage.fstab_ident != "uuid" or storage.atime != "noatime" or storage.filesystem == "f2fs"
            or storage.table != "gpt" or storage.firmware != ("uefi" if self.info.uefi else "bios"))

    # --- the choices of the guided layout ---------------------------------------------------------------

    def _rebuild_filesystems(self, keep: str) -> None:
        """XFS, ext4 and Btrfs always; F2FS (flash media) only in Advanced or while it is the chosen one."""
        self.filesystem.blockSignals(True)
        self.filesystem.clear()
        for value, text in FILESYSTEMS:
            if value != "f2fs" or self.advanced or keep == "f2fs":
                self.filesystem.addItem(text, value)
        self.filesystem.setCurrentIndex(max(0, self.filesystem.findData(keep)))
        self.filesystem.blockSignals(False)
        self._update_filesystem_note()

    def _filesystem_changed(self, _index: int = 0) -> None:
        self._update_filesystem_note()

    def _update_filesystem_note(self) -> None:
        if not hasattr(self, "fs_note"):
            return
        value = self.filesystem.currentData() or "ext4"
        self.fs_note.setText(FILESYSTEM_HELP.get(value, ""))
        disk = self.selected_disk()
        risky = value == "xfs" and disk is not None and layout.xfs_risky_model(disk.model)
        self.xfs_note.show_text(XFS_NVME_NOTE if risky else "")

    def _label_swap_kinds(self) -> None:
        encrypted = self.luks.isChecked() if hasattr(self, "luks") else False
        texts = {
            "auto": "File inside the root (required with encryption)" if encrypted else "Partition (default; a file when the disk is encrypted)",
            "partition": "Partition",
            "file": "File inside the root",
            "zram": "Compressed RAM only (zram)",
        }
        for index in range(self.swap_kind.count()):
            self.swap_kind.setItemText(index, texts[self.swap_kind.itemData(index)])

    def _luks_changed(self) -> None:
        """An encrypted disk keeps its swap inside the encrypted root: the plain partition choice is off. The passphrase fields show while it is on."""
        encrypted = self.luks.isChecked()
        self.luks_details.setVisible(encrypted)
        self._label_swap_kinds()
        model = self.swap_kind.model()
        item = model.item(self.swap_kind.findData("partition")) if hasattr(model, "item") else None
        if item is not None:
            item.setEnabled(not encrypted)
        if encrypted and self.swap_kind.currentData() == "partition":
            self.swap_kind.setCurrentIndex(self.swap_kind.findData("auto"))

    def _swap_mode_changed(self, _index: int = 0) -> None:
        self._update_swap_widgets()

    def _swap_size_edited(self, _value: int = 0) -> None:
        self._custom_origin = 0  # a size typed by hand replaces the loaded one
        if self.swap_mode.currentData() != "custom":
            self.swap_mode.setCurrentIndex(self.swap_mode.findData("custom"))

    def _memory_kib(self) -> int:
        kib = self.info.profile.memory.total_kib
        return kib or int(self.info.memory_gib * 1024 * 1024)

    def _recommended_mib(self, hibernate: bool | None = None) -> int:
        kib = self._memory_kib()
        wanted = self.hibernate.isChecked() if hibernate is None else hibernate
        if kib <= 0:
            return self.manifest.storage.swap_mib
        return recommend.recommend_swap_mib(kib, hibernate=wanted and recommend.hibernation_advised(kib))

    def _custom_mib(self) -> int:
        gib = self.swap.value()
        if self._custom_origin and max(1, round(self._custom_origin / 1024)) == gib:
            return self._custom_origin  # a size that is not a whole number of GiB, loaded from a file, stays as it was
        return gib * 1024

    def _effective_swap_mib(self) -> int:
        """The size of the swap on the disk, as the daemon will create it."""
        mode = self.swap_mode.currentData()
        if mode == "none" or self.swap_kind.currentData() == "zram":
            return 0
        return self._custom_mib() if mode == "custom" else self._recommended_mib()

    def _swap_choice(self) -> str:
        mode = self.swap_mode.currentData()
        return "hibernate" if mode == "recommended" and self.hibernate.isChecked() and self._hibernation_advised() else mode

    def _hibernation_advised(self) -> bool:
        kib = self._memory_kib()
        return kib <= 0 or recommend.hibernation_advised(kib)

    def _update_swap_widgets(self) -> None:
        advised = self._hibernation_advised()
        self.hibernate.setEnabled(advised)
        if not advised:
            self.hibernate.setToolTip(f"The Gentoo Handbook does not advise hibernation above {recommend.HIBERNATION_MAX_GIB} GiB of memory: the swap would have to be as large as the memory.")
        mib = self._recommended_mib()
        self.swap_mode.setItemText(self.swap_mode.findData("recommended"), f"Recommended for this computer ({mib / 1024:g} GiB)")
        custom = self.swap_mode.currentData() == "custom"
        self.swap.setVisible(custom)
        self.swap_label.setVisible(custom)
        zram = self.swap_kind.currentData() == "zram"
        if zram:
            note = "No swap partition or file is created. A compressed part of the memory is used as swap instead."
        elif self.swap_mode.currentData() == "none":
            note = "Without swap the computer cannot move rarely used memory to the disk, and hibernation does not work."
        else:
            note = SWAP_HELP
        self.swap_note.setText(note)

    def _update_disk_state(self) -> None:
        """No disk chosen yet: the layout is greyed out and an inline hint says what to do (nothing is preselected, for safety)."""
        disk = self.selected_disk()
        chosen = disk is not None
        self.choose_hint.setVisible(not chosen)
        for widget in (self.guided, self.encryption, self.advanced_section):
            widget.setEnabled(chosen)
        self.existing.set_disk(disk)
        self.existing.setVisible(chosen)
        erase = self.mode.currentData() == "erase"
        windows = chosen and erase and any(part.is_windows for part in disk.partitions)
        self.windows_note.show_text(WINDOWS_NOTE if windows else "")
        flash = chosen and (disk.removable or disk.path.startswith("/dev/mmcblk"))
        self.removable_note.show_text(REMOVABLE_NOTE if flash else "")
        self._update_filesystem_note()

    def _boot_mode_sentence(self) -> str:
        firmware, table = self.firmware.currentData(), self.table.currentData()
        detected = "UEFI" if self.info.uefi else "BIOS"
        if firmware == ("uefi" if self.info.uefi else "bios"):
            start = f"This computer started in {detected} mode, so Gentoo will boot with {detected}."
        else:
            start = f"You chose {'UEFI' if firmware == 'uefi' else 'legacy BIOS'} in the Advanced options, although this computer started in {detected} mode."
        name = "GPT" if table == "gpt" else "DOS (MBR)"
        extra = " with a small EFI system partition" if firmware == "uefi" else " with a small BIOS boot partition that lets GRUB start" if table == "gpt" else ""
        return f"{start} The disk gets a {name} partition table{extra}."

    def _refresh(self) -> None:
        """Everything that follows from the choices: swap labels, notes, the capacity text and the table of what will be created."""
        self.boot_noauto_note.setVisible(self.boot_noauto.isChecked())
        self.boot_mode_text.setText(self._boot_mode_sentence())
        self._update_swap_widgets()
        self._update_filesystem_note()
        self._update_capacity()
        self._update_preview()

    def _update_preview(self) -> None:
        disk = self.selected_disk()
        if disk is None or self.mode.currentData() != "erase":
            self.preview_card.hide()
            return
        storage = self._planned_storage()
        manifest = dataclasses.replace(self.manifest, storage=storage)
        try:
            plan = layout.plan_storage(manifest, self.info.block_devices(), memory_kib=self._memory_kib())
        except Exception:  # noqa: BLE001 - a preview must never break the page
            self.preview_card.hide()
            return
        self.preview.set_rows(layout.plan_table(plan.disks[0], lvm=storage.lvm))
        self.preview_card.show()

    def _home_changed(self, checked: bool) -> None:
        self.home_size.setEnabled(checked)

    def _show_passphrase(self, shown: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        self.passphrase.setEchoMode(mode)
        self.passphrase_confirm.setEchoMode(mode)

    def add_partition(self) -> None:
        self.manual_editor.add_row()

    # --- messages that follow the choices ----------------------------------------------------------------

    def _update_luks_note(self) -> None:
        """Tell which keyboard layout the passphrase prompt will use, because it is typed here with the live layout."""
        system = self.manifest.system
        derived = keymaps.console_keymap_for(system.keyboard_layout, system.keyboard_variant)
        keymap = system.keymap if system.keymap not in {"", "auto"} else derived
        if keymap != "us" and keymap == derived:
            note = (f"You will type this passphrase at every start, before the desktop is loaded, with the {keymap} keyboard layout. "
                    "That is the layout chosen on the first page, which is also active here.")
        elif keymap != derived and keymap != "us":
            note = ("The console keyboard layout and the layout chosen on the first page differ, so the start-up prompt uses a US layout. "
                    "Use characters that are the same on your keyboard and on a US layout.")
        else:
            note = ("You will type this passphrase at every start, before the desktop is loaded, with a US keyboard layout. "
                    "Use characters that are the same on your keyboard layout and on a US layout.")
        self.luks_note.setText(note)

    def _planned_storage(self) -> Any:
        """The storage choices as they stand in the widgets (the manifest is only written when the page is left)."""
        return dataclasses.replace(
            self.manifest.storage, mode=str(self.mode.currentData()), device=self.disk_group.value() or self.manifest.storage.device,
            firmware=str(self.firmware.currentData()), table=str(self.table.currentData()),
            filesystem=str(self.filesystem.currentData()), swap_mib=self._effective_swap_mib(), swap_choice=self._swap_choice(),
            swap_kind=str(self.swap_kind.currentData()), separate_home=self.home.isChecked(),
            home_mib=self.home_size.value() if self.home.isChecked() else 0, luks=self.luks.isChecked(), lvm=self.lvm.isChecked(),
            separate_boot=self.separate_boot.isChecked(), boot_noauto=self.boot_noauto.isChecked(),
            fstab_ident=str(self.fstab_ident.currentData()), atime=str(self.atime.currentData()))

    def _update_capacity(self) -> None:
        """'The system partition will get 41 GiB': what is left for Gentoo after the other partitions of the guided layout."""
        disk = self.selected_disk()
        if disk is None or self.mode.currentData() != "erase" or disk.size <= 0:
            self.capacity_label.hide()
            return
        storage = self._planned_storage()
        manifest = dataclasses.replace(self.manifest, storage=storage)
        plan = layout.guided_plan(storage, disk.size)
        errors, warnings = layout.capacity_problems(manifest, disk.size, disk.log_sec)
        desktop = "a minimal system" if manifest.system.desktop == "minimal" else manifest.system.desktop.upper()
        text = (f"The system will get {max(plan.usable_root_mib, 0) / 1024:.1f} GiB. {desktop.capitalize() if desktop.startswith('a ') else desktop} needs at least "
                f"{layout.minimum_root_mib(manifest) / 1024:.0f} GiB, and {layout.recommended_root_mib(manifest) / 1024:.0f} GiB or more is recommended.")
        floor = layout.MIN_ROOT_MIB["minimal"]
        status = "error" if plan.usable_root_mib < floor else "warning" if errors or warnings else "ok"
        if status == "error":
            text += " This is too little for any installation: use a larger disk, or lower the swap size or the /home size."
        elif errors:
            text += " That is too little for this desktop: choose a lighter desktop on the next steps, or use a larger disk."
        self.capacity_label.setText(text)
        self.capacity_label.setStyleSheet(f"color: {theme.status_color(status)};" if status != "ok" else "")
        self.capacity_label.show()

    # --- manifest -----------------------------------------------------------------------------------------

    def load_manifest(self) -> None:
        storage = self.manifest.storage
        if storage.device and storage.device in self.disk_group.cards:
            self.disk_group.set_value(storage.device)
        self.mode.setCurrentIndex(max(0, self.mode.findData(storage.mode)))
        self.firmware.setCurrentIndex(max(0, self.firmware.findData(storage.firmware)))
        self.table.setCurrentIndex(max(0, self.table.findData(storage.table)))
        self._rebuild_filesystems(storage.filesystem)
        self._load_swap(storage)
        self.home.setChecked(storage.separate_home)
        self.home_size.setValue(storage.home_mib or 16384)
        self.luks.setChecked(storage.luks)
        self.passphrase.setText(storage.luks_passphrase)
        self.passphrase_confirm.setText(storage.luks_passphrase)
        self.lvm.setChecked(storage.lvm)
        self.separate_boot.setChecked(storage.separate_boot)
        self.boot_noauto.setChecked(storage.boot_noauto)
        self.fstab_ident.setCurrentIndex(max(0, self.fstab_ident.findData(storage.fstab_ident)))
        self.atime.setCurrentIndex(max(0, self.atime.findData(storage.atime)))
        self.raid.setCurrentIndex(max(0, self.raid.findData(storage.raid_level)))
        self.release_holders.setChecked(storage.release_holders)
        self.manual_editor.set_rows(storage.partitions)
        self._luks_changed()
        self._mode_changed()
        self._update_approval()
        disk = self.selected_disk()
        if disk is not None and not disk.has_identity and storage.device_id_kind == "path":
            self.approve_path.setChecked(bool(storage.device_id) and storage.device_id == disk.path_identity())
        self._update_extra_approvals()
        self._update_firmware_note()
        self._update_luks_note()
        self._update_holders()
        self._refresh()
        self._update_empty_state()
        if self._has_advanced_values():
            self.advanced_section.set_open(True)

    def _load_swap(self, storage: Any) -> None:
        """Show the stored swap choice: Recommended, None or a custom size, the swap type and the hibernation box."""
        for widget in (self.swap_mode, self.swap, self.swap_kind, self.hibernate):
            widget.blockSignals(True)
        choice = storage.swap_choice
        if storage.swap_kind == "none" or (choice == "custom" and storage.swap_mib == 0):
            choice = "none"
        self.swap_mode.setCurrentIndex(self.swap_mode.findData("recommended" if choice == "hibernate" else choice))
        self.hibernate.setChecked(choice == "hibernate")
        self._custom_origin = storage.swap_mib if choice == "custom" else 0
        self.swap.setValue(min(128, max(1, round(storage.swap_mib / 1024))) if storage.swap_mib else 4)
        self.swap_kind.setCurrentIndex(max(0, self.swap_kind.findData(storage.swap_kind)))
        for widget in (self.swap_mode, self.swap, self.swap_kind, self.hibernate):
            widget.blockSignals(False)
        self._update_swap_widgets()
        self._loaded_swap = self._swap_state()

    def _swap_state(self) -> tuple:
        """The swap choices as they stand (the size only for a custom one): what tells a swap the user changed from one that was loaded."""
        choice = self._swap_choice()
        return (choice, self.swap_kind.currentData(), self._custom_mib() if choice == "custom" else 0)

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
        # The size written is the size the disk gets, so the review, the text installer and the service all show the same number.
        storage.swap_mib = self._effective_swap_mib()
        storage.swap_choice = self._swap_choice()
        storage.swap_kind = self.swap_kind.currentData()
        storage.separate_home = self.home.isChecked()
        storage.home_mib = self.home_size.value() if self.home.isChecked() else 0
        storage.luks = self.luks.isChecked()
        storage.luks_passphrase = self.passphrase.text() if storage.luks else ""
        storage.lvm = self.lvm.isChecked()
        storage.separate_boot = self.separate_boot.isChecked()
        storage.boot_noauto = self.boot_noauto.isChecked()
        storage.fstab_ident = self.fstab_ident.currentData()
        storage.atime = self.atime.currentData()
        storage.raid_level = self.raid.currentData()
        storage.partitions = self.manual_editor.rows()
        if self.holders_card.isVisibleTo(self):
            storage.release_holders = self.release_holders.isChecked()
        self._save_extra_disks(storage)

    def _save_extra_disks(self, storage: Any) -> None:
        """Manual rows on other disks: write their identities (erase mode keeps what it had, unless this page wrote it)."""
        if storage.mode != "manual":
            if self._extras_written:
                storage.additional_device_ids = []
                self._extras_written = False
            return
        wanted = self.additional_approvals()
        if all(item in storage.additional_device_ids for item in wanted):
            return  # the loaded approvals already cover every disk in use
        storage.additional_device_ids = wanted
        self._extras_written = True

    def validate_page(self) -> tuple[bool, str]:
        self.save_manifest()
        storage = self.manifest.storage
        if not self.disk_group.value():
            if self._vanished:
                if self._swapped:
                    return False, SWAPPED_DISK.format(path=self._vanished)
                return False, f"The disk you selected ({self._vanished}) is no longer present. Select a disk again."
            return False, "Select the disk you want to use."
        disk = self.selected_disk()
        if disk is None:
            return False, f"The selected disk {self.disk_group.value()} is no longer present. Press “Check again” on the readiness page and select a disk."
        if disk.live_medium:
            return False, LIVE_MEDIUM_REASON
        if disk.read_only:
            return False, "The selected disk is read-only."
        if disk.problem:
            return False, disk.unavailable_reason
        if storage.mode == "erase" and disk.mounted:
            return False, "The selected disk has mounted partitions. Unmount them before erasing it."
        smallest = smallest_disk_bytes(self.manifest)
        if storage.mode == "erase" and disk.size < smallest:
            return False, f"{disk.path} is only {disk.size_text}. The smallest possible installation needs a disk of at least {format_size(smallest)}."
        if not disk.has_identity:
            if not disk.by_path:
                return False, f"{disk.path} has no serial number and no stable hardware path, so it cannot be identified safely. {NO_IDENTITY_HINT}"
            if not self.approve_path.isChecked():
                return False, f"{disk.path} has no serial number or WWN. Tick the confirmation box to approve it explicitly, or choose another disk."
        if storage.firmware == "uefi" and not self.info.uefi:
            return False, UEFI_ON_BIOS
        if storage.luks and self.passphrase.text() != self.passphrase_confirm.text():
            return False, "The LUKS passphrases do not match."
        if storage.mode == "erase":
            problem = layout.swap_kind_problem(self._planned_storage())
            if problem:
                return False, problem
            problems = layout.table_problems(storage, disk.size, disk.log_sec)
            if problems:
                return False, problems[0]
            if storage.fstab_ident == "partuuid" and storage.table == "dos":
                return False, "Naming filesystems by partition UUID needs a GPT partition table. Choose the filesystem UUID or the label."
            if storage.boot_noauto and not layout.has_boot_partition(storage):
                return False, "Mounting /boot only when needed needs a /boot partition of its own: tick “Separate /boot partition”."
        raid = storage.raid_problem()
        if raid:
            return False, raid
        if storage.mode == "manual":
            problems = self.manual_editor.problems(storage.firmware)
            if problems:
                return False, problems[0]
            for path, check in self.extra_checks.items():
                if not check.isChecked():
                    return False, f"{path} has no serial number or WWN. Tick the confirmation box under the partition list to approve it explicitly, or use another disk."
        # What the detection knows about this disk and firmware (4K sectors with legacy BIOS, a DOS table that is too small, ...).
        # The room for the chosen desktop is judged later, when the desktop is known.
        known = blocking_findings(self.manifest, self.info.hardware, "storage", skip=CAPACITY_CODES)
        if known:
            return False, known[0]
        reasons = self.holders()
        if reasons and not storage.release_holders:
            return False, ("Something on the disk is still in use (" + "; ".join(reasons) + "). Tick “Switch these volumes off and erase them together with the disk” "
                           "to continue, or choose another disk.")
        errors = self.manifest.validate_sections()["storage"]
        return (False, errors[0]) if errors else (True, "")
