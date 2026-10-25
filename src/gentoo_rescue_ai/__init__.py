"""The AI rescue stick: three coding agents (Claude Code, Codex, OpenCode) next to a broken Gentoo disk, kept on a short leash.

The agents run as the unprivileged user ``rescue-ai``. They reach root only through ``gentoo-rescue-do``, which asks the
approver service (``approver.py``) on its own root-owned virtual console; the person types the per-boot password there,
for that one command. See docs/RESCUE-AI.md and the "Security design" section of build/plan/wave6-ai-rescue.md.

Importing this package touches nothing; every module keeps its side effects behind functions.
"""

from __future__ import annotations

AI_USER = "rescue-ai"
STATE_DIR = "/run/gentoo-rescue"
DO_SOCKET = f"{STATE_DIR}/do.sock"
DO_PASSWORD_FILE = f"{STATE_DIR}/do-password"
DO_AUDIT_LOG = "/var/log/gentoo-rescue/do.log"
TMUX_SESSION = "rescue"
TARGET_LINK = "/mnt/gentoo"
# Agents must never start, or run a command, below these (point 6 of the security design).
TARGET_PREFIXES = ("/mnt/gentoo", "/mnt/rescue")
