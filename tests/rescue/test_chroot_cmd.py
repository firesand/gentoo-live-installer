"""gentoo-chroot without a session: find, unlock, mount, one question, shell, unmount (interface I12)."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time

import pytest

from gentoo_installerd.rescue import chroot_cmd

from . import fakeroot as fr
from .fakes import LuksSpec, VgSpec
from .test_cli import Run, SpawnedShell, prepared
from . import lsblk_fixtures as lf
from .test_flow_encrypted import MAPPER, PASSPHRASE, ROOT_LV, encrypted_lab


@pytest.fixture(autouse=True)
def terminal(monkeypatch):
    monkeypatch.setattr("gentoo_installerd.rescue.shell._stdin_is_tty", lambda: True)


def inner_of(spawned: SpawnedShell) -> str:
    argv = spawned.calls[0]
    return argv[argv.index("--command") + 1]


def test_one_system_is_taken_at_once_and_read_only_is_the_default_answer(lab_factory, capsys) -> None:
    lab, _tree, _ = prepared(lab_factory)  # Gentoo on the NVMe disk, a Debian on the second disk (never opened)
    spawned = SpawnedShell()
    run = Run(lab, shell_spawn=spawned)
    code, out, err = run("chroot", interactive=True, answers=[""])
    assert code == 0, err
    assert "Gentoo systems found:" in out and "Using Gentoo Linux on /dev/nvme0n1p5" in out and "Mount plan" in out
    assert "Open it read-write? Changes are real" in out and "READ-WRITE" not in capsys.readouterr().out
    assert "--rcfile /run/gentoo-rescue/bashrc -i" in inner_of(spawned)
    assert "Unmounted." in out and lab.system.mounted_targets() == []
    assert not any("sdb1" in target for target in lab.system.mounted_targets())


def test_the_one_question_answered_yes_opens_the_system_read_write(lab_factory, capsys) -> None:
    lab, tree, _ = prepared(lab_factory)
    spawned = SpawnedShell()
    run = Run(lab, shell_spawn=spawned)
    code, out, err = run("chroot", interactive=True, answers=["y"])
    assert code == 0, err
    assert "Switching to read-write" in out and "READ-WRITE: changes are real" in capsys.readouterr().out
    assert "Unmounted." in out and lab.system.mounted_targets() == []


def test_read_only_and_writable_flags_skip_the_question(lab_factory, capsys) -> None:
    lab, _tree, _ = prepared(lab_factory)
    run = Run(lab, shell_spawn=SpawnedShell())
    code, out, _ = run("chroot", "--read-only", interactive=True, answers=["y"])  # a stray "y" is never read
    assert code == 0 and "Open it read-write?" not in out and "READ-ONLY" in capsys.readouterr().out
    code, out, _ = run("chroot", "--writable", interactive=True)
    assert code == 0 and "Open it read-write?" not in out and "Switching to read-write" in out and lab.system.mounted_targets() == []


def test_without_a_terminal_nothing_is_asked_and_the_system_is_read_only(lab_factory, capsys) -> None:
    lab, _tree, _ = prepared(lab_factory)
    run = Run(lab, shell_spawn=SpawnedShell())
    code, out, _ = run("chroot", interactive=False, answers=["y"])
    assert code == 0 and "Open it read-write?" not in out and "Mounted read-only (no terminal to ask on)" in out and "READ-ONLY" in capsys.readouterr().out
    assert lab.system.mounted_targets() == []


def test_several_systems_are_chosen_by_number_or_named_on_the_command_line(lab_factory) -> None:
    lab, _tree, _ = prepared(lab_factory)
    second = lab.add_tree("/dev/sdb1", "gentoo")
    assert second.exists()
    spawned = SpawnedShell()
    run = Run(lab, shell_spawn=spawned)
    code, out, err = run("chroot", interactive=False)
    assert code == 2  # not guessed: several systems and nobody to ask
    assert "Several Gentoo systems were found" in err and not spawned.calls
    code, out, err = run("chroot", interactive=True, answers=["x", "2", "n"])
    assert code == 0, err
    assert "Please type one of the numbers shown." in out and "/   /dev/sdb1" in out  # the second system, not the first
    assert lab.system.mounted_targets() == []
    spawned.calls.clear()
    code, out, err = run("chroot", "/dev/nvme0n1p5", "--read-only", interactive=False)
    assert code == 0, err
    assert "Open it read-write?" not in out and spawned.calls and lab.system.mounted_targets() == []


def test_a_system_that_is_not_gentoo_is_not_opened(lab_factory) -> None:
    lab, _tree, _ = prepared(lab_factory)
    run = Run(lab, shell_spawn=SpawnedShell())
    code, out, err = run("chroot", "/dev/sdb1", "--read-only")
    assert code == 7 and "Not a Gentoo system" in err and lab.system.mounted_targets() == []


def test_no_gentoo_system_says_so(lab_factory) -> None:
    lab, _tree, _ = prepared(lab_factory)
    lab.system.attach("/dev/nvme0n1p5", lab.tmp / "empty")
    (lab.tmp / "empty").mkdir()
    run = Run(lab, shell_spawn=SpawnedShell())
    code, out, err = run("chroot", interactive=True)
    assert code == 5 and "No Gentoo system was found" in err and lab.system.mounted_targets() == []


def test_an_encrypted_system_is_unlocked_on_the_console_and_locked_again_afterwards(lab_factory) -> None:
    lab, _tree, _boot = encrypted_lab(lab_factory)
    spawned = SpawnedShell()
    run = Run(lab, shell_spawn=spawned)
    code, out, err = run("chroot", interactive=True, secrets=[PASSPHRASE], answers=["n"])
    assert code == 0, err
    assert "Unlocked /dev/nvme0n1p3." in out and "Activating the LVM volume group" in out and "Using Gentoo Linux on " + ROOT_LV in out
    assert spawned.calls and "Unmounted." in out
    assert lab.system.mounted_targets() == [] and lab.system.mapped == {}  # the volume that the tool opened is closed again


def test_a_wrong_passphrase_is_tried_again_and_an_empty_one_gives_up(lab_factory) -> None:
    lab, _tree, _boot = encrypted_lab(lab_factory)
    spawned = SpawnedShell()
    run = Run(lab, shell_spawn=spawned)
    code, out, err = run("chroot", interactive=True, secrets=["wrong", ""], answers=[])
    assert code == 5 and not spawned.calls and lab.system.mapped == {}


class SleepingShell:
    """A terminal that runs until it is hung up, like a person typing at it."""

    def __init__(self) -> None:
        self.children: list[subprocess.Popen] = []

    def __call__(self, argv, **kwargs):
        child = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
        self.children.append(child)
        return child


def test_a_hangup_or_a_stop_signal_unmounts_everything(lab_factory) -> None:
    for signum in (signal.SIGHUP, signal.SIGTERM):
        lab, _tree, _ = prepared(lab_factory)
        shell = SleepingShell()
        run = Run(lab, shell_spawn=shell)
        threading.Thread(target=lambda: (time.sleep(0.6), os.kill(os.getpid(), signum)), daemon=True).start()
        try:
            code, out, err = run("chroot", "--read-only", interactive=False)
        finally:
            for child in shell.children:
                child.kill()
                child.wait()
        assert code == 130 and "Interrupted: unmounting." in err and "Unmounted." in out, (signum, out, err)
        assert lab.system.mounted_targets() == []
        assert signal.getsignal(signal.SIGTERM) is not chroot_cmd._Hangup._raise  # the handlers were put back


def test_the_session_form_still_works_and_keeps_the_typed_name(lab_factory) -> None:
    from .test_cli import mount

    lab, _tree, _ = prepared(lab_factory)
    spawned = SpawnedShell()
    run = Run(lab, shell_spawn=spawned)
    sid = mount(run)
    code, _out, err = run("chroot", sid)
    assert code == 4 and "Type nvme0n1p5 to confirm" in err and not spawned.calls
    code, _out, err = run("chroot", sid, "--typed", "nvme0n1p5")
    assert code == 0, err
    assert lab.system.mounted_targets() != []  # the session stays mounted until it is unmounted
    assert run("unmount", sid)[0] == 0 and lab.system.mounted_targets() == []


def test_piped_commands_do_not_ask_for_a_controlling_terminal(lab_factory, monkeypatch) -> None:
    monkeypatch.setattr("gentoo_installerd.rescue.shell._stdin_is_tty", lambda: False)
    lab, _tree, _ = prepared(lab_factory)
    spawned = SpawnedShell()
    code, _out, err = Run(lab, shell_spawn=spawned)("chroot", "--read-only", interactive=False)
    assert code == 0, err
    assert spawned.calls[0][:3] == ["setsid", "script", "--quiet"] and "--ctty" not in spawned.calls[0]
