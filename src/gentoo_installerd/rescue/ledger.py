"""The ledger of everything a session (or the unlock pool) acquired, released in reverse order."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

KINDS = ("blockro", "luks", "vg", "md", "mount", "bind", "placeholder", "cmdline", "shell")


@dataclass
class LedgerEntry:
    id: int
    kind: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, **self.data}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LedgerEntry:
        data = {key: value for key, value in raw.items() if key not in {"id", "kind"}}
        return cls(int(raw["id"]), str(raw["kind"]), data)


class Ledger:
    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self.entries: list[LedgerEntry] = []
        self._next = 1
        self.on_change = on_change or (lambda: None)

    def add(self, kind: str, **data: Any) -> LedgerEntry:
        if kind not in KINDS:
            raise ValueError(f"Unknown ledger kind {kind}")
        entry = LedgerEntry(self._next, kind, data)
        self._next += 1
        self.entries.append(entry)
        self.on_change()
        return entry

    def remove(self, entry: LedgerEntry) -> None:
        if entry in self.entries:
            self.entries.remove(entry)
            self.on_change()

    def of_kind(self, kind: str) -> list[LedgerEntry]:
        return [entry for entry in self.entries if entry.kind == kind]

    def find(self, kind: str, **match: Any) -> LedgerEntry | None:
        for entry in self.entries:
            if entry.kind == kind and all(entry.data.get(key) == value for key, value in match.items()):
                return entry
        return None

    def __iter__(self) -> Iterator[LedgerEntry]:
        return iter(list(self.entries))

    def __len__(self) -> int:
        return len(self.entries)

    def to_state(self) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in self.entries]

    @classmethod
    def from_state(cls, raw: list[dict[str, Any]], on_change: Callable[[], None] | None = None) -> Ledger:
        ledger = cls(on_change)
        for item in raw:
            entry = LedgerEntry.from_dict(item)
            ledger.entries.append(entry)
            ledger._next = max(ledger._next, entry.id + 1)
        return ledger


def write_json_atomic(path: Path, document: Any) -> None:
    """tmp + rename with mode 0600 so a crash never leaves a half-written state file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(document, handle, sort_keys=True, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
