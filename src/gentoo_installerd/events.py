from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import PurePath
from typing import Any, Callable

PROTOCOL_VERSION = 1

# Event types that a reconnecting client needs to rebuild its progress page.
# The attach ring buffer never drops them (see docs/protocol.md).
PROTECTED_EVENT_TYPES = frozenset({"stage", "error", "completed"})


class ErrorCode:
    """Machine-readable ``data.code`` values, namespaced ``<domain>.<code>``.

    Clients branch on these, never on the human text. ``rescue.*`` and ``hw.*``
    codes are defined by their own modules.
    """

    BAD_REQUEST = "proto.bad_request"
    UNSUPPORTED_ACTION = "proto.unsupported_action"
    INTERNAL = "proto.internal"
    TIMEOUT = "proto.timeout"
    BUSY = "install.busy"
    NOT_RUNNING = "install.not_running"
    FAILED = "install.failed"
    CANCELLED = "install.cancelled"
    INVALID_MANIFEST = "install.invalid_manifest"
    CLEANUP_FAILED = "install.cleanup_failed"


def _json_default(value: Any) -> Any:
    """Keep one odd value in ``data`` from killing a whole event stream."""
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


@dataclass(frozen=True)
class Event:
    type: str
    job_id: str = ""
    stage: str = ""
    message: str = ""
    progress: float | None = None
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    timestamp: float = field(default_factory=time.time)
    version: int = PROTOCOL_VERSION
    # Monotonic per job (install job or rescue operation); 0 means "no job
    # sequence" and is left out of the wire form.
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        seq = result.pop("seq")
        result = {key: value for key, value in result.items() if value not in ("", None, {})}
        if seq:
            result["seq"] = seq
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), default=_json_default)

    def replace(self, **changes: Any) -> Event:
        return dataclasses.replace(self, **changes)

    def with_data(self, **extra: Any) -> Event:
        """A copy with ``extra`` merged into ``data`` (existing keys win)."""
        merged = {**extra, **self.data}
        return dataclasses.replace(self, data=merged)


def error_event(code: str, message: str, *, job_id: str = "", stage: str = "", **data: Any) -> Event:
    """An ``error`` event with a namespaced machine-readable code."""
    return Event("error", job_id=job_id, stage=stage, message=message, error=message, data={"code": code, **data})


def completed_event(success: bool, message: str = "", *, job_id: str = "", **data: Any) -> Event:
    return Event("completed", job_id=job_id, message=message, data={"success": success, **data})


EventSink = Callable[[Event], None]
