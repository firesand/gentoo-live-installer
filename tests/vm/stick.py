"""A scripted AI rescue stick for the Wave 6 tests: a guest that answers the harness's commands the way the real tools do.

Every text here is what the tools print (src/gentoo_rescue_ai/*.py). The knobs switch one rule at a time to a broken behaviour, so each
check of the harness is shown to fail when the stick is wrong.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shlex
import tarfile
import io
from dataclasses import dataclass, field

import vm_wave6 as w6

PASSWORD = "maple-River-42"            # the per-boot password (the iso owner decides its alphabet; this one is typeable)
SHARE_PASSWORD = "bako-fidu-meza"
FINGERPRINT = "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89"
PRINTED_HEX = FINGERPRINT.replace(":", "").lower()
SHARE_PORT = 23456
BACKOFF_START = 5.0                    # seconds the real approver stays quiet after an unapproved request


class Clock:
    def __init__(self) -> None:
        self.time = 1000.0

    def now(self) -> float:
        return self.time

    def sleep(self, seconds: float) -> None:
        self.time += seconds


def mount_proof(**changes) -> str:
    values = {"link": "/mnt/rescue/r1a2b3", "options": "ro,relatime", "release": "Gentoo Base System release 2.18", "rc": 1}
    values.update(changes)
    return f"{values['link']}\n---\n{values['options']}\n---\n{values['release']}\n---\ntouch: cannot touch: Read-only file system\nrc={values['rc']}\n"


MOUNT_OUTPUT = """Systems found:
 1) /dev/vda2 Gentoo
Mounted at /mnt/rescue/r1a2b3 (read-only; session r1a2b3).

Your Gentoo system is open at /mnt/gentoo (read-only), session r1a2b3.
  look around:    ls /mnt/gentoo/etc
  a shell inside: gentoo-chroot r1a2b3      (the Handbook's chroot; read-only unless you add --writable)
  when finished:  gentoo-rescue-mount --unmount
"""


@dataclass
class Stick:
    """The behaviour of one booted AI rescue stick. The defaults are the correct stick."""
    clock: Clock = field(default_factory=Clock)
    # session
    session_after_keys: int = 2           # the tmux session appears after this many typed lines
    window_names: tuple[str, ...] = ("agent", "shell")
    pane_tty: str = "/dev/pts/3"
    pane_path: str = w6.AI_HOME
    client_tty: str = "/dev/tty1"
    tmux_owner: str = "rescue-ai"
    # security
    groups: str = "rescue-ai"
    sudo_text: str = "User rescue-ai is not allowed to run sudo on gentoo-vm.\n"
    yama: bool = True
    ptrace: int = 1
    console_open: bool = False
    secrets_readable: bool = False
    socket_mode: str = "root:rescue-ai 660"
    # do
    approver_vt: int = 9
    approver_running: bool = True
    password: str = PASSWORD
    approver_switches: bool = True
    runs_without_approval: bool = False
    carries_approval: bool = False        # a second request is run without asking again
    audit_has_password: bool = False
    # mount
    mount_ro: bool = True
    mount_rc: int = 1
    cwd_in_target: bool = False
    # share
    share_writable_flag: bool = True
    share_runs_as: str = "rescue-ai"
    share_stop_works: bool = True
    share_prints_password_in_status: bool = False
    share_ask_twice_ok: bool = False
    # agents
    refuses_bad: bool = True
    # state
    typed: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    active: str = "tty1"
    requests: dict[int, dict] = field(default_factory=dict)
    audit: list[dict] = field(default_factory=list)
    approved_before: bool = False
    quiet_until: float = 0.0              # the approver's backoff after a request that was not approved
    share_running: bool = False
    share_read_only: bool = False
    agents_installed: bool = False
    uploads: list[dict] = field(default_factory=list)
    bin_files: set = field(default_factory=set)
    mounted: bool = False

    # --- the person at the keyboard ------------------------------------------------------------------------------------------------------
    def password_file(self) -> str:
        return self.password

    def type_console(self, text: str) -> None:
        self.typed.append(text)
        pending = [number for number, item in sorted(self.requests.items()) if item["state"] == "waiting"]
        if self.active == f"tty{self.approver_vt}" and pending:
            number = pending[0]
            item = self.requests[number]
            ok = text == self.password
            self.audit.append({"event": "answer", "answer": "approved" if ok else "wrong"})
            if ok:
                self.quiet_until = 0.0
                self.approved_before = True
                self.audit.append({"event": "exit", "status": 0})
                item.update(state="done", rc=0, out="gentoo-rescue-do: waiting for the person at the PC's console\n0\n")
            else:
                self.quiet_until = self.clock.now() + BACKOFF_START
                item.update(state="done", rc=77, out="gentoo-rescue-do: waiting\ngentoo-rescue-do: not run: the password was wrong or empty: refused\n")
            self.active = "tty1"

    # --- the guest -----------------------------------------------------------------------------------------------------------------------
    def run(self, command: str, label: str = "") -> tuple[int, str]:
        self.log.append(command)
        for pattern, handler in self.rules():
            match = re.search(pattern, command, re.S)
            if match:
                result = handler(command, match)
                return result if isinstance(result, tuple) else (0, result)
        return 127, f"no rule for: {command[:200]}"

    def rules(self):
        return [
            (r"^cat /sys/class/tty/tty0/active$", lambda c, m: self.active + "\n"),
            (r"^cat /run/gentoo-rescue/do-password$", lambda c, m: self.password_file()),
            (r"^cat /var/log/gentoo-rescue/do\.log$", lambda c, m: "".join(json.dumps(e) + "\n" for e in self.audit_text())),
            (r"gentoo-rescue-do", self.do_start),
            (r"^rm -f /tmp/gentoo-vm-do-\d", self.do_start),
            (r"^test -e /tmp/gentoo-vm-do-(\d)\.rc", self.do_early),
            (r"^cat /tmp/gentoo-vm-do-(\d)\.rc", lambda c, m: self.do_file(int(m.group(1)), "rc")),
            (r"^cat /tmp/gentoo-vm-do-(\d)\.out", lambda c, m: self.do_file(int(m.group(1)), "out")),
            (r"tmux has-session", lambda c, m: (0, "") if self.session_up() else (1, "no server running\n")),
            (r"tmux list-windows", self.session_report),
            (r"^pgrep -af '\[g\]entoo-rescue-approver'", self.approver_report),
            (r"^cat /sys/kernel/security/lsm", lambda c, m: ("lockdown,capability,yama\n" if self.yama else "lockdown,capability\n") + f"\n---\n{self.ptrace}\n"),
            (r"^id -nG rescue-ai", lambda c, m: f"{self.groups}\n---\n{self.sudo_text}\n---\n\n---\nrescue-ai:x:1001:1001::/home/rescue-ai:/bin/bash\n"),
            (r"import fcntl, json, os, subprocess, sys", self.console_report),
            (r"for d in /proc/\[0-9\]", self.cwd_report),
            (r"gentoo-rescue-mount --unmount", self.unmount),
            (r"gentoo-rescue-mount </dev/null", self.mount),
            (r"readlink /mnt/gentoo", lambda c, m: mount_proof(options="ro,relatime" if self.mount_ro else "rw,relatime", rc=self.mount_rc)),
            (r"\[ -e /mnt/gentoo \]", lambda c, m: "0\n" if not self.mounted else "LEFT\n1\n"),
            (r"ls -A /run/gentoo-rescue-share", lambda c, m: ""),
            (r"gentoo-rescue-share status", self.share_status),
            (r"gentoo-rescue-share stop", self.share_stop),
            (r"gentoo-rescue-share", self.share_start),
            (r"ps -C ttyd -o user:32=,args=", self.ttyd_ps),
            (r"gentoo-rescue-agents status", self.agents_status_cli),
            (r"gentoo-rescue-agents install claude </dev/null", lambda c, m: (1, "gentoo-rescue-agents: do not run this as root.\n")),
            (r"agents\.load_pins\(\)\.items", self.real_pins),
            (r"import base64, json, os, pwd, sys", self.upload),
            (r"gentoo-rescue-agents install (\w+) --from /tmp/gentoo-vm-agent-bad", self.agents_bad),
            (r"agents\.main\(sys\.argv\[2:\]", self.agents_fake),
            (r"stat -c .* ~/\.local/bin/claude", self.agents_proof),
            (r"ls ~/\.local/bin/(\w+) 2>&1; ls ~/\.local/share", lambda c, m: ""),
            (r"ls ~/\.local/bin 2>&1; true", lambda c, m: "\n".join(sorted(self.bin_files)) + "\n"),
            (r"^rm -rf /tmp/gentoo-vm-agent", lambda c, m: ""),
            (r"^echo started$", lambda c, m: "started\n"),
        ]

    # --- session / security ----------------------------------------------------------------------------------------------------------
    def session_up(self) -> bool:
        return len(self.typed) >= self.session_after_keys

    def session_report(self, command, match):
        windows = "".join(f"{index} {name} {self.pane_tty} {self.pane_path}\n" for index, name in enumerate(self.window_names, 1))
        return f"{windows}---\n{self.client_tty}\n---\n{self.tmux_owner} tmux: server\n{self.tmux_owner} tmux -u new-session -A -s rescue\n"

    def approver_report(self, command, match):
        if not self.approver_running:
            return "9999 bash -c pgrep -af '[g]entoo-rescue-approver'; echo ---; stat\n---\nmissing\n---\n"
        return (f"412 /usr/bin/python3 /usr/bin/gentoo-rescue-approver --vt {self.approver_vt} --socket /run/gentoo-rescue/do.sock\n---\n"
                f"{self.socket_mode} /run/gentoo-rescue/do.sock\nroot:root 600 /run/gentoo-rescue/do-password\n---\n{self.approver_vt}\n")

    def console_report(self, command, match):
        state = "OPENED" if self.console_open else "refused: Permission denied"
        report = {"open": {"/dev/tty1": state, f"/dev/tty{self.approver_vt}": state, "/dev/tty0": state, "/dev/console": state},
                  "chvt": "refused (exit 1)", "tiocsti": "", "sudo": "refused", "active": self.active,
                  "secrets": {p: "READABLE" if self.secrets_readable else "refused: Permission denied" for p in ("/run/gentoo-rescue/do-password", "/var/log/gentoo-rescue/do.log")}}
        return json.dumps(report) + "\n"

    def cwd_report(self, command, match):
        return f"501 {w6.AI_HOME}\n502 {w6.AI_HOME}\n" + ("503 /mnt/gentoo/etc\n" if self.cwd_in_target else "")

    # --- gentoo-rescue-do ------------------------------------------------------------------------------------------------------------
    def do_start(self, command, match):
        number = int(re.search(r"gentoo-vm-do-(\d)", command).group(1))
        if self.runs_without_approval or (self.carries_approval and self.approved_before):
            self.requests[number] = {"state": "done", "rc": 0, "out": "0\n"}
            self.audit.append({"event": "exit", "status": 0})
        elif self.clock.now() < self.quiet_until:
            # the real approver: no question, no console switch, no 'request' event, until the quiet period is over
            self.requests[number] = {"state": "done", "rc": 77, "out": "gentoo-rescue-do: not run: rate limit after an unanswered request\n"}
            self.audit.append({"event": "refused", "reason": "rate limit after an unanswered request"})
        else:
            self.requests[number] = {"state": "waiting", "rc": None, "out": ""}
            self.audit.append({"event": "request", "argv": ["/usr/bin/id", "-u"], "cwd": w6.AI_HOME})
            if self.approver_switches:
                self.active = f"tty{self.approver_vt}"
        return "started\n"

    def do_early(self, command, match):
        item = self.requests.get(int(match.group(1)), {})
        return ("RC-EXISTS\n" if item.get("rc") is not None else "") + ("RAN\n" if item.get("rc") == 0 else "")

    def do_file(self, number, kind):
        item = self.requests.get(number, {})
        if kind == "rc":
            return "" if item.get("rc") is None else f"{item['rc']}\n"
        return item.get("out", "")

    def audit_text(self):
        events = list(self.audit)
        if self.audit_has_password:
            events.append({"event": "note", "text": self.password})
        # the real log has a timestamp; the harness does not care
        return events

    # --- mount ----------------------------------------------------------------------------------------------------------------------
    def mount(self, command, match):
        self.mounted = True
        return MOUNT_OUTPUT

    def unmount(self, command, match):
        self.mounted = False
        return "Released.\n"

    # --- share ----------------------------------------------------------------------------------------------------------------------
    def share_start(self, command, match):
        if self.share_running and not self.share_ask_twice_ok:
            return 1, "gentoo-rescue-share: A share is already running on port 23456. Stop it first: gentoo-rescue-share stop\n"
        self.share_running = True
        self.share_read_only = "--read-only" in command
        mode = "VIEW ONLY." if self.share_read_only else "the phone CAN TYPE (use --read-only for a view-only share)."
        return (f"\nSharing the rescue session on eth0 (10.0.2.15) port {SHARE_PORT}, {mode}\n(QR)\nAddress:      https://10.0.2.15:{SHARE_PORT}/\n"
                f"User/password: rescue / {SHARE_PASSWORD}\nCertificate (SHA-256): {FINGERPRINT}\n\nOne phone at a time.\n\n"
                "The phone counts as rescue-ai: it can type to the agent, but it can NEVER approve a root command.\n")

    def share_status(self, command, match):
        if not self.share_running:
            return 1, "No share is running.\n"
        extra = f" password {SHARE_PASSWORD}" if self.share_prints_password_in_status else ""
        return f"Sharing on https://10.0.2.15:{SHARE_PORT}/ (eth0), the phone can type; stops in 5 min 0 s{extra}\nCertificate (SHA-256): {FINGERPRINT}\n"

    def share_stop(self, command, match):
        if self.share_stop_works:
            self.share_running = False
        return "The share was stopped.\n"

    def ttyd_ps(self, command, match):
        if not self.share_running:
            return 1, ""
        write = "" if self.share_read_only or not self.share_writable_flag else " -W"
        return (f"{self.share_runs_as} ttyd -p {SHARE_PORT} -i eth0 -S -C /run/gentoo-rescue-share/cert.pem -K /run/gentoo-rescue-share/key.pem "
                f"-c rescue:{SHARE_PASSWORD} -m 1 -O{write} tmux attach -t rescue\n")

    def forward(self, guest_port: int) -> int:
        assert guest_port == SHARE_PORT
        return 41000

    def fetch(self, port, user, password, scheme):
        if not self.share_running:
            return w6.Fetch(None, "", "connection refused")
        if scheme == "http":
            return w6.Fetch(None, "", "bad status line")
        fingerprint = PRINTED_HEX
        if user == "rescue" and password == SHARE_PASSWORD:
            return w6.Fetch(200, fingerprint)
        return w6.Fetch(401, fingerprint)

    # --- agents ---------------------------------------------------------------------------------------------------------------------
    def agents_status_cli(self, command, match):
        if self.agents_installed:
            return "claude    installed 2.1.310\ncodex     installed 0.164.0\nopencode  installed 1.19.4\n"
        return "claude    not installed (pinned: 2.1.310)\ncodex     not installed (pinned: 0.164.0)\nopencode  not installed (pinned: 1.19.4)\n"

    def real_pins(self, command, match):
        return json.dumps({"claude": {"filename": "claude", "size": 291638640, "sha256": "b" * 64},
                           "codex": {"filename": "codex-x86_64-unknown-linux-musl.tar.gz", "size": 114979035, "sha256": "c" * 64},
                           "opencode": {"filename": "opencode-linux-x64-baseline.tar.gz", "size": 1, "sha256": "d" * 64}}) + "\n"

    def upload(self, command, match):
        return 0, ""

    def agents_bad(self, command, match):
        name = match.group(1)
        if self.refuses_bad:
            return 1, f"gentoo-rescue-agents: {name}: {name} has 22 bytes, but 291638640 were expected: refused\n"
        self.bin_files.add(name)
        return 0, f"{name} installed\n"

    def agents_fake(self, command, match):
        words = shlex.split(shlex.split(command)[-1])
        args = words[words.index(w6.FAKE_PINS) + 1:] if w6.FAKE_PINS in words else []
        if args[:1] == ["install"] and "--from" in args:
            folder = args[args.index("--from") + 1]
            if folder == w6.TAMPERED_DIR:
                return 1, "gentoo-rescue-agents: claude: claude-fake-linux-x64 does not match its pinned checksum: refused (nothing was installed)\n"
            self.bin_files |= set(w6.AGENTS)
            self.agents_installed = True
            return 0, "".join(f"Checksum is correct.\n{name} {w6.FAKE_VERSION} is installed\n" for name in w6.AGENTS)
        if args[:1] == ["status"]:
            return 0, "".join(f"{name:<9} installed {w6.FAKE_VERSION}\n" for name in w6.AGENTS)
        if args[:1] == ["remove"]:
            self.bin_files.clear()
            self.agents_installed = False
            return 0, "removed\n"
        return 2, "usage\n"

    def agents_proof(self, command, match):
        rows = "".join(f"rescue-ai 755 /home/rescue-ai/.local/bin/{name}\n" for name in w6.AGENTS)
        return rows + "---\n" + "".join(f"{name}-fake {w6.FAKE_VERSION}\n" for name in w6.AGENTS)

    # --- helpers for the tests --------------------------------------------------------------------------------------------------------
    def hands(self) -> w6.Hands:
        return w6.Hands(self.run, self.type_console, lambda: self.password, self.clock.sleep, self.clock.now)
