"""VM checks of Wave 6, the AI rescue stick: the boot entry and its tmux session, the security rules around ``rescue-ai``,
``gentoo-rescue-do`` with the approver on its own console, ``gentoo-rescue-mount``, ``gentoo-rescue-share`` over TLS and the agent installer.

Same shape as vm_wave5.py: every guest command is a string, every verdict a pure function of recorded output, and every flow takes its
side effects as callables (``Hands``), so each scenario is tested against a scripted guest and no VM. ``scripts/test-vm.py`` owns the order.

Who is who. The harness plays two parts: the *machine owner at the keyboard* (it types on the guest's console through QEMU, which is
what the person does in front of the PC; it may read the per-boot password as root, because the person reads it off the screen), and
the *agents* (commands run as ``rescue-ai`` the way the launcher starts them: ``setpriv`` with a clean environment and the home
directory as working directory). It never gives ``rescue-ai`` anything the real stick does not give it.

The strings that the parsers read are the ones the tools print (src/gentoo_rescue_ai/*.py, Wave 6 "ai" owner). A parser that cannot
read a line says "could not tell" and fails; it never passes on a format it does not know.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shlex
import tarfile
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import vm_qmp
from vm_probes import ANSI_RE, ProbeResult

AI_USER = "rescue-ai"
AI_HOME = "/home/rescue-ai"
SESSION = "rescue"
STATE_DIR = "/run/gentoo-rescue"
DO_SOCKET = f"{STATE_DIR}/do.sock"
DO_PASSWORD_FILE = f"{STATE_DIR}/do-password"
APPROVER_VT_FILE = f"{STATE_DIR}/approver-vt"
DO_AUDIT_LOG = "/var/log/gentoo-rescue/do.log"
TARGET_LINK = "/mnt/gentoo"
TARGET_PREFIXES = ("/mnt/gentoo", "/mnt/rescue")
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
# groups of the security design (point 4) plus the ones that would also hand over a console or the disks
FORBIDDEN_GROUPS = ("tty", "wheel", "disk", "video", "input", "audio", "plugdev", "usb", "kvm", "portage", "root", "sudo", "adm", "floppy", "cdrom")
DEFAULT_APPROVER_VT = 9


def _clean(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "")


def _sections(text: str) -> list[str]:
    """The parts of an output that the check separated with a line holding only ``---``."""
    return re.split(r"(?m)^---[ \t]*$\n?", _clean(text))


def _tail(text: str, size: int = 300) -> str:
    return _clean(text).strip()[-size:]


# --- running things as rescue-ai ----------------------------------------------------------------------------------------------------------------

def as_ai(command: str, home: str = AI_HOME) -> str:
    """``command`` (a shell line) run as rescue-ai the way the launcher starts the agents: groups reset, clean environment, home as cwd."""
    return shlex.join(["setpriv", f"--reuid={AI_USER}", f"--regid={AI_USER}", "--init-groups", "--reset-env", "env", "-i", "-C", home,
                       f"HOME={home}", f"USER={AI_USER}", f"LOGNAME={AI_USER}", f"PATH={home}/.local/bin:{SAFE_PATH}", "TERM=linux", "LANG=C.UTF-8",
                       "sh", "-c", command])


def in_background(command: str) -> str:
    """Start a prepared root command line in the background and return at once (the ssh session must not wait for it)."""
    return f"nohup {command} >/dev/null 2>&1 </dev/null & echo started"


@dataclass
class Report:
    """The steps of one flow. Secrets never reach it: the flows pass details through ``Hands.hide``."""
    steps: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return not self.problems

    def step(self, name: str, ok: bool, detail: str = "") -> bool:
        self.steps.append({"step": name, "ok": bool(ok), **({"detail": detail} if detail else {})})
        if not ok:
            self.problems.append(f"{name}: {detail}" if detail else name)
        return bool(ok)

    def to_dict(self) -> dict[str, Any]:
        return {"success": self.success, "steps": self.steps, "problems": self.problems, **({"data": self.data} if self.data else {})}


@dataclass
class Hands:
    """What a flow may do in the guest. ``run(command, label)`` runs a command as root over SSH and returns (exit status, output)."""
    run: Callable[[str, str], tuple[int, str]]
    type_console: Callable[[str], None]            # the person types a line on the PC's keyboard (Enter is added)
    password: Callable[[], str]                     # the per-boot password, as the person reads it off the screen
    sleep: Callable[[float], None]
    now: Callable[[], float]
    home: str = AI_HOME
    secrets: list[str] = field(default_factory=list)
    relog: Callable[[str], None] | None = None     # rewrites the logs of one label once a secret became known (the share password)

    def rehide(self, label: str) -> None:
        """A secret that is only known after a command ran (the share password) must also leave that command's own log."""
        if self.relog is not None:
            self.relog(label)

    def ai(self, command: str, label: str = "") -> tuple[int, str]:
        return self.run(as_ai(command, self.home), label)

    def hide(self, text: str) -> str:
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "[hidden]")
        return text

    def wait_for(self, check: Callable[[], bool], timeout: float, interval: float = 1.0) -> bool:
        deadline = self.now() + timeout
        while True:
            if check():
                return True
            if self.now() >= deadline:
                return False
            self.sleep(interval)


# --- the boot entry: the launcher on tty1, then the tmux session --------------------------------------------------------------------------------

HAS_SESSION_COMMAND = f"tmux has-session -t {SESSION}"
# gentoo-rescue-ai asks on tty1 before it opens the session: Enter after the password it shows, then "n" to "open your Gentoo system now?".
LAUNCHER_ANSWERS = ("", "n")


def drive_launcher(hands: Hands, *, timeout: float = 120.0, settle: float = 6.0) -> tuple[bool, list[str]]:
    """Answer the launcher's questions on tty1 the way a person would, until the tmux session of rescue-ai exists.

    Returns (session exists, what was typed). One answer at a time, and after each the session is looked for: a keystroke that arrives after the
    session is up would land in the agent window, so the loop stops typing the moment it exists. Typed-ahead input waits in the tty's queue, so
    it does not matter that the harness cannot see the questions.
    """
    typed: list[str] = []

    def up() -> bool:
        code, _text = hands.ai(HAS_SESSION_COMMAND, "ai-session-wait")
        return code == 0

    deadline = hands.now() + timeout
    answers = list(LAUNCHER_ANSWERS)
    while hands.now() < deadline:
        if up():
            return True, typed
        if answers:
            answer = answers.pop(0)
            hands.type_console(answer)
            typed.append(answer or "<Enter>")
        hands.sleep(settle)
    return up(), typed


@dataclass(frozen=True)
class Window:
    index: int
    name: str
    tty: str
    path: str


SESSION_WINDOWS = "tmux list-windows -t {session} -F '#{{window_index}} #{{window_name}} #{{pane_tty}} #{{pane_current_path}}'"
SESSION_CLIENTS = "tmux list-clients -t {session} -F '#{{client_tty}} #{{client_pid}}'"
# The terminals of a client's ancestors: sudo (use_pty, the default of sudo 1.9.14 and later) runs the launcher on a pty of its
# own and relays it to tty1, so the console's client sits on /dev/pts/N with a root sudo on tty1 above it (the first real run).
CLIENT_CHAIN = ('while read -r tty pid; do chain=""; c="$pid"; while [ -n "$c" ] && [ "$c" -gt 1 ] 2>/dev/null; do '
                'chain="$chain $(ps -o tty= -p "$c" | tr -d " ")"; c="$(ps -o ppid= -p "$c" | tr -d " ")"; done; echo "$tty$chain"; done')
# By program name (comm), so the shell that runs this check (its command line holds "tmux list-clients") is not counted. tmux
# renames its processes ("tmux: server", "tmux: client"), so ps -C tmux finds none: the name must start with "tmux".
# user:32: ps cuts longer names to 8 characters ("rescue-+") by default.
TMUX_PS_COMMAND = "ps -eo user:32=,comm=,args= | awk '$2 ~ /^tmux/'"


def session_command(home: str = AI_HOME) -> str:
    """Windows, clients and every tmux process, in three sections separated by ``---``."""
    return (f"{as_ai(SESSION_WINDOWS.format(session=SESSION), home)}; echo ---; {as_ai(SESSION_CLIENTS.format(session=SESSION), home)} | {CLIENT_CHAIN}; "
            f"echo ---; {TMUX_PS_COMMAND}")


def parse_session_probe(returncode: int, output: str, home: str = AI_HOME) -> ProbeResult:
    """The session of the launcher: window 1 the agent (or a shell), window 2 a shell, both on a pty (never a VT) in the home directory,
    the console attached as a client on tty1, every tmux process owned by rescue-ai, and NO window that takes approvals."""
    clean = _clean(output)
    parts = _sections(clean)
    if len(parts) != 3:
        return ProbeResult("ai-session", False, _tail(clean), "the check returned no three sections: could not tell")
    windows_text, clients_text, ps_text = parts
    windows: list[Window] = []
    for line in windows_text.strip().splitlines():
        fields = line.split(None, 3)
        if len(fields) == 4 and fields[0].isdigit():
            windows.append(Window(int(fields[0]), fields[1], fields[2], fields[3].strip()))
    problems: list[str] = []
    if not windows:
        problems.append("tmux has no session 'rescue' for rescue-ai (the launcher did not start it)")
    names = sorted(window.name for window in windows)
    if windows and names != ["agent", "shell"]:
        problems.append(f"the windows are {names}, expected exactly ['agent', 'shell'] (an extra window would be an approval window)")
    for window in windows:
        if not window.tty.startswith("/dev/pts/"):
            problems.append(f"window {window.name} runs on {window.tty}, not on a pty: an agent must never have a VT as its terminal")
        if window.path != home:
            problems.append(f"window {window.name} starts in {window.path}, not in {home}")
    client_lines = [line.split() for line in clients_text.strip().splitlines() if line.strip()]
    clients = [fields[0] for fields in client_lines]
    on_console = any(fields[0] == "/dev/tty1" or "tty1" in fields[1:] for fields in client_lines)  # directly, or through sudo's pty
    if not on_console:
        problems.append(f"no tmux client is attached on /dev/tty1, directly or through sudo (clients: {', '.join(clients) or 'none'})")
    owners = sorted({line.split(None, 1)[0] for line in ps_text.strip().splitlines() if line.strip()})
    if not owners:
        problems.append("no tmux process was found")
    elif owners != [AI_USER]:
        problems.append(f"tmux processes belong to {', '.join(owners)}, expected only {AI_USER}")
    shown = f"windows={[w.name for w in windows]} clients={clients} owners={owners}"
    return ProbeResult("ai-session", not problems, shown, "; ".join(problems), data={"windows": [w.__dict__ for w in windows], "clients": clients})


# agents must start in the home directory and never below the target disk (point 6): looked at as the processes of rescue-ai really are
AI_CWDS_COMMAND = ("for d in /proc/[0-9]*; do if [ \"$(stat -c %U \"$d\" 2>/dev/null)\" = " + AI_USER + " ]; then echo \"${d#/proc/} $(readlink \"$d/cwd\")\"; fi; done; true")


def parse_cwd_probe(returncode: int, output: str) -> ProbeResult:
    rows = []
    for line in _clean(output).strip().splitlines():
        pid, _, cwd = line.partition(" ")
        if pid.isdigit():
            rows.append((int(pid), cwd.strip()))
    below = [f"{pid}:{cwd}" for pid, cwd in rows if any(cwd == prefix or cwd.startswith(prefix + "/") for prefix in TARGET_PREFIXES)]
    if not rows:
        return ProbeResult("ai-cwd", False, "", "no process of rescue-ai was found: could not tell where the agents run")
    return ProbeResult("ai-cwd", not below, f"{len(rows)} processes of rescue-ai, none below /mnt/gentoo or /mnt/rescue" if not below else ", ".join(below),
                       f"processes of rescue-ai run inside the target disk: {', '.join(below)}" if below else "")


# --- the rules of the security design (point 4 and the approver) ---------------------------------------------------------------------------------

APPROVER_COMMAND = (f"pgrep -af '[g]entoo-rescue-approver'; echo ---; stat -c '%U:%G %a %n' {DO_SOCKET} {DO_PASSWORD_FILE} 2>&1; echo ---; "
                    f"cat {APPROVER_VT_FILE} 2>/dev/null; true")
VT_OPTION_RE = re.compile(r"--vt[ =](\d+)")


def approver_vt(output: str) -> int:
    """The VT the approver shows its question on: from its command line, else the file the launcher reads, else the default."""
    match = VT_OPTION_RE.search(_clean(output))
    if match:
        return int(match.group(1))
    parts = _sections(output)
    if len(parts) == 3 and parts[2].strip().isdigit():
        return int(parts[2].strip())
    return DEFAULT_APPROVER_VT


def approver_processes(output: str) -> list[str]:
    """The lines of pgrep that are the approver itself: not the remote shell whose command line holds the same words."""
    parts = _sections(output)
    lines = _clean(parts[0] if parts else output).splitlines()
    return [line for line in lines if "gentoo-rescue-approver" in line and not re.search(r"\b(?:bash|sh|pgrep)\b.*gentoo-rescue-approver", line)]


def parse_approver_probe(returncode: int, output: str) -> ProbeResult:
    parts = _sections(output)
    if len(parts) != 3:
        return ProbeResult("approver", False, _tail(output), "the check returned no three sections: could not tell")
    processes, stats, _vt = parts
    problems: list[str] = []
    if not approver_processes(processes):
        problems.append("the approver service is not running (it must be started at boot in the AI rescue entry)")
    modes = {line.split()[-1]: line.split()[0] + " " + line.split()[1] for line in stats.strip().splitlines() if len(line.split()) == 3}
    if modes.get(DO_SOCKET) != "root:rescue-ai 660":
        problems.append(f"{DO_SOCKET} is {modes.get(DO_SOCKET, 'missing')!r}, expected 'root:rescue-ai 660'")
    if modes.get(DO_PASSWORD_FILE) != "root:root 600":
        problems.append(f"{DO_PASSWORD_FILE} is {modes.get(DO_PASSWORD_FILE, 'missing')!r}, expected 'root:root 600'")
    return ProbeResult("approver", not problems, f"vt={approver_vt(output)} " + " ".join(f"{path}={mode}" for path, mode in modes.items()), "; ".join(problems))


YAMA_COMMAND = "cat /sys/kernel/security/lsm; echo; echo ---; cat /proc/sys/kernel/yama/ptrace_scope 2>&1"


def parse_yama_probe(returncode: int, output: str) -> ProbeResult:
    lsm, _, scope = (_sections(output) + ["", ""])[:3][0], None, (_sections(output) + ["", ""])[1]
    problems: list[str] = []
    if "yama" not in lsm.strip().split(","):
        problems.append(f"Yama is not in the kernel's LSM list ({lsm.strip() or 'empty'})")
    value = scope.strip().splitlines()[0] if scope.strip() else ""
    if not value.isdigit() or int(value) < 1:
        problems.append(f"kernel.yama.ptrace_scope is {value or 'missing'!r}, expected 1 or more")
    return ProbeResult("yama", not problems, f"lsm={lsm.strip()} ptrace_scope={value}", "; ".join(problems))


IDENTITY_COMMAND = (f"id -nG {AI_USER}; echo ---; sudo -n -l -U {AI_USER} 2>&1; echo ---; grep -rsl {AI_USER} /etc/sudoers /etc/sudoers.d; echo ---; "
                    f"getent passwd {AI_USER}")


def parse_identity_probe(returncode: int, output: str) -> ProbeResult:
    parts = _sections(output)
    if len(parts) != 4:
        return ProbeResult("ai-identity", False, _tail(output), "the check returned no four sections: could not tell")
    groups, sudo, rules, passwd = parts
    problems: list[str] = []
    member = sorted(set(groups.split()) & set(FORBIDDEN_GROUPS))
    if not groups.strip():
        problems.append(f"the user {AI_USER} does not exist")
    if member:
        problems.append(f"{AI_USER} is in the group(s) {', '.join(member)}")
    text = sudo.lower()
    if "may run the following" in text or ("(all" in text and "nopasswd" in text):
        problems.append("sudo lets rescue-ai run commands: it must have no sudo rule at all")
    if rules.strip():
        problems.append(f"{AI_USER} is named in {rules.strip()}")
    fields = passwd.strip().split(":")
    if len(fields) >= 6 and fields[1] not in ("", "x", "*", "!"):
        problems.append("rescue-ai has a password hash in /etc/passwd")
    return ProbeResult("ai-identity", not problems, f"groups={groups.strip()}", "; ".join(problems))


def home_of(passwd_output: str) -> str:
    """The home directory in ``getent passwd rescue-ai`` (the default when there is none)."""
    for line in _clean(passwd_output).splitlines():
        fields = line.strip().split(":")
        if len(fields) >= 6 and fields[0] == AI_USER and fields[5].startswith("/"):
            return fields[5]
    return AI_HOME


# What an agent can do about the consoles. Run AS rescue-ai: open the VTs, switch the screen (VT_ACTIVATE needs an open VT), push a
# keystroke into one (TIOCSTI needs an open terminal), and read the secrets. Prints one JSON line.
CONSOLE_SNIPPET = r"""
import fcntl, json, os, subprocess, sys
vt = sys.argv[1]
result = {"open": {}, "chvt": "", "tiocsti": "", "secrets": {}}
for device in ("/dev/tty1", "/dev/tty" + vt, "/dev/tty0", "/dev/console"):
    try:
        fd = os.open(device, os.O_RDWR | os.O_NOCTTY)
    except FileNotFoundError:
        result["open"][device] = "missing"
    except OSError as exc:
        result["open"][device] = "refused: " + exc.strerror
    else:
        result["open"][device] = "OPENED"
        try:
            fcntl.ioctl(fd, 0x5412, b"x")      # TIOCSTI
            result["tiocsti"] = "INJECTED"
        except OSError as exc:
            result["tiocsti"] = "refused: " + exc.strerror
        os.close(fd)
try:
    done = subprocess.run(["chvt", vt], capture_output=True, text=True, timeout=10)
    result["chvt"] = "SWITCHED" if done.returncode == 0 else "refused (exit %d)" % done.returncode
except (OSError, subprocess.SubprocessError) as exc:
    result["chvt"] = "refused: " + str(exc)[:60]
for path in ("/run/gentoo-rescue/do-password", "/var/log/gentoo-rescue/do.log"):
    try:
        open(path).read(1)
        result["secrets"][path] = "READABLE"
    except OSError as exc:
        result["secrets"][path] = "refused: " + exc.strerror
try:
    result["sudo"] = "RAN" if subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=10).returncode == 0 else "refused"
except OSError:
    result["sudo"] = "refused"
try:
    result["active"] = open("/sys/class/tty/tty0/active").read().strip()
except OSError:
    result["active"] = ""
print(json.dumps(result))
"""


def console_command(vt: int) -> str:
    return shlex.join(["python3", "-c", CONSOLE_SNIPPET, str(vt)])


def parse_console_probe(returncode: int, output: str, vt: int, before: str = "tty1") -> ProbeResult:
    """From inside the agent's account: tty1 and the approver's VT cannot be opened, ``chvt`` and TIOCSTI fail, the password and the audit log
    cannot be read, sudo does not run. A device that does not exist cannot be judged, so it fails the check (it would pass for the wrong reason)."""
    report: dict[str, Any] = {}
    for line in reversed(_clean(output).strip().splitlines()):
        try:
            report = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if not report:
        return ProbeResult("ai-consoles", False, _tail(output), "the check printed no report: could not tell")
    problems: list[str] = []
    for device, state in report.get("open", {}).items():
        if state == "OPENED":
            problems.append(f"rescue-ai can open {device}")
        elif state == "missing" and device in ("/dev/tty1", f"/dev/tty{vt}"):
            problems.append(f"{device} does not exist, so the check proves nothing")
    if report.get("chvt") != "" and report.get("chvt", "").startswith("SWITCHED"):
        problems.append(f"rescue-ai can switch the console (chvt {vt})")
    if report.get("tiocsti") == "INJECTED":
        problems.append("rescue-ai can inject keystrokes into a console (TIOCSTI)")
    for path, state in report.get("secrets", {}).items():
        if state == "READABLE":
            problems.append(f"rescue-ai can read {path}")
    if report.get("sudo") == "RAN":
        problems.append("rescue-ai can run sudo")
    if report.get("active") and report.get("active") != before:
        problems.append(f"the active console changed from {before} to {report['active']} during the check")
    return ProbeResult("ai-consoles", not problems, json.dumps(report, sort_keys=True), "; ".join(problems))


def security_probes(vt: int, home: str = AI_HOME) -> list[tuple[str, str, Callable[[int, str], ProbeResult], bool]]:
    """(name, command, parser, as rescue-ai) for the rules that need no person at the keyboard."""
    return [
        ("approver", APPROVER_COMMAND, parse_approver_probe, False),
        ("yama", YAMA_COMMAND, parse_yama_probe, False),
        ("ai-identity", IDENTITY_COMMAND, parse_identity_probe, False),
        ("ai-consoles", console_command(vt), lambda code, text: parse_console_probe(code, text, vt), True),
        ("ai-session", session_command(home), lambda code, text: parse_session_probe(code, text, home), False),
        ("ai-cwd", AI_CWDS_COMMAND, parse_cwd_probe, False),
    ]


# --- gentoo-rescue-do: one command, one typed approval --------------------------------------------------------------------------------------

ACTIVE_VT_COMMAND = "cat /sys/class/tty/tty0/active"
DO_COMMAND = ("id", "-u")
EXIT_REFUSED = 77


def do_paths(number: int) -> tuple[str, str]:
    return f"/tmp/gentoo-vm-do-{number}.out", f"/tmp/gentoo-vm-do-{number}.rc"


def do_start_command(number: int, home: str = AI_HOME) -> str:
    """As root: clear the old files, then start ``gentoo-rescue-do id -u`` as rescue-ai in the background. Its exit status lands in a file."""
    out, rc = do_paths(number)
    script = f"gentoo-rescue-do {shlex.join(DO_COMMAND)} >{out} 2>&1; echo $? >{rc}.tmp; mv {rc}.tmp {rc}"
    return f"rm -f {out} {rc} {rc}.tmp; " + in_background(as_ai(script, home))


def read_status(output: str) -> int | None:
    text = _clean(output).strip()
    return int(text) if text.isdigit() else None


def audit_events(text: str) -> list[dict[str, Any]]:
    events = []
    for line in _clean(text).splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and "event" in item:
            events.append(item)
    return events


def audit_problems(events: Sequence[Mapping[str, Any]], answers: Sequence[str], password: str, raw: str) -> list[str]:
    """The requests of the flow are in the audit log with their argv, each answer and the one exit status, and the password is not."""
    problems: list[str] = []
    requests = [event for event in events if event.get("event") == "request" and list(event.get("argv", []))[1:] == list(DO_COMMAND)[1:]
                and str(event.get("argv", [""])[0]).endswith("/" + DO_COMMAND[0])]
    if len(requests) != len(answers):
        problems.append(f"the audit log has {len(requests)} requests for {' '.join(DO_COMMAND)}, expected {len(answers)}")
    got = [event.get("answer") for event in events if event.get("event") == "answer"]
    if got != list(answers):
        problems.append(f"the audit log answers are {got}, expected {list(answers)}")
    exits = [event.get("status") for event in events if event.get("event") == "exit"]
    if exits != [0]:
        problems.append(f"the audit log exit statuses are {exits}, expected [0] (only the approved command ran)")
    if password and password in raw:
        problems.append("the per-boot password is in the audit log")
    if any(not isinstance(event.get("cwd", ""), str) for event in requests):
        problems.append("a request has no folder")
    return problems


def do_flow(hands: Hands, vt: int) -> Report:
    """Two requests, each asked on the approver's console only. The password approves exactly that one command; the next request asks again
    and bare Enter refuses it (77): an approval is never a window of time. The approval comes first on purpose: the approver keeps quiet for
    a few seconds after any request that was not approved (BACKOFF_START), so a refusal first would make the next request be turned away
    before it reaches the console. The audit log tells the same story afterwards."""
    report = Report()
    password = hands.password()
    hands.secrets.append(password)
    bad = vm_qmp.untypeable(password)
    if not report.step("the per-boot password can be typed on the guest's keyboard", not bad and bool(password),
                       f"it contains {''.join(bad)!r}, which QEMU key events here cannot type (the password should use letters, digits and -)" if bad else
                       "" if password else f"{DO_PASSWORD_FILE} is empty or unreadable"):
        return report
    code, text = hands.run(ACTIVE_VT_COMMAND, "do-vt-before")
    before = _clean(text).strip() or "tty1"
    target_vt = f"tty{vt}"
    report.step("the console shows tty1 before the first request", before == "tty1", f"it shows {before}" if before != "tty1" else "")

    def one(number: int, answer: str | None, expect: str) -> bool:
        out, rc = do_paths(number)
        label = f"request {number}"
        code, text = hands.run(do_start_command(number, hands.home), f"do-start-{number}")
        if code != 0 or "started" not in text:
            return report.step(f"{label} starts", False, _tail(text))
        switched = hands.wait_for(lambda: _clean(hands.run(ACTIVE_VT_COMMAND, f"do-vt-{number}")[1]).strip() == target_vt, 30)
        if not report.step(f"{label}: the approver takes over the console ({target_vt})", switched, "the console never left " + before if not switched else ""):
            return False
        _code, early = hands.run(f"test -e {rc} && echo RC-EXISTS; grep -qx 0 {out} 2>/dev/null && echo RAN; true", f"do-early-{number}")
        report.step(f"{label}: nothing ran before the approval", "RC-EXISTS" not in early and "RAN" not in early, _tail(early))
        hands.sleep(2)  # the approver clears what was typed before its question (TCSAFLUSH): type only after it is on screen
        hands.type_console(password if answer is None else answer)
        finished = hands.wait_for(lambda: read_status(hands.run(f"cat {rc} 2>/dev/null", f"do-rc-{number}")[1]) is not None, 40)
        status = read_status(hands.run(f"cat {rc} 2>/dev/null", f"do-rc-{number}")[1]) if finished else None
        _code, output = hands.run(f"cat {out} 2>/dev/null", f"do-out-{number}")
        lines = [line.strip() for line in _clean(output).splitlines()]
        if expect == "approved":
            ok = status == 0 and "0" in lines
            report.step(f"{label}: the password approves that one command", ok, f"exit {status}, output {_tail(output)!r}" if not ok else "")
        else:
            ok = status == EXIT_REFUSED and "0" not in lines
            report.step(f"{label}: bare Enter refuses it (exit {EXIT_REFUSED}, nothing runs)", ok, f"exit {status}, output {_tail(output)!r}" if not ok else "")
        back = hands.wait_for(lambda: _clean(hands.run(ACTIVE_VT_COMMAND, f"do-vt-back-{number}")[1]).strip() == before, 20)
        report.step(f"{label}: the console goes back to {before}", back, "it stayed on the approver's screen" if not back else "")
        return ok

    one(1, None, "approved")
    one(2, "", "refused")
    code, raw = hands.run(f"cat {DO_AUDIT_LOG}", "do-audit")
    events = audit_events(raw)
    problems = audit_problems(events, ["approved", "wrong"], password, raw) if code == 0 else [f"the audit log could not be read (exit {code})"]
    report.step("the audit log records the two requests, their answers and the one exit status, never the password", not problems, "; ".join(problems))
    return report


# --- gentoo-rescue-mount ---------------------------------------------------------------------------------------------------------------------

MOUNT_COMMAND = "gentoo-rescue-mount </dev/null"
UNMOUNT_COMMAND = "gentoo-rescue-mount --unmount </dev/null"
OPEN_RE = re.compile(r"open at (\S+) \((read-only|read-write)\), session (\w+)")
MOUNT_PROOF_COMMAND = (f"readlink {TARGET_LINK}; echo ---; findmnt -no OPTIONS -T {TARGET_LINK}/; echo ---; cat {TARGET_LINK}/etc/gentoo-release; echo ---; "
                       f"touch {TARGET_LINK}/.gentoo-vm-ro-probe 2>&1; echo rc=$?; test -e {TARGET_LINK}/.gentoo-vm-ro-probe && echo LEFT")


def mount_problems(returncode: int, output: str, proof: str) -> list[str]:
    """The tool said where the system is and how to get a shell and out again; the files show a read-only Gentoo behind /mnt/gentoo."""
    text = _clean(output)
    problems: list[str] = []
    if returncode != 0:
        problems.append(f"gentoo-rescue-mount ended with status {returncode}")
    match = OPEN_RE.search(text)
    if match is None:
        problems.append("the output does not say 'open at /mnt/gentoo (read-only), session ID' (could not tell)")
    else:
        if match.group(1) != TARGET_LINK:
            problems.append(f"the system is announced at {match.group(1)}, not {TARGET_LINK}")
        if match.group(2) != "read-only":
            problems.append("the system was opened read-write: it must stay read-only until a repair plan is confirmed")
    if "gentoo-chroot" not in text:
        problems.append("the output does not say how to open a shell (gentoo-chroot)")
    if "--unmount" not in text:
        problems.append("the output does not say how to unmount (gentoo-rescue-mount --unmount)")
    parts = _sections(proof)
    if len(parts) != 4:
        return problems + ["the proof returned no four sections: could not tell"]
    link, options, release, writing = (part.strip() for part in parts)
    if not link.startswith("/mnt/rescue/"):
        problems.append(f"{TARGET_LINK} points to {link or 'nothing'}, expected a session folder below /mnt/rescue")
    if "ro" not in options.split(","):
        problems.append(f"the mount options are {options!r}: not read-only")
    if "gentoo" not in release.lower():
        problems.append("/mnt/gentoo/etc/gentoo-release is missing or does not name Gentoo")
    rc = re.search(r"rc=(\d+)", writing)
    if rc is None or rc.group(1) == "0" or "LEFT" in writing:
        problems.append("a file could be created on the read-only system")
    return problems


def mount_flow(hands: Hands) -> Report:
    """gentoo-rescue-mount opens the installed Gentoo read-only at /mnt/gentoo; the agents still run in their home; --unmount releases it."""
    report = Report()
    code, output = hands.run(MOUNT_COMMAND, "mount")
    _code, proof = hands.run(MOUNT_PROOF_COMMAND, "mount-proof")
    problems = mount_problems(code, output, proof)
    report.step("gentoo-rescue-mount opens the installed system read-only at /mnt/gentoo", not problems, "; ".join(problems))
    _code, cwds = hands.run(AI_CWDS_COMMAND, "mount-cwd")
    probe = parse_cwd_probe(0, cwds)
    report.step("with the disk mounted, no process of rescue-ai works inside it", probe.success, probe.error)
    code, output = hands.run(UNMOUNT_COMMAND, "unmount")
    _code, left = hands.run(f"if [ -e {TARGET_LINK} ] || [ -L {TARGET_LINK} ]; then echo LEFT; fi; grep -c /mnt/rescue /proc/mounts; true", "unmount-proof")
    report.step("--unmount releases it and removes /mnt/gentoo", code == 0 and "LEFT" not in left, f"exit {code}: {_tail(output)}" if code != 0 or "LEFT" in left else "")
    return report


# --- gentoo-rescue-share: the session in a phone's browser -----------------------------------------------------------------------------------

SHARE_START = "gentoo-rescue-share --minutes 5"
SHARE_READ_ONLY = "gentoo-rescue-share --read-only --minutes 5"
SHARE_PS_COMMAND = "ps -C ttyd -o user:32=,args="  # by program name, with the whole user name (see TMUX_PS_COMMAND)
ADDRESS_RE = re.compile(r"Address:\s+https://([\w.:-]+):(\d+)/")
CREDENTIALS_RE = re.compile(r"(?m)^\W*User/password:\s*(\S+)\s*/\s*(\S+)\s*$")
FINGERPRINT_LINE_RE = re.compile(r"Certificate \(SHA-256\):\s*(\S+)")


@dataclass(frozen=True)
class ShareInfo:
    address: str = ""
    port: int = 0
    user: str = ""
    password: str = ""
    fingerprint: str = ""


def normalize_fingerprint(text: str) -> str:
    """A SHA-256 fingerprint as 64 lowercase hex digits, from openssl's ``AA:BB:...``, ``SHA256:`` plus base64 (OpenSSH) or plain hex. '' when unreadable."""
    value = text.strip()
    value = re.sub(r"(?i)^(?:sha-?256)(?:\s+fingerprint)?\s*[=:]\s*", "", value)
    hexish = value.replace(":", "").replace(" ", "")
    if re.fullmatch(r"[0-9a-fA-F]{64}", hexish):
        return hexish.lower()
    if re.fullmatch(r"[A-Za-z0-9+/]{43}=?", value):
        try:
            digest = base64.b64decode(value + "=" * (-len(value) % 4)).hex()
        except ValueError:
            return ""
        return digest if len(digest) == 64 else ""
    return ""


def parse_share_start(output: str) -> ShareInfo:
    text = _clean(output)
    address = ADDRESS_RE.search(text)
    credentials = CREDENTIALS_RE.search(text)
    fingerprint = FINGERPRINT_LINE_RE.search(text)
    return ShareInfo(address.group(1) if address else "", int(address.group(2)) if address else 0, credentials.group(1) if credentials else "",
                     credentials.group(2) if credentials else "", normalize_fingerprint(fingerprint.group(1)) if fingerprint else "")


@dataclass(frozen=True)
class TtydProcess:
    user: str
    arguments: tuple[str, ...]

    def has(self, flag: str) -> bool:
        return flag in self.arguments

    def value(self, flag: str) -> str:
        try:
            return self.arguments[self.arguments.index(flag) + 1]
        except (ValueError, IndexError):
            return ""


def parse_ttyd_processes(output: str) -> list[TtydProcess]:
    found = []
    for line in _clean(output).strip().splitlines():
        user, _, args = line.strip().partition(" ")
        words = tuple(args.split())
        if words and words[0].rsplit("/", 1)[-1] == "ttyd":
            found.append(TtydProcess(user, words))
    return found


def ttyd_problems(processes: Sequence[TtydProcess], *, writable: bool) -> list[str]:
    """ttyd runs as rescue-ai (never root), with TLS, a password, ONE client, an origin check, on a named interface; ``-W`` only when the phone may type."""
    if len(processes) != 1:
        return [f"{len(processes)} ttyd processes are running, expected exactly one"]
    process = processes[0]
    problems = []
    if process.user != AI_USER:
        problems.append(f"ttyd runs as {process.user}, not {AI_USER}")
    for flag, meaning in (("-S", "TLS"), ("-O", "the origin check")):
        if not process.has(flag):
            problems.append(f"ttyd has no {flag} ({meaning})")
    if not process.value("-c").startswith("rescue:"):
        problems.append("ttyd has no -c rescue:PASSWORD (basic authentication)")
    if process.value("-m") != "1":
        problems.append(f"ttyd allows {process.value('-m') or 'any number of'} clients, expected -m 1")
    interface = process.value("-i")
    if not interface or interface in ("0.0.0.0", "::", "any"):
        problems.append(f"ttyd listens on {interface or 'every interface'}, not on the chosen LAN interface")
    if process.has("-W") != writable:
        problems.append("ttyd lets the phone type (-W) in a read-only share" if not writable else "ttyd has no -W although the phone should be able to type")
    return problems


@dataclass(frozen=True)
class Fetch:
    """The answer of the host's HTTPS client: the status line (None when the connection failed), the SHA-256 of the certificate it was shown, or an error."""
    status: int | None = None
    fingerprint: str = ""
    error: str = ""


def fetch_https(port: int, user: str | None, password: str | None, scheme: str = "https", *, host: str = "127.0.0.1", timeout: float = 10.0) -> Fetch:
    """GET / on the forwarded port the way a phone's browser would, certificate warning accepted. Returns the status and the SHA-256 of the DER
    certificate that the server showed (compared with the fingerprint the tool printed: that is the real security claim)."""
    import http.client
    import ssl

    headers = {}
    if user is not None:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{password or ''}".encode()).decode()
    connection: http.client.HTTPConnection | None = None
    try:
        if scheme == "https":
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
        else:
            connection = http.client.HTTPConnection(host, port, timeout=timeout)
        connection.request("GET", "/", headers=headers)
        fingerprint = ""
        if scheme == "https" and connection.sock is not None:
            der = connection.sock.getpeercert(binary_form=True)  # type: ignore[union-attr]
            fingerprint = hashlib.sha256(der).hexdigest() if der else ""
        response = connection.getresponse()
        response.read(1024)
        return Fetch(response.status, fingerprint)
    except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
        return Fetch(None, "", f"{type(exc).__name__}: {exc}")
    finally:
        if connection is not None:
            connection.close()


Forward = Callable[[int], int]                        # guest port -> port on the host's loopback
Fetcher = Callable[[int, str | None, str | None, str], Fetch]   # host port, user, password, scheme


def share_flow(hands: Hands, forward: Forward, fetch: Fetcher) -> Report:
    """The share from the host's side: TLS with the certificate that was printed, 401 without the password, one tool, stop closes it."""
    report = Report()

    def stopped() -> bool:
        return not parse_ttyd_processes(hands.run(SHARE_PS_COMMAND, "share-ps-stop")[1])

    def one_share(command: str, *, writable: bool) -> None:
        mode = "writable" if writable else "read-only"
        code, output = hands.ai(command, f"share-start-{mode}")
        info = parse_share_start(output)
        hands.secrets.append(info.password)
        hands.rehide(f"share-start-{mode}")
        ready = code == 0 and info.port > 0 and bool(info.password) and bool(info.fingerprint)
        if not report.step(f"{mode}: gentoo-rescue-share starts and prints address, password and fingerprint", ready,
                           f"exit {code}: {hands.hide(_tail(output))}" if not ready else ""):
            return
        text = _clean(output)
        report.step(f"{mode}: it says whether the phone can type", ("CAN TYPE" in text) if writable else ("VIEW ONLY" in text), "")
        report.step(f"{mode}: it says the phone can never approve a root command", bool(re.search(r"NEVER approve", text)), "")
        report.step(f"{mode}: the password is three words (letters and dashes only)", bool(re.fullmatch(r"[a-z]+(-[a-z]+){2}", info.password)), "")
        problems = ttyd_problems(parse_ttyd_processes(hands.run(SHARE_PS_COMMAND, f"share-ps-{mode}")[1]), writable=writable)
        report.step(f"{mode}: ttyd runs as rescue-ai with TLS, a password, one client, an origin check, on the LAN interface", not problems, "; ".join(problems))
        try:
            port = forward(info.port)
        except (OSError, RuntimeError) as exc:
            report.step(f"{mode}: the guest port can be reached from the host", False, str(exc))
            return
        good = fetch(port, info.user, info.password, "https")
        report.step(f"{mode}: the right password opens the page (200)", good.status == 200, f"status {good.status} {good.error}" if good.status != 200 else "")
        report.step(f"{mode}: the certificate that is served is the one whose fingerprint was printed", bool(good.fingerprint) and good.fingerprint == info.fingerprint,
                    f"served {good.fingerprint or '(none)'}, printed {info.fingerprint}" if good.fingerprint != info.fingerprint else "")
        none = fetch(port, None, None, "https")
        report.step(f"{mode}: without the password the page is refused (401)", none.status == 401, f"status {none.status} {none.error}" if none.status != 401 else "")
        wrong = fetch(port, info.user, info.password + "x", "https")
        report.step(f"{mode}: a wrong password is refused (401)", wrong.status == 401, f"status {wrong.status} {wrong.error}" if wrong.status != 401 else "")
        plain = fetch(port, info.user, info.password, "http")
        report.step(f"{mode}: plain HTTP on the TLS port gets no page", plain.status != 200, "plain HTTP returned 200: no TLS" if plain.status == 200 else "")
        if writable:
            code, status_text = hands.ai("gentoo-rescue-share status", "share-status")
            report.step("status shows the port and fingerprint but never the password", code == 0 and str(info.port) in status_text and info.password not in status_text,
                        hands.hide(_tail(status_text)) if code != 0 or info.password in status_text else "")
            code, again = hands.ai(SHARE_START, "share-again")
            hands.rehide("share-again")
            report.step("a second start while one runs is refused", code != 0 and "already running" in again.lower(), f"exit {code}: {_tail(again)}" if code == 0 else "")
        code, output = hands.ai("gentoo-rescue-share stop", f"share-stop-{mode}")
        report.step(f"{mode}: stop succeeds", code == 0, _tail(output) if code != 0 else "")
        gone = hands.wait_for(stopped, 15)
        report.step(f"{mode}: stop ends ttyd", gone, "ttyd still runs" if not gone else "")
        after = fetch(port, info.user, info.password, "https")
        report.step(f"{mode}: after stop nothing answers on the port", after.status is None, f"status {after.status}" if after.status is not None else "")
        code, status_text = hands.ai("gentoo-rescue-share status", f"share-status-{mode}-after")
        report.step(f"{mode}: status says no share is running", code != 0 and "no share" in status_text.lower(), _tail(status_text))
        _code, left = hands.ai("ls -A /run/gentoo-rescue-share ~/.cache/gentoo-rescue-share 2>/dev/null; true", f"share-left-{mode}")
        report.step(f"{mode}: the certificate, its key and the state are removed", not [name for name in _clean(left).split() if name.endswith(".pem") or name == "state.json"],
                    _tail(left))

    one_share(SHARE_START, writable=True)
    one_share(SHARE_READ_ONLY, writable=False)
    return report


# --- gentoo-rescue-agents: install on first use, checked against a pin --------------------------------------------------------------------------

AGENTS = ("claude", "codex", "opencode")
FAKE_VERSION = "0.0.1"
MIRROR_DIR = "/tmp/gentoo-vm-agent-mirror"
BAD_DIR = "/tmp/gentoo-vm-agent-bad"
TAMPERED_DIR = "/tmp/gentoo-vm-agent-tampered"
FAKE_PINS = "/tmp/gentoo-vm-agent-pins.json"
# gentoo-rescue-agents takes its pins file only as a Python argument (agents.main(pins_path=...)): the harness calls it that way for the fake pins
AGENTS_WITH_PINS = "import sys; from gentoo_rescue_ai import agents; raise SystemExit(agents.main(sys.argv[2:], pins_path=sys.argv[1]))"
REAL_PINS_SNIPPET = ("import json; from gentoo_rescue_ai import agents; "
                     "print(json.dumps({n: {'filename': p.filename, 'size': p.size, 'sha256': p.sha256} for n, p in agents.load_pins().items()}))")
UPLOAD_SNIPPET = r"""
import base64, json, os, pwd, sys
payload = json.load(sys.stdin)
user = pwd.getpwnam('rescue-ai')
for directory, files in payload['dirs'].items():
    os.makedirs(directory, exist_ok=True)
    for name, data in files.items():
        with open(os.path.join(directory, name), 'wb') as handle:
            handle.write(base64.b64decode(data))
    for name in os.listdir(directory):
        os.chown(os.path.join(directory, name), user.pw_uid, user.pw_gid)
    os.chown(directory, user.pw_uid, user.pw_gid)
    os.chmod(directory, 0o755)
with open(payload['pins_path'], 'w') as handle:
    json.dump(payload['pins'], handle)
os.chmod(payload['pins_path'], 0o644)
"""


def _tar_with(name: str, data: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def fake_agent_script(name: str) -> bytes:
    return f"#!/bin/sh\necho '{name}-fake {FAKE_VERSION}'\n".encode()


def fake_mirror() -> tuple[dict[str, bytes], dict[str, Any]]:
    """(files by name, pins) for three tiny stand-ins: one plain program and two tarballs, with their real sizes and checksums.

    The URLs are on an allowed host but are never contacted: the helper is run with ``--from DIR``, which takes the files from a folder.
    """
    files = {"claude-fake-linux-x64": fake_agent_script("claude"),
             "codex-fake-x86_64.tar.gz": _tar_with("codex", fake_agent_script("codex")),
             "opencode-fake-linux-x64.tar.gz": _tar_with("opencode", fake_agent_script("opencode"))}
    pins: dict[str, Any] = {"_comment": "harness stand-ins; never contacted"}
    for name, filename, kind in (("claude", "claude-fake-linux-x64", "binary"), ("codex", "codex-fake-x86_64.tar.gz", "tar.gz"),
                                 ("opencode", "opencode-fake-linux-x64.tar.gz", "tar.gz")):
        data = files[filename]
        pins[name] = {"version": FAKE_VERSION, "url": f"https://github.com/gentoo-vm/fake/releases/download/v{FAKE_VERSION}/{filename}", "kind": kind,
                      "binary": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    return files, pins


def tampered(data: bytes) -> bytes:
    """The same size, one byte different: only the checksum can tell."""
    return data[:-2] + bytes([data[-2] ^ 0xFF]) + data[-1:]


def upload_command() -> str:
    return shlex.join(["python3", "-c", UPLOAD_SNIPPET])


def upload_payload(dirs: Mapping[str, Mapping[str, bytes]], pins: Mapping[str, Any]) -> bytes:
    encoded = {directory: {name: base64.b64encode(data).decode() for name, data in files.items()} for directory, files in dirs.items()}
    return json.dumps({"dirs": encoded, "pins_path": FAKE_PINS, "pins": pins}).encode()


def agents_command(*arguments: str) -> str:
    """The helper with the fake pins, as rescue-ai."""
    return shlex.join(["python3", "-c", AGENTS_WITH_PINS, FAKE_PINS, *arguments])


def agent_install_proof_command() -> str:
    paths = " ".join(f"~/.local/bin/{name}" for name in AGENTS)
    return f"stat -c '%U %a %n' {paths} 2>&1; echo ---; ~/.local/bin/claude 2>&1; ~/.local/bin/codex 2>&1; ~/.local/bin/opencode 2>&1"


def agent_install_problems(proof: str) -> list[str]:
    """All three programs belong to rescue-ai, are executable, and are the verified ones (they print their own fake version)."""
    stats, ran = (_sections(proof) + ["", ""])[:2]
    problems = []
    rows = {line.split()[-1].rsplit("/", 1)[-1]: line.split()[:2] for line in stats.strip().splitlines() if len(line.split()) == 3}
    for name in AGENTS:
        if rows.get(name) != [AI_USER, "755"]:
            problems.append(f"{name} is {' '.join(rows[name]) if name in rows else 'missing'}, expected '{AI_USER} 755'")
        if f"{name}-fake {FAKE_VERSION}" not in ran:
            problems.append(f"{name} does not run as the verified file")
    return problems


def agents_flow(hands: Hands, upload: Callable[[bytes], tuple[int, str]]) -> Report:
    """Nothing is bundled; install refuses a file that is not the pinned one and installs a good one as rescue-ai.

    * the real pins: no install as root, and a wrong-sized file named like the pinned download is refused;
    * fake pins (the helper's own ``pins_path``): a file of the right size with one changed byte is refused on its checksum and leaves nothing
      behind, the good files install as rescue-ai, run, and ``remove`` takes them away again.
    """
    report = Report()
    code, text = hands.ai("gentoo-rescue-agents status", "agents-status-before")
    lines = _clean(text).splitlines()
    report.step("before the first use no agent is installed", code == 0 and all(any(name in line and "not installed" in line for line in lines) for name in AGENTS), _tail(text))
    code, text = hands.run("gentoo-rescue-agents install claude </dev/null", "agents-as-root")
    report.step("installing as root is refused", code != 0 and "root" in text.lower(), f"exit {code}: {_tail(text)}")
    # the real pins: names of the real downloads, junk of the wrong size
    code, text = hands.ai(f"python3 -c {shlex.quote(REAL_PINS_SNIPPET)}", "agents-real-pins")
    try:
        real = json.loads(_clean(text).strip().splitlines()[-1])
    except (ValueError, IndexError):
        real = {}
    if not report.step("the pinned downloads can be listed", code == 0 and set(AGENTS) <= set(real), _tail(text)):
        return report
    junk = b"this is not the agent\n"
    code, text = upload(upload_payload({BAD_DIR: {real[name]["filename"]: junk for name in AGENTS}}, {}))
    report.step("a folder with wrong files is prepared", code == 0, _tail(text))
    for name in AGENTS:
        code, text = hands.ai(f"gentoo-rescue-agents install {name} --from {BAD_DIR}", f"agents-bad-{name}")
        refused = code != 0 and "refused" in text.lower()
        _code, present = hands.ai(f"ls ~/.local/bin/{name} 2>&1; ls ~/.local/share/gentoo-rescue-agents 2>&1; true", f"agents-bad-{name}-left")
        left = f"{name}.json" in present or f"~/.local/bin/{name}" in present or re.search(rf"(?m)^/\S*/{name}$", present) is not None
        report.step(f"{name}: a wrong file is refused with the real pin, nothing is installed", refused and not left,
                    f"exit {code}, {_tail(text)}" if not refused else (f"left behind: {_tail(present)}" if left else ""))
    # fake pins
    files, pins = fake_mirror()
    first = files["claude-fake-linux-x64"]
    code, text = upload(upload_payload({MIRROR_DIR: files, TAMPERED_DIR: {"claude-fake-linux-x64": tampered(first)}}, pins))
    if not report.step("the fake mirror folder and its pins are in place", code == 0, _tail(text)):
        return report
    code, text = hands.ai(agents_command("install", "claude", "--from", TAMPERED_DIR), "agents-tampered")
    _code, present = hands.ai("ls ~/.local/bin 2>&1; true", "agents-tampered-left")
    report.step("a file of the right size with one byte changed is refused on its checksum, nothing is installed",
                code != 0 and "pinned checksum" in text and "refused" in text and "claude" not in present.split(), f"exit {code}: {_tail(text)} / {_tail(present)}")
    code, text = hands.ai(agents_command("install", "all", "--from", MIRROR_DIR), "agents-install")
    report.step("the good files install", code == 0 and _clean(text).count("Checksum is correct") == 3, f"exit {code}: {_tail(text, 500)}")
    _code, proof = hands.ai(agent_install_proof_command(), "agents-proof")
    problems = agent_install_problems(proof)
    report.step("they belong to rescue-ai, are executable and are the verified files", not problems, "; ".join(problems))
    code, text = hands.ai(agents_command("status"), "agents-status-after")
    report.step("status shows the installed version", code == 0 and _clean(text).count(f"installed {FAKE_VERSION}") == 3, _tail(text))
    code, text = hands.ai(agents_command("remove", "all"), "agents-remove")
    _code, present = hands.ai("ls ~/.local/bin 2>&1; true", "agents-remove-left")
    report.step("remove takes them away", code == 0 and not set(AGENTS) & set(_clean(present).split()), f"exit {code}: {_tail(present)}")
    hands.run(f"rm -rf {BAD_DIR} {MIRROR_DIR} {TAMPERED_DIR} {FAKE_PINS}", "agents-cleanup")
    return report
