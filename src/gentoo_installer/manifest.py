from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields, is_dataclass
from functools import lru_cache
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable, ClassVar, get_args, get_origin, get_type_hints


SCHEMA_VERSION = 1
SECRET_KEYS = {"password", "root_password", "luks_passphrase", "wifi_password"}
# Keys of validate_sections(); GUI pages route errors by section, not wording.
VALIDATION_SECTIONS = ("general", "storage", "system", "locale", "portage", "network", "accounts", "options", "hardware")
CMDLINE_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:=,/+@%-]+")
# Shared with the daemon so that bad Portage input is rejected before any disk is touched.
ATOM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+.-]*/[A-Za-z0-9][A-Za-z0-9_+.-]*(?::[A-Za-z0-9_.-]+)?")
PROFILE_BASE = "default/linux/amd64/23.0"
PROFILE_RE = re.compile(r"default/linux/amd64/23\.0(?:/[a-z0-9][a-z0-9-]*)*")
KEYMAP_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,39}")
# language[_TERRITORY][.codeset][@modifier] (glibc also accepts the modifier first, as in de_DE@euro)
LOCALE_RE = re.compile(r"(?:[a-z]{2,3}(?:_[A-Z]{2})?(?:[.@][A-Za-z0-9-]+){0,2}|C(?:\.UTF-8)?|POSIX)")
# wget and the stage3 downloader only speak HTTP proxies; SOCKS proxies would fail late.
PROXY_SCHEMES = {"http", "https"}
KDE_APPS_CHOICES = {"core", "full"}
# One word of a package.use line: a flag, a negated flag or a USE_EXPAND heading such as "VIDEO_CARDS:".
USE_TOKEN_RE = re.compile(r"-?[A-Za-z0-9][A-Za-z0-9_+@.-]*|[A-Z][A-Z0-9_]*:")
REDACTED = "<redacted>"

# Choices of the settings that only allow a fixed list. Validation messages name the allowed values.
SWAP_KINDS = ("auto", "partition", "file", "zram", "none")
SWAP_CHOICES = ("recommended", "none", "custom", "hibernate")
ROOT_FILESYSTEMS = ("ext4", "xfs", "btrfs", "f2fs")
FSTAB_IDENTS = ("uuid", "label", "partuuid")
ATIME_MODES = ("noatime", "lazytime", "relatime")
OPTIMIZE_CHOICES = ("generic", "native")
BINHOST_VARIANTS = ("auto", "generic", "v3")
RSYNC_MODES = ("snapshot", "sync")
WORLD_UPDATE_CHOICES = ("auto", "now", "later")
KERNEL_CHOICES = ("gentoo-kernel-bin", "gentoo-kernel", "manual", "custom")
INITRAMFS_CHOICES = ("dracut", "ugrd", "none")
BOOTLOADER_CHOICES = ("grub", "systemd-boot", "efi-stub", "none")
SECURE_BOOT_MODES = ("off", "own-key", "create", "shim")
ROOT_MODES = ("same", "separate", "locked")
PRIVILEGE_TOOLS = ("sudo", "doas", "run0", "su")
LOGGER_CHOICES = ("sysklogd", "syslog-ng", "metalog", "none")
CRON_CHOICES = ("cronie", "dcron", "fcron", "bcron", "none")
TIMESYNC_CHOICES = ("chrony", "timesyncd", "none")
BROWSER_CHOICES = ("firefox", "none")
INPUT_METHODS = ("auto", "none", "ibus", "fcitx5")
GAMING_MODES = ("off", "flatpak", "native")
NVIDIA_EXTRAS = ("vaapi", "cuda", "nvtop", "persistenced")
HARDWARE_DEVICE_KEYS = ("openrazer", "openrgb", "game_rules", "solaar", "razer_frontend", "iio_sensors", "bolt", "wacom",
                        "fingerprint", "asus_tools", "thinkfan")
GENKERNEL_MESSAGE = (
    "genkernel is no longer offered: the current Gentoo Handbook builds the initramfs with dracut (or ugrd) and this installer "
    "has no genkernel support. Choose dracut, ugrd or none.")
F2FS_GRUB_MESSAGE = (
    "F2FS is meant for flash media such as USB sticks and SD cards (that is how the Gentoo Handbook describes it), and GRUB cannot "
    "reliably read /boot from it. Keep /boot on a separate ext4 partition, use systemd-boot or the EFI stub, or choose another root filesystem.")
SHIM_NOT_YET_MESSAGE = ("Secure Boot through shim and MOK is not available yet. Choose “Create a signing key for me” or “Use my own key”, "
                        "or turn Secure Boot off.")
SERIAL_CONSOLE_RE = re.compile(r"tty(?:S|USB|AMA)[0-9]{1,2}(?:,[0-9]{3,7}(?:[noe][5-8])?)?")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
LICENSE_TOKEN_RE = re.compile(r"-?@?[A-Za-z0-9][A-Za-z0-9_.+-]*")
KERNEL_CONFIG_LINE_RE = re.compile(r"CONFIG_[A-Z0-9_]+=\S.*|# CONFIG_[A-Z0-9_]+ is not set")
DOMAIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
HOSTS_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")

# Stage3 families and the desktops the installer can build on each. KDE Plasma needs the desktop USE flags that only
# the standard and desktop profiles enable (plasma-meta has unmet USE dependencies on no-multilib and hardened);
# GNOME and Xfce are unverified there. Minimal always works.
STAGE3_VARIANTS = frozenset(
    f"{family}-{init}" for family in ("amd64", "amd64-desktop", "amd64-nomultilib", "amd64-hardened") for init in ("openrc", "systemd"))
FAMILY_LABELS = {"standard": "Standard", "desktop": "Desktop", "nomultilib": "No multilib", "hardened": "Hardened"}
# The lighter desktops and window managers are listed for every family; the desktop registry (a later Wave 5 step) refines them.
_EXTRA_DESKTOPS = {"cinnamon", "mate", "lxqt", "gnome-light", "sway", "i3"}
DESKTOPS = ("minimal", "kde", "gnome", "xfce", "cinnamon", "mate", "lxqt", "gnome-light", "sway", "i3")
FAMILY_DESKTOP_SUPPORT = {
    "standard": {"kde", "gnome", "xfce"} | _EXTRA_DESKTOPS,
    "desktop": {"kde", "gnome", "xfce"} | _EXTRA_DESKTOPS,
    "nomultilib": {"gnome", "xfce"} | _EXTRA_DESKTOPS,
    "hardened": {"gnome", "xfce"} | _EXTRA_DESKTOPS,
}
UNVERIFIED_FAMILIES = {"nomultilib", "hardened"}
KDE_NEEDS_DESKTOP_STAGE = (
    "KDE Plasma needs a Standard or Desktop stage3: the Hardened and No-multilib profiles do not enable the desktop USE flags Plasma requires, "
    "so the package step would fail after your disk was erased. Choose a Standard stage3 or the Minimal system."
)
# The profiles Gentoo publishes under default/linux/amd64/23.0 (with and without /systemd).
PUBLISHED_PROFILES = frozenset(
    PROFILE_BASE + middle + suffix
    for middle in ("", "/desktop", "/desktop/plasma", "/desktop/gnome", "/hardened", "/no-multilib", "/no-multilib/hardened")
    for suffix in ("", "/systemd")
)
COMPANIONS_ON_MINIMAL = "HWMonux and PortageHub are graphical applications and cannot start on a minimal system; they only add build time and disk space there."


def stage_family(stage_variant: str) -> str:
    """standard | desktop | nomultilib | hardened, from the name of a published stage3 variant."""
    if "hardened" in stage_variant:
        return "hardened"
    if "nomultilib" in stage_variant:
        return "nomultilib"
    if "desktop" in stage_variant:
        return "desktop"
    return "standard"


def desktop_problem(stage_variant: str, desktop: str) -> str:
    """Why this desktop cannot be installed on this stage3 ("" when it can)."""
    if desktop in DESKTOPS and desktop != "minimal" and desktop not in FAMILY_DESKTOP_SUPPORT[stage_family(stage_variant)]:
        return KDE_NEEDS_DESKTOP_STAGE if desktop == "kde" else (
            f"{desktop.upper()} is not available on this stage3: choose a Standard or Desktop stage3 or the Minimal system.")
    return ""


def profile_problem(profile: str, init: str, stage_variant: str) -> str:
    """Why this Portage profile is wrong for the init system and stage3 ("" when it is fine or empty)."""
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


def derive_profile(init: str, desktop: str, *, multilib: bool = True, hardened: bool = False) -> str:
    """Return the published 23.0 profile for the selected init and profile family."""
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


def has_control_characters(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


@lru_cache(maxsize=1)
def _known_timezones() -> frozenset[str]:
    """Time zones of the running system; empty when the zone database is unavailable."""
    try:
        import zoneinfo
        return frozenset(zoneinfo.available_timezones())
    except Exception:  # noqa: BLE001 - a missing tzdata must not break validation
        return frozenset()


def _redact_url_password(url: str) -> str:
    """http://user:secret@host:3128 -> http://user:<redacted>@host:3128 (anything else unchanged).

    Everything up to the last "@" counts as the credentials, so a password with an "@" or a "/" in it
    is hidden completely; an address without a password is returned as it is.
    """
    scheme, separator, rest = url.partition("://")
    if not separator or not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*", scheme):
        return url
    userinfo, at, host = rest.rpartition("@")
    user, colon, password = userinfo.partition(":")
    if not at or not colon or password in {"", REDACTED}:
        return url
    return f"{scheme}://{user}:{REDACTED}@{host}"


def _unredact(value: Any) -> Any:
    """Every value that is exactly the redaction placeholder becomes an empty field again."""
    if isinstance(value, dict):
        return {key: _unredact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unredact(item) for item in value]
    return "" if value == REDACTED else value


def _cannot_be_stored(secret: str) -> bool:
    """Whether a password cannot be written to a command's input: a terminal in another character set leaves a lone surrogate."""
    try:
        secret.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _wifi_errors(ssid: str, password: str) -> list[str]:
    errors: list[str] = []
    if ssid:
        if has_control_characters(ssid) or not 1 <= len(ssid.encode("utf-8")) <= 32:
            errors.append("The Wi-Fi network name must be 1 to 32 bytes without control characters")
    elif password:
        errors.append("A Wi-Fi password needs a Wi-Fi network name")
    if password:
        printable = all(32 <= ord(char) < 127 for char in password)
        if not ((printable and 8 <= len(password) <= 63) or re.fullmatch(r"[0-9a-fA-F]{64}", password)):
            errors.append("The Wi-Fi password must be 8 to 63 printable ASCII characters or 64 hexadecimal digits")
    return errors


def _proxy_errors(proxy: str) -> list[str]:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(proxy)
        port = parts.port
    except ValueError:
        return ["The proxy address is not a valid URL"]
    if "<redacted>" in proxy:
        return ["Replace the redacted proxy password before installing"]
    if has_control_characters(proxy) or " " in proxy or parts.scheme not in PROXY_SCHEMES or not parts.hostname \
            or parts.path not in {"", "/"} or parts.query or parts.fragment or (port is not None and port == 0):
        return ["The proxy must look like http://host:port (http or https; SOCKS proxies are not supported)"]
    return []


def _make_opts_errors(make_opts: str) -> list[str]:
    """MAKEOPTS may only contain job-count options; anything else ends up in a shell variable."""
    message = ["MAKEOPTS may contain only job options such as -j4, -l8, --jobs=4 or --load-average=8"]
    if has_control_characters(make_opts):
        return message
    tokens = make_opts.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not re.fullmatch(r"-j[0-9]*|-l[0-9]*(?:\.[0-9]+)?|--jobs(?:=[0-9]+)?|--load-average(?:=[0-9]+(?:\.[0-9]+)?)?", token):
            return message
        if token in {"-j", "--jobs", "-l", "--load-average"} and index + 1 < len(tokens) \
                and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", tokens[index + 1]):
            index += 1
        index += 1
    return []


class ManifestError(ValueError):
    pass


def _choice_error(value: Any, choices: Sequence[str], label: str) -> list[str]:
    """One plain message when value is not one of choices."""
    if value in choices:
        return []
    return [f"{label} must be {_or_list(choices)}"]


def _or_list(items: Sequence[str]) -> str:
    return ", ".join(items[:-1]) + " or " + items[-1] if len(items) > 1 else items[0]


def _config_line_ok(line: str) -> bool:
    """A kernel .config line: CONFIG_X=value, "# CONFIG_X is not set", a plain comment or nothing."""
    line = line.strip()
    return not line or bool(KERNEL_CONFIG_LINE_RE.fullmatch(line)) or (line.startswith("#") and not line.startswith("# CONFIG_"))


def _is_https_url(url: str) -> bool:
    return url.startswith("https://") and not has_control_characters(url) and " " not in url


def _flag_to(true_value: str, false_value: str) -> Callable[[Any], Any]:
    """Migration of a setting that used to be on or off: true and false become the matching choice; anything else is kept."""
    def migrate(value: Any) -> Any:
        return true_value if value is True else false_value if value is False else value
    return migrate


def _renamed(old: str, new: str) -> Callable[[Any], Any]:
    return lambda value: new if value == old else value


class _Migrating:
    """Settings whose type was widened keep loading: every assignment, also a later ``section.name = value``, goes through ``_MIGRATE``.

    A bool becomes the choice of the same meaning (logger/cron/world_update) and an old spelling becomes the new one
    (gpu_driver "open", nvk "yes"/"no"), so code and old settings files that still use the old value keep working.
    """

    _MIGRATE: ClassVar[dict[str, Callable[[Any], Any]]] = {}

    def __setattr__(self, name: str, value: Any) -> None:
        migrate = self._MIGRATE.get(name)
        object.__setattr__(self, name, migrate(value) if migrate else value)


@dataclass
class Partition:
    """One manual-mode row: an existing partition or volume and what to do with it.

    A swap row has filesystem "swap" and an empty mountpoint. label is applied when the
    row is formatted. size_mib and flags are kept for draft compatibility only: the
    installer uses existing partitions as they are, so only 0 and [] validate. The same
    goes for the filesystem "none" (a row that mounts nothing): a partition that is not
    listed is never touched, so validate() asks for such a row to be removed.
    """

    device: str = ""
    mountpoint: str = "/"
    filesystem: str = "ext4"
    size_mib: int = 0
    format: bool = True
    label: str = ""
    flags: list[str] = field(default_factory=list)

    # Longest label (bytes) each mkfs/mkswap accepts.
    LABEL_LIMITS: ClassVar[dict[str, int]] = {"ext4": 16, "xfs": 12, "btrfs": 255, "vfat": 11, "swap": 15, "f2fs": 512}

    def label_problem(self) -> str:
        """Why ``label`` cannot be written ("" when it is fine or empty)."""
        if not self.label:
            return ""
        name = self.device or "a partition"
        limit = self.LABEL_LIMITS.get(self.filesystem)
        if limit is None:
            return f"A label cannot be set on {name}: filesystem {self.filesystem} has none"
        if not self.format:
            return f"The label of {name} would be ignored: labels are only written when the partition is formatted"
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in self.label) or self.label.startswith("-"):
            return f"The label of {name} may not contain spaces or control characters, or start with a dash"
        if self.filesystem == "vfat" and not re.fullmatch(r"[A-Za-z0-9_-]+", self.label):
            return f"The label of {name} may contain only letters, digits, dashes and underscores (FAT)"
        if len(self.label.encode()) > limit:
            return f"The label of {name} is too long: {self.filesystem} labels hold at most {limit} characters"
        return ""


@dataclass
class Storage:
    mode: str = "erase"
    device: str = ""
    device_id: str = ""
    # "" (legacy: device_id is a serial or WWN) | "serial" | "wwn" | "path".
    # For "path" the user approved a disk without a stable serial/WWN and
    # device_id is "<by-path link name>|<size in bytes>|<model>".
    device_id_kind: str = ""
    firmware: str = "uefi"
    table: str = "gpt"
    filesystem: str = "ext4"
    swap_mib: int = 4096
    separate_home: bool = False
    home_mib: int = 0
    luks: bool = False
    luks_passphrase: str = ""
    lvm: bool = False
    raid_level: str = ""
    partitions: list[Partition] = field(default_factory=list)
    # Manual mode: serial/WWN of every OTHER disk a partition row lives on (device_id keeps
    # pinning `device`). An entry "<by-path link name>|<size in bytes>|<model>" approves a disk
    # that has neither. The daemon refuses rows on a disk nobody approved. Erase mode leaves it empty.
    additional_device_ids: list[str] = field(default_factory=list)
    # Consent to deactivate LVM, dm-crypt and md volumes that sit on the target disk, and to erase
    # a disk that belongs to a volume group or array spanning other disks.
    release_holders: bool = False
    # Manual mode with a preserved (format=false) root that already holds a system: consent to
    # install the new system over its files (a backup of /etc is taken first).
    overwrite_existing_system: bool = False
    # Wave 5 settings (the effect of each is implemented by a later step; the defaults reproduce the earlier behaviour).
    # A /boot partition of its own (Advanced). Guided installs get one only where they always did, for example with LUKS.
    separate_boot: bool = False
    # "auto" keeps today's choice of swap (a partition, or a file inside an encrypted root).
    swap_kind: str = "auto"
    # "custom" uses swap_mib as it is; "recommended" lets the installer size it; "hibernate" sizes it for hibernation.
    swap_choice: str = "custom"
    # How /etc/fstab names a filesystem: by UUID (today), by label or by partition UUID.
    fstab_ident: str = "uuid"
    # The access-time mount option written to /etc/fstab (today noatime).
    atime: str = "noatime"
    # Mount /boot only on demand (noauto).
    boot_noauto: bool = False

    def raid_problem(self) -> str:
        """Why the RAID setting cannot work ("" when there is none).

        The installer does not create arrays: it installs onto an array that was assembled
        beforehand (mdadm) and is selected as a manual-mode target.
        """
        if not self.raid_level:
            return ""
        if self.mode != "manual":
            return "RAID is not created by the installer: assemble the array first (mdadm), then use manual mode with its /dev/md device"
        if not self.device.startswith("/dev/md"):
            return "RAID installations need the assembled /dev/md device as the target disk"
        return ""

    def encryption_summary(self) -> str:
        """What the encryption setting really covers, for the Review page and the install receipt."""
        if not self.luks:
            return "Not enabled"
        if self.mode == "manual":
            plain = sorted({
                p.mountpoint or "swap" for p in self.partitions
                if p.mountpoint not in {"/", "/boot", "/efi", "/boot/efi"} and not p.device.startswith("/dev/mapper/")
            })
            text = "LUKS2 on the root volume only"
            return text + (f"; NOT encrypted: {', '.join(plain)}" if plain else "") + " (/boot stays readable for the bootloader)"
        # Swap of an encrypted install is a file inside the encrypted root (or absent): never a plain partition.
        swap = "; swap is never stored unencrypted" if self.swap_mib else ""
        return f"LUKS2 on the root volume{swap} (/boot stays readable for the bootloader)"


@dataclass
class System(_Migrating):
    init: str = "openrc"
    stage_variant: str = "amd64-openrc"
    # "" derives the profile from init, desktop, multilib and stage variant
    # (see derive_profile); an explicit published profile path is used as is.
    profile: str = ""
    multilib: bool = True
    desktop: str = "kde"
    # "manual" is meant for command-line (headless) installs: the installer stops
    # after the initramfs configuration so a kernel can be installed by hand.
    kernel: str = "gentoo-kernel-bin"
    initramfs: str = "dracut"
    bootloader: str = "grub"
    secure_boot: bool = False
    secure_boot_key: str = ""
    secure_boot_certificate: str = ""
    uki: bool = False
    dual_boot: bool = False
    locale: str = "en_US.UTF-8"
    timezone: str = "UTC"
    keymap: str = "us"
    # X11/Wayland (XKB) keyboard for display managers and desktop sessions;
    # several layouts are comma-separated.
    keyboard_layout: str = "us"
    keyboard_variant: str = ""
    keyboard_model: str = "pc105"
    keyboard_options: str = ""
    rtc_local: bool = False
    console_font: str = ""
    # Extra kernel arguments appended after the installer's own (no root=/init=).
    kernel_cmdline_extra: str = ""
    # auto | now | later. Saved files with true/false load as now/later.
    world_update: str = "later"
    # Wave 5 settings (effects follow in later steps; defaults reproduce the earlier behaviour).
    # Pick the stage3 file that matches the other choices automatically.
    stage_auto: bool = True
    # The kernel .config lines added to a "custom" kernel (CONFIG_X=y lines only).
    kernel_config_snippet: str = ""
    # dracut hostonly mode (today the image is always generic: "no").
    hostonly: bool = False
    # Also build a generic Unified Kernel Image next to the machine-specific one.
    generic_uki: bool = False
    # off | own-key | create | shim. An old file with secure_boot true loads as own-key.
    secure_boot_mode: str = "off"
    # Kernel console on a serial port, for example ttyS0,115200 ("" for none).
    serial_console: str = ""
    domain: str = ""
    # Extra /etc/hosts lines: "address name [alias ...]".
    hosts_extra: list[str] = field(default_factory=list)
    # Locales generated besides system.locale.
    extra_locales: list[str] = field(default_factory=list)

    _MIGRATE: ClassVar[dict[str, Callable[[Any], Any]]] = {"world_update": _flag_to("now", "later")}

    def __post_init__(self) -> None:
        if self.secure_boot and self.secure_boot_mode == "off":
            self.secure_boot_mode = "own-key"  # the only Secure Boot an old file could ask for: its own key and certificate
        elif self.secure_boot_mode != "off" and not self.secure_boot and self.secure_boot_mode in SECURE_BOOT_MODES:
            self.secure_boot = True  # a mode other than off is a request for Secure Boot
        object.__setattr__(self, "_ready", True)

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name == "secure_boot" and value and self.secure_boot_mode == "off" and getattr(self, "_ready", False):
            object.__setattr__(self, "secure_boot_mode", "own-key")
        elif name == "secure_boot" and not value and getattr(self, "_ready", False):
            object.__setattr__(self, "secure_boot_mode", "off")
        elif name == "secure_boot_mode" and getattr(self, "_ready", False) and value in ("own-key", "create", "shim"):
            object.__setattr__(self, "secure_boot", True)
        elif name == "secure_boot_mode" and getattr(self, "_ready", False) and value == "off":
            object.__setattr__(self, "secure_boot", False)


@dataclass
class Portage:
    mirror: str = "https://distfiles.gentoo.org"
    rsync_mirror: str = ""
    binhost: bool = True
    binhost_url: str = "https://distfiles.gentoo.org/releases/amd64/binpackages/23.0/x86-64/"
    # "" chooses -j/-l from the CPU count and memory of the machine that runs the install.
    make_opts: str = ""
    cflags: str = "-O2 -pipe"
    cxxflags: str = "${CFLAGS}"
    rustflags: str = ""
    emerge_opts: str = "--ask=n --verbose=y"
    features: list[str] = field(default_factory=list)
    accept_license: str = "@FREE"
    use_flags: list[str] = field(default_factory=list)
    package_use: dict[str, list[str]] = field(default_factory=dict)
    # "" and "auto" keep the profile defaults (and any hardware-detected value);
    # a value replaces them.
    cpu_flags: str = ""
    video_cards: str = ""
    # Extra HTTPS mirrors tried in order when the primary stage3 mirror keeps failing.
    mirror_fallbacks: list[str] = field(default_factory=list)
    # Wave 5 settings (effects follow in later steps; defaults reproduce the earlier behaviour).
    optimize: str = "generic"  # generic | native (-march=native)
    binhost_variant: str = "auto"  # auto | generic | v3
    mirrors: list[str] = field(default_factory=list)  # HTTPS mirrors, tried in order (mirror and mirror_fallbacks stay valid)
    rsync_mode: str = "snapshot"  # snapshot (emerge-webrsync, today) | sync (emerge --sync)
    license_groups: list[str] = field(default_factory=list)  # ACCEPT_LICENSE groups; accept_license stays the source of truth
    depclean: bool = False  # run emerge --depclean at the end
    profile_override: str = ""  # a published profile path that replaces the derived one ("" for none)


@dataclass
class LiveStatic:
    """A fixed address for the live session's own network (empty by default: DHCP)."""

    address: str = ""
    gateway: str = ""
    dns: list[str] = field(default_factory=list)

    def is_set(self) -> bool:
        return bool(self.address or self.gateway or self.dns)


@dataclass
class Network:
    manager: str = "networkmanager"
    interface: str = ""
    dhcp: bool = True
    address: str = ""
    gateway: str = ""
    dns: list[str] = field(default_factory=list)
    proxy: str = ""
    wifi_ssid: str = ""
    wifi_password: str = ""
    # Copy the live session's NetworkManager connections (including saved
    # Wi-Fi secrets) into the target. Off unless the user asks for it.
    import_live_profiles: bool = False
    # Wave 5 settings (effects follow in later steps; defaults reproduce the earlier behaviour).
    keep_proxy: bool = True  # keep the proxy in the new system's make.conf (it is written there today)
    live_static: LiveStatic = field(default_factory=LiveStatic)
    wireless_tools: bool = False
    ppp: bool = False
    # The uuid of a live NetworkManager Wi-Fi connection that is copied, with its secret, into the new system.
    copy_live_wifi: str = ""


@dataclass
class Accounts:
    hostname: str = "gentoo"
    username: str = "gentoo"
    full_name: str = ""
    password: str = ""
    root_password: str = ""
    privilege: str = "sudo"
    groups: list[str] = field(default_factory=lambda: ["audio", "video", "wheel"])
    disable_root_login: bool = False
    ssh_authorized_keys: list[str] = field(default_factory=list)
    # Wave 5 settings (effects follow in later steps).
    # same: root uses the user's password | separate: root has its own (today) | locked: no root login (disable_root_login).
    root_mode: str = "separate"
    ssh_password_login: bool = True
    ssh_root_login: bool = False

    def __post_init__(self) -> None:
        if self.disable_root_login:
            self.root_mode = "locked"
        elif self.root_mode == "locked":
            self.disable_root_login = True
        object.__setattr__(self, "_ready", True)  # from here on the two settings follow each other

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
        if getattr(self, "_ready", False):
            if name == "disable_root_login":
                if value:
                    object.__setattr__(self, "root_mode", "locked")
                elif self.root_mode == "locked":
                    object.__setattr__(self, "root_mode", "separate")
            elif name == "root_mode":
                object.__setattr__(self, "disable_root_login", value == "locked")

    def root_locked(self) -> bool:
        return self.disable_root_login or self.root_mode == "locked"


@dataclass
class Options(_Migrating):
    install_hwmonux: bool = True
    install_portagehub: bool = True
    pipewire: bool = True
    bluetooth: bool = True
    printing: bool = True
    flatpak: bool = False
    ssh: bool = True  # sshd is enabled on the installed system (it was off before Wave 5)
    cron: str = "cronie"  # cronie | dcron | fcron | bcron | none; true/false load as cronie/none
    file_indexing: bool = False
    firmware: bool = True
    microcode: bool = True
    shell_completion: bool = True
    # sysklogd | syslog-ng | metalog | none. Saved files with true/false load as syslog-ng/none.
    logger: str = "syslog-ng"
    # KDE application set: "core" (file manager, terminal, editor, help) or
    # "full" (the complete kde-apps suite: games, education, multimedia, ...).
    kde_apps: str = "core"
    extra_packages: list[str] = field(default_factory=list)
    # Wave 5 settings (effects follow in later steps; defaults reproduce the earlier behaviour).
    timesync: str = "chrony"  # chrony | timesyncd | none. The old yes/no setting "chrony" still loads (true: chrony, false: none).
    gentoolkit: bool = False
    eclean_kernel: bool = False
    mlocate: bool = False
    journal_persistent: bool = False
    browser: str = "firefox"  # firefox | none (nothing is installed by this setting yet)
    flathub: bool = False
    input_method: str = "auto"  # auto | none | ibus | fcitx5 ("auto" installs nothing yet)
    accessibility: bool = False
    gaming: str = "off"  # off | flatpak | native
    gaming_tweaks: bool = True  # read only when gaming is not off
    gaming_windows_tools: bool = False
    gaming_accept_external: bool = False  # native: accept the Steam overlay, ~amd64 packages and the download from Valve

    _MIGRATE: ClassVar[dict[str, Callable[[Any], Any]]] = {"logger": _flag_to("syslog-ng", "none"), "cron": _flag_to("cronie", "none")}

    @property
    def chrony(self) -> bool:
        """The earlier yes/no setting for time synchronisation (timesync is the real setting)."""
        return self.timesync == "chrony"

    @chrony.setter
    def chrony(self, value: bool) -> None:
        self.timesync = "chrony" if value else "none"

    @classmethod
    def migrate_raw(cls, raw: dict[str, Any]) -> dict[str, Any]:
        """Settings-file keys of earlier versions: ``chrony`` becomes ``timesync``."""
        if "chrony" not in raw:
            return raw
        raw = dict(raw)
        chrony = raw.pop("chrony")
        if "timesync" not in raw and isinstance(chrony, bool):
            raw["timesync"] = "chrony" if chrony else "none"
        return raw

    def is_on(self, name: str) -> bool:
        """A yes/no view of any option: a choice that is "none" counts as off (for check boxes and summaries)."""
        value = getattr(self, name)
        return value if isinstance(value, bool) else value != "none"

    @property
    def logger_enabled(self) -> bool:
        return self.logger != "none"

    @property
    def cron_enabled(self) -> bool:
        return self.cron != "none"


@dataclass
class Hardware(_Migrating):
    """Hardware-dependent choices; "auto" means "decide from detection"."""

    target: str = "this"  # "this" (this machine) | "portable" (generic, movable install)
    gpu_driver: str = "auto"  # "auto" | "nvidia" | "nouveau" | "basic" (a saved "open" loads as "nouveau")
    nvidia_branch: str = "auto"  # "auto" | three-digit driver branch such as "580"
    nvidia_license_accepted: bool = False
    nvk: str = "auto"  # "auto" | "on" | "off" (saved "yes"/"no" load as on/off)
    prime: str = "auto"  # "auto" | "off" | "offload"
    cpu_flags_mode: str = "auto"  # "auto" | "detect" | "profile" | "manual" (portage.cpu_flags)
    power: str = "auto"  # "auto" | "ppd" | "tlp" | "none"
    thermald: str = "auto"  # "auto" | "yes" | "no"
    fingerprint: bool = False
    fwupd: bool = False
    guest_tools: str = "auto"  # "auto" | "none"
    zram: str = "auto"  # "auto" | "yes" | "no"
    zram_percent: int = 50
    hibernate: bool = False
    trim: str = "auto"  # "auto" | "weekly" | "off"
    luks_discard: bool = False
    sof_firmware: str = "auto"  # "auto" | "yes" | "no"
    printer_drivers: list[str] = field(default_factory=list)  # subset of PRINTER_DRIVERS
    hidpi: str = "auto"  # "auto" | "off" | "dpi:<96..288>"
    removable_fallback: str = "auto"  # "auto" | "always" | "if-missing" | "never"
    pipewire_alsa: bool = False
    # Wave 5 settings (effects follow in later steps; defaults reproduce the earlier behaviour).
    nvidia_modules: str = "auto"  # auto | open | closed
    safe_graphics_entry: bool = True
    nvidia_rtd3: str = "auto"  # auto | on | off
    nvidia_extras: list[str] = field(default_factory=list)  # subset of vaapi, cuda, nvtop, persistenced
    nvidia_cuda_license_accepted: bool = False  # "cuda" in nvidia_extras needs it
    signed_modules: str = "off"  # off | auto | kernel_key
    devices: dict[str, bool] = field(default_factory=dict)  # opt-ins for HARDWARE_DEVICE_KEYS
    battery_charge_limit: int = 0  # 0 (none) | 60 | 80
    lid_action: str = "suspend"  # suspend | lock | ignore
    vendor_workarounds: bool = False

    _MIGRATE: ClassVar[dict[str, Callable[[Any], Any]]] = {
        "gpu_driver": _renamed("open", "nouveau"),
        "nvk": lambda value: {"yes": "on", "no": "off"}.get(value, value) if isinstance(value, str) else value,
    }


PRINTER_DRIVERS = {"gutenprint", "hplip"}
HARDWARE_CHOICES = {
    "target": {"this", "portable"},
    "gpu_driver": {"auto", "nvidia", "nouveau", "basic"},
    "nvk": {"auto", "on", "off"},
    "nvidia_modules": {"auto", "open", "closed"},
    "nvidia_rtd3": {"auto", "on", "off"},
    "signed_modules": {"off", "auto", "kernel_key"},
    "lid_action": {"suspend", "lock", "ignore"},
    "prime": {"auto", "off", "offload"},
    "cpu_flags_mode": {"auto", "detect", "profile", "manual"},
    "power": {"auto", "ppd", "tlp", "none"},
    "thermald": {"auto", "yes", "no"},
    "guest_tools": {"auto", "none"},
    "zram": {"auto", "yes", "no"},
    "trim": {"auto", "weekly", "off"},
    "sof_firmware": {"auto", "yes", "no"},
    "removable_fallback": {"auto", "always", "if-missing", "never"},
}


def _is_path_identity(value: str) -> bool:
    """Whether value has the shape of a by-path identity: "<link name>|<size in bytes>|<model>"."""
    parts = value.split("|")
    return len(parts) == 3 and bool(parts[0]) and "/" not in parts[0] and parts[1].isdigit() and not any(c in value for c in "\n\r\0")


@dataclass
class InstallManifest:
    storage: Storage = field(default_factory=Storage)
    system: System = field(default_factory=System)
    portage: Portage = field(default_factory=Portage)
    network: Network = field(default_factory=Network)
    accounts: Accounts = field(default_factory=Accounts)
    options: Options = field(default_factory=Options)
    hardware: Hardware = field(default_factory=Hardware)
    version: int = SCHEMA_VERSION

    def to_dict(self, redact: bool = False) -> dict[str, Any]:
        def scrub(value: Any, key: str = "") -> Any:
            if redact and key in SECRET_KEYS and value:
                return "<redacted>"
            if redact and key == "proxy" and isinstance(value, str):
                return _redact_url_password(value)
            if is_dataclass(value):
                return {f.name: scrub(getattr(value, f.name), f.name) for f in fields(value)}
            if isinstance(value, dict):
                return {k: scrub(v, k) for k, v in value.items()}
            if isinstance(value, list):
                return [scrub(v) for v in value]
            return value

        return scrub(self)

    def to_json(self, redact: bool = False) -> str:
        return json.dumps(self.to_dict(redact), indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> InstallManifest:
        if not isinstance(raw, dict):
            raise ManifestError("The installation manifest must be a JSON object")

        def convert(tp: Any, value: Any, name: str) -> Any:
            origin = get_origin(tp)
            if origin is list:
                if not isinstance(value, list):
                    raise ManifestError(f"{name} must be a list")
                item_type = get_args(tp)[0]
                return [convert(item_type, item, f"{name}[{index}]") for index, item in enumerate(value)]
            if origin is dict:
                if not isinstance(value, dict):
                    raise ManifestError(f"{name} must be an object")
                key_type, value_type = get_args(tp)
                return {
                    convert(key_type, key, f"{name} key"): convert(value_type, item, f"{name}.{key}")
                    for key, item in value.items()
                }
            if is_dataclass(tp):
                if not isinstance(value, dict):
                    raise ManifestError(f"{name} must be an object")
                hints = get_type_hints(tp)
                allowed = {f.name: hints[f.name] for f in fields(tp)}
                if hasattr(tp, "migrate_raw"):
                    value = tp.migrate_raw(value)
                migrations = getattr(tp, "_MIGRATE", {})
                if migrations:
                    value = {key: migrations[key](item) if key in migrations else item for key, item in value.items()}
                unknown = set(value) - set(allowed)
                if unknown:
                    raise ManifestError(f"Unknown {name} fields: {', '.join(sorted(unknown))}")
                if tp is Partition and "format" not in value:
                    # The default would silently format (and erase) a partition the author may have meant to keep.
                    raise ManifestError(f"{name}.format must be stated explicitly: true formats the partition, false keeps its data")
                return tp(**{key: convert(allowed[key], item, f"{name}.{key}") for key, item in value.items()})
            if tp is bool:
                if type(value) is not bool:
                    raise ManifestError(f"{name} must be true or false")
            elif tp is int:
                if type(value) is not int:
                    raise ManifestError(f"{name} must be an integer")
            elif tp is str and not isinstance(value, str):
                raise ManifestError(f"{name} must be a string")
            return value

        return convert(cls, raw, "manifest")

    @classmethod
    def from_json(cls, text: str) -> InstallManifest:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"Invalid manifest JSON: {exc.msg}") from exc
        manifest = cls.from_dict(raw)
        manifest.require_valid()
        return manifest

    @classmethod
    def load(cls, path: str | Path) -> InstallManifest:
        return cls.from_json(Path(path).read_text())

    @classmethod
    def load_redacted(cls, path: str | Path) -> InstallManifest:
        """Read a settings file that was saved without its secrets.

        Every value that is exactly "<redacted>" becomes an empty field, and the manifest is not
        validated: the passwords have to be typed again. The password inside a proxy address stays
        "<redacted>" (validate() refuses it) so nobody installs with a proxy that cannot sign in.
        """
        try:
            raw = json.loads(Path(path).read_text())
        except (OSError, ValueError) as exc:
            raise ManifestError(f"Cannot read the settings file: {exc}") from exc
        return cls.from_dict(_unredact(raw))

    def save(self, path: str | Path, redact: bool = True) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json(redact) + "\n")
        target.chmod(0o600)

    def validate(self) -> list[str]:
        return [error for errors in self.validate_sections().values() for error in errors]

    def validate_sections(self) -> dict[str, list[str]]:
        """Return validation errors grouped by VALIDATION_SECTIONS key."""
        return {
            "general": self._validate_general(),
            "storage": self._validate_storage(),
            "system": self._validate_system(),
            "locale": self._validate_locale(),
            "portage": self._validate_portage(),
            "network": self._validate_network(),
            "accounts": self._validate_accounts(),
            "options": self._validate_options(),
            "hardware": self._validate_hardware(),
        }

    def _validate_general(self) -> list[str]:
        errors: list[str] = []
        for section in (self.storage, self.system, self.portage, self.network, self.accounts, self.options):
            for key in SECRET_KEYS:
                if getattr(section, key, None) == "<redacted>":
                    errors.append(f"Replace the redacted secret in {key} before installing")
        if self.version != SCHEMA_VERSION:
            errors.append(f"Unsupported manifest version {self.version}")
        return errors

    def _validate_storage(self) -> list[str]:
        errors: list[str] = []
        storage = self.storage
        if storage.mode not in {"erase", "manual"}:
            errors.append("Storage mode must be erase or manual")
        if not storage.device.startswith("/dev/") or storage.device in {"/dev/null", "/dev/zero", "/dev/random"}:
            errors.append("Select a real target disk")
        if "\n" in storage.device or ".." in storage.device:
            errors.append("The target disk path is invalid")
        if storage.firmware not in {"uefi", "bios"}:
            errors.append("Firmware must be uefi or bios")
        if storage.table not in {"gpt", "dos"}:
            errors.append("Partition table must be gpt or dos")
        if storage.filesystem not in ROOT_FILESYSTEMS:
            errors.append("Root filesystem must be ext4, XFS, Btrfs or F2FS")
        elif storage.filesystem == "f2fs" and storage.mode == "erase" and self.system.bootloader == "grub" and not storage.separate_boot:
            errors.append(F2FS_GRUB_MESSAGE)
        errors.extend(_choice_error(storage.swap_kind, SWAP_KINDS, "Swap kind"))
        errors.extend(_choice_error(storage.swap_choice, SWAP_CHOICES, "Swap choice"))
        errors.extend(_choice_error(storage.fstab_ident, FSTAB_IDENTS, "The way fstab names disks"))
        errors.extend(_choice_error(storage.atime, ATIME_MODES, "The access-time option"))
        if storage.swap_mib < 0 or storage.home_mib < 0:
            errors.append("Partition sizes cannot be negative")
        if storage.mode == "manual":
            if not any(p.mountpoint == "/" for p in storage.partitions):
                errors.append("Manual storage needs a root (/) mountpoint")
            root_partition = next((p for p in storage.partitions if p.mountpoint == "/"), None)
            if root_partition and not root_partition.format and (storage.luks or storage.lvm):
                errors.append("Preserved root cannot create a new LUKS/LVM stack; select an already opened volume with stack creation disabled")
            for partition in storage.partitions:
                name = partition.mountpoint or partition.filesystem
                if not partition.device.startswith("/dev/") or any(c.isspace() or ord(c) < 32 for c in partition.device) or ".." in partition.device:
                    errors.append(f"Invalid partition device for {name}")
                if partition.filesystem == "swap":
                    if partition.mountpoint:
                        errors.append(f"A swap partition has no mountpoint; leave it empty instead of {partition.mountpoint}")
                elif partition.filesystem == "none":
                    # Older drafts and hand-written files: such a row did nothing the installer can finish (no mount, no fstab type).
                    errors.append(f"{partition.device or 'A partition'}: the filesystem \"none\" is not supported. Remove the row; partitions that are not listed are never touched")
                elif not partition.mountpoint:
                    errors.append(f"Invalid mountpoint for {partition.device or 'a partition'}: choose where it is mounted (for example /home), or remove the row")
                elif not partition.mountpoint.startswith("/") or ".." in partition.mountpoint or any(c.isspace() or ord(c) < 32 for c in partition.mountpoint):
                    errors.append(f"Invalid mountpoint: {partition.mountpoint}")
                if partition.filesystem not in {"ext4", "xfs", "btrfs", "f2fs", "vfat", "swap", "none"}:
                    errors.append(f"Unsupported filesystem: {partition.filesystem}")
                elif partition.filesystem == "f2fs" and partition.mountpoint == "/boot" and self.system.bootloader == "grub":
                    errors.append(F2FS_GRUB_MESSAGE)
                if partition.size_mib:
                    errors.append(f"Partition sizes are not supported for {partition.device}: the installer uses existing partitions as they are (leave the size at 0)")
                if partition.flags:
                    errors.append(f"Partition flags are not supported for {partition.device}: set them with a partitioning tool such as GParted before installing")
                label_problem = partition.label_problem()
                if label_problem:
                    errors.append(label_problem)
            if storage.firmware == "uefi" and not any(p.mountpoint in {"/efi", "/boot/efi"} for p in storage.partitions):
                errors.append("UEFI installations need an EFI System Partition mounted at /efi or /boot/efi")
            if storage.luks and not any(p.mountpoint == "/boot" and p.filesystem not in {"swap", "none"} for p in storage.partitions):
                errors.append("Manual LUKS installations need a separate /boot partition outside the encrypted root")
            if storage.table == "dos":
                if len(storage.partitions) > 4:
                    errors.append("DOS partition tables support at most four primary partitions; use GPT or reduce the layout")
        else:
            if storage.separate_home and storage.home_mib <= 0:
                errors.append("A separate home partition needs a positive size")
            if storage.luks and storage.separate_home:
                errors.append(
                    "A separate /home partition would be stored unencrypted next to the encrypted root: "
                    "turn off 'separate /home' (it then lives inside the encrypted root) or turn encryption off"
                )
            if storage.table == "dos":
                required_partitions = 1 + int(storage.firmware == "uefi") + int(storage.swap_mib > 0) + int(storage.separate_home) + int(storage.luks)
                if required_partitions > 4:
                    errors.append("This guided DOS layout needs more than four primary partitions; use GPT or reduce the layout")
        if storage.luks and len(storage.luks_passphrase) < 8:
            errors.append("The LUKS passphrase must contain at least 8 characters")
        if storage.luks and _cannot_be_stored(storage.luks_passphrase):
            errors.append("The LUKS passphrase contains a character that cannot be stored; type it again")
        if storage.raid_level and storage.raid_level not in {"0", "1", "5", "6", "10"}:
            errors.append("Unsupported RAID level")
        elif storage.raid_problem():
            errors.append(storage.raid_problem())
        if storage.device_id_kind not in {"", "serial", "wwn", "path"}:
            errors.append("Unknown disk identity kind")
        if storage.device_id_kind in {"serial", "wwn"} and not storage.device_id:
            errors.append(f"The disk identity kind is {storage.device_id_kind} but no disk {storage.device_id_kind} is given")
        if storage.device_id_kind == "path" and not _is_path_identity(storage.device_id):
            errors.append("A disk without a serial number needs a by-path|size|model identity")
        for approval in storage.additional_device_ids:
            if not approval or any(c in approval for c in "\n\r\0") or ("|" in approval and not _is_path_identity(approval)):
                errors.append("Additional disk approvals must be a serial number, a WWN or a by-path|size|model identity")
                break
        return errors

    def _validate_system(self) -> list[str]:
        errors: list[str] = []
        storage, system = self.storage, self.system
        if system.init not in {"openrc", "systemd"}:
            errors.append("Init system must be OpenRC or systemd")
        if system.stage_variant not in STAGE3_VARIANTS:
            errors.append("Select a supported published AMD64 stage3 variant")
        if (system.init == "openrc") != ("openrc" in system.stage_variant):
            errors.append("The stage3 variant and init system do not match")
        if not system.multilib and "nomultilib" not in system.stage_variant:
            errors.append("Pure 64-bit installation requires a no-multilib stage3/profile")
        if system.multilib and "nomultilib" in system.stage_variant:
            errors.append("A no-multilib stage3 cannot run 32-bit applications: turn off multilib, or choose a stage3 with multilib")
        if system.desktop not in DESKTOPS:
            errors.append("Unknown desktop selection")
        elif desktop_problem(system.stage_variant, system.desktop):
            errors.append(desktop_problem(system.stage_variant, system.desktop))
        if system.kernel not in KERNEL_CHOICES:
            errors.append("Unknown kernel selection")
        if system.initramfs == "genkernel":
            errors.append(GENKERNEL_MESSAGE)
        elif system.initramfs not in INITRAMFS_CHOICES:
            errors.append("Unknown initramfs selection")
        if system.kernel in {"gentoo-kernel-bin", "gentoo-kernel"} and system.initramfs in {"genkernel", "ugrd", "none"}:
            errors.append("Automatic distribution kernels require Dracut; select a manual kernel for another initramfs choice")
        if (storage.luks or storage.lvm or storage.raid_level) and system.initramfs == "none":
            errors.append("Encrypted, LVM, and RAID root configurations need an initramfs")
        if system.bootloader not in BOOTLOADER_CHOICES:
            errors.append("Unknown bootloader selection")
        if system.bootloader in {"systemd-boot", "efi-stub"} and storage.firmware != "uefi":
            errors.append("systemd-boot and EFI stub require UEFI firmware")
        if system.uki and storage.firmware != "uefi":
            errors.append("A Unified Kernel Image requires UEFI firmware")
        if system.profile and not PROFILE_RE.fullmatch(system.profile):
            errors.append("Unknown Portage profile; use a published default/linux/amd64/23.0 profile or leave it empty")
        elif profile_problem(system.profile, system.init, system.stage_variant):
            errors.append(profile_problem(system.profile, system.init, system.stage_variant))
        errors.extend(self._secure_boot_errors())
        errors.extend(self._boot_not_yet_errors())
        errors.extend(self._validate_system_wave5())
        extra = system.kernel_cmdline_extra
        if extra:
            tokens = extra.split(" ")
            if len(extra) > 512 or not all(CMDLINE_TOKEN_RE.fullmatch(token) for token in tokens):
                errors.append("Extra kernel arguments may contain only single-space-separated plain tokens (at most 512 characters)")
            elif any(token.split("=", 1)[0] in {"root", "init", "rd.luks.uuid", "rd.luks.name"} for token in tokens):
                errors.append("Extra kernel arguments cannot override root=, init= or the encryption arguments")
        return errors

    def _validate_system_wave5(self) -> list[str]:
        import ipaddress

        errors: list[str] = []
        system = self.system
        errors.extend(_choice_error(system.world_update, WORLD_UPDATE_CHOICES, "The @world update choice"))
        errors.extend(_choice_error(system.secure_boot_mode, SECURE_BOOT_MODES, "The Secure Boot mode"))
        snippet = system.kernel_config_snippet
        if len(snippet) > 16384 or has_control_characters(snippet.replace("\n", "")) or not all(
                _config_line_ok(line) for line in snippet.splitlines()):
            errors.append("The kernel configuration lines must look like CONFIG_NAME=value or # CONFIG_NAME is not set (at most 16 KiB)")
        if system.serial_console and not SERIAL_CONSOLE_RE.fullmatch(system.serial_console):
            errors.append("The serial console must look like ttyS0 or ttyS0,115200")
        if system.domain and not DOMAIN_RE.fullmatch(system.domain):
            errors.append("The domain name may contain only letters, numbers, dots and hyphens")
        for line in system.hosts_extra:
            parts = line.split()
            try:
                ipaddress.ip_address(parts[0])
                valid = len(parts) >= 2 and not has_control_characters(line) and all(HOSTS_NAME_RE.fullmatch(name) for name in parts[1:])
            except (ValueError, IndexError):
                valid = False
            if not valid:
                errors.append(f"Extra hosts entries must look like “192.168.1.10 name [alias ...]”: {line[:60]}")
                break
        if any(not LOCALE_RE.fullmatch(name) for name in system.extra_locales):
            errors.append("Invalid entry in the extra locales")
        return errors

    def _secure_boot_errors(self) -> list[str]:
        """Secure Boot needs a signed Unified Kernel Image booted by systemd-boot or the EFI stub, and a key the installer makes or the user gives."""
        system, storage = self.system, self.storage
        if not system.secure_boot:
            return []
        errors: list[str] = []
        if system.secure_boot_mode == "shim":
            errors.append(SHIM_NOT_YET_MESSAGE)
        if storage.firmware != "uefi":
            errors.append("Secure Boot requires UEFI firmware")
        if system.bootloader == "grub":
            errors.append("Secure Boot is not supported with GRUB yet; choose systemd-boot or the EFI stub with a Unified Kernel Image")
        elif system.bootloader == "none":
            errors.append("Secure Boot needs a boot loader that the installer signs: choose systemd-boot or the EFI stub")
        elif not system.uki:
            errors.append("Secure Boot with systemd-boot or EFI stub requires a signed Unified Kernel Image (enable UKI)")
        if system.secure_boot_mode == "create":
            return errors
        if not system.secure_boot_key or not system.secure_boot_certificate:
            errors.append("Secure Boot needs both a signing key and certificate")
        elif not (system.secure_boot_key.startswith("/") and system.secure_boot_certificate.startswith("/")) \
                or has_control_characters(system.secure_boot_key + system.secure_boot_certificate):
            errors.append("The Secure Boot key and certificate must be absolute file paths")
        return errors

    def _boot_not_yet_errors(self) -> list[str]:
        """Boot settings the manifest accepts but no code acts on yet: refused with a plain message instead of being ignored."""
        system = self.system
        errors: list[str] = []
        if system.kernel == "custom":
            errors.append("A custom kernel is not available yet. Choose a Gentoo distribution kernel; the manual kernel works from the command-line installer.")
        if system.initramfs == "ugrd":
            errors.append("The ugrd initramfs is not available yet. Choose Dracut.")
        if system.hostonly:
            errors.append("A small initramfs for this PC only (hostonly) is not available yet; the installer builds a generic one that boots on any hardware.")
        if system.generic_uki:
            errors.append("The generic Unified Kernel Image is not available yet.")
        return errors

    def _validate_locale(self) -> list[str]:
        errors: list[str] = []
        system = self.system
        if not system.timezone or system.timezone.startswith("/") or ".." in system.timezone \
                or not re.fullmatch(r"[A-Za-z0-9_+./-]{1,64}", system.timezone):
            errors.append("Invalid timezone")
        elif _known_timezones() and system.timezone not in _known_timezones():
            errors.append(f"Unknown timezone: {system.timezone}")
        if not LOCALE_RE.fullmatch(system.locale):
            errors.append("Invalid locale")
        if system.keymap not in {"", "auto"} and not KEYMAP_RE.fullmatch(system.keymap):
            errors.append("Invalid console keymap")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,15}(?:,[a-z][a-z0-9_-]{0,15}){0,3}", system.keyboard_layout):
            errors.append("Invalid keyboard layout")
        if not re.fullmatch(r"[A-Za-z0-9_,-]{0,64}", system.keyboard_variant):
            errors.append("Invalid keyboard variant")
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", system.keyboard_model):
            errors.append("Invalid keyboard model")
        if not re.fullmatch(r"[A-Za-z0-9_:,+-]{0,256}", system.keyboard_options):
            errors.append("Invalid keyboard options")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{0,64}", system.console_font):
            errors.append("Invalid console font")
        return errors

    def _validate_accounts(self) -> list[str]:
        errors: list[str] = []
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", self.accounts.hostname):
            errors.append("Hostname must contain only letters, numbers, and hyphens")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}\$?", self.accounts.username):
            errors.append("Username must be a valid lowercase Linux username")
        if len(self.accounts.password) < 8:
            errors.append("The user password must contain at least 8 characters")
        errors.extend(_choice_error(self.accounts.root_mode, ROOT_MODES, "The root account mode"))
        if not self.accounts.root_locked() and len(self.accounts.root_password) < 8:
            errors.append("The root password must contain at least 8 characters")
        if self.accounts.privilege not in PRIVILEGE_TOOLS:
            errors.append("Privilege tool must be sudo, doas, run0 or su")
        elif self.accounts.privilege == "run0" and self.system.init != "systemd":
            errors.append("run0 is part of systemd: choose sudo, doas or su with OpenRC")
        if self.options.ssh and not self.accounts.ssh_password_login and not self.accounts.ssh_authorized_keys:
            errors.append("Nobody could log in over SSH: add a public key or allow password login")
        full_name = self.accounts.full_name
        if len(full_name) > 64 or ":" in full_name or has_control_characters(full_name):
            errors.append("The full name may have at most 64 characters, without colons or line breaks")
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", group) for group in self.accounts.groups):
            errors.append("Account groups contain an invalid name")
        if any(not key.strip() or has_control_characters(key) for key in self.accounts.ssh_authorized_keys):
            errors.append("SSH authorized keys must be single-line, non-empty keys")
        if any(c in secret for secret in (self.accounts.password, self.accounts.root_password) for c in "\n\r\0"):
            # chpasswd reads "user:password" lines; a newline would set a second account.
            errors.append("Passwords cannot contain line breaks or NUL characters")
        if _cannot_be_stored(self.accounts.password):
            errors.append("The user password contains a character that cannot be stored; type it again")
        if not self.accounts.root_locked() and _cannot_be_stored(self.accounts.root_password):
            errors.append("The root password contains a character that cannot be stored; type it again")
        return errors

    def _validate_network(self) -> list[str]:
        errors: list[str] = []
        system = self.system
        if self.network.manager not in {"networkmanager", "dhcpcd", "netifrc", "systemd-networkd"}:
            errors.append("Unknown network manager")
        if self.network.manager == "netifrc" and not re.fullmatch(r"[A-Za-z0-9_]+", self.network.interface):
            errors.append("netifrc needs a network interface name containing only letters, numbers, and underscores")
        if self.network.manager == "netifrc" and system.init != "openrc":
            errors.append("netifrc is available only with OpenRC")
        if self.network.manager == "systemd-networkd" and system.init != "systemd":
            errors.append("systemd-networkd requires systemd")
        network = self.network
        if network.interface and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", network.interface):
            errors.append("The network interface name may contain only letters, numbers, and . _ : - (at most 15 characters)")
        if network.manager == "dhcpcd" and not network.dhcp and not network.interface:
            errors.append("A static address with dhcpcd needs the network interface name")
        if not network.dhcp:
            import ipaddress
            try:
                ipaddress.ip_interface(network.address)
                ipaddress.ip_address(network.gateway)
                for server in network.dns:
                    ipaddress.ip_address(server)
            except ValueError:
                errors.append("Static networking needs a valid address/prefix, gateway, and DNS servers")
        if network.wifi_ssid or network.wifi_password:
            if network.manager != "networkmanager":
                errors.append("Wi-Fi setup is available only with NetworkManager")
            errors.extend(_wifi_errors(network.wifi_ssid, network.wifi_password))
        if network.import_live_profiles and network.manager != "networkmanager":
            errors.append("Importing this session's network connections requires NetworkManager")
        if network.proxy:
            errors.extend(_proxy_errors(network.proxy))
        live = network.live_static
        if live.is_set():
            import ipaddress
            try:
                ipaddress.ip_interface(live.address)
                ipaddress.ip_address(live.gateway)
                for server in live.dns:
                    ipaddress.ip_address(server)
            except ValueError:
                errors.append("A fixed address for this session needs a valid address/prefix, gateway, and DNS servers")
        if network.copy_live_wifi:
            if not UUID_RE.fullmatch(network.copy_live_wifi):
                errors.append("The Wi-Fi connection to copy must be given by its uuid")
            elif network.manager != "networkmanager":
                errors.append("Copying this session's Wi-Fi connection requires NetworkManager")
        return errors

    def _validate_portage(self) -> list[str]:
        errors: list[str] = []
        portage = self.portage
        if not portage.mirror.startswith("https://"):
            errors.append("The stage3 mirror must use HTTPS")
        if portage.binhost and not portage.binhost_url.startswith("https://"):
            errors.append("The binary package host must use HTTPS")
        for url in (portage.mirror, portage.binhost_url, *portage.mirror_fallbacks):
            if url and (has_control_characters(url) or " " in url):
                errors.append("Mirror and binary package host addresses cannot contain spaces or line breaks")
                break
        if any(not mirror.startswith("https://") for mirror in portage.mirror_fallbacks):
            errors.append("Fallback stage3 mirrors must use HTTPS")
        if portage.rsync_mirror and not portage.rsync_mirror.startswith(("rsync://", "https://")):
            errors.append("The repository sync mirror must use rsync or HTTPS")
        errors.extend(_choice_error(portage.optimize, OPTIMIZE_CHOICES, "Optimisation"))
        errors.extend(_choice_error(portage.binhost_variant, BINHOST_VARIANTS, "The binary package variant"))
        errors.extend(_choice_error(portage.rsync_mode, RSYNC_MODES, "The repository sync mode"))
        if any(not _is_https_url(mirror) for mirror in portage.mirrors):
            errors.append("Mirrors must use HTTPS and cannot contain spaces or line breaks")
        if any(not LICENSE_TOKEN_RE.fullmatch(group) for group in portage.license_groups):
            errors.append("Licence groups may contain only letters, numbers, and @ + _ . -")
        if portage.profile_override and not PROFILE_RE.fullmatch(portage.profile_override):
            errors.append("The profile override must be a published default/linux/amd64/23.0 profile or empty")
        errors.extend(_make_opts_errors(portage.make_opts))
        for label, value in (("CFLAGS", portage.cflags), ("CXXFLAGS", portage.cxxflags), ("RUSTFLAGS", portage.rustflags),
                             ("EMERGE_DEFAULT_OPTS", portage.emerge_opts), ("ACCEPT_LICENSE", portage.accept_license),
                             ("CPU_FLAGS_X86", portage.cpu_flags), ("VIDEO_CARDS", portage.video_cards)):
            if has_control_characters(value):
                errors.append(f"{label} cannot contain line breaks or control characters")
        if any(not re.fullmatch(r"-?[A-Za-z0-9_+@.-]+", token) for token in (*portage.features, *portage.use_flags)):
            errors.append("FEATURES and USE entries may contain only letters, numbers, and + _ @ . -")
        for atom, flags in portage.package_use.items():
            if not (ATOM_RE.fullmatch(atom) or atom == "*/*") or any(not USE_TOKEN_RE.fullmatch(flag) for flag in flags):
                errors.append(f"Invalid package.use entry: {atom}")
        return errors

    def _validate_options(self) -> list[str]:
        errors: list[str] = []
        for atom in self.options.extra_packages:
            if not ATOM_RE.fullmatch(atom):
                errors.append(f"Invalid package atom: {atom}")
        if self.options.kde_apps not in KDE_APPS_CHOICES:
            errors.append("KDE applications must be core or full")
        options = self.options
        errors.extend(_choice_error(options.logger, LOGGER_CHOICES, "The system logger"))
        errors.extend(_choice_error(options.cron, CRON_CHOICES, "The cron daemon"))
        errors.extend(_choice_error(options.timesync, TIMESYNC_CHOICES, "Time synchronisation"))
        if options.timesync == "timesyncd" and self.system.init != "systemd":
            errors.append("systemd-timesyncd needs systemd: choose chrony with OpenRC")
        errors.extend(_choice_error(options.browser, BROWSER_CHOICES, "The web browser"))
        errors.extend(_choice_error(options.input_method, INPUT_METHODS, "The input method"))
        errors.extend(_choice_error(options.gaming, GAMING_MODES, "Gaming"))
        if options.gaming != "off" and options.gaming in GAMING_MODES:
            if self.system.desktop == "minimal":
                errors.append("Gaming needs a desktop: choose a desktop, or turn gaming off")
            if options.gaming == "native" and (not self.system.multilib or "nomultilib" in self.system.stage_variant):
                errors.append("Native gaming (Steam) needs 32-bit support: choose a stage3 with multilib, or use Flatpak gaming")
        return errors

    def _validate_hardware(self) -> list[str]:
        errors: list[str] = []
        hardware = self.hardware
        for name, choices in HARDWARE_CHOICES.items():
            if getattr(hardware, name) not in choices:
                errors.append(f"Unknown hardware choice for {name.replace('_', ' ')}")
        if hardware.nvidia_branch != "auto" and not re.fullmatch(r"[0-9]{3}", hardware.nvidia_branch):
            errors.append("The NVIDIA driver branch must be auto or a three-digit branch number")
        if hardware.gpu_driver == "nvidia" and not hardware.nvidia_license_accepted:
            errors.append("The proprietary NVIDIA driver needs its licence to be accepted")
        if not 10 <= hardware.zram_percent <= 100:
            errors.append("Compressed RAM swap (zram) must use 10–100% of memory")
        if not set(hardware.printer_drivers) <= PRINTER_DRIVERS:
            errors.append("Unknown printer driver selection")
        if not set(hardware.nvidia_extras) <= set(NVIDIA_EXTRAS):
            errors.append("Unknown NVIDIA add-on: choose from " + ", ".join(NVIDIA_EXTRAS))
        elif "cuda" in hardware.nvidia_extras and not hardware.nvidia_cuda_license_accepted:
            errors.append("The NVIDIA CUDA toolkit needs its licence to be accepted")
        unknown_devices = sorted(set(hardware.devices) - set(HARDWARE_DEVICE_KEYS))
        if unknown_devices:
            errors.append("Unknown device option: " + ", ".join(unknown_devices))
        if hardware.battery_charge_limit not in (0, 60, 80):
            errors.append("The battery charge limit must be 0 (none), 60 or 80")
        dpi = re.fullmatch(r"dpi:([0-9]{2,3})", hardware.hidpi)
        if hardware.hidpi not in {"auto", "off"} and not (dpi and 96 <= int(dpi.group(1)) <= 288):
            errors.append("Display scaling must be auto, off, or dpi:96 to dpi:288")
        return errors

    def require_valid(self) -> None:
        errors = self.validate()
        if errors:
            raise ManifestError("\n".join(errors))

    def effective_profile(self) -> str:
        """The Portage profile that will be selected: the explicit one or the derived one."""
        system = self.system
        return system.profile or derive_profile(
            system.init, system.desktop, multilib=system.multilib, hardened="hardened" in system.stage_variant)

    def warnings(self) -> list[str]:
        """Advice that does not stop the installation (validate() lists what does)."""
        system, options, storage = self.system, self.options, self.storage
        advice: list[str] = []
        family = stage_family(system.stage_variant)
        if family in UNVERIFIED_FAMILIES and system.desktop not in {"minimal", "kde"} and system.desktop in FAMILY_DESKTOP_SUPPORT[family]:
            advice.append(f"Not verified: {system.desktop.upper()} on a {FAMILY_LABELS[family]} stage3 may need extra USE changes during the installation.")
        if system.desktop == "minimal" and (options.install_hwmonux or options.install_portagehub):
            advice.append(COMPANIONS_ON_MINIMAL)
        if system.kernel == "manual":
            advice.append("A manual kernel: the installation stops after the boot configuration so you can install the kernel yourself with the command-line installer, then resume it.")
        if system.secure_boot:
            advice.append("Secure Boot: you must enroll the signing certificate in the firmware once (the finish page shows how). Kernel updates are signed "
                          "automatically with the key kept in /etc/secureboot (readable by root only).")
        if system.dual_boot and system.bootloader != "grub":
            advice.append("Dual boot detection works with GRUB only: other systems will not be added to the boot menu automatically.")
        if self.network.import_live_profiles:
            advice.append("The saved network connections of this live session, including Wi-Fi passwords, are copied into the new system.")
        if storage.mode == "erase" and storage.filesystem == "btrfs" and storage.separate_home:
            advice.append("With a separate /home partition, only the root of the Btrfs disk gets a subvolume (@); /home is a plain partition.")
        return advice

    def summary(self) -> list[tuple[str, str]]:
        return [
            ("Disk", f"{self.storage.device} · {self.storage.mode} · {self.storage.filesystem}"),
            ("Firmware", self.storage.firmware.upper()),
            ("Encryption", self.storage.encryption_summary()),
            ("Base", f"{self.system.stage_variant} · {self.effective_profile()}"),
            ("Desktop", self.system.desktop.upper() if self.system.desktop != "minimal" else "Minimal (no desktop)"),
            ("Kernel", self.system.kernel),
            ("Bootloader", self.system.bootloader),
            ("Portage", "Binary packages preferred" if self.portage.binhost else "Build from source"),
            ("Network", self.network.manager),
            ("Account", f"{self.accounts.username}@{self.accounts.hostname}"),
            ("Companion apps", ", ".join(name for name, enabled in (("HWMonux", self.options.install_hwmonux), ("PortageHub", self.options.install_portagehub)) if enabled) or "None"),
        ]
