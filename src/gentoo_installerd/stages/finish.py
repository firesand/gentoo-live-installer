"""CORE-11 / CORE-08: the non-fatal checks after the install, and the first-steps file.

Everything here is best effort: a check that cannot run says nothing, a check that finds a problem
adds one plain sentence to ``installer.warnings`` (shown on the finish page), and nothing in this
module can fail the installation.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from gentoo_installer import first_steps

from ..runner import RunnerError

if TYPE_CHECKING:
    from ..install import Installer

LOG = logging.getLogger(__name__)

BOOTLOADER_NAMES = {"grub": "GRUB", "systemd-boot": "systemd-boot", "refind": "rEFInd", "limine": "Limine"}
FILE_CAPABILITY_TARGETS = ("/bin/ping", "/usr/bin/ping", "/usr/bin/newuidmap", "/bin/newuidmap")


def _out(installer: Installer, argv: tuple[str, ...]) -> str | None:
    """Output of a command in the target, None when it could not run."""
    try:
        return installer.configurer.chroot(argv, capture=True).stdout
    except RunnerError:
        return None
    except Exception:  # noqa: BLE001 - a check never stops the installation
        LOG.exception("Check %s failed", argv)
        return None


def machine_id_warning(installer: Installer) -> str:
    """OpenRC systems need /etc/machine-id filled in (D-Bus and desktops read it); systemd fills it at the first boot."""
    if installer.manifest.system.init == "systemd":
        return ""
    if installer.configurer.read_text("etc/machine-id").strip():
        return ""
    return ("The new system has no machine ID (/etc/machine-id is empty). Desktop programs may misbehave until you run "
            "'dbus-uuidgen --ensure=/etc/machine-id' as root.")


def capability_warning(installer: Installer) -> str:
    """The stage3 keeps file capabilities (or the setuid bit) on ping; a copy that lost them leaves ping working for root only."""
    for target in FILE_CAPABILITY_TARGETS:
        caps = _out(installer, ("getcap", target))
        if caps is None:
            continue  # not installed here, or getcap is missing
        if "cap_" in caps:
            return ""
        mode = _out(installer, ("stat", "-c", "%a", target))
        if mode is not None and mode.strip().isdigit() and int(mode.strip(), 8) & 0o4000:
            return ""
        return (f"{target} lost its file capabilities while the stage file was unpacked, so ordinary users may not be able to use it. "
                "Run 'emerge --oneshot net-misc/iputils' as root to restore them.")
    return ""


def microcode_planned(installer: Installer) -> bool:
    plan = installer.hardware_plan
    return bool(plan is not None and plan.scalars.get("microcode") not in (None, "", "none"))


def microcode_warning(installer: Installer) -> str:
    """Early microcode (CPU fixes loaded before the kernel) must be inside the initramfs when microcode was planned."""
    if not microcode_planned(installer) or installer.manifest.system.initramfs == "none":
        return ""
    images = sorted(installer.configurer.path("boot").glob("initramfs-*.img"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not images:
        return ""
    listing = _out(installer, ("lsinitrd", "/boot/" + images[0].name))
    if listing is None or not listing.strip():
        return ""
    if re.search(r"kernel/x86/microcode|(?:Genuine|Authentic)(?:Intel|AMD)", listing):
        return ""
    return ("The initramfs does not contain the early CPU microcode update, so the processor fixes load later in the boot. "
            "The computer still starts; run 'dracut --force' as root after the microcode package is installed to add it.")


def run_checks(installer: Installer) -> list[str]:
    """The three warnings of CORE-11 (empty when everything is fine); also appended to installer.warnings and logged."""
    if installer.dry_run:
        return []
    found = []
    for check in (machine_id_warning, capability_warning, microcode_warning):
        try:
            text = check(installer)
        except Exception:  # noqa: BLE001
            LOG.exception("Check %s failed", check.__name__)
            continue
        if text:
            found.append(text)
            if text not in installer.warnings:
                installer.warnings.append(text)
            installer.log(f"Warning: {text}")
    return found


def boot_check_text(installer: Installer) -> str:
    system = installer.manifest.system
    if system.bootloader == "none":
        return "Boot check passed: the kernel and initramfs are in /boot (no boot loader was installed)."
    name = BOOTLOADER_NAMES.get(system.bootloader, system.bootloader)
    return f"Boot check passed: {name} found the kernel" + (" and initramfs" if system.initramfs != "none" else "")


def unread_news(installer: Installer) -> int | None:
    output = _out(installer, ("eselect", "news", "count", "new"))
    if output is None:
        return None
    match = re.search(r"\d+", output)
    return int(match.group()) if match else None


def gather(installer: Installer) -> first_steps.FirstSteps:
    """Fill installer.boot_check / news_unread and return the text model."""
    plan = installer.hardware_plan
    installer.boot_check = boot_check_text(installer)
    installer.news_unread = None if installer.dry_run else unread_news(installer)
    return first_steps.from_manifest(installer.manifest, boot_check=installer.boot_check, news_unread=installer.news_unread,
                                     hardware=plan.first_steps if plan is not None else ())


def write_first_steps(installer: Installer, steps: first_steps.FirstSteps) -> str:
    """Write /home/<user>/FIRST-STEPS.txt (0644, owned by the user). Returns "" or a warning."""
    if installer.dry_run:
        return ""
    cfg, user = installer.configurer, installer.manifest.accounts.username
    relative = f"home/{user}/{first_steps.FIRST_STEPS_FILE}"
    try:
        if not cfg.path(f"home/{user}").is_dir():
            return ""
        cfg.write(relative, first_steps.first_steps_text(steps), 0o644)
        cfg.chroot(("chown", f"{user}:", f"/home/{user}/{first_steps.FIRST_STEPS_FILE}"))
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Could not write %s: %s", relative, exc)
        return f"Could not write {first_steps.FIRST_STEPS_FILE} into the home directory: {exc}"
    return ""


def finish(installer: Installer) -> None:
    """Called at the end of the validation stage."""
    run_checks(installer)
    steps = gather(installer)
    problem = write_first_steps(installer, steps)
    if problem and problem not in installer.warnings:
        installer.warnings.append(problem)
