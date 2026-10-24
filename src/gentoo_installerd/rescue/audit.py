"""Audit log and secret scrubbing.

Every command the engine runs, every plan, confirmation, file edit and refusal is appended to a
JSON-lines file (mode 0600). Secrets never reach it: request fields named like a secret are
masked, command lines of secret steps are replaced, and stdin is only ever recorded as
``"<secret>"``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from gentoo_installerd.runner import redact_text

SECRET_FIELDS = frozenset({
    "passphrase", "password", "secret", "secrets", "token", "luks_passphrase", "root_password", "new_password", "key", "keyfile_content",
})
REDACTED = "<redacted>"
MAX_OUTPUT_TAIL = 2048


def scrub(value: Any, extra: Iterable[str] = ()) -> Any:
    """Copy of a request/record structure with secret values masked (recursively)."""
    names = SECRET_FIELDS | set(extra)
    if isinstance(value, Mapping):
        return {key: (REDACTED if str(key).lower() in names and item not in (None, "", False) else scrub(item, extra)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(item, extra) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def tail(text: str, limit: int = MAX_OUTPUT_TAIL) -> str:
    cleaned = redact_text(text)
    return cleaned if len(cleaned) <= limit else cleaned[-limit:]


class AuditLog:
    """Append-only JSON-lines log for one session (or for the daemon when no session exists)."""

    def __init__(self, path: Path | str | None, sid: str = "", *, clock: Callable[[], float] = time.time, keep: int = 5000) -> None:
        self.path = Path(path) if path else None
        self.sid = sid
        self.clock = clock
        self._records: deque[dict[str, Any]] = deque(maxlen=keep)
        self._seq = 0
        self._fd = -1
        self._lock = threading.Lock()

    def _open(self) -> None:
        if self.path is None or self._fd >= 0:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        except OSError:
            self.path = None  # a full or read-only audit directory must never stop a repair

    def record(self, kind: str, *, sync: bool = False, **fields: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            entry: dict[str, Any] = {"ts": round(self.clock(), 3), "sid": self.sid, "seq": self._seq, "kind": kind}
            entry.update(scrub(fields))
            self._records.append(entry)
            self._open()
            if self._fd >= 0:
                try:
                    os.write(self._fd, (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode())
                    if sync:
                        os.fsync(self._fd)
                except OSError:
                    pass
            return entry

    @property
    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._records)

    def text(self, fmt: str = "jsonl", last: int = 0) -> str:
        records = self.records[-last:] if last > 0 else self.records
        if fmt == "jsonl":
            return "".join(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in records)
        lines = []
        for item in records:
            stamp = time.strftime("%H:%M:%S", time.gmtime(item.get("ts", 0)))
            detail = item.get("display") or " ".join(str(part) for part in item.get("argv", [])) or item.get("message", "")
            lines.append(f"{stamp} {item.get('kind', ''):<9} {detail}".rstrip())
        return "\n".join(lines) + ("\n" if lines else "")

    def close(self) -> None:
        with self._lock:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1


class NullAudit(AuditLog):
    def __init__(self) -> None:
        super().__init__(None)
