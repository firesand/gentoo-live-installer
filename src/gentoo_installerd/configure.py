from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import stat
import tempfile
import time
import types
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from gentoo_installer import __version__ as INSTALLER_VERSION
from gentoo_installer import blockdev, keymaps, recommend
from gentoo_installer import layout as guided_layout
from gentoo_installer.manifest import ATOM_RE, CMDLINE_TOKEN_RE, USE_TOKEN_RE, InstallManifest, Network, System

from .inspect import MD_MEMBER_TYPES
from .proxy import make_conf_settings, proxy_environment
from .runner import CommandResult, Runner, RunnerError, accepts_keyword
from .storage import LayoutEntry


class ConfigurationError(RuntimeError):
    pass


COMPANION_ATOMS = ("app-misc/hwmonux", "app-portage/portagehub")
# Installed before the desktop set so that the desktop's own choice of a power-profiles provider finds it.
PREFERRED_FIRST = "sys-power/tlp"


class PackageInstallError(ConfigurationError):
    """A package set could not be installed; ``failed`` names what is missing."""

    def __init__(self, message: str, failed: Sequence[str] = (), resolution: bool = False) -> None:
        super().__init__(message)
        self.failed = list(failed)
        self.resolution = resolution


# A package.use key: an ordinary atom, or */* for USE_EXPAND lines such as "*/* VIDEO_CARDS: virgl".
PACKAGE_USE_ATOM = re.compile(r"\*/\*|" + ATOM_RE.pattern)
# What the installer writes under /etc/portage so that its own package choices resolve without
# autounmask. These are configuration, never left to the binary package host (CRITIC-0/1).
DESKTOP_USE: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    # kde-apps-meta -> kdeutils-meta -> kweather needs qtpositioning[geoclue]
    "kde": {"dev-qt/qtpositioning": ("geoclue",)},
    # samba -> libngtcp2 needs the GnuTLS crypto backend
    "gnome": {"net-libs/ngtcp2": ("gnutls",)},
    # xfce4-panel needs libdbusmenu[gtk3]
    "xfce": {"dev-libs/libdbusmenu": ("gtk3",)},
}
NVRAM_FAILURE = re.compile(r"efibootmgr|EFI variables|efivars|NVRAM", re.IGNORECASE)
RECEIPT_PATH = "etc/gentoo-live-installer/install.json"
# The package.env of the hardware plan (hwplan.MEMORY_HEAVY_ATOMS): the compile jobs the memory-heavy packages may use.
MEMORY_ENV_NAME = "installer-memory"
MEMORY_ENV_FILE = f"etc/portage/env/{MEMORY_ENV_NAME}"
PACKAGE_ENV_FILE = "etc/portage/package.env/installer-hardware"
# What an out-of-memory failure says when the build already had one compile job: nothing is left to lower.
NOT_ENOUGH_MEMORY = "It still ran out of memory with one compile job: this PC needs more memory or swap for it."
# Where a live profile's certificates may be read from (never arbitrary files such as /etc/shadow).
CERTIFICATE_ROOTS = ("/home/", "/root/", "/etc/NetworkManager/", "/etc/ssl/", "/etc/pki/", "/etc/ca-certificates/",
                     "/usr/share/ca-certificates/", "/usr/local/share/ca-certificates/")


def shell_value(value: str) -> str:
    return shlex.quote(value)


def ninja_opts_from_make_opts(make_opts: str) -> str:
    """Keep Ninja bounded even when Portage exports an empty NINJAOPTS."""
    try:
        tokens = shlex.split(make_opts)
    except ValueError as exc:
        raise ConfigurationError("Invalid MAKEOPTS") from exc
    jobs = 2
    load: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"-j", "--jobs", "-l", "--load-average"} and index + 1 < len(tokens):
            candidate = tokens[index + 1]
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", candidate):
                token = token + ("=" if token.startswith("--") else "") + candidate
                index += 1
        if token in {"-j", "--jobs", "-j0", "--jobs=0"}:
            jobs = 2  # An unbounded make request must not unbound Ninja.
        match = re.fullmatch(r"(?:-j|--jobs=)([1-9][0-9]*)", token)
        if match:
            jobs = int(match.group(1))
        match = re.fullmatch(r"(?:-l|--load-average=)([0-9]+(?:\.[0-9]+)?)", token)
        if match:
            load = match.group(1)
        index += 1
    return f"-j{jobs}" + (f" -l{load}" if load is not None else "")


# --- build parallelism from the machine that runs the install -----------------------------------


@dataclass(frozen=True)
class Resources:
    cpus: int
    mem_total: int  # bytes
    mem_available: int  # bytes


def machine_resources(meminfo: str | Path = "/proc/meminfo") -> Resources:
    """CPU count and memory of this machine; unreadable values degrade to a small safe machine."""
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    total = available = 0
    try:
        for line in Path(meminfo).read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in {"MemTotal", "MemAvailable"} and rest.split():
                value = int(rest.split()[0]) * 1024
                if key == "MemTotal":
                    total = value
                else:
                    available = value
    except (OSError, ValueError):
        pass
    return Resources(max(1, cpus), total, available or total)


def auto_make_opts(cpus: int, mem_available: int, swap_mib: int = 0) -> str:
    """-j/-l for MAKEOPTS: about 2 GiB per compile job, never above the thread count or 32."""
    budget_gib = mem_available / 1024**3 + 0.5 * max(0, swap_mib) / 1024
    jobs = max(1, min(cpus, 32, int(budget_gib // 2)))
    return f"-j{jobs} -l{max(1, cpus)}"


def auto_emerge_jobs(cpus: int, mem_total: int) -> int:
    """How many packages Portage may build at the same time."""
    gib = mem_total / 1024**3
    if gib >= 60 and cpus >= 16:
        return 3
    if gib >= 14 and cpus >= 8:
        return 2
    return 1


# --- package.use helpers -------------------------------------------------------------------------


def _require_use(package_use: MutableMapping[str, list[str]], atom: str, *flags: str) -> None:
    """Make sure every flag is enabled for atom, overriding a user's -flag."""
    current = package_use.setdefault(atom, [])
    current[:] = [flag for flag in current if flag.lstrip("-") not in flags or not flag.startswith("-")]
    current.extend(flag for flag in flags if flag not in current)


def _default_use(package_use: MutableMapping[str, list[str]], atom: str, *flags: str) -> None:
    """Set a flag for atom unless the user already decided it, either way."""
    current = package_use.setdefault(atom, [])
    current.extend(flag for flag in flags if flag.lstrip("-") not in {item.lstrip("-") for item in current})


def _forbid_use(package_use: MutableMapping[str, list[str]], atom: str, *flags: str) -> None:
    """Make sure every flag is disabled for atom, overriding a user's flag."""
    current = package_use.setdefault(atom, [])
    current[:] = [flag for flag in current if flag not in flags]
    current.extend(f"-{flag}" for flag in flags if f"-{flag}" not in current)


# --- rendering -------------------------------------------------------------------------------------

# Variables whose values add to the profile's instead of replacing them when a plan supplies more.
_INCREMENTAL_KEYS = ("USE", "FEATURES", "EMERGE_DEFAULT_OPTS")
# A hardware plan may tune these but must never widen licences or change mirrors.
_PLAN_FORBIDDEN_KEYS = {"ACCEPT_LICENSE", "GENTOO_MIRRORS", "PORTAGE_BINHOST"}


def render_make_conf(manifest: InstallManifest, plan: object | None = None, *, make_opts: str | None = None) -> str:
    """Render the target's make.conf.

    Empty values are left out so the profile defaults (CPU_FLAGS_X86, VIDEO_CARDS, ...) stay in
    force; a hardware plan may add or tune keys through ``plan.make_conf``.
    """
    p = manifest.portage
    plan_values = {key: str(value) for key, value in dict(getattr(plan, "make_conf", None) or {}).items()
                   if str(value).strip() and key not in _PLAN_FORBIDDEN_KEYS}
    jobs = make_opts if make_opts is not None else p.make_opts
    features = " ".join([*p.features, *(["getbinpkg", "binpkg-request-signature"] if p.binhost else [])])
    values: list[tuple[str, str]] = [
        ("COMMON_FLAGS", p.cflags),
        ("CFLAGS", "${COMMON_FLAGS}"),
        ("CXXFLAGS", p.cxxflags),
        ("FCFLAGS", "${COMMON_FLAGS}"),
        ("FFLAGS", "${COMMON_FLAGS}"),
        ("RUSTFLAGS", p.rustflags),
        ("MAKEOPTS", jobs or plan_values.pop("MAKEOPTS", "")),
        ("LC_MESSAGES", "C.UTF-8"),
        ("EMERGE_DEFAULT_OPTS", p.emerge_opts),
        ("ACCEPT_LICENSE", p.accept_license),
        ("GENTOO_MIRRORS", p.mirror),
        ("USE", " ".join(p.use_flags)),
        ("FEATURES", features),
        # "auto" is a request for detection, not a value Portage understands.
        ("CPU_FLAGS_X86", "" if p.cpu_flags == "auto" else p.cpu_flags),
        ("VIDEO_CARDS", "" if p.video_cards == "auto" else p.video_cards),
        ("GRUB_PLATFORMS", "pc efi-64"),
    ]
    if p.binhost:
        values.append(("PORTAGE_BINHOST", p.binhost_url))
    values.extend(sorted(make_conf_settings(manifest.network.proxy).items()))
    plan_values.pop("MAKEOPTS", None)
    lines: list[str] = []
    emitted: set[str] = set()
    for key, value in values:
        if key in emitted:
            continue
        if key in plan_values:
            extra = plan_values.pop(key)
            value = f"{value} {extra}".strip() if key in _INCREMENTAL_KEYS else extra
        if not value.strip():
            continue
        emitted.add(key)
        lines.append(f"{key}={shell_value(value)}")
    for key, value in plan_values.items():
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key) and key not in emitted:
            lines.append(f"{key}={shell_value(value)}")
    return "\n".join(lines) + "\n"


def render_package_use(package_use: Mapping[str, Sequence[str]]) -> str:
    lines: list[str] = []
    for atom, flags in sorted(package_use.items()):
        if not PACKAGE_USE_ATOM.fullmatch(atom) or any(not USE_TOKEN_RE.fullmatch(flag) for flag in flags):
            raise ConfigurationError(f"Invalid package.use entry: {atom}")
        lines.append(f"{atom} {' '.join(flags)}".rstrip())
    return "\n".join(lines) + ("\n" if lines else "")


def render_fstab(layout: Sequence[LayoutEntry], uuids: Mapping[str, str], *, btrfs_discard: bool = False) -> str:
    """The target's /etc/fstab.

    An entry is named by ``LayoutEntry.source`` when it has one (a swap file inside the target),
    else by UUID, else by its device path. ``LayoutEntry.options`` (Btrfs ``subvol=...``) is
    appended to the default mount options. ``btrfs_discard`` adds asynchronous TRIM to every
    Btrfs mount (mount options are per filesystem, so all subvolumes get the same).
    """
    lines = ["# <file system> <mount point> <type> <options> <dump> <pass>"]
    for entry in sorted(layout, key=lambda item: (item.mountpoint != "/", item.mountpoint)):
        uuid = uuids.get(entry.device, "")
        source = entry.source or (f"UUID={uuid}" if uuid else entry.device)
        if entry.filesystem == "swap":
            lines.append(f"{source} none swap sw 0 0")
        elif entry.mountpoint:
            # fsck.btrfs is a stub that only prints a notice, so btrfs is never checked at boot.
            pass_number = 0 if entry.filesystem == "btrfs" else 1 if entry.mountpoint == "/" else 2
            options = "defaults,noatime" if entry.filesystem != "vfat" else "defaults,umask=0077"
            extra = [entry.options] if entry.options else []
            if btrfs_discard and entry.filesystem == "btrfs":
                extra.append("discard=async")
            lines.append(f"{source} {entry.mountpoint} {entry.filesystem} {','.join([options, *extra])} 0 {pass_number}")
    return "\n".join(lines) + "\n"


def locale_gen_line(locale: str, supported: str = "") -> str:
    """A line for /etc/locale.gen: the name and the charmap glibc generates it with."""
    for line in supported.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == locale:
            return f"{locale} {fields[1]}"
    codeset = re.search(r"\.([A-Za-z0-9-]+)", locale)
    return f"{locale} {codeset.group(1) if codeset else 'UTF-8'}"


def render_hosts(hostname: str) -> str:
    return f"127.0.0.1\tlocalhost\n::1\tlocalhost\n127.0.1.1\t{hostname}.localdomain {hostname}\n"


def render_systemd_network(manifest: InstallManifest) -> str:
    network = manifest.network
    lines = ["[Match]", "Name=en* eth*", "", "[Network]"]
    if network.dhcp:
        lines.append("DHCP=yes")
    else:
        lines.extend((f"Address={network.address}", f"Gateway={network.gateway}"))
        lines.extend(f"DNS={server}" for server in network.dns)
    return "\n".join(lines) + "\n"


def render_netifrc(manifest: InstallManifest) -> str:
    network = manifest.network
    interface = network.interface
    if not re.fullmatch(r"[A-Za-z0-9_]+", interface):
        raise ConfigurationError("netifrc requires a valid network interface name")
    if network.dhcp:
        return f'config_{interface}="dhcp"\n'
    dns = " ".join(network.dns)
    return f'config_{interface}="{network.address}"\nroutes_{interface}="default via {network.gateway}"\ndns_servers_{interface}="{dns}"\n'


def render_dhcpcd_static(manifest: InstallManifest) -> str:
    """The static block appended to the stock /etc/dhcpcd.conf."""
    network = manifest.network
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", network.interface):
        raise ConfigurationError("dhcpcd with a static address needs a valid network interface name")
    lines = ["", "# Static configuration written by the Gentoo live installer", f"interface {network.interface}",
             f"static ip_address={network.address}", f"static routers={network.gateway}"]
    if network.dns:
        lines.append(f"static domain_name_servers={' '.join(network.dns)}")
    return "\n".join(lines) + "\n"


def _keyfile_escape(value: str) -> str:
    """Escape a string for a NetworkManager keyfile (GKeyFile string rules)."""
    escaped = value.replace("\\", "\\\\")
    return "\\s" + escaped[1:] if escaped.startswith(" ") else escaped


def render_nm_ip_sections(network: Network | types.SimpleNamespace) -> list[str]:
    """[ipv4]/[ipv6] sections of a NetworkManager keyfile for the manifest's network settings."""
    if network.dhcp:
        return ["", "[ipv4]", "method=auto", "", "[ipv6]", "method=auto"]
    try:
        address = ipaddress.ip_interface(network.address)
        gateway = ipaddress.ip_address(network.gateway)
        dns = [ipaddress.ip_address(server) for server in network.dns]
    except ValueError as exc:
        raise ConfigurationError("Invalid static NetworkManager address or DNS server") from exc
    if address.version != gateway.version:
        raise ConfigurationError("Static address and gateway must use the same IP family")
    lines: list[str] = []
    for version in (4, 6):
        servers = [str(server) for server in dns if server.version == version]
        method = "manual" if address.version == version else ("auto" if servers else "disabled")
        lines.extend(("", f"[ipv{version}]", f"method={method}"))
        if address.version == version:
            lines.extend((f"address1={address}", f"gateway={gateway}"))
        if servers:
            lines.append(f"dns={';'.join(servers)};")
            if address.version != version:
                lines.append("ignore-auto-dns=true")
    return lines


def render_nm_wifi(network: Network, connection_uuid: str | None = None) -> str:
    """A NetworkManager keyfile for the Wi-Fi network chosen in the installer."""
    ssid, password = network.wifi_ssid, network.wifi_password
    if not ssid or any(ord(char) < 32 or ord(char) == 127 for char in ssid + password) or len(ssid.encode("utf-8")) > 32:
        raise ConfigurationError("The Wi-Fi network name or password is invalid")
    # The static address belongs to the wired link unless the user named a wireless interface.
    static = not network.dhcp and network.interface.startswith("wl")
    lines = ["[connection]", f"id={_keyfile_escape(ssid)}", f"uuid={connection_uuid or uuid.uuid4()}", "type=wifi",
             "autoconnect=true", "", "[wifi]", "mode=infrastructure", f"ssid={_keyfile_escape(ssid)}"]
    if password:
        lines.extend(("", "[wifi-security]", "key-mgmt=wpa-psk", f"psk={_keyfile_escape(password)}"))
    lines.extend(render_nm_ip_sections(network if static else types.SimpleNamespace(dhcp=True)))
    return "\n".join(lines) + "\n"


def render_nm_wired(network: Network, connection_uuid: str | None = None) -> str:
    """A NetworkManager keyfile with the static wired configuration."""
    interface = network.interface
    if interface and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface):
        raise ConfigurationError("Invalid NetworkManager interface name")
    lines = ["[connection]", "id=Installer wired static", f"uuid={connection_uuid or uuid.uuid4()}", "type=ethernet",
             "autoconnect=true"]
    if interface:
        lines.append(f"interface-name={interface}")
    lines.extend(("", "[ethernet]"))
    lines.extend(render_nm_ip_sections(network))
    return "\n".join(lines) + "\n"


# Profile types worth carrying to another machine; bridges, bonds, VLANs and tunnels describe the live setup.
_IMPORTABLE_NM_TYPES = {"wifi", "802-11-wireless", "ethernet", "802-3-ethernet", "vpn", "wireguard", "gsm", "cdma", "pppoe"}
_NM_CERTIFICATE_KEYS = {"ca-cert", "client-cert", "private-key", "phase2-ca-cert", "phase2-client-cert", "phase2-private-key"}


@dataclass
class ImportedProfile:
    text: str
    connection_type: str
    certificates: list[tuple[str, str]] = field(default_factory=list)  # (placeholder, live path)


def rewrite_keyfile(text: str, *, portable: bool = False) -> ImportedProfile | None:
    """Prepare a live NetworkManager profile for the installed system; None when it should be skipped.

    ``permissions=`` names the live user, which does not exist on the target, so it is dropped to
    make the profile system-wide. For portable installs ``interface-name=`` of Ethernet profiles is
    dropped too, because device names differ from PC to PC.
    """
    section = ""
    connection_type = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section == "connection" and line.startswith("type="):
            connection_type = line.partition("=")[2].strip()
    if connection_type not in _IMPORTABLE_NM_TYPES:
        return None
    out: list[str] = []
    certificates: list[tuple[str, str]] = []
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section == "connection" and line.startswith("permissions="):
            continue
        elif portable and section == "connection" and line.startswith("interface-name=") and connection_type in {"ethernet", "802-3-ethernet"}:
            continue
        elif section.startswith("802-1x") and "=" in line and line.partition("=")[0] in _NM_CERTIFICATE_KEYS:
            key, _, value = line.partition("=")
            path = value[len("file://"):] if value.startswith("file://") else value
            if path.startswith("/"):
                placeholder = f"@@CERT{len(certificates)}@@"
                certificates.append((placeholder, path))
                out.append(f"{key}={placeholder}")
                continue
        out.append(raw)
    return ImportedProfile("\n".join(out) + "\n", connection_type, certificates)


# --- keyboard files ----------------------------------------------------------------------------


def _keyboard_is_default(system: System) -> bool:
    return (system.keyboard_layout == "us" and not system.keyboard_variant
            and system.keyboard_model == "pc105" and not system.keyboard_options)


def render_xorg_keyboard(system: System) -> str:
    lines = ['Section "InputClass"', '\tIdentifier "system-keyboard"', '\tMatchIsKeyboard "on"',
             f'\tOption "XkbLayout" "{system.keyboard_layout}"', f'\tOption "XkbModel" "{system.keyboard_model}"']
    if system.keyboard_variant:
        lines.append(f'\tOption "XkbVariant" "{system.keyboard_variant}"')
    if system.keyboard_options:
        lines.append(f'\tOption "XkbOptions" "{system.keyboard_options}"')
    lines.append("EndSection")
    return "\n".join(lines) + "\n"


def render_kxkbrc(system: System) -> str:
    lines = ["[Layout]", f"LayoutList={system.keyboard_layout}", "Use=true", f"Model={system.keyboard_model}"]
    if system.keyboard_variant:
        lines.append(f"VariantList={system.keyboard_variant}")
    if system.keyboard_options:
        lines.extend((f"Options={system.keyboard_options}", "ResetOldOptions=true"))
    return "\n".join(lines) + "\n"


def render_dconf_keyboard(system: System) -> str:
    layouts = system.keyboard_layout.split(",")
    variants = system.keyboard_variant.split(",") if system.keyboard_variant else []
    sources = []
    for index, layout in enumerate(layouts):
        variant = variants[index] if index < len(variants) else ""
        sources.append(f"('xkb', '{layout}{'+' + variant if variant else ''}')")
    lines = ["[org/gnome/desktop/input-sources]", f"sources=[{', '.join(sources)}]"]
    if system.keyboard_options:
        lines.append("xkb-options=[" + ", ".join(f"'{option}'" for option in system.keyboard_options.split(",")) + "]")
    return "\n".join(lines) + "\n"


# --- keeping the EFI system partition current after kernel updates ------------------------------------

# One script, installed for both installkernel flavours: the traditional one runs /etc/kernel/postinst.d
# hooks as "hook <version> <image>", systemd's kernel-install runs /etc/kernel/install.d/*.install plugins as
# "plugin add|remove <version> <entry-dir> <image> [initrd...]".
ESP_SYNC_HOOKS = ("etc/kernel/postinst.d/95-gentoo-installer-esp", "etc/kernel/install.d/95-gentoo-installer-esp.install")
_ESP_SYNC_TEMPLATE = r"""#!/bin/sh
# Written by the Gentoo live installer.
# systemd-boot, the EFI stub and the Unified Kernel Image load their kernel from the EFI system
# partition, which installkernel does not touch. This hook copies each newly installed kernel there.
# It never fails the kernel installation: problems are only reported.
ROOT="${ESP_SYNC_ROOT:-}"
ESP="@ESP@"
UKI=@UKI@
FALLBACK_COPY=@FALLBACK@
case "$1" in
    add) shift ;;
    remove) exit 0 ;;
esac
version="$1"
[ -n "$version" ] || exit 0
kernel=
for candidate in "$ROOT/boot/vmlinuz-$version" "$ROOT/boot/kernel-$version"; do
    if [ -f "$candidate" ]; then kernel="$candidate"; break; fi
done
if [ -z "$kernel" ]; then
    echo "gentoo-installer-esp: no kernel image for $version under /boot; the EFI system partition was not updated" >&2
    exit 0
fi
initrd="$ROOT/boot/initramfs-$version.img"
[ -f "$initrd" ] || initrd=
if [ -z "$ROOT" ] && ! grep -q " $ESP " /proc/mounts 2>/dev/null; then
    echo "gentoo-installer-esp: $ESP is not mounted; the EFI system partition was not updated" >&2
    exit 0
fi
esp="$ROOT$ESP"
replace() {
    cp "$1" "$2.new" && mv -f "$2.new" "$2" || { rm -f "$2.new"; return 1; }
}
if [ "$UKI" = 1 ]; then
    mkdir -p "$esp/EFI/Linux"
    set -- ukify build --linux "$kernel" --cmdline "$(cat "$ROOT/etc/kernel/cmdline" 2>/dev/null)" --output "$esp/EFI/Linux/gentoo.efi.new"
    [ -n "$initrd" ] && set -- "$@" --initrd "$initrd"
    if "$@" && mv -f "$esp/EFI/Linux/gentoo.efi.new" "$esp/EFI/Linux/gentoo.efi"; then
        [ "$FALLBACK_COPY" = 1 ] && replace "$esp/EFI/Linux/gentoo.efi" "$esp/EFI/BOOT/BOOTX64.EFI"
    else
        rm -f "$esp/EFI/Linux/gentoo.efi.new"
        echo "gentoo-installer-esp: could not build the Unified Kernel Image for $version" >&2
    fi
else
    replace "$kernel" "$esp/vmlinuz" || echo "gentoo-installer-esp: could not copy the kernel to $ESP" >&2
    if [ -n "$initrd" ]; then
        replace "$initrd" "$esp/initramfs" || echo "gentoo-installer-esp: could not copy the initramfs to $ESP" >&2
    fi
fi
exit 0
"""


def render_esp_sync_hook(esp: str, uki: bool, fallback_copy: bool = False) -> str:
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", esp):
        raise ConfigurationError("Invalid EFI system partition mount point")
    return _ESP_SYNC_TEMPLATE.replace("@ESP@", esp).replace("@UKI@", "1" if uki else "0").replace("@FALLBACK@", "1" if fallback_copy else "0")


# --- checks on the live system, for the daemon's preflight --------------------------------------------


def check_secure_boot_material(runner: Runner, system: System) -> list[str]:
    """Is the Secure Boot signing key readable and does the certificate belong to it?"""
    key, certificate = Path(system.secure_boot_key), Path(system.secure_boot_certificate)
    problems: list[str] = []
    for label, path in (("signing key", key), ("certificate", certificate)):
        if not path.is_file() or not os.access(path, os.R_OK):
            problems.append(f"The Secure Boot {label} is missing or not readable: {path}")
    if problems:
        return problems
    try:
        public_from_certificate = runner.run(("openssl", "x509", "-noout", "-pubkey", "-in", str(certificate))).stdout.strip()
        public_from_key = runner.run(("openssl", "pkey", "-pubout", "-in", str(key))).stdout.strip()
    except RunnerError as exc:
        return [f"The Secure Boot key or certificate cannot be parsed: {str(exc)[-200:]}"]
    if public_from_certificate and public_from_key and public_from_certificate != public_from_key:
        problems.append("The Secure Boot certificate does not belong to the signing key")
    return problems


def live_input_problems(manifest: InstallManifest, *, root: str | Path = "/", runner: Runner | None = None) -> list[str]:
    """Choices that this live system can already tell are unusable.

    The daemon's preflight runs this before any disk is touched, so a typo in a time zone, keymap or
    locale (or a wrong Secure Boot key) stops the installation at the start instead of after the
    disk was wiped. Lists that the live image does not have are not checked.
    """
    base = Path(root)
    system = manifest.system
    problems: list[str] = []
    zones = base / "usr/share/zoneinfo"
    if zones.is_dir() and not (zones / system.timezone).is_file():
        problems.append(f"The time zone {system.timezone!r} does not exist on this system")
    available = keymaps.list_console_keymaps((base / "usr/share/keymaps", base / "usr/share/kbd/keymaps"))
    wanted = system.keymap if system.keymap not in {"", "auto"} else keymaps.console_keymap_for(system.keyboard_layout, system.keyboard_variant)
    if available and wanted not in available:
        close = ", ".join(keymaps.near_matches(wanted, sorted(available))) or "none"
        problems.append(f"The console keymap {wanted!r} does not exist (similar: {close})")
    supported = base / "usr/share/i18n/SUPPORTED"
    if supported.is_file() and system.locale not in {"C", "C.UTF-8", "POSIX"}:
        names = {line.split()[0] for line in supported.read_text(errors="replace").splitlines() if line.strip() and not line.startswith("#")}
        if names and system.locale not in names:
            problems.append(f"The locale {system.locale!r} is not available (see /usr/share/i18n/SUPPORTED)")
    fonts = base / "usr/share/consolefonts"
    if system.console_font and fonts.is_dir() and not list(fonts.glob(f"{system.console_font}.*")) and not system.console_font.startswith("ter-"):
        problems.append(f"The console font {system.console_font!r} is not installed")
    if system.secure_boot and runner is not None:
        problems.extend(check_secure_boot_material(runner, system))
    if manifest.storage.mode == "manual" and runner is not None:
        problems.extend(root_stack_problems(manifest, runner))
    return problems


def root_stack_problems(manifest: InstallManifest, runner: Runner) -> list[str]:
    """A manual root on a device type the initramfs cannot assemble, or on a volume or array that cannot be named for it.

    Looked up in the live device tree before anything is formatted; the kernel stage checks again for the layout it built.
    """
    try:
        output = runner.run(blockdev.lsblk_argv()).stdout
        disks = blockdev.parse_lsblk(output[output.index("{"):] if "{" in output else output)
    except (RunnerError, OSError, ValueError, KeyError, TypeError, AttributeError):
        return []  # no tree to judge by: the kernel stage looks again
    layout = [LayoutEntry(row.device, row.mountpoint, row.filesystem) for row in manifest.storage.partitions]
    stack = detect_existing_stack(runner, layout, disks)
    return [stack.unsupported] if stack.unsupported else []


# --- explaining a failed emerge ---------------------------------------------------------------------

# How much of the end of a failed package's build log is read: the compiler's own error is near the end of it.
BUILD_LOG_TAIL_BYTES = 256 * 1024
# Of that, the last lines are searched, and a longer line is a build command, not a message.
_LOG_LINES = 400
_LOG_LINE_CHARS = 300
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class FailedBuild:
    """A package that emerge could not build or install."""

    package: str  # category/name-version, without slot and repository
    phase: str = ""  # the Portage phase that failed (compile, configure, install, ...), "" when emerge did not say
    log: str = ""  # its build log as emerge names it: a path inside the new system


@dataclass(frozen=True)
class EmergeDiagnosis:
    resolution: bool  # the package set cannot be resolved; retrying cannot help
    summary: str  # short explanation for the user, empty when nothing recognisable was found
    build_failure: bool = False  # a package failed to compile or install
    transient: bool = False  # a download or network problem; a retry may well work
    out_of_memory: bool = False  # a build was killed for lack of memory; fewer parallel jobs may help
    failures: tuple[FailedBuild, ...] = ()  # the packages that failed themselves
    dropped: tuple[str, ...] = ()  # packages --keep-going left out because one they need failed

    @property
    def kind(self) -> str:
        if self.resolution:
            return "resolution"
        if self.out_of_memory:
            return "memory"
        if self.transient:
            return "transient"
        return "build" if self.build_failure else "unknown"


_RESOLUTION_MARKERS = (
    "USE changes are necessary", "REQUIRED_USE flag constraints are unsatisfied", "keyword changes are necessary",
    "license changes are necessary", "mask changes are necessary", "have been masked", "no ebuilds to satisfy",
    "Multiple package instances within a single package slot", "cannot be installed at the same time",
    "has unmet requirements", "Dependency problem", "circular dependencies", "Circular dependencies",
)


_TRANSIENT_PATTERN = re.compile(
    r"Fetch failed for|Temporary failure in name resolution|Could not resolve host|Name or service not known|"
    r"Connection (?:timed out|reset|refused)|Network is unreachable|Operation timed out|Couldn't connect|"
    r"Failed to connect|SSL connection|HTTP(?: error)? 5\d\d|503 Service|502 Bad Gateway", re.IGNORECASE)
_MEMORY_PATTERN = re.compile(
    r"signal terminated program|Killed\b|virtual memory exhausted|Cannot allocate memory|std::bad_alloc|"
    r"out of memory|memory exhausted|oom-kill", re.IGNORECASE)
# In a build log the signs are narrower: a compiler, linker or runtime that the kernel killed or that could not get memory.
_LOG_MEMORY_PATTERN = re.compile(
    r"signal terminated program|internal compiler error: Killed|virtual memory exhausted|Cannot allocate memory|std::bad_alloc|"
    r"memory allocation of \d+ bytes failed|out of memory|memory exhausted|oom-kill|terminated with signal 9|signal: 9, SIGKILL|(?-i:\bKilled\b)",
    re.IGNORECASE)
# The first of these that any line matches names the reason, so a compiler's "error:" beats a make or ninja summary line.
_LOG_ERROR_PATTERNS = (
    re.compile(r"(?<![-\w])(?:fatal )?error(?:\[\w+\])?:", re.IGNORECASE),
    re.compile(r"(?<![-\w])error(?![-\w=])|\bFAILED\b|undefined reference", re.IGNORECASE),
)
# What Portage prints about failed packages. Its summary block (only when several failed or --keep-going is on):
#   " *  (dev-python/pyside-6.11.2:6/6.11.2::gentoo, ebuild scheduled for merge), Log file:" and "'/var/tmp/.../build.log'"
_FAILED_ENTRY = re.compile(r"\((?P<package>[^\s:,()]+)(?::[^\s,()]*)?::[^\s,()]+, (?:ebuild|binary) scheduled for merge[^)]*\)(?P<postinst> \(postinst failed\))?")
#   " * ERROR: dev-python/pyside-6.11.2::gentoo failed (compile phase):" (the messages of the package; always printed)
_FAILED_PHASE = re.compile(r"ERROR: (?P<package>\S+?)(?:::\S+)? failed \((?P<phase>\w+) phase\)")
#   ">>> Failed to emerge dev-python/pyside-6.11.2, Log file:"
_FAILED_TO_EMERGE = re.compile(r"Failed to emerge (?P<package>[^\s,]+),")
#   " * emerge --keep-going: app-misc/hwmonux-1.0.0-r1 dropped because it requires"
_DROPPED = re.compile(r"emerge --keep-going: (?P<package>\S+) dropped because")
# Every place that names a build log has its path: /var/tmp/portage/<category>/<name-version>/temp/build.log
_BUILD_LOG = re.compile(r"'(?P<path>/[^'\s]*?/(?P<package>[^/'\s]+/[^/'\s]+)/temp/build\.log)'")
# A package that checks its memory needs before it builds (check-reqs.eclass: qtwebengine wants 2.5 GB per job) stops emerge with
#   " * There is NOT at least 10 GiB RAM" and " * ERROR: dev-qt/qtwebengine-6.11.2::gentoo failed (pretend phase):"; fewer jobs fix that.
_SHORT_OF_RAM = re.compile(r"There is NOT at least (?P<size>\d+(?:\.\d+)? \w+) RAM")
_SHORT_OF_SOMETHING = re.compile(r"There is NOT at least .+")
# The old summary block: " * 1) dev-qt/qtwebengine-6.11.2::gentoo (/var/tmp/portage/...)".
_NUMBERED_ENTRY = re.compile(r"\s*\*\s+\d+\)\s+(?P<package>\S+?)::")
# phase -> (what the build was doing when it died, what to say about the failure)
_PHASES = {
    "setup": ("setting up", "failed in its setup"), "unpack": ("unpacking", "failed to unpack"), "prepare": ("preparing", "failed to prepare"),
    "configure": ("configuring", "failed to configure"), "compile": ("compiling", "failed to compile"), "test": ("testing", "failed its tests"),
    "install": ("installing", "failed to install"), "preinst": ("installing", "failed before it was installed"),
    "pretend": ("checking", "failed its check before the build"),
    "postinst": ("installing", "was installed, but its final step failed"),
}


def _build_log_findings(text: str) -> tuple[str, str]:
    """(the line that shows the build was killed for lack of memory, the first error line) of the end of a build log; either may be ''."""
    lines = [_ANSI.sub("", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line and len(line) <= _LOG_LINE_CHARS][-_LOG_LINES:]
    memory = next((line for line in lines if _LOG_MEMORY_PATTERN.search(line)), "")
    if memory:
        return memory, ""
    for pattern in _LOG_ERROR_PATTERNS:
        error = next((line for line in lines if pattern.search(line)), "")
        if error:
            return "", error
    return "", ""


def _failure_sentence(failure: FailedBuild, memory: str, error: str) -> str:
    """What happened to one package, in a sentence that starts with its name ('' when nothing is known beyond the name)."""
    doing, failed = _PHASES.get(failure.phase, ("building", "failed to build"))
    if memory:
        return f"{failure.package} ran out of memory while {doing}: {memory[:160]}"
    if error:
        return f"{failure.package} {failed}: {error[:160]}"
    return f"{failure.package} {failed}" if failure.phase else ""


def explain_emerge_failure(output: str, read_log: Callable[[str], str] | None = None) -> EmergeDiagnosis:
    """Turn the tail of a failed emerge run into a short, actionable explanation.

    ``output`` is the end of what emerge printed. With ``read_log`` (the end of a build log, given the path emerge
    printed for it) the diagnosis also reads why a package failed: when other packages were built after the failure,
    as --keep-going does, the compiler's own error is no longer in the output.
    """
    output = _ANSI.sub("", output)
    lines = [line.rstrip() for line in output.splitlines()]
    items: list[str] = []
    resolution = any(marker in output for marker in _RESOLUTION_MARKERS)

    def following(marker: str, limit: int = 60) -> list[str]:
        for index, line in enumerate(lines):
            if marker in line:
                block: list[str] = []
                for follower in lines[index + 1:index + 1 + limit]:
                    if not follower.strip() and block:
                        break
                    if follower.strip():
                        block.append(follower.strip())
                return block
        return []

    for kind, marker in (("USE change needed", "USE changes are necessary"), ("licence change needed", "license changes are necessary"),
                         ("keyword change needed", "keyword changes are necessary"), ("mask change needed", "mask changes are necessary")):
        block = following(marker)
        reason = ""
        for entry in block:
            if entry.startswith("# required by"):
                reason = reason or entry.removeprefix("# required by").strip().split(" (")[0].split("::")[0]
            elif entry and not entry.startswith(("#", "(", "!!!")):
                items.append(f"{kind}: {entry}" + (f" (required by {reason})" if reason else ""))
                reason = ""
    phases: dict[str, str] = {}  # package -> phase, in the order the packages are first named
    for index, line in enumerate(lines):
        if "REQUIRED_USE flag constraints are unsatisfied" in line:
            package = next((lines[i].strip().lstrip("- ").split(" USE=")[0] for i in range(index - 1, max(index - 4, -1), -1)
                            if lines[i].strip().startswith("- ")), "a package")
            constraint = next((entry.strip() for entry in lines[index + 1:index + 4] if entry.strip()), "")
            items.append(f"{package} has unsatisfiable REQUIRED_USE: {constraint[:160]}")
        match = re.search(r'All ebuilds that could satisfy "([^"]+)" have been masked', line)
        if match:
            reason = next((entry.strip() for entry in lines[index + 1:index + 6] if "masked by" in entry), "")
            items.append(f"{match.group(1)} is masked" + (f" ({reason.split('masked by:')[-1].strip(' )')})" if reason else ""))
        match = re.search(r'no ebuilds to satisfy "([^"]+)"', line)
        if match:
            items.append(f"no package matches {match.group(1)}")
        if "[blocks B" in line:
            items.append("blocked packages: " + line.strip()[:160])
        if "Multiple package instances within a single package slot" in line:
            items.append("slot conflict between packages")
        for found in (_FAILED_ENTRY.search(line), _FAILED_PHASE.search(line), _FAILED_TO_EMERGE.search(line), _NUMBERED_ENTRY.match(line)):
            if found:
                groups = found.groupdict()
                package = groups["package"]
                phases[package] = groups.get("phase") or ("postinst" if groups.get("postinst") else "") or phases.get(package, "")
    dropped = tuple(dict.fromkeys(m.group("package") for m in _DROPPED.finditer(output)))
    logs = {m.group("package"): m.group("path") for m in _BUILD_LOG.finditer(output)}
    failures = tuple(FailedBuild(package, phase, logs.get(package) or f"/var/tmp/portage/{package}/temp/build.log")
                     for package, phase in phases.items() if package not in dropped)
    sentences: list[str] = []
    undescribed: list[str] = []
    killed = False
    short_of_ram = _SHORT_OF_RAM.search(output)
    short_of_something = _SHORT_OF_SOMETHING.search(output)
    for failure in failures[:4]:
        if failure.phase == "pretend" and short_of_something:
            killed = killed or bool(short_of_ram)
            sentences.append(f"{failure.package} needs {short_of_ram['size']} of memory for its compile jobs, and this PC has less" if short_of_ram
                             else f"{failure.package} {_PHASES['pretend'][1]}: {short_of_something.group(0).strip()[:160]}")
            continue
        text = ""
        if read_log is not None:
            try:
                text = read_log(failure.log) or ""
            except Exception:  # noqa: BLE001 - a log that cannot be read only costs the detail
                text = ""
        memory, error = _build_log_findings(text)
        killed = killed or bool(memory)
        sentence = _failure_sentence(failure, memory, error)
        (sentences if sentence else undescribed).append(sentence or failure.package)
    undescribed.extend(failure.package for failure in failures[4:])
    items = sentences + items
    if undescribed:
        items.append("failed to build: " + ", ".join(dict.fromkeys(undescribed)))
    fetch = [m.group(1) for line in lines for m in [re.search(r"Fetch failed for ([^\s,]+)", line)] if m]
    if fetch:
        items.append("download failed for " + ", ".join(dict.fromkeys(fetch)) + " (check the network connection)")
    unique = list(dict.fromkeys(items))
    summary = "; ".join(unique[:6]) + (f"; and {len(unique) - 6} more" if len(unique) > 6 else "")
    if not summary:
        tail = [line.strip() for line in lines if line.strip()][-3:]
        summary = " | ".join(tail)[:400]
    build_failure = bool(failures) or bool(re.search(r"\* ERROR: \S+ failed \(", output))
    memory_in_output = build_failure and bool(_MEMORY_PATTERN.search(output))
    if memory_in_output and not killed:
        summary += " (the build ran out of memory)"
    return EmergeDiagnosis(resolution, summary, build_failure, bool(fetch) or bool(_TRANSIENT_PATTERN.search(output)), killed or memory_in_output,
                           failures, dropped)


# emerge's own way out of a dependency cycle, for example "- dev-python/pillow-12.3.0 (Change USE: -truetype)".
_CYCLE_BREAK_RE = re.compile(r"^- (?P<cpv>[A-Za-z0-9+_.-]+/[A-Za-z0-9+_.-]+) \(Change USE: (?P<flags>[^)]+)\)\s*$", re.M)
CYCLE_BREAK_USE = "etc/portage/package.use/zz-installer-cycle-break"
CYCLE_BREAK_RECORD = "var/lib/gentoo-installer/cycle-breaks"
MAX_CYCLE_BREAKS = 3


def package_of(cpv: str) -> str:
    """'dev-python/pillow-12.3.0' -> 'dev-python/pillow' (a package name never ends in -<digit>...)."""
    category, _, rest = cpv.partition("/")
    match = re.match(r"(.+?)-[0-9]", rest)
    return f"{category}/{match.group(1) if match else rest}"


def cycle_break_suggestion(output: str) -> tuple[str, list[str]] | None:
    """The package and USE change emerge suggests to break a dependency cycle, or None.

    Building from source on a desktop profile can hit cycles a binary install never sees (in 2026
    glib -> docutils -> pillow[truetype] -> harfbuzz -> glib). emerge names a change that breaks the
    cycle; the first one it offers is used.
    """
    start = output.find("break this cycle")
    if start < 0 or "ircular dependencies" not in output:
        return None
    match = _CYCLE_BREAK_RE.search(output, start)
    if not match:
        return None
    flags = [flag.lstrip("+") for flag in match.group("flags").split()]
    if not flags or not all(USE_TOKEN_RE.fullmatch(flag) for flag in flags) or not ATOM_RE.fullmatch(package_of(match.group("cpv"))):
        return None
    return match.group("cpv"), flags


# --- package sets ---------------------------------------------------------------------------------


@dataclass
class DesktopAtoms:
    required: list[str] = field(default_factory=list)  # desktop environment, display manager, X server
    optional: list[str] = field(default_factory=list)  # extras: audio, Bluetooth, printing, companions, ...
    extras: list[str] = field(default_factory=list)  # packages typed in by the user, installed one by one
    groups: list[tuple[str, list[str]]] = field(default_factory=list)  # hardware plan groups

    def everything(self) -> list[str]:
        merged = [*self.required, *self.optional, *(atom for _, atoms in self.groups for atom in atoms), *self.extras]
        return list(dict.fromkeys(merged))


@dataclass
class KeepGoingResult:
    missing: list[str] = field(default_factory=list)  # requested packages that are still not installed
    reason: str = ""
    kind: str = ""  # "" (all installed) | EmergeDiagnosis.kind of the last failure


@dataclass
class DesktopResult:
    degraded: list[str] = field(default_factory=list)  # messages for packages that could not be installed
    failed: list[str] = field(default_factory=list)


def _plan_list(plan: object | None, name: str) -> list:
    return list(getattr(plan, name, None) or [])


def _plan_scalar(plan: object | None, name: str, default: Any = None) -> Any:
    """A resolved choice of the hardware plan (HardwarePlan.scalars), or ``default`` without a plan."""
    scalars = getattr(plan, "scalars", None)
    return scalars.get(name, default) if isinstance(scalars, dict) else default


# GRUB's EFI file names by target: the program on the ESP and the one on the removable-media path.
GRUB_EFI_FILES = {"x86_64-efi": ("grubx64.efi", "BOOTX64.EFI"), "i386-efi": ("grubia32.efi", "BOOTIA32.EFI")}


def grub_efi_target(plan: object | None) -> str:
    """x86_64-efi, or i386-efi for the PCs with 32-bit UEFI firmware (the plan knows which)."""
    target = _plan_scalar(plan, "grub_target", "x86_64-efi")
    return target if target in GRUB_EFI_FILES else "x86_64-efi"


def efi_arch(plan: object | None) -> str:
    """The suffix of systemd's EFI files (systemd-boot<arch>.efi, linux<arch>.efi.stub): x64, or ia32 on 32-bit UEFI firmware."""
    return "ia32" if grub_efi_target(plan) == "i386-efi" else "x64"


@dataclass(frozen=True)
class ExistingStack:
    """LVM volumes and md arrays under the layout that the installer did not create.

    A manual layout may sit on a volume group or array that was activated before the installation. The manifest
    cannot say so (storage.lvm and storage.raid_level mean "create one"), so Configurer.detect_stack() reads it from
    the device tree. The initramfs needs the tools and the activation arguments of what the root (and the swap to
    resume from) sit on; every other volume is activated by the init system once the system is up.
    """

    lvm: bool = False  # a layout device is on LVM: lvm2 is needed
    md: bool = False  # a layout device is on an md array: mdadm is needed
    thin: bool = False  # a logical volume is thin or cached: lvm2 needs its thin flag
    root_lvm: bool = False
    root_md: str = ""  # RAID level of the array under the root ("1", "5", "10"; "md" when unknown), "" when the root is not on one
    lvs: tuple[str, ...] = ()  # "vg/lv" of the volumes the initramfs has to activate (rd.lvm.lv)
    md_uuids: tuple[str, ...] = ()  # arrays the initramfs has to assemble (rd.md.uuid)
    arrays: tuple[str, ...] = ()  # ARRAY lines of every array under the layout, for /etc/mdadm.conf
    unsupported: str = ""  # why the root cannot be booted: a device type the initramfs cannot assemble, or a name that could not be found out

    @property
    def early_lvm(self) -> bool:
        return self.root_lvm or bool(self.lvs)

    @property
    def early_md(self) -> bool:
        return bool(self.root_md) or bool(self.md_uuids)


# Device-mapper types below a root that the installer cannot make bootable (multipath, linear/mirror maps, ...).
UNSUPPORTED_ROOT_TYPES = frozenset({"dm", "mpath"})
# What goes onto the kernel command line and into mdadm.conf comes from tools that print user-chosen names: only plain text passes.
LVM_NAME_RE = re.compile(r"[A-Za-z0-9+_.-]+/[A-Za-z0-9+_.-]+")
MD_UUID_RE = re.compile(r"[0-9a-fA-F:-]{32,40}")
MDADM_ARRAY_RE = re.compile(r"ARRAY [\x21-\x7e]+( [\x21-\x7e]+)*")


def _is_md(node: blockdev.BlockDevice) -> bool:
    """lsblk calls an md array by its RAID level (raid1, raid10, linear) or plain md."""
    return node.type == "md" or node.type == "linear" or node.type.startswith("raid")


def detect_existing_stack(runner: Runner, layout: Sequence[LayoutEntry], disks: Sequence[blockdev.BlockDevice], resume_device: str | None = None,
                          notify: Callable[[str], None] = lambda message: None) -> ExistingStack:
    """What the layout sits on that the installer did not create: LVM volumes and md arrays (see ExistingStack).

    ``disks`` is the live device tree (lsblk). Only read-only lookups (lvs, mdadm --examine/--detail) are run, and
    only for devices that really sit on a volume or an array.
    """
    everything = blockdev.all_nodes(disks)
    # device -> whether the initramfs needs it (the root, and the swap that hibernation resumes from)
    wanted: dict[str, bool] = {}
    root_device = ""
    for entry in layout:
        if entry.source:
            continue  # a swap file is named by its path inside the root
        wanted[entry.device] = wanted.get(entry.device, False) or entry.mountpoint == "/" or entry.device == resume_device
        root_device = entry.device if entry.mountpoint == "/" else root_device
    lvm = md = thin = root_lvm = False
    root_md = unsupported = ""
    lvs: list[str] = []
    uuids: list[str] = []
    arrays: list[str] = []
    for device, early in wanted.items():
        try:
            node = blockdev.find(disks, device)
        except (LookupError, ValueError):
            continue
        # the device and everything it sits on: partitions, arrays, volumes, disks
        chain = [other for other in everything if any(sub.key == node.key for sub in other.walk())]
        on_arrays = [other for other in chain if _is_md(other)]
        lvm = lvm or any(other.type == "lvm" for other in chain)
        md = md or bool(on_arrays)
        if device == root_device:
            root_lvm = any(other.type == "lvm" for other in chain)
            root_md = on_arrays[-1].type.removeprefix("raid") if on_arrays else ""
            bad = next((other for other in chain if other.type in UNSUPPORTED_ROOT_TYPES), None)
            if bad is not None:
                unsupported = (f"The root filesystem sits on a {bad.type} device ({bad.path}), which the installer cannot make bootable. "
                               "It can boot from a partition, an LVM volume, an md RAID array or the encrypted volume it creates itself.")
        if node.type == "lvm":
            name, segtype = _lvm_volume(runner, node, notify)
            thin = thin or "thin" in segtype or "cache" in segtype
            if early and name:
                lvs.append(name)
            elif device == root_device:
                unsupported = unsupported or f"Could not find out which LVM volume group the root volume {node.path} belongs to, so the initramfs could not activate it."
        for array in on_arrays:
            arrays.extend(_md_scan(runner, array))
            if early:
                uuids.append(_md_uuid(runner, array, everything, notify))
                if not uuids[-1] and device == root_device:
                    unsupported = unsupported or f"Could not read the UUID of the RAID array {array.path} under the root, so the initramfs could not assemble it."
    return ExistingStack(lvm=lvm, md=md, thin=thin, root_lvm=root_lvm, root_md=root_md, lvs=tuple(dict.fromkeys(lvs)),
                         md_uuids=tuple(dict.fromkeys(uuid for uuid in uuids if uuid)), arrays=tuple(dict.fromkeys(arrays)), unsupported=unsupported)


def _lvm_volume(runner: Runner, node: blockdev.BlockDevice, notify: Callable[[str], None]) -> tuple[str, str]:
    """("vg/lv", segment type) of a logical volume; the name is "" when it cannot be found out."""
    try:
        # name=value pairs: the runner mixes stderr into the output, and lvm likes to warn about leaked descriptors
        output = runner.run(("lvs", "--noheadings", "--nameprefixes", "-o", "vg_name,lv_name,segtype", node.path)).stdout
    except (RunnerError, OSError):
        output = ""
    fields = dict(re.findall(r"LVM2_(VG_NAME|LV_NAME|SEGTYPE)='([^']*)'", output))
    name = f"{fields.get('VG_NAME', '')}/{fields.get('LV_NAME', '')}"
    if LVM_NAME_RE.fullmatch(name):
        return name, fields.get("SEGTYPE", "")
    # /dev/mapper/<vg>-<lv>: a dash inside a name is doubled
    parts = re.split(r"(?<!-)-(?!-)", os.path.basename(node.path))
    name = "/".join(part.replace("--", "-") for part in parts)
    if len(parts) == 2 and LVM_NAME_RE.fullmatch(name):
        return name, ""
    notify(f"warning: could not tell which LVM volume {node.path} is, so the initramfs may not activate it")
    return "", ""


def _md_uuid(runner: Runner, array: blockdev.BlockDevice, everything: Sequence[blockdev.BlockDevice], notify: Callable[[str], None]) -> str:
    """The UUID dracut's rd.md.uuid wants: from a member as dracut itself reads it (a firmware-RAID volume has another UUID than its container)."""
    members = [other for other in everything if other.fstype in MD_MEMBER_TYPES and any(sub.key == array.key for sub in other.walk()[1:])]
    for argv in ([("mdadm", "--examine", "--export", members[0].path)] if members else []) + [("mdadm", "--detail", "--export", array.path)]:
        try:
            output = runner.run(argv).stdout
        except (RunnerError, OSError):
            continue
        uuid = next((line.partition("=")[2].strip() for line in output.splitlines() if line.startswith("MD_UUID=")), "")
        if MD_UUID_RE.fullmatch(uuid):
            return uuid
    notify(f"warning: could not read the UUID of the RAID array {array.path}, so the initramfs may not assemble it")
    return ""


def _md_scan(runner: Runner, array: blockdev.BlockDevice) -> list[str]:
    """The ARRAY line(s) mdadm prints for an assembled array, for /etc/mdadm.conf."""
    try:
        output = runner.run(("mdadm", "--detail", "--scan", array.path)).stdout
    except (RunnerError, OSError):
        return []
    return [line.strip() for line in output.splitlines() if MDADM_ARRAY_RE.fullmatch(line.strip())]


class Configurer:
    def __init__(self, runner: Runner, target_root: str | Path, *, dry_run: bool = False,
                 companion_bundle: str | Path = "/usr/local/share/gentoo-installer",
                 secret_dir: str | Path = "/run/gentoo-installer-sb",
                 live_network_dir: str | Path = "/etc/NetworkManager/system-connections",
                 plan: object | None = None) -> None:
        self.runner = runner
        self.root = Path(target_root)
        self.dry_run = dry_run
        self.companion_bundle = Path(companion_bundle)
        self.secret_dir = Path(secret_dir)  # where the live system keeps signing material
        self.secret_chroot_dir = "/run/gentoo-installer-sb"  # the same directory as seen from the chroot
        self.live_network_dir = Path(live_network_dir)
        self.hardware_plan = plan
        self.notify: Callable[[str], None] = lambda message: None
        self.sleep: Callable[[float], None] = time.sleep
        self.resources: Callable[[], Resources] = machine_resources
        # The daemon's runner takes root_identity=True so chrooted tools run as root:root and do
        # not inherit the live service's group, and capture=False so the output of a long emerge
        # is streamed to the log instead of being kept in memory; other runners (tests) do not
        # know these keywords.
        self._root_identity = accepts_keyword(runner.run, "root_identity")
        self._can_skip_capture = accepts_keyword(runner.run, "capture")

    # ----- paths and files ------------------------------------------------------------------

    def path(self, relative: str | Path) -> Path:
        value = str(relative)
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ConfigurationError("Invalid target path")
        pure = PurePosixPath(value)
        if ".." in pure.parts:
            raise ConfigurationError(f"Target path escapes installation root: {value}")
        candidate = self.root / value.lstrip("/")
        root_resolved = self.root.resolve()
        resolved = candidate.resolve()
        if os.path.commonpath((str(root_resolved), str(resolved))) != str(root_resolved):
            raise ConfigurationError(f"Target path escapes installation root: {value}")
        return resolved

    @staticmethod
    def _as_root() -> bool:
        return hasattr(os, "geteuid") and os.geteuid() == 0

    def _mkdirs(self, directory: Path, mode: int = 0o755) -> None:
        """Create missing parent directories as root:root, never with the live service's group."""
        missing: list[Path] = []
        current = directory
        while not current.exists() and current != current.parent:
            missing.append(current)
            current = current.parent
        for created in reversed(missing):
            created.mkdir(mode=mode)
            created.chmod(mode)
            if self._as_root():
                os.chown(created, 0, 0)

    def write(self, relative: str | Path, content: str, mode: int = 0o644) -> None:
        self.write_bytes(relative, content.encode("utf-8"), mode)

    def write_bytes(self, relative: str | Path, data: bytes, mode: int = 0o644) -> None:
        path = self.path(relative)
        if self.dry_run:
            return
        self._mkdirs(path.parent)
        path = self.path(relative)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fchmod(handle.fileno(), mode)
                if self._as_root():
                    os.fchown(handle.fileno(), 0, 0)
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        try:
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def read_text(self, relative: str | Path) -> str:
        path = self.path(relative)
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def set_shell_variable(self, relative: str | Path, key: str, value: str) -> None:
        """Replace an active KEY=... assignment in a shell-style config file, or append one."""
        old = "" if self.dry_run else self.read_text(relative)
        kept = [line for line in old.splitlines() if not re.match(rf"\s*{re.escape(key)}=", line)]
        kept.append(f"{key}={shell_value(value)}")
        self.write(relative, "\n".join(line for line in kept).strip("\n") + "\n")

    def chroot(self, argv: Sequence[str], *, stdin: str | None = None, secret: bool = False,
               env: Mapping[str, str] | None = None, capture: bool = False) -> CommandResult:
        """Run ``argv`` inside the target. Its output is only logged unless ``capture`` asks to read it back."""
        extra: dict[str, Any] = {}
        if env:
            extra["env"] = env
        if self._root_identity:
            extra["root_identity"] = True
        if self._can_skip_capture and not capture:
            extra["capture"] = False
        return self.runner.run(("chroot", str(self.root), *argv), stdin=stdin, secret=secret, **extra)

    # ----- Portage configuration ------------------------------------------------------------------

    def _plan(self, plan: object | None) -> object | None:
        return plan if plan is not None else self.hardware_plan

    def effective_make_opts(self, manifest: InstallManifest, plan: object | None = None) -> str:
        """Explicit MAKEOPTS, else the hardware plan's, else a value sized for this machine."""
        if manifest.portage.make_opts:
            return manifest.portage.make_opts
        planned = str((getattr(self._plan(plan), "make_conf", None) or {}).get("MAKEOPTS", "")).strip()
        if planned:
            return planned
        resources = self.resources()
        return auto_make_opts(resources.cpus, resources.mem_available, manifest.storage.swap_mib)

    def emerge_environment(self, manifest: InstallManifest) -> dict[str, str]:
        """Environment for the installer's own chroot emerges: proxy and parallel package builds."""
        environment = proxy_environment(manifest.network.proxy)
        if "--jobs" not in manifest.portage.emerge_opts:
            resources = self.resources()
            jobs = auto_emerge_jobs(resources.cpus, resources.mem_total)
            if jobs > 1:
                environment["EMERGE_DEFAULT_OPTS"] = f"{manifest.portage.emerge_opts} --jobs={jobs} --load-average={resources.cpus}".strip()
        return environment

    def package_use_table(self, manifest: InstallManifest, plan: object | None = None) -> dict[str, list[str]]:
        """The installer's package.use: the user's entries plus everything the chosen options need."""
        package_use = {atom: list(flags) for atom, flags in manifest.portage.package_use.items()}
        system, options, storage = manifest.system, manifest.options, manifest.storage
        if system.initramfs == "dracut":
            # Distribution kernels with initramfs require this installkernel hook
            # before the kernel package is merged.
            _require_use(package_use, "sys-kernel/installkernel", "dracut")
        if system.bootloader == "grub":
            # Regenerate grub.cfg whenever a kernel is installed (layout=grub keeps the files in /boot).
            _require_use(package_use, "sys-kernel/installkernel", "grub")
        if system.bootloader == "grub" and system.dual_boot:
            # os-prober needs grub's mount support to read the other systems' filesystems.
            _require_use(package_use, "sys-boot/grub", "mount")
        if system.uki or system.bootloader == "systemd-boot":
            atom = self.systemd_package(system)
            boot_flags = ["boot", *(["kernel-install"] if system.init == "openrc" else []), *(["ukify"] if system.uki else [])]
            package_use[atom] = [flag for flag in package_use.get(atom, []) if flag not in ("-boot", "-kernel-install", "-ukify")]
            package_use[atom].extend(flag for flag in boot_flags if flag not in package_use[atom])
        if options.install_hwmonux:
            # Minimal profiles do not enable the GUI flags required by PySide/Qt.
            _require_use(package_use, "sys-apps/i2c-tools", "python")
            _require_use(package_use, "dev-qt/qtbase", "opengl")
            _require_use(package_use, "dev-qt/qttools", "opengl")
            _require_use(package_use, "x11-libs/libxkbcommon", "X")
            # NVIDIA sensors need NVML, which needs the licence-masked proprietary driver: the hardware plan
            # turns that flag on when the driver is really installed. USB sensors (liquidctl) are optional.
            _default_use(package_use, "app-misc/hwmonux", "-nvidia", "liquidctl")
        if options.install_hwmonux or options.install_portagehub:
            # Both companions can pull in Mesa[X], which requires libglvnd[X] on the minimal profile.
            _require_use(package_use, "media-libs/libglvnd", "X")
        # The official binary QtWebEngine (KDE pulls it in) is built with bindist; without the flag the
        # binary is ignored and Chromium is compiled from source for hours. bindist only leaves out
        # proprietary codecs in embedded web views. Set with and without the binary host, so switching
        # it later rebuilds nothing; a user's own choice for the flag wins.
        _default_use(package_use, "dev-qt/qtwebengine", "bindist")
        if manifest.network.manager == "networkmanager":
            # NetworkManager uses wpa_supplicant's D-Bus interface for Wi-Fi.
            _require_use(package_use, "net-wireless/wpa_supplicant", "dbus")
        if storage.lvm:
            # lvm2[-lvm] installs only device-mapper, without the tools needed to activate the root volume.
            _require_use(package_use, "sys-fs/lvm2", "lvm")
        if options.install_portagehub and system.init == "systemd":
            # PortageHub's polkit dependency needs systemd's policykit support even on the minimal profile.
            _require_use(package_use, "sys-apps/systemd", "policykit")
        if system.init == "systemd" and storage.luks:
            # Dracut's systemd-cryptsetup module needs the helper installed by systemd[cryptsetup].
            _require_use(package_use, "sys-apps/systemd", "cryptsetup")
        if manifest.accounts.privilege == "doas":
            # doas.conf uses "persist", which the package only honours when built with it.
            _require_use(package_use, "app-admin/doas", "persist")
        if system.desktop != "minimal":
            # Current binary libwebp[tiff] and tiff[webp] depend on each other.
            _forbid_use(package_use, "media-libs/libwebp", "tiff")
            for atom, flags in DESKTOP_USE.get(system.desktop, {}).items():
                _require_use(package_use, atom, *flags)
        return package_use

    def configure_portage(self, manifest: InstallManifest, plan: object | None = None) -> None:
        plan = self._plan(plan)
        self.write("etc/portage/make.conf", render_make_conf(manifest, plan, make_opts=self.effective_make_opts(manifest, plan)))
        self.write("etc/portage/package.use/installer", render_package_use(self.package_use_table(manifest, plan)))
        hardware_use = getattr(plan, "package_use", None)
        if hardware_use:
            # Alphabetically after "installer", so within one directory the hardware lines win.
            self.write("etc/portage/package.use/installer-hardware", render_package_use(hardware_use))
        hardware_licenses = getattr(plan, "package_license", None)
        if hardware_licenses:
            self.write("etc/portage/package.license/installer-hardware",
                       "".join(f"{atom} {' '.join(names)}\n" for atom, names in sorted(hardware_licenses.items())))
        hardware_mask = _plan_list(plan, "package_mask")
        if hardware_mask:
            self.write("etc/portage/package.mask/installer-hardware", "".join(f"{entry}\n" for entry in hardware_mask))
        if manifest.portage.binhost:
            self.write("etc/portage/binrepos.conf/gentoo.conf", f"[gentoo]\npriority = 9999\nsync-uri = {manifest.portage.binhost_url}\n")
        if manifest.portage.rsync_mirror:
            if not manifest.portage.rsync_mirror.startswith(("rsync://", "https://")):
                raise ConfigurationError("Repository sync mirror must use rsync or HTTPS")
            self.write("etc/portage/repos.conf/gentoo.conf", f"[gentoo]\nlocation = /var/db/repos/gentoo\nsync-type = {'rsync' if manifest.portage.rsync_mirror.startswith('rsync:') else 'git'}\nsync-uri = {manifest.portage.rsync_mirror}\nauto-sync = yes\n")

    # ----- identity, accounts, network --------------------------------------------------------------

    def resolve_console_keymap(self, manifest: InstallManifest) -> str:
        """The console keymap to write: the chosen one if kbd has it, else US with a warning."""
        system = manifest.system
        wanted = system.keymap
        if wanted in {"", "auto"}:
            wanted = keymaps.console_keymap_for(system.keyboard_layout, system.keyboard_variant)
        if self.dry_run or wanted == "us":
            return wanted
        available = keymaps.list_console_keymaps((self.path("usr/share/keymaps"), self.path("usr/share/kbd/keymaps")))
        if available and wanted not in available:
            close = ", ".join(keymaps.near_matches(wanted, sorted(available))) or "none"
            self.notify(f"warning: the console keymap {wanted!r} does not exist on the installed system (similar: {close}); using 'us'")
            return "us"
        return wanted

    def luks_prompt_uses_console_keymap(self, manifest: InstallManifest) -> bool:
        """True when the console keymap was chosen through the keyboard layout, so the LUKS prompt can use it.

        The passphrase is typed in the live session; the boot prompt may only use a non-US map when
        that layout was also active while typing (the layout chooser switches it).
        """
        system = manifest.system
        derived = keymaps.console_keymap_for(system.keyboard_layout, system.keyboard_variant)
        keymap = system.keymap if system.keymap not in {"", "auto"} else derived
        return keymap != "us" and derived == keymap

    def keyboard_files(self, manifest: InstallManifest) -> list[tuple[str, str, int]]:
        """X11/Wayland keyboard layout for display managers and sessions (graphical desktops only)."""
        system = manifest.system
        if system.desktop == "minimal" or _keyboard_is_default(system):
            return []
        files = [("etc/X11/xorg.conf.d/00-keyboard.conf", render_xorg_keyboard(system), 0o644)]
        if system.desktop == "kde":
            # Plasma (X11 and Wayland) and the SDDM greeter read kxkbrc, not xorg.conf.d.
            files.append(("etc/xdg/kxkbrc", render_kxkbrc(system), 0o644))
        elif system.desktop == "gnome":
            files.append(("etc/dconf/profile/user", "user-db:user\nsystem-db:local\n", 0o644))
            files.append(("etc/dconf/db/local.d/00-gentoo-installer-keyboard", render_dconf_keyboard(system), 0o644))
        return files

    def configure_identity(self, manifest: InstallManifest) -> None:
        accounts, system = manifest.accounts, manifest.system
        keymap = self.resolve_console_keymap(manifest)
        self.write("etc/hostname", accounts.hostname + "\n")
        self.write("etc/conf.d/hostname", f'hostname="{accounts.hostname}"\n')
        self.write("etc/hosts", render_hosts(accounts.hostname))
        locales = [system.locale] if system.locale in {"C", "C.UTF-8", "POSIX"} else [system.locale, "en_US.UTF-8"]
        supported = "" if self.dry_run else self.read_text("usr/share/i18n/SUPPORTED")
        self.write("etc/locale.gen", "".join(locale_gen_line(name, supported) + "\n" for name in dict.fromkeys(locales)
                                            if name not in {"C", "C.UTF-8", "POSIX"}))
        self.write("etc/env.d/02locale", f"LANG={shell_value(system.locale)}\n")
        # systemd, its user manager and localed read /etc/locale.conf, not env.d.
        self.write("etc/locale.conf", f"LANG={system.locale}\n")
        self.write("etc/conf.d/keymaps", f'keymap="{keymap}"\n')
        vconsole = f"KEYMAP={keymap}\n" + (f"FONT={system.console_font}\n" if system.console_font else "")
        self.write("etc/vconsole.conf", vconsole)
        if system.console_font:
            self.write("etc/conf.d/consolefont", f'consolefont="{system.console_font}"\n')
        if system.rtc_local:
            # The hardware clock keeps local time (Windows dual boot); hwclock and systemd read adjtime.
            self.write("etc/adjtime", "0.0 0 0.0\n0\nLOCAL\n")
            self.write("etc/conf.d/hwclock", 'clock="local"\n')
        for path, content, mode in self.keyboard_files(manifest):
            self.write(path, content, mode)
        zone = self.path(f"usr/share/zoneinfo/{system.timezone}")
        if not self.dry_run and not zone.is_file():
            raise ConfigurationError(f"Timezone is not installed in the target: {system.timezone}")
        localtime = self.path("etc") / "localtime"
        if not self.dry_run:
            localtime.parent.mkdir(parents=True, exist_ok=True)
            localtime.unlink(missing_ok=True)
            localtime.symlink_to(f"/usr/share/zoneinfo/{system.timezone}")
        self.write("etc/timezone", system.timezone + "\n")
        self.chroot(("locale-gen",))

    def group_exists(self, name: str) -> bool:
        try:
            self.chroot(("getent", "group", name))
        except RunnerError:
            return False
        return True

    def configure_accounts(self, manifest: InstallManifest) -> None:
        a = manifest.accounts
        group_names = list(a.groups)
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", group) for group in group_names):
            raise ConfigurationError("Account groups contain an invalid name")
        # PortageHub reads the emerge log and the binary package cache through this group.
        if manifest.options.install_portagehub and "portage" not in group_names and self.group_exists("portage"):
            group_names.append("portage")
        groups = ",".join(group_names)
        command = ["useradd", "--create-home", "--shell", "/bin/bash"]
        if a.full_name:
            command.extend(("--comment", a.full_name))
        if groups:
            command.extend(("--groups", groups))
        command.append(a.username)
        try:
            if self.dry_run:
                raise RunnerError(("id",), 1)
            self.chroot(("id", "-u", a.username))
        except RunnerError:
            self.chroot(tuple(command))
        pairs = [(a.username, a.password)]
        if not a.disable_root_login:
            pairs.append(("root", a.root_password))
        payload = "".join(f"{user}:{password}\n" for user, password in pairs)
        self.chroot(("chpasswd",), stdin=payload, secret=True)
        if a.disable_root_login:
            self.chroot(("passwd", "--lock", "root"))
        if a.ssh_authorized_keys:
            if any("\n" in key or "\r" in key for key in a.ssh_authorized_keys):
                raise ConfigurationError("SSH authorized keys must be single-line keys")
            path = f"home/{a.username}/.ssh/authorized_keys"
            self.write(path, "\n".join(a.ssh_authorized_keys) + "\n", 0o600)
            self.chroot(("chown", "-R", f"{a.username}:{a.username}", f"/home/{a.username}/.ssh"))
            self.chroot(("chmod", "700", f"/home/{a.username}/.ssh"))
        self.chroot(("env-update",))

    def configure_privileges(self, manifest: InstallManifest) -> None:
        if manifest.accounts.privilege == "sudo":
            self.write("etc/sudoers.d/wheel", "%wheel ALL=(ALL:ALL) ALL\n", 0o440)
        else:
            self.write("etc/doas.conf", "permit persist :wheel\n", 0o600)

    def configure_network(self, manifest: InstallManifest) -> None:
        n = manifest.network
        if n.manager == "networkmanager":
            self.write("etc/NetworkManager/conf.d/dhcp.conf", "[main]\ndhcp=internal\n")
            if n.wifi_ssid:
                self._write_profile("installer-wifi.nmconnection", render_nm_wifi(n))
            if not n.dhcp and not n.interface.startswith("wl"):
                self._write_profile("installer-wired.nmconnection", render_nm_wired(n))
            if n.import_live_profiles:
                self.import_live_network_profiles(manifest)
        elif n.manager == "dhcpcd":
            if not n.dhcp:
                render_dhcpcd_static(manifest)  # validates the settings now; the block is added once the package exists
        elif n.manager == "netifrc":
            self.write("etc/conf.d/net", render_netifrc(manifest))
        else:
            self.write("etc/systemd/network/20-wired.network", render_systemd_network(manifest))
        if n.dns and n.manager not in {"systemd-networkd", "dhcpcd"}:
            self.write("etc/resolv.conf", "".join(f"nameserver {server}\n" for server in n.dns))

    def _write_profile(self, name: str, content: str) -> None:
        directory = self.path("etc/NetworkManager/system-connections")
        if not self.dry_run:
            self._mkdirs(directory, 0o700)
        self.write(f"etc/NetworkManager/system-connections/{name}", content, 0o600)

    def configure_network_late(self, manifest: InstallManifest) -> None:
        """Settings that extend a file the network package installs (so they run after it)."""
        n = manifest.network
        if n.manager == "dhcpcd" and not n.dhcp:
            marker = "# Static configuration written by the Gentoo live installer"
            stock = "" if self.dry_run else self.read_text("etc/dhcpcd.conf")
            if marker not in stock:
                self.write("etc/dhcpcd.conf", stock.rstrip("\n") + ("\n" if stock else "") + render_dhcpcd_static(manifest))

    def import_live_network_profiles(self, manifest: InstallManifest) -> list[str]:
        """Copy this session's NetworkManager connections (Wi-Fi, VPN, enterprise) to the target."""
        imported: list[str] = []
        if self.dry_run or not self.live_network_dir.is_dir():
            return imported
        portable = manifest.hardware.target == "portable"
        for source in sorted(self.live_network_dir.glob("*.nmconnection")):
            if source.name.startswith("installer-") or not source.is_file():
                continue
            try:
                profile = rewrite_keyfile(source.read_text(encoding="utf-8"), portable=portable)
            except (OSError, UnicodeDecodeError):
                self.notify(f"warning: network profile {source.name} could not be read and was skipped")
                continue
            if profile is None:
                continue
            text = profile.text
            missing = False
            for index, (placeholder, live_path) in enumerate(profile.certificates):
                certificate = Path(live_path)
                try:
                    if not live_path.startswith(CERTIFICATE_ROOTS) or ".." in certificate.parts or not certificate.is_file() \
                            or certificate.stat().st_size > 1024 * 1024:
                        missing = True
                        break
                    data = certificate.read_bytes()
                except OSError:
                    missing = True
                    break
                target_name = f"etc/NetworkManager/certs/{hashlib.sha256(source.name.encode()).hexdigest()[:8]}-{index}-{certificate.name}"
                self.write_bytes(target_name, data, 0o600)
                text = text.replace(placeholder, "/" + target_name)
            if missing:
                self.notify(f"warning: network profile {source.name} refers to a certificate that is missing; skipped")
                continue
            self._write_profile(source.name, text)
            imported.append(source.stem)
        if imported:
            self.notify("Copied network connections from this session: " + ", ".join(imported))
        return imported

    def finalize_network(self, manifest: InstallManifest) -> None:
        """Hand /etc/resolv.conf to systemd-resolved once nothing else needs name resolution."""
        if manifest.system.init != "systemd" or manifest.network.manager != "systemd-networkd" or self.dry_run:
            return
        resolv = self.path("etc") / "resolv.conf"
        resolv.unlink(missing_ok=True)
        resolv.symlink_to("../run/systemd/resolve/stub-resolv.conf")

    # ----- package sets ------------------------------------------------------------------------

    def early_boot_atoms(self, manifest: InstallManifest, plan: object | None = None) -> list[str]:
        """Firmware and microcode: the hardware plan decides (it reads options.firmware and options.microcode)."""
        return list(dict.fromkeys(str(atom) for atom in _plan_list(self._plan(plan), "early_atoms")))

    def filesystem_atoms(self, manifest: InstallManifest, layout: Sequence[LayoutEntry] = ()) -> list[str]:
        """Userland for the filesystems in use, so the installed system can check and repair them."""
        filesystems = {entry.filesystem for entry in layout}
        filesystems.add(manifest.storage.filesystem)
        for partition in manifest.storage.partitions:
            filesystems.add(partition.filesystem)
        atoms: list[str] = []
        if "vfat" in filesystems or manifest.storage.firmware == "uefi":
            atoms.append("sys-fs/dosfstools")
        if "xfs" in filesystems:
            atoms.append("sys-fs/xfsprogs")
        if "btrfs" in filesystems:
            atoms.append("sys-fs/btrfs-progs")
        return atoms

    def storage_tool_atoms(self, manifest: InstallManifest, stack: ExistingStack | None = None) -> list[str]:
        """Tools dracut needs on the target to build an initramfs for the storage stack."""
        atoms: list[str] = []
        if manifest.storage.luks:
            atoms.append("sys-fs/cryptsetup")
        if manifest.storage.lvm or (stack is not None and stack.lvm):
            atoms.append("sys-fs/lvm2")
        if manifest.storage.raid_level or (stack is not None and stack.md):
            atoms.append("sys-fs/mdadm")
        return atoms

    def kernel_stage_atoms(self, manifest: InstallManifest, layout: Sequence[LayoutEntry] = (), plan: object | None = None,
                           stack: ExistingStack | None = None) -> list[str]:
        """What must exist before the kernel and initramfs are built."""
        # The console font is built into the initramfs, which copies every font installed when it is built.
        fonts = ["media-fonts/terminus-font"] if manifest.system.console_font.startswith("ter-") else []
        return list(dict.fromkeys([*self.storage_tool_atoms(manifest, stack), *self.early_boot_atoms(manifest, plan),
                                   *self.filesystem_atoms(manifest, layout), *fonts]))

    def kernel_atoms(self, manifest: InstallManifest) -> list[str]:
        atoms = ["sys-kernel/installkernel"]
        if manifest.system.kernel != "manual":
            atoms.append(f"sys-kernel/{manifest.system.kernel}")
        if manifest.system.initramfs == "dracut":
            atoms.append("sys-kernel/dracut")
        return atoms

    @staticmethod
    def systemd_package(system: System) -> str:
        """The stage3 package that carries bootctl, ukify, the systemd-boot images and (on systemd) the cryptsetup helper."""
        return "sys-apps/systemd" if system.init == "systemd" else "sys-apps/systemd-utils"

    def flag_rebuild(self, manifest: InstallManifest, plan: object | None = None) -> tuple[str, list[tuple[str, str]]] | None:
        """The stage3 package whose USE flags the installer changes, and what must exist once they took effect.

        systemd (systemd-utils on OpenRC) is part of every stage3, so ``emerge --noreplace`` skips it and the
        flags written to package.use (cryptsetup for the initramfs, boot for systemd-boot, ukify for a Unified
        Kernel Image) would never apply. The package is rebuilt once with ``--update --newuse``. The pairs are a
        path in the target and the sentence for when that file is missing afterwards; None when nothing is needed.
        """
        system = manifest.system
        crypt_helper = system.init == "systemd" and manifest.storage.luks
        systemd_boot = system.bootloader == "systemd-boot"
        if not (crypt_helper or systemd_boot or system.uki):
            return None
        atom = self.systemd_package(system)
        arch = efi_arch(self._plan(plan))
        images = "usr/lib/systemd/boot/efi"

        def missing(name: str, flag: str, purpose: str) -> str:
            return f"{name} is missing after rebuilding {atom} with the {flag} USE flag, so {purpose}"

        expected: list[tuple[str, str]] = []
        if crypt_helper:
            expected.append(("usr/lib/systemd/systemd-cryptsetup", "systemd-cryptsetup is missing after enabling systemd[cryptsetup]"))
        if systemd_boot:
            purpose = "systemd-boot cannot be installed"
            if system.init == "openrc":  # systemd always has bootctl; its images come with the boot flag
                expected.append(("usr/bin/bootctl", missing("bootctl", "boot", purpose)))
            expected.append((f"{images}/systemd-boot{arch}.efi", missing(f"systemd-boot{arch}.efi", "boot", purpose)))
        if system.uki:
            purpose = "the Unified Kernel Image cannot be built"
            expected.append(("usr/bin/ukify", missing("ukify", "ukify", purpose)))
            expected.append((f"{images}/linux{arch}.efi.stub", missing(f"linux{arch}.efi.stub", "boot", purpose)))
        return atom, expected

    def bootloader_atoms(self, manifest: InstallManifest) -> list[str]:
        system = manifest.system
        systemd_package = self.systemd_package(system)
        atoms = [{"grub": "sys-boot/grub", "systemd-boot": systemd_package, "efi-stub": "sys-boot/efibootmgr"}[system.bootloader]]
        if system.bootloader == "grub" and system.dual_boot:
            atoms.append("sys-boot/os-prober")
        if system.uki and systemd_package not in atoms:
            atoms.append(systemd_package)
        if system.secure_boot:
            atoms.append("app-crypt/sbsigntools")
        return atoms

    def essential_atoms(self, manifest: InstallManifest, plan: object | None = None) -> list[str]:
        """Packages without which the installed system cannot be managed from a console."""
        system, options = manifest.system, manifest.options
        atoms: list[str] = []
        network_package = {"networkmanager": "net-misc/networkmanager", "dhcpcd": "net-misc/dhcpcd",
                           "netifrc": "net-misc/netifrc"}.get(manifest.network.manager)
        if network_package:
            atoms.append(network_package)
        atoms.extend(self.early_boot_atoms(manifest, plan))
        atoms.append("app-admin/sudo" if manifest.accounts.privilege == "sudo" else "app-admin/doas")
        if options.ssh:
            atoms.append("net-misc/openssh")
        if options.chrony:
            atoms.append("net-misc/chrony")
        if self.wants_cron(manifest):
            atoms.append("sys-process/cronie")
        if options.file_indexing:
            atoms.append("sys-apps/mlocate")
        if options.shell_completion:
            atoms.append("app-shells/bash-completion")
        if options.logger:
            if system.init == "openrc":
                # syslog-ng writes /var/log/messages forever without a rotator.
                atoms.extend(("app-admin/syslog-ng", "app-admin/logrotate"))
            else:
                atoms.append("sys-apps/systemd")
        if system.console_font.startswith("ter-"):
            atoms.append("media-fonts/terminus-font")
        return list(dict.fromkeys(atoms))

    def wants_cron(self, manifest: InstallManifest) -> bool:
        """cron runs logrotate, so a ticked OpenRC logger brings a cron daemon with it."""
        return manifest.options.cron or (manifest.options.logger and manifest.system.init == "openrc")

    def desktop_atoms(self, manifest: InstallManifest, plan: object | None = None) -> DesktopAtoms:
        system, options = manifest.system, manifest.options
        plan = self._plan(plan)
        graphical = system.desktop != "minimal"
        result = DesktopAtoms()
        if system.desktop == "kde":
            result.required.extend((
                "kde-plasma/plasma-meta", "kde-apps/kde-apps-meta" if options.kde_apps == "full" else "kde-apps/kdecore-meta",
                "gui-apps/sddm", "x11-base/xorg-server"))
        elif system.desktop == "gnome":
            result.required.extend(("gnome-base/gnome", "gnome-base/gdm", "x11-base/xorg-server"))
        elif system.desktop == "xfce":
            # The panel's polkit agent is not pulled in on every profile.
            result.required.extend(("xfce-base/xfce4-meta", "x11-base/xorg-server", "x11-misc/lightdm",
                                    "x11-misc/lightdm-gtk-greeter", "gnome-extra/polkit-gnome"))
        if graphical and system.init == "openrc":
            result.required.append("gui-libs/display-manager-init")
        result.required.extend(str(atom) for atom in _plan_list(plan, "required_atoms"))
        if options.pipewire:
            result.optional.append("media-video/pipewire")
        if options.bluetooth:
            result.optional.append("net-wireless/bluez")
            if graphical:
                result.optional.append("net-wireless/blueman")  # the GUI needs a toolkit and X
        if options.printing:
            result.optional.append("net-print/cups")
        if options.flatpak:
            result.optional.append("sys-apps/flatpak")
        if graphical:
            result.optional.extend(("sys-fs/exfatprogs", "sys-fs/ntfs3g"))
        if options.install_hwmonux:
            result.optional.append("app-misc/hwmonux")
        if options.install_portagehub:
            result.optional.append("app-portage/portagehub")
        for group in _plan_list(plan, "optional_atoms"):
            if isinstance(group, str):
                result.groups.append((group, [group]))
            else:
                label, atoms = group
                result.groups.append((str(label), [str(atom) for atom in atoms]))
        result.extras.extend(options.extra_packages)
        result.required[:] = list(dict.fromkeys(result.required))
        result.optional[:] = [atom for atom in dict.fromkeys(result.optional) if atom not in result.required]
        return result

    def package_atoms(self, manifest: InstallManifest, plan: object | None = None) -> list[str]:
        """Every package the installer asks for, in install order (the union of the stage sets)."""
        atoms = [*self.essential_atoms(manifest, plan), *self.bootloader_atoms(manifest),
                 *self.desktop_atoms(manifest, plan).everything()]
        return list(dict.fromkeys(atoms))

    # ----- installing packages --------------------------------------------------------------------

    def emerge(self, manifest: InstallManifest, arguments: Sequence[str], *, environment: Mapping[str, str] | None = None,
               capture: bool = False, one_job: bool = False) -> CommandResult:
        """Run emerge in the new system. ``one_job`` is the retry of a build that was killed for lack of memory: one compile job and one package at a time."""
        env = self.emerge_environment(manifest)
        env.update(environment or {})
        arguments = tuple(arguments)
        if one_job:
            # NINJAOPTS too: unset, ninja-utils derives it from MAKEOPTS, but an inherited value would win over that.
            env.update({"MAKEOPTS": "-j1", "NINJAOPTS": ninja_opts_from_make_opts("-j1")})
            arguments = ("--jobs=1", *arguments)  # the command line wins over --jobs in EMERGE_DEFAULT_OPTS
        return self.chroot(("emerge", *arguments), env=env, capture=capture)

    # ----- why a build failed --------------------------------------------------------------------------

    def build_log_tail(self, path: str) -> str:
        """The end of a failed package's build log. emerge names the log as the new system sees it (inside the chroot); '' when it cannot be read."""
        if self.dry_run or not path.startswith("/"):
            return ""
        try:
            log = self.path(path)
            if not stat.S_ISREG(log.stat().st_mode):
                return ""
            with log.open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                handle.seek(max(0, size - BUILD_LOG_TAIL_BYTES))
                return handle.read(BUILD_LOG_TAIL_BYTES).decode("utf-8", errors="replace")
        except (OSError, ConfigurationError):
            return ""

    def explain(self, failure: RunnerError | str) -> EmergeDiagnosis:
        """Explain a failed emerge from its output and from the build logs of the packages that failed."""
        return explain_emerge_failure(failure if isinstance(failure, str) else failure.output, self.build_log_tail)

    def compile_jobs(self, package: str) -> int | None:
        """The compile jobs a package (category/name-version) gets: MAKEOPTS of the new system, or the lower limit of the hardware plan; None when unknown or unlimited."""
        try:
            jobs = recommend.make_opts_jobs(self._variable("etc/portage/make.conf", "MAKEOPTS"))
            listed = {fields[0] for line in self.read_text(PACKAGE_ENV_FILE).splitlines() for fields in [line.split("#")[0].split()]
                      if len(fields) > 1 and MEMORY_ENV_NAME in fields[1:]}
            limit = recommend.make_opts_jobs(self._variable(MEMORY_ENV_FILE, "MAKEOPTS")) if package_of(package) in listed else None
        except (OSError, ValueError, ConfigurationError):
            return None
        return limit if limit is not None and (jobs is None or limit < jobs) else jobs

    def _variable(self, relative: str, name: str) -> str:
        """The value of NAME=... in a shell-style file of the new system ('' when it has none)."""
        for line in reversed(self.read_text(relative).splitlines()):
            if line.startswith(f"{name}="):
                return shlex.split(line)[0].partition("=")[2]
        return ""

    def one_job_cannot_help(self, manifest: InstallManifest, diagnosis: EmergeDiagnosis) -> bool:
        """Whether the packages that were killed already had one compile job and were built alone: a retry with one job would end the same way."""
        opts = self.emerge_environment(manifest).get("EMERGE_DEFAULT_OPTS", manifest.portage.emerge_opts)
        parallel = recommend.make_opts_jobs(opts)
        if parallel is None and re.search(r"(?:^|\s)(?:-j|--jobs)", opts) or parallel is not None and parallel > 1:
            return False  # other packages were built at the same time
        return bool(diagnosis.failures) and all(self.compile_jobs(failure.package) == 1 for failure in diagnosis.failures)

    def is_installed(self, atom: str) -> bool:
        if self.dry_run:
            return True
        category, _, name = atom.split(":")[0].partition("/")
        directory = self.path(f"var/db/pkg/{category}")
        return directory.is_dir() and any(re.fullmatch(re.escape(name) + r"-[0-9].*", entry.name) for entry in directory.iterdir())

    def pretend(self, manifest: InstallManifest, atoms: Sequence[str]) -> None:
        """Resolve the package set without installing it, so impossible choices fail in minutes.

        A dependency cycle that emerge knows how to break is broken here, before the stages run.
        """
        if not atoms:
            return
        # --noreplace never looks at a package that is installed, so the rebuild that the kernel stage does for
        # the flags of systemd(-utils) (see flag_rebuild) gets its own run; it shows what the new flags pull in.
        rebuild = self.flag_rebuild(manifest)
        for attempt in range(MAX_CYCLE_BREAKS + 1):
            try:
                # Same sources as the real install (binary packages included): the gate must predict
                # whether the installation would fail, not be stricter than it. scripts/lint-plan.py is
                # the strict, source-only check that runs in development.
                self.emerge(manifest, ("--pretend", "--noreplace", "--verbose=n", *atoms))
                if rebuild is not None:
                    self.emerge(manifest, ("--pretend", "--oneshot", "--update", "--newuse", "--verbose=n", rebuild[0]))
                return
            except RunnerError as exc:
                suggestion = cycle_break_suggestion(exc.output)
                if suggestion is not None and attempt < MAX_CYCLE_BREAKS:
                    self.break_cycle(manifest, *suggestion)
                    continue
                diagnosis = self.explain(exc)
                if diagnosis.resolution:
                    raise PackageInstallError(f"Portage cannot resolve the selected packages: {diagnosis.summary}", resolution=True) from exc
                self.notify("warning: the package resolution check could not run (" + diagnosis.summary[:200] + "); continuing")
                return

    def break_cycle(self, manifest: InstallManifest, cpv: str, flags: Sequence[str]) -> None:
        """Build one package with the USE change emerge suggested, as a Gentoo user breaks a cycle by hand.

        The change applies to that package only (a temporary package.use file), and the package is
        recorded in the new system so the desktop stage rebuilds it with the configured flags, also
        after a resume.
        """
        change = " ".join(flags)
        self.notify(f"Breaking a dependency cycle: building {cpv} once with USE=\"{change}\"")
        self.write(CYCLE_BREAK_USE, f"={cpv} {change}\n")
        try:
            self.emerge(manifest, ("--oneshot", f"={cpv}"))
        except RunnerError as exc:
            raise PackageInstallError(f"A dependency cycle could not be broken by building {cpv} with USE=\"{change}\": "
                                      + self.explain(exc).summary, resolution=True) from exc
        finally:
            if not self.dry_run:
                self.path(CYCLE_BREAK_USE).unlink(missing_ok=True)
        record = self.path(CYCLE_BREAK_RECORD)
        recorded = record.read_text().split() if record.is_file() else []
        if package_of(cpv) not in recorded:
            self.write(CYCLE_BREAK_RECORD, "\n".join([*recorded, package_of(cpv)]) + "\n")

    def restore_cycle_breaks(self, manifest: InstallManifest) -> list[str]:
        """Rebuild the packages a cycle break built with other flags; returns what is still left over."""
        record = self.path(CYCLE_BREAK_RECORD)
        if self.dry_run or not record.is_file():
            return []
        problems: list[str] = []
        for package in record.read_text().split():
            try:
                self.emerge(manifest, ("--oneshot", "--update", "--newuse", package))
            except RunnerError as exc:
                problems.append(f"{package} keeps the flags used to break a dependency cycle; later run: emerge --oneshot --newuse {package} "
                                f"({self.explain(exc).summary[:160]})")
        record.unlink(missing_ok=True)
        return problems

    def install_packages(self, manifest: InstallManifest, plan: object | None = None) -> None:
        """Install the whole package set with one emerge (the stages use install_essential/install_desktop)."""
        self.configure_companion_overlay(manifest)
        atoms = self.package_atoms(manifest, plan)
        if atoms:
            self.emerge(manifest, ("--noreplace", *atoms))

    def install_essential(self, manifest: InstallManifest, plan: object | None = None, attempts: int = 3) -> None:
        """Install the base system packages; this must succeed or the disk is not bootable.

        A dropped download is retried, a build that ran out of memory is retried with one compile
        job, anything else is reported at once.
        """
        atoms = self.essential_atoms(manifest, plan)
        if not atoms:
            return
        one_job = False
        for attempt in range(1, attempts + 1):
            try:
                self.emerge(manifest, ("--noreplace", *atoms), one_job=one_job)
                return
            except RunnerError as exc:
                diagnosis = self.explain(exc)
                hopeless = self.one_job_is_no_help(manifest, diagnosis, one_job)
                if diagnosis.kind in {"resolution", "build"} or attempt == attempts or hopeless:
                    raise PackageInstallError(f"The base system packages could not be installed: {self.memory_advice(diagnosis, hopeless)}",
                                              resolution=diagnosis.resolution) from exc
                if diagnosis.kind == "memory":
                    one_job = True
                    self.notify(f"Base system packages: {diagnosis.summary[:160]}; trying again with a single compile job ({attempt + 1}/{attempts})")
                else:
                    self.notify(f"Base system packages: {diagnosis.summary[:160]}; trying again ({attempt + 1}/{attempts})")
                self.sleep(5.0 * attempt)

    @staticmethod
    def memory_advice(diagnosis: EmergeDiagnosis, hopeless: bool) -> str:
        """The summary of a failed emerge; when one compile job was not enough either, it says so."""
        return diagnosis.summary.rstrip(". ") + f". {NOT_ENOUGH_MEMORY}" if hopeless else diagnosis.summary

    def one_job_is_no_help(self, manifest: InstallManifest, diagnosis: EmergeDiagnosis, one_job: bool) -> bool:
        """A build was killed for lack of memory, and the attempt already had one compile job (or had to have): another try would only fail the same way."""
        return diagnosis.kind == "memory" and (one_job or self.one_job_cannot_help(manifest, diagnosis))

    def emerge_keep_going(self, manifest: InstallManifest, atoms: Sequence[str], *, label: str, attempts: int = 3) -> KeepGoingResult:
        """Emerge atoms with --keep-going and retry what is still missing when a retry can help.

        Downloads that failed are retried; a build killed for lack of memory is retried with one
        compile job; a package that fails to build, or a set that cannot be resolved, is not (the
        same build would fail again, possibly after hours).
        """
        remaining = list(dict.fromkeys(atoms))
        result = KeepGoingResult(remaining, "", "unknown")
        one_job = False
        for attempt in range(1, attempts + 1):
            if not remaining:
                break
            try:
                self.emerge(manifest, ("--noreplace", "--keep-going", *remaining), one_job=one_job)
                return KeepGoingResult()
            except RunnerError as exc:
                diagnosis = self.explain(exc)
                remaining = [atom for atom in remaining if not self.is_installed(atom)]
                if not remaining:
                    self.notify(f"warning: {label}: some dependencies failed ({diagnosis.summary[:200]}) but everything requested is installed")
                    return KeepGoingResult()
                hopeless = self.one_job_is_no_help(manifest, diagnosis, one_job)
                result = KeepGoingResult(remaining, self.memory_advice(diagnosis, hopeless), diagnosis.kind)
                if diagnosis.kind in {"resolution", "build"} or attempt == attempts or hopeless:
                    return result
                if diagnosis.kind == "memory":
                    one_job = True
                    self.notify(f"{label}: {diagnosis.summary[:140]}; retrying with a single compile job ({attempt + 1}/{attempts})")
                else:
                    self.notify(f"{label}: {len(remaining)} package(s) failed ({diagnosis.summary[:160]}); retrying ({attempt + 1}/{attempts})")
                self.sleep(5.0 * attempt)
        return result

    def world_drift(self, manifest: InstallManifest) -> list[str]:
        """Installed packages that a USE change would rebuild; a drift between make.conf and the system.

        Informational: failures are ignored, and plain version upgrades (the tree may be newer than the
        stage3) are not drift.
        """
        try:
            output = self.emerge(manifest, ("--pretend", "--update", "--deep", "--newuse", "--usepkg=n", "--getbinpkg=n", "@world"), capture=True).stdout
        except RunnerError:
            return []
        rebuilds: list[str] = []
        for line in output.splitlines():
            match = re.match(r"\[ebuild\s+([A-Za-z ]*?)\s*\]\s+(\S+)", line)
            if match and "R" in match.group(1) and "U" not in match.group(1) and "N" not in match.group(1):
                rebuilds.append(match.group(2))
        return rebuilds

    def prepare_companions(self, manifest: InstallManifest) -> str:
        """Make the live image's companion applications installable; returns a problem description or ""."""
        try:
            self.configure_companion_overlay(manifest)
        except (ConfigurationError, OSError) as exc:
            return str(exc)
        return ""

    def install_desktop(self, manifest: InstallManifest, plan: object | None = None) -> DesktopResult:
        """Desktop environment first (must succeed), then every optional package on its own terms."""
        plan = self._plan(plan)
        atoms = self.desktop_atoms(manifest, plan)
        result = DesktopResult()
        problem = self.prepare_companions(manifest)
        if problem:
            result.degraded.append("The companion applications were skipped: " + problem)
            atoms.optional = [atom for atom in atoms.optional if atom not in COMPANION_ATOMS]
        if manifest.system.world_update:
            try:
                self.emerge(manifest, ("--update", "--deep", "--newuse", "--keep-going", "@world"))
            except RunnerError as exc:
                result.degraded.append("The @world update did not finish: " + self.explain(exc).summary[:200])
        # TLP goes first: KDE and GNOME need a power-profiles provider, and when TLP is not there yet they
        # pull in power-profiles-daemon, which TLP then blocks. With TLP installed first the desktop is satisfied by it.
        first = [(label, group) for label, group in atoms.groups if PREFERRED_FIRST in group]
        for label, group in first:
            outcome = self.emerge_keep_going(manifest, group, label=label, attempts=2)
            for atom in outcome.missing:
                result.failed.append(atom)
                result.degraded.append(f"{atom} ({label}) could not be installed: {outcome.reason}"[:300])
        atoms.groups = [item for item in atoms.groups if item not in first]
        if atoms.required:
            outcome = self.emerge_keep_going(manifest, atoms.required, label="Desktop")
            if outcome.missing:
                raise PackageInstallError("The desktop environment could not be installed completely (" + ", ".join(outcome.missing) + "): " + outcome.reason,
                                          failed=outcome.missing, resolution=outcome.kind == "resolution")
        if atoms.optional:
            outcome = self.emerge_keep_going(manifest, list(atoms.optional), label="Optional packages", attempts=2)
            # An unresolvable group installed nothing: try its packages one by one so a bad one cannot take
            # the others down. After build or download failures the missing ones are the broken ones.
            for atom in outcome.missing:
                single = self.emerge_keep_going(manifest, [atom], label=atom, attempts=1) if outcome.kind == "resolution" else outcome
                if single.missing:
                    result.failed.append(atom)
                    result.degraded.append(f"{atom} could not be installed: {single.reason or outcome.reason}"[:300])
        for label, group in atoms.groups:
            outcome = self.emerge_keep_going(manifest, group, label=label, attempts=2)
            for atom in outcome.missing:
                result.failed.append(atom)
                result.degraded.append(f"{atom} ({label}) could not be installed: {outcome.reason}"[:300])
        for atom in atoms.extras:
            outcome = self.emerge_keep_going(manifest, [atom], label=atom, attempts=1)
            if outcome.missing:
                result.failed.append(atom)
                result.degraded.append(f"{atom} could not be installed: {outcome.reason}"[:300])
        result.degraded.extend(problem[:300] for problem in self.restore_cycle_breaks(manifest))
        return result

    # ----- services ---------------------------------------------------------------------------------

    def service_specs(self, manifest: InstallManifest, plan: object | None = None, phase: str = "all") -> list[tuple[str, str, bool]]:
        """(service, runlevel, optional) triples; optional services are skipped when the package is missing."""
        system, options, network = manifest.system, manifest.options, manifest.network
        plan = self._plan(plan)
        base: list[tuple[str, str, bool]] = []
        desktop: list[tuple[str, str, bool]] = []
        if network.manager == "networkmanager":
            base.append(("NetworkManager", "default", False))
        elif network.manager == "dhcpcd":
            base.append(("dhcpcd", "default", False))
        elif network.manager == "systemd-networkd":
            base.extend((("systemd-networkd", "default", False), ("systemd-resolved", "default", False)))
        if options.ssh:
            base.append(("sshd", "default", False))
        if options.chrony:
            base.append(("chronyd", "default", False))
        if self.wants_cron(manifest):
            base.append(("cronie", "default", False))
        if options.logger and system.init == "openrc":
            base.append(("syslog-ng", "default", False))
        if network.manager == "netifrc":
            interface = network.interface
            if not re.fullmatch(r"[A-Za-z0-9_]+", interface):
                raise ConfigurationError("netifrc requires a valid network interface name")
            base.append((f"net.{interface}", "default", False))
        if system.init == "openrc" and (system.desktop != "minimal" or options.bluetooth or options.printing):
            # Without NetworkManager nothing else pulls the system bus into a runlevel.
            desktop.append(("dbus", "default", True))
        if system.init == "openrc" and system.desktop != "minimal":
            desktop.append(("elogind", "boot", True))
        if options.bluetooth:
            desktop.append(("bluetooth", "default", True))
        if options.printing:
            desktop.append(("cupsd" if system.init == "openrc" else "cups", "default", True))
        if system.console_font and system.init == "openrc":
            desktop.append(("consolefont", "boot", True))
        if system.desktop != "minimal":
            manager = {"kde": "sddm", "gnome": "gdm", "xfce": "lightdm"}[system.desktop]
            desktop.append(("display-manager" if system.init == "openrc" else manager, "default", True))
        for item in [*_plan_list(plan, "services"), *_plan_list(plan, "optional_services")]:
            name, runlevel = (item, "default") if isinstance(item, str) else (item[0], item[1])
            if (str(name), str(runlevel), True) not in desktop:
                desktop.append((str(name), str(runlevel), True))
        if phase == "base":
            return base
        if phase == "desktop":
            return desktop
        return [*base, *desktop]

    def service_installed(self, name: str, init: str) -> bool:
        if self.dry_run:
            return True
        if init == "openrc":
            return self.path(f"etc/init.d/{name}").exists()
        # A timer or a socket is named with its suffix (fstrim.timer); a plain name is a service.
        unit = name if name.endswith((".service", ".timer", ".socket", ".path", ".target")) else f"{name}.service"
        return any(self.path(f"{prefix}/{unit}").exists() for prefix in ("usr/lib/systemd/system", "lib/systemd/system", "etc/systemd/system"))

    def configure_services(self, manifest: InstallManifest, plan: object | None = None, phase: str = "all") -> list[str]:
        """Enable services; returns warnings for optional services whose package is not installed."""
        system = manifest.system
        warnings: list[str] = []
        if phase in {"all", "desktop"} and system.desktop != "minimal":
            manager = {"kde": "sddm", "gnome": "gdm", "xfce": "lightdm"}[system.desktop]
            if system.init == "openrc" and self.service_installed("display-manager", "openrc"):
                self.write("etc/conf.d/display-manager", f'DISPLAYMANAGER="{manager}"\n')
        for name, runlevel, optional in self.service_specs(manifest, plan, phase):
            if name.startswith("net.") and system.init == "openrc" and not self.dry_run:
                source = self.path("etc/init.d/net.lo")
                link = self.path(f"etc/init.d/{name}")
                if not source.is_file():
                    raise ConfigurationError("netifrc init script is missing")
                if not link.exists() and not link.is_symlink():
                    link.symlink_to("net.lo")
            if optional and not self.service_installed(name, system.init):
                warnings.append(f"Service {name} was not enabled because its package is not installed")
                self.notify("warning: " + warnings[-1])
                continue
            if system.init == "openrc":
                self.chroot(("rc-update", "add", name, runlevel))
            else:
                self.chroot(("systemctl", "enable", name))
        return warnings

    def configure_desktop_session(self, manifest: InstallManifest) -> None:
        """Files and steps that need the desktop packages: polkit agent autostart, dconf database."""
        system = manifest.system
        if system.desktop == "xfce":
            # polkit-gnome's own entry only starts under GNOME; PortageHub and settings tools need an agent.
            self.write("etc/xdg/autostart/gentoo-installer-polkit-agent.desktop",
                       "[Desktop Entry]\nType=Application\nName=PolicyKit Authentication Agent\n"
                       "Exec=/usr/libexec/polkit-gnome-authentication-agent-1\nOnlyShowIn=XFCE;\nNoDisplay=true\n")
        if system.desktop == "gnome" and not _keyboard_is_default(system):
            try:
                self.chroot(("dconf", "update"))
            except RunnerError:
                self.notify("warning: dconf update failed; the keyboard layout may need to be chosen again in GNOME settings")

    def run_plan_steps(self, plan: object | None = None) -> list[str]:
        """Post-install commands from the hardware plan; failures are reported, never fatal."""
        warnings: list[str] = []
        for step in _plan_list(self._plan(plan), "post_steps"):
            argv = tuple(str(part) for part in step)
            try:
                self.chroot(argv)
            except RunnerError as exc:
                warnings.append(f"{' '.join(argv)[:80]} failed: {str(exc)[-160:]}")
        return warnings

    def apply_hardware(self, plan: object | None = None) -> list[str]:
        """Write the plan's files and run its post-install steps; returns warnings for steps that failed."""
        self.write_plan_files(plan)
        return self.run_plan_steps(plan)

    def write_plan_files(self, plan: object | None = None) -> None:
        """Files the hardware plan wants on the target (written as root:root)."""
        for item in _plan_list(self._plan(plan), "files"):
            path, content, mode = item
            self.write(str(path), str(content), int(mode))

    # ----- LVM volumes and md arrays that existed before the installation ------------------------------

    def detect_stack(self, layout: Sequence[LayoutEntry], disks: Sequence[blockdev.BlockDevice] | None, resume_device: str | None = None) -> ExistingStack:
        """What the layout sits on that the installer did not create (see detect_existing_stack); a preview, or a missing tree, finds nothing."""
        if self.dry_run or not disks:
            return ExistingStack()
        return detect_existing_stack(self.runner, layout, disks, resume_device, self.notify)

    def prepare_stack_packages(self, stack: ExistingStack) -> None:
        """USE flags for the tools the existing volumes need; before they are emerged (lvm2[-lvm] has no lvm command, and the thin tools need thin)."""
        if stack.lvm:
            self.write("etc/portage/package.use/installer-storage", "sys-fs/lvm2 lvm" + (" thin" if stack.thin else "") + "\n")

    def finish_stack(self, manifest: InstallManifest, stack: ExistingStack) -> None:
        """After lvm2 and mdadm are installed: the array list for the installed system and, on OpenRC, the services that activate the other volumes at boot.

        The initramfs activates the root through its arguments. Home, swap or /boot on other volumes of the same group
        or array are activated by the lvm and mdraid services (systemd does it from udev rules and generators).
        """
        if stack.arrays:
            self.write("etc/mdadm.conf", "# Arrays this system was installed on, written by the Gentoo installer.\n" + "\n".join(stack.arrays) + "\n")
        if manifest.system.init != "openrc":
            return
        for name, needed in (("lvm", stack.lvm), ("mdraid", stack.md)):
            if not needed:
                continue
            if not self.service_installed(name, "openrc"):
                self.notify(f"warning: Service {name} was not enabled because its package is not installed")
                continue
            self.chroot(("rc-update", "add", name, "boot"))

    # ----- initramfs, boot loader -----------------------------------------------------------------

    def configure_initramfs(self, manifest: InstallManifest, root_device: str, luks_device: str | None = None,
                            plan: object | None = None, resume_device: str | None = None, stack: ExistingStack | None = None) -> None:
        """Persist the root discovery arguments before a kernel/initramfs is built.

        ``resume_device`` is the swap partition that hibernation writes to; the kernel looks for the image there.
        ``stack`` is what the layout sits on that already existed (LVM volumes, md arrays): dracut activates nothing
        without rd.lvm.lv or rd.md.uuid, whatever modules it has.
        """
        plan = self._plan(plan)
        root_uuid = "" if self.dry_run else self.runner.run(
            ("blkid", "-s", "UUID", "-o", "value", root_device)).stdout.strip()
        if not self.dry_run and not re.fullmatch(r"[0-9a-fA-F-]{36}", root_uuid):
            raise ConfigurationError("Root filesystem has no valid UUID for stable boot configuration")
        arguments = [f"root={'UUID=' + root_uuid if root_uuid else root_device}", "rw"]
        modules: list[str] = []
        if manifest.storage.luks:
            if not luks_device:
                raise ConfigurationError("LUKS backing device is required for initramfs configuration")
            uuid = "dry-run-luks-uuid" if self.dry_run else self.runner.run(
                ("blkid", "-s", "UUID", "-o", "value", luks_device)).stdout.strip()
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid) and not self.dry_run:
                raise ConfigurationError("LUKS backing device has no valid UUID")
            # Dracut uses rd.luks.uuid to select the backing device. The
            # rd.luks.name mapping only names a device selected that way.
            arguments.extend((f"rd.luks.uuid={uuid}", f"rd.luks.name={uuid}={guided_layout.luks_mapper_name(manifest.storage)}"))
            if _plan_scalar(plan, "luks_allow_discards", False):
                # TRIM through the encryption: the user accepted what that tells an attacker about free space.
                arguments.append(f"rd.luks.options={uuid}=discard" if manifest.system.init == "systemd" else f"rd.luks.allow-discards={uuid}")
            modules.append("crypt")
            if self.luks_prompt_uses_console_keymap(manifest):
                keymap = self.resolve_console_keymap(manifest)
                arguments.append(f"rd.vconsole.keymap={keymap}")
                # Non-systemd initramfs images take the keymap from the command line only; systemd ones read this file.
                self.write("etc/vconsole.conf", f"KEYMAP={keymap}\n")
        if manifest.system.console_font:
            # The font of the early console and the passphrase prompt. dracut sets 8x16 unless told otherwise:
            # a non-systemd initramfs takes the font from the command line, a systemd one from vconsole.conf.
            arguments.append(f"rd.vconsole.font={manifest.system.console_font}")
            self.set_shell_variable("etc/vconsole.conf", "FONT", manifest.system.console_font)
        if manifest.storage.lvm:
            arguments.append("rd.lvm.lv=gentoo/root")
            modules.append("lvm")
        if manifest.storage.raid_level:
            modules.append("mdraid")
        if stack is not None:
            arguments.extend(token for token in [*(f"rd.lvm.lv={lv}" for lv in stack.lvs), *(f"rd.md.uuid={uuid}" for uuid in stack.md_uuids)] if token not in arguments)
            modules.extend(name for name, wanted in (("lvm", stack.lvm), ("mdraid", stack.md)) if wanted and name not in modules)
        if resume_device:
            swap_uuid = "" if self.dry_run else self.runner.run(("blkid", "-s", "UUID", "-o", "value", resume_device)).stdout.strip()
            if not self.dry_run and not re.fullmatch(r"[0-9a-fA-F-]{36}", swap_uuid):
                raise ConfigurationError("The swap partition has no valid UUID, so the system could not resume from hibernation")
            arguments.append(f"resume={'UUID=' + swap_uuid if swap_uuid else resume_device}")
            modules.append("resume")  # included by hostonly="no" anyway; asked for so that it stays that way
        arguments.extend(token for token in _plan_list(plan, "kernel_cmdline") if CMDLINE_TOKEN_RE.fullmatch(str(token)))
        arguments.extend(manifest.system.kernel_cmdline_extra.split())
        cmdline = " ".join(arguments)
        self.write("etc/kernel/cmdline", cmdline + "\n")
        # The installer runs dracut inside a chroot before the target fstab
        # exists. A host-only image would describe the live installer's root.
        dracut_config = 'hostonly="no"\n'
        if modules:
            dracut_config += 'add_dracutmodules+=" ' + " ".join(modules) + ' "\n'
        self.write("etc/dracut.conf.d/gentoo-installer.conf", dracut_config)
        if manifest.system.bootloader == "grub" and len(arguments) > 2:
            self.set_grub_setting("GRUB_CMDLINE_LINUX", " ".join(arguments[2:]))

    def set_grub_setting(self, key: str, value: str) -> None:
        self.set_shell_variable("etc/default/grub", key, value)

    def _kernel_cmdline(self, root_device: str) -> str:
        path = self.path("etc/kernel/cmdline")
        return path.read_text(encoding="utf-8").strip() if not self.dry_run and path.is_file() else f"root={root_device} rw"

    def companion_archive(self, prefix: str) -> Path:
        """The one hwmonux-*.tar.zst / portagehub-*.tar.zst in the live image's companion bundle."""
        matches = sorted((self.companion_bundle / "distfiles").glob(f"{prefix}-*.tar.zst"))
        if len(matches) != 1:
            found = ", ".join(path.name for path in matches) or "none"
            raise ConfigurationError(f"Expected exactly one {prefix} source archive in {self.companion_bundle / 'distfiles'}, found: {found}")
        return matches[0]

    def configure_companion_overlay(self, manifest: InstallManifest) -> None:
        if not (manifest.options.install_hwmonux or manifest.options.install_portagehub):
            return
        if self.dry_run:
            return
        overlay = self.companion_bundle / "overlay"
        if not (overlay / "profiles/repo_name").is_file():
            raise ConfigurationError(f"Live companion overlay is missing: {overlay}")
        destination = self.path("var/db/repos/gentoo-live-installer")
        shutil.copytree(overlay, destination, dirs_exist_ok=True, symlinks=False)
        for prefix, enabled in (("hwmonux", manifest.options.install_hwmonux), ("portagehub", manifest.options.install_portagehub)):
            if enabled:
                source = self.companion_archive(prefix)
                cache = self.path("var/cache/distfiles")
                cache.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, cache / source.name)
        if manifest.options.install_hwmonux:
            # liquidctl is available for amd64 in the pinned Gentoo tree, but
            # carries only the testing keyword; HWMonux uses its USB sensors.
            self.write("etc/portage/package.accept_keywords/gentoo-installer-companions",
                       "app-misc/liquidctl ~amd64\ndev-python/hidapi ~amd64\n")
        self.write("etc/portage/repos.conf/gentoo-live-installer.conf",
                   "[gentoo-live-installer]\nlocation = /var/db/repos/gentoo-live-installer\nmasters = gentoo\nauto-sync = no\n")

    @staticmethod
    def efi_mountpoint(manifest: InstallManifest) -> str:
        if manifest.storage.mode == "manual" and any(part.mountpoint == "/boot/efi" for part in manifest.storage.partitions):
            return "/boot/efi"
        return "/efi"

    def removable_fallback_wanted(self, manifest: InstallManifest, plan: object | None = None, *, nvram_failed: bool = False) -> bool:
        """Should the removable-media boot path (EFI/BOOT/BOOTX64.EFI) be written?"""
        plan = self._plan(plan)
        choice = str(_plan_scalar(plan, "removable_fallback", "") or getattr(plan, "removable_fallback", "") or manifest.hardware.removable_fallback)
        if choice == "auto":
            # An ESP the installer just created has nothing to protect; a shared one keeps other systems' files.
            choice = "always" if manifest.storage.mode == "erase" or manifest.hardware.target == "portable" else "if-missing"
        if nvram_failed and choice == "never":
            self.notify("warning: the firmware did not accept a boot entry, so the fallback boot path is written regardless of the 'never' setting")
            return True
        if choice == "never":
            return False
        if choice == "if-missing" and not nvram_failed:
            efi = self.efi_mountpoint(manifest).lstrip("/")
            return self.dry_run or not self.path(f"{efi}/EFI/BOOT/{GRUB_EFI_FILES[grub_efi_target(plan)][1]}").exists()
        return True

    def _grub_install_efi(self, manifest: InstallManifest, plan: object | None, efi_mountpoint: str) -> bool:
        """Install GRUB for UEFI with a firmware entry and the removable fallback; True when NVRAM was written."""
        base = ("grub-install", f"--target={grub_efi_target(plan)}", f"--efi-directory={efi_mountpoint}")
        nvram = True
        try:
            self.chroot((*base, "--bootloader-id=Gentoo"))
        except RunnerError as exc:
            if not NVRAM_FAILURE.search(exc.output or str(exc)):
                raise
            self.notify("warning: the firmware refused the Gentoo boot entry; installing GRUB without it (the fallback path boots instead)")
            self.chroot((*base, "--bootloader-id=Gentoo", "--no-nvram"))
            nvram = False
        if self.removable_fallback_wanted(manifest, plan, nvram_failed=not nvram):
            self.chroot((*base, "--removable", "--no-nvram"))
        else:
            self.notify("The removable-media fallback boot path was not written (kept existing files)")
        return nvram

    def configure_bootloader(self, manifest: InstallManifest, root_device: str, efi_device: str | None = None,
                             plan: object | None = None) -> None:
        plan = self._plan(plan)
        system, storage = manifest.system, manifest.storage
        efi_mountpoint = self.efi_mountpoint(manifest)
        efi_prefix = efi_mountpoint.lstrip("/")
        cmdline = self._kernel_cmdline(root_device)
        if system.secure_boot and storage.firmware != "uefi":
            raise ConfigurationError("Secure Boot requires UEFI firmware")
        if system.secure_boot and system.bootloader == "grub":
            raise ConfigurationError("Secure Boot is not supported with GRUB; choose systemd-boot or the EFI stub with a signed UKI")
        if system.secure_boot and system.bootloader != "grub" and not system.uki:
            raise ConfigurationError("Secure Boot with systemd-boot or EFI stub requires a signed UKI")
        if system.uki and storage.firmware != "uefi":
            raise ConfigurationError("A Unified Kernel Image requires UEFI firmware")
        if system.secure_boot and not self.dry_run:
            problems = self.secure_boot_problems(manifest)
            if problems:
                raise ConfigurationError("; ".join(problems))
        if system.uki:
            self._build_uki(manifest, root_device)
        elif system.bootloader != "grub":
            self._copy_boot_files(manifest)
        fallback_written = False
        if system.bootloader == "grub":
            if storage.firmware == "uefi":
                self._grub_install_efi(manifest, plan, efi_mountpoint)
            else:
                self.chroot(("grub-install", "--target=i386-pc", storage.device))
            if system.uki:
                if not efi_device:
                    raise ConfigurationError("GRUB with UKI requires an EFI system partition")
                efi_uuid = "dry-run-efi-uuid" if self.dry_run else self.runner.run(
                    ("blkid", "-s", "UUID", "-o", "value", efi_device)).stdout.strip()
                if not self.dry_run and not re.fullmatch(r"[0-9a-fA-F-]+", efi_uuid):
                    raise ConfigurationError("EFI system partition has no valid UUID")
                self.write("etc/grub.d/40_custom", '#!/bin/sh\nexec tail -n +3 $0\n\n'
                           'menuentry "Gentoo Linux" {\n'
                           f'    search --no-floppy --fs-uuid --set=root {efi_uuid}\n'
                           '    chainloader /EFI/Linux/gentoo.efi\n}\n', 0o755)
            if system.dual_boot:
                # GRUB ships with os-prober disabled; other systems only appear in the menu when it is enabled.
                self.set_grub_setting("GRUB_DISABLE_OS_PROBER", "false")
            self.chroot(("grub-mkconfig", "-o", "/boot/grub/grub.cfg"))
        elif system.bootloader == "systemd-boot":
            try:
                self.chroot(("bootctl", f"--path={efi_mountpoint}", "install"))
            except RunnerError as exc:
                if not NVRAM_FAILURE.search(exc.output or str(exc)):
                    raise
                self.notify("warning: the firmware refused the boot entry; systemd-boot is installed on the fallback path only")
                self.chroot(("bootctl", f"--path={efi_mountpoint}", "--no-variables", "install"))
            initrd = "initrd /initramfs\n" if system.initramfs != "none" and not system.uki else ""
            linux = "/EFI/Linux/gentoo.efi" if system.uki else "/vmlinuz"
            options = "" if system.uki else f"options {cmdline}\n"
            self.write(f"{efi_prefix}/loader/loader.conf", "default gentoo.conf\ntimeout 3\n")
            self.write(f"{efi_prefix}/loader/entries/gentoo.conf", f"title Gentoo Linux\nlinux {linux}\n{initrd}{options}")
        else:
            if not root_device.startswith("/dev/"):
                raise ConfigurationError("EFI stub requires a stable root device")
            if system.uki and self.removable_fallback_wanted(manifest, plan):
                fallback_written = self._copy_uki_fallback(efi_prefix)
            elif not system.uki:
                self.notify("The EFI stub without a Unified Kernel Image has no fallback boot path; enable UKI to get one")
            disk, part = storage.device, "1"
            if efi_device and not self.dry_run:
                part = self.runner.run(("lsblk", "--noheadings", "--output", "PARTN", efi_device)).stdout.strip()
                parent = self.runner.run(("lsblk", "--noheadings", "--output", "PKNAME", efi_device)).stdout.strip()
                if not part.isdigit() or not re.fullmatch(r"[A-Za-z0-9._+-]+", parent):
                    raise ConfigurationError("Could not determine the EFI partition number and parent disk")
                disk = "/dev/" + parent
            loader = "\\EFI\\Linux\\gentoo.efi" if system.uki else "\\vmlinuz"
            command = ["efibootmgr", "--create", "--disk", disk, "--part", part, "--label", "Gentoo", "--loader", loader]
            if not system.uki:
                initrd = " initrd=\\initramfs" if system.initramfs != "none" else ""
                command.extend(("--unicode", f"{cmdline}{initrd}"))
            try:
                self.chroot(tuple(command))
            except RunnerError:
                if not fallback_written:
                    raise
                self.notify("warning: the firmware refused the boot entry; the system boots from the fallback path")
        if system.bootloader != "grub" or system.uki:
            self.install_esp_sync_hook(manifest, fallback_copy=fallback_written)
        if system.secure_boot:
            self._sign_boot_images(manifest, efi_mountpoint, sign_fallback_copy=fallback_written)

    def install_esp_sync_hook(self, manifest: InstallManifest, fallback_copy: bool = False) -> None:
        """Make kernel updates reach the EFI system partition (systemd-boot, EFI stub, UKI)."""
        system = manifest.system
        if system.secure_boot:
            self.notify("Secure Boot: kernel updates are not copied to the EFI system partition automatically, "
                        "because the new boot image would have to be signed again")
            return
        script = render_esp_sync_hook(self.efi_mountpoint(manifest), system.uki, fallback_copy)
        for hook in ESP_SYNC_HOOKS:
            self.write(hook, script, 0o755)

    def _copy_uki_fallback(self, efi_prefix: str) -> bool:
        if self.dry_run:
            return True
        source = self.path(f"{efi_prefix}/EFI/Linux/gentoo.efi")
        if not source.is_file():
            return False
        destination = self.path(f"{efi_prefix}/EFI/BOOT")
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination / "BOOTX64.EFI")
        return True

    # ----- Secure Boot ---------------------------------------------------------------------------------

    def secure_boot_problems(self, manifest: InstallManifest) -> list[str]:
        """Problems with the signing key and certificate on the live system (readable, matching pair)."""
        return check_secure_boot_material(self.runner, manifest.system)

    def _stage_signing_material(self, system: object) -> tuple[str, str]:
        """Copy key and certificate into a root-only directory of the live tmpfs, never onto the target disk."""
        self.secret_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.secret_dir.chmod(0o700)
        key_path = self.secret_dir / "signing.key"
        cert_path = self.secret_dir / "signing.crt"
        for source, destination, mode in ((Path(getattr(system, "secure_boot_key")), key_path, 0o600),
                                          (Path(getattr(system, "secure_boot_certificate")), cert_path, 0o644)):
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(source.read_bytes())
            destination.chmod(mode)
        return f"{self.secret_chroot_dir}/signing.key", f"{self.secret_chroot_dir}/signing.crt"

    def cleanup_secrets(self) -> None:
        """Remove staged signing material; plain filesystem calls so a cancelled install still cleans up."""
        for name in ("signing.key", "signing.crt"):
            try:
                os.unlink(self.secret_dir / name)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        try:
            os.rmdir(self.secret_dir)
        except OSError:
            pass

    def _sign_boot_images(self, manifest: InstallManifest, efi_mountpoint: str, *, sign_fallback_copy: bool = False) -> None:
        system = manifest.system
        images: list[str] = []
        if system.uki:
            images.append(f"{efi_mountpoint}/EFI/Linux/gentoo.efi")
        if system.bootloader == "systemd-boot":
            images.extend((f"{efi_mountpoint}/EFI/systemd/systemd-bootx64.efi", f"{efi_mountpoint}/EFI/BOOT/BOOTX64.EFI"))
        elif sign_fallback_copy:  # the copy of the UKI that was put on the removable-media path
            images.append(f"{efi_mountpoint}/EFI/BOOT/BOOTX64.EFI")
        if self.dry_run:
            key, certificate = f"{self.secret_chroot_dir}/signing.key", f"{self.secret_chroot_dir}/signing.crt"
        else:
            key, certificate = self._stage_signing_material(system)
        try:
            for image in dict.fromkeys(images):
                signed = image + ".signed"
                self.chroot(("sbsign", "--key", key, "--cert", certificate, "--output", signed, image))
                self.chroot(("sbverify", "--cert", certificate, signed))
                self.chroot(("mv", signed, image))
            if not self.dry_run:
                self._publish_certificate(manifest, efi_mountpoint)
        finally:
            self.cleanup_secrets()

    def _publish_certificate(self, manifest: InstallManifest, efi_mountpoint: str) -> None:
        """Put the certificate (DER) on the ESP so it can be enrolled from the firmware's setup menu."""
        destination = self.path(f"{efi_mountpoint.lstrip('/')}/EFI/enroll")
        destination.mkdir(parents=True, exist_ok=True)
        self.runner.run(("openssl", "x509", "-in", manifest.system.secure_boot_certificate, "-outform", "DER",
                         "-out", str(destination / "gentoo-secureboot.cer")))
        self.notify("Secure Boot: enroll EFI/enroll/gentoo-secureboot.cer from the firmware setup (append it to the db key store), "
                    "or with 'bootctl' while the firmware is in Setup Mode")

    # ----- boot images -----------------------------------------------------------------------------

    def _boot_images(self, manifest: InstallManifest) -> tuple[Path, Path | None]:
        boot = self.path("boot")
        # installkernel names the file vmlinuz-<version>; systemd's kernel-install (compat layout) kernel-<version>.
        kernels = sorted([*boot.glob("vmlinuz-*"), *boot.glob("kernel-*")], key=lambda path: path.stat().st_mtime, reverse=True)
        if not kernels:
            raise ConfigurationError("No installed kernel was found under /boot")
        initramfs = None
        if manifest.system.initramfs != "none":
            images = sorted([*boot.glob("initramfs-*.img"), *boot.glob("initrd-*")], key=lambda path: path.stat().st_mtime, reverse=True)
            if not images:
                raise ConfigurationError("No installed initramfs was found under /boot")
            initramfs = images[0]
        return kernels[0], initramfs

    def _build_uki(self, manifest: InstallManifest, root_device: str) -> None:
        efi_mountpoint = self.efi_mountpoint(manifest)
        efi_prefix = efi_mountpoint.lstrip("/")
        if self.dry_run:
            kernel, initramfs = "/boot/vmlinuz", "/boot/initramfs" if manifest.system.initramfs != "none" else None
        else:
            kernel_path, initramfs_path = self._boot_images(manifest)
            kernel, initramfs = "/boot/" + kernel_path.name, "/boot/" + initramfs_path.name if initramfs_path else None
            self.path(f"{efi_prefix}/EFI/Linux").mkdir(parents=True, exist_ok=True)
        command = ["ukify", "build", "--linux", kernel, "--cmdline", self._kernel_cmdline(root_device), "--output", f"{efi_mountpoint}/EFI/Linux/gentoo.efi"]
        if initramfs:
            command.extend(("--initrd", initramfs))
        self.chroot(tuple(command))

    def _copy_boot_files(self, manifest: InstallManifest) -> None:
        if self.dry_run:
            return
        kernel, initramfs = self._boot_images(manifest)
        efi = self.path(self.efi_mountpoint(manifest).lstrip("/"))
        efi.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(kernel, efi / "vmlinuz")
        if initramfs:
            shutil.copyfile(initramfs, efi / "initramfs")

    # ----- receipt and verification -------------------------------------------------------------------

    def build_receipt(self, manifest: InstallManifest, *, root_uuid: str = "", luks_uuid: str = "",
                      root_filesystem: str = "", boot_separate: bool = False, plan: object | None = None,
                      stack: ExistingStack | None = None) -> dict:
        system, storage = manifest.system, manifest.storage
        return {
            "version": 1,
            "installer": INSTALLER_VERSION,
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "firmware": storage.firmware,
            "bootloader": system.bootloader,
            "bootloader_id": "Gentoo",
            "esp": self.efi_mountpoint(manifest) if storage.firmware == "uefi" else "",
            "boot_separate": boot_separate,
            "init": system.init,
            "kernel": system.kernel,
            "initramfs": system.initramfs,
            "uki": system.uki,
            "secure_boot": system.secure_boot,
            "profile": manifest.effective_profile(),
            "root": {"fstype": root_filesystem or storage.filesystem, "uuid": root_uuid, "luks_uuid": luks_uuid,
                     "lvm": storage.lvm or (stack is not None and stack.root_lvm), "raid": storage.raid_level or (stack.root_md if stack is not None else "")},
            "cmdline_file": "/etc/kernel/cmdline",
            "hardware": [str(line) for line in _plan_list(self._plan(plan), "summary")],
        }

    def write_receipt(self, manifest: InstallManifest, root_device: str, layout: Sequence[LayoutEntry] = (),
                      luks_device: str | None = None, plan: object | None = None, stack: ExistingStack | None = None) -> dict:
        """Record what was installed (no secrets) for later repair tools: /etc/gentoo-live-installer/install.json."""
        def uuid_of(device: str | None) -> str:
            if self.dry_run or not device:
                return ""
            try:
                return self.runner.run(("blkid", "-s", "UUID", "-o", "value", device)).stdout.strip().splitlines()[0]
            except (RunnerError, IndexError):
                return ""

        root_entry = next((entry for entry in layout if entry.mountpoint == "/"), None)
        receipt = self.build_receipt(
            manifest, root_uuid=uuid_of(root_device), luks_uuid=uuid_of(luks_device) if manifest.storage.luks else "",
            root_filesystem=root_entry.filesystem if root_entry else "",
            boot_separate=any(entry.mountpoint == "/boot" for entry in layout), plan=plan, stack=stack)
        self.write(RECEIPT_PATH, json.dumps(receipt, indent=2) + "\n", 0o644)
        return receipt

    def verify_boot_setup(self, manifest: InstallManifest, root_device: str, *, root_uuid: str = "", plan: object | None = None,
                          stack: ExistingStack | None = None) -> list[str]:
        """Check the finished installation; returns a list of problems (empty when it looks bootable)."""
        plan = self._plan(plan)
        if self.dry_run:
            return []
        system, storage = manifest.system, manifest.storage
        problems: list[str] = []
        boot = self.path("boot")
        kernels = [p for pattern in ("vmlinuz-*", "kernel-*") for p in boot.glob(pattern) if p.is_file() and p.stat().st_size]
        initramfs = [p for pattern in ("initramfs*", "initrd*") for p in boot.glob(pattern) if p.is_file() and p.stat().st_size]
        if not kernels:
            problems.append("No kernel image was installed under /boot")
        if system.initramfs != "none" and not initramfs:
            problems.append("No initramfs was installed under /boot")
        fstab = self.read_text("etc/fstab")
        if root_uuid and f"UUID={root_uuid}" not in fstab:
            problems.append("/etc/fstab does not mention the root filesystem")
        elif not root_uuid and root_device not in fstab:
            problems.append("/etc/fstab does not mention the root filesystem")
        efi = self.efi_mountpoint(manifest).lstrip("/")
        if system.bootloader == "grub":
            config = self.read_text("boot/grub/grub.cfg")
            if not re.search(r"^\s*linux(?:efi)?\s+\S+", config, re.MULTILINE):
                problems.append("grub.cfg has no menu entry that loads a kernel")
            else:
                for wanted, present in (("rd.luks.uuid", storage.luks), ("rd.lvm.lv", storage.lvm or (stack is not None and stack.early_lvm)),
                                        ("rd.md.uuid", stack is not None and stack.early_md)):
                    if present and wanted not in config:
                        problems.append(f"grub.cfg does not pass {wanted}, so the root filesystem would not be found")
            if storage.firmware == "uefi":
                installed, removable = GRUB_EFI_FILES[grub_efi_target(plan)]
                if not self.path(f"{efi}/EFI/Gentoo/{installed}").is_file() and not self.path(f"{efi}/EFI/BOOT/{removable}").is_file():
                    problems.append("No GRUB EFI image was installed on the EFI system partition")
        elif system.bootloader == "systemd-boot":
            if not self.path(f"{efi}/loader/entries/gentoo.conf").is_file():
                problems.append("The systemd-boot entry was not written")
        if system.uki and not self.path(f"{efi}/EFI/Linux/gentoo.efi").is_file():
            problems.append("The Unified Kernel Image is missing from the EFI system partition")
        return problems

    def initramfs_modules(self) -> set[str] | None:
        """dracut modules inside the newest initramfs; None when they cannot be listed."""
        if self.dry_run:
            return None
        images = sorted(self.path("boot").glob("initramfs-*.img"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not images:
            return None
        try:
            output = self.chroot(("lsinitrd", "-m", "/boot/" + images[0].name), capture=True).stdout
        except RunnerError:
            return None
        modules = {line.strip() for line in output.splitlines() if re.fullmatch(r"\s*[a-z0-9][a-z0-9_-]*", line)}
        return modules or None
