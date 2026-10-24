"""PORT: the USE flag browser's data (Handbook: Optional, Configuring the USE variable).

A USE flag turns an optional feature of programs on or off, for example ``kde``, ``dvd`` or ``-gtk``. The browser lists the
global flags of the pinned tree (bundled in usedesc_data.py) with their descriptions; the flags of single packages come from
the tree's ``use.local.desc`` when the installer can read one. Each flag has three states: on (``flag``), off (``-flag``)
and default (not written: the profile decides).
"""

from __future__ import annotations

import re
from pathlib import Path

from .usedesc_data import GLOBAL_USE, VIDEO_CARDS

USE_HELP = "USE flags turn optional features of programs on or off, for example kde, dvd or -gtk."
FLAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+@.-]*")
STATES = ("default", "on", "off")
STAR_MESSAGE = (
    "“-*” is not accepted. It would switch off every default feature of every program, including the ones the system needs to "
    "start and to connect to the network, and the Handbook warns against it. Switch off single flags instead, for example -gtk.")


def parse_use_desc(text: str) -> dict[str, str]:
    """``flag - description`` lines of use.desc."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, separator, description = line.partition(" - ")
        if separator and FLAG.fullmatch(name.strip()):
            result[name.strip()] = description.strip()
    return result


def parse_use_local_desc(text: str) -> dict[str, list[tuple[str, str]]]:
    """``category/package:flag - description`` lines of use.local.desc, as {flag: [(atom, description), ...]}."""
    result: dict[str, list[tuple[str, str]]] = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        head, separator, description = line.partition(" - ")
        atom, colon, flag = head.partition(":")
        if separator and colon and "/" in atom and FLAG.fullmatch(flag.strip()):
            result.setdefault(flag.strip(), []).append((atom.strip(), description.strip()))
    return result


def local_flags_from_tree(tree: str | Path) -> dict[str, list[tuple[str, str]]]:
    """use.local.desc of a Portage tree ({} when the tree has none or cannot be read)."""
    try:
        return parse_use_local_desc((Path(tree) / "profiles/use.local.desc").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


def global_flags() -> dict[str, str]:
    return dict(GLOBAL_USE)


def search(query: str, flags: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Flags whose name or description contains every word of ``query``; names that start with the query come first."""
    table = GLOBAL_USE if flags is None else flags
    words = query.lower().split()
    found = [(name, description) for name, description in table.items()
             if all(word in name.lower() or word in description.lower() for word in words)]
    first = query.strip().lower()
    return sorted(found, key=lambda item: (not item[0].lower().startswith(first), item[0].lower()))


def use_problem(token: str) -> str:
    """Why this word cannot be a USE flag entry ("" when it can): -* gets the Handbook's reason."""
    word = token.strip()
    if word in {"-*", "*"} or word.startswith("-*"):
        return STAR_MESSAGE
    if not word:
        return "A USE flag cannot be empty."
    if not re.fullmatch(r"-?" + FLAG.pattern, word):
        return f"“{word}” is not a USE flag: flags contain only letters, numbers and + _ @ . -"
    return ""


def tokens_from_states(states: dict[str, str]) -> list[str]:
    """USE tokens for the flags the user decided: on -> ``flag``, off -> ``-flag``, default -> nothing. Sorted by name."""
    tokens = []
    for name in sorted(states):
        state = states[name]
        if state == "on":
            tokens.append(name)
        elif state == "off":
            tokens.append(f"-{name}")
        elif state != "default":
            raise ValueError(f"unknown state {state!r} for {name}")
    return tokens


def states_from_tokens(tokens: list[str]) -> dict[str, str]:
    """The reverse of tokens_from_states (the last mention of a flag wins, as in Portage)."""
    states: dict[str, str] = {}
    for token in tokens:
        if token.startswith("-"):
            states[token[1:]] = "off"
        elif token:
            states[token] = "on"
    return states


def merge_tokens(existing: list[str], states: dict[str, str]) -> list[str]:
    """Apply the browser's decisions to a token list: keeps the order and the entries the browser does not know."""
    decided = {name for name in states}
    kept = [token for token in existing if token.lstrip("-") not in decided]
    return [*kept, *tokens_from_states(states)]


# --- VIDEO_CARDS (Handbook: VIDEO_CARDS) -------------------------------------------------------------------------------


def video_cards() -> dict[str, str]:
    """The VIDEO_CARDS values that mean something on a PC, with a short description."""
    return dict(VIDEO_CARDS)


def video_cards_problem(text: str) -> str:
    """Why this VIDEO_CARDS value is wrong ("" when it is fine or empty). Several values are separated by spaces."""
    unknown = [word for word in text.split() if word != "auto" and word not in VIDEO_CARDS]
    if not unknown:
        return ""
    return (f"Unknown graphics driver {', '.join(unknown)}. Choose from: {', '.join(sorted(VIDEO_CARDS))}.")
