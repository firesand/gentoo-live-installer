"""scripts/vm_guest.py on the layouts of distribution images: a separate /boot partition (Ubuntu), a btrfs subvolume (Fedora, Arch),
labels instead of UUIDs (Debian), and the disks of a manual layout across two disks.

Mounting is done by a recorder that copies a fixture tree into the mount point and copies it back on umount, so the breaker's
real file operations run on real directories while no device is touched.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import vm_guest


def write(root: Path, name: str, text: str = "x" * 2048) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class FakeDisk:
    """Answers blockdev, lsblk, mount, umount and sync like the guest; ``devices`` maps a partition to the tree on it."""

    def __init__(self, tmp_path: Path, size: int, lsblk: dict, devices: dict[str, Path]) -> None:
        self.size, self.lsblk, self.devices = size, lsblk, devices
        self.commands: list[list[str]] = []
        self.mounted: dict[str, str] = {}
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp_path

    def __call__(self, argv, *, check=True, input_text=None):
        self.commands.append(list(argv))
        out = ""
        if argv[0] == "blockdev":
            out = f"{self.size}\n"
        elif argv[0] == "lsblk":
            out = json.dumps(self.lsblk)
        elif argv[0] == "mount":
            device, target = argv[1], argv[2]
            assert device in self.devices, f"the breaker mounted {device}, which is not a partition of the disk under test"
            shutil.copytree(self.devices[device], target, symlinks=True, dirs_exist_ok=True)
            self.mounted[target] = device
        elif argv[0] == "umount":
            device = self.mounted.pop(argv[1])
            shutil.rmtree(self.devices[device])  # what is on the mount now is what is on the partition
            shutil.copytree(argv[1], self.devices[device], symlinks=True)
            for entry in list(Path(argv[1]).iterdir()):
                shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
        elif argv[0] == "findmnt":
            return subprocess.CompletedProcess(argv, 1, "unreachable on boot required source: UUID=00000000", "")
        return subprocess.CompletedProcess(argv, 0, out, "")


def lsblk_of(disk: str, partitions: list[dict]) -> dict:
    return {"blockdevices": [{"path": disk, "type": "disk", "size": 100, "children": partitions}]}


def partition(path: str, size: int, fstype: str, **fields) -> dict:
    return {"path": path, "type": "part", "size": size, "fstype": fstype, **fields}


@pytest.fixture(autouse=True)
def nothing_mounted(tmp_path, monkeypatch):
    mounts = tmp_path / "mounts"
    mounts.write_text("proc /proc proc rw 0 0\n")
    monkeypatch.setattr(vm_guest, "MOUNTS_FILE", str(mounts))
    real = vm_guest.tempfile.mkdtemp

    def mkdtemp(prefix):
        return real(prefix=prefix, dir=str(tmp_path))

    monkeypatch.setattr(vm_guest.tempfile, "mkdtemp", mkdtemp)


# --- fstab fields ------------------------------------------------------------------------------------------------

PARTITIONS = [
    partition("/dev/vda1", 4, "ext4", label="cloudimg-rootfs", uuid="AAAAAAAA-1111", partuuid="11111111-01"),
    partition("/dev/vda15", 1, "vfat", label="UEFI", uuid="1234-ABCD", partuuid="11111111-0f"),
    partition("/dev/vda16", 2, "ext4", label="BOOT", uuid="BBBBBBBB-2222", partlabel="boot"),
]


@pytest.mark.parametrize(("spec", "device"), [
    ("LABEL=BOOT", "/dev/vda16"), ("UUID=bbbbbbbb-2222", "/dev/vda16"), ("UUID=1234-abcd", "/dev/vda15"), ("PARTUUID=11111111-01", "/dev/vda1"),
    ("PARTLABEL=boot", "/dev/vda16"), ('LABEL="BOOT"', "/dev/vda16"), ("/dev/vda15", "/dev/vda15"),
    ("LABEL=NOPE", ""), ("UUID=", ""), ("/dev/sdz1", ""), ("tmpfs", ""), ("proc", ""),
])
def test_an_fstab_device_field_names_one_partition_of_the_disk_or_nothing(spec, device):
    assert vm_guest.resolve_spec(spec, PARTITIONS) == device


def test_a_field_that_two_partitions_share_names_none():
    twins = [partition("/dev/vda1", 1, "ext4", label="X"), partition("/dev/vda2", 1, "ext4", label="X")]
    assert vm_guest.resolve_spec("LABEL=X", twins) == ""


def test_the_fstab_is_read_for_device_and_mount_point_only(tmp_path):
    write(tmp_path, "etc/fstab", "# comment\nLABEL=cloudimg-rootfs\t/\text4\tdiscard\t0 1\n\nLABEL=BOOT /boot ext4 defaults 0 2\nbroken\n")
    assert vm_guest.read_fstab(tmp_path) == [("LABEL=cloudimg-rootfs", "/"), ("LABEL=BOOT", "/boot")]
    assert vm_guest.read_fstab(tmp_path / "nowhere") == []


# --- where the system is -------------------------------------------------------------------------------------------

def test_the_system_is_the_mount_itself_unless_its_files_are_in_a_subvolume(tmp_path):
    plain = tmp_path / "plain"
    write(plain, "etc/fstab")
    assert vm_guest.find_system_root(plain) == plain
    top = tmp_path / "top"
    write(top, "root/etc/fstab")
    write(top, "home/user/file")
    assert vm_guest.find_system_root(top) == top / "root"


def test_several_candidate_subvolumes_are_told_apart_by_name_or_refused(tmp_path):
    write(tmp_path, "root/etc/fstab")
    write(tmp_path, "backup/etc/fstab")
    write(tmp_path, "other/etc/fstab")
    assert vm_guest.find_system_root(tmp_path) == tmp_path / "root"
    (tmp_path / "root" / "etc" / "fstab").unlink()
    with pytest.raises(vm_guest.GuestError, match="several directories have an /etc/fstab \\(backup, other\\)"):
        vm_guest.find_system_root(tmp_path)
    shutil.rmtree(tmp_path / "other")
    shutil.rmtree(tmp_path / "backup")
    shutil.rmtree(tmp_path / "root")
    with pytest.raises(vm_guest.GuestError, match="No installed system was found"):
        vm_guest.find_system_root(tmp_path)


def test_a_separate_boot_is_only_named_when_it_is_another_partition_of_the_disk(tmp_path):
    write(tmp_path, "etc/fstab", "LABEL=cloudimg-rootfs / ext4 defaults 0 1\nLABEL=BOOT /boot ext4 defaults 0 2\nLABEL=UEFI /boot/efi vfat defaults 0 1\n")
    assert vm_guest.boot_partition(tmp_path, "/dev/vda1", PARTITIONS) == "/dev/vda16"
    write(tmp_path, "etc/fstab", "LABEL=cloudimg-rootfs / ext4 defaults 0 1\nLABEL=cloudimg-rootfs /boot ext4 bind 0 2\n")
    assert vm_guest.boot_partition(tmp_path, "/dev/vda1", PARTITIONS) == ""  # the root again: nothing separate
    write(tmp_path, "etc/fstab", "LABEL=other-disk /boot ext4 defaults 0 2\n")
    assert vm_guest.boot_partition(tmp_path, "/dev/vda1", PARTITIONS) == ""  # a partition of another disk is never mounted
    write(tmp_path, "etc/fstab", "LABEL=cloudimg-rootfs / ext4 defaults 0 1\n")
    assert vm_guest.boot_partition(tmp_path, "/dev/vda1", PARTITIONS) == ""


def test_initramfs_images_are_found_in_boot_and_one_directory_per_kernel(tmp_path):
    write(tmp_path, "boot/initrd.img-6.8.0-45-generic")
    write(tmp_path, "boot/initramfs-0-rescue-abc.img")
    write(tmp_path, "boot/0123456789abcdef/6.11.9/initrd")
    write(tmp_path, "boot/vmlinuz-6.8.0-45-generic")
    write(tmp_path, "boot/grub/grub.cfg")
    write(tmp_path, "boot/a/b/c/d/initrd-too-deep")
    names = [path.relative_to(tmp_path).as_posix() for path in vm_guest.initramfs_images(tmp_path / "boot")]
    assert names == ["boot/0123456789abcdef/6.11.9/initrd", "boot/initramfs-0-rescue-abc.img", "boot/initrd.img-6.8.0-45-generic"]


@pytest.mark.parametrize("spec", ["LABEL=ROOT", "PARTUUID=1111-01", "UUID=2222-bbbb"])
def test_the_fstab_break_replaces_any_kind_of_root_field_with_a_uuid_that_does_not_exist(tmp_path, spec):
    write(tmp_path, "etc/fstab", f"{spec}\t/\text4\tdefaults\t0 1\nLABEL=UEFI\t/boot/efi\tvfat\tdefaults\t0 2\n")
    changes = vm_guest.break_root_tree("fstab-uuid", tmp_path)
    assert changes == {"replaced": [spec], "with": f"UUID={vm_guest.DEAD_UUID}"}
    assert "LABEL=UEFI\t/boot/efi" in (tmp_path / "etc/fstab").read_text()


def test_a_root_given_as_a_device_name_is_left_alone(tmp_path):
    write(tmp_path, "etc/fstab", "/dev/vda1 / ext4 defaults 0 1\n")
    with pytest.raises(vm_guest.GuestError, match="nothing to break"):
        vm_guest.break_root_tree("fstab-uuid", tmp_path)


# --- the layouts of the images ---------------------------------------------------------------------------------------

def ubuntu_like(tmp_path: Path) -> tuple[FakeDisk, Path, Path]:
    """Root on p1, the kernel, the initramfs and grub.cfg on a /boot partition of its own (p16), the ESP on p15."""
    root, boot = tmp_path / "p1", tmp_path / "p16"
    write(root, "etc/fstab", "LABEL=cloudimg-rootfs\t/\text4\tdiscard\t0 1\nLABEL=BOOT\t/boot\text4\tdefaults\t0 2\nLABEL=UEFI\t/boot/efi\tvfat\tumask=0077\t0 1\n")
    write(root, "etc/os-release", "ID=ubuntu\n")
    (root / "boot").mkdir()
    write(boot, "grub/grub.cfg", "menuentry 'Ubuntu' {}\n" * 30)
    write(boot, "initrd.img-6.8.0-45-generic")
    write(boot, "vmlinuz-6.8.0-45-generic")
    lsblk = lsblk_of("/dev/vda", [partition("/dev/vda1", 3000, "ext4", label="cloudimg-rootfs"), partition("/dev/vda15", 100, "vfat", label="UEFI",
                                                                                                   parttype="c12a7328-f81f-11d2-ba4b-00a0c93ec93b"),
                                  partition("/dev/vda16", 900, "ext4", label="BOOT")])
    return FakeDisk(tmp_path / "work", 100, lsblk, {"/dev/vda1": root, "/dev/vda16": boot}), root, boot


@pytest.mark.parametrize(("mode", "gone"), [("grub-cfg", "grub/grub.cfg"), ("initramfs", "initrd.img-6.8.0-45-generic")])
def test_the_files_on_a_separate_boot_partition_are_broken_there(tmp_path, mode, gone):
    disk, root, boot = ubuntu_like(tmp_path)
    report = vm_guest.rescue_break({"mode": mode, "path": "/dev/vda", "size": 100}, disk)
    assert not (boot / gone).exists(), "the damage reached the /boot partition"
    assert (boot / "vmlinuz-6.8.0-45-generic").exists() and not list((root / "boot").iterdir()), "the root's own /boot directory was never the target"
    assert report["boot_partition"] == "/dev/vda16" and report["root"] == "/dev/vda1" and report["system"] == ""
    mounts = [c for c in disk.commands if c[0] in {"mount", "umount"}]
    assert [c[0] for c in mounts] == ["mount", "mount", "umount", "umount"]
    assert mounts[0][1] == "/dev/vda1" and mounts[1][1] == "/dev/vda16" and mounts[1][2] == mounts[0][2] + "/boot"
    assert mounts[2][1] == mounts[1][2] and mounts[3][1] == mounts[0][2] and not disk.mounted, "both partitions are released, /boot first"


def test_the_fstab_break_on_a_system_with_labels_is_proven_by_findmnt_on_the_system_file(tmp_path):
    disk, root, _boot = ubuntu_like(tmp_path)
    report = vm_guest.rescue_break({"mode": "fstab-uuid", "path": "/dev/vda", "size": 100}, disk)
    assert report["replaced"] == ["LABEL=cloudimg-rootfs"] and report["findmnt_exit"] == 1
    assert f"UUID={vm_guest.DEAD_UUID}\t/\t" in (root / "etc/fstab").read_text()


def fedora_like(tmp_path: Path) -> tuple[FakeDisk, Path]:
    """One btrfs partition whose top level holds the subvolumes root and home; /boot is a directory of root."""
    top = tmp_path / "p3"
    write(top, "root/etc/fstab", "UUID=5555-aaaa\t/\tbtrfs\tsubvol=root,compress=zstd:1\t0 0\nUUID=1234-ABCD\t/boot/efi\tvfat\tumask=0077\t0 2\n")
    write(top, "root/etc/os-release", "ID=fedora\n")
    write(top, "root/boot/grub2/grub.cfg")
    write(top, "root/boot/initramfs-6.11.9-300.fc41.x86_64.img")
    write(top, "root/boot/loader/entries/abc-6.11.9.conf", "title Fedora\n")
    write(top, "home/user/.bashrc")
    lsblk = lsblk_of("/dev/vda", [partition("/dev/vda2", 100, "vfat", parttype="c12a7328-f81f-11d2-ba4b-00a0c93ec93b"), partition("/dev/vda3", 4000, "btrfs")])
    return FakeDisk(tmp_path / "work", 100, lsblk, {"/dev/vda3": top}), top


def test_a_system_in_a_btrfs_subvolume_is_broken_inside_that_subvolume(tmp_path):
    disk, top = fedora_like(tmp_path)
    report = vm_guest.rescue_break({"mode": "grub-cfg", "path": "/dev/vda", "size": 100}, disk)
    assert report["system"] == "root" and report["root_fstype"] == "btrfs" and report["removed"] == ["boot/grub2/grub.cfg"]
    assert not (top / "root/boot/grub2/grub.cfg").exists() and (top / "root/boot/initramfs-6.11.9-300.fc41.x86_64.img").exists()
    assert (top / "home/user/.bashrc").exists(), "the other subvolume is not touched"


def test_the_initramfs_break_of_a_subvolume_system_lists_paths_relative_to_the_system(tmp_path):
    disk, top = fedora_like(tmp_path)
    report = vm_guest.rescue_break({"mode": "initramfs", "path": "/dev/vda", "size": 100}, disk)
    assert report["removed"] == ["boot/initramfs-6.11.9-300.fc41.x86_64.img"]
    assert (top / "root/boot/loader/entries/abc-6.11.9.conf").exists(), "boot entries are not initramfs images"


def test_a_failure_while_breaking_still_releases_every_mount(tmp_path):
    disk, root, _boot = ubuntu_like(tmp_path)
    (root / "etc" / "fstab").write_text("LABEL=cloudimg-rootfs / ext4 defaults 0 1\nLABEL=BOOT /boot ext4 defaults 0 2\n")
    shutil.rmtree(_boot / "grub")
    with pytest.raises(vm_guest.GuestError, match="nothing to break"):
        vm_guest.rescue_break({"mode": "grub-cfg", "path": "/dev/vda", "size": 100}, disk)
    assert not disk.mounted and [c[0] for c in disk.commands if c[0] == "umount"] == ["umount", "umount"]


# --- the other disk of a manual layout and the proof that a disk was left alone -------------------------------------------

class Recorder:
    def __init__(self, size: int) -> None:
        self.size, self.commands = size, []

    def __call__(self, argv, *, check=True, input_text=None):
        self.commands.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, f"{self.size}\n" if argv[0] == "blockdev" else "", "")


def test_a_data_disk_gets_swap_first_then_home_and_nothing_is_formatted():
    recorder = Recorder(500)
    report = vm_guest.partition_manual({"role": "data-1", "path": "/dev/vdb", "size": 500}, recorder)
    assert report == {"path": "/dev/vdb", "swap": "/dev/vdb1", "home": "/dev/vdb2"}
    assert ["sgdisk", "--new=1:0:+512MiB", "--typecode=1:8200", "--change-name=1:swap", "/dev/vdb"] in recorder.commands
    assert ["sgdisk", "--new=2:0:0", "--typecode=2:8300", "--change-name=2:home", "/dev/vdb"] in recorder.commands
    assert not any(command[0].startswith("mkfs") or command[0] in {"mkswap", "mount", "wipefs"} for command in recorder.commands)


def test_a_data_disk_is_still_checked_by_name_and_size():
    with pytest.raises(vm_guest.GuestError, match="has 10 bytes"):
        vm_guest.partition_manual({"role": "data-1", "path": "/dev/vdb", "size": 500}, Recorder(10))
    with pytest.raises(vm_guest.GuestError, match="not a VM disk name"):
        vm_guest.partition_manual({"role": "data-1", "path": "/dev/loop0", "size": 500}, Recorder(500))


def test_the_disk_hash_is_the_checksum_of_the_whole_disk(monkeypatch):
    monkeypatch.setattr(vm_guest, "sha256_device", lambda path: "c" * 64)
    assert vm_guest.disk_hash({"path": "/dev/vda", "size": 7}, Recorder(7)) == {"path": "/dev/vda", "size": 7, "sha256": "c" * 64}
    with pytest.raises(vm_guest.GuestError, match="has 8 bytes"):
        vm_guest.disk_hash({"path": "/dev/vda", "size": 7}, Recorder(8))
    assert "disk-hash" in vm_guest.COMMANDS
