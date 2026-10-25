"""``gentoo-rescue-share [--read-only] [--interface IF] [--minutes N] | stop | status``: the rescue session in a phone's browser.

Starts ``ttyd`` as ``rescue-ai`` (never as root) on the LAN interface only, on a random port, with TLS (a fresh self-signed
certificate; its fingerprint is shown here so it can be compared with the phone's warning), a one-time three-word password, ONE
client, an origin check, and an idle timeout. The phone can type by default (``--read-only`` makes it a view-only share).

The phone is the user ``rescue-ai`` like the agents are: it can talk to the agent and use the shell window, but it can NEVER
approve a root command. Approvals (gentoo-rescue-do) are typed on the PC's own console, on a screen no ``rescue-ai`` process can
reach. Anyone on the same network who learns the password can use the agent for as long as the share runs: stop it when done.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence

from . import AI_USER, TMUX_SESSION
from .login import qr_text

DEFAULT_MINUTES = 15
PORT_RANGE = (20000, 60000)
CONSONANTS, VOWELS = "bdfgklmnprstvz", "aeiou"
POLL_SECONDS = 5.0
PHONE_NOTE = ("The phone counts as rescue-ai: it can type to the agent, but it can NEVER approve a root command.\n"
              "Approvals are typed only on this PC's console (the approver screen). Anyone on your network who learns the password can use the agent: stop it when you are done.")


class ShareError(Exception):
    """A sentence for the person: what is wrong and what to do."""


# ---- pieces --------------------------------------------------------------------------------------------------------------------------------------
def make_password(words: int = 3, rng: Callable[[str], str] = secrets.choice) -> str:
    """Three pronounceable words ("bako-fidu-meza"): easy to read off a screen and to type on a phone, about 37 bits."""
    return "-".join("".join(rng(CONSONANTS if i % 2 == 0 else VOWELS) for i in range(4)) for _ in range(words))


def lan_addresses(ip_output: str) -> list[tuple[str, str]]:
    """(interface, IPv4 address) from ``ip -4 -o addr show scope global``, loopback and link-local left out."""
    found = []
    for line in ip_output.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "inet":
            name, address = parts[1].rstrip(":"), parts[3].split("/")[0]
            if name != "lo" and not address.startswith(("127.", "169.254.")):
                found.append((name, address))
    return found


def default_route_interface(route_output: str) -> str | None:
    for line in route_output.splitlines():
        words = line.split()
        if words[:1] == ["default"] and "dev" in words and words.index("dev") + 1 < len(words):
            return words[words.index("dev") + 1]
    return None


def choose_interface(addresses: list[tuple[str, str]], wanted: str | None, route_interface: str | None) -> tuple[str, str]:
    if not addresses:
        raise ShareError("This PC has no LAN address, so there is nothing to share on. Connect to Wi-Fi or a cable first (gentoo-rescue-ai offers Wi-Fi).")
    if wanted:
        for name, address in addresses:
            if name == wanted:
                return name, address
        raise ShareError(f"{wanted} has no LAN address. Interfaces with one: {', '.join(name for name, _ in addresses)}")
    if route_interface:
        for name, address in addresses:
            if name == route_interface:
                return name, address
    return addresses[0]


def free_port(rng: Callable[[int, int], int] = secrets.SystemRandom().randint, address: str = "127.0.0.1", tries: int = 50) -> int:
    for _ in range(tries):
        port = rng(*PORT_RANGE)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((address, port))
            except OSError:
                continue
            return port
    raise ShareError("could not find a free port")


def ttyd_argv(*, ttyd: str, port: int, interface: str, cert: str, key: str, password: str, read_only: bool, tmux: str = "tmux") -> list[str]:
    """TLS, basic auth, one client, origin check, bound to the LAN interface; writable only without --read-only."""
    argv = [ttyd, "-p", str(port), "-i", interface, "-S", "-C", cert, "-K", key, "-c", f"rescue:{password}", "-m", "1", "-O"]
    if not read_only:
        argv.append("-W")
    argv += [tmux, "attach", *(["-r"] if read_only else []), "-t", TMUX_SESSION]
    return argv


def openssl_argv(openssl: str, cert: str, key: str, address: str) -> list[str]:
    return [openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", key, "-out", cert, "-days", "1",
            "-subj", "/CN=gentoo-rescue-share", "-addext", f"subjectAltName=IP:{address}"]


def fingerprint(openssl: str, cert: str) -> str:
    done = subprocess.run([openssl, "x509", "-noout", "-fingerprint", "-sha256", "-in", cert], capture_output=True, text=True, check=False)
    return done.stdout.strip().split("=", 1)[-1] if done.returncode == 0 and "=" in done.stdout else ""


def idle_seconds(clients: list[tuple[str, float]], now: float, started: float) -> float:
    """Seconds since the phone last did anything: the newest activity of any tmux client that is not the console (a pty, not a VT), else since the start."""
    phone = [activity for tty, activity in clients if tty.startswith("/dev/pts/")]
    return now - (max(phone) if phone else started)


def tmux_clients(tmux: str = "tmux") -> list[tuple[str, float]]:
    done = subprocess.run([tmux, "list-clients", "-t", TMUX_SESSION, "-F", "#{client_tty} #{client_activity}"], capture_output=True, text=True, check=False)
    clients = []
    for line in done.stdout.splitlines():
        tty, _, activity = line.partition(" ")
        if activity.strip().isdigit():
            clients.append((tty, float(activity)))
    return clients


# ---- state ---------------------------------------------------------------------------------------------------------------------------------------
@dataclass
class State:
    supervisor: int
    ttyd: int
    port: int
    address: str
    interface: str
    read_only: bool
    minutes: int
    started: float
    folder: str
    fingerprint: str


def state_dir(env: dict[str, str] | None = None, home: str | None = None) -> Path:
    env = env if env is not None else dict(os.environ)
    system = Path("/run/gentoo-rescue-share")
    if system.is_dir() and os.access(system, os.W_OK):
        return system
    base = Path(home or env.get("HOME") or "~").expanduser() / ".cache" / "gentoo-rescue-share"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    base.chmod(0o700)
    return base


def read_state(folder: Path) -> State | None:
    try:
        return State(**json.loads((folder / "state.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def alive(pid: int, needle: str) -> bool:
    """Whether ``pid`` is a running process whose command line contains ``needle`` (a pid alone could have been reused)."""
    try:
        return needle.encode() in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


def cleanup(folder: Path) -> None:
    """Remove the certificate, its private key and the state: nothing of a finished share stays behind."""
    for name in ("state.json", "cert.pem", "key.pem"):
        try:
            (folder / name).unlink()
        except OSError:
            pass


def stop_share(folder: Path, say: Callable[[str], None] = print, kill: Callable[[int, int], None] = os.kill) -> bool:
    state = read_state(folder)
    stopped = False
    if state is not None:
        for pid, needle in ((state.supervisor, "gentoo-rescue-share"), (state.ttyd, "ttyd")):
            if pid != os.getpid() and alive(pid, needle):
                try:
                    kill(pid, signal.SIGTERM)
                    stopped = True
                except ProcessLookupError:
                    pass
    cleanup(folder)
    say("The share was stopped." if stopped else "No share was running.")
    return stopped


# ---- supervisor ----------------------------------------------------------------------------------------------------------------------------------
def supervise(state: State, *, ttyd_alive: Callable[[], bool], clients: Callable[[], list[tuple[str, float]]], stop: Callable[[str], None], now: Callable[[], float] = time.time,
              sleep: Callable[[float], None] = time.sleep, poll: float = POLL_SECONDS, tmux_up: Callable[[], bool] = lambda: True) -> str:
    """Wait until the share has to end; call ``stop(reason)`` and return the reason."""
    while True:
        if not ttyd_alive():
            reason = "ttyd ended"
        elif not tmux_up():
            reason = "the rescue session ended"
        elif idle_seconds(clients(), now(), state.started) >= state.minutes * 60:
            reason = f"nothing happened for {state.minutes} minutes"
        else:
            sleep(poll)
            continue
        stop(reason)
        return reason


def start(args: argparse.Namespace, *, say: Callable[[str], None] = print, which: Callable[[str], str | None] = shutil.which, foreground: bool = False) -> int:
    if os.geteuid() == 0:
        raise ShareError(f"gentoo-rescue-share never runs as root. Run it in the {AI_USER} shell (window 2 of the rescue session).")
    ttyd, openssl, tmux = which("ttyd"), which("openssl"), which("tmux")
    for name, path in (("ttyd", ttyd), ("openssl", openssl), ("tmux", tmux)):
        if not path:
            raise ShareError(f"{name} is not installed on this stick.")
    assert ttyd and openssl and tmux
    if subprocess.run([tmux, "has-session", "-t", TMUX_SESSION], capture_output=True, check=False).returncode != 0:
        raise ShareError("There is no rescue session to share. Start it with gentoo-rescue-ai first.")
    folder = state_dir()
    current = read_state(folder)
    if current is not None and alive(current.ttyd, "ttyd"):
        raise ShareError(f"A share is already running on port {current.port}. Stop it first: gentoo-rescue-share stop")
    addresses = lan_addresses(subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"], capture_output=True, text=True, check=False).stdout)
    route = default_route_interface(subprocess.run(["ip", "-4", "route", "show", "default"], capture_output=True, text=True, check=False).stdout)
    interface, address = choose_interface(addresses, args.interface, route)
    cert, key = str(folder / "cert.pem"), str(folder / "key.pem")
    os.umask(0o077)
    made = subprocess.run(openssl_argv(openssl, cert, key, address), capture_output=True, text=True, check=False)
    if made.returncode != 0:
        raise ShareError(f"could not make the certificate: {made.stderr.strip()[:200]}")
    os.chmod(key, 0o600)
    password = make_password()
    port = free_port(address=address)
    subprocess.run([tmux, "set-option", "-t", TMUX_SESSION, "window-size", "latest"], capture_output=True, check=False)  # a small phone does not shrink the console
    process = subprocess.Popen(ttyd_argv(ttyd=ttyd, port=port, interface=interface, cert=cert, key=key, password=password, read_only=args.read_only, tmux=tmux),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(0.5)
    if process.poll() is not None:
        raise ShareError("ttyd did not start (is the port or the interface in use?).")
    state = State(os.getpid(), process.pid, port, address, interface, bool(args.read_only), args.minutes, time.time(), str(folder), fingerprint(openssl, cert))
    (folder / "state.json").write_text(json.dumps(asdict(state)), encoding="utf-8")
    url = f"https://rescue:{password}@{address}:{port}/"
    say("")
    say(f"Sharing the rescue session on {interface} ({address}) port {port}, " + ("VIEW ONLY." if args.read_only else "the phone CAN TYPE (use --read-only for a view-only share)."))
    picture = qr_text(url)
    say(picture.rstrip("\n") if picture else "(qrencode is not available: type the address and the password by hand.)")
    say(f"Address:      https://{address}:{port}/")
    say(f"User/password: rescue / {password}")
    say(f"Certificate (SHA-256): {state.fingerprint}")
    say("The phone's browser will warn about the certificate (it is self-signed): compare this fingerprint with the one it shows, then continue.")
    say(f"One phone at a time. It stops after {args.minutes} minutes without activity, or when you run: gentoo-rescue-share stop")
    say("")
    say(PHONE_NOTE)
    if foreground:
        supervise(state, ttyd_alive=lambda: process.poll() is None, clients=lambda: tmux_clients(tmux), stop=lambda reason: (process.terminate(), say(f"Stopped: {reason}")))
        cleanup(folder)
        return 0
    if os.fork() != 0:
        return 0
    os.setsid()
    for fd in (0, 1, 2):
        os.dup2(os.open(os.devnull, os.O_RDWR), fd)
    try:
        state.supervisor = os.getpid()
        (folder / "state.json").write_text(json.dumps(asdict(state)), encoding="utf-8")
        supervise(state, ttyd_alive=lambda: process.poll() is None, clients=lambda: tmux_clients(tmux), stop=lambda reason: process.terminate(),
                  tmux_up=lambda: subprocess.run([tmux, "has-session", "-t", TMUX_SESSION], capture_output=True, check=False).returncode == 0)
        cleanup(folder)
    finally:
        os._exit(0)


def status(say: Callable[[str], None] = print) -> int:
    state = read_state(state_dir())
    if state is None or not alive(state.ttyd, "ttyd"):
        say("No share is running.")
        return 1
    left = max(0, int(state.minutes * 60 - idle_seconds(tmux_clients(), time.time(), state.started)))
    say(f"Sharing on https://{state.address}:{state.port}/ ({state.interface}), " + ("view only" if state.read_only else "the phone can type") + f"; stops in {left // 60} min {left % 60} s without activity.")
    say(f"Certificate (SHA-256): {state.fingerprint}")
    say("(The password is shown only when the share starts.)")
    say(PHONE_NOTE.split("\n")[0])
    return 0


def parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gentoo-rescue-share", description="Show the rescue session in a phone's browser (TLS, one-time password, one client, LAN only, idle timeout).")
    parser.add_argument("action", nargs="?", choices=["start", "stop", "status"], default="start")
    parser.add_argument("--read-only", action="store_true", help="view-only share: the phone cannot type")
    parser.add_argument("--interface", help="network interface to share on (default: the one with the default route)")
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES, help=f"stop after this many minutes without activity (default {DEFAULT_MINUTES})")
    parser.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(list(argv))
    if not 1 <= args.minutes <= 240:
        parser.error("--minutes must be between 1 and 240")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    try:
        if args.action == "stop":
            stop_share(state_dir())
            return 0
        if args.action == "status":
            return status()
        return start(args, foreground=args.foreground)
    except ShareError as exc:
        print(f"gentoo-rescue-share: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
