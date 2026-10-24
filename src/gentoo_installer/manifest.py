from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields, is_dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints


SCHEMA_VERSION = 1
SECRET_KEYS = {"password", "root_password", "luks_passphrase", "wifi_password", "proxy_password"}
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


@dataclass
class Partition:
    device: str = ""
    mountpoint: str = "/"
    filesystem: str = "ext4"
    size_mib: int = 0
    format: bool = True
    label: str = ""
    flags: list[str] = field(default_factory=list)


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


@dataclass
class System:
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
    world_update: bool = False


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


@dataclass
class Options:
    install_hwmonux: bool = True
    install_portagehub: bool = True
    pipewire: bool = True
    bluetooth: bool = True
    printing: bool = True
    flatpak: bool = False
    ssh: bool = False
    chrony: bool = True
    cron: bool = True
    file_indexing: bool = False
    firmware: bool = True
    microcode: bool = True
    shell_completion: bool = True
    logger: bool = True
    # KDE application set: "core" (file manager, terminal, editor, help) or
    # "full" (the complete kde-apps suite: games, education, multimedia, ...).
    kde_apps: str = "core"
    extra_packages: list[str] = field(default_factory=list)


@dataclass
class Hardware:
    """Hardware-dependent choices; "auto" means "decide from detection"."""

    target: str = "this"  # "this" (this machine) | "portable" (generic, movable install)
    gpu_driver: str = "auto"  # "auto" | "open" | "nvidia"
    nvidia_branch: str = "auto"  # "auto" | three-digit driver branch such as "580"
    nvidia_license_accepted: bool = False
    nvk: str = "auto"  # "auto" | "yes" | "no"
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


PRINTER_DRIVERS = {"gutenprint", "hplip"}
HARDWARE_CHOICES = {
    "target": {"this", "portable"},
    "gpu_driver": {"auto", "open", "nvidia"},
    "nvk": {"auto", "yes", "no"},
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
                return [convert(item_type, item, f"{name}[]") for item in value]
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
                unknown = set(value) - set(allowed)
                if unknown:
                    raise ManifestError(f"Unknown {name} fields: {', '.join(sorted(unknown))}")
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
        if storage.filesystem not in {"ext4", "xfs", "btrfs"}:
            errors.append("Root filesystem must be ext4, XFS, or Btrfs")
        if storage.swap_mib < 0 or storage.home_mib < 0:
            errors.append("Partition sizes cannot be negative")
        if storage.mode == "manual":
            if not any(p.mountpoint == "/" for p in storage.partitions):
                errors.append("Manual storage needs a root (/) mountpoint")
            root_partition = next((p for p in storage.partitions if p.mountpoint == "/"), None)
            if root_partition and not root_partition.format and (storage.luks or storage.lvm):
                errors.append("Preserved root cannot create a new LUKS/LVM stack; select an already opened volume with stack creation disabled")
            for partition in storage.partitions:
                if not partition.device.startswith("/dev/"):
                    errors.append(f"Invalid partition device for {partition.mountpoint}")
                if not partition.mountpoint.startswith("/") or ".." in partition.mountpoint:
                    errors.append(f"Invalid mountpoint: {partition.mountpoint}")
                if partition.filesystem not in {"ext4", "xfs", "btrfs", "vfat", "swap", "none"}:
                    errors.append(f"Unsupported filesystem: {partition.filesystem}")
            if storage.firmware == "uefi" and not any(p.mountpoint in {"/efi", "/boot/efi"} for p in storage.partitions):
                errors.append("UEFI installations need an EFI System Partition mounted at /efi or /boot/efi")
            if storage.luks and not any(p.mountpoint == "/boot" and p.filesystem not in {"swap", "none"} for p in storage.partitions):
                errors.append("Manual LUKS installations need a separate /boot partition outside the encrypted root")
            if storage.table == "dos":
                if len(storage.partitions) > 4:
                    errors.append("DOS partition tables support at most four primary partitions; use GPT or reduce the layout")
        elif storage.table == "dos":
            required_partitions = 1 + int(storage.firmware == "uefi") + int(storage.swap_mib > 0) + int(storage.separate_home) + int(storage.luks)
            if required_partitions > 4:
                errors.append("This guided DOS layout needs more than four primary partitions; use GPT or reduce the layout")
        if storage.luks and len(storage.luks_passphrase) < 8:
            errors.append("The LUKS passphrase must contain at least 8 characters")
        if storage.raid_level and storage.raid_level not in {"0", "1", "5", "6", "10"}:
            errors.append("Unsupported RAID level")
        if storage.device_id_kind not in {"", "serial", "wwn", "path"}:
            errors.append("Unknown disk identity kind")
        if storage.device_id_kind == "path":
            parts = storage.device_id.split("|")
            if len(parts) != 3 or not parts[0] or "/" in parts[0] or not parts[1].isdigit() or any(c in storage.device_id for c in "\n\r\0"):
                errors.append("A disk without a serial number needs a by-path|size|model identity")
        return errors

    def _validate_system(self) -> list[str]:
        errors: list[str] = []
        storage, system = self.storage, self.system
        if system.init not in {"openrc", "systemd"}:
            errors.append("Init system must be OpenRC or systemd")
        if system.stage_variant not in {"amd64-openrc", "amd64-systemd", "amd64-desktop-openrc", "amd64-desktop-systemd", "amd64-nomultilib-openrc", "amd64-nomultilib-systemd", "amd64-hardened-openrc", "amd64-hardened-systemd"}:
            errors.append("Select a supported published AMD64 stage3 variant")
        if (system.init == "openrc") != ("openrc" in system.stage_variant):
            errors.append("The stage3 variant and init system do not match")
        if not system.multilib and "nomultilib" not in system.stage_variant:
            errors.append("Pure 64-bit installation requires a no-multilib stage3/profile")
        if system.desktop not in {"minimal", "kde", "gnome", "xfce"}:
            errors.append("Unknown desktop selection")
        if system.kernel not in {"gentoo-kernel-bin", "gentoo-kernel", "manual"}:
            errors.append("Unknown kernel selection")
        if system.initramfs not in {"dracut", "genkernel", "none"}:
            errors.append("Unknown initramfs selection")
        if system.kernel in {"gentoo-kernel-bin", "gentoo-kernel"} and system.initramfs in {"genkernel", "none"}:
            errors.append("Automatic distribution kernels require Dracut; select a manual kernel for another initramfs choice")
        if (storage.luks or storage.lvm or storage.raid_level) and system.initramfs == "none":
            errors.append("Encrypted, LVM, and RAID root configurations need an initramfs")
        if system.bootloader not in {"grub", "systemd-boot", "efi-stub"}:
            errors.append("Unknown bootloader selection")
        if system.bootloader != "grub" and storage.firmware != "uefi":
            errors.append("systemd-boot and EFI stub require UEFI firmware")
        if system.uki and storage.firmware != "uefi":
            errors.append("A Unified Kernel Image requires UEFI firmware")
        if system.profile and not PROFILE_RE.fullmatch(system.profile):
            errors.append("Unknown Portage profile; use a published default/linux/amd64/23.0 profile or leave it empty")
        errors.extend(self._secure_boot_errors())
        extra = system.kernel_cmdline_extra
        if extra:
            tokens = extra.split(" ")
            if len(extra) > 512 or not all(CMDLINE_TOKEN_RE.fullmatch(token) for token in tokens):
                errors.append("Extra kernel arguments may contain only single-space-separated plain tokens (at most 512 characters)")
            elif any(token.split("=", 1)[0] in {"root", "init", "rd.luks.uuid", "rd.luks.name"} for token in tokens):
                errors.append("Extra kernel arguments cannot override root=, init= or the encryption arguments")
        return errors

    def _secure_boot_errors(self) -> list[str]:
        """Secure Boot needs a signed Unified Kernel Image booted by systemd-boot or the EFI stub."""
        system, storage = self.system, self.storage
        if not system.secure_boot:
            return []
        errors: list[str] = []
        if storage.firmware != "uefi":
            errors.append("Secure Boot requires UEFI firmware")
        if system.bootloader == "grub":
            errors.append("Secure Boot is not supported with GRUB (it would need a signed shim chain); choose systemd-boot or the EFI stub with a Unified Kernel Image")
        elif not system.uki:
            errors.append("Secure Boot with systemd-boot or EFI stub requires a signed Unified Kernel Image (enable UKI)")
        if not system.secure_boot_key or not system.secure_boot_certificate:
            errors.append("Secure Boot needs both a signing key and certificate")
        elif not (system.secure_boot_key.startswith("/") and system.secure_boot_certificate.startswith("/")) \
                or has_control_characters(system.secure_boot_key + system.secure_boot_certificate):
            errors.append("The Secure Boot key and certificate must be absolute file paths")
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
        if not KEYMAP_RE.fullmatch(system.keymap):
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
        if not self.accounts.disable_root_login and len(self.accounts.root_password) < 8:
            errors.append("The root password must contain at least 8 characters")
        if self.accounts.privilege not in {"sudo", "doas"}:
            errors.append("Privilege tool must be sudo or doas")
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

    def summary(self) -> list[tuple[str, str]]:
        return [
            ("Disk", f"{self.storage.device} · {self.storage.mode} · {self.storage.filesystem}"),
            ("Firmware", self.storage.firmware.upper()),
            ("Encryption", "LUKS enabled" if self.storage.luks else "Not enabled"),
            ("Base", f"{self.system.stage_variant} · {self.effective_profile()}"),
            ("Desktop", self.system.desktop.upper() if self.system.desktop != "minimal" else "Minimal (no desktop)"),
            ("Kernel", self.system.kernel),
            ("Bootloader", self.system.bootloader),
            ("Portage", "Binary packages preferred" if self.portage.binhost else "Build from source"),
            ("Network", self.network.manager),
            ("Account", f"{self.accounts.username}@{self.accounts.hostname}"),
            ("Companion apps", ", ".join(name for name, enabled in (("HWMonux", self.options.install_hwmonux), ("PortageHub", self.options.install_portagehub)) if enabled) or "None"),
        ]
