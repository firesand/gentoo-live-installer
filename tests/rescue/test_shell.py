"""The terminal: fixed command shape, confirmation by risk, PTY hand-over, transcript and audit."""

from __future__ import annotations

import os
import select
import subprocess
import time
from pathlib import Path

import pytest

from gentoo_installerd.rescue import shell as shellmod
from gentoo_installerd.rescue.errors import RescueError

from .flow import mount_through_the_manager, plan_and_run, uefi_gentoo_lab, unmount


def confirm(root_name: str = "nvme0n1p5", phrase: str = "", **extra):
    return {"confirm": True, "typed": root_name, "phrase": phrase, **extra}


def test_chroot_terminal_command_shape(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    session = manager.session(sid)
    shell = manager.shell_for(session)
    argv, info = shell.prepare({"session_id": sid, **confirm(), "term": "xterm-256color"})
    assert argv[:7] == ["setsid", "--ctty", "script", "--quiet", "--return", "--flush", "--log-out"]
    inner = argv[argv.index("--command") + 1]
    assert inner.startswith("sh -c 'umask 022 && exec chroot \"$@\"' rescue-chroot --userspec=0:0 --groups= " + session.root + " /usr/bin/env -i ")
    assert "TERM=xterm-256color" in inner and "RESCUE_SESSION=" + sid in inner and "SHELL=/usr/bin/bash" in inner and inner.endswith("/usr/bin/bash -l")
    assert "PYTHONPATH" not in inner and "DISPLAY" not in inner and "SUDO_USER" not in inner
    assert info["mode"] == "chroot-ro" and oct(Path(info["transcript"]).parent.stat().st_mode & 0o777) == "0o700"
    assert "READ-ONLY" in info["banner"]
    assert session.has_virtual_mounts()  # /proc, /sys, /dev and /run were prepared first


def test_a_terminal_needs_the_typed_device_and_a_writable_one_the_backup_phrase(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    shell = manager.shell_for(manager.session(sid))
    for params, missing in (({}, "confirm"), ({"confirm": True}, "typed"), ({"confirm": True, "typed": "sda1"}, "typed")):
        with pytest.raises(RescueError) as info:
            shell.prepare({"session_id": sid, **params})
        assert info.value.short == "needs_confirmation" and info.value.details["missing"] == missing
    with pytest.raises(RescueError) as info:
        shell.prepare({"session_id": sid, **confirm(), "writable": True})
    assert info.value.short == "needs_write"
    plan_and_run(manager, sid, "session.enter_write")
    with pytest.raises(RescueError) as info:
        shell.prepare({"session_id": sid, **confirm(), "writable": True})
    assert info.value.details["missing"] == "phrase"
    argv, meta = shell.prepare({"session_id": sid, **confirm(phrase="I HAVE A BACKUP"), "writable": True})
    assert meta["mode"] == "chroot-rw" and "READ-WRITE" in meta["banner"]


def test_inspect_only_sessions_get_a_live_shell_at_the_mount_point_only(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, allow_write=False, candidate_device="/dev/nvme0n1p5")
    session = manager.session(sid)
    shell = manager.shell_for(session)
    with pytest.raises(RescueError) as info:
        shell.prepare({"session_id": sid, **confirm()})
    assert info.value.short == "needs_write" and info.value.details["host_shell"] is True
    argv, meta = shell.prepare({"session_id": sid, **confirm(), "host": True})
    inner = argv[argv.index("--command") + 1]
    assert meta["mode"] == "host" and "chroot" not in inner and f"RESCUE_ROOT={session.root}" in inner
    assert not lab.system.chroot_calls and not session.has_virtual_mounts()


def test_terminal_type_is_validated(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    with pytest.raises(RescueError) as info:
        manager.shell_for(manager.session(sid)).prepare({"session_id": sid, **confirm(), "term": "xterm; reboot"})
    assert info.value.short == "bad_request"


def test_the_pty_master_is_handed_over_and_the_terminal_is_tracked(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    started = []

    def stub(argv, **kwargs):
        started.append(argv)
        return subprocess.Popen(["/bin/sh", "-c", "echo rescue-terminal; exit 3"], **kwargs)

    manager.shell_spawn = stub
    master, info = manager.open_shell({"session_id": sid, **confirm(), "rows": 40, "cols": 120})
    try:
        output = b""
        deadline = time.time() + 5
        while b"rescue-terminal" not in output and time.time() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    output += os.read(master, 1024)
                except OSError:
                    break
        assert b"rescue-terminal" in output
    finally:
        os.close(master)
    session = manager.session(sid)
    deadline = time.time() + 5
    while session.shells and time.time() < deadline:
        time.sleep(0.05)
    assert info["pid"] > 0 and not session.shells
    kinds = [r["kind"] for r in session.audit.records]
    assert "shell_start" in kinds and "shell_end" in kinds
    assert [r for r in session.audit.records if r["kind"] == "shell_end"][0]["status"] == 3
    assert started and started[0][0] == "setsid"


def test_unmounting_waits_for_open_terminals_unless_forced(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    session = manager.session(sid)
    sleeper = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    session.shells.append({"pid": sleeper.pid, "writable": False, "mode": "chroot-ro", "transcript": ""})
    try:
        with pytest.raises(RescueError) as info:
            manager.unmount(None, {"session_id": sid})
        assert info.value.short == "busy" and info.value.details["shells"] == 1
        assert session.root in lab.system.mounts
        shellmod.close_shells(session, grace=0.2)
        sleeper.wait(timeout=5)
        assert sleeper.returncode is not None
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
    session.shells.clear()
    assert manager.unmount(None, {"session_id": sid})["leftovers"] == []


def test_the_shell_command_is_checked_by_the_host_policy() -> None:
    from gentoo_installerd.rescue.errors import SafetyViolation
    from gentoo_installerd.rescue.safety import CHROOT_CREDENTIALS, CHROOT_WRAPPER_ARGV, HostPolicy, PolicyContext

    ctx = PolicyContext(session_root="/mnt/rescue/r1a2b3c", session_id="r1a2b3c")
    policy = HostPolicy(ctx)
    good = [*CHROOT_WRAPPER_ARGV, *CHROOT_CREDENTIALS, ctx.session_root, "/usr/bin/env", "-i", "TERM=xterm", "/bin/bash", "-l"]
    assert policy.validate(good) == "chroot"
    for bad in ([*CHROOT_WRAPPER_ARGV, "--userspec=1000:1000", "--groups=", ctx.session_root, "/usr/bin/env", "-i"], [*CHROOT_WRAPPER_ARGV, *CHROOT_CREDENTIALS, "/", "/usr/bin/env", "-i"]):
        with pytest.raises(SafetyViolation):
            policy.validate(bad)
    with pytest.raises(SafetyViolation):
        policy.validate(["setsid", "--ctty", "script"])  # script/setsid are only used by the terminal launcher itself
