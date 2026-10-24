"""The installer on a real (pseudo) terminal: colours, hidden passwords, the screen size and Ctrl-C.

The program runs as a child process on the slave side of a pty, so every ``isatty()`` is true, the line
discipline of the kernel is in the loop (echo, ``termios``) and ``input()`` takes its readline path. The
programs it would start on a real live system (loadkeys, sudo, reboot ...) are scripts in a temporary
directory that is the whole PATH of the child, so nothing outside the test is ever touched.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import stat
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

import gentoo_installer

from tests.textui.support import FakeInstaller, inspect_reply

SOURCE = Path(gentoo_installer.__file__).resolve().parents[1]
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PASSWORD = "very-secret-pass-1"


class Terminal:
    """A child process on a pty, driven like a person would: wait for text, type a line."""

    def __init__(self, argv: list[str], env: dict[str, str], rows: int = 24, cols: int = 80) -> None:
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True, start_new_session=True)
        os.close(slave)
        self.seen = ""
        self.mark = 0

    def _read(self, timeout: float) -> bool:
        ready, _, _ = select.select([self.master], [], [], timeout)
        if not ready:
            return False
        try:
            data = os.read(self.master, 65536)
        except OSError:  # the child closed its side
            return False
        self.seen += data.decode("utf-8", "replace")
        return bool(data)

    def expect(self, text: str, timeout: float = 20.0) -> str:
        deadline = time.monotonic() + timeout
        while text not in self.seen[self.mark:]:
            if time.monotonic() > deadline or (self.proc.poll() is not None and not self._read(0.05)):
                raise AssertionError(f"Waited for {text!r}; the screen shows:\n{self.seen[self.mark:]}\n--- whole session ---\n{self.seen}")
            self._read(0.1)
        found = self.seen[self.mark:]
        self.mark = self.seen.index(text, self.mark) + len(text)
        return found

    def until_prompt(self, prompt: str, timeout: float = 20.0) -> None:
        """Wait for ``prompt``; a long screen pauses with '-- more' first (24 rows), and Enter goes on."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            tail = self.seen[self.mark:]
            if prompt in tail:
                self.expect(prompt)
                return
            if "-- more" in tail:
                self.expect("-- more")
                self.expect("skip the rest -- ")
                self.type("")
            self._read(0.1)
        raise AssertionError(f"Waited for {prompt!r}:\n{self.seen[self.mark:]}")

    def echo_is_off(self) -> bool:
        return not termios.tcgetattr(self.master)[3] & termios.ECHO

    def type(self, line: str, *, secret: bool = False) -> None:
        if secret:  # the program switches the echo off right after it printed the prompt
            deadline = time.monotonic() + 5
            while not self.echo_is_off() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert self.echo_is_off()
        os.write(self.master, line.encode() + b"\n")

    def finish(self, timeout: float = 20.0) -> int:
        deadline = time.monotonic() + timeout
        while self.proc.poll() is None and time.monotonic() < deadline:
            self._read(0.1)
        while self._read(0.05):
            pass
        code = self.proc.poll()
        assert code is not None, f"The program did not end:\n{self.seen}"
        return code

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(5)
        os.close(self.master)


@pytest.fixture
def sandbox(tmp_path: Path):
    """A PATH that holds only logging stand-ins for the programs the installer may start."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "programs.log"
    for name in ("loadkeys", "sudo", "reboot", "loginctl", "systemctl", "nmtui"):
        script = bin_dir / name
        script.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 0\n')
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home"
    home.mkdir()
    return bin_dir, log, home


@pytest.fixture
def terminal(installer, sandbox):
    started: list[Terminal] = []

    def start(fake: FakeInstaller, *, term: str = "xterm", extra_env: dict[str, str] | None = None, **size) -> Terminal:
        bin_dir, _, home = sandbox
        env = {"PATH": str(bin_dir), "HOME": str(home), "TERM": term, "PYTHONPATH": str(SOURCE), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8", **(extra_env or {})}
        shell = Terminal([sys.executable, "-m", "gentoo_installer.textui", "--socket", fake.path], env, **size)
        started.append(shell)
        return shell

    yield start
    for shell in started:
        shell.close()


def drive_through_the_questions(shell: Terminal) -> None:
    shell.expect("Step 1 of 9: Keyboard")
    shell.expect("Your choice")
    shell.type("4")  # German
    shell.expect("Test the layout")
    shell.type("yz@")
    shell.expect("match the keys")
    shell.type("")
    shell.expect("Step 2 of 9: Network")
    shell.expect("Your choice")
    shell.type("")
    shell.expect("Step 3 of 9: This computer")
    shell.expect("Your choice")
    shell.type("")
    shell.expect("Step 4 of 9: Disk")
    shell.expect("Your choice")
    shell.type("1")
    shell.expect("Step 5 of 9")
    shell.expect("Your choice")
    shell.type("")
    shell.expect("Encrypt the system disk")
    shell.type("n")
    shell.expect("Step 6 of 9")
    shell.expect("Your choice")
    shell.type("")
    shell.expect("Which init system")
    shell.expect("Your choice")
    shell.type("")
    shell.expect("Step 7 of 9: Accounts")
    shell.expect("Computer name")
    shell.type("mybox")
    shell.expect("Your user name")
    shell.type("alice")
    shell.expect("Password for alice")
    shell.type(PASSWORD, secret=True)
    shell.expect("Type it again")
    shell.type(PASSWORD, secret=True)
    shell.expect("How should the administrator")
    shell.expect("Your choice")
    shell.type("")
    shell.expect("Step 8 of 9")
    shell.expect("Your choice")
    shell.type("europe")  # the real list of time zones of this machine: search instead of counting
    shell.expect("Choose your time zone")
    shell.expect("Your choice")
    shell.type("berlin")
    shell.expect("Which language")
    shell.expect("Your choice")
    shell.type("")  # Enter takes the language that goes with the German keyboard (or the first one)
    shell.expect("Step 9 of 9: Review and start")
    shell.until_prompt("Type the disk name to start")
    shell.type("/dev/vda")


def test_a_whole_installation_on_a_real_terminal(installer, terminal, sandbox) -> None:
    fake = installer(inspect_reply("qemu-q35-virtio", serials={"/dev/vda": "VDA-SERIAL-0001"}))
    shell = terminal(fake)
    drive_through_the_questions(shell)
    shell.expect("The installation finished successfully.")
    shell.expect("What now")
    shell.type("")  # Enter = restart now
    assert shell.finish() == 0
    screen = shell.seen
    assert PASSWORD not in screen  # not echoed by the terminal and not printed by the program
    assert "\x1b[1m" in screen and "\x1b[0m" in screen  # bold headings on a colour terminal
    plain = ANSI.sub("", screen).replace("\r\n", "\n")
    longest = max(plain.splitlines(), key=len)
    assert len(longest) <= 80, longest
    assert fake.manifests[0]["accounts"]["password"] == PASSWORD and fake.manifests[0]["system"]["keymap"] == "de-latin1"
    programs = sandbox[1].read_text()
    assert "loadkeys de-latin1" in programs and "reboot" in programs


def test_no_colour_when_no_color_is_set_and_no_cursor_codes_at_any_time(installer, terminal) -> None:
    fake = installer(inspect_reply("qemu-q35-virtio", serials={"/dev/vda": "VDA-SERIAL-0001"}))
    shell = terminal(fake, extra_env={"NO_COLOR": "1"})
    drive_through_the_questions(shell)
    shell.expect("What now")
    shell.type("4")
    assert shell.finish() == 0
    assert "\x1b" not in shell.seen  # no colour, no cursor movement, no bracketed-paste codes: the screen only scrolls


def test_a_terminal_that_cannot_tell_its_size_gets_80_columns(installer, terminal) -> None:
    fake = installer(inspect_reply("qemu-q35-virtio", serials={"/dev/vda": "VDA-SERIAL-0001"}))
    shell = terminal(fake, term="linux", rows=0, cols=0)  # a serial line: the kernel reports 0x0
    shell.expect("Step 1 of 9: Keyboard")
    shell.expect("Your choice")
    shell.type("q")
    shell.expect("Quit the installer?")
    shell.type("y")
    assert shell.finish() == 130
    lines = ANSI.sub("", shell.seen).replace("\r\n", "\n").splitlines()
    assert max(len(line) for line in lines) <= 80 and any(len(line) > 60 for line in lines)


def test_ctrl_c_on_a_terminal_asks_and_then_leaves_with_130(installer, terminal) -> None:
    fake = installer(inspect_reply("qemu-q35-virtio", serials={"/dev/vda": "VDA-SERIAL-0001"}))
    shell = terminal(fake)
    shell.expect("Step 1 of 9: Keyboard")
    shell.expect("Your choice")
    shell.proc.send_signal(signal.SIGINT)
    shell.expect("Quit the installer? [y/N]")
    shell.type("n")
    shell.expect("Your choice")  # the same question again
    shell.proc.send_signal(signal.SIGINT)
    shell.expect("Quit the installer? [y/N]")
    shell.type("y")
    assert shell.finish() == 130
    assert not [request for request in fake.requests if request["action"] == "start"]
