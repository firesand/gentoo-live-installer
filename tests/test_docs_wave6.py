"""Wave 6 docs: links between the guides resolve, and the AI rescue guide keeps its security statements."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
LINK = re.compile(r"\]\(((?!https?:|mailto:)[^)#\s]+)(#[^)\s]*)?\)")


def anchors(path: Path) -> set[str]:
    found = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"#+\s+(.*)", line)
        if match:
            slug = re.sub(r"[^\w\- ]", "", match.group(1).lower()).strip().replace(" ", "-")
            found.add(slug)
    return found


def test_relative_links_and_anchors_resolve() -> None:
    problems = []
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for target, anchor in LINK.findall(text):
            path = (doc.parent / target).resolve()
            if not path.exists():
                problems.append(f"{doc.name}: {target} does not exist")
            elif anchor and path.suffix == ".md" and anchor[1:] not in anchors(path):
                problems.append(f"{doc.name}: {target}{anchor} has no such heading")
    assert not problems, "\n".join(problems)


def test_rescue_ai_guide_states_the_security_rules() -> None:
    text = (ROOT / "docs" / "RESCUE-AI.md").read_text(encoding="utf-8")
    for needle in ("no `sudo`", "one command", "phone can never approve", "read-only", "not on the stick", "have not been run on a real PC",
                   "sent to a cloud service", "never inside your system", "never as root"):
        assert needle in text, needle
    # the guide must not tell anyone to type the approval password into the agent's session
    assert "type it in the tmux" not in text.lower()


def test_user_guide_and_readme_point_to_the_rescue_guide() -> None:
    for name in ("README.md", "docs/USER-GUIDE.md"):
        assert "RESCUE-AI.md" in (ROOT / name).read_text(encoding="utf-8"), name
