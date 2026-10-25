"""The start-up checks of the Security design: groups, Yama, sudo, console devices, the approver socket."""

from __future__ import annotations

import os
import stat

import pytest

from gentoo_rescue_ai import preflight

UID, GID = 990, 990


def fake_stat(mode: int, uid: int = 0, gid: int = 0, kind: int = stat.S_IFCHR) -> os.stat_result:
    return os.stat_result((kind | mode, 1, 1, 1, uid, gid, 0, 0, 0, 0))


def good_facts(**override) -> preflight.Facts:
    values = dict(uid=UID, gids=frozenset({GID}), group_names=frozenset({"rescue-ai"}), ptrace_scope=1, sudo_allows=False,
                  socket_stat=fake_stat(0o660, 0, GID, stat.S_IFSOCK),
                  devices={"/dev/tty1": fake_stat(0o620, 0, 5), "/dev/tty9": fake_stat(0o600, 0, 0), "/dev/tty0": fake_stat(0o620, 0, 5), "/dev/console": fake_stat(0o600, 0, 0)},
                  socket_gid=GID)
    values.update(override)
    return preflight.Facts(**values)  # type: ignore[arg-type]


def test_a_correct_image_has_no_problems() -> None:
    assert preflight.problems(good_facts()) == []


@pytest.mark.parametrize("group", sorted(preflight.FORBIDDEN_GROUPS))
def test_every_forbidden_group_is_reported(group) -> None:
    found = preflight.problems(good_facts(group_names=frozenset({"rescue-ai", group})))
    assert len(found) == 1 and group in found[0]


def test_the_task_lists_these_groups() -> None:
    assert {"tty", "wheel", "disk", "video", "input", "audio", "plugdev", "usb", "kvm", "portage"} <= preflight.FORBIDDEN_GROUPS


def test_yama_missing_or_off_is_reported() -> None:
    assert "no Yama" in preflight.problems(good_facts(ptrace_scope=None))[0]
    assert "must be 1 or more" in preflight.problems(good_facts(ptrace_scope=0))[0]
    assert preflight.problems(good_facts(ptrace_scope=2)) == []


def test_a_sudo_rule_is_reported() -> None:
    assert "no sudo rule" in preflight.problems(good_facts(sudo_allows=True))[0]


def test_a_console_device_the_agent_can_open_is_reported() -> None:
    open_tty = fake_stat(0o620, 0, GID)  # group rescue-ai may write it
    found = preflight.problems(good_facts(devices={"/dev/tty1": open_tty, "/dev/tty9": fake_stat(0o666)}))
    assert len(found) == 2 and "/dev/tty1" in found[0] and "/dev/tty9" in found[1]
    assert not preflight.can_open(fake_stat(0o620, 0, 5), UID, frozenset({GID}))  # group tty (5) is not ours
    assert preflight.can_open(fake_stat(0o600, UID, 0), UID, frozenset({GID}))
    assert not preflight.can_open(None, UID, frozenset({GID}))


def test_the_approver_socket_must_exist_and_belong_to_root_and_rescue_ai() -> None:
    assert "not running" in preflight.problems(good_facts(socket_stat=None))[0]
    assert "0660" in preflight.problems(good_facts(socket_stat=fake_stat(0o666, 0, GID, stat.S_IFSOCK)))[0]
    assert "0660" in preflight.problems(good_facts(socket_stat=fake_stat(0o660, UID, GID, stat.S_IFSOCK)))[0]
    assert "0660" in preflight.problems(good_facts(socket_stat=fake_stat(0o660, 0, 5, stat.S_IFSOCK)))[0]