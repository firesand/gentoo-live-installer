"""The programs the harness runs in the guest (scripts/vm_guest.py): decoys and rescue breakage.

Only the pure parts run for real, on fixture directories; everything that would touch a block
device goes through an injected runner that records the commands instead of running them.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import vm_guest


@pytest.fixture(autouse=True)
def no_host_mounts(tmp_path, monkeypatch):
    """The mounted-disk check must never depend on what the machine running the tests has mounted."""
    mounts = tmp_path / "mounts"
    mounts.write_text("proc /proc proc rw 0 0\n")
    monkeypatch.setattr(vm_guest, "MOUNTS_FILE", str(mounts))
    return mounts


class Recorder:
    """A runner that answers like the guest's tools and remembers what it was asked to run."""

    def __init__(self, size: int = 0, lsblk: dict | None = None):
        self.commands: list[list[str]] = []
        self.size = size
        self.lsblk = lsblk or {}

    def __call__(self, argv, *, check=True, input_text=None):
        self.commands.append(list(argv))
        out = ""
        if argv[0] == "blockdev":
            out = f"{self.size}\n"
        elif argv[0] == "lsblk":
            out = json.dumps(self.lsblk)
        return subprocess.CompletedProcess(argv, 0, out, "")


def test_only_the_vms_own_disks_are_acceptable_targets():
    recorder = Recorder(size=1000)
    for bad in ("/dev/sda1", "/dev/nvme0n1p1", "/dev/mapper/gentoo-root", "/dev/zero", "vda", "/dev/vda; reboot"):
        with pytest.raises(vm_guest.GuestError, match="not a VM disk name"):
            vm_guest.verify_disk(bad, 1000, recorder)
    assert recorder.commands == []  # refused before anything ran
    for good in ("/dev/vdb", "/dev/sdc", "/dev/nvme1n1"):
        vm_guest.verify_disk(good, 1000, recorder)


def test_a_disk_with_a_mounted_partition_is_refused(no_host_mounts):
    no_host_mounts.write_text("/dev/vdb1 /mnt ext4 rw 0 0\n")
    with pytest.raises(vm_guest.GuestError, match="something on it is mounted"):
        vm_guest.verify_disk("/dev/vdb", 1000, Recorder(size=1000))


def test_a_disk_of_the_wrong_size_is_refused():
    with pytest.raises(vm_guest.GuestError, match="has 999 bytes, the harness created 1000"):
        vm_guest.verify_disk("/dev/vdb", 1000, Recorder(size=999))


def test_partition_names_follow_the_kernel_convention():
    assert vm_guest.partition_path("/dev/vdb", 1) == "/dev/vdb1"
    assert vm_guest.partition_path("/dev/nvme0n1", 2) == "/dev/nvme0n1p2"


def test_decoy_preparation_runs_the_expected_commands_in_order_and_reports_a_checksum(tmp_path, monkeypatch):
    disk = tmp_path / "decoy.img"
    disk.write_bytes(b"\0" * 4096)
    monkeypatch.setattr(vm_guest, "sha256_device", lambda path: "f" * 64)
    recorder = Recorder(size=256 << 20)
    monkeypatch.setattr(vm_guest.tempfile, "mkdtemp", lambda prefix: str(tmp_path / "mnt"))
    monkeypatch.setattr(vm_guest.os, "rmdir", lambda path: None)  # the fake mount point holds the marker file
    (tmp_path / "mnt").mkdir()
    report = vm_guest.decoy_prepare({"role": "decoy-1", "path": "/dev/vdb", "size": 256 << 20}, recorder)
    names = [command[0] for command in recorder.commands]
    assert names == ["blockdev", "sgdisk", "sgdisk", "partprobe", "udevadm", "mkfs.ext4", "mount", "sync", "umount"]
    assert recorder.commands[5] == ["mkfs.ext4", "-q", "-F", "-L", "DECOY", "/dev/vdb1"]
    assert report == {"path": "/dev/vdb", "partition": "/dev/vdb1", "size": 256 << 20, "sha256": "f" * 64}
    assert all("/dev/vda" not in " ".join(command) for command in recorder.commands)


def test_only_decoy_disks_are_prepared():
    with pytest.raises(vm_guest.GuestError, match="Only decoy disks"):
        vm_guest.decoy_prepare({"role": "target", "path": "/dev/vda", "size": 1}, Recorder(size=1))


def test_decoy_check_compares_checksums(monkeypatch):
    monkeypatch.setattr(vm_guest, "sha256_device", lambda path: "a" * 64)
    recorder = Recorder(size=10)
    same = vm_guest.decoy_check({"path": "/dev/vdb", "size": 10, "sha256": "a" * 64}, recorder)
    other = vm_guest.decoy_check({"path": "/dev/vdb", "size": 10, "sha256": "b" * 64}, recorder)
    assert same["unchanged"] is True and other["unchanged"] is False


LSBLK = {"blockdevices": [{"path": "/dev/vda", "type": "disk", "size": 100, "children": [
    {"path": "/dev/vda1", "type": "part", "size": 1, "fstype": "vfat", "parttype": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"},
    {"path": "/dev/vda2", "type": "part", "size": 2, "fstype": "swap", "parttype": "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f"},
    {"path": "/dev/vda3", "type": "part", "size": 90, "fstype": "ext4", "parttype": "0fc63daf-8483-4772-8e79-3d69d8477de4"}]}]}


def test_the_root_and_esp_are_found_from_lsblk():
    assert vm_guest.choose_partitions(LSBLK, "/dev/vda") == {"root": "/dev/vda3", "root_fstype": "ext4", "esp": "/dev/vda1"}


def test_the_largest_root_capable_partition_wins_and_bios_has_no_esp():
    lsblk = {"blockdevices": [{"path": "/dev/sda", "type": "disk", "children": [
        {"path": "/dev/sda1", "type": "part", "size": 10, "fstype": "xfs"}, {"path": "/dev/sda2", "type": "part", "size": 50, "fstype": "btrfs"}]}]}
    assert vm_guest.choose_partitions(lsblk, "/dev/sda") == {"root": "/dev/sda2", "root_fstype": "btrfs", "esp": ""}


@pytest.mark.parametrize("fstype", ["crypto_LUKS", "LVM2_member", "linux_raid_member"])
def test_layouts_the_breaker_cannot_open_are_reported_not_guessed(fstype):
    lsblk = {"blockdevices": [{"path": "/dev/vda", "type": "disk", "children": [
        {"path": "/dev/vda1", "type": "part", "size": 1, "fstype": "vfat"}, {"path": "/dev/vda2", "type": "part", "size": 9, "fstype": fstype}]}]}
    with pytest.raises(vm_guest.GuestError, match="LUKS, LVM or RAID"):
        vm_guest.choose_partitions(lsblk, "/dev/vda")


def test_a_disk_without_a_root_filesystem_is_reported():
    with pytest.raises(vm_guest.GuestError, match="No ext4, XFS or Btrfs root"):
        vm_guest.choose_partitions({"blockdevices": [{"path": "/dev/vda", "type": "disk", "children": []}]}, "/dev/vda")
    with pytest.raises(vm_guest.GuestError, match="does not list /dev/vdz"):
        vm_guest.choose_partitions(LSBLK, "/dev/vdz")


def installed_tree(root: Path) -> Path:
    (root / "boot" / "grub").mkdir(parents=True)
    (root / "boot" / "grub" / "grub.cfg").write_text("menuentry 'Gentoo' {}\n" * 40)
    (root / "boot" / "initramfs-6.12.0-gentoo-dist.img").write_bytes(b"\x1f" * 4096)
    (root / "boot" / "initramfs-6.6.0-gentoo-dist.img").write_bytes(b"\x1f" * 4096)
    (root / "boot" / "vmlinuz-6.12.0-gentoo-dist").write_bytes(b"kernel")
    (root / "etc").mkdir()
    (root / "etc" / "fstab").write_text(
        "UUID=1111-aaaa\t/boot/efi\tvfat\tdefaults\t0 2\nUUID=2222-bbbb\t/\text4\tnoatime\t0 1\nUUID=3333-cccc\tnone\tswap\tsw\t0 0\n")
    return root


def test_grub_cfg_breakage_removes_only_that_file(tmp_path):
    root = installed_tree(tmp_path)
    changes = vm_guest.break_root_tree("grub-cfg", root)
    assert changes["removed"] == ["boot/grub/grub.cfg"] and changes["bytes"] > 0
    assert not (root / "boot/grub/grub.cfg").exists() and (root / "boot/vmlinuz-6.12.0-gentoo-dist").exists()


def test_initramfs_breakage_removes_every_image_but_not_the_kernel(tmp_path):
    root = installed_tree(tmp_path)
    changes = vm_guest.break_root_tree("initramfs", root)
    assert changes["removed"] == ["boot/initramfs-6.12.0-gentoo-dist.img", "boot/initramfs-6.6.0-gentoo-dist.img"]
    assert (root / "boot/vmlinuz-6.12.0-gentoo-dist").exists() and (root / "boot/grub/grub.cfg").exists()


def test_fstab_breakage_replaces_only_the_root_uuid(tmp_path):
    root = installed_tree(tmp_path)
    changes = vm_guest.break_root_tree("fstab-uuid", root)
    assert changes == {"replaced": ["UUID=2222-bbbb"], "with": f"UUID={vm_guest.DEAD_UUID}"}
    text = (root / "etc/fstab").read_text()
    assert f"UUID={vm_guest.DEAD_UUID}\t/\text4" in text
    assert "UUID=1111-aaaa\t/boot/efi" in text and "UUID=3333-cccc\tnone\tswap" in text  # ESP and swap untouched


@pytest.mark.parametrize("mode", vm_guest.BREAK_MODES)
def test_an_installation_with_nothing_to_break_is_an_error_not_a_silent_success(tmp_path, mode):
    (tmp_path / "boot").mkdir()
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "fstab").write_text("# nothing\n")
    with pytest.raises(vm_guest.GuestError, match="nothing to break"):
        vm_guest.break_root_tree(mode, tmp_path)


def test_unknown_break_modes_are_refused(tmp_path):
    with pytest.raises(vm_guest.GuestError, match="Unknown break mode"):
        vm_guest.break_root_tree("format-disk", tmp_path)
    with pytest.raises(vm_guest.GuestError, match="Unknown break mode"):
        vm_guest.rescue_break({"mode": "mkfs", "path": "/dev/vda", "size": 1}, Recorder(size=1))


def test_the_fstab_breakage_is_proven_with_findmnt_before_the_unmount(tmp_path, monkeypatch):
    mount_point = installed_tree(tmp_path / "mnt")
    recorder = Recorder(size=100, lsblk=LSBLK)
    monkeypatch.setattr(vm_guest.tempfile, "mkdtemp", lambda prefix: str(mount_point))
    monkeypatch.setattr(vm_guest.os, "rmdir", lambda path: None)
    report = vm_guest.rescue_break({"mode": "fstab-uuid", "path": "/dev/vda", "size": 100}, recorder)
    assert [c[0] for c in recorder.commands] == ["blockdev", "lsblk", "mount", "sync", "findmnt", "umount"]
    assert recorder.commands[4] == ["findmnt", "--verify", "--tab-file", str(mount_point / "etc" / "fstab")]
    assert report["replaced"] == ["UUID=2222-bbbb"] and report["findmnt_exit"] == 0  # the fake runner is happy; the real one is not


def test_rescue_break_mounts_breaks_syncs_and_unmounts(tmp_path, monkeypatch):
    mount_point = installed_tree(tmp_path / "mnt")
    recorder = Recorder(size=100, lsblk=LSBLK)
    monkeypatch.setattr(vm_guest.tempfile, "mkdtemp", lambda prefix: str(mount_point))
    monkeypatch.setattr(vm_guest.os, "rmdir", lambda path: None)
    report = vm_guest.rescue_break({"mode": "grub-cfg", "path": "/dev/vda", "size": 100}, recorder)
    assert [c[0] for c in recorder.commands] == ["blockdev", "lsblk", "mount", "sync", "umount"]
    assert recorder.commands[2] == ["mount", "/dev/vda3", str(mount_point)]
    assert report["mode"] == "grub-cfg" and report["root"] == "/dev/vda3" and report["removed"] == ["boot/grub/grub.cfg"]
    # nothing in the run formats, partitions or wipes
    assert not any(c[0] in {"mkfs.ext4", "mkfs", "sgdisk", "wipefs", "parted"} for c in recorder.commands)


def test_main_prints_one_json_line_for_success_and_failure(capsys):
    assert vm_guest.main(["rescue-break", json.dumps({"mode": "mkfs", "path": "/dev/vda", "size": 1})]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["type"] == "vm_guest" and failed["ok"] is False and "Unknown break mode" in failed["error"]
    assert vm_guest.main(["no-such-command", "{}"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_the_guest_program_compiles_as_a_single_command_argument():
    source = Path(vm_guest.__file__).read_text()
    compile(source, "vm_guest", "exec")
    assert len(source) < 100_000  # one ssh command argument


# --- partitioning for manual mode -------------------------------------------------------------------

def test_a_manual_layout_is_a_gpt_with_an_esp_and_a_root_and_nothing_is_formatted():
    recorder = Recorder(size=500)
    report = vm_guest.partition_manual({"role": "target", "path": "/dev/vda", "size": 500, "firmware": "uefi"}, recorder)
    assert report == {"path": "/dev/vda", "boot": "/dev/vda1", "root": "/dev/vda2"}
    assert recorder.commands[1] == ["sgdisk", "--zap-all", "/dev/vda"]
    assert ["sgdisk", "--new=1:0:+512MiB", "--typecode=1:EF00", "--change-name=1:EFI", "/dev/vda"] in recorder.commands
    assert ["sgdisk", "--new=2:0:0", "--typecode=2:8300", "--change-name=2:root", "/dev/vda"] in recorder.commands
    assert not any(command[0].startswith("mkfs") or command[0] in {"mkswap", "mount", "wipefs"} for command in recorder.commands)


def test_a_bios_layout_has_a_bios_boot_partition_and_nvme_names_get_a_p():
    recorder = Recorder(size=500)
    report = vm_guest.partition_manual({"role": "target", "path": "/dev/nvme0n1", "size": 500, "firmware": "bios"}, recorder)
    assert report["boot"] == "/dev/nvme0n1p1" and report["root"] == "/dev/nvme0n1p2"
    assert ["sgdisk", "--new=1:0:+3MiB", "--typecode=1:EF02", "--change-name=1:BIOS", "/dev/nvme0n1"] in recorder.commands


def test_only_the_target_is_partitioned_and_only_with_a_known_firmware():
    with pytest.raises(vm_guest.GuestError, match="Only the target disk"):
        vm_guest.partition_manual({"role": "decoy-1", "path": "/dev/vdb", "size": 500, "firmware": "uefi"}, Recorder(size=500))
    with pytest.raises(vm_guest.GuestError, match="Unknown firmware"):
        vm_guest.partition_manual({"role": "target", "path": "/dev/vda", "size": 500, "firmware": "coreboot"}, Recorder(size=500))
    with pytest.raises(vm_guest.GuestError, match="not a VM disk name"):
        vm_guest.partition_manual({"role": "target", "path": "/dev/sda1", "size": 500, "firmware": "uefi"}, Recorder(size=500))
