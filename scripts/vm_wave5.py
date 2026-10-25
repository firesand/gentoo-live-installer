"""VM checks added in Wave 5 (interface I13): SSH on the installed system, remote access on the live medium, host keys, network status and
the non-interactive ``gentoo-chroot``.

Like vm_probes.py everything here is a command (run over SSH in the guest) and a pure parser of its output, so every rule is tested on
recorded output without a VM. ``scripts/test-vm.py`` owns the order in which they run. The commands and the text formats are the ones of
docs/protocol.md and the Wave 5 interface notes:

* installed system: sshd is enabled and running, and ``sshd -T`` agrees with ``accounts.ssh_password_login`` and says ``permitrootlogin no``;
* live medium: sshd runs at boot, ``gentoo-remote-access status`` shows remote access off, ``on`` makes a password login as the live user
  work (from the host), ``off`` makes it fail again, and no SSH host key is the same in two boots of one ISO;
* ``network_status`` answers in every VM (wired, so ``online`` and no problem code);
* ``gentoo-chroot`` run with its commands on standard input finds the installed system, mounts it, runs the commands with working DNS and
  leaves nothing mounted.

``gentoo-remote-access`` and ``gentoo-chroot`` print for people. The parsers look for what a person would read (an ``ssh gentoo@ADDRESS``
line, a ``Password:`` line, ``SHA256:`` fingerprints, a ``... enabled/disabled`` status line) and stay tolerant of the wording around it;
a format change that they cannot read is reported as "could not tell", never as a pass.
"""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from vm_probes import ANSI_RE, ProbeResult, _sudo_problem

if TYPE_CHECKING:  # pragma: no cover
    from gentoo_installer.manifest import InstallManifest

# --- SSH on the installed system (I10) -----------------------------------------------------------------------------


def sshd_service_command(init: str) -> str:
    """Whether sshd runs, then whether it starts at boot: two sections separated by ``---``."""
    if init == "systemd":
        return "systemctl is-active sshd; echo ---; systemctl is-enabled sshd"
    return "rc-service sshd status; echo ---; rc-update show default"


def parse_sshd_service_probe(returncode: int, output: str, init: str) -> ProbeResult:
    """sshd is running and enabled at boot (the harness reaches the machine through it, but only a probe shows that it is *enabled*)."""
    clean = ANSI_RE.sub("", output).strip()
    running_text, _, enabled_text = clean.partition("\n---\n")
    if "---" not in clean:
        return ProbeResult("sshd-service", False, clean[-400:], "the check returned no second section: could not tell whether sshd is enabled")
    problems: list[str] = []
    if init == "systemd":
        if running_text.strip().splitlines()[:1] != ["active"]:
            problems.append(f"systemctl says sshd is {running_text.strip() or 'unknown'!r}, not active")
        if enabled_text.strip().splitlines()[:1] != ["enabled"]:
            problems.append(f"systemctl says sshd is {enabled_text.strip() or 'unknown'!r}, not enabled")
    else:
        status = re.search(r"status:\s*(\w+)", running_text)
        if status is None or status.group(1) != "started":
            problems.append(f"rc-service says sshd is {status.group(1) if status else 'in an unknown state'}, not started")
        if re.search(r"(?m)^\s*sshd\s*\|", enabled_text) is None:
            problems.append("sshd is not in the default runlevel (rc-update show default)")
    return ProbeResult("sshd-service", not problems, clean[-400:], "; ".join(problems))


SSHD_T_COMMAND = "sudo -S -p '' sshd -T"


SSHD_KEYWORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def sshd_settings(output: str) -> dict[str, str]:
    """``sshd -T`` prints one ``keyword value`` per line; the first line of a keyword counts, and keywords are compared lowercase.

    OpenSSH before 10 printed the keywords lowercase (``permitrootlogin no``); the one in the pinned tree prints them as they are
    documented (``PermitRootLogin no``), which the first real VM run showed.
    """
    settings: dict[str, str] = {}
    for line in ANSI_RE.sub("", output).splitlines():
        key, _, value = line.strip().partition(" ")
        if SSHD_KEYWORD_RE.fullmatch(key) and value.strip() and key.lower() not in settings:
            settings[key.lower()] = value.strip()
    return settings


def password_login_possible(settings: Mapping[str, str]) -> bool:
    """A password can be typed: password authentication, or keyboard-interactive with PAM behind it (Gentoo's own configuration)."""
    keyboard = settings.get("kbdinteractiveauthentication", settings.get("challengeresponseauthentication", "no"))
    return settings.get("passwordauthentication") == "yes" or (keyboard == "yes" and settings.get("usepam") == "yes")


def sshd_config_problems(settings: Mapping[str, str], manifest: "InstallManifest") -> list[str]:
    """What the effective sshd configuration must say, judged against the manifest the machine was installed from."""
    if not settings:
        return ["sshd -T printed no settings"]
    problems: list[str] = []
    if not manifest.accounts.ssh_root_login and settings.get("permitrootlogin") != "no":
        problems.append(f"permitrootlogin is {settings.get('permitrootlogin', 'missing')!r}, not 'no'")
    wanted = bool(manifest.accounts.ssh_password_login)
    if password_login_possible(settings) != wanted:
        problems.append(f"password login is {'possible' if not wanted else 'impossible'} "
                        f"(passwordauthentication={settings.get('passwordauthentication')!r}, kbdinteractiveauthentication="
                        f"{settings.get('kbdinteractiveauthentication', settings.get('challengeresponseauthentication'))!r}, usepam={settings.get('usepam')!r}), "
                        f"but the manifest says ssh_password_login={wanted}")
    if not wanted:  # keys only: no mix of "no" and "yes" that happens to be unusable today but opens with the next PAM change
        for key in ("passwordauthentication", "kbdinteractiveauthentication"):
            if settings.get(key, "no") != "no":
                problems.append(f"{key} is {settings[key]!r}: keys-only login should switch it off explicitly")
    if settings.get("permitemptypasswords", "no") != "no":
        problems.append("permitemptypasswords is not 'no'")
    if manifest.accounts.ssh_authorized_keys and settings.get("pubkeyauthentication") == "no":
        problems.append("a public key was given, but pubkeyauthentication is 'no'")
    return problems


def parse_sshd_config_probe(returncode: int, output: str, manifest: "InstallManifest") -> ProbeResult:
    problem = _sudo_problem("sshd-config", returncode, output)
    if problem:
        return problem
    if returncode != 0:
        return ProbeResult("sshd-config", False, ANSI_RE.sub("", output).strip()[-400:], f"sshd -T exited with status {returncode}")
    settings = sshd_settings(output)
    keys = ("permitrootlogin", "passwordauthentication", "kbdinteractiveauthentication", "challengeresponseauthentication", "usepam", "pubkeyauthentication", "port")
    shown = " ".join(f"{key}={settings[key]}" for key in keys if key in settings)
    return ProbeResult("sshd-config", not (problems := sshd_config_problems(settings, manifest)), shown, "; ".join(problems))


def sshd_probes(manifest: "InstallManifest", init: str, password: bytes) -> list[tuple]:
    """(name, command, parser[, stdin]) for the installed system; nothing when the manifest has no SSH (the harness could not reach the machine)."""
    if not manifest.options.ssh:
        return []
    return [
        ("sshd-service", sshd_service_command(init), lambda code, text: parse_sshd_service_probe(code, text, init)),
        ("sshd-config", SSHD_T_COMMAND, lambda code, text: parse_sshd_config_probe(code, text, manifest), password),
    ]


# --- remote access on the live medium (I9) --------------------------------------------------------------------------

REMOTE_COMMAND = "gentoo-remote-access"
LIVE_LOGIN_MARKER_COMMAND = 'echo "LIVE-LOGIN-$((6*7))-$(id -un)"'  # computed, so the command's own text never counts as the answer
LIVE_LOGIN_OK = "LIVE-LOGIN-42-gentoo"
SSH_ADDRESS_RE = re.compile(r"\bssh\s+(?:-\S+\s+)*([\w.-]+)@([\w.:-]+)")
PASSWORD_RE = re.compile(r"(?im)^\W*password\s*[:=]\s*(\S+)\s*$")
# the whole key type, so a hybrid one such as MLDSA44-ED25519 (OpenSSH 10) is not read as a second ED25519 key
FINGERPRINT_RE = re.compile(r"(?:\b((?:[A-Z0-9]+-)*(?:ED25519|RSA|ECDSA))\D{0,12})?(SHA256:[A-Za-z0-9+/=]{20,})", re.I)
STATE_LINE_RE = re.compile(r"(?im)^\W*(?:remote access|ssh|remote login|password login|status)\b[^:\n]*:\s*(enabled|disabled|on|off)\b")


@dataclass(frozen=True)
class RemoteAccess:
    state: str = "unknown"                    # enabled | disabled | unknown
    addresses: tuple[str, ...] = ()
    user: str = ""
    password: str = ""
    fingerprints: tuple[tuple[str, str], ...] = ()   # (type, SHA256:...)


def parse_remote_access(output: str) -> RemoteAccess:
    """What ``gentoo-remote-access [status|on|off]`` printed: the state, the ``ssh USER@ADDRESS`` lines, the password and the key fingerprints."""
    text = ANSI_RE.sub("", output)
    state = "unknown"
    line = STATE_LINE_RE.search(text)
    if line:
        state = "enabled" if line.group(1).lower() in ("enabled", "on") else "disabled"
    else:
        lowered = text.lower()
        enabled, disabled = bool(re.search(r"\b(?:is|now|are)\s+(?:turned\s+)?(?:enabled|on)\b|\bremote access (?:is )?(?:enabled|on)\b|^\W*password:\s+set for\b", lowered, re.M)), \
            bool(re.search(r"\b(?:is|now|are)\s+(?:turned\s+)?(?:disabled|off)\b|\bremote access (?:is )?(?:disabled|off)\b|\bnobody can log in\b", lowered))
        state = "enabled" if enabled and not disabled else "disabled" if disabled and not enabled else "unknown"
    addresses = tuple(dict.fromkeys(address for _user, address in SSH_ADDRESS_RE.findall(text)))
    users = [user for user, _address in SSH_ADDRESS_RE.findall(text)]
    password = PASSWORD_RE.search(text)
    prints = tuple(dict.fromkeys((kind.upper() if kind else "", digest) for kind, digest in FINGERPRINT_RE.findall(text)))
    return RemoteAccess(state, addresses, users[0] if users else "", password.group(1) if password else "", prints)


def askpass_script(password: str) -> str:
    """A SSH_ASKPASS program that answers every prompt with the password. The password is a test-only value generated by the guest."""
    return "#!/bin/sh\nprintf '%s\\n' " + shlex.quote(password) + "\n"


def password_login_arguments(ssh_binary: str, host: str, port: int, user: str, known_hosts: str, remote: str = LIVE_LOGIN_MARKER_COMMAND) -> list[str]:
    """ssh with *only* password authentication: the harness's own key must not be able to answer (that would prove nothing).

    The password comes from SSH_ASKPASS (``SSH_ASKPASS_REQUIRE=force`` works without a terminal, OpenSSH 8.4 and later), never from the command line.
    """
    return [ssh_binary, "-F", "/dev/null", "-p", str(port), "-o", "BatchMode=no", "-o", "PubkeyAuthentication=no", "-o", "IdentitiesOnly=yes",
            "-o", "PreferredAuthentications=password,keyboard-interactive", "-o", "NumberOfPasswordPrompts=1", "-o", "ClearAllForwardings=yes",
            "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={known_hosts}", "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
            "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "LogLevel=ERROR", f"{user}@{host}", "--", remote]


def password_login_environment(askpass_path: str) -> dict[str, str]:
    return {"SSH_ASKPASS": askpass_path, "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ""}


def login_succeeded(returncode: int, output: str) -> bool:
    return returncode == 0 and LIVE_LOGIN_OK in ANSI_RE.sub("", output)


def login_was_refused(returncode: int, output: str) -> bool:
    """The server answered and said no (not: the network was down, the host key changed, the connection timed out)."""
    return returncode != 0 and LIVE_LOGIN_OK not in output and "permission denied" in output.lower()


@dataclass
class RemoteAccessReport:
    steps: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {"success": self.success, "steps": self.steps, "problems": self.problems}


def live_remote_access_flow(run: Callable[[str], tuple[int, str]], login: Callable[[str], tuple[int, str]]) -> RemoteAccessReport:
    """The whole I9 check on a booted live medium.

    ``run(command)`` runs a command as root in the live system, ``login(password)`` tries a password login as the live user from the host;
    both return (exit status, output). The passwords never reach the report (only whether the login worked).
    """
    report = RemoteAccessReport()

    def step(name: str, ok: bool, detail: str = "") -> None:
        report.steps.append({"step": name, "ok": ok, **({"detail": detail} if detail else {})})
        if not ok:
            report.problems.append(f"{name}: {detail}" if detail else name)

    code, text = run("rc-service sshd status")
    step("sshd runs at boot", code == 0 and "started" in text, ANSI_RE.sub("", text).strip()[-200:] if code != 0 or "started" not in text else "")
    code, text = run(f"{REMOTE_COMMAND} status")
    access = parse_remote_access(text)
    step("status says remote access is off at boot", code == 0 and access.state == "disabled",
         "" if access.state == "disabled" and code == 0 else f"state {access.state!r}, exit {code}")
    code, text = run(f"{REMOTE_COMMAND} on")
    access = parse_remote_access(text)
    if code != 0 or access.state != "enabled" or not access.password:
        step("on enables remote access and prints the password", False, f"exit {code}, state {access.state!r}, password {'printed' if access.password else 'missing'}")
        return report
    step("on enables remote access and prints the password", True)
    step("on prints how to log in", bool(access.addresses) and access.user == "gentoo", "" if access.addresses else "no 'ssh gentoo@ADDRESS' line")
    step("on prints host key fingerprints", bool(access.fingerprints), "" if access.fingerprints else "no SHA256 fingerprint")
    password = access.password
    code, text = login(password)
    step("a password login as gentoo works after on", login_succeeded(code, text), "" if login_succeeded(code, text) else f"exit {code}: {_tail(text)}")
    code, text = run(f"{REMOTE_COMMAND} status")
    step("status says remote access is on", parse_remote_access(text).state == "enabled", "")
    code, text = run(f"{REMOTE_COMMAND} off")
    step("off succeeds", code == 0, f"exit {code}")
    code, text = login(password)
    refused = login_was_refused(code, text)
    step("the same password is refused after off", refused,
         "" if refused else ("the login still works" if login_succeeded(code, text) else f"inconclusive, no 'Permission denied' (exit {code}): {_tail(text)}"))
    code, text = run(f"{REMOTE_COMMAND} status")
    step("status says remote access is off again", parse_remote_access(text).state == "disabled", "")
    code, text = run("rc-service sshd status")
    step("sshd keeps running after off", code == 0 and "started" in text, "")
    return report


def _tail(text: str) -> str:
    return ANSI_RE.sub("", text).strip()[-200:]


# --- host keys: fresh on every boot ---------------------------------------------------------------------------------

HOST_KEYS_COMMAND = 'for f in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$f"; done'
HOST_KEY_RE = re.compile(r"^\d+\s+(SHA256:\S+)\s.*\((\w+)\)\s*$")


def parse_host_keys(returncode: int, output: str) -> dict[str, str]:
    """{key type: fingerprint} from ``ssh-keygen -lf`` lines like ``256 SHA256:abc root@host (ED25519)``; empty when there are none."""
    keys: dict[str, str] = {}
    for line in ANSI_RE.sub("", output).splitlines():
        match = HOST_KEY_RE.match(line.strip())
        if match:
            keys[match.group(2).upper()] = match.group(1)
    return keys if returncode == 0 else {}


def host_key_problems(boots: Sequence[Mapping[str, str]]) -> list[str]:
    """Every boot has host keys, and no key type has the same fingerprint in two boots (a key baked into the image would)."""
    problems = [f"boot {number}: the live system has no SSH host keys" for number, keys in enumerate(boots, start=1) if not keys]
    if problems:
        return problems
    for index, first in enumerate(boots):
        for second_index, second in enumerate(boots[index + 1:], start=index + 1):
            same = sorted(kind for kind in first if second.get(kind) == first[kind])
            if same:
                problems.append(f"boots {index + 1} and {second_index + 1} have the same {', '.join(same)} host key: it is part of the image")
    return problems


# --- network_status (I8), run for real in every VM ------------------------------------------------------------------

NETWORK_STATUS_SNIPPET = r"""
import json, socket, sys, time
path = sys.argv[1] if len(sys.argv) > 1 else '/run/gentoo-installer/server.sock'
events = []
try:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(60)
    sock.connect(path)
    sock.sendall(json.dumps({'version': 1, 'action': 'network_status'}).encode() + b'\n')
    buffer = b''
    deadline = time.monotonic() + 60
    done = False
    while not done and time.monotonic() < deadline:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buffer += chunk
        while b'\n' in buffer:
            line, buffer = buffer.split(b'\n', 1)
            if line.strip():
                events.append(json.loads(line))
                done = done or events[-1].get('type') in ('result', 'error', 'completed')
except (OSError, ValueError) as exc:
    events.append({'type': 'harness-error', 'error': str(exc)})
print(json.dumps({'events': events}))
"""


def network_status_command(socket_path: str = "/run/gentoo-installer/server.sock") -> str:
    return shlex.join(("python3", "-c", NETWORK_STATUS_SNIPPET, socket_path))


def network_status_of(output: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(the network_status payload, the error event) from the snippet's last line."""
    for line in reversed(ANSI_RE.sub("", output).strip().splitlines()):
        try:
            events = json.loads(line).get("events", [])
        except (json.JSONDecodeError, AttributeError):
            continue
        result = next((event.get("data") for event in events if event.get("type") == "result" and isinstance(event.get("data"), dict)), None)
        error = next((event for event in events if event.get("type") in ("error", "harness-error")), None)
        return result, error
    return None, None


def network_status_problems(status: Mapping[str, Any]) -> list[str]:
    """A wired VM with working user-mode networking: online, a route, DNS, one connected ethernet device, no problem code.

    An unreachable mirror is not a finding of the harness: a host without internet cannot be fixed by the installer (the summary still shows it).
    """
    problems: list[str] = []
    for key in ("online", "default_route", "dns"):
        if status.get(key) is not True:
            problems.append(f"{key} is {status.get(key)!r}, expected true")
    if status.get("problem"):
        problems.append(f"problem {status['problem']!r}: {status.get('message', '')}".rstrip(": "))
    devices = status.get("devices")
    if not isinstance(devices, list) or not devices:
        problems.append("no network devices listed")
    elif not any(isinstance(device, dict) and device.get("kind") == "ethernet" and device.get("state") == "connected" for device in devices):
        problems.append("no connected ethernet device in the list")
    return problems


def parse_network_status_probe(returncode: int, output: str) -> ProbeResult:
    """Advisory when the daemon of this image has no network actions yet (it answers unsupported_action); a failure when it answers wrongly."""
    status, error = network_status_of(output)
    if status is None:
        code = ((error or {}).get("data") or {}).get("code", "") if error else ""
        if code == "proto.unsupported_action" or "unsupported" in str((error or {}).get("error", "")).lower():
            return ProbeResult("network-status", False, str(error)[:300], "this image's daemon has no network_status action (feature network.v1) yet", advisory=True)
        return ProbeResult("network-status", False, ANSI_RE.sub("", output).strip()[-400:],
                           "network_status gave no result" + (f": {(error or {}).get('error')}" if error else f" (exit {returncode})"))
    problems = network_status_problems(status)
    summary = {key: status.get(key) for key in ("online", "mirror", "default_route", "dns", "problem")}
    return ProbeResult("network-status", not problems, json.dumps(summary, sort_keys=True), "; ".join(problems), data={"status": dict(status)})


# --- gentoo-chroot, non-interactive (I12) -----------------------------------------------------------------------------

CHROOT_MARKERS = {
    "write": ("touch /root/.gentoo-vm-chroot-write && echo \"CHROOT-WRITE-$((6*7))\"", "CHROOT-WRITE-42"),
    "found": ("echo \"CHROOT-FOUND-$((6*7))\"", "CHROOT-FOUND-42"),
    "release": ("cat /etc/gentoo-release 2>/dev/null | sed 's/^/CHROOT-RELEASE-/'", "CHROOT-RELEASE-"),
    "dns": ("getent hosts distfiles.gentoo.org >/dev/null && echo \"CHROOT-DNS-$((6*7))\"", "CHROOT-DNS-42"),
    "emerge": ("emerge --info >/dev/null 2>&1 && echo \"CHROOT-EMERGE-$((6*7))\"", "CHROOT-EMERGE-42"),
    "grub": ("grub-mkconfig -o /dev/null >/dev/null 2>&1 && echo \"CHROOT-GRUB-$((6*7))\"", "CHROOT-GRUB-42"),
}


def chroot_input(bootloader: str = "grub", *, writable: bool = True) -> str:
    """What is typed into ``gentoo-chroot``: the answer to its one y/N question (when it asks), then the commands, then ``exit``.

    The markers are computed (``$((6*7))``), so the text of a command that the shell echoes can never pass for its output.
    """
    lines = ["y"] if writable else []
    names = ["found", "release", "dns", "emerge"] + (["grub"] if bootloader == "grub" else []) + (["write"] if writable else [])
    lines += [CHROOT_MARKERS[name][0] for name in names]
    lines.append("exit")
    return "\n".join(lines) + "\n"


def chroot_command(*, writable: bool = True) -> str:
    return "gentoo-chroot" + ("" if writable else " --read-only")


def chroot_problems(returncode: int, output: str, bootloader: str = "grub", *, writable: bool = True) -> list[str]:
    """Did the session find the installed system, mount it, run the commands with DNS, and end normally? A writable one must also be able to write."""
    clean = ANSI_RE.sub("", output)
    names = ["found", "dns", "emerge"] + (["grub"] if bootloader == "grub" else []) + (["write"] if writable else [])
    labels = {"found": "the shell inside the system never started", "dns": "DNS does not work inside the chroot (resolv.conf not prepared)",
              "emerge": "emerge --info failed inside the chroot", "grub": "grub-mkconfig failed inside the chroot",
              "write": "the system was opened read-only although a writable session was asked for (touch failed)"}
    problems = [labels[name] for name in names if CHROOT_MARKERS[name][1] not in clean]
    if "CHROOT-FOUND-42" not in clean and re.search(r"(?i)no (gentoo )?systems? found|nothing to open|not found", clean):
        problems.append("gentoo-chroot did not find the installed system")
    if "CHROOT-RELEASE-" not in clean and "CHROOT-FOUND-42" in clean:
        problems.append("/etc/gentoo-release is missing inside the chroot: this is not the installed Gentoo")
    if returncode != 0:
        problems.append(f"gentoo-chroot ended with status {returncode}")
    return problems


# --- the installer's hardware report of the VM (DMI vendors) ------------------------------------------------------------

HARDWARE_REPORT_COMMAND = "python3 -m gentoo_installer.hardware --json --redact"


def parse_hardware_report(returncode: int, output: str) -> dict[str, Any]:
    """The profile ``python -m gentoo_installer.hardware --json`` prints (the same data the wizard and the planner get); {} when there is none."""
    text = ANSI_RE.sub("", output).strip()
    start = text.find("{")
    if returncode != 0 or start < 0:
        return {}
    try:
        report = json.loads(text[start:])
    except json.JSONDecodeError:
        return {}
    return report if isinstance(report, dict) else {}
