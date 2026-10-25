"""A terminal inside the system, with a transcript, started from the daemon or directly on the console.

The command is ``setsid --ctty script ... --command '<chroot wrapper> /bin/bash --rcfile /run/gentoo-rescue/bashrc -i'``: the
wrapper is the same fixed form every chroot command uses (root:root, no supplementary groups, umask 022, clean environment).
The rcfile (written by ``chroot_help`` into the session's own fresh /run) gives the "(chroot) " prompt like the Handbook; a
system whose shell is not bash gets ``SHELL -l`` as before. The terminal is the user's
own shell, so it is outside the command policy; its start and end are audited and the output (not the typed keys, so
password prompts stay unrecorded) is kept as a transcript. A read-write terminal is the most dangerous action there is.
"""

from __future__ import annotations

import fcntl
import os
import re
import shlex
import signal
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import chroot_help
from .errors import NeedsConfirmation, RescueError
from .plan import BACKUP_PHRASE
from .runner import DEFAULT_CHROOT_ENV
from .safety import CHROOT_CREDENTIALS, CHROOT_WRAPPER_ARGV, CHROOT_PATH
from .session import RescueSession

TERM_RE = re.compile(r"^[A-Za-z0-9._+-]{1,40}$")
SHELLS = ("/bin/bash", "/usr/bin/bash", "/bin/ash", "/bin/sh", "/usr/bin/sh")


class Terminated(Exception):
    """SIGHUP or SIGTERM reached the tool that owns a terminal: unwind so the terminal is hung up and the system is released."""


def requirement(writable: bool, root_device: str, *, simple: bool = False) -> dict[str, Any]:
    """What the client must collect before a terminal opens (read-only: high risk, read-write: dangerous).

    ``simple`` is the one-question form of the repair window and of ``gentoo-chroot``: a read-write terminal needs one clear
    acknowledgement ("Commands I type here change the installed system"); the typed device name and the backup phrase are dropped.
    The audit record and the transcript stay.
    """
    name = root_device.rsplit("/", 1)[-1]
    if simple:
        return {"risk": "dangerous" if writable else "high", "required": True, "review": True, "typed": "", "phrase": "", "acknowledge": writable}
    return {"risk": "dangerous" if writable else "high", "required": True, "review": True, "typed": name, "phrase": BACKUP_PHRASE if writable else ""}


def check_confirmation(session: RescueSession, writable: bool, params: dict[str, Any]) -> None:
    simple = bool(params.get("simple"))
    need = requirement(writable, session.plan.root.device, simple=simple)
    if not params.get("confirm"):
        raise NeedsConfirmation("A terminal runs the system's own shell as root and needs confirmation", missing="confirm")
    if need.get("acknowledge") and not params.get("acknowledged"):
        raise NeedsConfirmation("Confirm that the commands you type in this terminal change the installed system", missing="acknowledged")
    if not simple and params.get("typed") != need["typed"]:
        raise NeedsConfirmation(f"Type {need['typed']} to confirm", missing="typed", expected=need["typed"])
    if need["phrase"] and params.get("phrase") != need["phrase"]:
        raise NeedsConfirmation(f"Type {need['phrase']} to confirm", missing="phrase", expected=need["phrase"])


def find_shell(session: RescueSession) -> str:
    with session.fs() as fs:
        for candidate in SHELLS:
            found = fs.which(candidate.rsplit("/", 1)[-1])
            if found:
                return found
    raise RescueError("tool_missing", "This system has no shell (bash, ash or sh)")


def chroot_command(session: RescueSession, term: str, shell: str, *, rcfile: bool = False) -> list[str]:
    """The fixed wrapper plus a clean environment and an interactive shell (a login shell without the rcfile); validated by the host policy before it is used."""
    env = dict(DEFAULT_CHROOT_ENV)
    env.update({"TERM": term, "LANG": "C.UTF-8", "SHELL": shell, "RESCUE_SESSION": session.sid, "PATH": CHROOT_PATH})
    pairs = [f"{key}={value}" for key, value in env.items()]
    start = [shell, "--rcfile", chroot_help.BASHRC_PATH, "-i"] if rcfile else [shell, "-l"]  # a login shell ignores --rcfile
    argv = [*CHROOT_WRAPPER_ARGV, *CHROOT_CREDENTIALS, session.root, "/usr/bin/env", "-i", *pairs, *start]
    session.runner.host.validate(argv)
    return argv


def host_command(session: RescueSession, term: str) -> list[str]:
    """A shell of the live system in the session directory: nothing of the target runs (inspect-only sessions)."""
    return ["/usr/bin/env", "-i", "HOME=/root", f"TERM={term}", "LANG=C.UTF-8", f"PATH={CHROOT_PATH}", f"RESCUE_SESSION={session.sid}", f"RESCUE_ROOT={session.root}", "/bin/bash", "-l"]


TRANSCRIPT_LIMIT = "64MiB"  # one runaway program (cat of a disk image) must not fill the live system's RAM disk (review R12)


def script_command(inner: list[str], transcript: Path) -> list[str]:
    """setsid --ctty script --log-out FILE --command 'inner': the child's exit status is propagated (--return)."""
    return ["setsid", "--ctty", "script", "--quiet", "--return", "--flush", "--output-limit", TRANSCRIPT_LIMIT, "--log-out", str(transcript), "--command", shlex.join(inner)]


class ShellManager:
    def __init__(self, session: RescueSession, *, spawn: Callable[..., subprocess.Popen[bytes]] | None = None, clock: Callable[[], float] = time.time,
                 view: Callable[[], Any] | None = None) -> None:
        self.session = session
        self.view = view
        self.spawn = spawn or subprocess.Popen
        self.clock = clock
        self.counter = 0

    def prepare(self, params: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        session = self.session
        writable = bool(params.get("writable"))
        host_only = bool(params.get("host"))
        term = str(params.get("term") or "xterm")
        if not TERM_RE.match(term):
            raise RescueError("bad_request", "Invalid terminal type", param="term")
        if not session.mounted:
            raise RescueError("bad_state", f"The session is {session.state}")
        if writable and session.mode != "rw":
            raise RescueError("needs_write", "Enable repairs first: a read-write terminal needs the system mounted read-write")
        if not host_only and not session.allow_write:
            raise RescueError("needs_write", "This session was opened for inspection only; its programs cannot run. A terminal in the live system at the mount point is available instead.", host_shell=True)
        check_confirmation(session, writable and not host_only, params)
        self.counter += 1
        directory = session.audit.path.parent / session.sid if session.audit.path else session.env.state_dir / "sessions" / session.sid
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        transcript = directory / f"shell-{self.counter}.log"
        if host_only:
            inner = host_command(session, term)
            mode = "host"
        else:
            if not session.ensure_virtual():
                raise RescueError("chroot_broken", "The system could not be prepared to run programs: " + "; ".join(session.warnings[-2:]))
            shell = find_shell(session)
            rcfile = chroot_help.uses_bash(shell) and chroot_help.write_bashrc(session)
            inner = chroot_command(session, term, shell, rcfile=rcfile)
            mode = "chroot-rw" if writable else "chroot-ro"
        argv = script_command(inner, transcript)
        return argv, {"mode": mode, "transcript": str(transcript), "writable": writable, "host": host_only, "term": term, "banner": self.banner(writable, host_only, params)}

    def banner(self, writable: bool, host_only: bool, params: dict[str, Any] | None = None) -> str:
        session = self.session
        name = session.candidate.get("os", {}).get("pretty_name", session.plan.root.device)
        state = "READ-WRITE: changes are real" if writable else "READ-ONLY"
        if host_only:
            return (f"\nGentoo Live rescue: a shell in the live system, in the directory of the mounted system ({name}), {state}.\n"
                    "Type 'exit' to leave. Unmount the system from the repair tool when you are done.\n\n")
        view = None
        if self.view is not None:
            try:
                view = self.view()
            except (RescueError, OSError, LookupError, ValueError):
                view = None
        has_dns = any(entry.data.get("virtual_name") == "resolv" for entry in session.ledger.entries)
        leave = chroot_help.LEAVE_UNMOUNTED if (params or {}).get("unmount_on_exit") else chroot_help.LEAVE_SESSION
        return chroot_help.banner(title=str(name), state=state, view=view, dns=has_dns, leave=leave)

    # ---- starting ----------------------------------------------------------------------------------------------------------------------------------
    def open_pty(self, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Daemon form: a PTY whose master the client receives over the socket; returns (master fd, info)."""
        argv, info = self.prepare(params)
        master, slave = os.openpty()
        rows, cols = int(params.get("rows") or 24), int(params.get("cols") or 80)
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0))
        try:
            process = self.spawn(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True, start_new_session=False, cwd="/")
        except OSError:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        self._track(process, info)
        info["pid"] = process.pid
        return master, info

    def run_here(self, params: dict[str, Any]) -> int:
        """Console form: the terminal shares the current tty until the shell exits; returns its exit status."""
        argv, info = self.prepare(params)
        info["_inline"] = True
        print(info["banner"], end="", flush=True)
        process = self.spawn(argv, cwd="/")
        self._track(process, info)
        try:
            return process.wait()
        except (KeyboardInterrupt, Terminated) as exc:
            self._hangup(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._hangup(process, signal.SIGKILL)
            if isinstance(exc, Terminated):
                raise
            return 130
        finally:
            self._finished(process, info)

    def _track(self, process: subprocess.Popen[bytes], info: dict[str, Any]) -> None:
        record = {"pid": process.pid, "writable": info["writable"], "mode": info["mode"], "transcript": info["transcript"]}
        self.session.shells.append(record)
        self.session.audit.record("shell_start", pid=process.pid, mode=info["mode"], transcript=info["transcript"], sync=True)
        if not info.get("_inline"):
            threading.Thread(target=self._reap, args=(process, info, record), name=f"rescue-shell-{process.pid}", daemon=True).start()

    def _reap(self, process: subprocess.Popen[bytes], info: dict[str, Any], record: dict[str, Any]) -> None:
        status = process.wait()
        if record in self.session.shells:
            self.session.shells.remove(record)
        self.session.audit.record("shell_end", pid=process.pid, status=status, transcript=info["transcript"], sync=True)

    def _finished(self, process: subprocess.Popen[bytes], info: dict[str, Any]) -> None:
        for record in list(self.session.shells):
            if record["pid"] == process.pid:
                self.session.shells.remove(record)
        self.session.audit.record("shell_end", pid=process.pid, status=process.returncode, transcript=info["transcript"], sync=True)

    @staticmethod
    def _hangup(process: subprocess.Popen[bytes], sig: int = signal.SIGHUP) -> None:
        try:
            os.killpg(process.pid, sig)
        except OSError:
            pass


def close_shells(session: RescueSession, grace: float = 3.0) -> int:
    """Hang up every terminal of a session (SIGHUP, then SIGKILL after a grace period); returns how many were open."""
    shells = list(session.shells)
    for record in shells:
        for sig in (signal.SIGHUP,):
            try:
                os.killpg(record["pid"], sig)
            except OSError:
                pass
    deadline = time.monotonic() + grace
    while session.shells and time.monotonic() < deadline:
        time.sleep(0.05)
    for record in list(session.shells):
        try:
            os.killpg(record["pid"], signal.SIGKILL)
        except OSError:
            pass
    return len(shells)
