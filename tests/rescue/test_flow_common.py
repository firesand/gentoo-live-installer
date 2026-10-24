"""The repairs that do not depend on the boot loader: fstab, file system checks, accounts, EFI entries, fallback boot file, cancel."""

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


AUTHOR_FSTAB = (f"UUID={fr.ROOT_UUID}\t/\text4\tnoatime\t0 1\nUUID={fr.DEAD_UUID}\t/efi\tvfat\tumask=0077\t0 2\n"
                "UUID=97af3e70-654c-4d96-8f88-32b2e9753e75\t/mnt/Extern2\text4\trw,noatime,nofail\t0 2\nUUID=0d8acaaf-9bd7-4bac-8dd1-cad07ff15f48\t/mnt/Extern3\text4\trw,noatime,nofail\t0 2\n"
                "UUID=11111111-aaaa-4bbb-8ccc-222222222222\t/mnt/Old\text4\tnoatime\t0 2\n")


def test_fstab_fix_leaves_unplugged_nofail_drives_alone(lab_factory) -> None:
    """A wrong ESP UUID puts fstab.fix into 'Fix everything', which processes every dead line: the external drives that are only not plugged in must keep their lines."""
    lab, tree = uefi_gentoo_lab(lab_factory, fstab=AUTHOR_FSTAB)
    (tree / "efi").mkdir(exist_ok=True)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    assert "fstab.fix" in info["recommended"]
    plan = manager.plan({"session_id": sid, "action_id": "fstab.fix"})
    changed = [line for line in plan["diff"].splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    assert f"+UUID={fr.ESP_UUID}\t/efi\tvfat\tumask=0077\t0 2" in changed
    assert not any("Extern" in line for line in changed)  # not touched (they only show as context around the lines that are)
    assert "+UUID=11111111-aaaa-4bbb-8ccc-222222222222\t/mnt/Old\text4\tnoatime,nofail\t0 2" in changed  # a drive without nofail is relaxed, as before
    assert any("/mnt/Old" in warning and "'nofail'" in warning for warning in plan["warnings"])
    assert not any("Extern" in warning for warning in plan["warnings"])
    kept = [note for note in plan["notes"] if "left as it is" in note]
    assert len(kept) == 2 and "/mnt/Extern2" in kept[0] and "/mnt/Extern3" in kept[1] and all("nofail" in note for note in kept)  # the person is told what was not touched
    assert {line: choice["action"] for line, choice in plan["params"]["choices"].items()} == {"2": "replace", "3": "keep", "4": "keep", "5": "nofail"}
    plan, result, _ = plan_and_run(manager, sid, "fstab.fix")
    assert result["outcome"] == "success" and all(check["ok"] for check in result["post_checks"])
    unmount(manager, sid)
    text = (tree / "etc/fstab").read_text()
    assert "/mnt/Extern2\text4\trw,noatime,nofail\t0 2" in text and "/mnt/Extern3\text4\trw,noatime,nofail\t0 2" in text and "# disabled" not in text


def test_fstab_fix_says_so_when_every_dead_entry_is_already_harmless(lab_factory) -> None:
    fstab = (f"UUID={fr.ROOT_UUID}\t/\text4\tnoatime\t0 1\nUUID=97af3e70-654c-4d96-8f88-32b2e9753e75\t/mnt/Extern2\text4\trw,noatime,nofail\t0 2\n"
             f"UUID=0d8acaaf-9bd7-4bac-8dd1-cad07ff15f48\t/mnt/Extern3\text4\tnoauto\t0 2\n")
    lab, _tree = uefi_gentoo_lab(lab_factory, fstab=fstab)
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    spec = {a["id"]: a for a in info["actions"]}["fstab.fix"]
    assert spec["available"] is False and "nofail" in spec["reason"]
    with pytest.raises(RescueError) as failed:
        manager.plan({"session_id": sid, "action_id": "fstab.fix"})
    assert failed.value.short == "unsupported_action"
    # the person may still choose a line on purpose
    plan = manager.plan({"session_id": sid, "action_id": "fstab.fix", "params": {"choices": {"2": {"action": "comment"}}}})
    changed = [line for line in plan["diff"].splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    assert "+# disabled by gentoo-rescue: device not found" in changed and not any("Extern3" in line for line in changed if not line.startswith("-"))
    unmount(manager, sid)


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


KERNEL_NAMED_FSTAB = f"UUID={fr.ROOT_UUID}\t/\text4\tnoatime\t0 1\n/dev/sdb1\t/boot\text4\tnoatime\t0 2\n"


def test_a_kernel_named_entry_on_another_disk_is_neither_mounted_nor_made_writable(lab_factory) -> None:
    """The reviewed case end to end: fstab says /dev/sdb1 for /boot, and /dev/sdb is an unrelated disk of this live session. Nothing on it may change."""
    from gentoo_installerd.rescue.scan import ScanOptions

    from .fakes import tree_digest

    lab, tree = uefi_gentoo_lab(lab_factory, fstab=KERNEL_NAMED_FSTAB)
    other = lab.system.device_trees["/dev/sdb1"]
    before = tree_digest(other)
    manager = lab.manager()
    scan = manager.scan(None, ScanOptions())
    request = {"candidate": next(c["id"] for c in scan["candidates"] if c["device"] == "/dev/nvme0n1p5"), "scan_id": scan["scan_id"], "allow_write": True}
    dry = manager.mount(None, {**request, "dry_run": True})["mount_plan"]
    assert dry["items"] == [] and [(entry["mountpoint"], entry["status"], entry["device"]) for entry in dry["unresolved"]] == [("/boot", "unstable", "/dev/sdb1")]
    assert not any("/dev/sdb1" in line for line in dry["commands"]) and (dry["suggestions"][0]["mountpoint"], dry["suggestions"][0]["device"]) == ("/boot", "/dev/sdb1")
    sid = manager.mount(None, {**request, "plan_digest": dry["digest"]})["session_id"]
    session = manager.session(sid)
    assert session.plan.devices() == ["/dev/nvme0n1p5"] and "/dev/sdb1" not in session.ctx.devices.devices
    assert lab.system.ro["/dev/sdb"] is True  # the unrelated disk is protected like any other
    assert not any("/dev/sdb" in step.display for step in session.enter_write_steps())
    plan_and_run(manager, sid, "boot.config")
    unmount(manager, sid)
    assert tree_digest(other) == before and not any(lab.system.ro.values())
    assert not [argv for argv in lab.system.commands_named("mount") if argv[-2] == "/dev/sdb1" and argv[-1].startswith(session.root)]  # only the scan's read-only look at it
    assert (tree / "boot/grub/grub.cfg").read_text().count("menuentry") >= 1  # the repair went to the /boot directory of the system's own root


def test_choosing_the_kernel_named_device_anyway_mounts_it(lab_factory) -> None:
    from gentoo_installerd.rescue.scan import ScanOptions

    lab, _tree = uefi_gentoo_lab(lab_factory, fstab=KERNEL_NAMED_FSTAB)
    manager = lab.manager()
    scan = manager.scan(None, ScanOptions())
    request = {"candidate": next(c["id"] for c in scan["candidates"] if c["device"] == "/dev/nvme0n1p5"), "scan_id": scan["scan_id"], "allow_write": True,
               "overrides": [{"mountpoint": "/boot", "device": "/dev/sdb1"}]}
    dry = manager.mount(None, {**request, "dry_run": True})["mount_plan"]
    assert [(item["mountpoint"], item["device"], item["source"]) for item in dry["items"]] == [("/boot", "/dev/sdb1", "user")] and dry["unresolved"] == []
    sid = manager.mount(None, {**request, "plan_digest": dry["digest"]})["session_id"]
    assert manager.session(sid).plan.devices() == ["/dev/nvme0n1p5", "/dev/sdb1"]
    unmount(manager, sid)


def test_a_file_system_check_after_a_repair_leaves_no_empty_resolv_conf_behind(lab_factory) -> None:
    """The check unmounts the bind and the system and mounts it read-only again; the placeholder made for the bind has to go before the read-write root does."""
    lab, tree = uefi_gentoo_lab(lab_factory)
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan_and_run(manager, sid, "boot.initramfs")
    session = manager.session(sid)
    assert [entry.data["path"] for entry in session.ledger.of_kind("placeholder")] == ["/etc/resolv.conf"]
    plan, result, _ = plan_and_run(manager, sid, "fs.check")
    assert result["outcome"] == "success"
    assert session.ledger.of_kind("placeholder") == [] and session.mode == "ro"
    assert not (tree / "etc/resolv.conf").exists()
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


def test_systems_that_start_through_a_shim_are_not_given_a_hand_made_fallback(lab_factory) -> None:
    lab, _ = uefi_gentoo_lab(lab_factory)
    fr.put(lab.system.device_trees["/dev/nvme0n1p1"], "EFI/Gentoo/shimx64.efi", size=1 << 20)  # a signed shim chain that was set up by hand
    manager, sid, _ = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    with pytest.raises(RescueError) as info:
        manager.plan({"session_id": sid, "action_id": "boot.esp_fallback"})
    assert info.value.short == "unsupported_action" and "shim" in info.value.message and "Secure Boot" in info.value.message


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
    assert plan["title"] == "Fix everything recommended" and plan["params"] == {"actions": ["boot.initramfs", "boot.config"], "kernels": [fr.GENTOO_KVER]} and plan["risk"] == "medium"
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
    lab.system.external_logs["/dev/sda2"] = "/dev/sdb1"
    manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/sda2")
    plan = manager.plan({"session_id": sid, "action_id": "fs.check", "params": {"mode": "repair"}})
    commands = [step["argv"] for step in plan["steps"] if step.get("argv") and step["argv"][0] == "xfs_repair"]
    assert commands == [["xfs_repair", "-l", "/dev/sdb1", "/dev/sda2"]]
    check = manager.plan({"session_id": sid, "action_id": "fs.check", "params": {"mode": "check"}})
    assert [step["argv"] for step in check["steps"] if step.get("argv") and step["argv"][0] == "xfs_repair"] == [["xfs_repair", "-n", "-l", "/dev/sdb1", "/dev/sda2"]]


def test_a_missing_mount_point_directory_is_explained_and_repairs_create_it(lab_factory) -> None:
    import shutil

    lab, tree = uefi_gentoo_lab(lab_factory)
    shutil.rmtree(tree / "efi")
    manager, sid, info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    m01 = [f for f in info["findings"] if f["id"] == "M01"]
    assert len(m01) == 1 and m01[0]["severity"] == "warn" and m01[0]["actions"] == [] and m01[0]["detail"].startswith("The directory /efi does not exist in the system.")
    assert "fs.check" not in info["recommended"]  # the file system is fine; only the directory is gone
    with pytest.raises(RescueError) as err:
        manager.plan({"session_id": sid, "action_id": "boot.bootloader"})
    assert err.value.short == "unsupported_action" and "The directory /efi is missing" in err.value.message and "Enable repairs first" in err.value.message
    plan_and_run(manager, sid, "session.enter_write")
    assert [m.data["device"] for m in manager.session(sid).ledger.of_kind("mount") if m.data["mountpoint"] == "/efi"] == ["/dev/nvme0n1p1"]
    plan = manager.plan({"session_id": sid, "action_id": "boot.bootloader"})
    assert plan["action"] == "boot.bootloader" and any("grub-install" in step["display"] for step in plan["steps"])
    unmount(manager, sid)
    assert (tree / "efi").is_dir()  # the directory was created in the repaired system


def test_the_output_of_a_command_reaches_the_client_once_while_the_plan_runs(lab_factory) -> None:
    lab, tree = uefi_gentoo_lab(lab_factory, initramfs=False)
    lab.system.chroot_effects["dracut"] = lambda system, root, command, extra: (0, "dracut: Executing: dracut --force\ndracut: *** Creating image file ***\ndracut: *** Creating initramfs image file done ***\n")
    manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan = manager.plan({"session_id": sid, "action_id": "boot.initramfs"})
    op = manager.new_op("run", sid)
    manager.run_plan(op, {"session_id": sid, "plan_id": plan["plan_id"], "digest": plan["digest"], "confirm": True})
    events = op.buffer.since(0)
    lines = [event.message for event in events if event.type == "log"]
    assert lines.count("dracut: *** Creating image file ***") == 1 and not any(line.startswith("$ ") for line in lines)  # streamed, not repeated at the end of the step; the command line is in the plan
    running = next(i for i, e in enumerate(events) if e.type == "stage" and e.data.get("step", "").startswith("c") and e.data.get("status") == "running" and "dracut" in e.message)
    done = next(i for i, e in enumerate(events) if e.type == "stage" and e.data.get("status") == "completed" and "dracut" in e.message)
    assert running < next(i for i, e in enumerate(events) if e.message == "dracut: *** Creating image file ***") < done
    unmount(manager, sid)


def test_secret_output_is_never_streamed(lab_factory) -> None:
    lab, _tree = uefi_gentoo_lab(lab_factory)
    lab.system.chroot_effects["chpasswd"] = lambda system, root, command, extra: (0, "chpasswd: password for 'root' changed to hunter2\n")
    manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    plan, result, op = plan_and_run(manager, sid, "acct.password", {"username": "root"}, secrets={"password": "hunter2"})
    assert result["outcome"] == "success"
    assert "hunter2" not in " ".join(event.message for event in op.buffer.since(0)) + str(result)
    assert "hunter2" not in manager.audit_text({"session_id": sid})["text"] + manager.audit.text("jsonl")
    unmount(manager, sid)


def test_a_dirty_xfs_log_is_not_a_failed_check_and_the_user_is_told_how_to_replay_it(lab_factory) -> None:
    lab = lab_factory(lf.xfs_boot_esp())
    tree = lab.add_tree("/dev/sda3", "gentoo", firmware="uefi", fstab=f"UUID={fr.ROOT_UUID} / xfs noatime 0 1\n")
    lab.system.script(("xfs_repair", "-n"), "ERROR: The filesystem has valuable metadata changes in a log which needs to be replayed.\n", rc=2)
    manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/sda3")
    plan, result, _ = plan_and_run(manager, sid, "fs.check")
    assert result["outcome"] == "success" and [n for n in plan["notes"] if "replays it" in n and "-L" in n]  # shown with the plan, before it is confirmed
    unmount(manager, sid)
