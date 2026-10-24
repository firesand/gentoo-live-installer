"""The read-only scan: candidates, probe discipline, hints, and what it must never do."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gentoo_installerd.rescue.scan import ScanOptions, parse_blkid_export

from . import fakeroot as fr
from . import lsblk_fixtures as lf
from .fakes import assert_nothing_destructive, tree_digest


def run_scan(lab, **options):
    return lab.scanner().scan(ScanOptions(**options))


def mount_commands(lab) -> list[tuple[str, ...]]:
    return lab.system.commands_named("mount")


def test_finds_a_gentoo_root_on_an_internal_disk_and_never_the_live_stick(lab_factory) -> None:
    lab = lab_factory(lf.usb_live_hybrid())
    lab.system.live_mounts.append(("/dev/sdb1", "/run/initramfs/live"))
    gentoo = lab.add_tree("/dev/sda2", "gentoo", firmware="uefi")
    lab.add_tree("/dev/sdb1", "debian")  # even if the stick had a tree it must not be read
    before = tree_digest(gentoo)
    result = run_scan(lab)
    assert [item["device"] for item in result.candidates] == ["/dev/sda2"]
    candidate = result.candidates[0]
    assert candidate["os"]["id"] == "gentoo" and candidate["os"]["family"] == "gentoo" and candidate["init"] == "openrc"
    assert candidate["fstype"] == "ext4" and candidate["uuid"] == fr.ROOT_UUID and candidate["confidence"] == "high"
    assert candidate["boot"]["kernels"][0]["version"] == fr.GENTOO_KVER and candidate["boot"]["kernels"][0]["initramfs"] is True
    assert candidate["fstab"] == {"present": True, "entries": 3, "dead": 1, "needs_unlock": 0}  # the swap UUID of the fixture is not on this machine
    assert result.live_medium == ["/dev/sdb", "/dev/sdb1", "/dev/sdb2"]
    # discipline: nothing but sda2 was mounted, always read-only, and everything was released again
    mounts = mount_commands(lab)
    assert len(mounts) == 1 and mounts[0][:2] == ("mount", "-t")
    options = mounts[0][4].split(",")
    assert {"ro", "noload", "nodev", "nosuid", "noexec"} <= set(options) and "rw" not in options
    assert lab.system.mounted_targets() == []
    assert lab.system.ro == {"/dev/sda2": False}  # set read-only for the probe, restored afterwards
    assert ("blockdev", "--setro", "/dev/sda2") in lab.system.commands and ("blockdev", "--setrw", "/dev/sda2") in lab.system.commands
    assert tree_digest(gentoo) == before
    assert not list(lab.probe_root.glob("*"))
    assert_nothing_destructive(lab.system)


def test_each_probe_is_a_separate_directory_under_the_probe_root(lab_factory) -> None:
    lab = lab_factory(lf.nvme_dualboot())
    lab.add_tree("/dev/nvme0n1p5", "ubuntu", firmware="uefi")
    run_scan(lab)
    target = lab.system.commands_named("mount")[0][-1]
    assert target.startswith(str(lab.probe_root) + "/") and target.endswith("nvme0n1p5")


def test_windows_and_exotic_filesystems_are_listed_but_never_mounted(lab_factory) -> None:
    for fixture in (lf.windows_only(), lf.exotic_filesystems()):
        lab = lab_factory(fixture)
        result = run_scan(lab)
        assert result.candidates == []
        assert mount_commands(lab) == []
    windows = lab_factory(lf.windows_only())
    assert any(hint["id"] == "only_windows" for hint in run_scan(windows).hints)
    exotic = lab_factory(lf.exotic_filesystems())
    texts = " ".join(hint["text"] for hint in run_scan(exotic).hints)
    assert "6.13" in texts and "ZFS" in texts and "bcachefs" in texts and "BitLocker" in texts


def test_a_failing_device_is_reported_and_the_scan_goes_on(lab_factory) -> None:
    lab = lab_factory(lf.btrfs_multi_device())
    lab.add_tree("/dev/sdb1", "debian")
    lab.system.fail_mount["/dev/sda1"] = "mount: /x: can't read superblock on /dev/sda1."
    result = run_scan(lab)
    assert [item["device"] for item in result.candidates] == ["/dev/sdb1"]
    assert any("/dev/sda1" in warning for warning in result.warnings)
    assert lab.system.mounted_targets() == []
    assert lab.system.ro.get("/dev/sda1") is False  # restored even though the mount failed


def test_unknown_filesystem_types_map_to_unsupported(lab_factory) -> None:
    lab = lab_factory(lf.plain_sata_mbr())
    lab.system.filesystems.discard("ext4")
    result = run_scan(lab)
    assert result.candidates == []
    assert any("cannot mount" in warning for warning in result.warnings)


def test_debian_cloud_layout(lab_factory) -> None:
    lab = lab_factory(lf.debian_cloud())
    lab.add_tree("/dev/vda1", "debian", firmware="uefi")
    result = run_scan(lab)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate["os"]["id"] == "debian" and candidate["boot"]["kernels"][0]["version"] == fr.DEBIAN_KVER
    roles = {device["path"]: device["role"] for device in result.to_dict()["devices"]}
    assert roles["/dev/vda14"] == "bios_boot" and roles["/dev/vda15"] == "esp"


def test_candidate_ids_are_stable_across_rescans(lab_factory) -> None:
    lab = lab_factory(lf.plain_sata_mbr())
    lab.add_tree("/dev/sda1", "arch")
    first = run_scan(lab).candidates[0]["id"]
    second = run_scan(lab).candidates[0]["id"]
    assert first == second and first.startswith("c") and len(first) == 9


def test_no_disks_gives_the_raid_mode_hint(lab_factory) -> None:
    lab = lab_factory(json.dumps({"blockdevices": []}))
    result = run_scan(lab)
    assert result.candidates == [] and [hint["id"] for hint in result.hints] == ["no_disks"]
    assert "AHCI" in result.hints[0]["text"] and "VMD" in result.hints[0]["text"]


def test_locked_encryption_lvm_and_raid_hints(lab_factory) -> None:
    luks = run_scan(lab_factory(lf.luks_lvm_ext4()))
    assert luks.candidates == [] and "locked_volumes" in [hint["id"] for hint in luks.hints]
    assert luks.stacks["luks"][0]["state"] == "locked" and luks.stacks["luks"][0]["version"] == 2
    lvm_lab = lab_factory(lf.lvm_inactive())
    lvm_lab.system.vgs["ubuntu-vg"] = __import__("tests.rescue.fakes", fromlist=["VgSpec"]).VgSpec("ubuntu-vg", "vguuid-1", ["/dev/sda2"], [{"name": "root"}])
    lvm = run_scan(lvm_lab)
    assert "inactive_vg" in [hint["id"] for hint in lvm.hints]
    assert lvm.stacks["vgs"][0]["name"] == "ubuntu-vg" and lvm.stacks["vgs"][0]["active"] is False
    md_lab = lab_factory(lf.md_raid1_unassembled())
    from .fakes import MdSpec
    md_lab.system.md["x"] = MdSpec("aaaa1111:bbbb2222:cccc3333:44445555", ["/dev/sdb1", "/dev/sdc1"], name="host:root")
    md = run_scan(md_lab)
    assert "md_members" in [hint["id"] for hint in md.hints]
    assert md.stacks["arrays"][0]["state"] == "not_assembled" and md.stacks["arrays"][0]["devices"] == 2


@pytest.mark.parametrize("method", ["lvm_inventory", "md_inventory"])
def test_a_parsing_surprise_in_lvm_or_raid_degrades_the_scan_instead_of_killing_it(lab_factory, monkeypatch, method) -> None:
    """The scan is the first thing the window does: one tool that prints something unexpected must cost that part of the list, not the whole answer."""
    from gentoo_installerd.rescue.stack import StackManager

    from .fakes import MdSpec, VgSpec

    lab = lab_factory(lf.lvm_inactive() if method == "lvm_inventory" else lf.md_raid1_unassembled())
    lab.system.vgs["ubuntu-vg"] = VgSpec("ubuntu-vg", "vguuid-1", ["/dev/sda2"], [{"name": "root"}])
    lab.system.md["x"] = MdSpec("aaaa1111:bbbb2222:cccc3333:44445555", ["/dev/sdb1", "/dev/sdc1"], name="host:root")

    def surprise(*_args, **_kwargs):
        raise ValueError("lvm printed something new")

    monkeypatch.setattr(StackManager, method, surprise)
    result = run_scan(lab)
    assert result.stacks["vgs" if method == "lvm_inventory" else "arrays"] == []
    assert any("could not be read" in warning for warning in result.warnings)
    assert [device["path"] for device in result.to_dict()["devices"]]  # the disks are still listed


def test_firmware_mismatch_hints(lab_factory) -> None:
    bios_live = lab_factory(lf.debian_cloud(), uefi=False, name="bios")
    assert "fw_mismatch" in [hint["id"] for hint in run_scan(bios_live).hints]
    uefi_live = lab_factory(lf.plain_sata_mbr(), uefi=True, name="uefi")
    assert "fw_mismatch_bios" in [hint["id"] for hint in run_scan(uefi_live).hints]


def test_environment_reports_firmware_memory_and_secure_boot(lab_factory) -> None:
    lab = lab_factory(lf.plain_sata_mbr(), secure_boot=True, mem_total_kib=4 * 1024 * 1024, mem_available_kib=2 * 1024 * 1024)
    env = run_scan(lab).environment
    assert env.uefi and env.fw_platform_size == 64 and env.secure_boot is True and env.efivars == "rw"
    assert env.memory_bytes == 4 * 1024**3 and env.free_bytes == 2 * 1024**3
    bios = run_scan(lab_factory(lf.plain_sata_mbr(), uefi=False, name="bios")).environment
    assert not bios.uefi and bios.efivars == "" and bios.secure_boot is None


def test_blkid_fills_in_signatures_udev_missed(lab_factory) -> None:
    data = json.loads(lf.plain_sata_mbr())
    for child in data["blockdevices"][0]["children"]:
        if child["name"] == "sda1":
            child["fstype"] = None
            child["uuid"] = None
    lab = lab_factory(json.dumps(data))
    lab.system.blkid["/dev/sda1"] = f"DEVNAME=/dev/sda1\nUUID={fr.ROOT_UUID}\nTYPE=ext4\nLABEL=root\n"
    lab.add_tree("/dev/sda1", "alpine")
    result = run_scan(lab)
    assert ("blkid", "-p", "-o", "export", "/dev/sda1") in lab.system.commands
    assert [item["device"] for item in result.candidates] == ["/dev/sda1"]
    assert parse_blkid_export("A=1\nB=two words\nnoise") == {"A": "1", "B": "two words"}


def test_busy_elsewhere_devices_are_read_through_their_mountpoint(lab_factory, tmp_path: Path) -> None:
    mountpoint = tmp_path / "media-root"
    data = json.loads(lf.desktop_automount())
    data["blockdevices"][0]["children"][0]["mountpoints"] = [str(mountpoint)]
    lab = lab_factory(json.dumps(data))
    fr.make_root(mountpoint, "gentoo")
    result = run_scan(lab)
    candidate = [item for item in result.candidates if item["device"] == "/dev/sda1"][0]
    assert candidate["busy_elsewhere"] == str(mountpoint)
    assert not [argv for argv in lab.system.commands_named("mount") if "/dev/sda1" in argv]


def test_a_system_mounted_several_times_is_found_through_the_mount_that_shows_it(lab_factory, tmp_path: Path) -> None:
    """The running system of a btrfs user: /var/lib/docker (a subvolume) is listed before / (the root), as lsblk prints them."""
    root, docker = tmp_path / "rootfs", tmp_path / "rootfs-docker"
    fr.make_root(root, "gentoo")
    docker.mkdir()
    data = json.loads(lf.desktop_automount())
    data["blockdevices"][0]["children"][1]["mountpoints"] = [str(docker), str(root)]
    data["blockdevices"][0]["children"][1]["fsroots"] = ["/docker", "/@"]
    lab = lab_factory(json.dumps(data))
    result = run_scan(lab)
    candidates = [item for item in result.candidates if item["device"] == "/dev/sda2"]
    assert len(candidates) == 1 and candidates[0]["busy_elsewhere"] == str(root) and candidates[0]["os"]["id"] == "gentoo"
    assert not [argv for argv in lab.system.commands_named("mount") if "/dev/sda2" in argv]


def test_probe_mounts_use_the_lsblk_filesystem_not_fstab(lab_factory) -> None:
    data = json.loads(lf.plain_sata_mbr())
    data["blockdevices"][0]["children"][0]["fstype"] = "ext3"
    lab = lab_factory(json.dumps(data))
    lab.add_tree("/dev/sda1", "gentoo")
    run_scan(lab)
    command = lab.system.commands_named("mount")[0]
    assert command[2] == "ext4" and "noload" in command[4]  # ext3 is mounted with the ext4 driver


def test_the_scan_audit_log_never_contains_a_write(lab_factory) -> None:
    lab = lab_factory(lf.plain_sata_mbr())
    lab.add_tree("/dev/sda1", "gentoo")
    run_scan(lab)
    kinds = {record["kind"] for record in lab.audit.records}
    assert kinds == {"exec"}
    assert all(record["class"] in {"inspect", "mount", "device"} for record in lab.audit.records)


def test_the_live_medium_is_found_in_findmnt_output_without_the_tree_drawing(lab_factory) -> None:
    """findmnt prints a tree by default (├─/run/initramfs/live); the scan asks for the plain list."""
    from gentoo_installerd.rescue.scan import live_sources

    lab = lab_factory(lf.usb_live_hybrid())
    lab.system.live_mounts.append(("/dev/sdb1", "/run/initramfs/live"))
    lab.system.live_mounts.append(("/dev/mapper/ventoy[/iso]", "/run/live/medium"))
    assert live_sources(lab.runner) == ["/dev/sdb1", "/dev/mapper/ventoy"]
    assert ("findmnt", "-n", "-l", "-o", "SOURCE,TARGET") in lab.system.commands
    assert lab.system._do_findmnt(("findmnt", "-n", "-o", "SOURCE,TARGET"), None)[1].splitlines()[1].split("\t")[1].startswith("├─")  # the fake really prints a tree without -l
