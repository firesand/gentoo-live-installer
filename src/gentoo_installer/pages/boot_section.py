"""BOOT: the "Kernel" and "Boot loader" cards of the System page (kernel, initramfs, boot loader, dual boot, UKI, Secure Boot)."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QVBoxLayout, QWidget

from ..manifest import Hardware, InstallManifest, System
from ..widgets import Card, ChoiceGroup, wrapped_label
from .help import AdvancedSection, HelpNote

KERNELS = (
    ("Gentoo kernel, ready-made (about 5 min)", "gentoo-kernel-bin"),
    ("Gentoo kernel, built on your PC (slow, allows custom settings)", "gentoo-kernel"),
)
KERNEL_HELP = {
    "gentoo-kernel-bin": "Downloaded ready-made and installed in minutes. Updates itself with the rest of the system.",
    "gentoo-kernel": "Compiled on this PC, which takes a long time. Choose it if you want to add your own kernel settings.",
}
KERNEL_GUIDANCE = "Gentoo’s own kernel is the Handbook’s recommended start and updates automatically."
INITRAMFS_LINE = "Initramfs: Dracut (required by Gentoo kernels, automatic)"

BOOTLOADERS = (
    ("grub", "GRUB", "Works on every PC, old (BIOS) and new (UEFI). The safe default.", True),
    ("systemd-boot", "systemd-boot", "A small, simple UEFI boot menu.", False),
    ("efi-stub", "EFI stub (no boot menu)", "UEFI only. May not work on every computer, so keep a rescue USB.", False),
)
UEFI_ONLY = ("systemd-boot", "efi-stub")
BIOS_REASON = "This PC starts in BIOS (legacy) mode, and this boot loader needs UEFI. GRUB works here."

SECURE_BOOT_ON_NOTE = ("Secure Boot is on in this PC’s firmware. Either turn it off in the firmware screen (easiest), "
                       "or tick Secure Boot below and let the installer sign the boot files.")
SECURE_BOOT_CONFIRM = ("Secure Boot makes the computer start only boot files signed with your key; you must enroll the key in the firmware once. "
                       "Updates are signed automatically.")
SECURE_BOOT_CONFIRM_MISSING = "Tick the box that confirms you understand how Secure Boot works, or turn Secure Boot off."
SECURE_BOOT_NVIDIA = ("Secure Boot cannot be used with the NVIDIA proprietary driver yet: its kernel modules are not signed, so the firmware would refuse "
                      "to load them and the screen would stay black. Use the open driver (Hardware tab) or turn Secure Boot off.")
SECURE_MODES = (
    ("create", "Create a signing key for me (recommended)"),
    ("own-key", "Use my own key"),
    ("shim", "Use shim (Handbook method) - not available yet"),
)
NOT_AVAILABLE = "(not available in this installer)"


def detect_other_systems(info: Any, manifest: InstallManifest) -> list[str]:
    """Other operating systems on this PC that an install will not erase: ``["Windows on /dev/nvme0n1p3", ...]``.

    Windows is recognised by its entry in the firmware boot menu and by NTFS partitions on disks other than the one that is erased."""
    found: list[str] = []
    if info is None:
        return found
    storage = manifest.storage
    erased = storage.device if storage.mode == "erase" else ""
    for disk in getattr(info, "disks", []):
        if getattr(disk, "live_medium", False) or disk.path == erased:
            continue
        for partition in disk.partitions:
            if partition.fstype.lower() in {"ntfs", "bitlocker"} or partition.fstype == "BitLocker":
                found.append(f"Windows on {partition.path}")
    try:
        windows_entry = bool(info.profile.firmware.windows_boot_entry)
    except Exception:  # noqa: BLE001 - a probe that failed knows nothing; that is not an error here
        windows_entry = False
    if windows_entry and not any(item.startswith("Windows") for item in found):
        found.insert(0, "Windows (Boot Manager)")
    return found


def firmware_secure_boot(info: Any) -> str:
    """The firmware's Secure Boot state: enabled | disabled | setup | unsupported | unknown | n/a ("" without a probe)."""
    try:
        return str(info.profile.firmware.secure_boot)
    except Exception:  # noqa: BLE001
        return ""


class BootSection(QWidget):
    """Kernel and boot loader controls; ``load``/``collect`` move the values between the widgets and a System."""

    def __init__(self, manifest: InstallManifest) -> None:
        super().__init__()
        self.manifest = manifest
        self.info: Any = None
        self._loading = 0
        self._dual_boot_touched = False
        self._fallback_touched = False
        self._others: list[str] = []
        self._advanced = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        # --- kernel -------------------------------------------------------------------------------------------------
        kernel_card = Card("Kernel")
        self.kernel = ChoiceGroup([(value, label, KERNEL_HELP[value], value == "gentoo-kernel-bin") for label, value in KERNELS], columns=1)
        self.kernel.selectionChanged.connect(lambda _key: self._kernel_changed())
        kernel_card.content.addWidget(self.kernel)
        self.kernel_guidance = wrapped_label(KERNEL_GUIDANCE, muted=True)
        kernel_card.content.addWidget(self.kernel_guidance)
        self.initramfs_line = wrapped_label(INITRAMFS_LINE, muted=True)
        kernel_card.content.addWidget(self.initramfs_line)
        layout.addWidget(kernel_card)

        # --- boot loader --------------------------------------------------------------------------------------------
        boot = Card("Boot loader")
        self.bootloader = ChoiceGroup(list(BOOTLOADERS), columns=1)
        self.bootloader.selectionChanged.connect(self._bootloader_selected)
        boot.content.addWidget(self.bootloader)
        self.bootloader_note = wrapped_label("", muted=True)
        self.bootloader_note.hide()
        boot.content.addWidget(self.bootloader_note)
        self.firmware_note = wrapped_label("", muted=True)
        self.firmware_note.hide()
        boot.content.addWidget(self.firmware_note)
        self.dual_boot_found = wrapped_label("", muted=True)
        self.dual_boot_found.hide()
        boot.content.addWidget(self.dual_boot_found)
        self.dual_boot = QCheckBox("Add other operating systems to the boot menu (os-prober)")
        self.dual_boot.setToolTip("This makes the boot menu list Windows or other systems found on your disks so you can pick one at start-up.")
        self.dual_boot.clicked.connect(lambda _=False: setattr(self, "_dual_boot_touched", True))
        self.dual_boot.toggled.connect(lambda _=False: self._update_boot_rules())
        dual_row = QHBoxLayout()
        dual_row.addWidget(self.dual_boot)
        dual_row.addWidget(HelpNote("This makes the boot menu list Windows or other systems found on your disks so you can pick one at start-up.", "Bootloader"))
        dual_row.addStretch()
        boot.content.addLayout(dual_row)
        self.fallback = QCheckBox("Also make the disk bootable on any PC (fallback boot file)")
        self.fallback.clicked.connect(lambda _=False: setattr(self, "_fallback_touched", True))
        boot.content.addWidget(self.fallback)
        self.uki = QCheckBox("Build a Unified Kernel Image (UKI)")
        self.uki.setToolTip("A single file that contains the kernel, startup files and options; needed for Secure Boot, optional otherwise.")
        self.uki.toggled.connect(lambda _=False: self._update_boot_rules())
        uki_row = QHBoxLayout()
        uki_row.addWidget(self.uki)
        uki_row.addWidget(HelpNote("A single file that contains the kernel, startup files and options; needed for Secure Boot, optional otherwise. "
                                   "The previous image is kept as gentoo-old.efi for a rollback.", "Kernel"))
        uki_row.addStretch()
        self.uki_row = QWidget()
        self.uki_row.setLayout(uki_row)
        uki_row.setContentsMargins(0, 0, 0, 0)
        boot.content.addWidget(self.uki_row)
        self.secure_boot = QCheckBox("Secure Boot: start only boot files signed with my key")
        self.secure_boot.toggled.connect(lambda _=False: self._update_boot_rules())
        self.secure_boot.clicked.connect(self._secure_clicked)
        boot.content.addWidget(self.secure_boot)

        self.secure_frame = QWidget()
        secure_layout = QFormLayout(self.secure_frame)
        secure_layout.setContentsMargins(24, 0, 0, 0)
        self.secure_mode = QComboBox()
        for value, label in SECURE_MODES:
            self.secure_mode.addItem(label, value)
        shim_item = self.secure_mode.model().item(self.secure_mode.findData("shim"))
        if shim_item is not None:
            shim_item.setEnabled(False)
        self.secure_mode.currentIndexChanged.connect(lambda _=0: self._update_boot_rules())
        secure_layout.addRow("Signing key", self.secure_mode)
        self.signing_key = QLineEdit()
        self.signing_key.setPlaceholderText("Absolute path to a private signing key")
        self.signing_key_label = QLabel("Key file")
        secure_layout.addRow(self.signing_key_label, self.signing_key)
        self.signing_cert = QLineEdit()
        self.signing_cert.setPlaceholderText("Absolute path to the signing certificate")
        self.signing_cert_label = QLabel("Certificate")
        secure_layout.addRow(self.signing_cert_label, self.signing_cert)
        self.secure_nvidia_note = wrapped_label(SECURE_BOOT_NVIDIA)
        self.secure_nvidia_note.hide()
        secure_layout.addRow(self.secure_nvidia_note)
        self.secure_note = wrapped_label(
            "The key stays on the installed system, readable by root only, so every kernel update is signed again. "
            "The binary kernel’s modules are signed by Gentoo; modules you add later need your own key.", muted=True)
        secure_layout.addRow(self.secure_note)
        self.secure_confirm = QCheckBox(SECURE_BOOT_CONFIRM)
        secure_layout.addRow(self.secure_confirm)
        boot.content.addWidget(self.secure_frame)
        layout.addWidget(boot)

        # --- more options ------------------------------------------------------------------------------------------
        self.more = AdvancedSection("system-boot", "More kernel and boot options")
        form = QFormLayout()
        self.initramfs = QComboBox()
        self.initramfs.addItem("Dracut", "dracut")
        self.initramfs.addItem("ugrd (experimental) - not available yet", "ugrd")
        self.initramfs.addItem("None (manual kernel with a plain root) - not available yet", "none")
        for value in ("ugrd", "none"):
            item = self.initramfs.model().item(self.initramfs.findData(value))
            if item is not None:
                item.setEnabled(False)
        form.addRow("Initramfs", self.initramfs)
        self.extra_options = QLineEdit()
        self.extra_options.setPlaceholderText("quiet")
        self.extra_options.setAccessibleName("Extra startup options")
        extra_row = QHBoxLayout()
        extra_row.addWidget(self.extra_options, 1)
        extra_row.addWidget(HelpNote("Add kernel options here only if a guide tells you to, for example nomodeset when the screen stays black.", "Kernel"))
        form.addRow("Extra startup options (optional)", extra_row)
        self.serial_console = QLineEdit()
        self.serial_console.setPlaceholderText("ttyS0,115200")
        self.serial_console.setToolTip("Sends the boot messages to a serial port and starts a login prompt there. Leave it empty on a normal PC.")
        form.addRow("Serial console (optional)", self.serial_console)
        self.no_bootloader = QCheckBox("Install no boot loader (I will set it up myself)")
        self.no_bootloader.toggled.connect(lambda _=False: self._update_boot_rules())
        form.addRow(self.no_bootloader)
        self.snippet_label = QLabel("Kernel config snippet")
        self.config_snippet = QPlainTextEdit()
        self.config_snippet.setPlaceholderText("CONFIG_HZ_1000=y")
        self.config_snippet.setFixedHeight(90)
        self.config_snippet.setToolTip("One CONFIG_NAME=y per line; merged into Gentoo’s standard kernel configuration. Only used when the kernel is built on this PC.")
        form.addRow(self.snippet_label, self.config_snippet)
        self.more.add_layout(form)
        layout.addWidget(self.more)
        self.kernel.set_value("gentoo-kernel-bin")
        self.bootloader.set_value("grub")
        self._update_boot_rules()

    # --- the detail level ---------------------------------------------------------------------------------------------

    def set_advanced(self, advanced: bool) -> None:
        self.more.set_advanced(advanced)
        self._advanced = advanced
        self._update_boot_rules()

    # --- what the probe found -------------------------------------------------------------------------------------------

    def set_info(self, info: Any) -> None:
        """The inspection result: other systems on the disks (dual boot) and the firmware's Secure Boot state."""
        self.info = info
        self._others = detect_other_systems(info, self.manifest)
        if self._others and not self._dual_boot_touched and not self.dual_boot.isChecked():
            self.dual_boot.setChecked(True)  # found Windows or another system: the boot menu lists it unless the user says no
        secure = firmware_secure_boot(info)
        self.firmware_note.setText(SECURE_BOOT_ON_NOTE if secure == "enabled" else "")
        self.firmware_note.setVisible(secure == "enabled")
        self._update_boot_rules()

    # --- manifest -----------------------------------------------------------------------------------------------------

    def load(self, system: System, hardware: Hardware | None = None) -> None:
        self._loading += 1
        try:
            self._load_kernel(system.kernel)
            self._select_combo(self.initramfs, system.initramfs)
            self.no_bootloader.setChecked(system.bootloader == "none")
            self.bootloader.set_value(system.bootloader if system.bootloader in {key for key, *_ in BOOTLOADERS} else "grub")
            self.dual_boot.setChecked(system.dual_boot)
            self.uki.setChecked(system.uki)
            self.secure_boot.setChecked(system.secure_boot)
            self._select_combo(self.secure_mode, system.secure_boot_mode if system.secure_boot_mode != "off" else "create")
            self.signing_key.setText(system.secure_boot_key)
            self.signing_cert.setText(system.secure_boot_certificate)
            self.secure_confirm.setChecked(system.secure_boot)  # a saved setup was confirmed when it was made
            self.extra_options.setText(system.kernel_cmdline_extra)
            self.serial_console.setText(system.serial_console)
            self.config_snippet.setPlainText(system.kernel_config_snippet)
            if hardware is not None:
                self.fallback.setChecked(hardware.removable_fallback == "always")
                self._fallback_touched = False
        finally:
            self._loading -= 1
        self._refresh_bootloaders()
        self._update_boot_rules()
        if self._others and not self._dual_boot_touched and not self.dual_boot.isChecked():
            self.dual_boot.setChecked(True)

    def collect(self, system: System) -> None:
        system.kernel = self.kernel.value() or system.kernel
        system.initramfs = self.initramfs.currentData()
        system.bootloader = "none" if self.no_bootloader.isChecked() else (self.bootloader.value() or "grub")
        system.dual_boot = self.dual_boot.isChecked()
        system.uki = self.uki.isChecked()
        secure = self.secure_boot.isChecked()
        system.secure_boot_mode = self.secure_mode.currentData() if secure else "off"
        own = secure and system.secure_boot_mode == "own-key"
        system.secure_boot_key = self.signing_key.text().strip() if own else ""
        system.secure_boot_certificate = self.signing_cert.text().strip() if own else ""
        system.kernel_cmdline_extra = " ".join(self.extra_options.text().split())
        system.serial_console = self.serial_console.text().strip()
        if system.kernel == "gentoo-kernel":  # the lines belong to the kernel that is built here; the box keeps them while it is hidden
            system.kernel_config_snippet = self.config_snippet.toPlainText().strip("\n")
        else:
            system.kernel_config_snippet = system.kernel_config_snippet or self.config_snippet.toPlainText().strip("\n")

    def collect_hardware(self, hardware: Hardware) -> None:
        """The fallback boot file is a hardware setting; it is only written when the user used the box."""
        if self._fallback_touched:
            hardware.removable_fallback = "always" if self.fallback.isChecked() else "auto"

    def problem(self, system: System) -> str:
        """Why the boot choices cannot be installed ('' when they can); the System page shows it before the user moves on."""
        if system.secure_boot:
            if not self.secure_confirm.isChecked():
                return SECURE_BOOT_CONFIRM_MISSING
            if self.manifest.hardware.gpu_driver == "nvidia":
                return SECURE_BOOT_NVIDIA
            if system.secure_boot_mode == "own-key" and (not system.secure_boot_key or not system.secure_boot_certificate):
                return "Provide both Secure Boot signing files, choose “Create a signing key for me”, or turn Secure Boot off."
        return ""

    def _load_kernel(self, value: str) -> None:
        """Select value; a kernel the cards do not offer (a loaded manual or custom one) is shown as an extra, flagged card, never replaced."""
        known = {key for _, key in KERNELS}
        choices = [(key, label, KERNEL_HELP[key], key == "gentoo-kernel-bin") for label, key in KERNELS]
        if value and value not in known:
            choices.append((value, f"{value} {NOT_AVAILABLE}", "Choose a Gentoo kernel to continue.", False))
        self.kernel.set_choices(choices)
        self.kernel.set_value(value if value else "gentoo-kernel-bin")

    @staticmethod
    def _select_combo(combo: QComboBox, value: str, unavailable: str = NOT_AVAILABLE) -> None:
        """Select value; a value the combo does not offer (from a loaded manifest) is shown as an extra, flagged entry, never replaced silently."""
        index = combo.findData(value)
        if index < 0 and value:
            combo.addItem(f"{value} {unavailable}", value)
            index = combo.count() - 1
        combo.setCurrentIndex(max(0, index))

    # --- rules --------------------------------------------------------------------------------------------------------

    def _kernel_changed(self) -> None:
        self._update_boot_rules()

    def _bootloader_selected(self, key: str) -> None:
        """The user chose a boot loader: what only works with the other kinds is switched off, and the EFI stub starts with its fallback image on."""
        if self._loading:
            self._update_boot_rules()
            return
        if key == "grub":
            self.secure_boot.setChecked(False)  # signing needs a signed image that GRUB does not boot
        else:
            self.dual_boot.setChecked(False)  # os-prober belongs to GRUB
        if key == "efi-stub":
            self.uki.setChecked(True)  # the EFI stub has no menu; the image is its fallback
        self._update_boot_rules()

    def _secure_clicked(self, checked: bool) -> None:
        if checked:
            self.uki.setChecked(True)  # the signed boot file is a Unified Kernel Image
        self._update_boot_rules()

    def _update_boot_rules(self) -> None:
        """Dual boot needs GRUB; Secure Boot needs UEFI and systemd-boot or the EFI stub, with a signed UKI.
        Values that were loaded are only explained here, never changed: a box that is ticked stays editable."""
        uefi = self.manifest.storage.firmware == "uefi"
        none = self.no_bootloader.isChecked()
        grub = self.bootloader.value() in {"grub", ""} and not none
        secure = self.secure_boot.isChecked()
        self.bootloader.setEnabled(not none)
        self.dual_boot.setEnabled(grub)
        self.dual_boot.setToolTip(
            "This makes the boot menu list Windows or other systems found on your disks so you can pick one at start-up." if grub
            else "systemd-boot finds Windows on a shared EFI partition by itself; os-prober is only needed with GRUB.")
        found = [item for item in self._others]
        self.dual_boot_found.setText("Found: " + ", ".join(found) + (" - it will be added to the boot menu." if grub else ""))
        self.dual_boot_found.setVisible(bool(found))
        self.dual_boot.setVisible(grub or self.dual_boot.isChecked() or self._advanced)
        possible = uefi and not grub and not none
        self.secure_boot.setEnabled(possible or secure)
        if possible:
            self.secure_boot.setToolTip("")
        elif none:
            self.secure_boot.setToolTip("Secure Boot signing needs a boot loader that the installer sets up.")
        else:
            self.secure_boot.setToolTip("Secure Boot signing needs UEFI firmware." if not uefi else "Secure Boot signing needs systemd-boot or the EFI stub. GRUB cannot be used with it.")
        forced = secure and not grub and self.uki.isChecked()
        self.uki.setEnabled(not forced and (uefi or self.uki.isChecked()))
        self.uki.setToolTip("Secure Boot needs the signed Unified Kernel Image." if forced else
                            "" if uefi else "A Unified Kernel Image needs UEFI firmware.")
        self.uki_row.setVisible(self._advanced or not grub or self.uki.isChecked())
        own_key = secure and self.secure_mode.currentData() == "own-key"
        self.secure_frame.setVisible(secure)
        self.secure_mode.setEnabled(secure)
        self.signing_key.setEnabled(own_key)
        self.signing_cert.setEnabled(own_key)
        for widget in (self.signing_key, self.signing_key_label, self.signing_cert, self.signing_cert_label):
            widget.setVisible(own_key)
        self.secure_nvidia_note.setVisible(secure and self.manifest.hardware.gpu_driver == "nvidia")
        self.config_snippet.setVisible(self.kernel.value() == "gentoo-kernel")
        self.snippet_label.setVisible(self.kernel.value() == "gentoo-kernel")
        self.fallback.setVisible(uefi and not none)
        reasons = [] if uefi else [BIOS_REASON]
        self.bootloader_note.setText(" ".join(reasons))
        self.bootloader_note.setVisible(bool(reasons))

    def _refresh_bootloaders(self) -> None:
        """systemd-boot and the EFI stub work on UEFI only: grey them out, with the reason, for a BIOS install."""
        uefi = self.manifest.storage.firmware == "uefi"
        for key in UEFI_ONLY:
            self.bootloader.set_card_enabled(key, uefi, "" if uefi else BIOS_REASON)
        self._update_boot_rules()
