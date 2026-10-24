from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

PROTOCOL_VERSION = 1


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

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        return {key: value for key, value in result.items() if value not in ("", None, {})}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


EventSink = Callable[[Event], None]
