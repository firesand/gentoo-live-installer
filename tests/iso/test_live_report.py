"""The live disc when the desktop does not start: a notice on the first console, and gentoo-live-report.

The first test on a real PC (RTX 4090): the desktop did not come up, the screen showed the first console, and gentoo-installer
typed there ended in Qt's abort about the xcb plugin. Nothing said what had happened or how to report it.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

from .conftest import ROOT, write_executable

HINT = ROOT / "iso/root_overlay/etc/profile.d/10-gentoo-rescue-hint.sh"
PACKAGE = ROOT / "packaging/gentoo-overlay/app-admin/gentoo-live-installer"
REPORT = PACKAGE / "files/gentoo-live-report"


def hint(tmp_path: Path, cmdline: str, xorg_running: bool) -> str:
    (tmp_path / "cmdline").write_text(cmdline + "\n")
    bin_dir = tmp_path / "bin"
    write_executable(bin_dir / "pgrep", "#!/bin/sh\nexit 0\n" if xorg_running else "#!/bin/sh\nexit 1\n")
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    env.update({"GENTOO_HINT_CMDLINE": str(tmp_path / "cmdline"), "PATH": f"{bin_dir}:{env['PATH']}"})
    return subprocess.run(["bash", "--norc", "-i", "-c", f". {HINT}"], capture_output=True, text=True, env=env, check=False).stdout


DESKTOP = "BOOT_IMAGE=/boot/gentoo nodhcp secureconsole root=live:CDLABEL=GENTOO_INSTALLER_AMD64 rd.live.dir=/ cdroot"


def test_the_desktop_entry_without_x_says_what_to_do(tmp_path) -> None:
    text = hint(tmp_path, DESKTOP, xorg_running=False)
    assert "No desktop is running yet" in text and "sudo rc-service display-manager restart" in text
    assert "Safe graphics" in text and "sudo gentoo-live-report" in text
    assert max(len(line) for line in text.splitlines()) <= 80, "the console is 80 columns wide"


def test_no_notice_when_x_runs_or_the_entry_has_no_desktop(tmp_path) -> None:
    assert "No desktop" not in hint(tmp_path, DESKTOP, xorg_running=True)
    assert "No desktop" not in hint(tmp_path, DESKTOP + " nox dokeymap", xorg_running=False)
    assert "No desktop" not in hint(tmp_path, DESKTOP + " nox gentoo.autostart=rescue", xorg_running=False)


def test_the_report_is_valid_bash_installed_by_the_package_and_reads_only() -> None:
    assert subprocess.run(["bash", "-n", str(REPORT)], capture_output=True, check=False).returncode == 0
    ebuild = next(PACKAGE.glob("*.ebuild")).read_text()
    assert 'dobin "${FILESDIR}/gentoo-live-report"' in ebuild
    text = REPORT.read_text()
    for section in ("kernel command line", "graphics cards", "display manager", "kernel messages about graphics", "X server"):
        assert section in text, section
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("rm ", "mkfs", " dd ", "passwd", "/etc/shadow", "serial", "wipefs", "> /dev/"):
        assert forbidden not in code, forbidden


def test_the_report_needs_root_and_says_so(tmp_path) -> None:
    bin_dir = tmp_path / "bin"
    write_executable(bin_dir / "id", "#!/bin/sh\necho 1000\n")
    result = subprocess.run(["bash", str(REPORT)], capture_output=True, text=True, env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}, check=False)
    assert result.returncode == 1 and "sudo gentoo-live-report" in result.stderr


def test_the_desktop_waits_for_udev_to_load_the_graphics_drivers() -> None:
    """One boot of RC1 on an RTX 4090 showed only the text console (X before nouveau and its firmware were ready)."""
    fsscript = (ROOT / "iso/fsscript-stage2.sh").read_text()
    line = next(line for line in fsscript.splitlines() if "/etc/conf.d/display-manager" in line and line.startswith("printf"))
    assert 'DISPLAYMANAGER="lightdm"' in line and 'rc_need="udev-settle"' in line
