"""CORE-08 / B24: the "Your first steps in Gentoo" text.

One pure builder feeds the GUI finish page, the text installer and the FIRST-STEPS.txt that the
daemon writes into the new home directory, so the three can never disagree. Nothing here touches
the system.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .hardware_data import DIST_KERNELS

HANDBOOK_WORKING = "https://wiki.gentoo.org/wiki/Handbook:AMD64/Full/Working"
HANDBOOK_PORTAGE = "https://wiki.gentoo.org/wiki/Handbook:AMD64/Full/Portage"
HANDBOOK_FINALIZING = "https://wiki.gentoo.org/wiki/Handbook:AMD64/Installation/Finalizing"
GENTOO_WIKI = "https://wiki.gentoo.org"
GENTOO_FORUMS = "https://forums.gentoo.org"
GENTOO_IRC = "#gentoo on Libera.Chat"

FIRST_STEPS_FILE = "FIRST-STEPS.txt"
MAX_HARDWARE_LINES = 6  # B24: at most six applicable hardware lines

PRIVILEGE_PREFIX = {"sudo": "sudo ", "doas": "doas ", "run0": "run0 ", "su": ""}


@dataclass
class FirstSteps:
    """What the finish page and FIRST-STEPS.txt say. Empty values hide only their own line."""

    username: str = "gentoo"
    hostname: str = "gentoo"
    privilege: str = "sudo"
    ssh: bool = False
    minimal: bool = False  # no desktop: Wi-Fi is set up with nmtui
    boot_check: str = ""  # "Boot check passed: GRUB found kernel and initramfs"
    news_unread: int | None = None
    hardware: Sequence[str] = field(default_factory=tuple)  # HardwarePlan.first_steps
    bootloader: str = "grub"
    # What this install brought: a step never names a command the new system does not have.
    gentoolkit: bool = False  # eclean-dist
    eclean_kernel: bool = False
    networkmanager: bool = True  # nmtui


def privileged(command: str, privilege: str) -> str:
    return PRIVILEGE_PREFIX.get(privilege, "sudo ") + command


def with_tool(command: str, atom: str, installed: bool, privilege: str) -> str:
    """The command, after the emerge that brings its tool when the install did not."""
    return privileged(command, privilege) if installed else privileged(f"emerge --ask {atom}", privilege) + " && " + privileged(command, privilege)


def from_manifest(manifest, *, boot_check: str = "", news_unread: int | None = None, hardware: Iterable[str] = ()) -> FirstSteps:
    """FirstSteps for an InstallManifest (anything with .accounts, .options, .system and .network)."""
    return FirstSteps(
        username=manifest.accounts.username, hostname=manifest.accounts.hostname, privilege=manifest.accounts.privilege,
        ssh=bool(manifest.options.ssh), minimal=manifest.system.desktop == "minimal", boot_check=boot_check,
        news_unread=news_unread, hardware=tuple(hardware), bootloader=manifest.system.bootloader,
        gentoolkit=bool(manifest.options.gentoolkit),
        eclean_kernel=bool(manifest.options.eclean_kernel) and manifest.system.kernel in DIST_KERNELS,  # configure_boot.kernel_extra_atoms
        networkmanager=manifest.network.manager == "networkmanager")


def login_line(steps: FirstSteps) -> str:
    return f"ssh {steps.username}@{steps.hostname}"


def hardware_lines(lines: Iterable[object]) -> list[str]:
    """At most MAX_HARDWARE_LINES non-empty, distinct lines."""
    kept: list[str] = []
    for line in lines:
        text = str(line).strip()
        if text and text not in kept:
            kept.append(text)
    return kept[:MAX_HARDWARE_LINES]


def checks_lines(steps: FirstSteps) -> list[str]:
    lines = []
    if steps.boot_check:
        lines.append(steps.boot_check)
    if steps.news_unread is not None:
        count = steps.news_unread
        lines.append("There are no unread Gentoo news items." if count == 0 else
                     f"There {'is 1 unread Gentoo news item' if count == 1 else f'are {count} unread Gentoo news items'}: "
                     f"read {'it' if count == 1 else 'them'} with `eselect news read`.")
    return lines


def steps_lines(steps: FirstSteps) -> list[str]:
    """The numbered next steps (without numbers), each one a sentence that may contain a command."""
    p = steps.privilege
    lines = [
        f"Log in as {steps.username} (the password you chose). Administrator commands start with {PRIVILEGE_PREFIX.get(p, 'sudo ').strip() or 'su -c'}"
        + ("." if p != "su" else ", or become root with `su -`."),
        "Update the system: " + privileged("emerge --sync", p) + " && " + privileged("emerge --ask --update --deep --newuse @world", p),
        f"Read the Handbook part 'Working with Gentoo': {HANDBOOK_WORKING}",
        f"Your settings live in /etc/portage/make.conf and /etc/portage/package.use ({HANDBOOK_PORTAGE}).",
        "Free disk space from old downloads: " + with_tool("eclean-dist -d", "app-portage/gentoolkit", steps.gentoolkit, p),
        "After a kernel update, rebuild the out-of-tree drivers: " + privileged("emerge @module-rebuild", p)
        + ". Old kernels stay installed; remove them with " + with_tool("eclean-kernel", "app-admin/eclean-kernel", steps.eclean_kernel, p) + ".",
        "After installing another operating system, run " + privileged("grub-mkconfig -o /boot/grub/grub.cfg", p)
        + " so the boot menu lists it." if steps.bootloader == "grub" else
        "After installing another operating system, check that your boot manager lists it.",
    ]
    if steps.ssh:
        lines.append(f"From another computer on this network, log in with: {login_line(steps)}")
    if steps.minimal and steps.networkmanager:
        lines.append("To connect to Wi-Fi on this system without a desktop, run " + privileged("nmtui", p) + ".")
    return lines


def help_lines() -> list[str]:
    return [f"Get help: the wiki ({GENTOO_WIKI}), the forums ({GENTOO_FORUMS}) and {GENTOO_IRC}."]


def first_steps_text(steps: FirstSteps) -> str:
    """The whole FIRST-STEPS.txt."""
    out = ["Your first steps in Gentoo", "=" * 26, ""]
    checks = checks_lines(steps)
    if checks:
        out += [*checks, ""]
    out += [f"{number}. {line}" for number, line in enumerate(steps_lines(steps), 1)]
    extra = hardware_lines(steps.hardware)
    if extra:
        out += ["", "For this computer:"] + [f"- {line}" for line in extra]
    out += ["", *help_lines(), ""]
    return "\n".join(out)
