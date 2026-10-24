"""After a crash: what is left behind is found again from the state files and released in reverse order, with the disks returned to the state they were in."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gentoo_installerd.rescue.errors import RescueError
from gentoo_installerd.rescue.scan import ScanOptions

from . import fakeroot as fr
from . import lsblk_fixtures as lf
from .fakes import LuksSpec, VgSpec, assert_nothing_destructive
from .flow import mount_through_the_manager, plan_and_run, uefi_gentoo_lab, unmount
from .test_flow_encrypted import MAPPER, PASSPHRASE, encrypted_lab


def crash(lab):
    """A new daemon (or command) starts: it only has what the old one wrote to disk."""
    return lab.manager()


def test_a_read_write_session_with_everything_prepared_is_released_completely(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory)
    manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan_and_run(manager, sid, "session.enter_write")
    manager.session(sid).ensure_virtual()
    assert any(target.endswith("/proc") for target in lab.system.mounted_targets()) and any(target.endswith("/proc/cmdline") for target in lab.system.mounted_targets())
    del manager  # the daemon died: processes, threads and memory are gone; the mounts, the kernel flags and the state file are not
    again = crash(lab)
    found = again.orphans()
    assert [(item["sid"], item["state"]) for item in found] == [(sid, "alive")] and len(found[0]["mounts"]) >= 8
    summary = again.cleanup_all()
    assert summary["sessions"][sid]["leftovers"] == [] and lab.system.mounted_targets() == []
    assert not any(lab.system.ro.values())  # the disks are writable again, exactly as they were
    assert again.orphans() == [] and not list((lab.state_dir / "sessions").glob("*.json"))
    umounts = [argv[-1] for argv in lab.system.commands_named("umount")]
    assert umounts.index(next(m for m in umounts if m.endswith("/proc/cmdline"))) < umounts.index(next(m for m in umounts if m.endswith("/proc")))  # reverse order: the bind first
    assert_nothing_destructive(lab.system)


def test_a_session_whose_mounts_are_gone_is_stale_and_only_its_flags_are_restored(lab_factory) -> None:
    lab, _tree = uefi_gentoo_lab(lab_factory)
    manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    session = manager.session(sid)
    for target in sorted(lab.system.mounted_targets(), reverse=True):  # somebody unmounted everything by hand
        lab.system.mounts.pop(target)
    again = crash(lab)
    assert [(item["sid"], item["state"], item["mounts"]) for item in again.orphans()] == [(sid, "stale", [])]
    assert lab.system.ro["/dev/nvme0n1p5"] is True
    result = again.recover(None, {"do": "cleanup", "sid": sid})
    assert result["leftovers"] == [] and lab.system.ro["/dev/nvme0n1p5"] is False and not session.state_path.exists()
    assert again.orphans() == []


def test_mounts_without_a_state_file_are_reported_but_never_touched(lab_factory) -> None:
    lab, _tree = uefi_gentoo_lab(lab_factory)
    manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    manager.session(sid).state_path.unlink()  # the state file is lost
    again = crash(lab)
    before = len(lab.system.commands)
    found = again.orphans()
    assert [(item["sid"], item["state"]) for item in found] == [(sid, "unknown")]
    summary = again.cleanup_all()
    assert list(summary["sessions"]) == [sid] and "umount -R" in summary["sessions"][sid]["error"] and lab.system.mounted_targets() != []  # ownership cannot be proven, so nothing is unmounted
    with pytest.raises(RescueError) as err:
        again.recover(None, {"do": "adopt", "sid": sid})
    assert err.value.short == "not_found"
    assert not [argv for argv in lab.system.commands[before:] if argv[0] == "umount"]


def test_a_damaged_state_file_does_not_stop_the_listing(lab_factory) -> None:
    lab, _tree = uefi_gentoo_lab(lab_factory)
    manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    directory = lab.state_dir / "sessions"
    (directory / "r999999.json").write_text("{this is not json")
    (directory / "r888888.json").write_text(json.dumps({"version": 1}))
    again = crash(lab)
    assert sid in [item["sid"] for item in again.orphans()]  # the readable one is found, the others are ignored
    assert again.cleanup_all()["sessions"][sid]["leftovers"] == []


def test_a_busy_mount_is_reported_and_a_second_cleanup_finishes_the_job(lab_factory) -> None:
    lab, _tree = uefi_gentoo_lab(lab_factory)
    manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    root = manager.session(sid).root
    lab.system.fail_umount[root + "/efi"] = (32, "umount: busy: target is busy.")
    again = crash(lab)
    first = again.recover(None, {"do": "cleanup", "sid": sid, "force": False})
    assert first["leftovers"] == [root + "/efi"] and lab.system.ro["/dev/nvme0n1p5"] is True  # the flags stay while a mount remains
    assert not [argv for argv in lab.system.commands_named("umount") if argv[1] == "-l"]  # no lazy unmount without the user's say
    lab.system.fail_umount.clear()
    second = crash(lab).recover(None, {"do": "cleanup", "sid": sid})
    assert second["leftovers"] == [] and lab.system.mounted_targets() == [] and not any(lab.system.ro.values())
    forced_lab, _forced_tree = uefi_gentoo_lab(lab_factory)
    forced_manager, forced_sid, _ = mount_through_the_manager(forced_lab, candidate_device="/dev/nvme0n1p5")
    forced_lab.system.fail_umount[forced_manager.session(forced_sid).root + "/efi"] = (32, "umount: busy: target is busy.")
    forced = crash(forced_lab).recover(None, {"do": "cleanup", "sid": forced_sid, "force": True})
    assert forced["leftovers"] == [] and any(argv[1] == "-l" for argv in forced_lab.system.commands_named("umount"))  # --force is the lazy unmount


def test_unlocked_volumes_without_a_session_are_closed_in_reverse_order(lab_factory) -> None:
    lab, _tree, _boot = encrypted_lab(lab_factory)
    manager = lab.manager()
    manager.scan(None, ScanOptions())
    manager.unlock(None, {"device": "/dev/nvme0n1p3", "passphrase": PASSPHRASE})
    manager.activate(None, {"kind": "lvm", "vg": "vg0"})
    del manager
    again = crash(lab)
    pool = [item for item in again.orphans() if item["state"] == "pool"]
    assert len(pool) == 1 and pool[0]["mappings"] == [MAPPER] and pool[0]["vgs"] == ["vg0"]
    summary = again.cleanup_all()
    assert summary["pool"]["leftovers"] == [] and lab.system.mapped == {} and lab.system.vgs["vg0"].active is False
    commands = lab.system.commands
    assert commands.index(("vgchange", "-an", "--nohints", "vg0")) < commands.index(("cryptsetup", "close", MAPPER))  # the volume group goes before the encrypted volume below it
    assert again.orphans() == [] and not (lab.state_dir / "pool.json").exists()


def test_a_volume_that_was_open_before_rescue_started_is_never_closed(lab_factory) -> None:
    lab, _tree, _boot = encrypted_lab(lab_factory)
    lab.system.mapped[MAPPER] = "/dev/nvme0n1p3"  # opened by the user's own cryptsetup before the rescue tool was started
    lab.system.vgs["vg0"].active = True
    manager = lab.manager()
    manager.scan(None, ScanOptions())
    assert manager.pool_ledger.entries == []  # nothing in the ledger: it is not ours
    again = crash(lab)
    again.cleanup_all()
    assert lab.system.mapped == {MAPPER: "/dev/nvme0n1p3"} and lab.system.vgs["vg0"].active is True
    assert not any(argv[0] in {"vgchange"} or argv[:2] == ("cryptsetup", "close") for argv in lab.system.commands)


def test_an_adopted_session_continues_where_the_old_one_stopped(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory)
    (tree / "boot/grub/grub.cfg").unlink()
    manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    before = len(manager.session(sid).audit.records)
    adopted = crash(lab)
    info = adopted.recover(None, {"do": "adopt", "sid": sid})
    assert info["state"] == "mounted_ro" and info["session_id"] == sid
    plan_and_run(adopted, sid, "boot.config")
    audit = adopted.session(sid).audit
    assert audit.path is not None and audit.path.read_text().count('"kind":"mount"') >= 1 and len(audit.path.read_text().splitlines()) > before  # one log file for the whole life of the session
    unmount(adopted, sid)
    assert (tree / "boot/grub/grub.cfg").exists() and lab.system.mounted_targets() == [] and not any(lab.system.ro.values())
