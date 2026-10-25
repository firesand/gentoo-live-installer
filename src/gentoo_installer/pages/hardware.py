"""Hardware choices of the installation: the "Hardware" tab of the Customization page.

Every field of ``manifest.hardware`` has one control here, next to what the
installer found on this computer. The pure helpers at the top (no Qt) are shared
with the Readiness and Review pages:

* ``hardware_plan()`` asks the planner (``gentoo_installerd.hwplan``) what the
  installer will do, so the page never decides anything itself;
* ``nvidia_offer()`` says whether the proprietary NVIDIA driver can be offered, and ``driver_cards()`` what each driver card of the page says;
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

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import devices as devices_module
from .. import hardware_data, recommend
from ..hardware import HardwareProfile, gpu_label
from ..manifest import HARDWARE_CHOICES, NVIDIA_EXTRAS, PRINTER_DRIVERS, Hardware, InstallManifest
from ..system_info import SystemInfo, profile_of
from ..widgets import Card, ChoiceGroup, wrapped_label
from .devices_card import DevicesCard

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
    flavour: str = ""  # open | closed kernel modules
    recommendation: str = ""  # the plain sentence of the recommended card
    generations: tuple[str, ...] = ()  # of the NVIDIA cards in this PC


DRIVER_KEYS = ("auto", "open", "closed", "nouveau", "basic")


@dataclass(frozen=True)
class DriverCard:
    """One card of the driver choice: what it says and whether it can be picked here."""

    key: str
    title: str
    description: str
    available: bool = True
    reason: str = ""  # why it cannot be picked ("" when it can)
    recommended: bool = False


def _trial(manifest: InstallManifest, **hardware: Any) -> InstallManifest:
    return dataclasses.replace(manifest, hardware=dataclasses.replace(manifest.hardware, **hardware))


def nvidia_offer(manifest: InstallManifest, profile: HardwareProfile) -> NvidiaOffer:
    """Ask the planner with the proprietary driver switched on: a branch means it fits."""
    try:
        from gentoo_installerd import hwplan

        decision = hwplan.nvidia_decision(_trial(manifest, gpu_driver="nvidia", nvidia_branch="auto", nvidia_modules="auto"), profile)
    except Exception:  # noqa: BLE001
        LOG.exception("The NVIDIA decision could not be made")
        return NvidiaOffer(False, False)
    has_card = any(gpu.vendor == "nvidia" and not gpu.virtual for gpu in profile.gpus)
    visible = has_card or manifest.hardware.target == "portable" or manifest.hardware.gpu_driver == "nvidia"
    if decision.branch:
        info = hardware_data.NVIDIA_BRANCHES.get(decision.branch, {})
        return NvidiaOffer(visible, True, decision.branch, str(info.get("label", decision.branch)), str(info.get("license", "")),
                           flavour=decision.flavour, recommendation=decision.reason, generations=decision.generations)
    return NvidiaOffer(visible, False, reasons=tuple(issue.message for issue in decision.errors) or ((decision.reason,) if decision.reason else ()),
                       recommendation=decision.reason, generations=decision.generations)


def driver_key(hardware: Hardware, flavour: str = "open") -> str:
    """The card that shows these settings; ``flavour`` is what Automatic modules mean for the NVIDIA driver on this machine."""
    if hardware.gpu_driver in ("auto", "nouveau", "basic"):
        return hardware.gpu_driver
    modules = hardware.nvidia_modules if hardware.nvidia_modules != "auto" else (flavour or "open")
    return "closed" if modules == "closed" else "open"


def driver_settings(key: str) -> tuple[str, str]:
    """(gpu_driver, nvidia_modules) that a card stands for."""
    return {"auto": ("auto", "auto"), "open": ("nvidia", "open"), "closed": ("nvidia", "closed"), "nouveau": ("nouveau", "auto"), "basic": ("basic", "auto")}[key]


def driver_cards(manifest: InstallManifest, profile: HardwareProfile) -> list[DriverCard]:
    """The five driver cards for this machine, in the order they are shown. Plain trade-offs, no numbers; a card that cannot be picked says why."""
    offer = nvidia_offer(manifest, profile)
    reason_for: dict[str, str] = {}
    try:
        from gentoo_installerd import hwplan

        for modules in ("open", "closed"):
            decision = hwplan.nvidia_decision(_trial(manifest, gpu_driver="nvidia", nvidia_branch="auto", nvidia_modules=modules), profile)
            if not decision.branch:
                reason_for[modules] = " ".join(issue.message for issue in decision.errors) or decision.reason or "Not available on this computer."
    except Exception:  # noqa: BLE001
        LOG.exception("The driver cards could not be checked")
        reason_for = {"open": "Not available on this computer.", "closed": "Not available on this computer."}
    gpu_text = "your NVIDIA card" if not offer.recommendation else offer.recommendation
    if offer.available:
        automatic = (f"{gpu_text} Automatic uses it as soon as you accept NVIDIA's licence, and the open nouveau driver until then." if offer.recommendation.startswith("Recommended")
                     else "Uses the NVIDIA driver once you accept its licence, and the open nouveau driver until then.")
    else:
        automatic = "Uses the open-source driver that fits your graphics card. Nothing to accept, nothing downloaded from NVIDIA."
    sway = _trial(manifest).system.desktop == "sway"
    return [
        DriverCard("auto", "Automatic", automatic, recommended=True),
        DriverCard("open", "NVIDIA, open kernel modules (version 595)",
                   "For GeForce RTX 20 series and newer, and the only choice for the RTX 50 series. Needs NVIDIA's licence; the driver is built on this PC.",
                   "open" not in reason_for, reason_for.get("open", "")),
        DriverCard("closed", "NVIDIA, closed kernel modules (version 580)",
                   "For GeForce GTX 750 Ti to GTX 10 series, TITAN V and some RTX 20 laptops (their power saving needs it). Long-term version; not for the RTX 50 series. "
                   "Needs NVIDIA's licence; built on this PC.", "closed" not in reason_for, reason_for.get("closed", "")),
        DriverCard("nouveau", "Open-source driver (nouveau, with NVK)",
                   "Works on every card and needs no licence. Games and 3D programs run slower than on the NVIDIA driver, but nothing is downloaded from NVIDIA."),
        DriverCard("basic", "Basic display",
                   "No 3D acceleration and no Wayland: the safest start if nothing else works. You can change it later.",
                   not sway, "Basic display has no 3D, and the Sway desktop only runs on Wayland." if sway else ""),
    ]


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
    Spec("safe_graphics_entry", "Safe graphics boot entry", "check", advanced=True, group="graphics",
         hint="A second entry in the boot menu that starts without the NVIDIA driver. It is always added when the NVIDIA driver is installed, because it is the way back "
              "to a working screen."),
    Spec("nvidia_rtd3", "Power saving of the NVIDIA card", "choice", _choices("nvidia_rtd3", {"auto": AUTO, "on": "On", "off": "Off"}), advanced=True, group="graphics",
         hint="Lets a laptop switch its NVIDIA card off when idle. Only for RTX 20 series laptops with the closed modules; the newer cards do it with the open modules."),
    Spec("battery_charge_limit", "Battery charge limit", "choice", (("0", "No limit"), ("80", "Stop charging at 80%"), ("60", "Stop charging at 60%")), advanced=True, group="power",
         hint="A battery that is not always full lasts more years. Uses TLP, so it replaces power-profiles-daemon; the laptop must support it."),
    Spec("lid_action", "When the lid is closed", "choice", _choices("lid_action", {"suspend": "Sleep (the default)", "lock": "Lock the screen", "ignore": "Do nothing"}),
         advanced=True, group="power", hint="What closing the lid does, on battery and on the charger."),
    Spec("vendor_workarounds", "Known fixes for this computer's maker", "check", advanced=True, group="power",
         hint="Applies the small fixes the installer knows for your model, for example the lid of a Razer Blade."),
    Spec("signed_modules", "Signed kernel modules (Secure Boot)", "choice", (("off", "Off"), ("auto", "Automatic (not available yet)"), ("kernel_key", "Kernel key (not available yet)")),
         advanced=True, group="boot", hint="Signing the NVIDIA and other add-on modules so they load with Secure Boot is not available yet; the installer says so instead of ignoring the choice."),
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
CUSTOM_FIELDS = ("target", "gpu_driver", "nvidia_modules", "nvidia_branch", "nvidia_license_accepted", "nvidia_extras", "nvidia_cuda_license_accepted", "printer_drivers", "devices")
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
        self.recommendation = wrapped_label("", selectable=True)
        self.recommendation.hide()
        self.graphics.content.addWidget(self.recommendation)
        self.nvidia_license = QCheckBox("I accept the NVIDIA Software License")
        self.nvidia_license.setAccessibleName("Accept the NVIDIA licence")
        self.controls["nvidia_license_accepted"] = self.nvidia_license
        self.nvidia_license.toggled.connect(lambda _=False: self._edited("nvidia_license_accepted"))
        self.graphics.content.addWidget(self.nvidia_license)
        self.license_note = wrapped_label("The driver is downloaded from NVIDIA and built on this PC, which takes a while. If it cannot be built, the open nouveau driver stays in use.", muted=True)
        self.graphics.content.addWidget(self.license_note)
        self.nvidia_reason = wrapped_label("", muted=True)
        self.nvidia_reason.hide()
        self.graphics.content.addWidget(self.nvidia_reason)
        self.driver_toggle = QToolButton()
        self.driver_toggle.setText("Choose another driver")
        self.driver_toggle.setCheckable(True)
        self.driver_toggle.setAutoRaise(True)
        self.driver_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.driver_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.driver_toggle.setAccessibleName("Choose another driver")
        self.driver_toggle.toggled.connect(self._driver_toggled)
        self.graphics.content.addWidget(self.driver_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.driver_box = QWidget()
        box = QVBoxLayout(self.driver_box)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)
        self.gpu_driver = ChoiceGroup([(key, key, "", key == "auto") for key in DRIVER_KEYS], columns=2)
        self.controls["gpu_driver"] = self.gpu_driver
        self.controls["nvidia_modules"] = self.gpu_driver  # the kernel modules are part of the driver card
        box.addWidget(self.gpu_driver)
        self.nvidia_form = self._form()
        self.nvidia_branch = QComboBox()
        self.nvidia_branch.setAccessibleName("NVIDIA driver version")
        self.controls["nvidia_branch"] = self.nvidia_branch
        self.nvidia_branch.activated.connect(lambda _=0: self._edited("nvidia_branch"))
        self.nvidia_branch_label = QLabel("Driver version")
        self.nvidia_branch_label.setBuddy(self.nvidia_branch)
        self.nvidia_form.addRow(self.nvidia_branch_label, self.nvidia_branch)
        box.addLayout(self.nvidia_form)
        self.graphics.content.addWidget(self.driver_box)
        self.forms: dict[str, QFormLayout] = {"graphics": self._form()}
        self.graphics.content.addLayout(self.forms["graphics"])
        self._add_extras()
        outer.addWidget(self.graphics)
        self.devices = DevicesCard()
        self.devices.toggledKey.connect(lambda _key, _checked: self._edited("devices"))
        outer.addWidget(self.devices)

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

    EXTRA_LABELS = {
        "vaapi": ("Video decoding on the NVIDIA card", "Uses a testing version of nvidia-vaapi-driver, built on this PC, so the browser can decode video on the card."),
        "cuda": ("CUDA toolkit", "For programs that compute on the card. A very large download that needs a second licence."),
        "nvtop": ("nvtop", "A monitor for the card, like top for the processor."),
        "persistenced": ("Persistence daemon", "Keeps the card ready between programs."),
    }

    def _add_extras(self) -> None:
        """The Advanced add-ons of the NVIDIA driver (hardware.nvidia_extras and the second licence of CUDA)."""
        self.extras_box = QWidget()
        layout = QVBoxLayout(self.extras_box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        heading = QLabel("NVIDIA add-ons")
        layout.addWidget(heading)
        self.extra_checks: dict[str, QCheckBox] = {}
        for name in NVIDIA_EXTRAS:
            title, hint = self.EXTRA_LABELS[name]
            check = QCheckBox(title)
            check.setAccessibleName(title)
            check.setAccessibleDescription(hint)
            check.toggled.connect(lambda _=False: self._edited("nvidia_extras"))
            self.extra_checks[name] = check
            layout.addWidget(check)
            layout.addWidget(wrapped_label(hint, muted=True))
        self.cuda_license = QCheckBox("I accept the NVIDIA CUDA licence (NVIDIA-CUDA)")
        self.cuda_license.setAccessibleName("Accept the NVIDIA CUDA licence")
        self.cuda_license.toggled.connect(lambda _=False: self._edited("nvidia_cuda_license_accepted"))
        layout.addWidget(self.cuda_license)
        self.controls["nvidia_extras"] = self.extra_checks["vaapi"]
        self.controls["nvidia_cuda_license_accepted"] = self.cuda_license
        self.graphics.content.addWidget(self.extras_box)

    def _driver_toggled(self, opened: bool) -> None:
        self.driver_toggle.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)
        self.refresh()

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
            if self.gpu_driver.value() != "auto":
                # Accepting NVIDIA's licence is its own act: choosing a driver never ticks it, whatever was loaded before.
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
            self.devices.clear_touched()
            self.gpu_driver.set_value(driver_key(hardware, self._flavour()))
            self.nvidia_license.setChecked(hardware.nvidia_license_accepted)
            for name, check in self.extra_checks.items():
                check.setChecked(name in hardware.nvidia_extras)
            self.cuda_license.setChecked(hardware.nvidia_cuda_license_accepted)
            for spec in SPECS:
                self._load_spec(spec, getattr(hardware, spec.name))
            for name, check in self.printer_checks.items():
                check.setChecked(name in hardware.printer_drivers)
            self._fill_branches()
        finally:
            self._loading -= 1
        self.refresh()

    def _flavour(self) -> str:
        """Which kernel modules Automatic means for the NVIDIA driver on this machine (open unless the machine needs the closed ones)."""
        if not self.detected:
            return "open"
        return nvidia_offer(self.manifest, self.profile).flavour or "open"

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

    LEGACY_BRANCHES = (
        ("470", "470 (not offered: the last for Kepler cards, removed from Gentoo)"),
        ("390", "390 (not offered: the last for Fermi cards, removed from Gentoo)"),
    )

    def _fill_branches(self) -> None:
        current = self.manifest.hardware.nvidia_branch
        self.nvidia_branch.clear()
        self.nvidia_branch.addItem("Automatic", "auto")
        for branch, details in hardware_data.NVIDIA_BRANCHES.items():
            self.nvidia_branch.addItem(str(details["label"]), branch)
        for branch, label in self.LEGACY_BRANCHES:  # greyed with the reason; 535 is not listed at all
            self.nvidia_branch.addItem(label, branch)
            index = self.nvidia_branch.count() - 1
            item = self.nvidia_branch.model().item(index)  # type: ignore[attr-defined]
            if item is not None:
                item.setEnabled(False)
            self.nvidia_branch.setItemData(index, "This version is no longer in Gentoo and cannot be installed here; the open nouveau driver supports these cards.",
                                           Qt.ItemDataRole.ToolTipRole)
        self._select(self.nvidia_branch, current, current)

    def save_manifest(self) -> None:
        """Write back only what the user changed on this page."""
        self._write(self.manifest.hardware)
        self._touched.clear()
        self.devices.clear_touched()

    def _write(self, hardware: Hardware) -> None:
        for name in sorted(self._touched):
            if name == "target":
                hardware.target = self.target.value() or hardware.target
            elif name == "gpu_driver":
                hardware.gpu_driver, hardware.nvidia_modules = driver_settings(self.gpu_driver.value() or "auto")
            elif name == "nvidia_license_accepted":
                hardware.nvidia_license_accepted = self.nvidia_license.isChecked() and self.gpu_driver.value() in ("auto", "open", "closed")
            elif name == "nvidia_branch":
                hardware.nvidia_branch = str(self.nvidia_branch.currentData() or "auto")
            elif name == "nvidia_extras":
                hardware.nvidia_extras = [extra for extra in NVIDIA_EXTRAS if self.extra_checks[extra].isChecked()]
            elif name == "nvidia_cuda_license_accepted":
                hardware.nvidia_cuda_license_accepted = self.cuda_license.isChecked()
            elif name == "devices":
                hardware.devices = {**hardware.devices, **self.devices.touched}
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
        if spec.name == "battery_charge_limit":
            return int(item)
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
        self._refresh_graphics(profile, working)
        self._refresh_visibility(profile, scalars, working)
        offers = devices_module.device_offers(profile, working) if self.detected else []
        self.devices.set_offers(offers, devices_module.wanted(working, offers), self.advanced)

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

    def _refresh_graphics(self, profile: HardwareProfile, working: InstallManifest) -> None:
        """The driver cards, the recommendation, the licence and the version: what the planner says for this machine and these choices."""
        offer = self._offer
        key = self.gpu_driver.value() or "auto"
        chosen_nvidia = key in ("open", "closed")
        relevant = offer.visible or chosen_nvidia
        for card in driver_cards(working, profile) if self.detected else [DriverCard(name, name, "", name in ("auto", "nouveau", "basic")) for name in DRIVER_KEYS]:
            widget = self.gpu_driver.cards[card.key]
            if card.title != card.key:
                widget.title_label.setText(card.title)
                widget.description_label.setText(card.description)
                widget.setAccessibleName(card.title)
                widget.setAccessibleDescription(card.description)
            self.gpu_driver.set_card_enabled(card.key, card.available or card.key == key, card.reason)
            widget.setVisible(card.key not in ("open", "closed") or relevant)
        guided_open = self.driver_toggle.isChecked()
        self.driver_toggle.setVisible(not self.advanced and relevant)
        self.driver_box.setVisible(self.advanced or guided_open or key != "auto")
        self.nvidia_license.setText(f"I accept the NVIDIA Software License ({offer.licence or 'NVIDIA-2025'})")
        self.nvidia_license.setVisible(relevant and (offer.available or chosen_nvidia) and key in ("auto", "open", "closed"))
        self.license_note.setVisible(self.nvidia_license.isVisibleTo(self.graphics))
        recommended = relevant and offer.available and key in ("auto", "open", "closed")
        self.recommendation.setText(offer.recommendation if recommended else "")
        self.recommendation.setVisible(recommended and bool(offer.recommendation))
        reasons = " ".join(offer.reasons) if relevant and not offer.available and key in ("auto", "open", "closed") else ""
        self.nvidia_reason.setText(reasons)
        self.nvidia_reason.setVisible(bool(reasons))
        show_branch = relevant and key in ("auto", "open", "closed") and (self.advanced or working.hardware.nvidia_branch != "auto")
        self.nvidia_branch.setVisible(show_branch)
        self.nvidia_branch_label.setVisible(show_branch)
        self.extras_box.setVisible(self.advanced and relevant and offer.available)
        cuda = self.extra_checks["cuda"].isChecked()
        self.cuda_license.setVisible(self.advanced and relevant and offer.available and cuda)
        self.graphics.setVisible(bool(profile.gpus) or offer.visible or not self.detected)

    def _refresh_visibility(self, profile: HardwareProfile, scalars: dict[str, Any], working: InstallManifest) -> None:
        mobile = profile.machine.form_factor in ("laptop", "tablet")
        nvidia_active = scalars.get("gpu_driver") == "nvidia"
        nvidia_present = any(gpu.vendor == "nvidia" and not gpu.virtual for gpu in profile.gpus)
        dense = max((display.dpi for display in profile.displays), default=0) > DENSE_SCREEN_DPI
        unknown = not self.detected
        vm = bool(profile.machine.hypervisor)
        relevant = {
            "nvk": nvidia_present and not nvidia_active,
            "safe_graphics_entry": nvidia_active,
            "nvidia_rtd3": nvidia_active and bool(scalars.get("hybrid")) and mobile,
            "battery_charge_limit": mobile or unknown,
            "lid_action": mobile or unknown,
            "vendor_workarounds": profile.machine.vendor_family == "razer" and profile.machine.oem == "blade",
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
