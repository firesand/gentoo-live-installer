"""Every shell and Python file of the ISO build parses; shellcheck runs when it is installed."""
from __future__ import annotations

import py_compile
import shutil
import subprocess

import pytest

from .conftest import ROOT

SHELL_FILES = [
    "scripts/build-iso.sh",
    "iso/builder-entrypoint.sh",
    "iso/fsscript-stage2.sh",
    "iso/verify-snapshot.sh",
    "iso/lib/naming.sh",
    "iso/lib/overlay.sh",
    "iso/lib/run.sh",
    "iso/root_overlay/etc/acpi/actions/powerbtn.sh",
    "iso/root_overlay/etc/profile.d/10-gentoo-rescue-hint.sh",
    "packaging/gentoo-overlay/app-admin/gentoo-live-installer/files/gentoo-installerd.initd",
]
PYTHON_FILES = [
    "scripts/verify-iso.py",
    "iso/catalyst-no-loop.py",
    "iso/make_conf_keys.py",
    "iso/render-config.py",
    "iso/stage_reuse.py",
    "iso/write-manifest.py",
]


@pytest.mark.parametrize("relative", SHELL_FILES)
def test_shell_syntax(relative: str) -> None:
    path = ROOT / relative
    first_line = path.read_text(encoding="utf-8").splitlines()[0]
    # The init script is OpenRC's dialect (#!/sbin/openrc-run) but plain sh syntax.
    shell = "sh" if "openrc-run" in first_line or first_line.startswith("#!/bin/sh") else "bash"
    result = subprocess.run([shell, "-n", str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("relative", PYTHON_FILES)
def test_python_compiles(relative: str, tmp_path) -> None:
    py_compile.compile(str(ROOT / relative), cfile=str(tmp_path / "out.pyc"), doraise=True)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
@pytest.mark.parametrize("relative", [f for f in SHELL_FILES if not f.endswith(".initd")])
def test_shellcheck(relative: str) -> None:
    result = subprocess.run(
        ["shellcheck", "--severity=warning", "--shell=bash", str(ROOT / relative)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout


def test_scripts_are_executable() -> None:
    for relative in (
        "scripts/build-iso.sh", "scripts/verify-iso.py", "iso/verify-snapshot.sh", "iso/render-config.py",
        "iso/stage_reuse.py", "iso/write-manifest.py", "iso/root_overlay/etc/acpi/actions/powerbtn.sh",
        "iso/root_overlay/etc/init.d/xdm",
    ):
        assert (ROOT / relative).stat().st_mode & 0o111, f"{relative} must be executable"
