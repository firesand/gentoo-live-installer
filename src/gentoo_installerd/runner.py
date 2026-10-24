from __future__ import annotations

import codecs
import logging
import os
import queue
import re
import select
import shlex
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from gentoo_installer.manifest import SECRET_KEYS

LOG = logging.getLogger(__name__)

LogSink = Callable[[str], None]

# How long a cancelled command (and everything it started) may take to exit
# after SIGTERM before the whole tree is SIGKILLed. emerge and dracut clean up
# build directories on SIGTERM, so this is generous.
CANCEL_GRACE_SECONDS = 20.0
# How long to wait for the tree to vanish after SIGKILL.
KILL_WAIT_SECONDS = 5.0
# After the command itself exited: how long the output may stay silent while a
# descendant keeps the pipe open before that descendant is terminated, and the
# absolute limit even when the descendant keeps printing.
EOF_GRACE_SECONDS = 3.0
EOF_HARD_LIMIT_SECONDS = 30.0
# Lines handled per pass before cancellation and the exit status are checked again.
BATCH_LINES = 500
# Recent (redacted) output lines kept for RunnerError.
TAIL_LINES = 200
# Upper bound for output kept when capture=True (blkid/lsblk/findmnt are tiny).
MAX_CAPTURE_CHARS = 32 * 1024 * 1024
# Longest single line passed on; longer ones are split.
MAX_LINE_CHARS = 64 * 1024
# Back-pressure: how many lines the reader may be ahead of the log sink.
OUTPUT_QUEUE_LINES = 5000


class RunnerError(RuntimeError):
    def __init__(self, argv: Sequence[str], returncode: int, output: str = "", *, summary: str = "") -> None:
        self.argv = tuple(argv)
        self.returncode = returncode
        self.output = output
        headline = summary or f"Command failed ({returncode})"
        super().__init__(f"{headline}: {format_command(argv)}" + (f": {output[-1000:]}" if output.strip() else ""))


class CommandTimeout(RunnerError):
    """The command exceeded the ``timeout`` it was given and its process tree was killed."""

    def __init__(self, argv: Sequence[str], timeout: float, output: str = "") -> None:
        self.timeout = timeout
        super().__init__(argv, 124, output, summary=f"Command timed out after {timeout:g} s")


class InstallCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        secret: bool = False,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout: float | None = None,
        capture: bool = True,
        root_identity: bool = False,
    ) -> CommandResult:
        """Run ``argv`` and return its combined stdout/stderr.

        ``secret`` means "``stdin`` carries a secret": the command line and the
        tool's own diagnostics stay visible, the secret values are scrubbed.
        ``timeout`` kills the command's whole process tree when exceeded.
        ``capture=False`` streams the output to the log only and returns an
        empty ``stdout`` (use it for long commands whose output nobody parses).
        ``root_identity`` runs the child with gid 0, no supplementary groups and
        umask 022, for commands that write into the target system.
        """
        ...


# --------------------------------------------------------------------- redaction

# Extra names that always mark a value as a secret, on top of the manifest's.
_EXTRA_SECRET_KEYS = {"passphrase", "secret", "token", "psk"}
_KEY_ALTERNATIVES = "|".join(re.escape(key) for key in sorted(SECRET_KEYS | _EXTRA_SECRET_KEYS, key=len, reverse=True))
# scheme://user:password@host (any scheme; a raw "@" inside the password is
# swallowed because the match runs to the last "@" before the first "/").
# The repetition bounds keep a pathological line (thousands of "x://u:" without a space) linear.
_URL_CREDENTIALS = re.compile(r"(?i)([a-z][a-z0-9+.-]{0,15}://)[^\s/'\"@:]{1,128}:[^\s/'\"]{0,512}@")
_JSON_SECRET = re.compile(rf'(?i)("(?:{_KEY_ALTERNATIVES})"\s*:\s*")(?:[^"\\]|\\.)*("?)')
_FLAG_SECRET = re.compile(rf"(?i)(--?(?:{_KEY_ALTERNATIVES})(?:=|\s+))\S+")
_ASSIGN_SECRET = re.compile(rf"(?i)(\b(?:{_KEY_ALTERNATIVES})\b\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)")


def redact_url(text: str) -> str:
    return _URL_CREDENTIALS.sub(r"\1<redacted>@", text)


def _ordered(secrets: Iterable[str]) -> tuple[str, ...]:
    """Longest first, so a secret that contains another one is replaced whole."""
    return tuple(sorted({item for item in secrets if item}, key=len, reverse=True))


def _scrub(text: str, ordered: tuple[str, ...]) -> str:
    if not text:
        return text
    result = redact_url(text)
    for secret in ordered:
        result = result.replace(secret, "<redacted>")
    result = _JSON_SECRET.sub(r"\g<1><redacted>\2", result)
    result = _FLAG_SECRET.sub(r"\g<1><redacted>", result)
    return _ASSIGN_SECRET.sub(r"\g<1><redacted>", result)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    return _scrub(text, _ordered(secrets))


def format_command(argv: Sequence[str], secrets: Iterable[str] = ()) -> str:
    ordered = _ordered(secrets)
    # Scrub every argument before quoting: shlex.quote can split a secret that
    # contains a quote character, so the raw value would no longer be found.
    parts = [_scrub(str(arg), ordered) for arg in argv]
    return _scrub(shlex.join(parts), ordered)


def stdin_secrets(stdin: str | None) -> list[str]:
    """Values worth scrubbing when ``stdin`` carries a secret: the payload, its lines, and the part after ``user:``."""
    if not stdin:
        return []
    values = {stdin, stdin.strip()}
    for line in stdin.splitlines():
        line = line.rstrip("\r")
        values.add(line)
        if ":" in line:
            values.add(line.split(":", 1)[1])
    return sorted((value for value in values if value), key=len, reverse=True)


def validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if not argv or any(not isinstance(arg, str) or not arg or "\x00" in arg or "\n" in arg or "\r" in arg for arg in argv):
        raise ValueError("Commands must be non-empty, single-line argument arrays")
    return tuple(argv)


# ------------------------------------------------------------ process tree walking


@dataclass(frozen=True)
class _ProcInfo:
    ppid: int
    pgrp: int
    session: int
    state: str
    start: int


def scan_processes(proc_root: Path = Path("/proc")) -> dict[int, _ProcInfo]:
    """One pass over ``/proc/<pid>/stat``: pid -> parent, group, session, state, start time."""
    table: dict[int, _ProcInfo] = {}
    try:
        names = os.listdir(proc_root)
    except OSError:
        return table
    for name in names:
        if not name.isdigit():
            continue
        try:
            raw = (proc_root / name / "stat").read_text(errors="replace")
            # "pid (comm) state ppid pgrp session ...": comm may contain spaces and parentheses.
            fields = raw[raw.rindex(")") + 2:].split()
            table[int(name)] = _ProcInfo(int(fields[1]), int(fields[2]), int(fields[3]), fields[0], int(fields[19]))
        except (OSError, ValueError, IndexError):
            continue  # the process exited while we looked
    return table


class ProcessTree:
    """Everything a command started: its group, its session and all descendants.

    Portage's pid-sandbox runs build phases under ``pid-ns-init``, which calls
    setsid(): that subtree is no longer in the command's process group, so
    killpg alone never reaches it. The tree is therefore followed through parent
    links, and remembered by (pid, start time) so that members re-parented to
    init when their parent dies stay tracked without risking a reused pid.
    """

    def __init__(self, root_pid: int, proc_root: Path = Path("/proc")) -> None:
        self.root_pid = root_pid
        self.proc_root = proc_root
        self._members: dict[int, int] = {}
        self._table: dict[int, _ProcInfo] = {}
        self.refresh()

    def refresh(self) -> None:
        table = self._table = scan_processes(self.proc_root)
        own = os.getpid()
        # Forget members that exited or whose pid was reused.
        self._members = {pid: start for pid, start in self._members.items() if pid in table and table[pid].start == start}
        known = set(self._members)
        if self.root_pid in table:
            known.add(self.root_pid)
        changed = True
        while changed:
            changed = False
            for pid, info in table.items():
                if pid in known or pid == own:
                    continue
                if info.ppid in known or info.pgrp == self.root_pid or info.session == self.root_pid:
                    known.add(pid)
                    changed = True
        for pid in known:
            if pid in table and pid != own:
                self._members[pid] = table[pid].start

    def alive(self) -> list[int]:
        """Members that are still running (zombies are already dead)."""
        return [pid for pid in self._members if pid in self._table and self._table[pid].state not in ("Z", "X")]

    def group_alive(self) -> bool:
        try:
            os.killpg(self.root_pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        # A zombie leader keeps its group "alive" until it is reaped; the caller reaps it.
        return True

    def empty(self) -> bool:
        return not self.alive()

    def signal(self, sig: int) -> None:
        """Signal the group (if anything of it remains) and every live member."""
        members = self.alive()
        if members or self.group_alive():
            try:
                os.killpg(self.root_pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        for pid in members:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass


# ------------------------------------------------------------------ the runners


def _read_output(fd: int, out: queue.Queue[str | None], stop: threading.Event) -> None:
    """Pump a pipe into ``out`` as text lines, ``None`` at EOF.

    Works on the raw descriptor with poll(): the thread can always be told to
    stop, and nobody ever closes a buffered file another thread is blocked in.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    poller = select.poll()
    poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    pending = ""

    def put(item: str | None) -> bool:
        while not stop.is_set():
            try:
                out.put(item, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def emit(chunk: str, final: bool) -> bool:
        nonlocal pending
        text = pending + chunk
        # Universal newlines: "\r\n" and a lone "\r" (progress bars) end a line.
        held = ""
        if not final and text.endswith("\r"):
            text, held = text[:-1], "\r"
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        *lines, pending = text.split("\n")
        pending += held
        for line in lines:
            if not put(line):
                return False
        while len(pending) > MAX_LINE_CHARS:
            if not put(pending[:MAX_LINE_CHARS]):
                return False
            pending = pending[MAX_LINE_CHARS:]
        return True

    try:
        while not stop.is_set():
            if not poller.poll(200):
                continue
            try:
                data = os.read(fd, 65536)
            except InterruptedError:
                continue
            except OSError:
                break
            if not data:
                break
            if not emit(decoder.decode(data), final=False):
                return
        else:
            return
        emit(decoder.decode(b"", final=True), final=True)
        if pending and not put(pending):
            return
        pending = ""
    finally:
        put(None)


class SubprocessRunner:
    def __init__(self, log: LogSink | None = None, cancel: threading.Event | None = None, secrets: Iterable[str] = ()) -> None:
        self.log = log or (lambda _line: None)
        self.cancel = cancel or threading.Event()
        self.secrets = tuple(secrets)
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def run_cleanup(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        """Release resources even after the installation cancellation event is set."""
        return SubprocessRunner(self.log, secrets=self.secrets).run(argv, **kwargs)  # type: ignore[arg-type]

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        secret: bool = False,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout: float | None = None,
        capture: bool = True,
        root_identity: bool = False,
    ) -> CommandResult:
        args = validate_argv(argv)
        if self.cancel.is_set():
            raise InstallCancelled("Installation cancelled")
        # ``secret`` means "stdin carries a secret": the command line and the tool's own
        # diagnostics stay visible, with the secret values (and anything echoing them) scrubbed.
        ordered = _ordered((*self.secrets, *stdin_secrets(stdin if secret else None)))
        shown = tuple(_scrub(arg, ordered) for arg in args)
        self.log(f"$ {format_command(args, ordered)}")
        process_env = os.environ.copy()
        if env:
            process_env.update(env)
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=process_env,
            start_new_session=True,
            shell=False,
            **self._identity_options(root_identity),
        )
        with self._lock:
            self._process = process
        assert process.stdout is not None
        lines: queue.Queue[str | None] = queue.Queue(OUTPUT_QUEUE_LINES)
        stop_reader = threading.Event()
        reader = threading.Thread(target=_read_output, args=(process.stdout.fileno(), lines, stop_reader), name="runner-output", daemon=True)
        reader.start()
        recent: deque[str] = deque(maxlen=TAIL_LINES)
        captured: list[str] = []
        captured_chars = 0
        capture_full = False
        deadline = time.monotonic() + timeout if timeout else None
        exited_at: float | None = None
        last_line_at = time.monotonic()
        eof = False

        def consume(line: str) -> None:
            nonlocal captured_chars, capture_full
            clean = _scrub(line, ordered)  # once per line; reused for the log and the failure tail
            self.log(clean)
            recent.append(clean)
            if capture and not capture_full:
                captured_chars += len(line) + 1
                if captured_chars > MAX_CAPTURE_CHARS:
                    capture_full = True
                    LOG.warning("Output of %s exceeds %d MiB; the rest is only streamed to the log", shown[0], MAX_CAPTURE_CHARS >> 20)
                else:
                    captured.append(clean if secret else line)

        try:
            if stdin is not None:
                try:
                    assert process.stdin is not None
                    process.stdin.write(stdin.encode("utf-8"))
                    process.stdin.close()
                except OSError:
                    pass  # the command exited without reading its input; its status says why
            while True:
                if self.cancel.is_set():
                    self._terminate(process)
                    raise InstallCancelled("Installation cancelled")
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    self._terminate(process)
                    raise CommandTimeout(shown, timeout or 0.0, "\n".join(recent))
                code = process.poll()
                if code is not None and exited_at is None:
                    exited_at = now
                if code is not None and eof:
                    break
                if code is not None and exited_at is not None and (now - max(exited_at, last_line_at) >= EOF_GRACE_SECONDS or now - exited_at >= EOF_HARD_LIMIT_SECONDS):
                    # The command is done but a descendant still holds its output pipe open.
                    self._reap_stragglers(process)
                    self.log("WARNING: command left background processes holding its output; terminated them")
                    stop_reader.set()
                    while True:  # whatever the pump had already queued
                        try:
                            line = lines.get_nowait()
                        except queue.Empty:
                            break
                        if line is not None:
                            consume(line)
                    break
                try:
                    batch = [lines.get(timeout=0.1)]
                except queue.Empty:
                    continue
                while len(batch) < BATCH_LINES:
                    try:
                        batch.append(lines.get_nowait())
                    except queue.Empty:
                        break
                for line in batch:
                    if line is None:
                        eof = True
                        break
                    consume(line)
                last_line_at = time.monotonic()
            returncode = process.wait()
            reader.join(timeout=1)
            if returncode:
                raise RunnerError(shown, returncode, "\n".join(recent))
            return CommandResult(args, returncode, "\n".join(captured) if capture else "")
        finally:
            stop_reader.set()
            with self._lock:
                self._process = None
            if process.poll() is None:
                # Abnormal exit (an exception from the log sink, ...): never leave the tree behind.
                self._terminate(process, grace=0.0)
            reader.join(timeout=2)
            self._close_pipes(process, reader)

    @staticmethod
    def _identity_options(root_identity: bool) -> dict[str, Any]:
        """Popen options that keep the daemon's live-only credentials out of the child.

        Popen applies these natively in the forked child; preexec_fn is not safe
        in a multi-threaded daemon. Changing gid needs root, so unprivileged runs
        (unit tests, --dry-run) simply keep their own credentials.
        """
        options: dict[str, Any] = {}
        is_root = hasattr(os, "geteuid") and os.geteuid() == 0
        if is_root:
            # The daemon's service group (gentoo-installer) means nothing on the target.
            options["group"] = 0
        if root_identity:
            options["umask"] = 0o022
            if is_root:
                options["extra_groups"] = []
        return options

    def _terminate(self, process: subprocess.Popen[bytes], *, grace: float | None = None) -> None:
        """SIGTERM the command's whole tree, wait for it to be gone, SIGKILL the rest."""
        if grace is None:
            grace = CANCEL_GRACE_SECONDS
        tree = ProcessTree(process.pid)
        if grace > 0:
            tree.signal(signal.SIGTERM)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                process.poll()  # reap the leader so its group can disappear
                tree.refresh()
                if tree.empty() and not tree.group_alive():
                    break
                time.sleep(0.05)
        deadline = time.monotonic() + KILL_WAIT_SECONDS
        while True:
            process.poll()
            tree.refresh()
            if tree.empty() and not tree.group_alive():
                break
            tree.signal(signal.SIGKILL)
            if time.monotonic() >= deadline:
                LOG.error("Processes of %s survived SIGKILL: %s", process.args, tree.alive())
                break
            time.sleep(0.05)
        try:
            process.wait(timeout=KILL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            LOG.error("Could not reap pid %d", process.pid)

    def _reap_stragglers(self, process: subprocess.Popen[bytes]) -> None:
        """The command exited; kill what it left behind in its group and session."""
        tree = ProcessTree(process.pid)
        tree.signal(signal.SIGKILL)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            tree.refresh()
            if tree.empty():
                break
            time.sleep(0.02)

    @staticmethod
    def _close_pipes(process: subprocess.Popen[bytes], reader: threading.Thread) -> None:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        # The reader owns the descriptor while it runs; closing it under a blocked
        # read would hang, so a reader that did not stop is left to finish on its own.
        if process.stdout is not None and not reader.is_alive():
            try:
                process.stdout.close()
            except OSError:
                pass


class DryRunRunner:
    def __init__(self, log: LogSink | None = None, secrets: Iterable[str] = ()) -> None:
        self.log = log or (lambda _line: None)
        self.secrets = tuple(secrets)
        self.commands: list[tuple[str, ...]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        secret: bool = False,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout: float | None = None,
        capture: bool = True,
        root_identity: bool = False,
    ) -> CommandResult:
        args = validate_argv(argv)
        self.commands.append(args)
        self.log("$ [dry-run] " + format_command(args, (*self.secrets, *stdin_secrets(stdin if secret else None))))
        return CommandResult(args, 0, "")
