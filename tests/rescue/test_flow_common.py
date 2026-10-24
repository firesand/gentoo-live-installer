"""The family-independent repairs: fstab, file system checks, accounts, EFI entries, fallback boot file, cancel."""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import pytest

from gentoo_installerd.rescue.errors import RescueError

from . import fakeroot as fr
from . import lsblk_fixtures as lf
from .fakes import assert_nothing_destructive
from .flow import chroot_commands, mount_through_the_manager, plan_and_run, uefi_gentoo_lab, unmount

DEAD_FSTAB = (f"UUID={fr.ROOT_UUID}\t/\text4\tnoatime\t0 1\nUUID={fr.DEAD_UUID}\t/efi\tvfat\tumask=0077\t0 2\nUUID={fr.DEAD_UUID}\t/home\text4\tnoatime\t0 2\n"
              f"UUID={fr.SWAP_UUID}\tnone\tswap\tsw\t0 0\n")


def test_fstab_fix_preselects_only_what_is_safe_and_shows_a_diff(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory, fstab=DEAD_FSTAB)
    (tree / "efi").mkdir(exist_ok=True)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert any(f["id"] == "D07" for f in info["findings"])
    plan = manager.plan({"session_id": sid, "action_id": "fstab.fix"})
    assert plan["risk"] == "high" and plan["diff"].startswith("--- a/etc/fstab")
    assert f"-UUID={fr.DEAD_UUID}\t/efi" in plan["diff"] and f"+UUID={fr.ESP_UUID}\t/efi" in plan["diff"]  # the only EFI-typed partition on the disk, with an EFI directory
    assert f"-UUID={fr.DEAD_UUID}\t/home" in plan["diff"] and "nofail" in plan["diff"]  # no safe replacement: relaxed, not guessed (review R13)
    assert any("no safe replacement" in w for w in plan["warnings"]) and plan["params"]["choices"]
    plan, result, _ = plan_and_run(manager, sid, "fstab.fix")
    assert result["outcome"] == "success" and all(check["ok"] for check in result["post_checks"])
    unmount(manager, sid)
    text = (tree / "etc/fstab").read_text()
    assert f"UUID={fr.ESP_UUID}\t/efi" in text and "/home\text4\tnoatime,nofail" in text and text.count(fr.DEAD_UUID) == 1  # only /home keeps its dead UUID
    backups = list((tree / "etc").glob("fstab.gentoo-rescue-*"))
    assert len(backups) == 1 and backups[0].read_text() == DEAD_FSTAB and oct(backups[0].stat().st_mode & 0o777) == "0o600"


def test_fstab_fix_refuses_when_the_file_was_edited_after_the_plan(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory, fstab=DEAD_FSTAB)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan = manager.plan({"session_id": sid, "action_id": "fstab.fix"})
    (tree / "etc/fstab").write_text(DEAD_FSTAB + "# edited in a terminal in the meantime\n")
    result = manager.run_plan(manager.new_op("run", sid), {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True, "typed": plan["confirm"]["typed"]})
    assert result["outcome"] == "failed"
    failed = [step for step in result["steps"] if step["status"] == "failed"]
    assert failed and "changed after the plan was made" in failed[0]["error"]
    unmount(manager, sid)
    assert (tree / "etc/fstab").read_text().endswith("# edited in a terminal in the meantime\n")  # the edit survived


def test_fstab_fix_user_choices_are_validated(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory, fstab=DEAD_FSTAB)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    for bad in ({"3": {"action": "replace", "spec": "UUID=ffffffff-ffff-ffff-ffff-ffffffffffff"}}, {"3": {"action": "replace", "spec": "/dev/sdb1"}}, {"x": {"action": "nofail"}}, {"99": {"action": "nofail"}}, "nope"):
        with pytest.raises(RescueError) as info:
            manager.plan({"session_id": sid, "action_id": "fstab.fix", "params": {"choices": bad}})
        assert info.value.short == "bad_request"
    plan = manager.plan({"session_id": sid, "action_id": "fstab.fix", "params": {"choices": {"3": {"action": "comment"}}}})
    assert "+# disabled by gentoo-rescue" in plan["diff"]


def test_fstab_fix_has_nothing_to_do_for_a_healthy_system(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert {a["id"]: a for a in info["actions"]}["fstab.fix"]["available"] is False
    with pytest.raises(RescueError) as info2:
        manager.plan({"session_id": sid, "action_id": "fstab.fix"})
    assert info2.value.short == "unsupported_action"


def test_fs_check_is_read_only_first_and_returns_to_a_read_only_mount(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    lab.system.script(("e2fsck", "-n"), "Pass 1: Checking inodes\n/dev/nvme0n1p5: 11/655360 files\n", rc=4)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, result, _ = plan_and_run(manager, sid, "fs.check")
    assert plan["risk"] == "medium" and plan["needs"]["unmounted_device"] == "/dev/nvme0n1p5"
    assert result["outcome"] == "success"  # exit code 4 only means 'errors found' for a read-only check
    commands = lab.system.commands
    session = manager.session(sid)
    check = commands.index(("e2fsck", "-n", "-f", "/dev/nvme0n1p5"))
    first_umount = commands.index(("umount", session.root))
    assert first_umount < check
    assert ("blockdev", "--setrw", "/dev/nvme0n1p5") not in commands[first_umount:]  # a read-only check never needs a writable device
    assert session.mode == "ro" and session.state == "mounted_ro" and session.root in lab.system.mounts
    assert (Path(session.root) / "etc/os-release").exists()  # remounted
    unmount(manager, sid)


def test_fs_repair_makes_the_device_writable_explicitly_and_restores_it(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, result, _ = plan_and_run(manager, sid, "fs.check", {"mode": "repair"})
    assert plan["risk"] == "high"
    commands = lab.system.commands
    setrw = commands.index(("blockdev", "--setrw", "/dev/nvme0n1p5"))
    run = commands.index(("e2fsck", "-f", "-p", "/dev/nvme0n1p5"))
    assert setrw < run and commands.index(("blockdev", "--setro", "/dev/nvme0n1p5"), run) > run
    assert lab.system.ro["/dev/nvme0n1p5"] is True and manager.session(sid).mode == "ro"
    unmount(manager, sid)


def test_forced_repairs_are_dangerous_and_need_the_backup_phrase(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan = manager.plan({"session_id": sid, "action_id": "fs.check", "params": {"mode": "repair_force"}})
    assert plan["risk"] == "dangerous" and plan["confirm"]["phrase"] == "I HAVE A BACKUP" and plan["confirm"]["typed"] == "nvme0n1p5"
    request = {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True, "typed": "nvme0n1p5"}
    with pytest.raises(RescueError) as info:
        manager.run_plan(manager.new_op("run", sid), request)
    assert info.value.short == "needs_confirmation" and info.value.details["missing"] == "phrase"
    result = manager.run_plan(manager.new_op("run", sid), {**request, "phrase": "I HAVE A BACKUP"})
    assert ("e2fsck", "-f", "-y", "/dev/nvme0n1p5") in lab.system.commands and result["outcome"] == "success"


def test_the_cleanup_remounts_even_when_the_check_fails(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    lab.system.script(("e2fsck", "-f", "-p"), "UNEXPECTED INCONSISTENCY; RUN fsck MANUALLY", rc=4)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, result, _ = plan_and_run(manager, sid, "fs.check", {"mode": "repair"})
    assert result["outcome"] == "failed"
    session = manager.session(sid)
    assert session.root in lab.system.mounts and session.mode == "ro" and lab.system.ro["/dev/nvme0n1p5"] is True
    unmount(manager, sid)


def test_fs_check_only_accepts_devices_of_the_session_and_known_file_systems(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    for device in ("/dev/sdb1", "/dev/nvme0n1p6", "not a device"):
        with pytest.raises(RescueError) as info:
            manager.plan({"session_id": sid, "action_id": "fs.check", "params": {"device": device}})
        assert info.value.short == "bad_request"
    plan = manager.plan({"session_id": sid, "action_id": "fs.check", "params": {"device": "/dev/nvme0n1p1"}})
    assert plan["steps"][-1]["argv"][:2] == ["fsck.vfat", "-n"]
    with pytest.raises(RescueError):
        manager.plan({"session_id": sid, "action_id": "fs.check", "params": {"mode": "format"}})


def test_set_a_password_without_it_ever_appearing_in_a_log(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert {a["id"]: a for a in info["actions"]}["acct.password"]["needs"]["secrets"] == ["password"]
    plan = manager.plan({"session_id": sid, "action_id": "acct.password", "params": {"username": "user", "expire": True}})
    assert plan["secrets"] == ["password"] and any("chpasswd" in step["display"] and "not shown" in step["display"] for step in plan["steps"])
    secret = "s3cret! pass:word"
    with pytest.raises(RescueError) as err:
        manager.run_plan(manager.new_op("run", sid), {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True, "typed": plan["confirm"]["typed"]})
    assert err.value.short == "needs_confirmation" and err.value.details["missing"] == "password"
    plan, result, op = plan_and_run(manager, sid, "acct.password", {"username": "user", "expire": True}, secrets={"password": secret})
    assert result["outcome"] in {"success", "warning"}
    session = manager.session(sid)
    chpasswd = [call for call in lab.system.calls if call.secret and call.stdin]
    assert chpasswd and chpasswd[0].stdin == f"user:{secret}\n"
    everything = json.dumps([call.argv for call in lab.system.calls]) + session.audit.text() + json.dumps(plan) + json.dumps(result) + json.dumps([e.to_dict() for e in op.buffer.since(0)])
    assert secret not in everything and "s3cret" not in everything
    assert ("usermod", "-U", "user") in chroot_commands(lab) and ("chage", "-d", "0", "user") in chroot_commands(lab)
    unmount(manager, sid)
    assert "$6$rescued" in (tree / "etc/shadow").read_text()
    assert secret not in (tree / "var/log/gentoo-rescue").glob("*.jsonl").__next__().read_text()


import json  # noqa: E402  (used above; kept near its only users)


@pytest.mark.parametrize("bad", ["root\nadmin", "us er", "-r", "UPPER", "a:b", "x" * 40, "", "../etc", "ghost"])
def test_account_names_are_validated_against_the_system(lab_factory, bad: str) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    with pytest.raises(RescueError) as info:
        manager.plan({"session_id": sid, "action_id": "acct.password", "params": {"username": bad}})
    assert info.value.short in {"bad_request", "not_found"}


@pytest.mark.parametrize("password", ["two\nlines", "carriage\rreturn", "nul\x00byte"])
def test_passwords_with_line_breaks_are_rejected(lab_factory, password: str) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, result, _ = plan_and_run(manager, sid, "acct.password", {"username": "root"}, secrets={"password": password})
    assert result["outcome"] == "failed"
    assert not [call for call in lab.system.calls if call.secret]  # chpasswd never ran: no second account can be injected


def test_unlocking_an_account(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan_and_run(manager, sid, "acct.unlock", {"username": "user"})
    assert ("usermod", "-U", "user") in chroot_commands(lab)


def test_efi_entries_can_be_listed_added_ordered_and_deleted(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    lab.system.nvram["entries"]["0000"] = {"label": "Windows Boot Manager", "loader": "\\EFI\\Microsoft\\Boot\\bootmgfw.efi", "active": True, "part": "1"}
    lab.system.nvram["order"] = ["0000"]
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    listing = manager.query({"session_id": sid, "query": "efi.list"})["data"]
    assert listing["usable"] and [e["label"] for e in listing["entries"]] == ["Windows Boot Manager"] and listing["order"] == ["0000"]
    plan, result, _ = plan_and_run(manager, sid, "efi.add", {"loader": "\\EFI\\Gentoo\\grubx64.efi", "label": "Gentoo"})
    assert plan["needs"]["write"] is False and plan["risk"] == "medium"
    created = [c for c in lab.system.commands if c[:2] == ("efibootmgr", "--create")][0]
    assert created == ("efibootmgr", "--create", "--disk", "/dev/nvme0n1", "--part", "1", "--label", "Gentoo", "--loader", "\\EFI\\Gentoo\\grubx64.efi")
    assert result["outcome"] == "success" and manager.session(sid).mode == "ro"  # NVRAM edits need no read-write mount
    listing = manager.query({"session_id": sid, "query": "efi.list"})["data"]
    assert [e["number"] for e in listing["entries"]] == ["0000", "0001"] and listing["order"][0] == "0001"
    plan_and_run(manager, sid, "efi.order", {"order": ["0000", "0001"]})
    assert ("efibootmgr", "--bootorder", "0000,0001") in lab.system.commands
    plan_and_run(manager, sid, "efi.next", {"bootnum": "0000"})
    assert ("efibootmgr", "--bootnext", "0000") in lab.system.commands
    plan_and_run(manager, sid, "efi.delete", {"bootnum": "0001"})
    assert ("efibootmgr", "--bootnum", "0001", "--delete-bootnum") in lab.system.commands
    assert manager.query({"session_id": sid, "query": "efi.list"})["data"]["order"] == ["0000"]


def test_efi_parameters_are_validated(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    lab.system.nvram["entries"]["0000"] = {"label": "Windows Boot Manager", "loader": "\\EFI\\Microsoft\\Boot\\bootmgfw.efi", "active": True, "part": "1"}
    lab.system.nvram["order"] = ["0000"]
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    for action, params, code in (
        ("efi.delete", {"bootnum": "ZZZZ"}, "bad_request"), ("efi.delete", {"bootnum": "0042"}, "not_found"), ("efi.delete", {}, "bad_request"),
        ("efi.order", {"order": []}, "bad_request"), ("efi.order", {"order": ["0000", "0000"]}, "bad_request"), ("efi.order", {"order": "0000"}, "bad_request"),
        ("efi.next", {"bootnum": "12345"}, "bad_request"), ("efi.add", {"loader": "\\EFI\\nope\\x.efi"}, None),
    ):
        if code is None:
            plan = manager.plan({"session_id": sid, "action_id": action, "params": params})
            assert [p for p in plan["preconditions"] if not p["ok"]]  # a loader that is not on the ESP is flagged and cannot run
            continue
        with pytest.raises(RescueError) as info:
            manager.plan({"session_id": sid, "action_id": action, "params": params})
        assert info.value.short == code


def test_efi_label_is_sanitised(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, _, _ = plan_and_run(manager, sid, "efi.add", {"loader": "\\EFI\\Gentoo\\grubx64.efi", "label": "Evil; rm -rf /"})
    assert ("--label", "Evil rm -rf") == [c for c in lab.system.commands if c[:2] == ("efibootmgr", "--create")][0][6:8]


def test_efi_is_unavailable_from_a_bios_session(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory, uefi=False)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    entry = {a["id"]: a for a in info["actions"]}["efi.add"]
    assert entry["available"] is False and "legacy BIOS mode" in entry["reason"]
    plan = manager.plan({"session_id": sid, "action_id": "efi.add", "params": {"loader": "\\EFI\\Gentoo\\grubx64.efi"}})
    assert [p for p in plan["preconditions"] if not p["ok"]]
    listing = manager.query({"session_id": sid, "query": "efi.list"})["data"]
    assert listing["usable"] is False and "Restart it in UEFI mode" in listing["error"]


def test_the_fallback_boot_file_is_a_copy_with_a_backup(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, result, _ = plan_and_run(manager, sid, "boot.esp_fallback")
    assert any("copy" in step["display"] and "BOOTX64.EFI" in step["display"] for step in plan["steps"]) and result["outcome"] == "success"
    unmount(manager, sid)
    esp = lab.system.device_trees["/dev/nvme0n1p1"]
    assert (esp / "EFI/BOOT/BOOTX64.EFI").stat().st_size == (esp / "EFI/Gentoo/grubx64.efi").stat().st_size
    assert list((esp / "EFI/BOOT").glob("BOOTX64.EFI.gentoo-rescue-*"))


def test_shim_systems_are_not_given_a_hand_made_fallback(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory, "debian")
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    with pytest.raises(RescueError) as info:
        manager.plan({"session_id": sid, "action_id": "boot.esp_fallback"})
    assert info.value.short == "unsupported_action" and "shim" in info.value.message


def test_cancel_stops_between_steps_and_still_runs_the_cleanup(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan = manager.plan({"session_id": sid, "action_id": "fs.check"})
    op = manager.new_op("run", sid)
    original = lab.system.run
    seen = {"n": 0}

    def cancelling(argv, **kwargs):
        seen["n"] += 1
        if seen["n"] == 1:
            op.request_cancel()  # the user pressed Cancel while the first step was running
        return original(argv, **kwargs)

    lab.system.run = cancelling  # type: ignore[method-assign]
    result = manager.run_plan(op, {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True})
    assert result["outcome"] == "cancelled"
    assert ("e2fsck", "-n", "-f", "/dev/nvme0n1p5") not in lab.system.commands  # the check itself never started
    session = manager.session(sid)
    assert session.root in lab.system.mounts  # the cleanup remounted the system


def test_a_forced_cancel_is_reported_with_its_warning(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    op = manager.new_op("run", sid)
    reply = manager.cancel({"job_id": op.id, "force": True})
    assert reply["force"] is True and "half-written" in reply["message"]
    soft = manager.cancel({"job_id": manager.new_op("run", sid).id})
    assert "after the step that is running" in soft["message"]
    with pytest.raises(RescueError) as info:
        manager.cancel({"session_id": "r000000"})
    assert info.value.short == "not_found"


def test_nothing_destructive_was_ever_issued(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory, fstab=DEAD_FSTAB)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    for action, params in (("boot.config", {}), ("boot.initramfs", {}), ("boot.bootloader", {}), ("fstab.fix", {}), ("fs.check", {"mode": "repair"})):
        try:
            plan_and_run(manager, sid, action, params)
        except RescueError:
            pass
    unmount(manager, sid)
    assert_nothing_destructive(lab.system)
    refused = [r for r in manager.session(sid).audit.records if r["kind"] == "refused"] if sid in manager.sessions else []
    assert refused == []


def test_a_forced_cancel_kills_the_step_runs_the_cleanup_and_never_blocks_the_unmount(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    session = manager.session(sid)
    plan = manager.plan({"session_id": sid, "action_id": "fs.check", "params": {"mode": "repair"}})
    op = manager.new_op("run", sid)
    original = lab.system.run

    def killing(argv, **kwargs):
        if argv[:3] == ("e2fsck", "-f", "-p") or (argv[0] == "timeout" and "e2fsck" in argv):
            manager.cancel({"job_id": op.id, "force": True})  # the user chose "Stop now" while the repair ran
        return original(argv, **kwargs)

    lab.system.run = killing  # type: ignore[method-assign]
    result = manager.run_plan(op, {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True, "typed": plan["confirm"]["typed"]})
    assert result["outcome"] == "cancelled" and not session.cancel.is_set()
    assert session.root in lab.system.mounts and lab.system.ro["/dev/nvme0n1p5"] is True  # the cleanup restored the read-only mount
    assert any(r["kind"] == "cancel" for r in session.audit.records)
    lab.system.run = original  # type: ignore[method-assign]
    assert unmount(manager, sid)["leftovers"] == []
    assert lab.system.mounted_targets() == []


def test_a_forced_cancel_during_the_write_transition_returns_to_the_read_only_mount(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    session = manager.session(sid)
    plan = manager.plan({"session_id": sid, "action_id": "boot.config"})
    op = manager.new_op("run", sid)
    original = lab.system.run

    def killing(argv, **kwargs):
        if "-o" in argv and "rw,nodev,nosuid" in argv:
            manager.cancel({"job_id": op.id, "force": True})
        return original(argv, **kwargs)

    lab.system.run = killing  # type: ignore[method-assign]
    result = manager.run_plan(op, {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True})
    assert result["outcome"] == "cancelled"
    lab.system.run = original  # type: ignore[method-assign]
    assert session.mode == "ro" and session.root in lab.system.mounts
    assert unmount(manager, sid)["leftovers"] == []


def test_fix_everything_recommended_is_one_reviewed_plan(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory, initramfs=False)
    (tree / "boot/grub/grub.cfg").unlink()
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    spec = {a["id"]: a for a in info["actions"]}["repair.recommended"]
    assert spec["available"] and spec["recommended"] and spec["risk"] == "high"  # the catalogue states the worst case; the plan states its own
    plan = manager.plan({"session_id": sid, "action_id": "repair.recommended"})
    assert plan["title"] == "Fix everything recommended" and plan["params"]["actions"] == ["boot.initramfs", "boot.config"] and plan["risk"] == "medium"
    ids = [step["id"] for step in plan["steps"]]
    assert len(ids) == len(set(ids)), "merged steps need unique ids"
    assert ids.count("smoke") == 1 and ids.count("enter-write-mount-root") == 1  # one read-write switch and one smoke test for the whole plan
    shown = [step["display"] for step in plan["steps"]]
    assert next(i for i, d in enumerate(shown) if "dracut" in d) < next(i for i, d in enumerate(shown) if "grub-mkconfig" in d)  # the menu is generated after the initramfs exists
    plan, result, _ = plan_and_run(manager, sid, "repair.recommended")
    assert result["outcome"] == "success" and result["post_checks"] and all(check["ok"] for check in result["post_checks"])
    unmount(manager, sid)
    assert (tree / "boot/grub/grub.cfg").exists() and (tree / f"boot/initramfs-{fr.GENTOO_KVER}.img").exists()


def test_fix_everything_recommended_fixes_fstab_first_and_says_what_it_left_out(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory, fstab=DEAD_FSTAB, initramfs=False)
    (tree / "boot/grub/grub.cfg").unlink()
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan = manager.plan({"session_id": sid, "action_id": "repair.recommended"})
    assert plan["params"]["actions"] == ["fstab.fix", "boot.initramfs"]
    assert plan["risk"] == "high" and plan["confirm"]["typed"] == "nvme0n1p5"  # the worst step decides how much confirmation is asked
    assert any(w.startswith("Skipped: Reinstall the boot loader") and "EFI system partition" in w for w in plan["warnings"])  # the user is told why, not left guessing
    shown = [step["display"] for step in plan["steps"]]
    assert next(i for i, d in enumerate(shown) if d.startswith("write /etc/fstab")) < next(i for i, d in enumerate(shown) if "dracut" in d)
    assert plan["diff"].startswith("--- a/etc/fstab")
    plan, result, _ = plan_and_run(manager, sid, "repair.recommended")
    assert result["outcome"] == "success" and all(check["ok"] for check in result["post_checks"])
    unmount(manager, sid)
    assert (tree / f"boot/initramfs-{fr.GENTOO_KVER}.img").exists()
    assert f"UUID={fr.ESP_UUID}\t/efi" in (tree / "etc/fstab").read_text()


def test_nothing_recommended_means_no_composite_plan(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert {a["id"]: a for a in info["actions"]}["repair.recommended"]["available"] is False
    with pytest.raises(RescueError) as err:
        manager.plan({"session_id": sid, "action_id": "repair.recommended"})
    assert err.value.short == "unsupported_action"


def test_repairing_a_multi_device_btrfs_makes_every_member_writable_and_restores_them(lab_factory, tmp_path: Path) -> None:
    lab = lab_factory(lf.btrfs_multi_device())
    top = fr.make_root(tmp_path / "top", "gentoo", fstab=f"UUID={fr.ROOT_UUID} / btrfs noatime 0 0\n")
    lab.system.attach("/dev/sda1", top)
    lab.system.subvolumes["/dev/sda1"] = {5: ""}
    lab.system.btrfs_text["/dev/sda1"] = ""
    lab.system.btrfs_default["/dev/sda1"] = "ID 5 (FS_TREE)"
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/sda1")
    plan = manager.plan({"session_id": sid, "action_id": "fs.check", "params": {"mode": "repair_force"}})
    shown = [step["display"] for step in plan["steps"]]
    assert shown.index("blockdev --setrw /dev/sda1") < shown.index("blockdev --setrw /dev/sdb1") < shown.index("btrfs rescue zero-log /dev/sda1")  # review R7a/c
    assert [step["display"] for step in plan["cleanup"]][:2] == ["blockdev --setro /dev/sda1", "blockdev --setro /dev/sdb1"]
    assert plan["risk"] == "dangerous" and plan["confirm"]["phrase"] == "I HAVE A BACKUP"
    plan, result, _ = plan_and_run(manager, sid, "fs.check", {"mode": "repair_force"})
    assert result["outcome"] == "success"
    assert lab.system.ro["/dev/sda1"] is True and lab.system.ro["/dev/sdb1"] is True  # the cleanup protected them again
    unmount(manager, sid)
    assert not any(lab.system.ro.values())


def test_xfs_repair_names_the_external_log(lab_factory) -> None:
    lab = lab_factory(lf.xfs_external_log())
    lab.add_tree("/dev/sda2", "gentoo", firmware="uefi", fstab=f"UUID={fr.ROOT_UUID} / xfs noatime 0 1\n")
    manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/sda2")
    plan = manager.plan({"session_id": sid, "action_id": "fs.check", "params": {"mode": "repair"}})
    commands = [step["argv"] for step in plan["steps"] if step.get("argv") and step["argv"][0] == "xfs_repair"]
    assert commands == [["xfs_repair", "-l", "/dev/sdb1", "/dev/sda2"]]
    check = manager.plan({"session_id": sid, "action_id": "fs.check", "params": {"mode": "check"}})
    assert [step["argv"] for step in check["steps"] if step.get("argv") and step["argv"][0] == "xfs_repair"] == [["xfs_repair", "-n", "-l", "/dev/sdb1", "/dev/sda2"]]
