"""PORT: software licences in plain words (Handbook: Optional, Configure the ACCEPT_LICENSE variable).

Gentoo only installs a program when its licence is accepted. Two simple choices cover most computers:

* Free software only          ACCEPT_LICENSE="-* @FREE"
* Free software and redistributable binaries (firmware, drivers)   ACCEPT_LICENSE="-* @FREE @BINARY-REDISTRIBUTABLE"

Advanced users can pick the licence groups one by one and accept a licence for a single package (package.license). A hardware
plan can accept the licence of one driver or firmware package for itself; it can never widen ACCEPT_LICENSE.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .manifest import Portage

FREE = "@FREE"
BINARY_REDISTRIBUTABLE = "@BINARY-REDISTRIBUTABLE"
EULA = "@EULA"
GPL_COMPATIBLE = "@GPL-COMPATIBLE"

GROUPS: tuple[tuple[str, str, str], ...] = (
    (FREE, "Free software", "Programs whose source code is open (the default for everything Gentoo installs)."),
    (BINARY_REDISTRIBUTABLE, "Redistributable binaries", "Firmware and drivers that are free to use but not open source."),
    (GPL_COMPATIBLE, "GPL compatible", "Licences that work together with the GNU General Public License."),
    (EULA, "Licence agreements", "Programs that ask you to accept an agreement (for example games launchers and some drivers)."),
)

FREE_ONLY = "-* @FREE"
RECOMMENDED = "-* @FREE @BINARY-REDISTRIBUTABLE"
# What an untouched settings file has: it counts as "free software only" (and as "not chosen" for license_groups).
DEFAULT = "@FREE"

LICENSE_HELP = "Some drivers and firmware are free to use but not open source; this decides whether Gentoo may install them for you."
LICENSE_TOKEN = re.compile(r"-?@?[A-Za-z0-9][A-Za-z0-9_.+-]*|-\*|\*")
PACKAGE_LICENSE_ATOM = re.compile(r"\*/\*|[A-Za-z0-9][A-Za-z0-9_+.-]*/[A-Za-z0-9][A-Za-z0-9_+.-]*(?::[A-Za-z0-9_.-]+)?")


def groups_value(groups: list[str] | tuple[str, ...]) -> str:
    """ACCEPT_LICENSE for a list of groups: only those are accepted."""
    return " ".join(["-*", *groups])


def effective_accept_license(portage: "Portage") -> str:
    """The ACCEPT_LICENSE that is written to make.conf.

    ``accept_license`` is the source of truth. A settings file that only lists ``license_groups`` (and leaves accept_license
    at its untouched default) gets those groups.
    """
    if portage.license_groups and portage.accept_license.strip() in {"", DEFAULT}:
        return groups_value(portage.license_groups)
    return portage.accept_license


def choice_of(portage: "Portage") -> str:
    """free | redistributable | custom: which of the two plain choices the settings amount to."""
    tokens = effective_accept_license(portage).split()
    if tokens in (["@FREE"], ["-*", "@FREE"]):
        return "free"
    if sorted(tokens) in (sorted(RECOMMENDED.split()), sorted(["@FREE", "@BINARY-REDISTRIBUTABLE"])):
        return "redistributable"
    return "custom"


def groups_of(accept_license: str) -> list[str]:
    """The @groups of an ACCEPT_LICENSE value that are switched on."""
    return [token for token in accept_license.split() if token.startswith("@")]


def license_problems(accept_license: str, package_license: dict[str, list[str]]) -> list[str]:
    errors = []
    if any(not LICENSE_TOKEN.fullmatch(token) for token in accept_license.split()):
        errors.append("ACCEPT_LICENSE may contain only licence names, @groups, -* and *")
    for atom, names in package_license.items():
        if not PACKAGE_LICENSE_ATOM.fullmatch(atom) or not names or any(not LICENSE_TOKEN.fullmatch(name) for name in names):
            errors.append(f"Invalid package licence entry: {atom}")
    return errors


def render_package_license(package_license: dict[str, list[str]]) -> str:
    return "".join(f"{atom} {' '.join(names)}\n" for atom, names in sorted(package_license.items()))
