"""The finding type of the hardware planner (kept apart so the planner's helper modules can use it without importing the planner)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Issue:
    """A finding about the combination of manifest and hardware. ``code`` is in the ``hw.`` namespace."""

    code: str
    severity: str  # error | warning | info
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "message": self.message}
