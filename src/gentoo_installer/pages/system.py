"""CORE: the System page: init system, stage3 and profile, with the desktop and boot sections embedded."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QHBoxLayout

from ..manifest import InstallManifest, derive_profile
from ..widgets import Card, ChoiceGroup, Page, wrapped_label
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
from .help import AdvancedSection, HelpNote

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


DESKTOP_NAMES = {"kde": "KDE Plasma", "gnome": "GNOME", "xfce": "Xfce", "minimal": "a minimal system"}
INIT_WARNING = ("You cannot easily switch later. The init system starts your services at boot; OpenRC is Gentoo’s classic one, "
                "systemd is what most other distributions use.")
DESKTOP_STAGE_HELP = "A Desktop stage3 already contains the graphics and Rust/LLVM tools desktops need, so the install is faster."
MULTILIB_HELP = "Multilib lets 32-bit programs such as Steam or Wine run; most people want it."
PURE64_WARNING = "No 32-bit programs at all, and it cannot be undone easily; only choose this if you know you need it."


def automatic_family(desktop: str, multilib: bool) -> str:
    """The stage3 family that suits the choices: no-multilib without 32-bit support, the Desktop stage3 for any desktop, else Standard."""
    if not multilib:
        return "nomultilib"
    return "standard" if desktop in {"", "minimal"} else "desktop"


class SystemPage(Page):
    desktopChanged = Signal(str)  # a desktop was picked by the user (not loaded from the manifest)

    def __init__(self, manifest: InstallManifest):
        super().__init__("Choose your Gentoo foundation", "These choices determine the desktop, init system, kernel and boot process. The recommended choices work well for a first installation.")
        self.manifest = manifest
        self._derived_profile = ""
        self._loaded_profile = ""
        self._updating = False
        self._loading = 0
        self._changing_init = False

        # 1. the desktop, first: it is what most people come here to choose
        self.desktop_section = DesktopSection()
        self.desktop_group = self.desktop_section.desktop_group
        self.family_note = self.desktop_section.family_note
        self.content.addWidget(self.desktop_section)
        self.desktop_section.selectionChanged.connect(self._desktop_selected)

        # 2. the init system
        init_card = Card("Init system")
        self.init_group = ChoiceGroup([
            ("openrc", "OpenRC", "Gentoo’s lightweight, traditional init system. A simple default with extensive Handbook support.", True),
            ("systemd", "systemd", "Integrated service management, journaling, networking, and boot tooling used by many desktop systems.", False),
        ])
        init_card.content.addWidget(self.init_group)
        self.init_note = wrapped_label(INIT_WARNING, muted=True)
        init_card.content.addWidget(self.init_note)
        self.profile_summary = wrapped_label("", muted=True)
        init_card.content.addWidget(self.profile_summary)
        self.content.addWidget(init_card)
        self.init_group.selectionChanged.connect(self._init_changed)

        # 3. kernel and boot loader
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
        self.secure_mode = self.boot_section.secure_mode
        self.secure_confirm = self.boot_section.secure_confirm
        self.extra_options = self.boot_section.extra_options
        self.config_snippet = self.boot_section.config_snippet
        self.serial_console = self.boot_section.serial_console
        self.no_bootloader = self.boot_section.no_bootloader
        self.fallback = self.boot_section.fallback
        self.content.addWidget(self.boot_section)
        self.advance_on_enter(self.signing_key, self.signing_cert)

        # 4. expert options: the stage3, 32-bit support and the Portage profile
        self.expert = AdvancedSection("system", "Expert options")
        form = QFormLayout()
        self.stage = QComboBox()
        self.stage.setAccessibleName("Stage3 variant")
        self.stage_auto = QCheckBox("Choose the stage3 for me")
        self.stage_auto.setToolTip(DESKTOP_STAGE_HELP)
        self.stage_auto.toggled.connect(self._auto_toggled)
        stage_row = QHBoxLayout()
        stage_row.addWidget(self.stage, 1)
        stage_row.addWidget(self.stage_auto)
        stage_row.addWidget(HelpNote(DESKTOP_STAGE_HELP, "Stage"))
        form.addRow("Stage3 variant", stage_row)
        self.pure64 = QCheckBox("Pure 64-bit (no-multilib)")
        self.pure64.setToolTip(MULTILIB_HELP)
        self.pure64.toggled.connect(self._refresh_stages)
        pure_row = QHBoxLayout()
        pure_row.addWidget(self.pure64)
        pure_row.addWidget(HelpNote(MULTILIB_HELP, "Stage"))
        pure_row.addStretch()
        form.addRow(pure_row)
        self.pure64_warning = wrapped_label(PURE64_WARNING)
        self.pure64_warning.hide()
        form.addRow(self.pure64_warning)
        self.profile = QComboBox()  # not editable: only published profiles can be installed
        self.profile.setAccessibleName("Portage profile")
        form.addRow("Portage profile", self.profile)
        self.expert.add_layout(form)
        self.content.addWidget(self.expert)
        self.stage.currentIndexChanged.connect(self._stage_changed)

        self._refresh_stages()
        self.load_manifest()
        self.finish_layout()

    def set_advanced(self, advanced: bool) -> None:
        self.desktop_section.set_advanced(advanced)
        self.boot_section.set_advanced(advanced)
        self.expert.set_advanced(advanced)

    def set_info(self, info: Any) -> None:
        """The inspection result (other systems on the disks, the firmware's Secure Boot state)."""
        self.boot_section.set_info(info)

    # --- stage3, profile, desktop ----------------------------------------------------------------

    def _multilib(self) -> bool:
        return not self.pure64.isChecked()

    def _init_changed(self) -> None:
        if self._changing_init:
            return
        self._changing_init = True
        try:
            self._refresh_stages()
        finally:
            self._changing_init = False

    def _auto_toggled(self, checked: bool) -> None:
        if checked and not self._loading:
            self._refresh_stages()

    def _refresh_stages(self) -> None:
        """Rebuild the stage3 list for the current init system. With "choose for me" on, the family follows the desktop and the
        32-bit choice; otherwise it is the family currently shown, never what was clicked before."""
        if self._updating:
            return
        self._updating = True
        try:
            init = self.init_group.value() or self.manifest.system.init
            current = self.stage.currentData() or ""
            family = stage_family(current) if current else "standard"
            if self.stage_auto.isChecked():
                family = automatic_family(self.desktop_group.value() if hasattr(self, "desktop_group") else "", self._multilib())
            elif not self._multilib():
                family = "nomultilib"
            elif family == "nomultilib":
                family = "standard"
            self.stage.clear()
            for name, prefix in FAMILY_PREFIX.items():
                self.stage.addItem(f"{FAMILY_LABELS[name]} · {init}", f"{prefix}-{init}")
            self.stage.setCurrentIndex(max(0, self.stage.findData(f"{FAMILY_PREFIX[family]}-{init}")))
            self.pure64_warning.setVisible(self.pure64.isChecked())
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
            self.pure64.setChecked("nomultilib" in variant)
            self.stage_auto.setChecked(False)  # the user picked a stage3 by hand: it stays as picked
            self.pure64_warning.setVisible(self.pure64.isChecked())
        finally:
            self._updating = False
        self._refresh_profiles()
        self._apply_family_constraints()

    def _desktop_selected(self, key: str) -> None:
        if self.stage_auto.isChecked() and not self._loading:
            self._refresh_stages()  # a desktop preselects the Desktop stage3
        else:
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
        shown = str(self.profile.currentData() or derived)
        self.profile_summary.setText(f"Profile: {shown} (chosen for {DESKTOP_NAMES.get(desktop, desktop)} and {init.replace('openrc', 'OpenRC')})")

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
            self.pure64.setChecked(not system.multilib)
            # "Choose for me" applies to the Standard and Desktop stage3; Hardened and No-multilib were picked on purpose.
            self.stage_auto.setChecked(system.stage_auto and system.multilib and stage_family(system.stage_variant) in {"standard", "desktop"})
            self.desktop_section.load(system)
            self._refresh_stages()
            if not self.stage_auto.isChecked():
                self.stage.setCurrentIndex(max(0, self.stage.findData(system.stage_variant)))
            self._loaded_profile = system.profile
            self._refresh_profiles(keep=system.profile)
            self.boot_section.load(system, self.manifest.hardware)
        finally:
            self._loading -= 1
        self._apply_family_constraints(user_change=False)

    def save_manifest(self) -> None:
        system = self.manifest.system
        system.init = self.init_group.value() or "openrc"
        system.stage_variant = self.stage.currentData()
        system.stage_auto = self.stage_auto.isChecked()
        system.multilib = self._multilib()
        chosen = str(self.profile.currentData() or self.profile.currentText()).strip()
        # Empty means "derive from init, stage3 and desktop", so the profile follows later desktop
        # changes. A profile the loaded settings named explicitly is kept as it was.
        system.profile = "" if chosen == self._derived_profile and chosen != self._loaded_profile else chosen
        self.desktop_section.collect(system)
        self.boot_section.collect(system)
        self.boot_section.collect_hardware(self.manifest.hardware)

    def validate_page(self) -> tuple[bool, str]:
        self.save_manifest()
        system = self.manifest.system
        if system.kernel not in {value for _, value in KERNELS}:
            return False, f"The kernel choice “{system.kernel}” is not available in the graphical installer. Choose a Gentoo kernel."
        if system.initramfs != "dracut":
            return False, f"The initramfs choice “{system.initramfs}” is not available in the graphical installer: Gentoo kernels need Dracut."
        problem = desktop_problem(system.stage_variant, system.desktop) or profile_problem(self.manifest.effective_profile(), system.init, system.stage_variant)
        if problem:
            return False, problem
        problem = self.boot_section.problem(system)
        if problem:
            return False, problem
        errors = self.manifest.validate_sections()["system"]
        return (False, errors[0]) if errors else (True, "")
