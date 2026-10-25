#!/usr/bin/env python3
"""Real-hardware checklist for the installed system on the user's PC (hardware backlog H05). READ-ONLY.

Run it yourself, as the normal user, in the installed KDE Wayland session of the ASUS desktop (i9-14900K, RTX 4090)::

    python3 scripts/hardware-checklist.py                  # a report on the screen
    python3 scripts/hardware-checklist.py --markdown       # the same as a table for the release notes
    python3 scripts/hardware-checklist.py --json           # for a bug report

It only reads: files under /proc, /sys, /boot, /efi and /etc/gentoo-live-installer, and the output of a short list of query commands
(nvidia-smi, vulkaninfo, lsusb, loginctl, ss, ...; see READ_ONLY_COMMANDS, each in one exact form). It never uses sudo, a shell, or a write: nothing on the machine
changes, and it does not probe the SMBus, so it cannot upset RGB controllers or RAM. Checks that need root say so and are skipped, not
forced. What cannot be read-only (suspend and resume with a full VRAM, the fixture re-capture, a controller button press, an SSH login from
another PC) is printed as a MANUAL step with the exact commands; do those by hand and note the result next to this report.

Statuses: PASS, FAIL (the installer's promise is not kept: report it), RECORD (a fact for the release notes, no verdict), SKIP (this PC or user cannot
tell), MANUAL (do it yourself). The exit status is 1 when any check FAILs, else 0.

Nothing in this file has been run on the PC it is written for: the first run may show wrong expectations (a changed output format); a FAIL with
a plausible output in its detail is a reason to look at the check first.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, Sequence

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

PASS, FAIL, RECORD, SKIP, MANUAL = "PASS", "FAIL", "RECORD", "SKIP", "MANUAL"

# The only programs this script starts, and the only argument lists each of them gets. Every one of them reads and prints; a program whose
# arguments could make it write (efibootmgr -B, systemctl restart, rc-service sshd stop) is accepted only in the exact read-only form.
LOGINCTL_PROPERTIES = ("-p", "Type", "-p", "Desktop", "-p", "State", "-p", "Remote")
NAME = re.compile(r"[A-Za-z0-9@._-]+")


def exactly(*expected: str) -> Callable[[tuple[str, ...]], bool]:
    return lambda arguments: arguments == expected


READ_ONLY_COMMANDS: dict[str, Callable[[tuple[str, ...]], bool]] = {
    "nvidia-smi": exactly("--query-gpu=name,driver_version,memory.total,persistence_mode", "--format=csv,noheader"),
    "loginctl": lambda a: len(a) == 2 + len(LOGINCTL_PROPERTIES) and a[0] == "show-session" and NAME.fullmatch(a[1]) is not None and a[2:] == LOGINCTL_PROPERTIES,
    "vulkaninfo": exactly("--summary"),
    "lsusb": exactly(),
    "ss": exactly("-ltn"),
    "pgrep": lambda a: len(a) == 2 and a[0] == "-x" and NAME.fullmatch(a[1]) is not None,
    "systemctl": lambda a: len(a) == 2 and a[0] == "is-active" and NAME.fullmatch(a[1]) is not None,
    "rc-service": lambda a: len(a) == 2 and a[1] == "status" and NAME.fullmatch(a[0]) is not None,
    "efibootmgr": exactly(),
    "journalctl": exactly("-k", "-b", "-p", "err", "--no-pager", "-q"),
    "df": lambda a: len(a) == 3 and a[:2] == ("--output=avail", "-B1") and a[2].startswith("/"),
}


def is_read_only(argv: Sequence[str]) -> bool:
    rule = READ_ONLY_COMMANDS.get(argv[0]) if argv else None
    return rule is not None and bool(rule(tuple(argv[1:])))


READ_ONLY_PATH_PREFIXES = ("/proc/", "/sys/", "/boot/", "/efi/", "/etc/gentoo-live-installer/", "/usr/lib/udev/rules.d/", "/lib/udev/rules.d/", "/dev/input/by-id")
EXPECTED = {"gpu": "4090", "driver_branch": "595", "viper_usb": "1532:0091", "aura_usb": "0b05:18f3", "dualsense_usb": "054c:0ce6", "vram_gib": 24}


class Machine(Protocol):
    """What the checks may do. The real one is SystemMachine; the tests give a made-up one."""

    def run(self, argv: Sequence[str]) -> tuple[int, str]: ...
    def read(self, path: str) -> str | None: ...
    def glob(self, pattern: str) -> list[str]: ...
    def environ(self, name: str) -> str: ...


class SystemMachine:
    def run(self, argv: Sequence[str]) -> tuple[int, str]:
        if not is_read_only(argv):
            raise ValueError(f"{' '.join(argv)} is not a read-only command of this checklist")
        try:
            done = subprocess.run(list(argv), capture_output=True, text=True, timeout=20, env={**os.environ, "LC_ALL": "C"}, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, str(exc)
        return done.returncode, done.stdout + done.stderr

    def read(self, path: str) -> str | None:
        if not path.startswith(READ_ONLY_PATH_PREFIXES):
            raise ValueError(f"{path} is outside the places this checklist reads")
        try:
            return Path(path).read_text(errors="replace")
        except OSError:
            return None

    def glob(self, pattern: str) -> list[str]:
        if not pattern.startswith(READ_ONLY_PATH_PREFIXES):
            raise ValueError(f"{pattern} is outside the places this checklist reads")
        return sorted(glob.glob(pattern))

    def environ(self, name: str) -> str:
        return os.environ.get(name, "")


@dataclass
class Result:
    id: str
    title: str
    status: str
    detail: str = ""
    steps: list[str] = field(default_factory=list)


Check = Callable[[Machine], Result]


def first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ""


# --- NVIDIA and the session -----------------------------------------------------------------------------------------------

def check_nvidia_smi(m: Machine) -> Result:
    code, out = m.run(("nvidia-smi", "--query-gpu=name,driver_version,memory.total,persistence_mode", "--format=csv,noheader"))
    title = "nvidia-smi sees the GPU with the expected driver"
    if code == 127:
        return Result("nvidia-smi", title, FAIL, "nvidia-smi is not installed or did not start: the NVIDIA driver is not usable")
    if code != 0 or not out.strip():
        return Result("nvidia-smi", title, FAIL, f"nvidia-smi failed (exit {code}): {first_line(out)}")
    name, driver, memory = (field.strip() for field in (out.strip().splitlines()[0].split(",") + ["", "", ""])[:3])
    problems = []
    if EXPECTED["gpu"] not in name:
        problems.append(f"the GPU is {name!r}, expected an RTX {EXPECTED['gpu']}")
    if not driver.startswith(f"{EXPECTED['driver_branch']}."):
        problems.append(f"driver {driver} is not branch {EXPECTED['driver_branch']}")
    gib = int(re.search(r"(\d+)", memory).group(1)) / 1024 if re.search(r"(\d+)", memory) else 0
    if gib and gib < EXPECTED["vram_gib"] - 1:
        problems.append(f"{memory} of video memory is less than the {EXPECTED['vram_gib']} GiB of the card")
    return Result("nvidia-smi", title, FAIL if problems else PASS, "; ".join(problems) or f"{name}, driver {driver}, {memory}")


def check_modeset(m: Machine) -> Result:
    value = m.read("/sys/module/nvidia_drm/parameters/modeset")
    title = "nvidia_drm modeset=Y (needed for Wayland)"
    if value is None:
        return Result("nvidia-drm-modeset", title, FAIL, "/sys/module/nvidia_drm is missing: nvidia_drm is not loaded")
    return Result("nvidia-drm-modeset", title, PASS if value.strip() == "Y" else FAIL, f"modeset={value.strip()}")


def check_open_modules(m: Machine) -> Result:
    version = m.read("/proc/driver/nvidia/version") or ""
    title = "the kernel modules are the open ones (RTX 40 series, branch 595)"
    if not version:
        return Result("nvidia-open-modules", title, FAIL, "/proc/driver/nvidia/version is missing: the nvidia module is not loaded")
    return Result("nvidia-open-modules", title, PASS if "Open Kernel Module" in version else FAIL, first_line(version))


def check_wayland(m: Machine) -> Result:
    session = m.environ("XDG_SESSION_ID")
    title = "the login session is Wayland (loginctl Type=wayland)"
    if not session:
        return Result("wayland-session", title, SKIP, "XDG_SESSION_ID is not set: run this inside the graphical session")
    code, out = m.run(("loginctl", "show-session", session, *LOGINCTL_PROPERTIES))
    fields = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if code != 0 or "Type" not in fields:
        return Result("wayland-session", title, SKIP, f"loginctl gave no answer (exit {code})")
    detail = f"Type={fields['Type']} Desktop={fields.get('Desktop', '')} State={fields.get('State', '')}"
    return Result("wayland-session", title, PASS if fields["Type"] == "wayland" else FAIL, detail)


def check_vulkan(m: Machine) -> Result:
    code, out = m.run(("vulkaninfo", "--summary"))
    title = "Vulkan sees the NVIDIA GPU"
    if code == 127:
        return Result("vulkan", title, SKIP, "vulkaninfo is not installed (media-libs/vulkan-tools)")
    names = re.findall(r"deviceName\s*=\s*(.+)", out)
    if code != 0 or not names:
        return Result("vulkan", title, FAIL, f"vulkaninfo found no device (exit {code})")
    good = [name for name in names if "NVIDIA" in name and EXPECTED["gpu"] in name]
    return Result("vulkan", title, PASS if good else FAIL, "; ".join(names))


def check_xid(m: Machine) -> Result:
    code, out = m.run(("journalctl", "-k", "-b", "-p", "err", "--no-pager", "-q"))
    title = "no NVIDIA driver errors (NVRM, Xid) in this boot's kernel log"
    if code != 0:
        return Result("nvidia-kernel-log", title, SKIP, "the kernel log of this boot is not readable for this user (add the user to systemd-journal or read it as root)")
    bad = [line for line in out.splitlines() if "NVRM" in line or "Xid" in line]
    return Result("nvidia-kernel-log", title, FAIL if bad else PASS, bad[0] if bad else "no NVRM or Xid errors")


# --- suspend and resume (what can be read) ---------------------------------------------------------------------------------

def check_suspend_preparation(m: Machine) -> Result:
    params = m.read("/proc/driver/nvidia/params") or ""
    title = "suspend can keep the video memory (PreserveVideoMemoryAllocations) and there is room to save it"
    problems = []
    if not re.search(r"PreserveVideoMemoryAllocations:\s*1", params):
        problems.append("PreserveVideoMemoryAllocations is not 1: a resume with a busy GPU can come back with a black screen")
    code, out = m.run(("df", "--output=avail", "-B1", "/var/tmp"))
    try:
        free = int(out.strip().splitlines()[-1]) / 2**30
    except (ValueError, IndexError):
        free = -1
    if 0 <= free < EXPECTED["vram_gib"] + 2:
        problems.append(f"/var/tmp has {free:.0f} GiB free, less than the {EXPECTED['vram_gib']} GiB the driver may save there")
    return Result("suspend-preparation", title, FAIL if problems else PASS, "; ".join(problems) or f"preserve on, {free:.0f} GiB free in /var/tmp")


def manual_suspend(m: Machine) -> Result:
    return Result("suspend-resume", "suspend and resume with the video memory full", MANUAL, "needs you at the keyboard", [
        "Fill the GPU memory: start a game or `glmark2 --run-forever` and `nvidia-smi --query-gpu=memory.used --format=csv` until it shows about 20 GiB.",
        "Suspend from the KDE menu (or `systemctl suspend`), wait for the power light, wake the PC with the keyboard.",
        "Expected: the same session on screen within seconds, the game still running, `nvidia-smi` working, no `Xid` lines in `journalctl -k -b`.",
        "Note the result and the time it took. Do it twice.",
    ])


# --- the boot menu and Secure Boot ---------------------------------------------------------------------------------------------

def check_safe_graphics(m: Machine) -> Result:
    import vm_session

    title = "the boot menu lists the Safe graphics entry"
    found = {}
    grub = m.read("/boot/grub/grub.cfg")
    if grub:
        found = vm_session.find_safe_entry("grub", grub)
        loader = "GRUB"
    else:
        entries = "".join(f"== {path}\n{m.read(path) or ''}\n" for path in m.glob("/efi/loader/entries/*.conf") + m.glob("/boot/loader/entries/*.conf"))
        found, loader = (vm_session.find_safe_entry("systemd-boot", entries), "systemd-boot") if entries else ({}, "")
        if not found:
            code, out = m.run(("efibootmgr",))
            found, loader = (vm_session.find_safe_entry("efi-stub", out), "the EFI stub") if code == 0 else ({}, loader)
    if not loader:
        return Result("safe-graphics", title, SKIP, "no boot menu could be read (GRUB's grub.cfg or loader entries); /boot may be root-only")
    if not found:
        return Result("safe-graphics", title, FAIL, f"{loader} has no entry called Safe graphics")
    problems = vm_session.safe_entry_problems(found)
    return Result("safe-graphics", title, FAIL if problems else PASS, "; ".join(problems) or f"{loader}: {found['label']}")


def check_secure_boot(m: Machine) -> Result:
    title = "Secure Boot and kernel lockdown (recorded for the NVIDIA decision)"
    variables = m.glob("/sys/firmware/efi/efivars/SecureBoot-*") if m.read("/sys/firmware/efi/fw_platform_size") else []
    state = "unknown"
    if variables:
        data = m.read(variables[0])
        state = "enabled" if data and data[-1:] == "\x01" else "disabled"
    elif m.read("/sys/firmware/efi/fw_platform_size") is None:
        state = "not UEFI"
    lockdown = re.search(r"\[(\w+)\]", m.read("/sys/kernel/security/lockdown") or "")
    taint = (m.read("/proc/sys/kernel/tainted") or "?").strip()
    return Result("secure-boot", title, RECORD, f"SecureBoot {state}; lockdown {lockdown.group(1) if lockdown else 'unknown'}; kernel taint {taint} (4096 = out-of-tree module, normal with NVIDIA)")


# --- peripherals --------------------------------------------------------------------------------------------------------------

def lsusb_ids(m: Machine) -> set[str] | None:
    code, out = m.run(("lsusb",))
    if code != 0:
        return None
    return set(re.findall(r"ID ([0-9a-f]{4}:[0-9a-f]{4})", out))


def check_razer(m: Machine) -> Result:
    title = "OpenRazer sees the Razer Viper"
    ids = lsusb_ids(m)
    if ids is None:
        return Result("openrazer-viper", title, SKIP, "lsusb is not installed (sys-apps/usbutils)")
    if EXPECTED["viper_usb"] not in ids:
        return Result("openrazer-viper", title, SKIP, f"no USB device {EXPECTED['viper_usb']} is plugged in")
    bound = m.glob("/sys/bus/hid/drivers/razermouse/0003:1532:*")
    code, _ = m.run(("pgrep", "-x", "openrazer-daemon"))
    problems = []
    if not bound:
        problems.append("the razermouse kernel driver is not bound to the mouse (is openrazer-driver loaded?)")
    if code != 0:
        problems.append("openrazer-daemon is not running")
    return Result("openrazer-viper", title, FAIL if problems else PASS, "; ".join(problems) or "driver bound, daemon running")


def check_aura(m: Machine) -> Result:
    title = "the ASUS Aura controller is visible and OpenRGB's udev rules are installed"
    ids = lsusb_ids(m)
    if ids is None:
        return Result("openrgb-aura", title, SKIP, "lsusb is not installed")
    if EXPECTED["aura_usb"] not in ids:
        return Result("openrgb-aura", title, SKIP, f"no USB device {EXPECTED['aura_usb']} (Aura) is present")
    rules = m.glob("/usr/lib/udev/rules.d/*openrgb*.rules") + m.glob("/lib/udev/rules.d/*openrgb*.rules")
    return Result("openrgb-aura", title, PASS if rules else FAIL, "the controller is there; " + (f"rules: {rules[0]}" if rules else "the OpenRGB udev rules are missing: ordinary users cannot talk to the controller"),
                  ["OpenRGB itself is not started here: it scans the SMBus, which this read-only checklist leaves alone. Run `openrgb --list-devices` yourself."])


def check_dualsense(m: Machine) -> Result:
    title = "the DualSense is seen as a controller"
    ids = lsusb_ids(m)
    if ids is None:
        return Result("dualsense", title, SKIP, "lsusb is not installed")
    if EXPECTED["dualsense_usb"] not in ids:
        return Result("dualsense", title, SKIP, f"no USB device {EXPECTED['dualsense_usb']}: plug the controller in with its cable")
    nodes = [path for path in m.glob("/dev/input/by-id/*") if "Wireless_Controller" in path or "DualSense" in path]
    return Result("dualsense", title, PASS if nodes else FAIL, f"{len(nodes)} input nodes" if nodes else "the controller is on the USB bus but has no input node (hid-playstation not loaded?)",
                  ["Press a button in `jstest-gtk` or Steam's controller test to prove it end to end."])


# --- the installed system ---------------------------------------------------------------------------------------------------

def check_ssh(m: Machine) -> Result:
    title = "SSH is reachable (sshd runs and listens on port 22)"
    code, out = m.run(("ss", "-ltn"))
    listening = code == 0 and re.search(r":22\s", out) is not None
    active = m.run(("systemctl", "is-active", "sshd"))[1].strip() == "active" or "started" in m.run(("rc-service", "sshd", "status"))[1]
    problems = []
    if not listening:
        problems.append("nothing listens on port 22")
    if not active:
        problems.append("sshd is not running according to systemctl or rc-service")
    return Result("ssh", title, FAIL if problems else PASS, "; ".join(problems) or "sshd runs and listens", ["From another computer: `ssh USER@ADDRESS`, then `exit`."])


def check_receipt(m: Machine) -> Result:
    text = m.read("/etc/gentoo-live-installer/install.json")
    title = "the install receipt"
    if text is None:
        return Result("install-receipt", title, SKIP, "/etc/gentoo-live-installer/install.json not found: this system was not made by this installer")
    try:
        receipt = json.loads(text)
    except json.JSONDecodeError:
        return Result("install-receipt", title, FAIL, "the receipt is not valid JSON")
    return Result("install-receipt", title, RECORD, f"installer {receipt.get('installer', '?')}, {receipt.get('init', '?')}, {receipt.get('bootloader', '?')}, kernel {receipt.get('kernel', '?')}, "
                                                    f"hardware: {'; '.join(receipt.get('hardware', [])[:6])}")


def manual_recapture(m: Machine) -> Result:
    return Result("fixture-recapture", "re-capture the PC as a test fixture", MANUAL, "writes a new directory, so it is not run here", [
        "From the live ISO (or the installed system): `python3 scripts/capture-hardware-fixture.py build/fixtures/asus-desktop-rtx4090-v2`",
        "Read the result for anything personal (the tool redacts serial numbers and MAC addresses, but you decide), then add it to tests/hardware/fixtures.",
    ])


CHECKS: tuple[Check, ...] = (
    check_nvidia_smi, check_modeset, check_open_modules, check_wayland, check_vulkan, check_xid, check_suspend_preparation, manual_suspend,
    check_safe_graphics, check_secure_boot, check_razer, check_aura, check_dualsense, check_ssh, check_receipt, manual_recapture,
)


def run_checks(machine: Machine, checks: Sequence[Check] = CHECKS) -> list[Result]:
    results = []
    for check in checks:
        try:
            results.append(check(machine))
        except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
            results.append(Result(check.__name__, check.__name__, SKIP, f"the check itself failed: {type(exc).__name__}: {exc}"))
    return results


def format_text(results: Sequence[Result]) -> str:
    lines = []
    for result in results:
        lines.append(f"[{result.status:6}] {result.title}")
        if result.detail:
            lines.append(f"         {result.detail}")
        lines.extend(f"         - {step}" for step in result.steps)
    counts = {status: sum(r.status == status for r in results) for status in (PASS, FAIL, RECORD, SKIP, MANUAL)}
    lines.append("")
    lines.append("  ".join(f"{status} {count}" for status, count in counts.items()))
    return "\n".join(lines)


def format_markdown(results: Sequence[Result]) -> str:
    rows = ["| Check | Result | Detail |", "|---|---|---|"]
    rows += [f"| {r.title} | {r.status} | {r.detail.replace('|', '/')} |" for r in results]
    return "\n".join(rows)


def main(argv: Sequence[str] | None = None, machine: Machine | None = None, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description="Read-only real-hardware checklist for the installed system (docs/TESTING.md, H05)")
    parser.add_argument("--markdown", action="store_true", help="print a table for the release notes")
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    args = parser.parse_args(argv)
    results = run_checks(machine or SystemMachine())
    if args.json:
        out(json.dumps([r.__dict__ for r in results], indent=2))
    elif args.markdown:
        out(format_markdown(results))
    else:
        out(format_text(results))
    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
