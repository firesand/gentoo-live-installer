"""Session mounting, write transition, virtual file systems, release and the kernel read-only flags."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gentoo_installerd.rescue.errors import RescueError
from gentoo_installerd.rescue.session import parse_fuser, session_from_state

from . import fakeroot as fr
from . import lsblk_fixtures as lf
from .fakes import assert_nothing_destructive, tree_digest


def gentoo_lab(lab_factory, **kwargs):
    """A UEFI Gentoo on nvme0n1p5 with its ESP (nvme0n1p1 -> /efi) and an unrelated second disk."""
    data = json.loads(lf.nvme_dualboot())
    data["blockdevices"].append(lf.sata_disk("b", 1, 500 * lf.GiB, [lf.part("sdb", 1, 500 * lf.GiB, 8, 1, fstype="ext4", uuid="22222222-2222-4222-8222-222222222222", parttype=lf.LINUX_FS, pttype="gpt")]))
    lab = lab_factory(json.dumps(data), **kwargs)
    lab.add_tree("/dev/nvme0n1p5", "gentoo", firmware="uefi")
    esp = fr.esp_tree(lab.tmp / "trees" / "esp", "gentoo")
    lab.system.attach("/dev/nvme0n1p1", esp)
    lab.add_tree("/dev/sdb1", "debian")
    return lab


def mounts_of(lab) -> list[tuple[str, ...]]:
    return [argv for argv in lab.system.commands if argv[0] in {"mount", "umount"}]


def test_mount_ro_command_sequence_and_ledger(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    root = session.root
    assert session.mode == "ro" and session.state == "mounted_ro" and session.generation == 1
    assert mounts_of(lab) == [
        ("mount", "-t", "ext4", "-o", "ro,noload,nodev,nosuid", "/dev/nvme0n1p5", root),
        ("mount", "-t", "vfat", "-o", "ro,umask=0077,nodev,nosuid", "/dev/nvme0n1p1", root + "/efi"),
    ]
    assert [(e.kind, e.data["mountpoint"], e.data["rw"]) for e in session.ledger.of_kind("mount")] == [("mount", "/", False), ("mount", "/efi", False)]
    assert oct(os.stat(root).st_mode & 0o777) in {"0o700", "0o755"}  # the mount point is created private (the mounted root shows its own mode)
    assert (Path(root) / "etc/os-release").exists() and (Path(root) / "efi/EFI/Gentoo/grubx64.efi").exists()
    assert_nothing_destructive(lab.system)


def test_selected_devices_are_flagged_read_only_and_other_disks_protected(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    flagged = {entry.data["device"]: entry.data["purpose"] for entry in session.ledger.of_kind("blockro")}
    assert flagged["/dev/nvme0n1p5"] == "session" and flagged["/dev/nvme0n1p1"] == "session"
    assert flagged["/dev/sdb"] == "protect"  # the unrelated whole disk (review R4)
    assert "/dev/nvme0n1" not in flagged  # the disk of the selected stack stays writable
    assert flagged["/dev/nvme0n1p3"] == "protect" and flagged["/dev/nvme0n1p6"] == "protect"  # sibling partitions of the selected disk
    assert lab.system.ro["/dev/sdb"] is True and lab.system.ro["/dev/nvme0n1p5"] is True
    assert "/dev/sdb1" not in flagged  # covered by its whole disk
    result = session.release()
    assert result.complete
    assert not any(lab.system.ro.values())  # everything restored


def test_hotplug_live_and_busy_disks_are_not_protected(lab_factory) -> None:
    data = json.loads(lf.nvme_dualboot())
    stick = lf.sata_disk("c", 2, 32 * lf.GiB, [lf.part("sdc", 1, 32 * lf.GiB, 8, 2, fstype="vfat", uuid="AAAA-0001", parttype=lf.MS_BASIC, pttype="gpt")], hotplug=True, tran="usb")
    busy = lf.sata_disk("d", 3, 100 * lf.GiB, [lf.part("sdd", 1, 100 * lf.GiB, 8, 3, fstype="ext4", uuid="33333333-3333-4333-8333-333333333333", parttype=lf.LINUX_FS, pttype="gpt",
                                                      mountpoints=["/run/media/gentoo/data"])])
    data["blockdevices"].extend([stick, busy])
    lab = lab_factory(json.dumps(data))
    lab.add_tree("/dev/nvme0n1p5", "gentoo", firmware="uefi")
    lab.system.attach("/dev/nvme0n1p1", fr.esp_tree(lab.tmp / "trees" / "esp", "gentoo"))
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    protected = {e.data["device"] for e in session.ledger.of_kind("blockro") if e.data["purpose"] == "protect"}
    assert "/dev/sdc" not in protected and "/dev/sdd" not in protected


def test_device_already_read_only_is_left_as_found(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    lab.system.ro["/dev/nvme0n1p1"] = True
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    session.release()
    assert lab.system.ro["/dev/nvme0n1p1"] is True and lab.system.ro["/dev/nvme0n1p5"] is False
    assert ("blockdev", "--setrw", "/dev/nvme0n1p1") not in lab.system.commands


def test_release_is_in_reverse_order_and_removes_the_state(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    assert session.state_path.exists() and oct(session.state_path.stat().st_mode & 0o777) == "0o600"
    result = session.release()
    assert result.complete and result.released[:2] == [session.root + "/efi", session.root]
    umounts = lab.system.commands_named("umount")
    assert umounts == [("umount", session.root + "/efi"), ("umount", session.root)]
    assert lab.system.mounted_targets() == [] and not session.state_path.exists() and not os.path.exists(session.root)
    assert session.state == "closed" and len(session.ledger) == 0


def test_busy_mounts_stop_the_release_and_report_holders(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    lab.system.fail_umount[session.root + "/efi"] = (32, "umount: busy: target is busy.")
    lab.system.holders[session.root + "/efi"] = ("                     USER        PID ACCESS COMMAND\n" + f"{session.root}/efi:      gentoo     4321 ..c.. thunar\n")
    result = session.release()
    assert not result.complete and result.leftovers == [session.root + "/efi"]
    assert result.busy_holders == [{"user": "gentoo", "pid": 4321, "access": "..c..", "command": "thunar"}]
    assert session.root in lab.system.mounts and session.state == "closing" and session.state_path.exists()
    assert not [c for c in lab.system.commands if c[:2] == ("blockdev", "--setrw")]  # flags stay while anything is mounted
    assert len(lab.system.commands_with("umount", session.root + "/efi")) == 3  # retried
    forced = session.release(force=True)
    assert forced.complete and ("umount", "-l", session.root + "/efi") in lab.system.commands
    assert any(r["message"] == "lazy unmount" for r in session.audit.records if r["kind"] == "release")


def test_a_failing_root_mount_leaves_nothing_behind(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    lab.system.fail_mount["/dev/nvme0n1p5"] = "mount: wrong fs type, bad option, bad superblock on /dev/nvme0n1p5, can't read superblock"
    session = lab.session("/dev/nvme0n1p5")
    with pytest.raises(RescueError) as info:
        session.mount_ro(lab.graph_obj)
    assert info.value.short == "degraded" and "file system check" in info.value.message
    assert lab.system.mounted_targets() == [] and not any(lab.system.ro.values()) and not os.path.exists(session.root) and not session.state_path.exists()


def test_a_failing_boot_mount_is_a_warning_not_a_failure(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    lab.system.fail_mount["/dev/nvme0n1p1"] = "mount: /x: wrong fs type, bad option, bad superblock on /dev/nvme0n1p1"
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    assert session.mounted and [m["mountpoint"] for m in session.failed_mounts] == ["/efi"]
    assert any("/efi could not be mounted" in w for w in session.warnings)
    assert session.release().complete


def test_inspect_only_mounts_are_noexec_and_refuse_the_write_transition(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    session = lab.session("/dev/nvme0n1p5", allow_write=False)
    session.mount_ro(lab.graph_obj)
    assert all("noexec" in argv[4].split(",") for argv in lab.system.commands_named("mount"))
    with pytest.raises(RescueError) as info:
        session.enter_write_steps()
    assert info.value.short == "needs_write"


def test_the_write_transition_is_a_visible_command_list(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    r = session.root
    displays = [step.display for step in session.enter_write_steps(with_virtual=False)]
    assert displays == [
        "Check that no terminal is open and no other step is running",
        f"umount {r}/efi", f"umount {r}",
        "blockdev --setrw /dev/nvme0n1p5", "blockdev --setrw /dev/nvme0n1p1",
        f"mount -t ext4 -o rw,nodev,nosuid /dev/nvme0n1p5 {r}",
        f"mount -t vfat -o rw,umask=0077,nodev,nosuid /dev/nvme0n1p1 {r}/efi",
    ]


def run_all(session, steps) -> None:
    for step in steps:
        rc, _out = session.run_step(step, session.runner)
        assert rc in step.ok_rc, step.display


def test_executing_the_write_transition_switches_to_read_write(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    before = session.generation
    run_all(session, session.enter_write_steps(with_virtual=False))
    assert session.mode == "rw" and session.state == "mounted_rw" and session.generation > before
    assert session.ledger.find("mount", mountpoint="/").data["rw"] is True
    assert lab.system.ro["/dev/nvme0n1p5"] is False and lab.system.ro["/dev/nvme0n1p1"] is False
    assert lab.system.ro["/dev/sdb"] is True  # unrelated disks stay protected
    assert session.ledger.find("mount", mountpoint="/efi").data["rw"] is True
    rc, out = session.run_step(__import__("gentoo_installerd.rescue.plan", fromlist=["Step"]).Step(
        "f1", "file", "write", file_op={"op": "write", "path": "/etc/hostname", "content": "rescued\n", "mode": 0o644, "create": True}), session.runner)
    assert (Path(session.root) / "etc/hostname").read_text() == "rescued\n"
    result = session.release()
    assert result.complete
    assert (lab.system.device_trees["/dev/nvme0n1p5"] / "etc/hostname").read_text() == "rescued\n"  # written back to the device on unmount
    assert not any(lab.system.ro.values())


def test_every_member_device_of_the_root_is_unprotected_and_made_writable_together(lab_factory) -> None:
    lab = lab_factory(lf.xfs_external_log())
    lab.add_tree("/dev/sda2", "gentoo", firmware="uefi", fstab=f"UUID={fr.ROOT_UUID} / xfs noatime 0 1\n")
    lab.system.external_logs["/dev/sda2"] = "/dev/sdb1"  # the kernel refuses to mount this file system unless its log is named
    session = lab.session("/dev/sda2")
    session.mount_ro(lab.graph_obj)
    flagged = {entry.data["device"]: entry.data["purpose"] for entry in session.ledger.of_kind("blockro")}
    assert flagged["/dev/sda2"] == "session" and flagged["/dev/sdb1"] == "session"  # the external log is part of the file system (review R7c)
    assert flagged["/dev/sdc"] == "protect" and "/dev/sdb" not in flagged and "/dev/sda" not in flagged
    mount_root = lab.system.commands_named("mount")[0]
    assert mount_root[:3] == ("mount", "-t", "xfs") and "logdev=/dev/sdb1" in mount_root[4].split(",") and mount_root[5:] == ("/dev/sda2", session.root)
    displays = [step.display for step in session.enter_write_steps(with_virtual=False)]
    assert displays[2:4] == ["blockdev --setrw /dev/sda2", "blockdev --setrw /dev/sdb1"]
    assert displays[-1].startswith("mount -t xfs -o rw,logdev=/dev/sdb1,nodev,nosuid /dev/sda2 ")
    run_all(session, session.enter_write_steps(with_virtual=False))
    assert lab.system.ro["/dev/sda2"] is False and lab.system.ro["/dev/sdb1"] is False and lab.system.ro["/dev/sdc"] is True
    leave = [step.display for step in session.leave_write_steps()]
    assert "blockdev --setro /dev/sda2" in leave and "blockdev --setro /dev/sdb1" in leave
    assert session.release().complete and not any(lab.system.ro.values())


def test_a_btrfs_raid_root_keeps_all_of_its_disks_writable(lab_factory) -> None:
    lab = lab_factory(lf.btrfs_multi_device())
    lab.add_tree("/dev/sda1", "arch", firmware="uefi", fstab=f"UUID={fr.ROOT_UUID} / btrfs subvol=@,noatime 0 0\n")
    lab.system.subvolumes["/dev/sda1"] = {5: "", 256: ""}
    session = lab.session("/dev/sda1", rootflags="subvolid=256")
    session.mount_ro(lab.graph_obj)
    flagged = {entry.data["device"]: entry.data["purpose"] for entry in session.ledger.of_kind("blockro")}
    assert flagged == {"/dev/sda1": "session", "/dev/sdb1": "session"}  # the second disk is not "another disk": it is flagged with the file system, not protected
    assert ("blockdev", "--setrw", "/dev/sdb1") in [step.argv for step in session.enter_write_steps(with_virtual=False)]
    assert session.release().complete and not any(lab.system.ro.values())


def test_file_steps_are_refused_in_a_read_only_session(lab_factory) -> None:
    from gentoo_installerd.rescue.plan import Step

    lab = gentoo_lab(lab_factory)
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    with pytest.raises(RescueError) as info:
        session.run_step(Step("f", "file", "x", file_op={"op": "write", "path": "/etc/x", "content": "x"}), session.runner)
    assert info.value.short == "needs_write"


def test_failed_remount_leaves_a_consistent_session(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    steps = session.enter_write_steps(with_virtual=False)
    lab.system.fail_mount["/dev/nvme0n1p5"] = "mount: /x: wrong fs type, bad option, bad superblock on /dev/nvme0n1p5"
    with pytest.raises(Exception):
        run_all(session, steps)
    # the executor rolls back; at this level the ledger must not claim mounts that do not exist
    for entry in session.ledger.of_kind("mount"):
        assert entry.data["path"] in lab.system.mounts


def test_virtual_filesystems_and_the_synthesized_command_line(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    assert session.ensure_virtual()
    r = session.root
    commands = [argv for argv in lab.system.commands if argv[0] == "mount"][2:]
    assert commands[:8] == [
        ("mount", "-t", "proc", "proc", r + "/proc"),
        ("mount", "--rbind", "/sys", r + "/sys"), ("mount", "--make-rslave", r + "/sys"),
        ("mount", "--rbind", "/dev", r + "/dev"), ("mount", "--make-rslave", r + "/dev"),
        ("mount", "-t", "tmpfs", "-o", "mode=0755,nosuid,nodev", "tmpfs", r + "/run"),
        ("mount", "--bind", "/run/udev", r + "/run/udev"), ("mount", "-o", "remount,bind,ro", r + "/run/udev"),
    ]
    cmdline = [argv for argv in lab.system.commands if argv[:2] == ("mount", "--bind") and argv[-1].endswith("/proc/cmdline")]
    assert cmdline and cmdline[0][2] == str(session.cmdline_path)
    text = session.cmdline_path.read_text()
    assert "root=live" not in text and "rd.live" not in text and f"root=UUID={fr.ROOT_UUID}" in text
    assert session.ledger.find("cmdline") is not None
    result = session.release()
    assert result.complete and lab.system.mounted_targets() == []
    umounts = lab.system.commands_named("umount")
    assert umounts.index(("umount", r + "/proc/cmdline")) < umounts.index(("umount", r + "/proc"))


def test_resolv_conf_is_bound_not_replaced(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    fr.put(lab.host_root, "etc/resolv.conf", "nameserver 192.168.1.1\n")
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    original = lab.system.device_trees["/dev/nvme0n1p5"]
    before = tree_digest(original)
    assert session.ensure_virtual()
    binds = [argv for argv in lab.system.commands if argv[:2] == ("mount", "--bind") and argv[-1].endswith("resolv.conf")]
    assert binds and binds[0][2] == os.path.realpath(lab.host_root / "etc/resolv.conf")
    assert ("mount", "-o", "remount,bind,ro", binds[0][-1]) in lab.system.commands
    session.release()
    assert tree_digest(original) == before  # the target's own file was never modified


def test_resolv_conf_symlink_into_a_fresh_run_is_followed_chroot_style(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    fr.put(lab.host_root, "run/NetworkManager/resolv.conf", "nameserver 10.0.0.1\n")
    tree = lab.system.device_trees["/dev/nvme0n1p5"]
    (tree / "etc/resolv.conf").symlink_to("../run/systemd/resolve/stub-resolv.conf")
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    assert session.ensure_virtual()
    bind = [argv for argv in lab.system.commands if argv[:2] == ("mount", "--bind") and "stub-resolv" in argv[-1]]
    assert bind and bind[0][-1] == session.root + "/run/systemd/resolve/stub-resolv.conf"
    assert session.ledger.of_kind("placeholder") == []  # created on the tmpfs, nothing to delete from the root file system
    session.release()


def test_without_any_dns_on_the_live_system_public_resolvers_are_used(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    assert session.ensure_virtual()
    assert "nameserver 1.1.1.1" in session.resolv_path.read_text()
    assert any("public resolvers" in w for w in session.warnings)
    session.release()


def test_virtual_mounts_refuse_a_target_that_hides_proc_behind_a_symlink(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    tree = lab.system.device_trees["/dev/nvme0n1p5"]
    (tree / "proc").rmdir()
    (tree / "proc").symlink_to("/etc")
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    assert session.ensure_virtual() is False
    assert any("not a plain directory" in w for w in session.warnings)
    assert not [argv for argv in lab.system.commands if argv[:3] == ("mount", "-t", "proc")]
    assert session.release().complete


def test_mountpoints_are_resolved_inside_the_target_never_on_the_live_system(lab_factory, tmp_path: Path) -> None:
    """A hostile /efi (absolute symlink to a live directory) must not make rescue mount over that directory (review R2)."""
    lab = gentoo_lab(lab_factory)
    live_dir = tmp_path / "live-dir"
    live_dir.mkdir()
    tree = lab.system.device_trees["/dev/nvme0n1p5"]
    (tree / "efi").rmdir()
    (tree / "efi").symlink_to(str(live_dir))
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    assert [m["mountpoint"] for m in session.failed_mounts] == ["/efi"]
    assert not [argv for argv in lab.system.commands_named("mount") if str(live_dir) in argv]
    assert list(live_dir.iterdir()) == []
    session.release()


def test_a_mount_point_swapped_after_the_plan_is_refused(lab_factory, tmp_path: Path) -> None:
    """Between the plan and the mount the directory is replaced by a link: the mount is re-resolved and refused (review R2)."""
    lab = gentoo_lab(lab_factory)
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    steps = session.enter_write_steps(with_virtual=False)
    efi_step = [s for s in steps if s.mountpoint == "/efi" and s.argv[0] == "mount"][0]
    for step in steps[: steps.index(efi_step)]:
        session.run_step(step, session.runner)
    efi = Path(session.root) / "efi"
    live_dir = tmp_path / "live-target"
    live_dir.mkdir()
    efi.rmdir()
    efi.symlink_to(str(live_dir))
    before = list(lab.system.commands)
    with pytest.raises((RescueError, OSError)):
        session.run_step(efi_step, session.runner)
    assert lab.system.commands == before  # no mount command was even attempted
    assert list(live_dir.iterdir()) == []  # nothing was mounted over, or created in, the live directory the link pointed at


def test_adoption_from_the_state_file_releases_everything(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    session.ensure_virtual()
    raw = json.loads(session.state_path.read_text())
    assert raw["version"] == 1 and raw["sid"] == "r1a2b3c" and raw["pid"] == 4242 and raw["mode"] == "ro"
    assert {entry["kind"] for entry in raw["ledger"]} >= {"blockro", "mount", "bind", "cmdline"}
    assert "passphrase" not in session.state_path.read_text()
    adopted = session_from_state(lab.session_env(), raw)
    assert adopted.mode == "ro" and len(adopted.ledger) == len(session.ledger)
    result = adopted.release()
    assert result.complete and lab.system.mounted_targets() == [] and not any(lab.system.ro.values())


def test_the_audit_log_records_the_mount_and_the_refusals(lab_factory) -> None:
    lab = gentoo_lab(lab_factory)
    session = lab.session("/dev/nvme0n1p5")
    session.mount_ro(lab.graph_obj)
    with pytest.raises(RescueError):
        session.runner.run(("mkfs.ext4", "/dev/nvme0n1p5"))
    session.release()
    kinds = [record["kind"] for record in session.audit.records]
    assert kinds[0] == "mount" and "refused" in kinds and "exec" in kinds and kinds[-1] == "release"
    refused = [r for r in session.audit.records if r["kind"] == "refused"][0]
    assert refused["argv"][0] == "mkfs.ext4"
    assert session.audit.path is not None and oct(session.audit.path.stat().st_mode & 0o777) == "0o600"


def test_parse_fuser() -> None:
    text = "                     USER        PID ACCESS COMMAND\n/mnt/rescue/r1:      root     kernel mount /mnt/rescue/r1\n                     gentoo     4321 ..c.. bash\n"
    assert parse_fuser(text) == [{"user": "gentoo", "pid": 4321, "access": "..c..", "command": "bash"}]
    assert parse_fuser("") == []


def test_parse_fuser_reads_a_real_capture() -> None:
    """fuser -vm prints the path only on the first row, a 'kernel mount' row without a process, access letters in any case and shortened command names."""
    text = ("                     USER        PID ACCESS COMMAND\n"
            "/tmp:                root     kernel mount /\n"
            "                     user        3137 .rce. python3\n"
            "                     user        4149 .rce. dbus-run-sessio\n"
            "                     user        4219 Frce. dbus-daemon\n"
            "                     root       10231 ..c.m my script\n")
    assert [(item["user"], item["pid"], item["access"], item["command"]) for item in parse_fuser(text)] == [
        ("user", 3137, ".rce.", "python3"), ("user", 4149, ".rce.", "dbus-run-sessio"), ("user", 4219, "Frce.", "dbus-daemon"), ("root", 10231, "..c.m", "my script")]
