"""System tuning of the hardware plan: memory, disk, Wi-Fi, audio, touchpad and watchdog settings, after CachyOS.

``TuningPlanMixin`` is mixed into ``hwplan._Planner``; ``tuning()`` runs after power() and memory(), whose decisions it reads. The values
come from CachyOS-Settings (https://github.com/CachyOS/CachyOS-Settings, GPL-3.0); the files and their comments are written here, not copied.
Every file works the same with OpenRC and systemd: OpenRC applies sysctl.d with its sysctl service (procps ``sysctl --system``) and
tmpfiles.d with the systemd-tmpfiles-setup service of systemd-utils (boot runlevel), and udev, modprobe.d and xorg.conf.d are read by the
same programs on both. What the init changes is who applies a file, and the summary names it. ``hardware.tuning`` turns all of it off.
Left out on purpose (docs/USER-GUIDE.md, "System tuning"): settings that cost security or battery, need CachyOS's own kernel or servers,
or exist only for systemd.
"""

from __future__ import annotations

import functools
from typing import Any

from gentoo_installer import desktops
from gentoo_installer.hardware import HardwareProfile
from gentoo_installer.manifest import InstallManifest

CREDIT = "written by the Gentoo installer, values from CachyOS-Settings"
DIST_KERNELS = ("gentoo-kernel", "gentoo-kernel-bin")
A_REGDB = "net-wireless/wireless-regdb"
ZONE_TAB = "/usr/share/zoneinfo/zone.tab"

SYSCTL_FILE = "/etc/sysctl.d/60-installer-tuning.conf"
IOSCHED_FILE = "/etc/udev/rules.d/59-installer-ioschedulers.rules"
THP_FILE = "/etc/tmpfiles.d/installer-thp.conf"
WIFI_FILE = "/etc/modprobe.d/installer-wifi-country.conf"
AUDIO_TLP_FILE = "/etc/tlp.d/80-installer-audio.conf"
AUDIO_RULES_FILE = "/etc/udev/rules.d/19-installer-audio-power.rules"
AUDIO_SCRIPT = "/usr/local/libexec/installer-audio-power"
TOUCHPAD_X11_FILE = "/etc/X11/xorg.conf.d/50-installer-touchpad.conf"
TOUCHPAD_SWAY_FILE = "/etc/sway/config.d/30-installer-touchpad.conf"
WATCHDOG_FILE = "/etc/modprobe.d/installer-watchdog.conf"

APPLIED_BY = {
    "openrc": {"sysctl": "OpenRC's sysctl service", "tmpfiles": "OpenRC's systemd-tmpfiles-setup service"},
    "systemd": {"sysctl": "systemd-sysctl", "tmpfiles": "systemd-tmpfiles-setup"},
}


def sysctl_text(zram: bool) -> str:
    lines = [f"# System tuning, {CREDIT}. Delete this file to go back to the kernel's values.",
             "# A leading \"-\" lets a kernel without the setting skip it (systemd-sysctl and procps sysctl both read it so).",
             "# Keep more of the directory and file-name cache in memory.",
             "vm.vfs_cache_pressure = 50",
             "# Write changed data to disk early and in smaller amounts, so a large copy does not stall the desktop.",
             "vm.dirty_bytes = 268435456",
             "vm.dirty_background_bytes = 67108864",
             "vm.dirty_writeback_centisecs = 1500",
             "# A longer queue for received network packets.",
             "-net.core.netdev_max_backlog = 4096",
             "# The NMI watchdog costs CPU time and power, and a desktop or laptop does not need it.",
             "-kernel.nmi_watchdog = 0"]
    if zram:
        lines += ["# zram swaps into compressed memory, which is fast: swap rather than drop the file cache, one page at a time.",
                  "vm.swappiness = 150",
                  "vm.page-cluster = 0"]
    return "\n".join(lines) + "\n"


IOSCHED_RULES = f"""# I/O schedulers by disk type, {CREDIT}. Delete this file to use the kernel's choice.
# Hard disks: BFQ keeps the desktop responsive while the disk is busy.
ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="sd[a-z]*", ATTR{{queue/rotational}}=="1", ATTR{{queue/scheduler}}="bfq"
# SATA SSDs, SD and eMMC cards: mq-deadline.
ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="sd[a-z]*|mmcblk[0-9]*", ATTR{{queue/rotational}}=="0", ATTR{{queue/scheduler}}="mq-deadline"
# NVMe SSDs: Kyber, which keeps reads quick while much is written.
ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="nvme[0-9]*n[0-9]*", ATTR{{queue/rotational}}=="0", ATTR{{queue/scheduler}}="kyber"
"""

THP_TEXT = f"""# Transparent huge pages, {CREDIT}. Delete this file to use the kernel's values.
# A program that asks for huge pages gets them without waiting for memory to be compacted.
w! /sys/kernel/mm/transparent_hugepage/defrag - - - - defer+madvise
# Split a huge page again when most of it (80 percent) is unused, so that memory is not wasted.
w! /sys/kernel/mm/transparent_hugepage/khugepaged/max_ptes_none - - - - 409
"""


def wifi_text(country: str, zone: str) -> str:
    return (f"# Wi-Fi country {country}, from the time zone {zone} ({CREDIT}): every channel allowed there can be used.\n"
            "# Change the two letters after a move, or delete this file to let the access points tell the country.\n"
            f"options cfg80211 ieee80211_regdom={country}\n")


AUDIO_TLP_TEXT = f"""# Audio power saving, {CREDIT}: off on the charger, where it makes some laptops crackle; TLP keeps it on battery.
SOUND_POWER_SAVE_ON_AC=0
"""

AUDIO_RULES = f"""# Audio power saving only on battery, {CREDIT}: on the charger it makes some laptops crackle.
ACTION=="add", SUBSYSTEM=="sound", KERNEL=="card*", DRIVERS=="snd_hda_intel", RUN+="{AUDIO_SCRIPT}"
ACTION=="add|change", SUBSYSTEM=="power_supply", ENV{{POWER_SUPPLY_TYPE}}=="Mains", RUN+="{AUDIO_SCRIPT}"
"""

AUDIO_SCRIPT_TEXT = f"""#!/bin/sh
# Audio power saving only on battery, {CREDIT}.
# udev runs this when the sound card appears and when the charger is plugged in or out ({AUDIO_RULES_FILE}).
param=/sys/module/snd_hda_intel/parameters/power_save
[ -w "$param" ] || exit 0
mains=0
online=0
for supply in /sys/class/power_supply/*; do
	[ "$(cat "$supply/type" 2>/dev/null)" = Mains ] || continue
	mains=1
	[ "$(cat "$supply/online" 2>/dev/null)" = 1 ] && online=1
done
if [ "$mains" = 1 ] && [ "$online" = 0 ]; then
	echo 1 > "$param"
else
	echo 0 > "$param"
fi
exit 0
"""

TOUCHPAD_X11 = f"""# Tap to click on the touchpad, {CREDIT}. The desktop's mouse and touchpad settings can still turn it off.
Section "InputClass"
	Identifier "installer touchpad tapping"
	MatchIsTouchpad "on"
	MatchDevicePath "/dev/input/event*"
	Driver "libinput"
	Option "Tapping" "on"
EndSection
"""

TOUCHPAD_SWAY = f"""# Tap to click on the touchpad, {CREDIT}.
input type:touchpad {{
    tap enabled
}}
"""

WATCHDOG_TEXT = f"""# Watchdog timers, {CREDIT}: a desktop or laptop does not use them, and without them it starts and stops a little faster.
# Delete this file to load them again.
blacklist iTCO_wdt
blacklist sp5100_tco
blacklist wdat_wdt
"""


@functools.lru_cache(maxsize=4)
def zone_countries(path: str = ZONE_TAB) -> dict[str, str]:
    """Time zone -> ISO country code, from tzdata's zone.tab ({} when it cannot be read)."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return {}
    table: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if line.startswith("#") or len(fields) < 3 or len(fields[0]) != 2 or not fields[0].isalpha():
            continue
        table.setdefault(fields[2], fields[0].upper())
    return table


def wifi_country(zone: str, table: dict[str, str] | None = None) -> str:
    """The country of a time zone, or "" for a zone with no single country (UTC, Etc/...) or one zone.tab does not list."""
    return (zone_countries() if table is None else table).get(zone, "")


class TuningPlanMixin:
    m: InstallManifest
    hw: HardwareProfile
    p: Any
    init: str
    desktop: str
    mobile: bool
    portable: bool
    vm: bool
    tuning_lines: list[str]

    def tuning(self) -> None:
        self.p.scalars["tuning"] = "on" if self.m.hardware.tuning else "off"
        if not self.m.hardware.tuning:
            return
        applied = APPLIED_BY.get(self.init, APPLIED_BY["openrc"])
        zram = self.p.scalars.get("zram") == "yes"
        dist = self.m.system.kernel in DIST_KERNELS
        self.file(SYSCTL_FILE, sysctl_text(zram))  # type: ignore[attr-defined]
        self.tuning_lines.append(f"Tuning: {SYSCTL_FILE}, memory and disk-write settings" + (" and swap settings for zram" if zram else "")
                                 + f" (applied by {applied['sysctl']})")
        if zram:
            self.p.kernel_cmdline.append("zswap.enabled=0")  # zswap in front of zram would compress the same pages twice
            self.tuning_lines.append("Tuning: zswap.enabled=0 on the kernel command line, because zram does the swapping")
        if dist:
            self.file(IOSCHED_FILE, IOSCHED_RULES)  # type: ignore[attr-defined]
            self.file(THP_FILE, THP_TEXT)  # type: ignore[attr-defined]
            self.tuning_lines.append(f"Tuning: {IOSCHED_FILE}, BFQ for hard disks, mq-deadline for SATA SSDs and cards, Kyber for NVMe (udev)")
            self.tuning_lines.append(f"Tuning: {THP_FILE}, huge pages for programs that ask for them (applied by {applied['tmpfiles']})")
        else:
            self.note("System tuning leaves the disk schedulers and huge pages alone: a kernel you configure yourself may not have them.")  # type: ignore[attr-defined]
        self.tuning_wifi()
        self.tuning_audio()
        self.tuning_touchpad()
        if not self.vm:
            self.file(WATCHDOG_FILE, WATCHDOG_TEXT)  # type: ignore[attr-defined]
            self.tuning_lines.append(f"Tuning: {WATCHDOG_FILE}, the unused watchdog timers are not loaded")

    def tuning_wifi(self) -> None:
        if not any(nic.kind == "wifi" for nic in self.hw.nics) and not self.portable:
            return
        zone = self.m.system.timezone
        country = wifi_country(zone)
        self.p.optional_atoms.append(A_REGDB)  # the kernel reads the rules of every country from it; without it only the strict world rules apply
        self.p.scalars["wifi_country"] = country
        if country:
            self.file(WIFI_FILE, wifi_text(country, zone))  # type: ignore[attr-defined]
            self.tuning_lines.append(f"Tuning: {WIFI_FILE}, Wi-Fi country {country} from the time zone, with the wireless regulatory database")
        else:
            self.tuning_lines.append(f"Tuning: the wireless regulatory database; the time zone {zone} names no single country, so the access points tell it")

    def tuning_audio(self) -> None:
        if self.p.scalars.get("power") == "tlp":
            self.file(AUDIO_TLP_FILE, AUDIO_TLP_TEXT)  # type: ignore[attr-defined]
            self.tuning_lines.append(f"Tuning: {AUDIO_TLP_FILE}, no audio power saving on the charger (TLP)")
            return
        self.file(AUDIO_RULES_FILE, AUDIO_RULES)  # type: ignore[attr-defined]
        self.file(AUDIO_SCRIPT, AUDIO_SCRIPT_TEXT, 0o755)  # type: ignore[attr-defined]
        self.tuning_lines.append(f"Tuning: {AUDIO_RULES_FILE}, audio power saving only on battery, which stops crackling (udev)")

    def tuning_touchpad(self) -> None:
        if not (self.mobile or self.portable) or not desktops.is_graphical(self.desktop):
            return
        entry = desktops.get(self.desktop)
        if self.desktop == "sway":
            self.file(TOUCHPAD_SWAY_FILE, TOUCHPAD_SWAY)  # type: ignore[attr-defined]
            self.tuning_lines.append(f"Tuning: {TOUCHPAD_SWAY_FILE}, tap to click on the touchpad")
        elif entry is not None and entry.session == "x11":
            self.file(TOUCHPAD_X11_FILE, TOUCHPAD_X11)  # type: ignore[attr-defined]
            self.tuning_lines.append(f"Tuning: {TOUCHPAD_X11_FILE}, tap to click on the touchpad")
