"""``gentoo-rescue-ai [claude|codex|opencode|shell]``: the AI rescue session.

It runs as root on tty1 first (so the person can fix the network and type a disk passphrase where no agent can see it), then
opens a tmux session ``rescue`` as the unprivileged user ``rescue-ai``:

* the window named ``agent`` is the agent (or a shell when none is installed yet), the window ``shell`` a plain ``rescue-ai`` shell;
* every window starts in the home directory of ``rescue-ai``, never below the broken disk (``/mnt/gentoo``, ``/mnt/rescue``);
* the agents get a clean environment, their safe configuration files are put back at every start, and project settings
  of the working directory are switched off where the agent allows it (Claude ``--setting-sources user``, Codex untrusted
  projects, OpenCode ``OPENCODE_DISABLE_CLAUDE_CODE`` and an inline permission config);
* there is no approval window: approvals are typed on the approver's own virtual terminal (see approver.py).
"""

from __future__ import annotations

import getpass
import json
import os
import pwd
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Sequence

from . import AI_USER, DO_PASSWORD_FILE, TARGET_PREFIXES, TMUX_SESSION
from . import preflight as checks
from .agents import AGENTS, installed_version

GUIDE_PATH = "/etc/gentoo-rescue/AGENTS.md"
SKEL_PATH = "/etc/skel-rescue-ai"
SYMLINKS = {".codex/AGENTS.md": GUIDE_PATH, ".config/opencode/AGENTS.md": GUIDE_PATH}
SAFE_FILES = (".codex/config.toml", ".config/opencode/opencode.json", ".bashrc", ".bash_profile")  # put back at every start, whatever an agent wrote there
DAEMON_SOCKET = "/run/gentoo-installer/server.sock"
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
CLEAR_SCREEN = "\033[3J\033[H\033[2J"

CLAUDE_COMMAND = ["claude", "--setting-sources", "user"]
CODEX_COMMAND = ["codex", "--sandbox", "read-only", "--ask-for-approval", "on-request"]
OPENCODE_COMMAND = ["opencode"]
COMMANDS = {"claude": CLAUDE_COMMAND, "codex": CODEX_COMMAND, "opencode": OPENCODE_COMMAND}


@dataclass(frozen=True)
class Launch:
    """Everything needed to start a process of the session: nothing here comes from the broken disk."""

    argv: list[str]
    cwd: str
    env: dict[str, str]


def skel_dir() -> Path:
    system = Path(SKEL_PATH)
    return system if system.is_dir() else Path(str(resources.files("gentoo_rescue_ai").joinpath("data/root/etc/skel-rescue-ai")))


def opencode_permissions(skel: Path | None = None) -> str:
    """The OpenCode config as inline JSON: it outranks every opencode.json found in a project folder."""
    path = (skel or skel_dir()) / ".config" / "opencode" / "opencode.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    return json.dumps({"permission": config["permission"], "autoupdate": False, "share": "disabled"}, separators=(",", ":"))


def home_of(user: str = AI_USER) -> str:
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        return f"/home/{user}"


def agent_env(home: str, term: str = "linux", lang: str = "C.UTF-8", skel: Path | None = None) -> dict[str, str]:
    """A fresh environment for the session: no variable of the launcher (and so none of the broken disk) is passed on."""
    return {
        "HOME": home, "USER": AI_USER, "LOGNAME": AI_USER, "SHELL": "/bin/bash", "PWD": home, "TERM": term, "LANG": lang,
        "PATH": f"{home}/.local/bin:{SAFE_PATH}",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1", "OPENCODE_CONFIG_CONTENT": opencode_permissions(skel),
        "DISABLE_AUTOUPDATER": "1",
    }


def agent_launch(agent: str, home: str | None = None, term: str = "linux") -> Launch:
    """How to start ``agent``: its fixed command line, the home directory as working directory, a clean environment."""
    if agent not in COMMANDS:
        raise ValueError(f"unknown agent {agent!r}")
    home = home or home_of()
    return Launch(list(COMMANDS[agent]), home, agent_env(home, term))


def below_target(path: str) -> bool:
    real = os.path.realpath(path)
    return any(real == prefix or real.startswith(prefix + "/") for prefix in TARGET_PREFIXES)


class UnsafeHome(Exception):
    """The home directory of rescue-ai holds a link or a file of another user where a folder must be: root does not touch it."""


def _open_folder(parent_fd: int, name: str, owner: tuple[int, int] | None, shown: str) -> int:
    """Open (making it when missing) the folder ``name`` below ``parent_fd`` without ever following a link. With ``owner`` the folder
    must belong to rescue-ai or to root (root made it at an earlier start) and is handed to rescue-ai."""
    try:
        os.mkdir(name, 0o755, dir_fd=parent_fd)
    except FileExistsError:
        pass
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    except OSError as exc:
        raise UnsafeHome(f"{shown} is a link or not a folder; it is left alone ({exc.strerror})") from exc
    if owner is not None:
        info = os.fstat(fd)
        if info.st_uid not in (owner[0], 0):
            os.close(fd)
            raise UnsafeHome(f"{shown} belongs to user id {info.st_uid}, not to {AI_USER}; it is left alone")
        os.fchown(fd, *owner)
    return fd


def _walk(home_fd: int, relative: str, owner: tuple[int, int] | None, home: str) -> tuple[int, str]:
    """The fd of the folder that holds ``relative`` (every folder on the way opened with O_NOFOLLOW) and the file name."""
    *folders, name = relative.split("/")
    fd, shown = os.dup(home_fd), home
    try:
        for part in folders:
            shown = f"{shown}/{part}"
            child = _open_folder(fd, part, owner, shown)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd, name


def _remove(fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=fd)
    except FileNotFoundError:
        return
    except IsADirectoryError:
        raise UnsafeHome(f"{name} is a folder where a file belongs; it is left alone") from None


def prepare_home(home: str, skel: Path | None = None, owner: tuple[int, int] | None = None) -> list[str]:
    """Put the safe configuration of the agents in the home directory (always replacing what is there) and the AGENTS.md links.
    Returns the paths written. ``owner`` is (uid, gid) for chown (None: leave as is, for tests).

    The home directory is controlled by the agent, so every step goes through directory file descriptors opened with O_NOFOLLOW: a
    folder the agent replaced by a link to /etc (or anything else) is refused (``UnsafeHome``) and never chowned or written into.
    Files are made with O_EXCL|O_NOFOLLOW and handed over with fchown; links with lchown semantics."""
    skel = skel or skel_dir()
    written: list[str] = []
    try:
        home_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise UnsafeHome(f"{home} cannot be opened as a plain folder ({exc.strerror})") from exc
    try:
        if owner is not None:
            info = os.fstat(home_fd)
            if info.st_uid not in (owner[0], 0):
                raise UnsafeHome(f"{home} belongs to user id {info.st_uid}, not to {AI_USER}")
            os.fchown(home_fd, *owner)
        for relative in SAFE_FILES:
            folder_fd, name = _walk(home_fd, relative, owner, home)
            try:
                _remove(folder_fd, name)
                data = (skel / relative).read_bytes()
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=folder_fd)
                try:
                    os.write(fd, data)
                    os.fchmod(fd, 0o644)
                    if owner is not None:
                        os.fchown(fd, *owner)
                finally:
                    os.close(fd)
            finally:
                os.close(folder_fd)
            written.append(f"{home}/{relative}")
        for relative, destination in SYMLINKS.items():
            folder_fd, name = _walk(home_fd, relative, owner, home)
            try:
                _remove(folder_fd, name)
                os.symlink(destination, name, dir_fd=folder_fd)
                if owner is not None:
                    os.chown(name, *owner, dir_fd=folder_fd, follow_symlinks=False)
            finally:
                os.close(folder_fd)
            written.append(f"{home}/{relative}")
        if owner is not None:
            for relative in (".local/bin/x",):  # the agents install themselves here
                folder_fd, _ = _walk(home_fd, relative, owner, home)
                os.close(folder_fd)
    finally:
        os.close(home_fd)
    return written


def drop_privileges(launch: Launch) -> list[str]:
    """The argv that runs ``launch`` as rescue-ai from a root process: setpriv resets the groups and the environment."""
    env = [f"{key}={value}" for key, value in launch.env.items()]
    return ["setpriv", f"--reuid={AI_USER}", f"--regid={AI_USER}", "--init-groups", "--no-new-privs", "--reset-env", "env", "-i", "-C", launch.cwd, *env, *launch.argv]


def tmux_session_argv(agent_window: Launch, shell_window: Launch) -> list[str]:
    """One tmux invocation: create (or attach to) the session with the agent in the window ``agent`` and a plain shell in the window ``shell``.
    Both windows start in the home directory. No other window exists: in particular none that takes approvals."""
    return ["tmux", "-u", "new-session", "-A", "-s", TMUX_SESSION, "-n", "agent", "-c", agent_window.cwd, *agent_window.argv,
            ";", "new-window", "-n", "shell", "-c", shell_window.cwd, "/bin/bash", "-l",
            ";", "select-window", "-t", f"{TMUX_SESSION}:agent"]  # by name: tmux numbers windows from 0 unless base-index is set


def session_launches(agent: str | None, home: str, term: str) -> tuple[Launch, Launch]:
    shell = Launch(["/bin/bash", "-l"], home, agent_env(home, term))
    return (agent_launch(agent, home, term) if agent in COMMANDS else shell), shell


# ---- the root part on tty1 ----------------------------------------------------------------------------------------------------------------------
class Prompts:
    def __init__(self, ask: Callable[[str], str] = input, secret: Callable[[str], str] = getpass.getpass, out: Any = None) -> None:
        self._ask, self._secret, self.out = ask, secret, out or sys.stdout

    def say(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def ask(self, prompt: str) -> str:
        try:
            return self._ask(prompt).strip()
        except EOFError:
            return ""

    def yes(self, prompt: str, default: bool = True) -> bool:
        answer = self.ask(prompt + (" [Y/n] " if default else " [y/N] ")).lower()
        return default if not answer else answer in {"y", "yes"}

    def secret(self, prompt: str) -> str:
        return self._secret(prompt)


NetCall = Callable[[str, dict[str, Any]], dict[str, Any]]


def daemon_call(action: str, payload: dict[str, Any], socket_path: str = DAEMON_SOCKET) -> dict[str, Any]:
    """One action of the installer daemon (network actions) without Qt; never raises. The answer holds ``ok`` and ``message``."""
    from gentoo_installer import daemon_client

    try:
        events = daemon_client.request(socket_path, action, payload, timeout=75.0 if action == "wifi_connect" else 12.0, max_seconds=120.0)
    except (OSError, TimeoutError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}
    error = daemon_client.first_error(events)
    if error is not None:
        return {"ok": False, "message": error[0]}
    for event in events:
        if event.get("type") == "result":
            data = dict(daemon_client.event_data(event))
            data.setdefault("ok", True)
            data.setdefault("message", str(event.get("message") or ""))
            return data
    return {"ok": False, "message": "The installer service gave no answer."}


def offer_network(prompts: Prompts, call: NetCall) -> bool:
    """When the stick is offline, offer Wi-Fi (the agents and their login need the network). Returns whether it ended up online."""
    status = call("network_status", {})
    if status.get("online"):
        return True
    prompts.say("\nThis PC is not online. The agents are cloud services and need the network.")
    if not status.get("wifi_present") and status.get("ok", True):
        prompts.say("There is no Wi-Fi adapter. Plug in a network cable, then run gentoo-rescue-ai again.")
        return False
    if not status.get("ok", True):
        prompts.say(f"(The network service said: {status.get('message', 'no answer')})")
        return False
    if not prompts.yes("Connect to a Wi-Fi network now?"):
        return False
    while True:
        networks = call("wifi_scan", {"rescan": True}).get("networks") or []
        if not networks:
            prompts.say("No Wi-Fi networks were found.")
            if not prompts.yes("Look again?"):
                return False
            continue
        for index, network in enumerate(networks, 1):
            prompts.say(f"  {index}) {network.get('ssid', '?'):<32} signal {network.get('signal', '?')}%  {network.get('security', '')}")
        choice = prompts.ask("Number of your network (Enter to stop, r to scan again): ")
        if not choice:
            return False
        if choice.lower() == "r":
            continue
        if not choice.isdecimal() or not 1 <= int(choice) <= len(networks):
            prompts.say("Type one of the numbers.")
            continue
        network = networks[int(choice) - 1]
        password = "" if network.get("security") in {"open", "owe"} else prompts.secret(f"Password for {network.get('ssid')} (hidden): ")
        prompts.say("Connecting ...")
        result = call("wifi_connect", {"ssid": network.get("ssid"), "password": password})
        if result.get("ok", True) and not result.get("reason"):
            prompts.say("Connected.")
            return True
        prompts.say(f"Could not connect: {result.get('message') or result.get('reason')}")
        if not prompts.yes("Try again?"):
            return False


def installed_agents(home: str) -> list[str]:
    return [name for name in AGENTS if installed_version(name, home) is not None]


def approver_vt(vt_file: str = checks.VT_FILE) -> int:
    """The console of the approver: the service writes it (tty9 unless that one was taken)."""
    try:
        return int(Path(vt_file).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return checks.DEFAULT_APPROVER_VT


def show_password_once(prompts: Prompts, password_file: str = DO_PASSWORD_FILE) -> None:
    """The per-boot password goes on tty1 only as root, before the tmux session exists; the screen is cleared after a key press."""
    try:
        password = Path(password_file).read_text(encoding="utf-8").strip()
    except OSError:
        prompts.say("\nThe approval service is not running, so the agents cannot ask for root commands (boot the 'AI rescue' entry).")
        return
    prompts.say("\n=== The rescue password (this boot only) ===")
    prompts.say(f"    {password}")
    vt = approver_vt()
    prompts.say(f"When an agent asks to run a root command, the approver switches this PC to its own screen (Alt+F{vt} shows it).")
    prompts.say("Type the password THERE, for that one command, after reading the command. Never type it into the agent, the phone or anywhere else.")
    prompts.ask("Press Enter when you have noted it; this screen is cleared. ")
    print(CLEAR_SCREEN, end="", file=prompts.out, flush=True)


def main(argv: Sequence[str] | None = None, *, prompts: Prompts | None = None, run: Callable[[Sequence[str]], int] | None = None, net: NetCall = daemon_call,
         home: str | None = None, password_file: str = DO_PASSWORD_FILE, skel: Path | None = None, require_root: bool = True,
         preflight: Callable[[], list[str]] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    prompts = prompts or Prompts()
    choice = arguments[0] if arguments else None
    if choice in {"-h", "--help"}:
        prompts.say(__doc__ or "")
        return 0
    if choice is not None and choice not in {*AGENTS, "shell"}:
        prompts.say("usage: gentoo-rescue-ai [claude|codex|opencode|shell]")
        return 2
    if require_root and os.geteuid() != 0:
        prompts.say("gentoo-rescue-ai starts as root (sudo gentoo-rescue-ai) so you can fix the network and unlock disks here; the agents then run as rescue-ai.")
        return 1
    if require_root:
        os.chdir("/")  # whatever folder this was started from, it is never the broken disk
    run = run or (lambda command: subprocess.run(list(command), check=False).returncode)
    owner: tuple[int, int] | None = None
    try:
        info = pwd.getpwnam(AI_USER)
        owner = (info.pw_uid, info.pw_gid)
    except KeyError:
        if require_root:
            prompts.say(f"The user {AI_USER} does not exist; this is not the AI rescue image.")
            return 1
    home = home or home_of()
    os.makedirs(home, exist_ok=True)
    try:
        prepare_home(home, skel, owner if os.geteuid() == 0 else None)
    except (UnsafeHome, OSError) as exc:
        prompts.say(f"\nThe home folder of {AI_USER} is not safe to prepare: {exc}\nThis looks like tampering by an agent. Reboot the stick before going on.")
        return 1
    problems = (preflight or checks.check)() if (require_root or preflight is not None) else []
    if problems:
        prompts.say("\nThis image does not meet the safety rules of the AI rescue stick:")
        for problem in problems:
            prompts.say(f"  - {problem}")
        if prompts.ask("Type yes to start anyway (the agents are then NOT kept away from root): ").lower() != "yes":
            return 1
    show_password_once(prompts, password_file)
    online = offer_network(prompts, net)
    if prompts.yes("\nOpen your Gentoo system now (gentoo-rescue-mount)? You type a disk passphrase here, nowhere else."):
        run(["gentoo-rescue-mount"])
    agents = installed_agents(home)
    agent = choice if choice in COMMANDS else None
    if choice is None and agents:
        agent = agents[0] if len(agents) == 1 else next((name for name in agents if prompts.yes(f"Start {name}?", default=name == agents[0])), agents[0])
    if agent and agent not in agents:
        if not online:
            prompts.say(f"{agent} is not installed and there is no network. Use a copy from a USB stick in the shell window: gentoo-rescue-agents install {agent} --from DIR")
            agent = None
        elif prompts.yes(f"{agent} is not installed. Download it now (checked against a pinned checksum, as {AI_USER})?"):
            home_launch = agent_launch(agent, home)
            run(drop_privileges(Launch(["gentoo-rescue-agents", "install", agent], home, home_launch.env)))
            if agent not in installed_agents(home):
                agent = None
        else:
            agent = None
    if agent is None:
        prompts.say("\nWindow 1 is a shell. Install an agent there: gentoo-rescue-agents install claude|codex|opencode, then log in: gentoo-rescue-login")
    agent_window, shell_window = session_launches(agent, home, os.environ.get("TERM", "linux"))
    already = run(drop_privileges(Launch(["tmux", "has-session", "-t", TMUX_SESSION], home, agent_window.env))) == 0
    prompts.say("\nStarting the session as rescue-ai. Window 'agent': " + (agent or "a shell") + ", window 'shell': a plain shell. Ctrl-b n switches, Ctrl-b w lists, Ctrl-b d detaches.\n")
    command = ["tmux", "-u", "attach", "-t", TMUX_SESSION] if already else tmux_session_argv(agent_window, shell_window)
    if already:
        prompts.say("The rescue session was already running; joining it.")
    return run(drop_privileges(Launch(command, home, agent_window.env)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
