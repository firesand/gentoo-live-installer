"""``gentoo-rescue-login [claude|codex|opencode]``: sign in to an agent from the phone.

The agent does its own login; this helper only makes it easier to read: it runs the agent's login command, copies what the
command prints to the screen, and when a login address (https://...) shows up it also draws it as a QR code
(``qrencode -t ANSIUTF8``) so the phone camera can open it. The one-time code is then typed on the phone (Codex, GitHub
Copilot in OpenCode) or pasted back into the agent's own prompt (Claude Code).

What this helper never does:
* it never reads the keyboard while the agent logs in: the agent's standard input is the real terminal itself, so a pasted
  login code goes straight to the agent (Anthropic's terms forbid a wrapper to collect, store or relay Claude credentials);
* it never stores, logs or passes on a token, a code or an API key of Claude. For an API key it only tells you how to give it
  to the agent yourself;
* it never decides anything from the screen text except "is there an https address in it" (scraping is fragile, so a miss
  only means no QR code: the output is still shown in full).
"""

from __future__ import annotations

import fcntl
import getpass
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
from typing import Callable, Sequence
from urllib.parse import urlparse

from . import AI_USER
from .agents import AGENTS, installed_version
from .launcher import agent_env, home_of

LOGIN_COMMANDS = {
    "claude": ["claude", "auth", "login"],
    "codex": ["codex", "login", "--device-auth"],
    "opencode": ["opencode", "auth", "login"],
}
HOST_HINTS = {"claude": ("claude.com", "claude.ai", "anthropic.com"), "codex": ("openai.com", "chatgpt.com"), "opencode": ()}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>]")
URL = re.compile(r"https://[^\s\x00-\x1f\"'<>\\^`{|}]+")
LOGINISH = re.compile(r"login|device|auth|connect|sign-?in", re.I)
CONTINUATION = re.compile(r"^[A-Za-z0-9%&=_.~/?#+:@!$*,;-]+$")
MAX_URL = 2000
WAIT_FOR_URL_SECONDS = 600.0


def clean(text: str) -> str:
    return ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def find_login_url(text: str, columns: int = 80, agent: str = "") -> str | None:
    """The first login address in what an agent printed, or None.

    A long address wrapped by the terminal at ``columns`` is joined from its lines. Trailing punctuation is dropped. When the
    agent is known, an address on a clearly unrelated host is skipped (a docs link is not the login link)."""
    lines = clean(text).split("\n")
    hints = HOST_HINTS.get(agent, ())
    for index, line in enumerate(lines):
        for match in URL.finditer(line):
            url = match.group(0)
            if match.end() == len(line.rstrip()) and len(line.rstrip()) >= columns - 2:
                following = index + 1
                while following < len(lines) and CONTINUATION.match(lines[following].strip()) and len(url) < MAX_URL:
                    piece = lines[following].strip()
                    url += piece
                    following += 1
                    if len(lines[following - 1].rstrip()) < columns - 2:
                        break
            url = url.rstrip(".,;:)]}>'\"")
            host = urlparse(url).hostname or ""
            if not host or len(url) > MAX_URL:
                continue
            if hints and not any(host == h or host.endswith("." + h) for h in hints):
                continue
            if not LOGINISH.search(url):  # a docs or troubleshooting link in the output is not the login link
                continue
            return url
    return None


def qr_text(url: str, which: Callable[[str], str | None] = shutil.which, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> str | None:
    """The QR code of ``url`` as text for the terminal, or None when qrencode is missing or fails. The address is an argument, never shell text."""
    program = which("qrencode")
    if not program:
        return None
    try:
        done = run([program, "-t", "ANSIUTF8", "-m", "2", url], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 and done.stdout.strip() else None


def terminal_size(fd: int) -> tuple[int, int]:
    try:
        rows, cols = struct.unpack("hhhh", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))[:2]
        return (rows or 24, cols or 80)
    except OSError:
        return (24, 80)


def run_relayed(argv: Sequence[str], *, cwd: str, env: dict[str, str], stdin_fd: int, out_fd: int, on_text: Callable[[str], None], size_from: int | None = None,
                timeout: float = WAIT_FOR_URL_SECONDS * 3) -> int:
    """Run ``argv`` with its output on a pty that is copied to ``out_fd`` and shown to ``on_text``, and with ``stdin_fd`` as its standard input.

    The child inherits ``stdin_fd`` itself; this function never reads it. Returns the exit status."""
    master, slave = pty.openpty()
    rows, cols = terminal_size(size_from if size_from is not None else out_fd)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("hhhh", rows, cols, 0, 0))
    child = subprocess.Popen(list(argv), cwd=cwd, env=env, stdin=stdin_fd, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)

    def resize(*_: object) -> None:
        r, c = terminal_size(size_from if size_from is not None else out_fd)
        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("hhhh", r, c, 0, 0))
        except OSError:
            pass

    previous = signal.signal(signal.SIGWINCH, resize) if hasattr(signal, "SIGWINCH") and _main_thread() else None
    deadline = time.monotonic() + timeout
    try:
        while True:
            ready = select.select([master], [], [], 0.2)[0]
            if ready:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    data = b""
                if not data:
                    break
                os.write(out_fd, data)
                on_text(data.decode("utf-8", "replace"))
            elif child.poll() is not None:
                # drain what is left, then stop
                while select.select([master], [], [], 0.05)[0]:
                    try:
                        data = os.read(master, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    os.write(out_fd, data)
                    on_text(data.decode("utf-8", "replace"))
                break
            if time.monotonic() > deadline:
                child.terminate()
                break
        return child.wait()
    finally:
        os.close(master)
        if previous is not None:
            signal.signal(signal.SIGWINCH, previous)
        if child.poll() is None:
            child.kill()
            child.wait()


def _main_thread() -> bool:
    import threading

    return threading.current_thread() is threading.main_thread()


class Say:
    def __init__(self, out_fd: int) -> None:
        self.out_fd = out_fd

    def __call__(self, text: str = "") -> None:
        os.write(self.out_fd, (text.replace("\n", "\r\n") + "\r\n").encode("utf-8", "replace"))


FALLBACK = {
    "claude": ("Claude Code can also use an API key (Anthropic Console). Do it yourself in the shell, this helper never sees it:\n"
               "    read -rs ANTHROPIC_API_KEY; export ANTHROPIC_API_KEY; claude\n"
               "or sign in with a Console account: claude auth login --console"),
    "codex": "Codex can use an OpenAI API key: gentoo-rescue-login codex --api-key (the key is typed hidden and handed straight to codex).",
    "opencode": "OpenCode: start it (gentoo-rescue-ai opencode), type /connect, choose a provider and paste an API key.",
}
NO_URL = ("I could not find the login address in the output above (agents change their screens between versions).\n"
          "Look for a line starting with https:// above, or press c in Claude Code to copy it, and open it on your phone.")


def login(agent: str, *, home: str, stdin_fd: int, out_fd: int, say: Callable[[str], None], qr: Callable[[str], str | None] = qr_text) -> int:
    launch_env = agent_env(home)
    argv = LOGIN_COMMANDS[agent]
    seen: dict[str, str | None] = {"url": None, "buffer": ""}

    def show(url: str) -> None:
        seen["url"] = url
        say("")
        say(f"--- Phone: scan this to open the login page ({urlparse(url).hostname}) ---")
        picture = qr(url)
        if picture:
            for line in picture.rstrip("\n").split("\n"):
                say(line)
        else:
            say("(qrencode is not available; open the address by hand.)")
        say(url)

    def on_text(chunk: str) -> None:
        if seen["url"] is not None:
            return
        seen["buffer"] = ((seen["buffer"] or "") + chunk)[-20000:]
        url = find_login_url(seen["buffer"] or "", terminal_size(out_fd)[1], agent)
        if url is not None and (seen["buffer"] or "").rstrip(" \t").endswith(("\n", "\r")):  # an address at the very end may still be arriving
            show(url)

    status = run_relayed(argv, cwd=home, env=launch_env, stdin_fd=stdin_fd, out_fd=out_fd, on_text=on_text)
    if seen["url"] is None:
        url = find_login_url(seen["buffer"] or "", terminal_size(out_fd)[1], agent)
        if url is not None:
            show(url)
    if seen["url"] is None:
        say("")
        say(NO_URL)
    if status != 0:
        say(FALLBACK[agent])
    return status


def api_key_codex(home: str, prompt_key: Callable[[str], str] = getpass.getpass) -> int:
    """Codex only: the key is typed hidden, written to the standard input of ``codex login --with-api-key`` and kept nowhere else."""
    key = prompt_key("OpenAI API key (hidden): ").strip()
    if not key:
        print("No key typed.", file=sys.stderr)
        return 1
    done = subprocess.run(["codex", "login", "--with-api-key"], input=key + "\n", text=True, cwd=home, env=agent_env(home), check=False)
    return done.returncode


def main(argv: Sequence[str] | None = None, *, home: str | None = None, stdin_fd: int | None = None, out_fd: int | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    api_key = "--api-key" in arguments
    arguments = [a for a in arguments if a != "--api-key"]
    if arguments[:1] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    if os.geteuid() == 0 and home is None:
        print(f"gentoo-rescue-login: run it as {AI_USER} (the window named shell of the rescue session: Ctrl+B n). The agents and their logins belong to that user, not to root.", file=sys.stderr)
        return 1
    home = home or home_of()
    agent = arguments[0] if arguments else None
    if agent is None:
        print("Which agent? 1) claude  2) codex  3) opencode")
        pick = input("Number: ").strip()
        agent = {"1": "claude", "2": "codex", "3": "opencode"}.get(pick)
    if agent not in AGENTS:
        print("usage: gentoo-rescue-login [claude|codex|opencode] [--api-key (codex)]", file=sys.stderr)
        return 2
    if installed_version(agent, home) is None:
        print(f"{agent} is not installed yet. Run: gentoo-rescue-agents install {agent}", file=sys.stderr)
        return 1
    if api_key:
        if agent != "codex":
            print(FALLBACK[agent])
            return 0
        return api_key_codex(home)
    out = out_fd if out_fd is not None else 1
    return login(agent, home=home, stdin_fd=stdin_fd if stdin_fd is not None else 0, out_fd=out, say=Say(out))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
