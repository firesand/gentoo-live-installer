"""PORT: Gentoo news (Handbook: Reading news items). ``eselect news list`` names the items; unread ones carry an ``N``."""

from __future__ import annotations

import re
from dataclasses import dataclass

_ITEM = re.compile(r"^\s*\[(\d+)\]\s+(N\s+)?(\d{4}-\d{2}-\d{2})\s+(\S.*?)\s*$")


@dataclass(frozen=True)
class NewsItem:
    number: int
    date: str
    title: str
    unread: bool


def parse_news_list(text: str) -> list[NewsItem]:
    """The items of ``eselect news list`` output. Anything else ("No news is good news.", warnings) gives an empty list."""
    items = []
    for line in text.splitlines():
        match = _ITEM.match(line)
        if match:
            items.append(NewsItem(int(match.group(1)), match.group(3), match.group(4), bool(match.group(2))))
    return items


def news_summary(items: list[NewsItem], limit: int = 20) -> dict[str, object]:
    """What the progress event carries: ``{"unread": count, "titles": [newest unread titles]}`` (empty dict: nothing to say)."""
    unread = [item for item in items if item.unread]
    if not unread:
        return {}
    newest = sorted(unread, key=lambda item: (item.date, item.number), reverse=True)[:limit]
    return {"unread": len(unread), "titles": [f"{item.date}  {item.title}" for item in newest]}
