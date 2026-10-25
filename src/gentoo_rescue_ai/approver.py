"""``gentoo-rescue-approver``: the only way the AI agents reach root, one typed approval per command.

Runs as root, started only by the OpenRC service ``gentoo-rescue-approver`` (never by an agent). It listens on a Unix socket
that ``gentoo-rescue-do`` (running as ``rescue-ai``) connects to. For every request it:

1. checks the caller with ``SO_PEERCRED``: only the ``rescue-ai`` user is served, anything else is refused and logged;
2. checks the request (a list of strings, an absolute folder that is not below the target disk);
3. switches the physical console to its own virtual terminal (``chvt``), a root-owned device that no ``rescue-ai`` process can
   open, and shows the exact command and folder with every control character made visible;
4. reads the per-boot password from THAT terminal, once, for this one command: a wrong or empty answer refuses the request,
   there is no retry loop and nothing carries over to the next request;
5. runs the command with ``execve`` (no shell), a clean environment and a fixed PATH, and relays its output over the socket as
   data (the caller's terminal is never handed to the root process);
6. writes everything (the request, the answer, the exit status, never the password) to the audit log and switches back.

The password is never read from the socket, from standard input or from any terminal an agent can reach: the approver only
ever reads it from the virtual terminal it opened itself. Everything that touches the system (terminal, clock, runner, peer
check) is injectable, so the rules are tested without root.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import grp
import hmac
import json
import os
import pwd
import select
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import termios
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from . import AI_USER, DO_AUDIT_LOG, DO_PASSWORD_FILE, DO_SOCKET, TARGET_PREFIXES

FIXED_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
CLEAN_ENV = {"PATH": FIXED_PATH, "HOME": "/root", "LANG": "C.UTF-8", "TERM": "dumb"}
MAX_REQUEST_BYTES = 64 * 1024
MAX_ARGV = 256
MAX_ARG_BYTES = 16 * 1024
APPROVAL_SECONDS = 300.0  # how long the person has to type on the console
REQUEST_SECONDS = 5.0  # how long a caller may take to send its request line
MIN_PASSWORD = 8
CHUNK = 16 * 1024

VT_GETSTATE, VT_ACTIVATE, VT_WAITACTIVE = 0x5603, 0x5606, 0x5607


class Refused(Exception):
    """A request that is turned down before anyone is asked; the text is shown to the caller."""


# ---- what the person sees ----------------------------------------------------------------------------------------------------------------------
def visible(text: str) -> str:
    """``text`` with every control character, escape and non-printable made visible, so that nothing a command line contains
    can redraw the approval screen (a request is data from an agent that may have read hostile files)."""
    out = []
    for char in text:
        code = ord(char)
        if char == "\\":
            out.append("\\\\")
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif code < 0x20 or code == 0x7F or 0x80 <= code < 0xA0 or not char.isprintable():
            out.append(f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}" if code < 0x10000 else f"\\U{code:08x}")
        else:
            out.append(char)
    return "".join(out)


def show_argv(argv: list[str]) -> str:
    """One line, one word per argument, quoted when it holds a space or quote; escapes made visible."""
    words = []
    for arg in argv:
        shown = visible(arg)
        words.append(f"'{shown}'" if (not shown or any(c in shown for c in " '\"$`;&|<>()*?#~\\")) else shown)
    return " ".join(words)


def prompt_text(peer: "Peer", argv: list[str], cwd: str) -> str:
    return (
        "\n==================== gentoo-rescue-do ====================\n"
        f"An AI agent ({visible(peer.name)}, process {peer.pid}) asks to run this command as ROOT:\n\n"
        f"    {show_argv(argv)}\n\n"
        f"  in the folder: {visible(cwd)}\n\n"
        "Nothing has been run yet. Read it carefully; the agent may have been misled by a file on the broken disk.\n"
        "Type the rescue password to allow THIS ONE command, or just press Enter to refuse.\n"
        "Password (hidden): ")


# ---- the caller ----------------------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Peer:
    pid: int
    uid: int
    gid: int
    name: str = ""


def peer_creds(conn: socket.socket) -> Peer:
    """Who is on the other end of a Unix socket, as the kernel recorded it when it connected."""
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        name = str(uid)
    return Peer(pid, uid, gid, name)


def user_uid(name: str = AI_USER) -> int | None:
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return None


# ---- the virtual terminal ------------------------------------------------------------------------------------------------------------------------
class Console(Protocol):
    """The approver's own terminal. ``read_secret`` is the ONLY place a password is ever read."""

    def show(self, text: str) -> None: ...
    def read_secret(self, prompt: str, timeout: float) -> str | None: ...
    def enter(self) -> None: ...
    def leave(self) -> None: ...


class VtConsole:
    """A real virtual terminal ``/dev/ttyN``: root-owned, mode 0600, opened without becoming its controlling terminal."""

    def __init__(self, number: int, device: str | None = None, control: str = "/dev/tty0") -> None:
        self.number = number
        self.device = device or f"/dev/tty{number}"
        self.control = control
        self._previous: int | None = None
        self._fd = -1

    def secure(self) -> None:
        """Make the terminal root:root 0600 and refuse to go on if it is anything else (a getty or a login could hand it to a user)."""
        fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
        try:
            os.fchown(fd, 0, 0)
            os.fchmod(fd, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISCHR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
                raise OSError(f"{self.device} is not a private root terminal")
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def _ioctl_control(self, request: int, arg: Any, mutate: bool = False) -> Any:
        fd = os.open(self.control, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
        try:
            return fcntl.ioctl(fd, request, arg, mutate) if mutate else fcntl.ioctl(fd, request, arg)
        finally:
            os.close(fd)

    def enter(self) -> None:
        if self._fd < 0:
            self.secure()
        try:
            state = self._ioctl_control(VT_GETSTATE, b"\0" * 6)
            self._previous = struct.unpack("HHH", state)[0] or None
        except OSError:
            self._previous = None
        self._ioctl_control(VT_ACTIVATE, self.number)
        self._ioctl_control(VT_WAITACTIVE, self.number)

    def leave(self) -> None:
        if self._previous and self._previous != self.number:
            try:
                self._ioctl_control(VT_ACTIVATE, self._previous)
            except OSError:
                pass

    def show(self, text: str) -> None:
        os.write(self._fd, text.replace("\n", "\r\n").encode("utf-8", "replace"))

    def read_secret(self, prompt: str, timeout: float) -> str | None:
        fd = self._fd
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] &= ~(termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL)
        new[3] |= termios.ICANON
        try:
            termios.tcsetattr(fd, termios.TCSAFLUSH, new)  # TCSAFLUSH also throws away anything typed before the question
            self.show(prompt)
            deadline = time.monotonic() + timeout
            data = b""
            while not data.endswith(b"\n"):
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([fd], [], [], left)[0]:
                    return None
                chunk = os.read(fd, 256)
                if not chunk:
                    return None
                data += chunk
                if len(data) > 1024:
                    return ""
            return data.decode("utf-8", "replace").rstrip("\r\n")
        finally:
            termios.tcsetattr(fd, termios.TCSAFLUSH, old)
            self.show("\n")


# ---- the audit log -------------------------------------------------------------------------------------------------------------------------------
class Audit:
    """JSON lines in a root-only file; a failure to write never lets a request through unlogged: the caller checks ``ok``."""

    def __init__(self, path: str = DO_AUDIT_LOG, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock

    def write(self, event: str, **fields: Any) -> bool:
        record = {"ts": round(self.clock(), 3), "event": event, **fields}
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode())
            finally:
                os.close(fd)
            return True
        except OSError:
            return False


# ---- running the command -------------------------------------------------------------------------------------------------------------------------
def resolve_program(program: str) -> str:
    """The absolute path that will be executed. A name without a slash is searched in the fixed PATH only."""
    if "/" in program:
        if not program.startswith("/"):
            raise Refused("the program must be an absolute path or a bare name (not a relative path)")
        resolved = program
    else:
        for folder in FIXED_PATH.split(":"):
            candidate = os.path.join(folder, program)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        raise Refused(f"{program!r} was not found in {FIXED_PATH}")
    if not (os.path.isfile(resolved) and os.access(resolved, os.X_OK)):
        raise Refused(f"{resolved} is not an executable file")
    return resolved


def below_target(path: str) -> bool:
    real = os.path.realpath(path)
    return any(real == prefix or real.startswith(prefix + "/") for prefix in TARGET_PREFIXES)


def check_request(request: Any) -> tuple[list[str], str]:
    """The (argv, cwd) of a request, or ``Refused``."""
    if not isinstance(request, dict):
        raise Refused("the request is not a JSON object")
    argv, cwd = request.get("argv"), request.get("cwd")
    if not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV or not all(isinstance(a, str) for a in argv):
        raise Refused("argv must be a short, non-empty list of strings")
    if any("\0" in a or len(a.encode("utf-8", "surrogateescape")) > MAX_ARG_BYTES for a in argv):
        raise Refused("an argument holds a NUL byte or is too long")
    if not argv[0]:
        raise Refused("the program name is empty")
    if not isinstance(cwd, str) or not cwd.startswith("/") or "\0" in cwd:
        raise Refused("cwd must be an absolute folder")
    if not os.path.isdir(cwd):
        raise Refused(f"{cwd} is not a folder")
    if below_target(cwd):
        raise Refused("commands never start inside the target disk (/mnt/gentoo, /mnt/rescue): use 'gentoo-rescue' for the target, or a full path from another folder")
    return list(argv), cwd


Send = Callable[[dict[str, Any]], bool]


def run_command(argv: list[str], cwd: str, send: Send) -> int:
    """execve ``argv`` without a shell in a clean environment; relay its output as data. Returns the exit status (128+signal when killed)."""
    program = resolve_program(argv[0])
    process = subprocess.Popen([program, *argv[1:]], executable=program, cwd=cwd, env=dict(CLEAN_ENV), shell=False, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, start_new_session=True)
    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    open_streams = 2
    while open_streams:
        for key, _ in selector.select():
            data = os.read(key.fileobj.fileno(), CHUNK)  # type: ignore[union-attr]
            if not data:
                selector.unregister(key.fileobj)
                open_streams -= 1
                continue
            send({"event": "output", "stream": key.data, "data": base64.b64encode(data).decode()})  # a gone caller never stops the command
    code = process.wait()
    return code if code >= 0 else 128 - code


# ---- the service ---------------------------------------------------------------------------------------------------------------------------------
class Approver:
    def __init__(self, console: Console, password: str, audit: Audit, allowed_uids: set[int], *, runner: Callable[[list[str], str, Send], int] = run_command,
                 peer_of: Callable[[socket.socket], Peer] = peer_creds, approval_seconds: float = APPROVAL_SECONDS) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError("the per-boot password is too short")
        self._password = password.encode()
        self.console, self.audit, self.allowed_uids = console, audit, set(allowed_uids)
        self.runner, self.peer_of, self.approval_seconds = runner, peer_of, approval_seconds

    # one request, one connection
    def handle(self, conn: socket.socket) -> None:
        def send(event: dict[str, Any]) -> bool:
            try:
                conn.sendall((json.dumps(event) + "\n").encode())
                return True
            except OSError:
                return False

        peer = self.peer_of(conn)
        if peer.uid not in self.allowed_uids:
            self.audit.write("refused", reason="caller is not rescue-ai", pid=peer.pid, uid=peer.uid)
            send({"event": "denied", "reason": "only the rescue-ai user may use gentoo-rescue-do"})
            return
        try:
            argv, cwd = check_request(self._read_request(conn))
        except Refused as exc:
            self.audit.write("refused", reason=str(exc), pid=peer.pid, uid=peer.uid)
            send({"event": "denied", "reason": str(exc)})
            return
        try:
            program = resolve_program(argv[0])
        except Refused as exc:
            self.audit.write("refused", reason=str(exc), argv=argv, cwd=cwd, pid=peer.pid, uid=peer.uid)
            send({"event": "denied", "reason": str(exc)})
            return
        shown = [program, *argv[1:]]
        if not self.audit.write("request", argv=shown, cwd=cwd, pid=peer.pid, uid=peer.uid):
            send({"event": "denied", "reason": "the audit log cannot be written, so nothing is run"})
            return
        send({"event": "waiting", "message": "waiting for the person at the PC's console to approve this command"})
        answer = self._ask(peer, shown, cwd)
        if answer != "approved":
            self.audit.write("answer", answer=answer, argv=shown, pid=peer.pid)
            send({"event": "denied", "reason": {"timeout": "nobody approved it in time", "wrong": "the password was wrong or empty: refused", "console": "the console is not available"}.get(answer, answer)})
            return
        if self._caller_gone(conn):
            self.audit.write("answer", answer="caller left before the command started", argv=shown, pid=peer.pid)
            return
        self.audit.write("answer", answer="approved", argv=shown, pid=peer.pid)
        self.console.show("Approved. Running it; its output goes back to the agent.\n")
        status = -1
        try:
            status = self.runner([program, *argv[1:]], cwd, send)
        except (OSError, Refused) as exc:
            send({"event": "denied", "reason": f"could not start it: {exc}"})
            status = -2
        self.audit.write("exit", status=status, argv=shown, pid=peer.pid)
        self.console.show(f"Finished (exit status {status}). Going back.\n")
        self.console.leave()  # before the caller hears about the end, so it never sees a half-restored console
        if status != -2:
            send({"event": "exit", "status": status})

    def _ask(self, peer: Peer, argv: list[str], cwd: str) -> str:
        try:
            self.console.enter()
            typed = self.console.read_secret(prompt_text(peer, argv, cwd), self.approval_seconds)
        except OSError:
            return "console"
        if typed is None:
            self.console.leave()
            return "timeout"
        ok = bool(typed) and hmac.compare_digest(typed.encode(), self._password)
        if not ok:
            self.console.show("Refused.\n")
            self.console.leave()
        return "approved" if ok else "wrong"

    @staticmethod
    def _read_request(conn: socket.socket) -> Any:
        conn.settimeout(REQUEST_SECONDS)
        data = b""
        try:
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if len(data) > MAX_REQUEST_BYTES:
                    raise Refused("the request is too large")
        except (socket.timeout, OSError):
            raise Refused("no complete request arrived")
        finally:
            conn.settimeout(None)
        try:
            return json.loads(data.split(b"\n", 1)[0])
        except ValueError:
            raise Refused("the request is not JSON")

    @staticmethod
    def _caller_gone(conn: socket.socket) -> bool:
        try:
            return conn.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True

    def serve(self, listener: socket.socket, stop: Callable[[], bool] = lambda: False) -> None:
        listener.settimeout(0.2)
        while not stop():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            with conn:
                try:
                    self.handle(conn)
                except Exception as exc:  # one bad request never stops the service
                    self.audit.write("error", error=repr(exc))


# ---- start-up ------------------------------------------------------------------------------------------------------------------------------------
def read_password(path: str) -> str:
    """The per-boot password; refuses a file that is not root:root 0600, so a password an agent could read is never used."""
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError(f"{path} must be a regular file owned by root with mode 0600")
    return Path(path).read_text(encoding="utf-8").strip()


def make_listener(path: str, group: str = AI_USER) -> socket.socket:
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o755, exist_ok=True)
    if os.path.lexists(path):
        os.unlink(path)
    old = os.umask(0o177)
    try:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
    finally:
        os.umask(old)
    try:
        gid = grp.getgrnam(group).gr_gid
        os.chown(path, 0, gid)
        os.chmod(path, 0o660)
    except KeyError:
        os.chmod(path, 0o600)  # no rescue-ai group: nobody but root can connect
    listener.listen(4)
    return listener


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gentoo-rescue-approver", description="Root service behind gentoo-rescue-do. Started by the OpenRC service only.")
    parser.add_argument("--vt", type=int, required=True, help="the virtual terminal that shows the question (for example 9)")
    parser.add_argument("--socket", default=DO_SOCKET)
    parser.add_argument("--audit", default=DO_AUDIT_LOG)
    parser.add_argument("--password-file", default=DO_PASSWORD_FILE)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("gentoo-rescue-approver: this service runs as root (it is started by the OpenRC service, not by hand)", file=sys.stderr)
        return 1
    uid = user_uid()
    if uid is None:
        print(f"gentoo-rescue-approver: the user {AI_USER} does not exist", file=sys.stderr)
        return 1
    try:
        password = read_password(args.password_file)
        console = VtConsole(args.vt)
        console.secure()
        approver = Approver(console, password, Audit(args.audit), {uid})
    except (OSError, ValueError) as exc:
        print(f"gentoo-rescue-approver: {exc}", file=sys.stderr)
        return 1
    stopping = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stopping.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stopping.update(now=True))
    listener = make_listener(args.socket)
    approver.audit.write("start", vt=args.vt, socket=args.socket)
    approver.serve(listener, lambda: stopping["now"])
    listener.close()
    try:
        os.unlink(args.socket)
    except OSError:
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
