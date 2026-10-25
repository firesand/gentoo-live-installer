"""``gentoo-chroot`` without a session: find a Gentoo system, open it, give a shell in it, and put everything back.

This is the Handbook's "chroot into your system" for a person whose system does not start, in one command: find the Gentoo
systems (the scan the repair tool already has), take the only one or ask by number, unlock an encrypted volume and activate LVM or
RAID on the console when needed, mount the system by its own fstab (read-only), ask one y/N question before it is mounted
read-write ("Changes are real"), prepare /proc, /sys, /dev, /run and the DNS settings, print a banner of the usual fixes, open
the shell, and unmount everything when the shell ends. A lost terminal (SIGHUP) or SIGTERM ends the shell the same way. Every
step goes through the same engine calls the GUI uses, so the audit record and the transcript are kept.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import sys
import time
from typing import Any

from .client import RescueClientError, attach_terminal
from .shell import Terminated

SESSION_RE = re.compile(r"^r[0-9a-f]{6}$")
CANDIDATE_RE = re.compile(r"^c[0-9a-f]{8}$")
MAX_TRIES = 3
SCAN_ROUNDS = 4  # unlock, then activate the volume group inside it, then (rarely) another layer
UNMOUNT_ATTEMPTS = 8
READ_WRITE_QUESTION = "Open it read-write? Changes are real: the commands you type here change the installed system. [y/N] "


def is_session_id(text: str) -> bool:
    return bool(SESSION_RE.match(text or ""))


def wants_chroot_without_session(args: argparse.Namespace) -> bool:
    """No session id given: ``gentoo-chroot``, ``gentoo-chroot /dev/sda2`` or ``gentoo-chroot c1a2b3c4d`` find and open a system first."""
    return not is_session_id(getattr(args, "session", None) or "")


# ---- finding the system ---------------------------------------------------------------------------------------------------------------------------
def _supported(scan: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in scan.get("candidates", []) if item.get("supported", True) and item.get("kind") != "snapshot"]


def _open_storage(ctx: Any, args: argparse.Namespace, scan: dict[str, Any]) -> bool:
    """Activate LVM and RAID, and unlock encrypted volumes (asking on the console); True when something was opened, so the scan is worth repeating."""
    console = ctx.console
    opened = False
    for vg in scan.get("stacks", {}).get("vgs", []):
        if vg.get("active"):
            continue
        console.say(f'Activating the LVM volume group "{vg["name"]}" (it makes its logical volumes available).')
        try:
            ctx.client.result("rescue.activate", kind="lvm", vg=vg["name"])
            opened = True
        except RescueClientError as exc:
            console.say(f"  could not activate it: {exc.message}")
    arrays = [array["uuid"] for array in scan.get("stacks", {}).get("arrays", []) if array.get("state") == "not_assembled"]
    if arrays:
        console.say("Assembling the RAID arrays that are not running.")
        try:
            ctx.client.result("rescue.activate", kind="md", uuids=arrays, degraded_ok=False)
            opened = True
        except RescueClientError as exc:
            console.say(f"  could not assemble them: {exc.message}")
    for node in scan.get("devices", []):
        if node.get("role") != "luks_locked":
            continue
        device = node["path"]
        if not (console.interactive or getattr(args, "passphrase_stdin", False)):
            console.say(f"{device} is encrypted. Run this on a terminal to type its passphrase, or give --passphrase-stdin.")
            continue
        for attempt in range(MAX_TRIES):
            passphrase = sys.stdin.readline().rstrip("\n") if getattr(args, "passphrase_stdin", False) else console.secret(f"Passphrase for the encrypted volume {device} (hidden, empty to skip): ")
            if not passphrase:
                break
            try:
                ctx.client.result("rescue.unlock", device=device, passphrase=passphrase)
                console.say(f"Unlocked {device}.")
                opened = True
                break
            except RescueClientError as exc:
                console.say(f"  {exc.message}")
                if exc.short != "bad_passphrase" or getattr(args, "passphrase_stdin", False):
                    break
    return opened


def find_system(ctx: Any, args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    """(scan, the chosen Gentoo system). Raises RescueClientError when none can be chosen."""
    from . import cli

    console = ctx.console
    wanted = getattr(args, "session", None) or ""
    scan = ctx.client.result("rescue.scan", probe=True)
    for _round in range(SCAN_ROUNDS):
        if _supported(scan) or not _open_storage(ctx, args, scan):
            break
        scan = ctx.client.result("rescue.scan", probe=True)
    candidates = _supported(scan)
    if wanted:
        chosen = next((item for item in scan.get("candidates", []) if wanted in {item["id"], item["device"]}), None)
        if chosen is None:
            raise RescueClientError("rescue.not_found", f"{wanted} is not a system this scan found. Run 'gentoo-rescue scan' to list them.")
        if not chosen.get("supported", True):
            raise RescueClientError("rescue.unsupported_system", " ".join(chosen.get("warnings") or ["Not a Gentoo system - this repair tool only supports Gentoo"]))
        return scan, chosen
    if not candidates:
        cli.render_others(console, [item for item in scan.get("candidates", []) if not item.get("supported", True) and item.get("kind") != "snapshot"])
        raise RescueClientError("rescue.not_found", "No Gentoo system was found. Check that its disk is connected; encrypted volumes ask for their passphrase when this runs on a terminal.")
    console.say("Gentoo systems found:")
    cli.render_systems(console, scan)
    if len(candidates) == 1:
        console.say(f"Using {cli.found_line(candidates[0])}.")
        return scan, candidates[0]
    if not console.interactive:
        raise RescueClientError("rescue.ambiguous", "Several Gentoo systems were found. Give the one to open: gentoo-chroot /dev/sda2 (or its id in brackets above).")
    for _attempt in range(MAX_TRIES):
        number = cli.typed_number(console.ask(f"Which one? [1-{len(candidates)}] "))
        if number is not None and 1 <= number <= len(candidates):
            return scan, candidates[number - 1]
        console.say("Please type one of the numbers shown.")
    raise RescueClientError("rescue.bad_request", "No system was chosen.")


# ---- mounting, the one question, the shell, unmounting ------------------------------------------------------------------------------------------
def mount_system(ctx: Any, scan: dict[str, Any], chosen: dict[str, Any]) -> dict[str, Any]:
    from . import cli

    request = {"candidate": chosen["id"], "scan_id": scan["scan_id"], "allow_write": True, "allow_degraded": False, "include_tier4": [], "overrides": []}
    dry = ctx.client.result("rescue.mount", dry_run=True, **request)
    cli.render_mount_plan(ctx.console, dry["mount_plan"])
    info = ctx.client.request("rescue.mount", plan_digest=dry["mount_plan"]["digest"], **request).result
    for warning in info.get("warnings", []):
        ctx.console.say(f"  ! {warning}")
    return info


def wants_read_write(ctx: Any, args: argparse.Namespace) -> bool:
    """The one question. ``--read-only`` skips it (no), ``--writable`` answers it (yes); without a terminal the answer is no."""
    if getattr(args, "read_only", False):
        return False
    if getattr(args, "writable", False):
        return True
    if not ctx.console.interactive:
        ctx.console.say("Mounted read-only (no terminal to ask on). Add --writable to change the system.")
        return False
    return ctx.console.ask(READ_WRITE_QUESTION).strip().lower() in {"y", "yes"}


def unmount_everything(ctx: Any, session_id: str) -> bool:
    """Release the session: the terminal is gone, so wait briefly for it to close, then force; True when nothing is left mounted."""
    console = ctx.console
    for attempt in range(UNMOUNT_ATTEMPTS):
        force = attempt == UNMOUNT_ATTEMPTS - 1
        try:
            result = ctx.client.result("rescue.unmount", session_id=session_id, force=force, save_report=False)
        except RescueClientError as exc:
            if exc.short == "not_found":
                return True
            if exc.short == "busy" and not force:
                time.sleep(0.5)
                continue
            holders = (exc.data.get("details") or {}).get("busy_holders") or []
            for holder in holders:
                console.say(f"  still in use by {holder['user']} {holder['pid']} {holder['command']}")
            console.say(f"Could not unmount everything: {exc.message}. Run 'gentoo-rescue unmount {session_id} --force' or 'gentoo-rescue cleanup'.")
            return False
        if result.get("leftovers"):
            console.say("Some mounts are still busy: " + ", ".join(str(item) for item in result["leftovers"]) + f". Run 'gentoo-rescue unmount {session_id} --force'.")
            return False
        console.say("Unmounted. You can remove the live medium and restart.")
        return True
    return False


class _Hangup:
    """SIGHUP (the terminal went away) and SIGTERM end the shell like exit does: the handler raises, the finally clauses unwind and unmount."""

    SIGNALS = tuple(sig for sig in (getattr(signal, "SIGHUP", None), getattr(signal, "SIGTERM", None)) if sig is not None)

    def __enter__(self) -> _Hangup:
        self.previous = {}
        for sig in self.SIGNALS:
            try:
                self.previous[sig] = signal.signal(sig, self._raise)
            except ValueError:  # not the main thread (a test harness): the tool then ends with its caller
                break
        return self

    @staticmethod
    def _raise(_signum: int, _frame: Any) -> None:
        raise Terminated()

    def __exit__(self, *_exc: object) -> None:
        for sig, handler in self.previous.items():
            signal.signal(sig, handler)


def run_chroot(ctx: Any, args: argparse.Namespace) -> int:
    from . import cli

    console = ctx.console
    session_id = ""
    status = cli.EXIT_OK
    with _Hangup():
        try:
            scan, chosen = find_system(ctx, args)
            info = mount_system(ctx, scan, chosen)
            session_id = info["session_id"]
            console.say(f"Mounted at {info['path']} (read-only; session {session_id}).")
            writable = wants_read_write(ctx, args)
            size = shutil.get_terminal_size((80, 24))
            request = {"action": "rescue.shell", "session_id": session_id, "writable": writable, "host": False, "simple": True, "confirm": True, "acknowledged": writable,
                       "unmount_on_exit": True, "term": os.environ.get("TERM", "xterm"), "rows": size.lines, "cols": size.columns}
            if writable:
                console.say("Switching to read-write (the file system journal is replayed, as a normal boot would).")
            reply, fd = ctx.transport.shell(request)
            if fd >= 0:
                console.say(reply.get("banner", ""))
                attach_terminal(fd)
            else:
                status = int(reply.get("exit") or 0)
        except RescueClientError as exc:
            console.warn(f"gentoo-chroot: {exc.message}")
            status = cli.exit_code_for(exc)
        except (KeyboardInterrupt, Terminated):
            console.warn("\nInterrupted: unmounting.")
            status = cli.EXIT_INTERRUPTED
        finally:
            if session_id:
                with _IgnoreSignals():
                    if not unmount_everything(ctx, session_id) and status == cli.EXIT_OK:
                        status = cli.EXIT_FAILED
    return status


class _IgnoreSignals:
    """While the system is being released a second hang-up must not leave it half unmounted."""

    def __enter__(self) -> None:
        self.previous = {}
        for sig in _Hangup.SIGNALS:
            try:
                self.previous[sig] = signal.signal(sig, signal.SIG_IGN)
            except ValueError:
                break

    def __exit__(self, *_exc: object) -> None:
        for sig, handler in self.previous.items():
            signal.signal(sig, handler)
