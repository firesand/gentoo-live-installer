"""Identify the operating system of a mounted root from its own files (review R9: read, never execute)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .errors import RescueError
from .paths import RootFS

FAMILY_TOKENS: dict[str, tuple[str, ...]] = {
    "gentoo": ("gentoo", "funtoo", "calculate"),
    "arch": ("arch", "archlinux", "manjaro", "endeavouros", "cachyos", "artix", "garuda", "arcolinux"),
    "debian": ("debian", "ubuntu", "linuxmint", "pop", "kali", "raspbian", "devuan", "zorin", "elementary", "neon", "mx", "parrot", "deepin", "tuxedo"),
    "fedora": ("fedora", "rhel", "centos", "almalinux", "rocky", "ol", "amzn", "nobara", "scientific", "eurolinux", "oracle"),
    "suse": ("opensuse", "opensuse-leap", "opensuse-tumbleweed", "opensuse-microos", "opensuse-aeon", "opensuse-kalpa", "suse", "sles", "sled", "sle-micro", "leapmicro"),
}
TOKEN_TO_FAMILY = {token: family for family, tokens in FAMILY_TOKENS.items() for token in tokens}
OSTREE_VARIANTS = {"silverblue", "kinoite", "sericea", "onyx", "coreos", "iot", "bazzite", "cosmic-atomic"}
TRANSACTIONAL_IDS = {"opensuse-microos", "opensuse-aeon", "opensuse-kalpa", "leapmicro", "sle-micro"}

LEGACY_FILES = (
    ("/etc/gentoo-release", "gentoo", "Gentoo Linux"),
    ("/etc/arch-release", "arch", "Arch Linux"),
    ("/etc/fedora-release", "fedora", "Fedora"),
    ("/etc/redhat-release", "fedora", "Red Hat compatible Linux"),
    ("/etc/SuSE-release", "suse", "SUSE Linux"),
    ("/etc/debian_version", "debian", "Debian"),
    ("/etc/alpine-release", "alpine", "Alpine Linux"),
    ("/etc/slackware-version", "slackware", "Slackware"),
)
_QUOTE_RE = re.compile(r"""^(["'])(.*)\1$""")


@dataclass(frozen=True)
class OsInfo:
    id: str = ""
    id_like: tuple[str, ...] = ()
    name: str = ""
    pretty_name: str = ""
    version: str = ""
    version_id: str = ""
    variant_id: str = ""
    build_id: str = ""
    family: str = "generic"
    flavor: str = ""
    source: str = ""  # the file that identified the system

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["id_like"] = list(self.id_like)
        return data

    @property
    def title(self) -> str:
        return self.pretty_name or self.name or self.id or "Linux"


def parse_os_release(text: str) -> dict[str, str]:
    """KEY=VALUE lines with optional single or double quotes; no expansion, comments ignored."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        match = _QUOTE_RE.match(value)
        result[key] = match.group(2) if match else value
    return result


def family_for(os_id: str, id_like: tuple[str, ...] | list[str] = ()) -> str:
    """First of [ID] + ID_LIKE that is a known distribution wins; everything else is generic."""
    for token in (os_id.lower(), *[item.lower() for item in id_like]):
        family = TOKEN_TO_FAMILY.get(token)
        if family:
            return family
    return "generic"


def _from_fields(fields: dict[str, str], source: str) -> OsInfo:
    os_id = fields.get("ID", "").strip().lower()
    like = tuple(fields.get("ID_LIKE", "").lower().split())
    return OsInfo(
        id=os_id, id_like=like, name=fields.get("NAME", ""), pretty_name=fields.get("PRETTY_NAME", ""),
        version=fields.get("VERSION", ""), version_id=fields.get("VERSION_ID", ""), variant_id=fields.get("VARIANT_ID", ""),
        build_id=fields.get("BUILD_ID", ""), family=family_for(os_id, like), flavor=os_id, source=source,
    )


def read_os_info(fs: RootFS) -> OsInfo | None:
    """OsInfo of the system in ``fs``, or None when the directory does not look like a Linux root."""
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            fields = parse_os_release(fs.read_text(path, 64 * 1024))
        except (OSError, RescueError):
            continue
        if fields.get("ID") or fields.get("NAME"):
            return _from_fields(fields, path)
    for path, family, name in LEGACY_FILES:
        if not fs.is_file(path):
            continue
        text = fs.read_text_or(path, "", 4096).strip()
        os_id = family
        if family == "debian" and "ubuntu" in fs.read_text_or("/etc/lsb-release", "", 4096).lower():
            os_id, name = "ubuntu", "Ubuntu"
        pretty = text.splitlines()[0] if text and family != "debian" else name
        return OsInfo(id=os_id, name=name, pretty_name=pretty or name, family=family if family in FAMILY_TOKENS else "generic", flavor=os_id, source=path)
    if fs.is_file("/etc/fstab") and (fs.is_dir("/usr/bin") or fs.is_dir("/bin")):
        return OsInfo(id="linux", name="Linux", pretty_name="Unidentified Linux", family="generic", flavor="linux", source="/etc/fstab")
    return None


def detect_immutable(fs: RootFS, info: OsInfo) -> str:
    """'ostree', 'transactional' or 'nix' for systems the rescue tool must not write to; '' otherwise."""
    if info.id in TRANSACTIONAL_IDS or info.variant_id in {"microos"}:
        return "transactional"
    if info.variant_id in OSTREE_VARIANTS or fs.is_dir("/ostree/repo"):
        return "ostree"
    if info.id == "nixos" or fs.is_dir("/nix/store"):
        return "nix"
    return ""


def detect_init(fs: RootFS) -> str:
    """'systemd', 'openrc' or 'other' from the target's init links and directories."""
    for path in ("/sbin/init", "/usr/sbin/init", "/init"):
        try:
            target = fs.readlink(path)
        except (OSError, RescueError):
            continue
        base = target.rsplit("/", 1)[-1]
        if "systemd" in target:
            return "systemd"
        if base == "openrc-init":
            return "openrc"
        if base in ("runit-init", "s6-linux-init", "dinit", "busybox"):
            return "other"
    if fs.exists("/usr/lib/systemd/systemd") or fs.exists("/lib/systemd/systemd"):
        return "systemd"
    if fs.exists("/sbin/openrc") or fs.exists("/usr/sbin/openrc") or fs.exists("/usr/bin/openrc"):
        return "openrc"
    if fs.is_dir("/etc/init.d") and fs.exists("/etc/inittab"):
        return "other"
    return ""


@dataclass
class SystemFacts:
    """Everything identification reads from a root, in one place for scan results."""

    os: OsInfo
    init: str = ""
    arch: str = ""
    immutable: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"os": self.os.to_dict(), "init": self.init, "arch": self.arch, "immutable": self.immutable, "extra": self.extra}


def identify(fs: RootFS) -> SystemFacts | None:
    info = read_os_info(fs)
    if info is None:
        return None
    facts = SystemFacts(info, detect_init(fs), fs.exe_arch(), detect_immutable(fs, info))
    if info.family == "gentoo":
        facts.extra["profile"] = _gentoo_profile(fs)
    return facts


def _gentoo_profile(fs: RootFS) -> str:
    try:
        target = fs.readlink("/etc/portage/make.profile")
    except (OSError, RescueError):
        return ""
    marker = "/profiles/"
    return target.split(marker, 1)[1] if marker in target else target.rsplit("/", 1)[-1]
