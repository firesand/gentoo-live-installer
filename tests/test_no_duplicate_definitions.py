"""No module or class defines the same function twice: the later one silently wins.

Two branches that each add a method of the same name merge without a conflict, and Python keeps only the last one
(it happened: two ReviewPage.set_advanced methods, and the Review previews stopped opening in Advanced mode).
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [*sorted((ROOT / "src").rglob("*.py")), *sorted((ROOT / "scripts").glob("*.py")), *sorted((ROOT / "apps/hwmonux").rglob("*.py")),
           *sorted((ROOT / "tests").rglob("*.py"))]  # in tests a second definition silently drops the first test or helper


def _allowed(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A property setter/deleter and typing.overload variants share their name on purpose."""
    for decorator in node.decorator_list:
        text = ast.unparse(decorator)
        if text.endswith((".setter", ".deleter", ".getter")) or text in ("overload", "typing.overload"):
            return True
    return False


def duplicates(tree: ast.AST) -> list[str]:
    found: list[str] = []

    def scan(body: list[ast.stmt], where: str) -> None:
        seen: dict[str, int] = {}
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in seen and not _allowed(node):
                    found.append(f"{where}{node.name} (lines {seen[node.name]} and {node.lineno})")
                seen.setdefault(node.name, node.lineno)
            elif isinstance(node, ast.ClassDef):
                if node.name in seen:
                    found.append(f"{where}{node.name} (lines {seen[node.name]} and {node.lineno})")
                seen.setdefault(node.name, node.lineno)
                scan(node.body, f"{where}{node.name}.")

    scan(tree.body, "")  # type: ignore[attr-defined]
    return found


def test_the_scan_finds_a_duplicate_method() -> None:
    tree = ast.parse("class A:\n    def f(self): pass\n    def g(self): pass\n    def f(self): return 1\n")
    assert duplicates(tree) == ["A.f (lines 2 and 4)"]
    assert duplicates(ast.parse("class B:\n    @property\n    def x(self): pass\n    @x.setter\n    def x(self, v): pass\n")) == []


def test_no_source_file_defines_a_name_twice() -> None:
    problems = []
    for path in SOURCES:
        for item in duplicates(ast.parse(path.read_text(encoding="utf-8"), str(path))):
            problems.append(f"{path.relative_to(ROOT)}: {item}")
    assert not problems, "defined twice (the first one is dead code):\n" + "\n".join(problems)
