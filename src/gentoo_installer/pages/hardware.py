"""Hardware choices of the installation: the "Hardware" tab of the Customization page.

Every field of ``manifest.hardware`` has one control here, next to what the
installer found on this computer. The pure helpers at the top (no Qt) are shared
with the Readiness and Review pages:

* ``hardware_plan()`` asks the planner (``gentoo_installerd.hwplan``) what the
  installer will do, so the page never decides anything itself;
* ``nvidia_offer()`` says whether the proprietary NVIDIA driver can be offered;
* ``hardware_issues()`` returns the planner's cross-checks, sorted by page.

Rule for the widgets: a value that was loaded from a settings file is never
rewritten by looking at the page. Only a control the user changed is written back.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import hardware_data, recommend
from ..hardware import HardwareProfile, gpu_label
from ..manifest import HARDWARE_CHOICES, PRINTER_DRIVERS, Hardware, InstallManifest
from ..system_info import SystemInfo, profile_of
from ..widgets import Card, ChoiceGroup, wrapped_label

LOG = logging.getLogger("gentoo_installer.hardware_page")

# Plain-English names of what an "automatic" choice turned into (HardwarePlan.scalars).
RESOLVED_TEXT: dict[str, dict[str, str]] = {
    "power": {"ppd": "power-profiles-daemon", "tlp": "TLP", "none": "no power management"},
    "thermald": {"yes": "on", "no": "off"},
    "zram": {"yes": "on", "no": "off"},
    "trim": {"weekly": "once a week", "off": "off"},
    "sof_firmware": {"yes": "added", "no": "not needed"},
    "nvk": {"yes": "on", "no": "off"},
    "prime": {"offload": "install the switching helpers", "off": "off"},
    "removable_fallback": {"always": "always", "if-missing": "only if missing", "never": "never"},
    "cpu_flags_mode": {"profile": "profile defaults", "detect": "tuned for this CPU", "manual": "your own list"},
}

# Which page can fix a cross-check of the planner (by code, never by wording).
ISSUE_SECTIONS = (
    ("storage", ("hw.st_", "hw.bios_4kn", "hw.nvme_bios", "hw.dos_over_2tib", "hw.disk_", "hw.fw_")),
    ("network", ("hw.net_", "hw.import_profiles_portable")),
    ("hardware", ("hw.",)),
)
# Capacity depends on the desktop chosen after the Storage page: it is checked once, before the review.
CAPACITY_CODES = ("hw.disk_too_small", "hw.disk_small")


def issue_section(code: str) -> str:
    """'storage' | 'network' | 'hardware': the part of the wizard that can fix this planner finding."""
    for section, prefixes in ISSUE_SECTIONS:
        if code.startswith(prefixes):
            return section
    return "hardware"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str  # error | warning | info
    message: str


def hardware_plan(manifest: InstallManifest, hardware: dict[str, Any] | None) -> Any:
    """The planner's HardwarePlan for this manifest and machine, or None when the planner cannot run."""
    try:
        from gentoo_installerd import hwplan

        return hwplan.plan(manifest, hardware or {})
    except Exception:  # noqa: BLE001 - a planner bug must never take the wizard down
        LOG.exception("The hardware plan could not be built")
        return None


def hardware_issues(manifest: InstallManifest, hardware: dict[str, Any] | None) -> list[Finding]:
    """Cross-checks of the choices against the detected machine (empty without detection data)."""
    if not hardware:
        return []
    try:
        from gentoo_installerd import hwplan

        return [Finding(issue.code, issue.severity, issue.message) for issue in hwplan.validate_plan(manifest, hardware)]
    except Exception:  # noqa: BLE001
        LOG.exception("The hardware cross-checks could not run")
        return []


def blocking_findings(manifest: InstallManifest, hardware: dict[str, Any] | None, section: str, *, skip: tuple[str, ...] = ()) -> list[str]:
    """Messages of the planner's errors that belong to one part of the wizard."""
    return [finding.message for finding in hardware_issues(manifest, hardware)
            if finding.severity == "error" and issue_section(finding.code) == section and not finding.code.startswith(skip)]


def plan_lines(plan: Any) -> list[str]:
    """A HardwarePlan as short plain lines: what will be set up, why, then what degrades or surprises."""
    if plan is None:
        return []
    lines = list(plan.summary)
    lines += [note for note in plan.notes if note not in lines]
    lines += [issue.message for issue in plan.warnings if issue.severity != "info" and issue.message not in lines]
    return lines


def preview_lines(manifest: InstallManifest, hardware: dict[str, Any] | None) -> list[str]:
    """What the installer will set up for this machine (empty without detection data)."""
    return plan_lines(hardware_plan(manifest, hardware)) if hardware else []


@dataclass(frozen=True)
class NvidiaOffer:
    """Whether the proprietary NVIDIA driver can be offered on this machine with these settings."""

    visible: bool  # worth showing at all (an NVIDIA card is present, a portable install, or the settings ask for it)
    available: bool
    branch: str = ""
    label: str = ""
    licence: str = ""
    reasons: tuple[str, ...] = ()  # why it is not available


def nvidia_offer(manifest: InstallManifest, profile: HardwareProfile) -> NvidiaOffer:
    """Ask the planner with the proprietary driver switched on: a branch means it fits."""
    try:
        from gentoo_installerd import hwplan

        trial = dataclasses.replace(manifest, hardware=dataclasses.replace(manifest.hardware, gpu_driver="nvidia", nvidia_branch="auto"))
        decision = hwplan.nvidia_decision(trial, profile)
    except Exception:  # noqa: BLE001
        LOG.exception("The NVIDIA decision could not be made")
        return NvidiaOffer(False, False)
    has_card = any(gpu.vendor == "nvidia" and not gpu.virtual for gpu in profile.gpus)
    visible = has_card or manifest.hardware.target == "portable" or manifest.hardware.gpu_driver == "nvidia"
    if decision.branch:
        info = hardware_data.NVIDIA_BRANCHES.get(decision.branch, {})
        return NvidiaOffer(visible, True, decision.branch, str(info.get("label", decision.branch)), str(info.get("license", "")))
    return NvidiaOffer(visible, False, reasons=tuple(issue.message for issue in decision.errors))


def gpu_lines(profile: HardwareProfile) -> list[str]:
    """One line per graphics card that was found."""
    lines = []
    for gpu in profile.gpus:
        name = gpu_label(gpu) if (gpu.name or gpu.vendor_name) else gpu.vendor
        role = "built into the processor" if gpu.integrated else ("virtual" if gpu.virtual else "separate card")
        lines.append(f"{name} ({role})")
    return lines


def resolved_text(name: str, scalars: dict[str, Any]) -> str:
    """'power-profiles-daemon' for hardware.power when it is on 'auto' and the plan chose ppd ('' when unknown)."""
    value = scalars.get(name)
    if name == "hidpi":
        return "" if value is None else ("no scaling" if value == "off" else f"{str(value)[4:]} dpi")
    return RESOLVED_TEXT.get(name, {}).get(str(value), "")


# --- the controls -------------------------------------------------------------------------------------------

AUTO = "Automatic"


@dataclass(frozen=True)
class Spec:
    """One row of the page. ``kind`` is choice (combo box), check or spin."""

    name: str
    label: str
    kind: str
    choices: tuple[tuple[str, str], ...] = ()  # (value, label) of a choice
    hint: str = ""
    advanced: bool = False  # shown in the Advanced experience, or when the value is not the default
    group: str = ""


def _choices(name: str, labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """Items for a combo box; every value the manifest allows has a label (a test keeps it so)."""
    return tuple((value, labels[value]) for value in labels if value in HARDWARE_CHOICES[name])


SPECS: tuple[Spec, ...] = (
    Spec("nvk", "NVK Vulkan driver", "choice", _choices("nvk", {"auto": AUTO, "on": "On", "off": "Off"}), advanced=True, group="graphics",
         hint="NVK is the open Vulkan driver for NVIDIA cards. It needs the graphics library Mesa to be compiled, which takes 30 to 60 minutes."),
    Spec("prime", "Two graphics cards", "choice", _choices("prime", {"auto": AUTO, "offload": "Install the switching helpers", "off": "Off"}), group="graphics",
         hint="On a laptop with an integrated and a separate card, a program can be started on the faster card when it needs it."),
    Spec("power", "Power management", "choice", _choices("power", {"auto": AUTO, "ppd": "power-profiles-daemon", "tlp": "TLP", "none": "None"}), group="power",
         hint="Saves battery on laptops. power-profiles-daemon is simple and fits KDE and GNOME; TLP saves more power and has many settings."),
    Spec("thermald", "Thermal management (Intel)", "choice", _choices("thermald", {"auto": AUTO, "yes": "On", "no": "Off"}), advanced=True, group="power",
         hint="Keeps an Intel laptop cooler and quieter. It does nothing on other processors."),
    Spec("fingerprint", "Fingerprint reader support", "check", group="power",
         hint="Installs the fingerprint service. Not every reader is supported, and you still enrol your finger in the installed desktop."),
    Spec("zram", "Compressed RAM swap (zram)", "choice", _choices("zram", {"auto": AUTO, "yes": "On", "no": "Off"}), group="memory",
         hint="Makes better use of a small memory. Recommended below 8 GiB."),
    Spec("zram_percent", "Memory used by zram", "spin", advanced=True, group="memory"),
    Spec("trim", "TRIM for solid-state disks", "choice", _choices("trim", {"auto": AUTO, "weekly": "Once a week", "off": "Off"}), advanced=True, group="memory",
         hint="Tells the disk which blocks are free, which keeps an SSD fast."),
    Spec("luks_discard", "Pass TRIM through the disk encryption", "check", advanced=True, group="memory",
         hint="Keeps an encrypted SSD fast, but an attacker can then see which blocks are in use. Leave it off if you are unsure."),
    Spec("hibernate", "Allow hibernation", "check", advanced=True, group="memory",
         hint="Needs a swap partition as large as the memory. It cannot be used together with disk encryption."),
    Spec("removable_fallback", "Fallback boot entry", "choice",
         _choices("removable_fallback", {"auto": AUTO, "always": "Always", "if-missing": "Only if missing", "never": "Never"}), advanced=True, group="boot",
         hint="A second boot file in the standard place lets the firmware find the system even when its boot list is reset. Windows keeps its own."),
    Spec("sof_firmware", "Laptop audio firmware (Sound Open Firmware)", "choice", _choices("sof_firmware", {"auto": AUTO, "yes": "Yes", "no": "No"}),
         advanced=True, group="boot", hint="Many new laptops have no sound without this firmware."),
    Spec("cpu_flags_mode", "CPU optimisation", "choice",
         _choices("cpu_flags_mode", {"auto": AUTO, "detect": "Tune for this CPU", "profile": "Profile defaults", "manual": "My own list"}), advanced=True, group="boot",
         hint="Tuning for this CPU makes some packages build from source instead of downloading them. 'My own list' uses CPU_FLAGS_X86 from the Portage tab."),
    Spec("fwupd", "Firmware updates from the vendor (fwupd)", "check", group="boot",
         hint="Lets you update the firmware of the computer, disks and devices from the desktop."),
    Spec("hidpi", "Screen scaling (Xfce)", "choice", (("auto", AUTO), ("off", "No scaling"), ("custom", "Custom")), group="display",
         hint="Makes text and buttons readable on a dense screen. KDE and GNOME scale by themselves."),
    Spec("pipewire_alsa", "Let old audio programs use PipeWire", "check", advanced=True, group="display",
         hint="Installs the ALSA plug-in of PipeWire for programs that only speak ALSA."),
    Spec("guest_tools", "Install guest tools for this virtual machine", "check", group="vm",
         hint="Clipboard sharing, automatic screen size and a clean shutdown from the host."),
)
SPEC_BY_NAME = {spec.name: spec for spec in SPECS}
# Fields with their own widgets.
CUSTOM_FIELDS = ("target", "gpu_driver", "nvidia_branch", "nvidia_license_accepted", "printer_drivers")
HIDPI_DEFAULT = 144
DENSE_SCREEN_DPI = 110  # above this the scaling control is worth showing in the Guided view


def covered_fields() -> set[str]:
    """The manifest.hardware fields that have a control on the page (a test compares it with the dataclass)."""
    return set(SPEC_BY_NAME) | set(CUSTOM_FIELDS)


class HardwareTab(QWidget):
    """The Hardware tab: detection summary, one control per manifest.hardware field, and the plan that follows from them."""

    changed = Signal()

    def __init__(self, manifest: InstallManifest, info: SystemInfo | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.manifest = manifest
        self.info = info
        self.advanced = False
        self._loading = 0
        self._touched: set[str] = set()
        self.controls: dict[str, QWidget] = {}
        self._rows: dict[str, list[tuple[QFormLayout, QWidget]]] = {}
        self._shown: dict[str, bool] = {}  # row name -> shown (isVisibleTo is unreliable while a parent card is hidden)
        self._offer = NvidiaOffer(False, False)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 8, 0, 0)
        outer.setSpacing(14)

        self.detected_card = Card("This computer", "")
        self.summary = wrapped_label("", selectable=True)
        self.detected_card.content.addWidget(self.summary)
        self.advice = wrapped_label("", muted=True, selectable=True)
        self.detected_card.content.addWidget(self.advice)
        self.target = ChoiceGroup([
            ("this", "This computer", "Tuned for the hardware found here. The best choice for a normal installation.", True),
            ("portable", "A portable disk",
             "For a disk that will move to other computers or run from a USB drive. It keeps generic CPU flags and drivers, installs both Intel and AMD "
             "microcode and all firmware, and adds a fallback boot entry.", False),
        ], columns=2)
        self.controls["target"] = self.target
        self.detected_card.content.addWidget(self.target)
        outer.addWidget(self.detected_card)

        self.graphics = Card("Graphics", "")
        self.gpus = wrapped_label("", muted=True, selectable=True)
        self.graphics.content.addWidget(self.gpus)
        self.gpu_driver = ChoiceGroup([
            ("open", "Open-source driver", "Works on every graphics card and needs no licence. This is the recommended choice.", True),
            ("nvidia", "NVIDIA proprietary driver",
             "Faster 3D and CUDA on NVIDIA cards. It is non-free software and needs your agreement to NVIDIA's licence. "
             "If it cannot be built, the open driver stays in use.", False),
        ], columns=2)
        self.controls["gpu_driver"] = self.gpu_driver
        self.graphics.content.addWidget(self.gpu_driver)
        self.nvidia_reason = wrapped_label("", muted=True)
        self.nvidia_reason.hide()
        self.graphics.content.addWidget(self.nvidia_reason)
        self.nvidia_form = self._form()
        self.nvidia_license = QCheckBox("I accept the NVIDIA Software License")
        self.nvidia_license.setAccessibleName("Accept the NVIDIA licence")
        self.controls["nvidia_license_accepted"] = self.nvidia_license
        self.nvidia_license.toggled.connect(lambda _=False: self._edited("nvidia_license_accepted"))
        self.nvidia_form.addRow(self.nvidia_license)
        self.nvidia_branch = QComboBox()
        self.nvidia_branch.setAccessibleName("NVIDIA driver version")
        self.controls["nvidia_branch"] = self.nvidia_branch
        self.nvidia_branch.activated.connect(lambda _=0: self._edited("nvidia_branch"))
        self.nvidia_branch_label = QLabel("Driver version")
        self.nvidia_branch_label.setBuddy(self.nvidia_branch)
        self.nvidia_form.addRow(self.nvidia_branch_label, self.nvidia_branch)
        self.graphics.content.addLayout(self.nvidia_form)
        self.forms: dict[str, QFormLayout] = {"graphics": self._form()}
        self.graphics.content.addLayout(self.forms["graphics"])
        outer.addWidget(self.graphics)

        self.cards: dict[str, Card] = {"graphics": self.graphics}
        for group, title in (("power", "Laptop and power"), ("memory", "Memory and storage"), ("boot", "Boot and firmware"),
                             ("display", "Screen, sound and printing"), ("vm", "Virtual machine")):
            card = Card(title, "")
            form = self._form()
            card.content.addLayout(form)
            self.forms[group] = form
            self.cards[group] = card
            outer.addWidget(card)
        for spec in SPECS:
            self._add_spec(spec)
        self._add_printers(self.forms["display"])

        self.plan_card = Card("What the installer will set up", "")
        self.plan_text = wrapped_label("", selectable=True)
        self.plan_card.content.addWidget(self.plan_text)
        outer.addWidget(self.plan_card)
        outer.addStretch(1)

        self.target.selectionChanged.connect(lambda _key: self._edited("target"))
        self.gpu_driver.selectionChanged.connect(lambda _key: self._edited("gpu_driver"))
        self.load_manifest()

    # --- building -------------------------------------------------------------------------------------------

    @staticmethod
    def _form() -> QFormLayout:
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(6)
        return form

    def _remember(self, name: str, form: QFormLayout, widget: QWidget) -> None:
        self._rows.setdefault(name, []).append((form, widget))

    def _add_spec(self, spec: Spec) -> None:
        form = self.forms[spec.group]
        widget: QWidget
        if spec.kind == "choice":
            combo = QComboBox()
            for value, text in spec.choices:
                combo.addItem(text, value)
            combo.setAccessibleName(spec.label)
            combo.activated.connect(lambda _=0, name=spec.name: self._edited(name))
            widget = combo
        elif spec.kind == "spin":
            spin = QSpinBox()
            spin.setRange(10, 100)
            spin.setSuffix(" %")
            spin.setAccessibleName(spec.label)
            spin.valueChanged.connect(lambda _=0, name=spec.name: self._edited(name))
            widget = spin
        else:
            check = QCheckBox(spec.label)
            check.toggled.connect(lambda _=False, name=spec.name: self._edited(name))
            widget = check
        self.controls[spec.name] = widget
        if spec.kind == "check":
            form.addRow(widget)
        else:
            label = QLabel(spec.label)
            label.setBuddy(widget)
            form.addRow(label, widget)
        self._remember(spec.name, form, widget)
        if spec.name == "hidpi":
            custom = QSpinBox()
            custom.setRange(96, 288)
            custom.setSingleStep(24)
            custom.setSuffix(" dpi")
            custom.setValue(HIDPI_DEFAULT)
            custom.setAccessibleName("Custom screen scaling")
            custom.valueChanged.connect(lambda _=0: self._edited("hidpi"))
            self.controls["hidpi_custom"] = custom
            form.addRow(custom)
        if spec.hint:
            hint = wrapped_label(spec.hint, muted=True)
            form.addRow(hint)
            self._remember(spec.name, form, hint)

    def _add_printers(self, form: QFormLayout) -> None:
        self.printer_checks: dict[str, QCheckBox] = {}
        labels = {"gutenprint": "Epson, Canon and other inkjet printers (Gutenprint)", "hplip": "HP printers (HPLIP)"}
        heading = QLabel("Printer drivers")
        form.addRow(heading)
        self._remember("printer_drivers", form, heading)
        for name in sorted(PRINTER_DRIVERS):
            check = QCheckBox(labels[name])
            check.toggled.connect(lambda _=False: self._edited("printer_drivers"))
            self.printer_checks[name] = check
            form.addRow(check)
            self._remember("printer_drivers", form, check)
        hint = wrapped_label("Network printers are found automatically. These add drivers for printers that need them.", muted=True)
        form.addRow(hint)
        self._remember("printer_drivers", form, hint)
        self.controls["printer_drivers"] = self.printer_checks["gutenprint"]

    # --- the machine ----------------------------------------------------------------------------------------

    def set_info(self, info: SystemInfo) -> None:
        self.info = info
        self.refresh()

    def set_advanced(self, advanced: bool) -> None:
        self.advanced = advanced
        self.refresh()

    @property
    def profile(self) -> HardwareProfile:
        return self.info.profile if self.info is not None else profile_of({})

    @property
    def hardware(self) -> dict[str, Any]:
        return self.info.hardware if self.info is not None else {}

    @property
    def detected(self) -> bool:
        return bool(self.hardware)

    # --- state <-> widgets ----------------------------------------------------------------------------------

    def _edited(self, name: str) -> None:
        if self._loading:
            return
        self._touched.add(name)
        if name == "gpu_driver":
            self._touched.add("nvidia_license_accepted")
            if self.gpu_driver.value() == "nvidia":
                # Accepting NVIDIA's licence is its own act: choosing the driver never ticks it, whatever was loaded before.
                self.nvidia_license.blockSignals(True)
                self.nvidia_license.setChecked(False)
                self.nvidia_license.blockSignals(False)
        self.refresh()
        self.changed.emit()

    def load_manifest(self) -> None:
        self._loading += 1
        try:
            hardware = self.manifest.hardware
            self._touched.clear()
            self.target.set_value(hardware.target if hardware.target in self.target.cards else "this")
            self.gpu_driver.set_value("nvidia" if hardware.gpu_driver == "nvidia" else "open")
            self.nvidia_license.setChecked(hardware.nvidia_license_accepted)
            for spec in SPECS:
                self._load_spec(spec, getattr(hardware, spec.name))
            for name, check in self.printer_checks.items():
                check.setChecked(name in hardware.printer_drivers)
            self._fill_branches()
        finally:
            self._loading -= 1
        self.refresh()

    def _load_spec(self, spec: Spec, value: Any) -> None:
        widget = self.controls[spec.name]
        if isinstance(widget, QCheckBox):
            widget.setChecked(value == "auto" if spec.name == "guest_tools" else bool(value))
        elif isinstance(widget, QSpinBox):
            widget.setValue(int(value))
        elif isinstance(widget, QComboBox):
            item = "custom" if spec.name == "hidpi" and str(value).startswith("dpi:") else str(value)
            self._select(widget, item, str(value))
            custom = self.controls.get("hidpi_custom")
            if spec.name == "hidpi" and isinstance(custom, QSpinBox):
                match = re.fullmatch(r"dpi:(\d+)", str(value))
                custom.setValue(int(match.group(1)) if match else HIDPI_DEFAULT)

    @staticmethod
    def _select(combo: QComboBox, item: str, stored: str) -> None:
        """Show ``item``; a stored value the combo does not offer is added as a flagged entry (never replaced silently)."""
        index = combo.findData(item)
        if index < 0:
            combo.addItem(f"{stored} (custom value)", item)
            index = combo.count() - 1
        combo.setCurrentIndex(index)

    def _fill_branches(self) -> None:
        current = self.manifest.hardware.nvidia_branch
        self.nvidia_branch.clear()
        self.nvidia_branch.addItem("Automatic", "auto")
        for branch, details in hardware_data.NVIDIA_BRANCHES.items():
            self.nvidia_branch.addItem(str(details["label"]), branch)
        self._select(self.nvidia_branch, current, current)

    def save_manifest(self) -> None:
        """Write back only what the user changed on this page."""
        self._write(self.manifest.hardware)
        self._touched.clear()

    def _write(self, hardware: Hardware) -> None:
        for name in sorted(self._touched):
            if name == "target":
                hardware.target = self.target.value() or hardware.target
            elif name == "gpu_driver":
                if self.gpu_driver.value() == "nvidia":
                    hardware.gpu_driver = "nvidia"
                elif hardware.gpu_driver == "nvidia":
                    hardware.gpu_driver = "open"
            elif name == "nvidia_license_accepted":
                hardware.nvidia_license_accepted = self.nvidia_license.isChecked() and self.gpu_driver.value() == "nvidia"
            elif name == "nvidia_branch":
                hardware.nvidia_branch = str(self.nvidia_branch.currentData() or "auto")
            elif name == "printer_drivers":
                hardware.printer_drivers = [driver for driver, check in sorted(self.printer_checks.items()) if check.isChecked()]
            elif name in SPEC_BY_NAME:
                setattr(hardware, name, self._spec_value(SPEC_BY_NAME[name]))

    def _spec_value(self, spec: Spec) -> Any:
        widget = self.controls[spec.name]
        if isinstance(widget, QCheckBox):
            return ("auto" if widget.isChecked() else "none") if spec.name == "guest_tools" else widget.isChecked()
        if isinstance(widget, QSpinBox):
            return widget.value()
        assert isinstance(widget, QComboBox)
        item = str(widget.currentData())
        if spec.name == "hidpi" and item == "custom":
            custom = self.controls["hidpi_custom"]
            return f"dpi:{custom.value()}" if isinstance(custom, QSpinBox) else "auto"
        return item

    def working_manifest(self) -> InstallManifest:
        """The manifest as it would be if this page's edits were saved (the real one is not touched)."""
        copy = dataclasses.replace(self.manifest, hardware=dataclasses.replace(self.manifest.hardware))
        self._write(copy.hardware)
        return copy

    # --- what the page shows --------------------------------------------------------------------------------

    def refresh(self) -> None:
        """Recompute what the page shows from the machine and the current (unsaved) choices."""
        if self._loading:
            return
        working = self.working_manifest()
        profile = self.profile
        plan = hardware_plan(working, self.hardware) if self.detected else None
        scalars = dict(plan.scalars) if plan is not None else {}
        self._offer = nvidia_offer(working, profile) if self.detected else NvidiaOffer(False, False)
        self._refresh_texts(profile, working, plan)
        self._refresh_auto_labels(scalars)
        self._refresh_visibility(profile, scalars, working)

    def _refresh_texts(self, profile: HardwareProfile, working: InstallManifest, plan: Any) -> None:
        if self.detected:
            self.summary.setText(profile.summary() or "This computer was detected, but there is little to report about it.")
            self.advice.setText(self._advice(profile, working))
            self.gpus.setText(("Found: " + "; ".join(gpu_lines(profile)) + ".") if profile.gpus else "The detection found no graphics card.")
        else:
            self.summary.setText("The hardware of this computer could not be detected here. Every choice stays on Automatic and is decided during the installation.")
            self.advice.setText("")
            self.gpus.setText("")
        self.advice.setVisible(bool(self.advice.text()))
        self.gpus.setVisible(bool(self.gpus.text()))
        lines = plan_lines(plan)
        self.plan_text.setText("\n".join("• " + line for line in lines) if lines else "Nothing special: the standard packages are installed.")
        self.plan_card.setVisible(self.detected)

    @staticmethod
    def _advice(profile: HardwareProfile, working: InstallManifest) -> str:
        try:
            found = recommend.recommend(profile, working)
        except Exception:  # noqa: BLE001
            LOG.exception("Hardware advice could not be built")
            return ""
        return "\n".join("• " + item.message for item in found[:5])

    def _refresh_auto_labels(self, scalars: dict[str, Any]) -> None:
        """'Automatic (power-profiles-daemon)': say what automatic means on this computer."""
        for spec in SPECS:
            widget = self.controls[spec.name]
            if not isinstance(widget, QComboBox):
                continue
            index = widget.findData("auto")
            if index >= 0:
                text = resolved_text(spec.name, scalars)
                widget.setItemText(index, f"{AUTO} ({text})" if text else AUTO)

    def _refresh_visibility(self, profile: HardwareProfile, scalars: dict[str, Any], working: InstallManifest) -> None:
        offer = self._offer
        chosen_nvidia = self.gpu_driver.value() == "nvidia"
        self.gpu_driver.set_card_enabled("nvidia", offer.available or chosen_nvidia, " ".join(offer.reasons) or "Not available on this computer.")
        self.gpu_driver.cards["nvidia"].setVisible(offer.visible or chosen_nvidia)
        reasons = " ".join(offer.reasons) if offer.visible or chosen_nvidia else ""
        self.nvidia_reason.setText(reasons if not offer.available else "")
        self.nvidia_reason.setVisible(bool(self.nvidia_reason.text()))
        self.nvidia_license.setText(f"I accept the NVIDIA Software License (Portage licence {offer.licence or 'NVIDIA-2025'})")
        self.nvidia_license.setVisible(chosen_nvidia)
        show_branch = chosen_nvidia and (self.advanced or working.hardware.nvidia_branch != "auto")
        self.nvidia_branch.setVisible(show_branch)
        self.nvidia_branch_label.setVisible(show_branch)
        self.graphics.setVisible(bool(profile.gpus) or offer.visible or not self.detected)

        mobile = profile.machine.form_factor in ("laptop", "tablet")
        nvidia_present = any(gpu.vendor == "nvidia" and not gpu.virtual for gpu in profile.gpus)
        dense = max((display.dpi for display in profile.displays), default=0) > DENSE_SCREEN_DPI
        unknown = not self.detected
        vm = bool(profile.machine.hypervisor)
        relevant = {
            "nvk": nvidia_present and not chosen_nvidia,
            "prime": bool(scalars.get("hybrid")),
            "power": mobile or unknown,
            "thermald": profile.cpu.vendor in ("intel", "") and (mobile or unknown),
            "fingerprint": bool(profile.input.fingerprint_usb),
            "luks_discard": working.storage.luks,
            "removable_fallback": working.storage.firmware == "uefi",
            "hibernate": not vm,
            "fwupd": not vm,
            "hidpi": dense or self.advanced,
            "guest_tools": vm,
            "zram_percent": working.hardware.zram != "no" and (working.hardware.zram == "yes" or scalars.get("zram") == "yes" or unknown),
        }
        defaults = Hardware()
        for spec in SPECS:
            loaded_differs = getattr(working.hardware, spec.name) != getattr(defaults, spec.name)
            fits = relevant.get(spec.name, True)
            self._show_row(spec.name, loaded_differs or (fits and (self.advanced or not spec.advanced)))
        hidpi, custom = self.controls["hidpi"], self.controls["hidpi_custom"]
        custom.setVisible(isinstance(hidpi, QComboBox) and self.row_shown("hidpi") and hidpi.currentData() == "custom")
        hibernate = self.controls["hibernate"]
        if isinstance(hibernate, QCheckBox):
            blocked = working.storage.luks
            hibernate.setEnabled(not blocked or hibernate.isChecked())
            hibernate.setToolTip("Hibernation cannot be used with disk encryption." if blocked else "")
        for group, card in self.cards.items():
            if group != "graphics":
                card.setVisible(self._group_has_visible_rows(group))

    def _show_row(self, name: str, shown: bool) -> None:
        self._shown[name] = shown
        for form, widget in self._rows.get(name, []):
            form.setRowVisible(widget, shown)

    def row_shown(self, name: str) -> bool:
        """Whether the row of this setting is on show (printer drivers are always on show)."""
        return self._shown.get(name, True)

    def _group_has_visible_rows(self, group: str) -> bool:
        names = [spec.name for spec in SPECS if spec.group == group]
        if group == "display":
            names.append("printer_drivers")
        return any(self.row_shown(name) for name in names)

    # --- checks ----------------------------------------------------------------------------------------------

    def problems(self) -> list[str]:
        """Errors that stop the installation and belong to this tab: the planner's cross-checks by code, then the manifest's own."""
        working = self.working_manifest()
        found = blocking_findings(working, self.hardware, "hardware")
        found += working.validate_sections()["hardware"]
        return list(dict.fromkeys(found))

    def values(self) -> dict[str, Any]:
        """The hardware choices as they would be saved now."""
        return dataclasses.asdict(self.working_manifest().hardware)
