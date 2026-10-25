#!/usr/bin/env python3
"""Claude Code PreToolUse hook of the Gentoo rescue stick (installed at /etc/gentoo-rescue/hooks/pretooluse-guard.py).

Claude Code gives this script one JSON document on standard input (tool_name, tool_input, cwd). It answers with a JSON
"deny" when the tool call would write below the broken system (/mnt/gentoo, /mnt/rescue), start from there, or climb to root
with sudo, and says nothing otherwise (the normal "ask for every Bash command" rules then apply).

This is a seat belt, not the lock: shell tricks can hide a path from any text check. The lock is that the target is mounted
read-only, that rescue-ai has no root and no sudo, and that every root command needs a password typed on the PC's console.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

TARGETS = ("/mnt/gentoo", "/mnt/rescue")
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Programs that change the files they are given (every path argument counts).
WRITERS = {"rm", "rmdir", "unlink", "shred", "touch", "mkdir", "mkfifo", "mknod", "chmod", "chown", "chgrp", "chattr", "setfacl", "setfattr", "truncate", "tee",
           "patch", "wipefs", "mkswap", "fallocate", "split", "csplit", "mv", "sponge", "gunzip", "gzip", "bzip2", "xz", "unzstd", "zstd", "ed", "ex"}
# Programs whose last path argument is where data goes.
COPIERS = {"cp", "install", "ln", "rsync", "scp", "dd", "tar", "unzip", "cpio"}
SHELLS = {"bash", "sh", "zsh", "dash", "ash", "ksh", "fish", "eval", "exec", "source", ".", "python", "python3", "perl", "ruby", "node", "env", "xargs", "nohup", "setsid",
          "script", "parallel", "busybox", "chroot", "gentoo-chroot"}
ESCALATION = {"sudo", "su", "doas", "pkexec", "runuser", "setpriv"}
OPERATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", "\n"}
REDIRECTS = (">", ">>", ">|", "&>", "&>>")
WRITE_FS = re.compile(r"^(mkfs(\.|$)|mke2fs|mkntfs|mkdosfs|newfs)")


def below_target(path: str, cwd: str) -> bool:
    if not path:
        return False
    full = path if os.path.isabs(path) else os.path.join(cwd, path)
    for candidate in (os.path.normpath(full), os.path.realpath(full)):
        if any(candidate == root or candidate.startswith(root + "/") for root in TARGETS):
            return True
    return False


def mentions_target(text: str) -> bool:
    return any(root in text for root in TARGETS)


def simple_commands(command: str) -> list[list[str]]:
    """The commands of a shell line as lists of words (best effort: quoting is honoured, expansions are not)."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    words = list(lexer)
    commands: list[list[str]] = [[]]
    for word in words:
        if word in OPERATORS or set(word) <= set(";&|()"):
            commands.append([])
        else:
            commands[-1].append(word)
    return [c for c in commands if c]


def judge_command(command: str, cwd: str) -> str:
    """The reason to deny a Bash command, or an empty string."""
    try:
        parsed = simple_commands(command)
    except ValueError:
        return "this command line cannot be read safely (unbalanced quotes); write it more simply" if mentions_target(command) else ""
    for words in parsed:
        # leading VAR=value words
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words = words[1:]
        if not words:
            continue
        program = os.path.basename(words[0])
        args = words[1:]
        paths = [a for a in args if not a.startswith("-") or "=" in a]
        if program in ESCALATION:
            return "there is no sudo here. To run something as root use: gentoo-rescue-do COMMAND ... (the person approves it on the PC's console)"
        if program in {"cd", "pushd"} and any(below_target(a, cwd) for a in args):
            return "never cd into the broken system: files there could load as instructions. Use full paths from your home directory"
        if program in SHELLS and mentions_target(command):
            return f"'{program}' hides what happens to the broken system. Run the plain commands one at a time"
        if program == "find" and mentions_target(command) and re.search(r"\s-(delete|exec|execdir|ok|okdir|fprint\w*)\b", command):
            return "find -delete/-exec on the broken system is not allowed; list first, change only through gentoo-rescue with the person's yes"
        if (program in WRITERS or WRITE_FS.match(program)) and any(below_target(a.split("=", 1)[-1], cwd) for a in paths):
            return f"'{program}' would change the broken system. It is mounted read-only; changes go through gentoo-rescue plan/run with the person's yes"
        if program == "sed" and any(re.match(r"-[A-Za-z]*i", a) or a == "--in-place" or a.startswith("--in-place=") for a in args if not a.startswith("--") or a.startswith("--in-place")) and any(below_target(a, cwd) for a in paths):
            return "sed -i would change the broken system. Show the change instead and ask"
        if program in COPIERS:
            destination = [a.split("=", 1)[-1] for a in paths][-1:] if program != "dd" else [a.split("=", 1)[1] for a in args if a.startswith("of=")]
            if program == "tar" and not any(a in {"-C", "--directory"} or a.startswith("--directory=") or a.startswith("-C") for a in args):
                destination = []
            if program == "tar":
                for index, arg in enumerate(args):
                    if arg in {"-C", "--directory"} and index + 1 < len(args):
                        destination = [args[index + 1]]
                    elif arg.startswith("--directory="):
                        destination = [arg.split("=", 1)[1]]
            if any(below_target(d, cwd) for d in destination):
                return f"'{program}' would write into the broken system. It is mounted read-only; changes go through gentoo-rescue plan/run with the person's yes"
        for index, word in enumerate(words):
            for redirect in REDIRECTS:
                if word == redirect and index + 1 < len(words) and below_target(words[index + 1], cwd):
                    return "redirecting output into the broken system is not allowed"
                if word.startswith(redirect) and len(word) > len(redirect) and below_target(word[len(redirect):], cwd):
                    return "redirecting output into the broken system is not allowed"
        if program in {"mount", "umount", "losetup", "cryptsetup", "mkswap", "swapon", "swapoff"}:
            return f"'{program}' needs root and changes the storage. Ask the person, then run it with gentoo-rescue-do"
    return ""


def judge(payload: dict) -> str:
    tool = payload.get("tool_name", "")
    data = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    if tool in EDIT_TOOLS:
        for key in ("file_path", "notebook_path", "path"):
            value = data.get(key)
            if isinstance(value, str) and below_target(value, cwd):
                return "the broken system is read-only for you. Show the change you propose and let the person decide; repairs go through gentoo-rescue plan/run"
        return ""
    if tool == "Bash":
        command = data.get("command", "")
        return judge_command(command, cwd) if isinstance(command, str) else ""
    return ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        reason = judge(payload) if isinstance(payload, dict) else ""
    except ValueError:
        reason = ""
    if reason:
        json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "Gentoo rescue stick: " + reason}}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
