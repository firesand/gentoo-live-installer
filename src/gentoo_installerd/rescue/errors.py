"""Error types shared by every rescue module.

``RescueError`` subclasses ``RuntimeError`` so the daemon's baseline handler
(which only catches ValueError/RuntimeError) still turns one that escapes into
an error event. Codes are namespaced ``rescue.<name>`` as docs/protocol.md
requires; clients branch on ``code``, never on the message.
"""

from __future__ import annotations

from typing import Any

CODE_PREFIX = "rescue."

# Every code the engine can emit (docs/protocol.md namespaces them under "rescue.").
CODES = frozenset({
    "bad_request", "not_found", "ambiguous", "busy", "busy_install", "live_medium", "mounted_elsewhere",
    "unsupported_fs", "unsupported_system", "bad_passphrase", "no_memory", "not_luks", "already_open",
    "duplicate_vg", "duplicate_fsid", "degraded", "needs_write", "needs_network", "needs_uefi",
    "needs_unmounted", "precondition_failed", "unsupported_action", "plan_stale", "plan_expired",
    "plan_mismatch", "needs_confirmation", "safety_violation", "command_failed", "cancelled", "timeout",
    "internal", "disabled", "symlink_loop", "path_escape", "not_regular", "file_changed", "tool_missing",
    "chroot_broken", "read_only", "bad_state",
})


class RescueError(RuntimeError):
    """A failure with a machine-readable code and optional details."""

    def __init__(self, code: str, message: str = "", **details: Any) -> None:
        short = code.removeprefix(CODE_PREFIX)
        self.short = short
        self.code = CODE_PREFIX + short
        self.message = message or short.replace("_", " ")
        self.details: dict[str, Any] = details
        super().__init__(self.message)

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"code": self.code}
        if self.details:
            data["details"] = self.details
        return data


class SafetyViolation(RescueError):
    """A command or argument the safety policy refuses to run."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("safety_violation", message, **details)


class NeedsConfirmation(RescueError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("needs_confirmation", message, **details)


class PlanStale(RescueError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("plan_stale", message, **details)


class Unsupported(RescueError):
    """The adapter cannot do this here; ``reason`` is shown to the user."""

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__("unsupported_action", reason, reason=reason, **details)


class PreconditionFailed(RescueError):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("precondition_failed", "; ".join(reasons), reasons=list(reasons))
