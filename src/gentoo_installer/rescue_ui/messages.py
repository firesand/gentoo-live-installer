"""Plain-English texts of the repair window (no Qt).

The window repairs Gentoo systems only. Many users are not native English speakers and are stressed because their computer does not start: short sentences,
no jargon without a few words of explanation, and the engine's own messages only where they are already clear.
Code branches on the machine-readable error code (``rescue.*``, ``proto.*``), never on the wording.
"""

from __future__ import annotations

from typing import Any

TERMINAL_COMMAND = "sudo gentoo-rescue"
REPAIR_TITLE = "Repair a Gentoo system that does not start"
NOT_GENTOO_TEXT = "Not a Gentoo system - this repair tool only supports Gentoo"  # the engine says the same words about every other Linux it finds

# Texts the design fixes word for word (docs: rescue design 9.4).
FIRST_WRITE_NOTICE = (
    "Repairs change files on the selected system. Back up important data first. "
    "This tool never formats or repartitions disks and only touches the devices you selected."
)
LIVE_MEDIUM_NOTICE = "This is the medium the live system is running from. It is hidden and can never be modified."
LVM_ACTIVATION_NOTICE = "Activating a volume group makes its logical volumes available. In rare cases LVM repairs out-of-date metadata while doing so."
LOGS_NOTICE = (
    "The messages of the boot that failed are only kept when that system stores its logs on disk. "
    "If you find nothing here, the system did not keep them."
)
JOURNAL_NOTICE = (
    "Reading the journal starts the system's own journalctl program as root. Only continue if you trust this system."
)

NO_RESCUE_TITLE = "The repair engine is not available"
NO_RESCUE_TEXT = (
    "The installer service on this computer does not include the repair engine. "
    "This happens when the service is older than the installer, when it was started without repair support, "
    "or when this computer was not started from the live disc.\n\n"
    "You can still repair a Gentoo system from a terminal. Open a terminal and run:\n\n"
    f"    {TERMINAL_COMMAND}\n\n"
    "The terminal tool asks the same questions and shows every command before it runs."
)
INSTALL_RUNNING_TEXT = (
    "An installation is running. Repairs cannot run at the same time. "
    "Wait until the installation has finished, or stop it in the installer, then try again."
)
CONNECTING_TEXT = "Connecting to the installer service…"
CONNECTION_LOST_TEXT = "The connection to the installer service was lost."
RECONNECTING_TEXT = "The connection was lost. The repair keeps running in the background. Trying to reconnect…"
RECONNECT_FAILED_TEXT = (
    "The connection could not be restored. The repair may still be running in the background. "
    "Check the result with “Try again” before you start anything else."
)

LEFTOVERS_TEXT = (
    "The service was stopped or restarted while a repair was open. Release these so the disks can be used again. "
    "Only what the repair tool itself opened is released."
)

NOTHING_FOUND_TITLE = "No Gentoo system was found"
NOTHING_FOUND_INTRO = "The tool looked at every disk in this computer. These are common reasons:\n"
NOTHING_FOUND_OTHERS = "• The systems listed below are not Gentoo. This tool repairs Gentoo only.\n"  # added after the intro when the scan found another Linux
NOTHING_FOUND_REASONS = (
    "• The disk is encrypted. Unlock it first (see “Encrypted disks” above, if it is listed).\n"
    "• The system is on a RAID or LVM volume that is not active yet.\n"
    "• The disk is not connected, or the firmware hides it. Check the cables and the SATA mode "
    "(AHCI works best), then scan again.\n"
    "• The system uses a file system this live disc cannot read."
)
NOTHING_FOUND_TEXT = NOTHING_FOUND_INTRO + NOTHING_FOUND_REASONS

NOTHING_YET_TITLE = "No Gentoo system was found yet"
NOTHING_YET_TEXT = "There are encrypted disks or volumes that are not open. Unlock or activate them first: the tool then looks inside."
OTHER_SYSTEMS_TITLE = "Other systems"

RISK_TITLES = {
    "info": "Only looks",
    "low": "Low risk",
    "medium": "Medium risk",
    "high": "High risk",
    "dangerous": "Dangerous",
}
RISK_TEXTS = {
    "info": "This only looks at the system. Nothing is changed.",
    "low": "A small change that is easy to undo.",
    "medium": "This changes files in the system. Read the commands below before you continue.",
    "high": "This changes files that the system needs to start. If it fails, the system may not start. Make a backup first if you can.",
    "dangerous": "This can destroy data. Make a backup first.",
}

SEVERITY_TITLES = {"error": "Problem", "warn": "Warning", "info": "Note"}
SEVERITY_SYMBOLS = {"error": "×", "warn": "!", "info": "i"}

OUTCOME_TITLES = {
    "success": "The repair finished.",
    "warning": "The repair finished, but a check did not pass.",
    "failed": "The repair did not finish.",
    "cancelled": "The repair was stopped.",
}
OUTCOME_STATUS = {"success": "ok", "warning": "warning", "failed": "error", "cancelled": "warning"}

STOP_NOW_WARNING = (
    "Stopping now kills the command that is running.\n\n"
    "Programs such as dracut or grub-install may leave files half-written, and the system may not start afterwards. "
    "Only do this if the step is stuck and nothing has happened for a long time.\n\n"
    "The tool then runs its clean-up steps."
)
CANNOT_CLOSE_TEXT = (
    "A repair step is running and cannot be stopped safely. Please wait until it has finished.\n\n"
    "If the step is stuck, use “Stop now” in the repair window."
)

# Error codes whose engine message is too technical, or that need a next step. {message} is the engine's own text.
ERROR_TEXTS = {
    "rescue.busy_install": INSTALL_RUNNING_TEXT,
    "rescue.live_medium": LIVE_MEDIUM_NOTICE,
    "rescue.needs_network": "{message}\n\nConnect to a network (use the network icon in the panel) and try again.",
    "rescue.plan_stale": "The system changed since this plan was made. A new plan was prepared. Please read it again.",
    "rescue.plan_expired": "The plan was ready for too long and expired. A new plan was prepared. Please read it again.",
    "rescue.plan_mismatch": "The plan changed while you were reading it. A new plan was prepared. Please read it again.",
    "rescue.precondition_failed": "A condition for this repair is not met:\n{message}",
    "rescue.cancelled": "The operation was stopped.",
    "rescue.disabled": NO_RESCUE_TEXT,
    "rescue.no_memory": "{message}\n\nClose other programs and try again.",
    "proto.unsupported_action": NO_RESCUE_TEXT,
    "proto.internal": "The repair engine had an internal problem. Close this window and try again. If it happens again, use the terminal tool: " + TERMINAL_COMMAND,
    "proto.timeout": "The installer service did not answer in time. Try again in a moment.",
    "proto.bad_request": "The repair engine did not understand the request. This is a bug; please save the diagnostics and report it.",
}
STALE_PLAN_CODES = frozenset({"rescue.plan_stale", "rescue.plan_expired", "rescue.plan_mismatch"})


def friendly_error(code: str, message: str = "", data: dict[str, Any] | None = None) -> str:
    """Text for an error event. Unknown codes show the engine's message, which is written for people."""
    template = ERROR_TEXTS.get(code)
    text = (message or "").strip()
    if template is None:
        return text or "Something went wrong. Please try again."
    details = (data or {}).get("details") if isinstance(data, dict) else None
    reasons = details.get("reasons") if isinstance(details, dict) else None
    if code == "rescue.precondition_failed" and isinstance(reasons, list) and reasons:
        text = "\n".join(f"• {item}" for item in reasons)
    return template.format(message=text or "No further details were given.")


def describe_daemon_problem(kind: str, message: str) -> str:
    """The text for a service that cannot be reached; the client's own message already says what to do."""
    extra = f"\n\nYou can also repair a Gentoo system from a terminal without the service:\n\n    {TERMINAL_COMMAND}"
    return (message or "The installer service could not be reached.") + extra
