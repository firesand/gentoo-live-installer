"""Rescue mode: find, unlock, mount, diagnose and repair an installed Linux.

The package is the engine behind the daemon's ``rescue.*`` actions and the
``gentoo-rescue`` command. Importing it stays cheap: nothing here touches the
system until a manager or API object is built. See docs/contracts.md for the
shared rules (target paths are only ever resolved with dir_fd walks, repairs
never format or repartition).
"""

from __future__ import annotations

RESCUE_API_VERSION = 1
FEATURE = "rescue.v1"
DEFAULT_SESSION_BASE = "/mnt/rescue"
DEFAULT_STATE_DIR = "/run/gentoo-rescue"
DEFAULT_AUDIT_DIR = "/var/lib/gentoo-rescue"

__all__ = ["RESCUE_API_VERSION", "FEATURE", "DEFAULT_SESSION_BASE", "DEFAULT_STATE_DIR", "DEFAULT_AUDIT_DIR"]
