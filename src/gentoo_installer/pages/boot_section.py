"""BOOT: the "Kernel and boot" card of the System page (kernel, initramfs, bootloader, os-prober, UKI and Secure Boot)."""

from __future__ import annotations

from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from ..manifest import InstallManifest, System
from ..widgets import Card

KERNELS = (
    ("Distribution binary kernel — fastest, recommended", "gentoo-kernel-bin"),
    ("Distribution source kernel — customizable", "gentoo-kernel"),
)


class BootSection(QWidget):
    """Kernel, initramfs and bootloader controls; ``load``/``collect`` move the values between the widgets and a System."""

    def __init__(self, manifest: InstallManifest) -> None:
        super().__init__()
        self.manifest = manifest
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        boot = Card("Kernel and boot")
        boot_form = QFormLayout()
        self.kernel = QComboBox()
        for label, value in KERNELS:
            self.kernel.addItem(label, value)
        boot_form.addRow("Kernel", self.kernel)
        self.initramfs = QComboBox()
        self.initramfs.addItem("Dracut", "dracut")
        self.initramfs.setEnabled(False)
        boot_form.addRow("Initramfs", self.initramfs)
        self.kernel_guidance = QLabel(
            "Gentoo distribution kernels require Dracut, so it is selected for you. Fully custom kernel builds are not available in the graphical installer."
        )
        self.kernel_guidance.setProperty("muted", True)
        self.kernel_guidance.setWordWrap(True)
        boot_form.addRow("", self.kernel_guidance)
        self.bootloader = QComboBox()
        self.bootloader.addItem("GRUB — UEFI and legacy BIOS", "grub")
        self.bootloader.addItem("systemd-boot — UEFI only", "systemd-boot")
        self.bootloader.addItem("EFI stub / UKI — UEFI only", "efi-stub")
        self.bootloader.currentIndexChanged.connect(self._bootloader_changed)
        self.bootloader.activated.connect(self._bootloader_picked)
        boot_form.addRow("Bootloader", self.bootloader)
        self.dual_boot = QCheckBox("Detect other operating systems with os-prober")
        boot_form.addRow(self.dual_boot)
        self.uki = QCheckBox("Build a Unified Kernel Image (UKI)")
        boot_form.addRow(self.uki)
        self.secure_boot = QCheckBox("Sign the boot chain for Secure Boot")
        self.secure_boot.toggled.connect(lambda _=False: self._update_boot_rules())
        self.secure_boot.clicked.connect(self._secure_clicked)
        boot_form.addRow(self.secure_boot)
        self.signing_key = QLineEdit()
        self.signing_key.setPlaceholderText("Absolute path to a private signing key")
        boot_form.addRow("Signing key", self.signing_key)
        self.signing_cert = QLineEdit()
        self.signing_cert.setPlaceholderText("Absolute path to the signing certificate")
        boot_form.addRow("Certificate", self.signing_cert)
        boot.content.addLayout(boot_form)
        layout.addWidget(boot)

    def set_advanced(self, advanced: bool) -> None:
        """Nothing is hidden yet: the owner of this section decides which of its controls are Advanced."""

    def load(self, system: System) -> None:
        self._select(self.kernel, system.kernel)
        self._select(self.initramfs, system.initramfs)
        self.bootloader.setCurrentIndex(max(0, self.bootloader.findData(system.bootloader)))
        self.dual_boot.setChecked(system.dual_boot)
        self.uki.setChecked(system.uki)
        self.secure_boot.setChecked(system.secure_boot)
        self.signing_key.setText(system.secure_boot_key)
        self.signing_cert.setText(system.secure_boot_certificate)
        self._refresh_bootloaders()
        self._update_boot_rules()

    def collect(self, system: System) -> None:
        system.kernel = self.kernel.currentData()
        system.initramfs = self.initramfs.currentData()
        system.bootloader = self.bootloader.currentData()
        system.dual_boot = self.dual_boot.isChecked()
        system.uki = self.uki.isChecked()
        system.secure_boot = self.secure_boot.isChecked()
        system.secure_boot_key = self.signing_key.text().strip() if system.secure_boot else ""
        system.secure_boot_certificate = self.signing_cert.text().strip() if system.secure_boot else ""

    @staticmethod
    def _select(combo: QComboBox, value: str, unavailable: str = "(not available in this installer)") -> None:
        """Select value; a value the combo does not offer (from a loaded manifest)
        is shown as an extra, flagged entry instead of being replaced silently."""
        index = combo.findData(value)
        if index < 0 and value:
            combo.addItem(f"{value} {unavailable}", value)
            index = combo.count() - 1
        combo.setCurrentIndex(max(0, index))

    def _bootloader_changed(self) -> None:
        self._update_boot_rules()

    def _bootloader_picked(self, _index: int = 0) -> None:
        """The user chose a bootloader: what only works with the other kinds is switched off."""
        if self.bootloader.currentData() == "grub":
            self.secure_boot.setChecked(False)  # signing needs a signed image that GRUB does not boot
        else:
            self.dual_boot.setChecked(False)  # os-prober belongs to GRUB
        self._update_boot_rules()

    def _secure_clicked(self, checked: bool) -> None:
        if checked:
            self.uki.setChecked(True)  # the signed boot file is a Unified Kernel Image
        self._update_boot_rules()

    def _update_boot_rules(self) -> None:
        """Dual boot needs GRUB; Secure Boot needs UEFI and systemd-boot or the EFI stub, with a signed UKI.
        Values that were loaded are only explained here, never changed: a box that is ticked stays editable."""
        uefi = self.manifest.storage.firmware == "uefi"
        grub = self.bootloader.currentData() == "grub"
        secure = self.secure_boot.isChecked()
        self.dual_boot.setEnabled(grub)
        self.dual_boot.setToolTip("" if grub else "systemd-boot finds Windows on a shared EFI partition by itself; os-prober is only needed with GRUB.")
        possible = uefi and not grub
        self.secure_boot.setEnabled(possible or secure)
        if possible:
            self.secure_boot.setToolTip("")
        else:
            self.secure_boot.setToolTip("Secure Boot signing needs UEFI firmware." if not uefi else "Secure Boot signing needs systemd-boot or the EFI stub. GRUB cannot be used with it.")
        forced = secure and not grub and self.uki.isChecked()
        self.uki.setEnabled(not forced and (uefi or self.uki.isChecked()))
        self.uki.setToolTip(
            "Secure Boot needs the signed Unified Kernel Image." if forced else "" if uefi else "A Unified Kernel Image needs UEFI firmware.")
        self.signing_key.setEnabled(secure)
        self.signing_cert.setEnabled(secure)

    def _refresh_bootloaders(self) -> None:
        """systemd-boot and EFI stub work on UEFI only: grey them out for a BIOS install."""
        uefi = self.manifest.storage.firmware == "uefi"
        model = self.bootloader.model()
        if isinstance(model, QStandardItemModel):
            for index in range(self.bootloader.count()):
                item = model.item(index)
                if item is not None:
                    item.setEnabled(uefi or self.bootloader.itemData(index) == "grub")
