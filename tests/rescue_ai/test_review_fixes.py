"""Fixes of the review of the ai branch (F1-F7): each has a test that fails without the fix."""

from __future__ import annotations

import os
import socket
import stat
import threading
import time
from pathlib import Path

import pytest

from gentoo_rescue_ai import approver, launcher, preflight
from tests.rescue_ai.test_approver import PASSWORD, FakeConsole, Service  # noqa: F401  (fixtures below reuse the service)
from tests.rescue_ai.test_approver import make  # noqa: F401
from tests.rescue_ai.test_preflight import UID, GID, fake_stat, good_facts

ME = os.getuid()


# ---- F1: root never follows a link planted in the home of rescue-ai --------------------------------------------------------------------------
@pytest.mark.parametrize("planted", [".local", ".codex", ".config", ".config/opencode"])
def test_f1_a_folder_replaced_by_a_link_is_refused_and_the_victim_is_untouched(tmp_path, planted) -> None:
    home, victim = tmp_path / "home", tmp_path / "victim"
    home.mkdir()
    victim.mkdir()
    (victim / "keep").write_text("root's file")
    target = home / planted
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(victim)
    before = victim.stat()
    with pytest.raises(launcher.UnsafeHome):
        launcher.prepare_home(str(home), owner=(ME, os.getgid()))
    assert sorted(p.name for p in victim.iterdir()) == ["keep"]
    assert (victim.stat().st_uid, victim.stat().st_mode) == (before.st_uid, before.st_mode)


def test_f1_the_home_itself_may_not_be_a_link(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "home").symlink_to(real)
    with pytest.raises(launcher.UnsafeHome):
        launcher.prepare_home(str(tmp_path / "home"))
    assert list(real.iterdir()) == []


def test_f1_a_file_link_is_replaced_not_followed(tmp_path) -> None:
    home, victim = tmp_path / "home", tmp_path / "victim.txt"
    (home / ".codex").mkdir(parents=True)
    victim.write_text("precious")
    (home / ".codex" / "config.toml").symlink_to(victim)
    launcher.prepare_home(str(home))
    assert victim.read_text() == "precious"
    assert not (home / ".codex" / "config.toml").is_symlink() and 'sandbox_mode = "read-only"' in (home / ".codex" / "config.toml").read_text()


def test_f1_a_folder_of_another_user_is_refused(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    real_fstat = os.fstat

    def fake(fd):
        info = real_fstat(fd)
        return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, ME + 777, info.st_gid, info.st_size, 0, 0, 0))

    monkeypatch.setattr(launcher.os, "fstat", fake)
    with pytest.raises(launcher.UnsafeHome):
        launcher.prepare_home(str(home), owner=(ME, os.getgid()))


def test_f1_main_stops_when_the_home_is_unsafe(tmp_path) -> None:
    from tests.rescue_ai.test_launcher import run_main

    (tmp_path / "home").mkdir()
    (tmp_path / "home" / ".codex").symlink_to(tmp_path)
    code, commands, prompts, _ = run_main(tmp_path, "shell", answers=[])
    assert code == 1 and commands == [] and "not safe" in prompts.text
    assert sorted(p.name for p in tmp_path.iterdir()) == ["home"]


def test_f1_the_session_is_started_with_no_new_privs() -> None:
    argv = launcher.drop_privileges(launcher.Launch(["tmux"], "/home/rescue-ai", {"A": "b"}))
    assert "--no-new-privs" in argv


# ---- F2: a command that does not fit the screen cannot be approved ------------------------------------------------------------------------
def test_f2_a_padded_command_is_refused_before_anyone_is_asked(make, tmp_path) -> None:
    service = make([PASSWORD])
    code, out, err, notes = service.ask(["/bin/rm", "-rf", "/", *["A" * 15000] * 3], cwd=str(tmp_path))
    assert code != 0 and out == b""
    assert service.console.entered == 0 and service.console.prompts == []
    assert any(r["event"] == "refused" and "too long" in r["reason"] for r in service.records())


def test_f2_a_normal_command_stays_well_under_the_limit(tmp_path) -> None:
    prompt = approver.prompt_text(approver.Peer(1, 1, 1, "rescue-ai"), ["/bin/echo", "x" * 500], str(tmp_path))
    assert len(prompt) < 2000 and prompt.count("\n") < 20


def test_f2_many_warnings_are_capped() -> None:
    prompt = approver.prompt_text(approver.Peer(1, 1, 1, "rescue-ai"), ["/bin/cat"], "/", [f"/home/f{i}" for i in range(100)])
    assert prompt.count("!!") == approver.MAX_WARNINGS + 1 and "95 more" in prompt


def test_f2_a_very_long_folder_name_is_refused() -> None:
    with pytest.raises(approver.Refused):
        approver.check_request({"argv": ["/bin/true"], "cwd": "/" + "d" * 400})


# ---- F3: the folder cannot be swapped after approval ---------------------------------------------------------------------------------------
def test_f3_a_folder_swapped_for_a_link_while_the_person_reads_is_not_used(make, tmp_path) -> None:
    work, other = tmp_path / "work", tmp_path / "other"
    work.mkdir()
    other.mkdir()
    ran: list[str] = []
    service = make([PASSWORD], runner=lambda argv, cwd, send, *rest: ran.append(cwd) or 0)
    real_read = service.console.read_secret

    def swapping(prompt, timeout):
        work.rename(tmp_path / "work-moved")
        work.symlink_to(other)
        return real_read(prompt, timeout)

    service.console.read_secret = swapping
    code, out, err, notes = service.ask(["/bin/echo", "x"], cwd=str(work))
    assert ran == [] and code == approver_refused()
    assert any(r.get("answer", "").startswith("folder changed") for r in service.records())


def approver_refused() -> int:
    from gentoo_rescue_ai import do_client

    return do_client.EXIT_REFUSED


def test_f3_the_real_runner_starts_in_the_folder_it_was_opened_on(tmp_path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    fd = os.open(a, os.O_RDONLY | os.O_DIRECTORY)
    try:
        chunks: list[dict] = []
        status = approver.run_command(["/bin/pwd"], str(b), lambda event: chunks.append(event) or True, cwd_fd=fd)
    finally:
        os.close(fd)
    import base64

    assert status == 0 and base64.b64decode(chunks[0]["data"]).decode().strip() == str(a)


def test_f3_a_folder_the_agent_can_change_is_replaced_by_root_and_said_so(make, tmp_path) -> None:
    seen: list[str] = []
    service = make([PASSWORD], runner=lambda argv, cwd, send, *rest: seen.append(cwd) or 0, cwd_check=lambda path, peer: path)
    service.ask(["/bin/echo", "x"], cwd=str(tmp_path))
    assert seen == ["/"]
    assert "runs in / instead" in service.console.prompts[0] and "folder: /" in service.console.prompts[0]


def test_f3_an_argument_that_does_not_exist_yet_in_an_agent_folder_is_flagged(tmp_path, monkeypatch) -> None:
    peer = approver.Peer(1, ME, os.getgid(), "x")
    monkeypatch.setattr(approver, "changeable_by_peer", lambda path, peer: path if path == str(tmp_path) else "")
    assert approver.writable_arguments(["/bin/cat", str(tmp_path / "log.txt"), "log.txt", "/etc/nothing-here"], str(tmp_path), peer) == [str(tmp_path / "log.txt"), "log.txt"]


# ---- F4 --------------------------------------------------------------------------------------------------------------------------------------
def test_f4_everything_outside_ascii_is_shown_as_an_escape() -> None:
    assert approver.visible("sdа é ☃") == "sd\\u0430 \\u00e9 \\u2603"
    assert approver.visible("plain ~!") == "plain ~!"


# ---- F5 --------------------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("device", ["/dev/input/event3", "/dev/uinput", "/dev/vcs1", "/dev/vcsa2", "/dev/vcsu", "/dev/mem", "/dev/kmsg"])
def test_f5_an_input_or_screen_buffer_device_the_agent_can_open_is_reported(device) -> None:
    facts = good_facts(devices={**good_facts().devices, device: fake_stat(0o660, 0, GID)})
    assert any(device in line for line in preflight.problems(facts))


def test_f5_gather_lists_those_devices_and_skips_folders() -> None:
    assert {"/dev/uinput", "/dev/mem", "/dev/kmsg"} <= {p for p in ("/dev/uinput", "/dev/mem", "/dev/kmsg") if p in preflight.KEYBOARD_PATTERNS}
    assert "/dev/input/*" in preflight.KEYBOARD_PATTERNS and "/dev/vcsa*" in preflight.KEYBOARD_PATTERNS


def test_f5_an_unlocked_root_account_is_reported() -> None:
    assert any("root account" in line for line in preflight.problems(good_facts(root_locked=False)))
    assert preflight.problems(good_facts(root_locked=True)) == []


def test_f5_root_lock_is_read_from_shadow(tmp_path) -> None:
    shadow = tmp_path / "shadow"
    shadow.write_text("root:!:19000:0:99999:7:::\nbin:*:1::::::\n")
    assert preflight.root_is_locked(str(shadow)) is True
    shadow.write_text("root:$6$abc$def:19000:0:99999:7:::\n")
    assert preflight.root_is_locked(str(shadow)) is False
    assert preflight.root_is_locked(str(tmp_path / "nope")) is None


# ---- F6 --------------------------------------------------------------------------------------------------------------------------------------
def test_f6_the_shell_window_runs_claude_with_user_settings_only_and_not_inside_the_target() -> None:
    rc = (launcher.skel_dir() / ".bashrc").read_text()
    assert "--setting-sources user" in rc and "/mnt" in rc and rc.lstrip().splitlines()[0].startswith("#")
    assert ".bashrc" in (launcher.skel_dir() / ".bash_profile").read_text()
    assert ".bashrc" in launcher.SAFE_FILES and ".bash_profile" in launcher.SAFE_FILES


def test_f6_the_function_really_adds_the_flag(tmp_path) -> None:
    import subprocess

    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "claude").write_text('#!/bin/sh\necho "args: $*"\n')
    (fake / "claude").chmod(0o755)
    run = subprocess.run(["bash", "-c", f'. {launcher.skel_dir() / ".bashrc"}; claude hello'], env={"PATH": f"{fake}:/usr/bin:/bin"}, capture_output=True, text=True, cwd=tmp_path)
    assert run.stdout.strip() == "args: --setting-sources user hello"
    run = subprocess.run(["bash", "-c", f'. {launcher.skel_dir() / ".bashrc"}; cd /; claude hello'], env={"PATH": f"{fake}:/usr/bin:/bin"}, capture_output=True, text=True, cwd=tmp_path)
    assert run.stdout.strip() == "args: --setting-sources user hello"


# ---- F7 --------------------------------------------------------------------------------------------------------------------------------------
def test_f7_after_an_unanswered_request_the_next_ones_do_not_switch_the_console(make, tmp_path) -> None:
    now = [100.0]
    service = make([None, PASSWORD])
    service.approver.clock = lambda: now[0]
    assert service.ask(["/bin/echo", "a"], cwd=str(tmp_path))[0] == approver_refused()
    code, out, _, notes = service.ask(["/bin/echo", "b"], cwd=str(tmp_path))
    assert code == approver_refused() and len(service.console.prompts) == 1 and service.console.entered == 1
    now[0] += approver.BACKOFF_START + 1
    assert service.ask(["/bin/echo", "c"], cwd=str(tmp_path))[0] == 0
    assert len(service.console.prompts) == 2


def test_f7_the_pause_doubles_and_is_capped() -> None:
    a = approver.Approver(FakeConsole([]), PASSWORD, approver.Audit("/nonexistent/x"), {1})
    values = [a._grow_backoff() for _ in range(10)]
    assert values[0] == approver.BACKOFF_START and values[1] == 2 * approver.BACKOFF_START and values[-1] == approver.BACKOFF_MAX


def test_f7_a_command_running_too_long_is_stopped(tmp_path) -> None:
    chunks: list[dict] = []
    start = time.monotonic()
    status = approver.run_command(["/bin/sleep", "30"], str(tmp_path), lambda event: chunks.append(event) or True, max_seconds=0.5)
    assert status == 124 and time.monotonic() - start < 10
    assert any(event.get("stream") == "stderr" for event in chunks)
