#!/usr/bin/env python3
"""A stand-in for ssh that plays the guest from a JSON rule table.

The harness runs ``ssh <options> user@host -- <remote command>`` for everything it does in the
guest. This script finds the remote command, matches it against the rules in the file named
by FAKE_SSH_RULES (first match wins) and answers like the guest would. Rule keys:

  match      regex searched in the remote command        user      regex for the ssh user
  run_dir    regex for the run directory (from UserKnownHostsFile)
  requires   file (relative to the run directory) that must exist for the rule to apply
  absent     file that must NOT exist
  times      the rule only answers its first N invocations (counted across processes)
  rc, stdout, stderr, sleep                              the answer
  events     list of objects printed as JSON lines (the installer's event stream)
  stdin_to   save the command's stdin below <run dir>/fake-guest/<name>
  touch      files (relative to the run directory) to create
  shutdown_vm  "live" or "target": SIGTERM the fake QEMU of that phase (the guest powers off)
  write      {"path": ..., "text": ...}: write a file below the run directory (the guest changes a disk or NVRAM)
  guest_tool answer like scripts/vm_guest.py for decoy-prepare / decoy-check (checksums of the host's image file)
  guard      answer like the guest's disk guard for the GuardSpec found in the command; an object
             {"bus": ..., "mutate": ...} describes the fabricated guest (see tests/vm/guard_fixtures.py)

Every invocation is appended to FAKE_SSH_LOG as one JSON object.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import select
import signal
import stat
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

OPTIONS_WITH_ARGUMENT = {"-F", "-i", "-p", "-o", "-l", "-L", "-R", "-D", "-J", "-b", "-c", "-E", "-S", "-W", "-w"}


def parse(argv: list[str]) -> tuple[dict[str, str], str, str]:
    options: dict[str, str] = {}
    index = 0
    destination = ""
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            return options, destination, argv[index + 1] if index + 1 < len(argv) else ""
        if argument in OPTIONS_WITH_ARGUMENT:
            value = argv[index + 1]
            if argument == "-o" and "=" in value:
                key, _, setting = value.partition("=")
                options[key] = setting
            index += 2
            continue
        if not argument.startswith("-"):
            destination = argument
        index += 1
    return options, destination, ""


def guest_tool_answer(remote: str, run_dir: Path) -> str:
    """vm_guest.py as seen from the host: the decoy checksums are those of the extra-N.img files."""
    import hashlib
    import shlex
    words = shlex.split(remote)
    command, payload = words[-2], json.loads(words[-1])
    index = int(payload["role"].rsplit("-", 1)[1]) if payload.get("role") else 1
    image = run_dir / f"extra-{index}.img"
    digest = hashlib.sha256(image.read_bytes()).hexdigest() if image.exists() else ""
    if command == "decoy-prepare":
        report = {"path": payload["path"], "partition": payload["path"] + "1", "size": payload["size"], "sha256": digest}
    elif command == "decoy-check":
        report = {"path": payload["path"], "size": payload["size"], "sha256": digest, "unchanged": digest == payload["sha256"]}
    else:
        report = {"mode": payload.get("mode"), "root": payload.get("path", "") + "3", "removed": ["boot/grub/grub.cfg"]}
    return json.dumps({"type": "vm_guest", "ok": True, **report}) + "\n"


def read_stdin() -> bytes:
    if not stat.S_ISFIFO(os.fstat(0).st_mode) and not stat.S_ISREG(os.fstat(0).st_mode):
        return b""
    if select.select([0], [], [], 1.0)[0]:
        return sys.stdin.buffer.read()
    return b""


def bump(state_path: Path, index: int) -> int:
    with open(str(state_path) + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        state[str(index)] = state.get(str(index), 0) + 1
        state_path.write_text(json.dumps(state))
        return state[str(index)]


def main() -> int:
    rules_path = Path(os.environ["FAKE_SSH_RULES"])
    rules = json.loads(rules_path.read_text())
    options, destination, remote = parse(sys.argv[1:])
    user = destination.split("@", 1)[0]
    known_hosts = options.get("UserKnownHostsFile", "")
    run_dir = Path(known_hosts).parent if known_hosts else Path.cwd()
    entry = {"user": user, "remote": remote, "options": options, "matched": None}
    chosen = None
    for index, rule in enumerate(rules):
        if "user" in rule and not re.search(rule["user"], user):
            continue
        if "run_dir" in rule and not re.search(rule["run_dir"], str(run_dir)):
            continue
        if "match" in rule and not re.search(rule["match"], remote, re.S):
            continue
        if "requires" in rule and not (run_dir / rule["requires"]).exists():
            continue
        if "absent" in rule and (run_dir / rule["absent"]).exists():
            continue
        if "times" in rule and bump(Path(str(rules_path) + ".state"), index) > rule["times"]:
            continue
        chosen = rule
        entry["matched"] = index
        break
    stdin = read_stdin() if chosen and chosen.get("stdin_to") else b""
    entry["stdin_bytes"] = len(stdin)
    code = 127
    if chosen is None:
        print(f"fake ssh: no rule for {user}@guest: {remote[:200]}", file=sys.stderr)
    else:
        if chosen.get("sleep"):
            time.sleep(float(chosen["sleep"]))
        if chosen.get("stdin_to"):
            target = run_dir / "fake-guest" / chosen["stdin_to"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(stdin)
        for name in chosen.get("touch", []):
            (run_dir / name).write_text("1")
        if chosen.get("write"):
            (run_dir / chosen["write"]["path"]).write_text(chosen["write"]["text"])
        output = chosen.get("stdout", "")
        if chosen.get("guard"):
            from tests.vm import guard_fixtures
            options = chosen["guard"] if isinstance(chosen["guard"], dict) else {}
            output += guard_fixtures.answer_guard(remote, **options)
        if chosen.get("guest_tool"):
            output += guest_tool_answer(remote, run_dir)
        if "events" in chosen:
            output += "".join(json.dumps(event) + "\n" for event in chosen["events"])
        sys.stdout.write(output)
        sys.stdout.flush()
        sys.stderr.write(chosen.get("stderr", ""))
        which = chosen.get("shutdown_vm")
        if which:
            try:
                os.kill(int((run_dir / f"qemu-{which}.pid").read_text()), signal.SIGTERM)
            except (OSError, ValueError):
                pass
        code = int(chosen.get("rc", 0))
    if os.environ.get("FAKE_SSH_LOG"):
        with open(os.environ["FAKE_SSH_LOG"], "a") as log:
            log.write(json.dumps(entry) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
