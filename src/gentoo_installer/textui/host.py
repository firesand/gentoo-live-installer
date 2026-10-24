"""The live system the console installer runs on.

Every program the console installer starts (``loadkeys``, ``nmtui``, ``reboot``) and every system
file it reads (time zones, locales, keymaps, the by-path disk links) goes through ``Host``, so the
tests can replace all of it and never touch the machine they run on. The installation itself is
done by the installer daemon, never from here.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
import zoneinfo
from pathlib import Path
from typing import Any, Sequence

from .. import blockdev, hardware, keymaps, system_info

SUPPORTED_LOCALES = Path("/usr/share/i18n/SUPPORTED")
COMMON_LOCALES = (
    "en_US.UTF-8", "en_GB.UTF-8", "de_DE.UTF-8", "es_ES.UTF-8", "fr_FR.UTF-8", "it_IT.UTF-8", "ja_JP.UTF-8", "nl_NL.UTF-8",
    "pl_PL.UTF-8", "pt_BR.UTF-8", "ru_RU.UTF-8", "tr_TR.UTF-8",
)
REBOOT_COMMANDS: tuple[tuple[str, ...], ...] = (("loginctl", "reboot"), ("systemctl", "reboot"), ("reboot",))


def parse_supported_locales(text: str) -> list[str]:
    """UTF-8 locales listed in glibc's SUPPORTED file, en_US.UTF-8 first."""
    found: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if line.startswith("#") or len(parts) != 2 or parts[1] != "UTF-8":
            continue
        name, _, modifier = parts[0].partition("@")
        found.add(name.split(".")[0] + ".UTF-8" + (f"@{modifier}" if modifier else ""))
    return sorted(found, key=lambda locale: (locale != "en_US.UTF-8", locale))


class Host:
    """Programs and files of the live system."""

    def __init__(self, *, supported_locales: Path = SUPPORTED_LOCALES) -> None:
        self.supported_locales = supported_locales

    # --- time ---------------------------------------------------------------------------

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> float:
        return time.time()

    # --- programs ------------------------------------------------------------------------

    @staticmethod
    def _run(argv: Sequence[str], timeout: float = 15.0) -> tuple[bool, str]:
        try:
            result = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
        except FileNotFoundError:
            return False, f"{argv[0]} is not installed on this system"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if result.returncode != 0:
            lines = (result.stderr or result.stdout or "").strip().splitlines()
            return False, lines[0] if lines else f"{argv[0]} stopped with status {result.returncode}"
        return True, ""

    @staticmethod
    def _as_root(argv: Sequence[str]) -> list[str]:
        return list(argv) if os.geteuid() == 0 else ["sudo", "-n", *argv]

    def loadkeys(self, keymap: str) -> tuple[bool, str]:
        """Switch the keyboard layout of the text console that is in front of the user (needs root)."""
        return self._run(self._as_root(("loadkeys", keymap)))

    def reboot(self) -> tuple[bool, str]:
        """Restart the computer. Returns (False, why) when no restart command worked."""
        reason = "no restart command is available"
        for command in REBOOT_COMMANDS:
            ok, reason = self._run(self._as_root(command), timeout=30.0)
            if ok:
                return True, ""
        return False, reason

    def run_interactive(self, argv: Sequence[str]) -> int:
        """Run a full-screen program (nmtui) on the user's terminal and return its exit status."""
        try:
            return subprocess.call(list(argv))
        except FileNotFoundError:
            return 127
        except OSError:
            return 126

    # --- system data -----------------------------------------------------------------------

    def home_dir(self) -> Path:
        return Path.home()

    def console_keymaps(self) -> set[str]:
        """Names of the installed console keymaps (empty when they cannot be listed)."""
        return keymaps.list_console_keymaps()

    def timezones(self) -> list[str]:
        try:
            return sorted(zoneinfo.available_timezones())
        except Exception:  # noqa: BLE001 - a missing zone database must not stop the installer
            return []

    def locales(self) -> list[str]:
        try:
            found = parse_supported_locales(self.supported_locales.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            found = []
        return found or list(COMMON_LOCALES)

    def by_path_names(self) -> dict[str, str]:
        """Real path of every whole disk -> name of its /dev/disk/by-path link."""
        return system_info.by_path_names()

    def local_inspection(self) -> dict[str, Any]:
        """A stand-in for the daemon's ``inspect`` reply, gathered without root.

        Used by --dry-run when the daemon cannot be asked. It has the same keys, but it cannot
        say whether the network works or which tools the daemon sees."""
        try:
            result = subprocess.run(list(blockdev.lsblk_argv()), capture_output=True, text=True, check=True, timeout=15)
            roots = blockdev.parse_lsblk(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            roots = []
        profile = hardware.probe().to_dict()
        return {
            "architecture": platform.machine(),
            "memory_bytes": int(profile["memory"]["total_kib"]) * 1024,
            "uefi": profile["firmware"]["mode"] == "uefi",
            "tools": {tool: shutil.which(tool) is not None for tool in system_info.REQUIRED_TOOLS},
            "disks": [node.to_dict() for node in roots],
            "live_medium": [disk["path"] for disk in profile["disks"] if disk.get("live_medium")],
            "hardware": profile,
            "warnings": profile["warnings"],
            "errors": [],
            "source": "local",
        }
