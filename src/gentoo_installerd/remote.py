"""Live remote access (feature ``remote.v1``, interface I9): log in to the live medium over SSH from another computer.

sshd starts at boot on the live image, but nobody can log in: root is locked and the live user ``gentoo`` has an empty password,
which sshd refuses (``PermitEmptyPasswords no``). ``remote_enable`` gives ``gentoo`` a password (the one given, or a generated one
that is easy to read aloud) and makes sure sshd runs; ``remote_disable`` empties the password again (the boot state).

* ``remote_status`` -> ``{sshd_running, enabled, user, port, addresses, fingerprints}``
* ``remote_enable`` ``{password}`` -> the status plus ``password`` (once)
* ``remote_disable`` -> the status

The password travels on the standard input of ``chpasswd`` only. These actions work on the live medium and refuse to run anywhere else.
"""

from __future__ import annotations

import glob
import logging
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gentoo_installer import netdiag

from .events import ErrorCode, Event, completed_event
from .runner import Runner, RunnerError, SubprocessRunner
from .server import ActionError, Channel

if TYPE_CHECKING:
    from .server import ActionRegistry

LOG = logging.getLogger(__name__)

FEATURE = "remote.v1"
USER = "gentoo"
PORT = 22
LIVE_MARKERS = ("/run/initramfs/live", "/run/live/medium")
LIVE_CMDLINE_TOKENS = ("cdroot", "rd.live.image")
HOST_KEY_GLOB = "/etc/ssh/ssh_host_*_key.pub"
COMMAND_TIMEOUT = 20.0
MIN_PASSWORD = 6

WORDS = (
    "acorn amber anchor apple arrow aspen badge baker beach birch bison blaze bloom boat brick brook cabin camel candy canoe cedar chair "
    "cherry cliff cloud clover coast comet coral crane creek daisy delta diner dove dune eagle ember fable falcon fern field finch flame "
    "flute forest fox frost garden ginger glade globe grape grove harbor hazel heron honey igloo iris island ivory jade jelly jewel kayak "
    "kite lake lemon lilac lily lotus maple marsh meadow melon mint moon moss nest noble oak ocean olive onyx orbit otter panda peach "
    "pearl pine plum pond poppy quail quartz rabbit raven reed ridge river robin rose sage sand seal shell silver sky snow spark "
    "spruce star stone storm sugar sunny swan tiger tulip valley velvet violet walnut wave willow wind wolf zebra"
).split()


def make_runner() -> Runner:
    """The runner that executes the commands (tests replace this with a fake)."""
    return SubprocessRunner()


def is_live_medium() -> bool:
    """Whether this is the live medium (the same markers the repair tool uses)."""
    if any(Path(marker).exists() for marker in LIVE_MARKERS):
        return True
    try:
        words = Path("/proc/cmdline").read_text(encoding="utf-8").split()
    except OSError:
        return False
    return any(token in words for token in LIVE_CMDLINE_TOKENS)


def host_key_files() -> list[str]:
    return sorted(glob.glob(HOST_KEY_GLOB))


def generate_password(words: int = 3) -> str:
    """Three short words and two digits, such as 'maple-river-stone42': easy to read aloud and type."""
    return "-".join(secrets.choice(WORDS) for _ in range(words)) + f"{secrets.randbelow(90) + 10}"


def register(registry: ActionRegistry) -> None:
    registry.register_action("remote_status", handle_remote_status, feature=FEATURE)
    registry.register_action("remote_enable", handle_remote_enable, feature=FEATURE)
    registry.register_action("remote_disable", handle_remote_disable, feature=FEATURE)


# ------------------------------------------------------------------------------------------------ parsing


def parse_passwd_status(text: str) -> str:
    """The status letter of ``passwd -S USER``: P (a password), NP (none) or L (locked); '' when unreadable."""
    parts = text.split()
    return parts[1] if len(parts) >= 2 else ""


def parse_fingerprint(text: str) -> dict[str, str] | None:
    """``256 SHA256:abc... comment (ED25519)`` as {type, sha256}."""
    parts = text.split()
    if len(parts) < 2 or not parts[1].startswith("SHA256:"):
        return None
    kind = parts[-1].strip("()") if parts[-1].startswith("(") else "unknown"
    return {"type": kind, "sha256": parts[1]}


# ------------------------------------------------------------------------------------------------ actions


def _require_live() -> None:
    if not is_live_medium():
        raise ActionError("Remote access is only offered on the live medium, never on an installed system.", ErrorCode.BAD_REQUEST)


def _try(runner: Runner, argv: list[str]) -> str | None:
    try:
        return runner.run(argv, timeout=COMMAND_TIMEOUT).stdout
    except RunnerError:
        return None


def status(runner: Runner) -> dict[str, Any]:
    running = _try(runner, ["rc-service", "sshd", "status"]) is not None
    enabled = parse_passwd_status(_try(runner, ["passwd", "-S", USER]) or "") == "P"
    adapters = netdiag.parse_ip_addr(_try(runner, ["ip", "-j", "addr"]) or "")
    addresses = [address.split("/")[0] for adapter in adapters for address in adapter.ipv4]
    fingerprints = []
    for key in host_key_files():
        parsed = parse_fingerprint(_try(runner, ["ssh-keygen", "-l", "-f", key]) or "")
        if parsed:
            fingerprints.append(parsed)
    return {"sshd_running": running, "enabled": enabled, "user": USER, "port": PORT, "addresses": addresses, "fingerprints": fingerprints}


def _reply(channel: Channel, ok: bool, message: str, **data: Any) -> None:
    channel.send(Event("result", message=message, data={"ok": ok, "message": message, **data}))
    channel.send(completed_event(ok, message))


def handle_remote_status(request: dict, channel: Channel) -> None:
    _require_live()
    _reply(channel, True, "", **status(make_runner()))


def handle_remote_enable(request: dict, channel: Channel) -> None:
    _require_live()
    password = request.get("password") or ""
    if not isinstance(password, str) or any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise ActionError("The password contains characters that cannot be used.", ErrorCode.BAD_REQUEST)
    if password and len(password) < MIN_PASSWORD:
        raise ActionError(f"Use at least {MIN_PASSWORD} characters, or leave the password empty to get a generated one.", ErrorCode.BAD_REQUEST)
    generated = not password
    password = password or generate_password()
    runner = make_runner()
    try:
        runner.run(["chpasswd"], stdin=f"{USER}:{password}\n", secret=True, timeout=COMMAND_TIMEOUT)
    except RunnerError:
        LOG.warning("chpasswd failed for the live user")
        _reply(channel, False, "The password could not be set.")
        return
    if _try(runner, ["rc-service", "sshd", "status"]) is None and _try(runner, ["rc-service", "sshd", "start"]) is None:
        _reply(channel, False, "The password was set, but the SSH server could not be started.")
        return
    _reply(channel, True, "Remote access is on.", **status(runner), password=password, generated=generated)


def handle_remote_disable(request: dict, channel: Channel) -> None:
    _require_live()
    runner = make_runner()
    try:
        runner.run(["passwd", "-d", USER], timeout=COMMAND_TIMEOUT)
    except RunnerError:
        _reply(channel, False, "Remote access could not be turned off.")
        return
    _reply(channel, True, "Remote access is off.", **status(runner))
