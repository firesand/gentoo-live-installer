"""Vendor tools, peripherals and laptop extras of the hardware plan (Razer, RGB, game controllers, ASUS ROG and TUF, Thunderbolt, pens, ...).

``DevicePlanMixin`` is mixed into ``hwplan._Planner``. Everything here follows probe evidence (``gentoo_installer.devices.device_offers``): a tool
is planned only when the hardware was found, or when the user switched it on in ``hardware.devices``. Anything that needs a testing version gets a
keyword for that one exact version, never a global ~amd64; anything from an unofficial repository says so in the notes and in first-steps.txt.
"""

from __future__ import annotations

from typing import Any

from gentoo_installer import devices
from gentoo_installer import hardware_data as data
from gentoo_installer.hardware import HardwareProfile, is_hybrid
from gentoo_installer.manifest import InstallManifest

from .hwissue import Issue

GURU = "guru git https://github.com/gentoo/guru.git"
EDORP = "edorp git https://github.com/firesand/edorp-overlay.git"
ASUSCTL_ATOM, ASUSCTL_VERSION = "sys-power/asusctl", "6.5.0"
SUPERGFXCTL_ATOM, SUPERGFXCTL_VERSION = "sys-power/supergfxctl", "5.2.7"
# Tools that fight asusd or supergfxd over the same hardware: the plan never installs them beside the ASUS tools.
ASUS_POWER_CONFLICTS = ("sys-power/tlp", "sys-power/power-profiles-daemon")
ASUS_GPU_CONFLICTS = ("sys-power/envycontrol", "x11-misc/optimus-manager", "sys-power/optimus-manager", "sys-apps/prime-select", "sys-power/bbswitch", "sys-kernel/bbswitch")
OPENRAZER_AUTOSTART = ("if [ -f /usr/share/openrazer/openrazer-daemon.desktop ]; then mkdir -p /etc/xdg/autostart && "
                       "ln -sf /usr/share/openrazer/openrazer-daemon.desktop /etc/xdg/autostart/openrazer-daemon.desktop; fi; exit 0")
LID_CONF = "[Login]\nHandleLidSwitch={action}\nHandleLidSwitchExternalPower={action}\nHandleLidSwitchDocked={action}\n"
TLP_BATTERY = ("# Charge limit (written by the Gentoo installer): the battery stops at {stop}% and charges again below {start}%.\n"
               "START_CHARGE_THRESH_BAT0={start}\nSTOP_CHARGE_THRESH_BAT0={stop}\nSTART_CHARGE_THRESH_BAT1={start}\nSTOP_CHARGE_THRESH_BAT1={stop}\n")
BLUETOOTH_AUTOENABLE = "# Written by the Gentoo installer.\n[Policy]\nAutoEnable=true\n"
I2C_MODULE = "# Written by the Gentoo installer: OpenRGB reads the lighting of memory and motherboard over I2C.\ni2c-dev\n"
THINKPAD_FAN = "# Written by the Gentoo installer: lets thinkfan control the fan of this ThinkPad.\noptions thinkpad_acpi fan_control=1\n"


class DevicePlanMixin:
    """Vendor tools and laptop extras of the planner."""

    # Supplied by hwplan._Planner.
    m: InstallManifest
    hw: HardwareProfile
    p: Any
    desktop: str
    init: str
    portable: bool
    mobile: bool
    vm: bool
    offers: list[devices.DeviceOffer]

    # -- what is wanted -----------------------------------------------------------------------------------------

    def device(self, key: str) -> bool:
        return devices.is_wanted(self.m, self.offers, key)

    @property
    def asus_wanted(self) -> bool:
        """The ASUS laptop tools are switched on and this really is an ASUS laptop (decided before power management, which they replace)."""
        return self.device("asus_tools") and devices.is_asus_laptop(self.hw)

    @property
    def fingerprint_wanted(self) -> bool:
        return bool(self.m.hardware.fingerprint or self.m.hardware.devices.get("fingerprint", False))

    # -- the stage ----------------------------------------------------------------------------------------------

    def vendor(self) -> None:
        """Peripherals and vendor tools from probe evidence, then the laptop extras."""
        if self.portable:
            if any(self.m.hardware.devices.values()):
                self.note("Portable install: the device tools were skipped because they follow the hardware of one computer.")
            self.battery_and_lid()
            return
        self.razer()
        self.openrgb()
        self.game_rules()
        self.solaar()
        self.thunderbolt()
        self.pen()
        self.iio_sensors()
        self.asus_tools()
        self.thinkfan()
        self.battery_and_lid()
        if self.mobile and not self.vm:
            self.laptop_extras()
        self.vendor_workarounds()

    # -- Razer (B10) --------------------------------------------------------------------------------------------

    def razer(self) -> None:
        if not self.device("openrazer"):
            return
        self.p.optional_atoms.append("sys-apps/openrazer")
        self.use("sys-apps/openrazer", "dist-kernel")  # type: ignore[attr-defined]  # the kernel module is rebuilt with every kernel update
        self.p.groups.append("plugdev")
        # The ebuild only ships an example autostart file (/usr/share/openrazer/openrazer-daemon.desktop) and a systemd user unit; nothing starts the
        # daemon by itself. The link is made after the package is installed, and it is a no-op when the package failed to build.
        self.post("sh", "-c", OPENRAZER_AUTOSTART)  # type: ignore[attr-defined]
        if self.init == "systemd":
            self.p.user_services.append(("openrazer-daemon.service", "service"))
        frontend = ""
        if self.device("razer_frontend"):
            frontend = "sys-apps/razergenie" if self.desktop in data.QT_DESKTOPS else "sys-apps/polychromatic"
            self.p.optional_atoms.append(frontend)
        name = {"": "", "sys-apps/razergenie": "RazerGenie", "sys-apps/polychromatic": "Polychromatic"}[frontend]
        self.note("OpenRazer is built on this PC against your kernel; a kernel update rebuilds it. If the build fails the system still starts without it.")  # type: ignore[attr-defined]
        self.first_step("Razer devices: " + (f"open {name} to choose colours and DPI. " if name else "the daemon starts when you log in. ")  # type: ignore[attr-defined]
                        + "You were added to the group plugdev; log out and in once so it applies.")

    # -- RGB, controllers, Logitech ------------------------------------------------------------------------------

    def openrgb(self) -> None:
        if not self.device("openrgb"):
            return
        self.p.optional_atoms.append("app-misc/openrgb")
        self.p.groups.append("i2c")
        self.file("/etc/modules-load.d/installer-i2c-dev.conf", I2C_MODULE)  # type: ignore[attr-defined]  # only because OpenRGB was asked for
        self.note("OpenRGB is built on this PC. The i2c-dev kernel module is loaded at boot for it, and you are added to the group i2c.")  # type: ignore[attr-defined]
        self.first_step("OpenRGB: log out and in once so the group i2c applies, then start OpenRGB to set the lighting.")  # type: ignore[attr-defined]

    def game_rules(self) -> None:
        if not self.device("game_rules"):
            return
        atom = "games-util/game-device-udev-rules"
        self.p.optional_atoms.append(atom)
        self.keyword("=games-util/game-device-udev-rules-20260625", "~amd64")  # type: ignore[attr-defined]
        self.use(atom, "acl", "systemd" if self.init == "systemd" else "elogind")  # type: ignore[attr-defined]

    def solaar(self) -> None:
        if self.device("solaar"):
            self.p.optional_atoms.append("app-misc/solaar")

    # -- laptop and peripheral extras that follow the probe (B09) ----------------------------------------------------

    def thunderbolt(self) -> None:
        if not self.device("bolt"):
            return
        if self.desktop == "kde":
            self.use("kde-plasma/plasma-meta", "thunderbolt")  # type: ignore[attr-defined]  # pulls plasma-thunderbolt and bolt
        else:
            self.p.optional_atoms.append("sys-apps/bolt")
            if self.init == "openrc":
                self.service("boltd", optional=True)  # type: ignore[attr-defined]

    def pen(self) -> None:
        if not self.device("wacom"):
            return
        if self.desktop == "kde":
            self.use("kde-plasma/plasma-meta", "wacom")  # type: ignore[attr-defined]
        elif self.desktop != "minimal":
            self.p.optional_atoms.append("x11-drivers/xf86-input-wacom")

    def iio_sensors(self) -> None:
        if not self.device("iio_sensors"):
            return
        self.p.repos.append(GURU)
        self.p.optional_atoms.append("gnome-extra/iio-sensor-proxy")
        self.keyword("=gnome-extra/iio-sensor-proxy-3.9", "~amd64")  # type: ignore[attr-defined]
        self.service("iio-sensor-proxy", optional=True)  # type: ignore[attr-defined]
        self.note("The screen-rotation sensors need iio-sensor-proxy from the unofficial repository GURU, which is added to this system.")  # type: ignore[attr-defined]

    def thinkfan(self) -> None:
        if not self.device("thinkfan"):
            return
        self.p.optional_atoms.append("app-laptop/thinkfan")
        self.file("/etc/modprobe.d/installer-thinkpad-fan.conf", THINKPAD_FAN)  # type: ignore[attr-defined]
        self.first_step("ThinkPad fan: write /etc/thinkfan.yaml (see the thinkfan documentation), then enable the thinkfan service. "  # type: ignore[attr-defined]
                        "The installer does not start it for you because a wrong fan setting can overheat the laptop.")

    def laptop_extras(self) -> None:
        """Defaults that only a laptop needs and that the probe proved (Guided asks nothing new)."""
        hw = self.hw
        if self.desktop == "kde" and (hw.input.touchscreen or hw.laptop.convertible):
            self.use("kde-plasma/plasma-meta", "virtualkeyboard")  # type: ignore[attr-defined]
        if self.desktop in ("gnome", "gnome-light") and self.fingerprint_wanted:
            self.use("gnome-base/gdm", "fprint")  # type: ignore[attr-defined]
        if self.m.options.bluetooth and hw.bluetooth.present:
            self.file("/etc/bluetooth/main.conf.d/10-installer.conf", BLUETOOTH_AUTOENABLE)  # type: ignore[attr-defined]
        if hw.laptop.mem_sleep == "s2idle" and "deep" not in hw.laptop.mem_sleep_options:
            self.p.warnings.append(Issue("hw.sleep_s2idle", "info", "This laptop only offers the s2idle suspend mode (modern standby). Suspend works, "
                                         "but the battery drains faster than with classic suspend; the firmware settings may offer a Linux or S3 sleep mode."))
        if hw.laptop.ipu_camera:
            self.p.warnings.append(Issue("hw.camera_ipu", "info", "The built-in camera of this laptop (Intel IPU6) has no working driver on Gentoo yet. "
                                         "A USB webcam works, and so do video calls with it."))

    # -- battery limit, lid (B09) -------------------------------------------------------------------------------------

    def battery_and_lid(self) -> None:
        limit = self.m.hardware.battery_charge_limit
        if limit and self.p.scalars.get("power") == "tlp":
            self.file("/etc/tlp.d/90-installer-battery.conf", TLP_BATTERY.format(start=limit - 5, stop=limit))  # type: ignore[attr-defined]
            self.p.scalars["battery_charge_limit"] = limit
            self.first_step(f"Your battery stops charging at {limit}%. Change /etc/tlp.d/90-installer-battery.conf to use another limit.")  # type: ignore[attr-defined]
        action = self.m.hardware.lid_action
        if action != "suspend":
            if self.mobile or self.hw.power.has_lid or self.portable:
                directory = "/etc/systemd/logind.conf.d" if self.init == "systemd" else "/etc/elogind/logind.conf.d"
                self.file(f"{directory}/10-installer-lid.conf", LID_CONF.format(action=action))  # type: ignore[attr-defined]
                self.p.scalars["lid_action"] = action
            else:
                self.warn("lid_action_no_lid", "info", "The lid action was set, but this computer has no lid, so nothing was changed.")  # type: ignore[attr-defined]

    def vendor_workarounds(self) -> None:
        if not self.m.hardware.vendor_workarounds:
            return
        machine = self.hw.machine
        if machine.vendor_family == "razer" and machine.oem == "blade":
            self.p.kernel_cmdline.append("button.lid_init_state=open")  # the Blade reports its lid closed at boot
            self.p.scalars["vendor_workaround"] = "razer-blade-lid"
            self.note("Razer Blade workaround: the lid is treated as open at boot (button.lid_init_state=open).")  # type: ignore[attr-defined]
        else:
            self.warn("vendor_workarounds_none", "info", "Vendor workarounds are switched on, but the installer knows none for this computer.")  # type: ignore[attr-defined]

    # -- ASUS ROG and TUF laptops (B13) ---------------------------------------------------------------------------------

    def asus_tools(self) -> None:
        if not self.device("asus_tools"):
            return
        if not devices.is_asus_laptop(self.hw):
            self.warn("asus_tools_skipped", "warning",  # type: ignore[attr-defined]
                      "The ASUS laptop tools were not installed: this is not an ASUS laptop. (Their service refuses to start on any other computer.)")
            return
        systemd = "systemd" if self.init == "systemd" else "-systemd"
        hybrid = is_hybrid(self.hw)
        self.p.repos.extend([GURU, EDORP])  # edorp lists GURU as a master repository, so both are needed
        self.p.optional_atoms.append(ASUSCTL_ATOM)
        self.keyword(f"={ASUSCTL_ATOM}-{ASUSCTL_VERSION}", "~amd64")  # type: ignore[attr-defined]
        self.use(ASUSCTL_ATOM, "gui", systemd)  # type: ignore[attr-defined]
        self.service("asusd", optional=True)  # type: ignore[attr-defined]
        self.service("asus-shutdown", optional=True)  # type: ignore[attr-defined]
        if hybrid:
            self.p.optional_atoms.append(SUPERGFXCTL_ATOM)
            self.keyword(f"={SUPERGFXCTL_ATOM}-{SUPERGFXCTL_VERSION}", "~amd64")  # type: ignore[attr-defined]
            self.use(SUPERGFXCTL_ATOM, systemd)  # type: ignore[attr-defined]
            self.service("supergfxd", optional=True)  # type: ignore[attr-defined]
        self.p.scalars["asus_tools"] = "asusctl+supergfxctl" if hybrid else "asusctl"
        self.note("ASUS tools: asusctl" + (" and supergfxctl" if hybrid else "") + " come from the unofficial repositories edorp and GURU (user-maintained, not "  # type: ignore[attr-defined]
                  "reviewed by Gentoo), which are added to this system. They are built on this PC. asusd replaces power-profiles-daemon and TLP, "
                  "which fight it over the same power profiles" + ("; supergfxctl replaces envycontrol, optimus-manager, prime-select and bbswitch" if hybrid else "") + ".")
        self.first_step("ASUS tools come from the unofficial repositories edorp and GURU. They are updated by 'emerge --sync'; remove "  # type: ignore[attr-defined]
                        "/etc/portage/repos.conf/edorp.conf and guru.conf if you do not want them any more.")


def asus_conflicts(manifest: InstallManifest, profile: HardwareProfile) -> list[Issue]:
    """What stops the ASUS tools from being planned beside the rest of the settings (errors with the reason)."""
    offers = devices.device_offers(profile, manifest)
    if not (devices.is_wanted(manifest, offers, "asus_tools") and devices.is_asus_laptop(profile)):
        return []
    out: list[Issue] = []
    extras = set(manifest.options.extra_packages)
    power = [atom for atom in ASUS_POWER_CONFLICTS if atom in extras]
    if manifest.hardware.power in ("ppd", "tlp") or power or manifest.hardware.battery_charge_limit:
        what = ", ".join(power) or ({"ppd": "power-profiles-daemon", "tlp": "TLP"}.get(manifest.hardware.power, "TLP (for the battery limit)"))
        out.append(Issue("hw.asus_power_conflict", "error",
                         f"The ASUS tools (asusd) manage the power profiles and the battery limit themselves and conflict with {what}. "
                         "Set power management to Automatic and the battery limit to none, or switch the ASUS tools off."))
    gpu_switch = [atom for atom in ASUS_GPU_CONFLICTS if atom in extras]
    if gpu_switch and is_hybrid(profile):
        out.append(Issue("hw.asus_gpu_conflict", "error",
                         f"supergfxctl (part of the ASUS tools) switches the graphics itself and conflicts with {', '.join(gpu_switch)}. Remove them from the extra packages."))
    return out


def device_issues(manifest: InstallManifest, profile: HardwareProfile) -> list[Issue]:
    """Cross-checks of the Devices choices against the machine: Secure Boot, conflicts and settings that cannot work here."""
    out: list[Issue] = []
    offers = devices.device_offers(profile, manifest)
    if devices.is_wanted(manifest, offers, "openrazer") and manifest.system.secure_boot:
        out.append(Issue("hw.device_secure_boot", "error", data.SECURE_BOOT_MODULES_MESSAGE.format(what="OpenRazer")))
    out.extend(asus_conflicts(manifest, profile))
    hardware = manifest.hardware
    limit = hardware.battery_charge_limit
    if limit:
        if hardware.power in ("ppd", "none"):
            out.append(Issue("hw.power_conflict", "error", "A battery charge limit is set with TLP, but power management is set to something else. Set it to Automatic or TLP, or remove the limit."))
        if profile.machine.form_factor not in ("laptop", "tablet") and (profile.cpu.threads or profile.disks):
            out.append(Issue("hw.battery_limit_unsupported", "error", "A battery charge limit needs a laptop battery, and this computer has none."))
        elif profile.laptop.pm_profile is not None or profile.laptop.charge_threshold or profile.platform.platform_profile:
            if not profile.laptop.charge_threshold and profile.machine.form_factor in ("laptop", "tablet"):
                out.append(Issue("hw.battery_limit_unsupported", "warning",
                                 "The battery of this laptop did not report support for a charge limit; the limit is set anyway and works only if the firmware allows it."))
    return out
