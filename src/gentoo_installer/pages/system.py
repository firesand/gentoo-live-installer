"""CORE: the System page: init system, stage3 and profile, with the desktop and boot sections embedded."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout

from ..manifest import InstallManifest, derive_profile
from ..widgets import Card, ChoiceGroup, Page
from .boot_section import KERNELS, BootSection
from .desktop_section import (  # noqa: F401 - the page used to define these
    FAMILY_DESKTOP_SUPPORT,
    FAMILY_LABELS,
    FAMILY_PREFIX,
    KDE_NEEDS_DESKTOP_STAGE,
    UNVERIFIED_FAMILIES,
    DesktopSection,
    desktop_problem,
    stage_family,
)

PROFILE_BASE = "default/linux/amd64/23.0"
INITS = ("openrc", "systemd")


# One derivation for the wizard, the daemon and the plan lint.
profile_path = derive_profile


PUBLISHED_PROFILES = frozenset(
    PROFILE_BASE + middle + suffix
    for middle in ("", "/desktop", "/desktop/plasma", "/desktop/gnome", "/hardened", "/no-multilib", "/no-multilib/hardened")
    for suffix in ("", "/systemd")
)


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
        self._derived_profile = ""
        self._loaded_profile = ""
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

        # The sections own their widgets; the page keeps the old attribute names for them.
        self.desktop_section = DesktopSection()
        self.desktop_group = self.desktop_section.desktop_group
        self.family_note = self.desktop_section.family_note
        self.content.addWidget(self.desktop_section)
        self.desktop_section.selectionChanged.connect(self._desktop_selected)

        self.boot_section = BootSection(manifest)
        self.kernel = self.boot_section.kernel
        self.initramfs = self.boot_section.initramfs
        self.kernel_guidance = self.boot_section.kernel_guidance
        self.bootloader = self.boot_section.bootloader
        self.dual_boot = self.boot_section.dual_boot
        self.uki = self.boot_section.uki
        self.secure_boot = self.boot_section.secure_boot
        self.signing_key = self.boot_section.signing_key
        self.signing_cert = self.boot_section.signing_cert
        self.content.addWidget(self.boot_section)
        self.advance_on_enter(self.signing_key, self.signing_cert)
        self._refresh_stages()
        self.load_manifest()
        self.finish_layout()

    def set_advanced(self, advanced: bool) -> None:
        self.desktop_section.set_advanced(advanced)
        self.boot_section.set_advanced(advanced)

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
        self._derived_profile = derived
        self.profile.clear()
        self.profile.addItem(derived, derived)
        if keep and keep != derived:
            if keep in PUBLISHED_PROFILES:
                self.profile.addItem(keep, keep)
            else:
                self.profile.addItem(f"{keep} (not a published profile)", keep)
            self.profile.setCurrentIndex(self.profile.count() - 1)

    def _apply_family_constraints(self, user_change: bool = True) -> None:
        if not hasattr(self, "desktop_group"):
            return
        family = stage_family(self.stage.currentData() or "")
        self.desktop_section.apply_family_constraints(family, user_change, bool(self._loading))

    # --- manifest -----------------------------------------------------------------------------------------

    def load_manifest(self) -> None:
        system = self.manifest.system
        self._loading += 1
        try:
            self.init_group.set_value(system.init)
            self.multilib.setChecked(system.multilib)
            self.desktop_section.load(system)
            self._refresh_stages()
            self.stage.setCurrentIndex(max(0, self.stage.findData(system.stage_variant)))
            self._loaded_profile = system.profile
            self._refresh_profiles(keep=system.profile)
            self.world_update.setChecked(system.world_update == "now")
            self.boot_section.load(system)
        finally:
            self._loading -= 1
        self._apply_family_constraints(user_change=False)

    def save_manifest(self) -> None:
        system = self.manifest.system
        system.init = self.init_group.value() or "openrc"
        system.stage_variant = self.stage.currentData()
        system.multilib = self.multilib.isChecked()
        chosen = str(self.profile.currentData() or self.profile.currentText()).strip()
        # Empty means "derive from init, stage3 and desktop", so the profile follows later desktop
        # changes. A profile the loaded settings named explicitly is kept as it was.
        system.profile = "" if chosen == self._derived_profile and chosen != self._loaded_profile else chosen
        self.desktop_section.collect(system)
        self.boot_section.collect(system)
        system.world_update = self.world_update.isChecked()

    def validate_page(self) -> tuple[bool, str]:
        self.save_manifest()
        system = self.manifest.system
        if system.kernel not in {value for _, value in KERNELS}:
            return False, f"The kernel choice “{system.kernel}” is not available in the graphical installer. Choose a distribution kernel."
        if system.initramfs != "dracut":
            return False, f"The initramfs choice “{system.initramfs}” is not available in the graphical installer: distribution kernels need Dracut."
        problem = desktop_problem(system.stage_variant, system.desktop) or profile_problem(self.manifest.effective_profile(), system.init, system.stage_variant)
        if problem:
            return False, problem
        if system.secure_boot and (not system.secure_boot_key or not system.secure_boot_certificate):
            return False, "Provide both Secure Boot signing files or disable Secure Boot signing."
        errors = self.manifest.validate_sections()["system"]
        return (False, errors[0]) if errors else (True, "")
