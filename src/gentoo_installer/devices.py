"""The "Devices on this computer" offers: which optional device tools make sense for the hardware the probe found (pure, no Qt).

``device_offers`` is the one list the Hardware page (the Devices card), the hardware planner and the text installer read, so they cannot
disagree. A key is offered only when the probe found the hardware; ``wanted`` resolves what the manifest says (an explicit choice in
``hardware.devices`` wins, else the offer's default). Defaults follow one rule: a tool is on by default only when the probe proved the
hardware and the tool is small and binary (bolt, wacom); anything that compiles on this PC, needs a testing keyword or comes from an
unofficial repository is offered unchecked and says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import hardware_data as data
from .hardware import HardwareProfile, is_hybrid
from .manifest import HARDWARE_DEVICE_KEYS, InstallManifest

# Keys that need an out-of-tree kernel module: refused while Secure Boot signing is on (the modules would be unsigned).
MODULE_DEVICE_KEYS = frozenset({"openrazer"})
LVFS_VENDORS = frozenset({"dell", "hp", "lenovo", "framework", "system76"})  # makers that publish firmware updates on the LVFS
UNOFFICIAL_REPOS_NOTE = ("It comes from the unofficial repositories edorp and GURU (user-maintained, not reviewed by Gentoo); "
                         "they are added to this system and updated with it.")


@dataclass(frozen=True)
class DeviceOffer:
    key: str  # one of HARDWARE_DEVICE_KEYS
    title: str  # short label of the checkbox
    sentence: str  # one plain sentence about what the tool does
    found: str  # what the probe saw that makes the offer
    default: bool  # on unless the user decided otherwise
    built_here: bool = False  # compiled on this PC instead of downloaded
    overlay: bool = False  # from an unofficial repository
    testing: bool = False  # a testing (~amd64) version, pinned to one exact version
    advanced: bool = False  # shown in the Advanced view only
    blocked: str = ""  # why it cannot be chosen now ("" when it can)

    @property
    def note(self) -> str:
        """The honest cost line: where it comes from and what it takes."""
        parts: list[str] = []
        if self.overlay:
            parts.append(UNOFFICIAL_REPOS_NOTE if self.key == "asus_tools" else "It comes from the unofficial repository GURU (user-maintained, not reviewed by Gentoo).")
        if self.testing:
            parts.append("Only one tested version is allowed.")
        if self.built_here:
            parts.append("It is built on this PC, which takes a few minutes.")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "title": self.title, "sentence": self.sentence, "found": self.found, "default": self.default, "built_here": self.built_here,
                "overlay": self.overlay, "testing": self.testing, "advanced": self.advanced, "blocked": self.blocked, "note": self.note}


def is_asus_laptop(profile: HardwareProfile) -> bool:
    """ASUS tools are for ASUS laptops only: the vendor string the asusd init script checks, a laptop chassis and the kernel's ASUS driver or a ROG/TUF line."""
    machine = profile.machine
    return (machine.sys_vendor.upper().startswith("ASUS") and machine.form_factor in ("laptop", "tablet")
            and (profile.platform.asus_nb_wmi or machine.oem in ("rog", "tuf")))


def _label(device: Any, fallback: str) -> str:
    name = " ".join(part for part in (device.manufacturer, device.product) if part).strip()
    return f"{name or fallback} ({device.vendor_id}:{device.product_id})"


def device_offers(profile: HardwareProfile, manifest: InstallManifest | None = None) -> list[DeviceOffer]:
    """The offers for this machine, most important first. Empty on a machine with nothing to offer (a plain virtual machine)."""
    secure_boot = bool(manifest and manifest.system.secure_boot)
    secure_note = "Secure Boot signing is on and this driver's kernel module would be unsigned." if secure_boot else ""
    mobile = profile.machine.form_factor in ("laptop", "tablet")
    usb = profile.peripherals
    offers: list[DeviceOffer] = []

    razer = usb.with_role("razer")
    if razer or profile.machine.vendor_family == "razer":
        found = _label(razer[0], "Razer device") if razer else f"{profile.machine.product_name or 'Razer'}"
        offers.append(DeviceOffer("openrazer", "Razer devices", "Lets you set the colours, lighting effects and DPI of your Razer keyboard, mouse or laptop.",
                                  found, default=True, built_here=True, blocked=secure_note))
        offers.append(DeviceOffer("razer_frontend", "A colour and DPI app for Razer", "RazerGenie on KDE, Polychromatic on the other desktops.",
                                  found, default=True, advanced=True))

    aura = usb.with_role("aura")
    other_rgb = [device for role in ("corsair", "steelseries") for device in usb.with_role(role)]
    if aura or other_rgb or (profile.platform.i2c_dev and not mobile and not profile.machine.hypervisor):
        found = _label((aura or other_rgb)[0], "RGB controller") if (aura or other_rgb) else "RGB memory or motherboard lighting (I2C)"
        offers.append(DeviceOffer("openrgb", "RGB lighting (OpenRGB)", "One app for the lighting of your motherboard, memory, fans and keyboards.", found, default=False, built_here=True))

    controllers = usb.with_role("controller")
    if controllers:
        offers.append(DeviceOffer("game_rules", "Game controller access", "Lets games use your controller without extra setup (udev rules).",
                                  _label(controllers[0], "Game controller"), default=False, testing=True))

    logitech = usb.with_role("logitech")
    if logitech:
        offers.append(DeviceOffer("solaar", "Logitech devices (Solaar)", "Shows battery, pairing and settings of your Logitech mouse or keyboard.",
                                  _label(logitech[0], "Logitech device"), default=False))

    if mobile and usb.thunderbolt:
        offers.append(DeviceOffer("bolt", "Thunderbolt devices", "Asks you before a Thunderbolt dock or drive is allowed to connect.", "Thunderbolt", default=True))
    pen_tablets = usb.with_role("tablet")
    if (mobile and profile.laptop.pen) or pen_tablets:
        found = "pen input" if (mobile and profile.laptop.pen) else _label(pen_tablets[0], "Drawing tablet")
        offers.append(DeviceOffer("wacom", "Pen and drawing tablet", "Pressure and buttons for a pen or drawing tablet.", found, default=True))
    sensors = [name for name in profile.laptop.iio_sensors if name.startswith(("accel", "gyro", "als"))]
    if mobile and sensors and (profile.laptop.convertible or "als" in sensors):
        offers.append(DeviceOffer("iio_sensors", "Screen rotation and brightness sensors", "Turns the screen with the laptop and adapts the brightness to the light.",
                                  ", ".join(sensors), default=False, overlay=True, testing=True, built_here=True))
    if is_asus_laptop(profile):
        hybrid = is_hybrid(profile)
        what = "keyboard lighting, fan and power profiles, battery limit" + (" and graphics switching" if hybrid else "")
        offers.append(DeviceOffer("asus_tools", "ASUS ROG and TUF tools", f"Controls {what} (asusctl{' and supergfxctl' if hybrid else ''}).",
                                  f"{profile.machine.product_name or 'ASUS laptop'}", default=False, built_here=True, overlay=True, testing=True))
    if mobile and profile.platform.thinkpad_acpi:
        offers.append(DeviceOffer("thinkfan", "ThinkPad fan control", "Installs thinkfan and lets the fan be controlled; you write its settings yourself.", "ThinkPad", default=False, advanced=True))
    # "fingerprint" has its own row on the Hardware page (hardware.fingerprint); the key is honoured by the planner all the same.
    assert {offer.key for offer in offers} <= set(HARDWARE_DEVICE_KEYS)
    return offers


def device_hints(profile: HardwareProfile) -> list[str]:
    """Plain lines for hardware that has a tool but not one the installer sets up (text only)."""
    hints: list[str] = []
    if profile.peripherals.with_role("corsair"):
        hints.append("Corsair devices: the app ckb-next controls many of them; it is not set up by the installer.")
    if profile.peripherals.with_role("steelseries"):
        hints.append("SteelSeries devices: OpenRGB can control many of them (offered above when found).")
    if profile.peripherals.with_role("tablet"):
        hints.append("Drawing tablets: libwacom knows most models; see the wacom option.")
    return hints


def wanted(manifest: InstallManifest, offers: list[DeviceOffer]) -> dict[str, bool]:
    """Key -> on or off for every offered key: the user's explicit choice, else the default (a blocked default is off)."""
    explicit: Mapping[str, bool] = manifest.hardware.devices
    return {offer.key: explicit[offer.key] if offer.key in explicit else (offer.default and not offer.blocked) for offer in offers}


def is_wanted(manifest: InstallManifest, offers: list[DeviceOffer], key: str) -> bool:
    """Whether a device tool is on: offered keys by ``wanted``, a key with no offer only when the user switched it on explicitly."""
    return wanted(manifest, offers).get(key, bool(manifest.hardware.devices.get(key, False)))
