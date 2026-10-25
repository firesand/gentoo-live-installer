"""The package data and entry points: what the iso owner installs must be what the tree holds."""

from __future__ import annotations

import glob
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "src" / "gentoo_rescue_ai"


def test_the_package_data_patterns_ship_every_file_of_the_tree() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    patterns = pyproject["tool"]["setuptools"]["package-data"]["gentoo_rescue_ai"]
    shipped = {Path(path) for pattern in patterns for path in glob.glob(str(PACKAGE / pattern), recursive=True) if Path(path).is_file() and not Path(path).is_symlink()}
    wanted = {p for p in (PACKAGE / "data").rglob("*") if p.is_file() and not p.is_symlink()}
    assert wanted <= shipped, sorted(wanted - shipped)


def test_every_entry_point_names_a_function_that_exists() -> None:
    import importlib

    scripts = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["scripts"]
    names = ["gentoo-rescue-agents", "gentoo-rescue-login", "gentoo-rescue-ai", "gentoo-rescue-do", "gentoo-rescue-approver", "gentoo-rescue-mount", "gentoo-rescue-share"]
    for name in names:
        module, _, function = scripts[name].partition(":")
        assert callable(getattr(importlib.import_module(module), function)), name
