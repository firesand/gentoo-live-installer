"""CORE-01: the product is Gentoo only; CORE-10 font: Fira Sans first."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from gentoo_installer import theme

SRC = Path(__file__).resolve().parents[2] / "src" / "gentoo_installer"
ROOT = SRC.parents[1]
PATTERN = re.compile(r"distributions?|distros?|other linux", re.IGNORECASE)

# (file relative to src/gentoo_installer, a phrase of the allowed string, why). The owner of a file removes its entry when the wording is fixed.
ALLOWED = [
    ("rescue_ui/messages.py", "this repair tool only supports Gentoo", "scope sentence of the repair tool"),
    ("rescue_ui/messages.py", "This tool repairs Gentoo only", "scope sentence of the repair tool"),
    ("rescue_ui/messages.py", "another Linux", "the repair tool names what it does not open"),
    ("textui/wizard.py", "most other Linux distributions use", "BOOT/ACCT: text installer systemd description"),
]


def string_literals(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def test_user_facing_strings_do_not_promise_other_distributions():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        for text in string_literals(path):
            text_to_check = re.sub(r"distribution (?:binary |source )?kernels?", "", text, flags=re.IGNORECASE)  # Gentoo's own term
            if not PATTERN.search(text_to_check) or text.lstrip().startswith(("#", "\"\"\"")):
                continue
            if "\n" in text and len(text) > 300 and relative.endswith("__init__.py"):
                continue
            if any(relative == name and phrase in text for name, phrase, _ in ALLOWED):
                continue
            offenders.append((relative, text[:100]))
    # Docstrings and module comments in the code are not user text: only literals that are not the first statement of a body count.
    offenders = [(f, t) for f, t in offenders if not _is_docstring(SRC / f, t)]
    assert offenders == []


def _is_docstring(path: Path, prefix: str) -> bool:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str) and first.value.value.startswith(prefix[:60]):
                return True
    return False


def test_welcome_names_gentoo_only():
    text = (SRC / "pages" / "welcome.py").read_text()
    assert "Fix a Gentoo system that will not boot" in text


def test_user_guide_states_the_scope():
    guide = (ROOT / "docs" / "USER-GUIDE.md").read_text()
    assert "Scope: Gentoo only" in guide


def test_ui_font_is_fira_sans_with_fallbacks():
    assert theme.FONT_FAMILY.startswith("'Fira Sans', 'Noto Sans', 'DejaVu Sans'")
