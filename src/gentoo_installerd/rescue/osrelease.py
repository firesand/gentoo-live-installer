"""Identify the operating system of a mounted root from its own files (review R9: read, never execute).

This repair tool supports Gentoo only. A root is told apart as Gentoo (family "gentoo") or as any other Linux (family "generic"): the
other systems are listed by the name they give themselves, with ``NOT_GENTOO`` as the reason they are not opened.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .errors import RescueError
from .paths import RootFS

NOT_GENTOO = "Not a Gentoo system - this repair tool only supports Gentoo"
GENTOO_TOKENS = ("gentoo", "funtoo", "calculate")  # ID= and ID_LIKE= values of the systems that are Gentoo for this tool (Portage, the same boot files)
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
    family: str = "generic"  # "gentoo", or "generic" for every other Linux
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
    """"gentoo" when the ID or one of the ID_LIKE values is a Gentoo one, otherwise "generic"."""
    tokens = (os_id.lower(), *[item.lower() for item in id_like])
    return "gentoo" if any(token in GENTOO_TOKENS for token in tokens) else "generic"


def _from_fields(fields: dict[str, str], source: str) -> OsInfo:
    os_id = fields.get("ID", "").strip().lower()
    like = tuple(fields.get("ID_LIKE", "").lower().split())
    return OsInfo(
        id=os_id, id_like=like, name=fields.get("NAME", ""), pretty_name=fields.get("PRETTY_NAME", ""),
        version=fields.get("VERSION", ""), version_id=fields.get("VERSION_ID", ""), variant_id=fields.get("VARIANT_ID", ""),
        build_id=fields.get("BUILD_ID", ""), family=family_for(os_id, like), source=source,
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
    if fs.is_file("/etc/gentoo-release"):  # an old system without os-release
        release = fs.read_text_or("/etc/gentoo-release", "", 4096).strip()
        return OsInfo(id="gentoo", name="Gentoo Linux", pretty_name=release.splitlines()[0] if release else "Gentoo Linux", family="gentoo", source="/etc/gentoo-release")
    if fs.is_file("/etc/fstab") and (fs.is_dir("/usr/bin") or fs.is_dir("/bin")):
        return OsInfo(id="linux", name="Linux", pretty_name="Unidentified Linux", family="generic", source="/etc/fstab")
    return None


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
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"os": self.os.to_dict(), "init": self.init, "arch": self.arch, "extra": self.extra}


def identify(fs: RootFS) -> SystemFacts | None:
    info = read_os_info(fs)
    if info is None:
        return None
    facts = SystemFacts(info, detect_init(fs), fs.exe_arch())
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
