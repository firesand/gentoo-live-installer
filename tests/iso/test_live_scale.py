"""The live desktop on a 4K screen: scale 2 for GTK and Qt (the first real PC of RC1 had a 4K monitor and tiny text)."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from .conftest import ROOT, write_executable

SCRIPT = ROOT / "iso/root_overlay/etc/X11/xinit/xinitrc.d/50-gentoo-live-scale"


def environment_after(tmp_path: Path, xrandr: str, cmdline: str = "BOOT_IMAGE=/boot/gentoo cdroot") -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    write_executable(bin_dir / "xrandr", "#!/bin/sh\ncat <<'X'\n" + xrandr + "X\n")
    (tmp_path / "cmdline").write_text(cmdline + "\n")
    env = {k: v for k, v in os.environ.items() if k not in ("GDK_SCALE", "QT_SCALE_FACTOR", "XCURSOR_SIZE")}
    env.update({"PATH": f"{bin_dir}:{env['PATH']}", "GENTOO_SCALE_CMDLINE": str(tmp_path / "cmdline")})
    out = subprocess.run(["sh", "-c", f'. "{SCRIPT}"; echo "GDK=${{GDK_SCALE:-}} QT=${{QT_SCALE_FACTOR:-}} CURSOR=${{XCURSOR_SIZE:-}}"'],
                         capture_output=True, text=True, env=env, check=True).stdout.split()
    return dict(item.split("=", 1) for item in out)


UHD = "Screen 0: minimum 8 x 8, current 3840 x 2160, maximum 32767 x 32767\nHDMI-1 connected primary 3840x2160+0+0 (normal left inverted right x axis y axis) 600mm x 340mm\n   3840x2160     60.00*+\nDP-1 disconnected (normal left inverted right x axis y axis)\n"
FHD = "Screen 0: minimum 8 x 8, current 1920 x 1080, maximum 32767 x 32767\nVirtual-1 connected primary 1920x1080+0+0 0mm x 0mm\n   1920x1080     60.00*+\n"
QHD = "eDP-1 connected primary 2560x1440+0+0 (normal) 344mm x 194mm\n"


def test_a_4k_screen_gets_scale_2_for_gtk_qt_and_the_pointer(tmp_path) -> None:
    assert environment_after(tmp_path, UHD) == {"GDK": "2", "QT": "2", "CURSOR": "48"}


@pytest.mark.parametrize("xrandr", [FHD, QHD, ""], ids=["1080p", "1440p", "no-xrandr-output"])
def test_ordinary_screens_and_an_unknown_screen_stay_at_scale_1(tmp_path, xrandr) -> None:
    assert environment_after(tmp_path, xrandr) == {"GDK": "", "QT": "", "CURSOR": ""}


def test_the_kernel_command_line_overrides_it(tmp_path) -> None:
    assert environment_after(tmp_path, UHD, "BOOT_IMAGE=/boot/gentoo cdroot gentoo.scale=1") == {"GDK": "", "QT": "", "CURSOR": ""}
    assert environment_after(tmp_path, FHD, "BOOT_IMAGE=/boot/gentoo cdroot gentoo.scale=2")["GDK"] == "2"
    assert environment_after(tmp_path, FHD, "gentoo.scale=9")["GDK"] == "", "only 1, 2 or 3"


def test_the_script_is_plain_sh_and_leaves_no_variables_behind(tmp_path) -> None:
    assert subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True, check=False).returncode == 0
    text = SCRIPT.read_text()
    assert "unset gentoo_live_scale gentoo_live_word gentoo_live_width" in text


def test_it_is_executable_because_lightdms_xsession_sources_only_executable_scripts() -> None:
    """/etc/lightdm/Xsession: 'if [ -x "${script}" ] ...; then . "${script}"': a plain file would be skipped without a word."""
    assert os.access(SCRIPT, os.X_OK)
    import json
    contract = json.loads((ROOT / "iso/image-contract.json").read_text())
    entry = next(item for item in contract["squashfs"]["required"] if item.get("path") == "etc/X11/xinit/xinitrc.d/50-gentoo-live-scale")
    assert entry.get("executable") is True
