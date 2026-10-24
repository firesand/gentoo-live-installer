from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QLabel, QLineEdit

from ..manifest import InstallManifest
from ..widgets import Card, ChoiceGroup, Page, wrapped_label

PROFILE_BASE = "default/linux/amd64/23.0"
INITS = ("openrc", "systemd")

# Stage3 families and what the installer can build on each. KDE Plasma needs
# the desktop USE flags that only the Standard and Desktop profiles enable
# (plasma-meta has unmet USE dependencies on No-multilib and Hardened); GNOME
# and Xfce are unverified there. Minimal always works.
FAMILY_PREFIX = {"standard": "amd64", "desktop": "amd64-desktop", "nomultilib": "amd64-nomultilib", "hardened": "amd64-hardened"}
FAMILY_LABELS = {"standard": "Standard", "desktop": "Desktop", "nomultilib": "No multilib", "hardened": "Hardened"}
FAMILY_DESKTOP_SUPPORT = {
    "standard": {"kde", "gnome", "xfce"},
    "desktop": {"kde", "gnome", "xfce"},
    "nomultilib": {"gnome", "xfce"},
    "hardened": {"gnome", "xfce"},
}
UNVERIFIED_FAMILIES = {"nomultilib", "hardened"}
KERNELS = (
    ("Distribution binary kernel — fastest, recommended", "gentoo-kernel-bin"),
    ("Distribution source kernel — customizable", "gentoo-kernel"),
)
KDE_NEEDS_DESKTOP_STAGE = (
    "KDE Plasma needs a Standard or Desktop stage3: the Hardened and No-multilib profiles do not enable the desktop USE flags Plasma requires, "
    "so the package step would fail after your disk was erased. Choose a Standard stage3 or the Minimal system."
)


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


PUBLISHED_PROFILES = frozenset(
    PROFILE_BASE + middle + suffix
    for middle in ("", "/desktop", "/desktop/plasma", "/desktop/gnome", "/hardened", "/no-multilib", "/no-multilib/hardened")
    for suffix in ("", "/systemd")
)


def stage_family(stage_variant: str) -> str:
    if "hardened" in stage_variant:
        return "hardened"
    if "nomultilib" in stage_variant:
        return "nomultilib"
    if "desktop" in stage_variant:
        return "desktop"
    return "standard"


def desktop_problem(stage_variant: str, desktop: str) -> str:
    """Why this desktop cannot be installed on this stage3 ('' when it can)."""
    if desktop in {"kde", "gnome", "xfce"} and desktop not in FAMILY_DESKTOP_SUPPORT[stage_family(stage_variant)]:
        return KDE_NEEDS_DESKTOP_STAGE if desktop == "kde" else (
            f"{desktop.upper()} is not available on this stage3: choose a Standard or Desktop stage3 or the Minimal system.")
    return ""


def profile_problem(profile: str, init: str, stage_variant: str) -> str:
    """Why this Portage profile is wrong for the init system and stage3 ('' when fine)."""
    if not profile:
        return ""
    if profile not in PUBLISHED_PROFILES:
        return f"The Portage profile “{profile}” is not a published Gentoo 23.0 profile. Choose the profile offered for your stage3."
    if ("/systemd" in profile) != (init == "systemd"):
        return "The Portage profile does not match the init system: systemd profiles end in /systemd and OpenRC ones do not."
    if ("no-multilib" in profile) != ("nomultilib" in stage_variant):
        return "The Portage profile does not match the stage3 (multilib versus no-multilib)."
    if ("hardened" in profile) != ("hardened" in stage_variant):
        return "The Portage profile does not match the stage3 (hardened versus standard)."
    return ""


class SystemPage(Page):
    desktopChanged = Signal(str)  # a desktop was picked by the user (not loaded from the manifest)

    def __init__(self, manifest: InstallManifest):
        super().__init__("Choose your Gentoo foundation", "These choices determine the stage3, profile, kernel, boot process, and desktop. Recommended choices work well for a first installation.")
        self.manifest = manifest
        self._updating = False
        self._loading = 0
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
        self.multilib.setChecked(True)
        self.multilib.toggled.connect(self._refresh_stages)
        form.addRow(self.multilib)
        self.profile = QComboBox()  # not editable: only published profiles can be installed
        self.profile.setAccessibleName("Portage profile")
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
        self.family_note = wrapped_label("", muted=True)
        self.family_note.hide()
        self.content.addWidget(self.family_note)
        self.desktop_group.selectionChanged.connect(self._desktop_selected)

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
        self.advance_on_enter(self.signing_key, self.signing_cert)
        self._refresh_stages()
        self.load_manifest()

    # --- stage3, profile, desktop ----------------------------------------------------------------

    def _init_changed(self) -> None:
        if getattr(self, "_changing_init", False):
            return
        self._changing_init = True
        try:
            self._refresh_stages()
        finally:
            self._changing_init = False

    def _refresh_stages(self) -> None:
        """Rebuild the stage3 list for the current init system. The result depends
        only on the init system, the family currently shown and the multilib box,
        never on what was clicked before."""
        if self._updating:
            return
        self._updating = True
        try:
            init = self.init_group.value() or self.manifest.system.init
            current = self.stage.currentData() or ""
            family = stage_family(current) if current else "standard"
            if not self.multilib.isChecked():
                family = "nomultilib"
            elif family == "nomultilib":
                family = "standard"
            self.stage.clear()
            for name, prefix in FAMILY_PREFIX.items():
                self.stage.addItem(f"{FAMILY_LABELS[name]} · {init}", f"{prefix}-{init}")
            self.stage.setCurrentIndex(max(0, self.stage.findData(f"{FAMILY_PREFIX[family]}-{init}")))
            self._refresh_profiles()
        finally:
            self._updating = False
        self._apply_family_constraints()

    def _stage_changed(self) -> None:
        if self._updating:
            return
        variant = self.stage.currentData() or ""
        self._updating = True
        try:
            self.multilib.setChecked("nomultilib" not in variant)
        finally:
            self._updating = False
        self._refresh_profiles()
        self._apply_family_constraints()

    def _desktop_selected(self, key: str) -> None:
        self._refresh_profiles()
        if not self._loading:
            self._apply_family_constraints()
            self.desktopChanged.emit(key)

    def _refresh_profiles(self, keep: str = "") -> None:
        init = self.init_group.value() or "openrc"
        desktop = self.desktop_group.value() if hasattr(self, "desktop_group") else "kde"
        variant = self.stage.currentData() or ""
        derived = profile_path(init, desktop or "minimal", multilib="nomultilib" not in variant, hardened="hardened" in variant)
        self.profile.clear()
        self.profile.addItem(derived, derived)
        if keep and keep != derived:
            if keep in PUBLISHED_PROFILES:
                self.profile.addItem(keep, keep)
            else:
                self.profile.addItem(f"{keep} (not a published profile)", keep)
            self.profile.setCurrentIndex(self.profile.count() - 1)

    def _apply_family_constraints(self, user_change: bool = True) -> None:
        """Disable the desktops the chosen stage3 cannot build; when the user's
        own change made the current desktop impossible, fall back to Minimal.
        A manifest that is merely loaded is never changed here (the page then
        refuses it instead)."""
        if not hasattr(self, "desktop_group"):
            return
        family = stage_family(self.stage.currentData() or "")
        supported = FAMILY_DESKTOP_SUPPORT[family]
        for key in ("kde", "gnome", "xfce"):
            reason = "" if key in supported else desktop_problem(f"amd64-{family}", key)
            self.desktop_group.set_card_enabled(key, key in supported, reason)
        chosen = self.desktop_group.value()
        if chosen and chosen != "minimal" and chosen not in supported and user_change and not self._loading:
            name = {"kde": "KDE Plasma", "gnome": "GNOME", "xfce": "Xfce"}[chosen]
            self.desktop_group.set_value("minimal")
            self.family_note.setText(f"{name} needs a Standard or Desktop stage3, so the selection was changed to the Minimal system. Choose a Standard stage3 to use {name}.")
            self.family_note.show()
        elif family in UNVERIFIED_FAMILIES and chosen in {"gnome", "xfce"}:
            self.family_note.setText("Not verified: GNOME and Xfce on this stage3 may need extra USE changes during the installation.")
            self.family_note.show()
        else:
            self.family_note.hide()

    # --- boot -------------------------------------------------------------------------------------------

    def _bootloader_changed(self) -> None:
        grub = self.bootloader.currentData() == "grub"
        self.dual_boot.setEnabled(grub)
        self.dual_boot.setToolTip("" if grub else "systemd-boot finds Windows on a shared EFI partition by itself; os-prober is only needed with GRUB.")

    def _refresh_bootloaders(self) -> None:
        """systemd-boot and EFI stub work on UEFI only: grey them out for a BIOS install."""
        uefi = self.manifest.storage.firmware == "uefi"
        model = self.bootloader.model()
        if isinstance(model, QStandardItemModel):
            for index in range(self.bootloader.count()):
                item = model.item(index)
                if item is not None:
                    item.setEnabled(uefi or self.bootloader.itemData(index) == "grub")

    def _secure_changed(self, checked: bool) -> None:
        self.signing_key.setEnabled(checked)
        self.signing_cert.setEnabled(checked)

    # --- manifest -----------------------------------------------------------------------------------------

    @staticmethod
    def _select(combo: QComboBox, value: str, unavailable: str = "(not available in this installer)") -> None:
        """Select value; a value the combo does not offer (from a loaded manifest)
        is shown as an extra, flagged entry instead of being replaced silently."""
        index = combo.findData(value)
        if index < 0 and value:
            combo.addItem(f"{value} {unavailable}", value)
            index = combo.count() - 1
        combo.setCurrentIndex(max(0, index))

    def load_manifest(self) -> None:
        system = self.manifest.system
        self._loading += 1
        try:
            self.init_group.set_value(system.init)
            self.multilib.setChecked(system.multilib)
            self.desktop_group.set_value(system.desktop)
            self._refresh_stages()
            self.stage.setCurrentIndex(max(0, self.stage.findData(system.stage_variant)))
            self._refresh_profiles(keep=system.profile)
            self._select(self.kernel, system.kernel)
            self._select(self.initramfs, system.initramfs)
            self.bootloader.setCurrentIndex(max(0, self.bootloader.findData(system.bootloader)))
            self.dual_boot.setChecked(system.dual_boot)
            self.uki.setChecked(system.uki)
            self.secure_boot.setChecked(system.secure_boot)
            self.signing_key.setText(system.secure_boot_key)
            self.signing_cert.setText(system.secure_boot_certificate)
            self.world_update.setChecked(system.world_update)
            self._secure_changed(system.secure_boot)
            self._refresh_bootloaders()
            self._bootloader_changed()
        finally:
            self._loading -= 1
        self._apply_family_constraints(user_change=False)

    def save_manifest(self) -> None:
        system = self.manifest.system
        system.init = self.init_group.value() or "openrc"
        system.stage_variant = self.stage.currentData()
        system.multilib = self.multilib.isChecked()
        system.profile = str(self.profile.currentData() or self.profile.currentText()).strip()
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
        if system.kernel not in {value for _, value in KERNELS}:
            return False, f"The kernel choice “{system.kernel}” is not available in the graphical installer. Choose a distribution kernel."
        if system.initramfs != "dracut":
            return False, f"The initramfs choice “{system.initramfs}” is not available in the graphical installer: distribution kernels need Dracut."
        problem = desktop_problem(system.stage_variant, system.desktop) or profile_problem(system.profile, system.init, system.stage_variant)
        if problem:
            return False, problem
        if system.secure_boot and (not system.secure_boot_key or not system.secure_boot_certificate):
            return False, "Provide both Secure Boot signing files or disable Secure Boot signing."
        errors = self.manifest.validate_sections()["system"]
        return (False, errors[0]) if errors else (True, "")
