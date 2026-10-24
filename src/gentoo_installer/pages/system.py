from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton

from ..manifest import InstallManifest
from ..widgets import Card, ChoiceGroup, Page


PROFILE_BASE = "default/linux/amd64/23.0"


def profile_path(init: str, desktop: str, *, multilib: bool, hardened: bool = False) -> str:
    """Return a published 23.0 profile for the selected init and profile family."""
    path = PROFILE_BASE
    if hardened:
        path += "/no-multilib/hardened" if not multilib else "/hardened"
    elif not multilib:
        # The pinned tree has no desktop/no-multilib profiles. Desktop package
        # selection remains independent, while this chooses the valid ABI profile.
        path += "/no-multilib"
    elif desktop == "kde":
        path += "/desktop/plasma"
    elif desktop == "gnome":
        path += "/desktop/gnome"
    elif desktop == "xfce":
        path += "/desktop"
    if init == "systemd":
        path += "/systemd"
    return path


class SystemPage(Page):
    def __init__(self, manifest: InstallManifest):
        super().__init__("Choose your Gentoo foundation", "These choices determine the stage3, profile, kernel, boot process, and desktop. Recommended choices work well for a first installation.")
        self.manifest = manifest
        self.init_group = ChoiceGroup([
            ("openrc", "OpenRC", "Gentoo’s lightweight, traditional init system. A simple default with extensive Handbook support.", True),
            ("systemd", "systemd", "Integrated service management, journaling, networking, and boot tooling used by many desktop systems.", False),
        ])
        self.content.addWidget(self.init_group)

        base = Card("Stage3 and profile")
        form = QFormLayout()
        self.stage = QComboBox()
        form.addRow("Stage3 variant", self.stage)
        self.multilib = QCheckBox("Include 32-bit application support (multilib)")
        self.multilib.toggled.connect(self._refresh_stages)
        form.addRow(self.multilib)
        self.profile = QComboBox()
        self.profile.setEditable(True)
        form.addRow("Portage profile", self.profile)
        self.world_update = QCheckBox("Update @world during installation (can take much longer)")
        form.addRow(self.world_update)
        base.content.addLayout(form)
        self.content.addWidget(base)
        self.init_group.selectionChanged.connect(self._init_changed)
        self.stage.currentIndexChanged.connect(self._stage_changed)

        self.desktop_group = ChoiceGroup([
            ("kde", "KDE Plasma", "A polished, flexible desktop with SDDM and a familiar application suite.", True),
            ("gnome", "GNOME", "A focused, modern desktop with GDM and GNOME applications.", False),
            ("xfce", "Xfce", "A lightweight desktop that works well on older or resource-limited hardware.", False),
            ("minimal", "Minimal system", "No graphical desktop. Install only the base system and selected tools.", False),
        ])
        self.content.addWidget(QLabel("Desktop environment"))
        self.content.addWidget(self.desktop_group)
        self.desktop_group.selectionChanged.connect(self._refresh_profiles)

        boot = Card("Kernel and boot")
        boot_form = QFormLayout()
        self.kernel = QComboBox()
        self.kernel.addItem("Distribution binary kernel — fastest, recommended", "gentoo-kernel-bin")
        self.kernel.addItem("Distribution source kernel — customizable", "gentoo-kernel")
        self.kernel.addItem("Manual kernel configuration — advanced", "manual")
        boot_form.addRow("Kernel", self.kernel)
        self.initramfs = QComboBox()
        self.initramfs.addItem("Dracut", "dracut")
        self.initramfs.addItem("Genkernel", "genkernel")
        self.initramfs.addItem("None", "none")
        boot_form.addRow("Initramfs", self.initramfs)
        self.kernel_guidance = QLabel(
            "Gentoo distribution kernels require Dracut. Manual kernel configuration is the advanced path for custom kernel and initramfs workflows."
        )
        self.kernel_guidance.setProperty("muted", True)
        self.kernel_guidance.setWordWrap(True)
        boot_form.addRow("", self.kernel_guidance)
        self.bootloader = QComboBox()
        self.bootloader.addItem("GRUB — UEFI and legacy BIOS", "grub")
        self.bootloader.addItem("systemd-boot — UEFI only", "systemd-boot")
        self.bootloader.addItem("EFI stub / UKI — UEFI only", "efi-stub")
        boot_form.addRow("Bootloader", self.bootloader)
        self.dual_boot = QCheckBox("Detect other operating systems with os-prober")
        boot_form.addRow(self.dual_boot)
        self.uki = QCheckBox("Build a Unified Kernel Image (UKI)")
        boot_form.addRow(self.uki)
        self.secure_boot = QCheckBox("Sign the boot chain for Secure Boot")
        self.secure_boot.toggled.connect(self._secure_changed)
        boot_form.addRow(self.secure_boot)
        self.signing_key = QLineEdit()
        self.signing_key.setPlaceholderText("Absolute path to a private signing key")
        boot_form.addRow("Signing key", self.signing_key)
        self.signing_cert = QLineEdit()
        self.signing_cert.setPlaceholderText("Absolute path to the signing certificate")
        boot_form.addRow("Certificate", self.signing_cert)
        boot.content.addLayout(boot_form)
        self.content.addWidget(boot)
        self._refresh_stages()
        self.load_manifest()

    def _init_changed(self) -> None:
        if getattr(self, "_changing_init", False):
            return
        self._changing_init = True
        try:
            self._refresh_stages()
        finally:
            self._changing_init = False

    def _refresh_stages(self) -> None:
        if getattr(self, "_updating", False):
            return
        self._updating = True
        try:
            current = self.stage.currentData()
            init = self.init_group.value() or self.manifest.system.init
            variants = []
            for prefix, label in (("amd64", "Standard"), ("amd64-desktop", "Desktop"), ("amd64-nomultilib", "No multilib"), ("amd64-hardened", "Hardened")):
                value = f"{prefix}-{init}"
                variants.append((value, f"{label} · {init}"))
            self.stage.clear()
            for value, label in variants:
                self.stage.addItem(label, value)
            wanted = current if current and current.endswith(init) else f"amd64-{init}"
            if not self.multilib.isChecked() and current:
                wanted = f"amd64-nomultilib-{init}"
            self.stage.setCurrentIndex(max(0, self.stage.findData(wanted)))
            self._refresh_profiles()
        finally:
            self._updating = False

    def _stage_changed(self) -> None:
        if getattr(self, "_updating", False):
            return
        variant = self.stage.currentData() or ""
        self.multilib.setChecked("nomultilib" not in variant)
        self._refresh_profiles()

    def _refresh_profiles(self) -> None:
        current = self.profile.currentText()
        init = self.init_group.value() or "openrc"
        desktop = self.desktop_group.value() if hasattr(self, "desktop_group") else "kde"
        variant = self.stage.currentData() or ""
        values = [profile_path(init, desktop,
                               multilib="nomultilib" not in variant,
                               hardened="hardened" in variant)]
        self.profile.clear()
        self.profile.addItems(dict.fromkeys(values))
        if current in values:
            self.profile.setCurrentText(current)

    def _secure_changed(self, checked: bool) -> None:
        self.signing_key.setEnabled(checked)
        self.signing_cert.setEnabled(checked)

    def load_manifest(self) -> None:
        system = self.manifest.system
        self.init_group.set_value(system.init)
        self.multilib.setChecked(system.multilib)
        self.desktop_group.set_value(system.desktop)
        self._refresh_stages()
        self.stage.setCurrentIndex(max(0, self.stage.findData(system.stage_variant)))
        self.profile.setCurrentText(system.profile)
        self.kernel.setCurrentIndex(max(0, self.kernel.findData(system.kernel)))
        self.initramfs.setCurrentIndex(max(0, self.initramfs.findData(system.initramfs)))
        self.bootloader.setCurrentIndex(max(0, self.bootloader.findData(system.bootloader)))
        self.dual_boot.setChecked(system.dual_boot)
        self.uki.setChecked(system.uki)
        self.secure_boot.setChecked(system.secure_boot)
        self.signing_key.setText(system.secure_boot_key)
        self.signing_cert.setText(system.secure_boot_certificate)
        self.world_update.setChecked(system.world_update)
        self._secure_changed(system.secure_boot)

    def save_manifest(self) -> None:
        system = self.manifest.system
        system.init = self.init_group.value() or "openrc"
        system.stage_variant = self.stage.currentData()
        system.multilib = self.multilib.isChecked()
        system.profile = self.profile.currentText().strip()
        system.desktop = self.desktop_group.value() or "minimal"
        system.kernel = self.kernel.currentData()
        system.initramfs = self.initramfs.currentData()
        system.bootloader = self.bootloader.currentData()
        system.dual_boot = self.dual_boot.isChecked()
        system.uki = self.uki.isChecked()
        system.secure_boot = self.secure_boot.isChecked()
        system.secure_boot_key = self.signing_key.text().strip() if system.secure_boot else ""
        system.secure_boot_certificate = self.signing_cert.text().strip() if system.secure_boot else ""
        system.world_update = self.world_update.isChecked()

    def validate_page(self) -> tuple[bool, str]:
        self.save_manifest()
        system = self.manifest.system
        if system.secure_boot and (not system.secure_boot_key or not system.secure_boot_certificate):
            return False, "Provide both Secure Boot signing files or disable Secure Boot signing."
        errors = [error for error in self.manifest.validate() if any(word in error.lower() for word in ("stage3", "init ", "profile", "kernel", "boot", "multilib", "initramfs"))]
        return (False, errors[0]) if errors else (True, "")
