"""The command policy: what may run on the live host and inside a target."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from gentoo_installerd.rescue import safety
from gentoo_installerd.rescue.errors import RescueError, SafetyViolation
from gentoo_installerd.rescue.safety import (
    CHROOT_CREDENTIALS, CHROOT_WRAPPER_ARGV, ChrootPolicy, DeviceAllowList, HostPolicy, PolicyContext, check_mount_destination,
)

ROOT = "/mnt/rescue/r1a2b3c"


def make_ctx(**overrides: object) -> PolicyContext:
    devices = DeviceAllowList()
    devices.add("/dev/vda2", "/dev/vda1", "/dev/mapper/rescue-1f2e3d4c5b6a")
    devices.add_disks("/dev/vda")
    devices.mark_live("/dev/sr0", "/dev/sdb")
    ctx = PolicyContext(session_root=ROOT, session_id="r1a2b3c", devices=devices)
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


@pytest.fixture
def host() -> HostPolicy:
    return HostPolicy(make_ctx())


def ok(policy: HostPolicy, *argv: str) -> str:
    return policy.validate(argv)


def refused(policy: HostPolicy, *argv: str) -> SafetyViolation:
    with pytest.raises(SafetyViolation) as info:
        policy.validate(argv)
    return info.value


DESTRUCTIVE = [
    ("mkfs.ext4", "/dev/vda2"), ("mkfs.vfat", "/dev/vda1"), ("mkfs", "-t", "ext4", "/dev/vda2"), ("mkswap", "/dev/vda3"),
    ("wipefs", "-a", "/dev/vda"), ("sgdisk", "--zap-all", "/dev/vda"), ("sfdisk", "/dev/vda"), ("fdisk", "/dev/vda"),
    ("gdisk", "/dev/vda"), ("parted", "--script", "/dev/vda", "mklabel", "gpt"), ("partprobe", "/dev/vda"),
    ("dd", "if=/dev/zero", "of=/dev/vda"), ("shred", "/dev/vda"), ("blkdiscard", "/dev/vda"), ("hdparm", "-I", "/dev/vda"),
    ("pvcreate", "/dev/vda2"), ("vgcreate", "vg", "/dev/vda2"), ("vgremove", "vg"), ("vgrename", "a", "b"), ("lvcreate", "-L1G", "vg"),
    ("lvremove", "vg/lv"), ("lvresize", "-L+1G", "vg/lv"), ("lvchange", "-ay", "vg/lv"), ("resize2fs", "/dev/vda2"),
    ("xfs_growfs", "/mnt"), ("e2label", "/dev/vda2", "x"), ("losetup", "-f", "x.img"), ("swapon", "/dev/vda3"), ("swapoff", "-a"),
    ("rm", "-rf", "/"), ("cp", "a", "b"), ("mv", "a", "b"), ("cat", "/etc/shadow"), ("tee", "x"), ("bash", "-c", "true"),
    ("python3", "-c", "1"), ("reboot",), ("kill", "-9", "1"), ("/usr/bin/mkfs.ext4", "/dev/vda2"),
]


@pytest.mark.parametrize("argv", DESTRUCTIVE, ids=lambda a: a[0])
def test_destructive_and_generic_tools_are_refused(host: HostPolicy, argv: tuple[str, ...]) -> None:
    refused(host, *argv)


def test_unknown_programs_and_strange_paths_are_refused(host: HostPolicy) -> None:
    refused(host, "frobnicate")
    refused(host, "/tmp/evil/lsblk")
    refused(host, "/home/user/bin/mount", "-t", "ext4")
    assert ok(host, "/usr/bin/lsblk", "--json") == "inspect"


def test_tune2fs_only_lists(host: HostPolicy) -> None:
    assert ok(host, "tune2fs", "-l", "/dev/vda2") == "inspect"
    refused(host, "tune2fs", "-O", "^has_journal", "/dev/vda2")
    refused(host, "tune2fs", "-U", "clear", "/dev/vda2")


def test_inspectors_are_allowed_but_never_on_the_live_medium(host: HostPolicy) -> None:
    assert ok(host, "lsblk", "--json", "--bytes") == "inspect"
    assert ok(host, "blkid", "-p", "-o", "export", "/dev/vda2") == "inspect"
    assert ok(host, "findmnt", "-R", "/mnt/rescue") == "inspect"
    assert ok(host, "udevadm", "settle", "--timeout=10") == "inspect"
    assert ok(host, "cryptsetup", "isLuks", "/dev/vda5") == "inspect"
    assert ok(host, "mdadm", "--examine", "--export", "/dev/vda6") == "inspect"
    assert ok(host, "pvs", "--reportformat", "json") == "inspect"
    assert ok(host, "smartctl", "-j", "-H", "-A", "-i", "/dev/vda") == "inspect"
    assert ok(host, "dmesg", "--level=err,warn") == "inspect"
    assert ok(host, "dumpe2fs", "-h", "/dev/vda2") == "inspect"
    with pytest.raises(RescueError) as info:
        host.validate(("blkid", "-p", "/dev/sr0"))
    assert info.value.code == "rescue.live_medium"
    with pytest.raises(RescueError):
        host.validate(("cryptsetup", "isLuks", "/dev/sdb"))
    refused(host, "udevadm", "trigger")
    refused(host, "blkid", "-g")
    refused(host, "smartctl", "-t", "long", "/dev/vda")
    refused(host, "mdadm", "--examine", "--zero", "/dev/vda6")
    refused(host, "ntfsfix", "/dev/vda7")
    refused(host, "dumpe2fs", "/dev/vda2")


def test_block_device_flags_follow_the_allow_list() -> None:
    ctx = make_ctx()
    ctx.devices.protect("/dev/nvme0n1")
    host = HostPolicy(ctx)
    assert ok(host, "blockdev", "--getro", "/dev/vda2") == "inspect"
    assert ok(host, "blockdev", "--setro", "/dev/vda2") == "device"
    assert ok(host, "blockdev", "--setrw", "/dev/vda2") == "device"
    assert ok(host, "blockdev", "--setro", "/dev/nvme0n1") == "device"
    refused(host, "blockdev", "--setro", "/dev/zzz9")
    refused(host, "blockdev", "--rereadpt", "/dev/vda")
    refused(host, "blockdev", "--setbsz", "512", "/dev/vda2")
    with pytest.raises(RescueError):
        host.validate(("blockdev", "--setro", "/dev/sr0"))


def test_scan_may_flag_any_non_live_device_read_only() -> None:
    host = HostPolicy(make_ctx(probing=True, session_root=""))
    assert ok(host, "blockdev", "--setro", "/dev/nvme3n1p9") == "device"
    with pytest.raises(RescueError):
        host.validate(("blockdev", "--setro", "/dev/sr0"))


def test_mount_forms(host: HostPolicy) -> None:
    ro = "ro,noload,nodev,nosuid"
    assert ok(host, "mount", "-t", "ext4", "-o", ro, "/dev/vda2", ROOT) == "mount"
    assert ok(host, "mount", "-t", "ext4", "-o", "rw,nodev,nosuid", "/dev/vda2", ROOT) == "mount"
    assert ok(host, "mount", "-t", "vfat", "-o", "ro,nodev,nosuid,umask=0077", "/dev/vda1", ROOT + "/boot/efi") == "mount"
    assert ok(host, "mount", "-t", "btrfs", "-o", "ro,rescue=nologreplay,subvolid=256,nodev,nosuid", "/dev/mapper/rescue-1f2e3d4c5b6a", ROOT) == "mount"
    assert ok(host, "mount", "-t", "proc", "proc", ROOT + "/proc") == "mount"
    assert ok(host, "mount", "--rbind", "/sys", ROOT + "/sys") == "mount"
    assert ok(host, "mount", "--rbind", "/dev", ROOT + "/dev") == "mount"
    assert ok(host, "mount", "--make-rslave", ROOT + "/dev") == "mount"
    assert ok(host, "mount", "-t", "tmpfs", "-o", "mode=0755,nosuid,nodev", "tmpfs", ROOT + "/run") == "mount"
    assert ok(host, "mount", "--bind", "/run/udev", ROOT + "/run/udev") == "mount"
    assert ok(host, "mount", "-o", "remount,bind,ro", ROOT + "/run/udev") == "mount"
    assert ok(host, "mount", "-t", "efivarfs", "efivarfs", "/sys/firmware/efi/efivars") == "mount"
    assert ok(host, "umount", "-R", ROOT + "/dev") == "mount"
    assert ok(host, "umount", ROOT) == "mount"
    assert ok(host, "umount", "-l", ROOT + "/boot") == "mount"


def test_mount_refusals(host: HostPolicy) -> None:
    ro = "ro,noload,nodev,nosuid"
    refused(host, "mount", "-a")
    refused(host, "mount", "--move", ROOT, "/mnt")
    refused(host, "mount", "-t", "ext4", "-o", ro, "/dev/vda2", "/mnt/gentoo")  # outside the session root
    refused(host, "mount", "-t", "ext4", "-o", ro, "/dev/vda2", "/")
    refused(host, "mount", "-t", "ext4", "-o", ro, "/dev/vda2", ROOT + "/../../etc")
    refused(host, "mount", "-t", "ext4", "-o", ro, "/dev/vda9", ROOT)  # not selected
    refused(host, "mount", "-t", "ext4", "-o", "ro,noload,nosuid", "/dev/vda2", ROOT)  # no nodev
    refused(host, "mount", "-t", "ext4", "-o", "ro,rw,nodev", "/dev/vda2", ROOT)
    refused(host, "mount", "-t", "ext4", "-o", "ro,nodev,suid", "/dev/vda2", ROOT)
    refused(host, "mount", "-t", "ext4", "-o", "ro,nodev,exec,x-systemd.automount", "/dev/vda2", ROOT)
    refused(host, "mount", "-t", "btrfs", "-o", "ro,nodev,subvol=@,suid", "/dev/vda2", ROOT)  # injected option
    refused(host, "mount", "-t", "btrfs", "-o", "ro,nodev,subvol=a,b", "/dev/vda2", ROOT)
    refused(host, "mount", "-t", "btrfs", "-o", "ro,nodev,subvol=../x", "/dev/vda2", ROOT)
    refused(host, "mount", "-t", "reiserfs", "-o", "ro,nodev", "/dev/vda2", ROOT)
    refused(host, "mount", "-t", "iso9660", "-o", "ro,nodev", "/dev/vda2", ROOT)
    refused(host, "mount", "--bind", "/etc", ROOT + "/etc")
    refused(host, "mount", "--rbind", "/", ROOT + "/mnt")
    refused(host, "mount", "-t", "efivarfs", "efivarfs", ROOT + "/x")
    refused(host, "mount", "-t", "proc", "proc", "/proc")
    refused(host, "mount", "-L", "boot", ROOT)
    refused(host, "umount", "/")
    refused(host, "umount", "-R", "/mnt")
    refused(host, "umount", "-a")
    with pytest.raises(RescueError) as info:
        host.validate(("mount", "-t", "iso9660", "-o", "ro,nodev", "/dev/sr0", ROOT))
    assert info.value.code in {"rescue.live_medium", "rescue.safety_violation"}
    with pytest.raises(RescueError) as info:
        host.validate(("mount", "-t", "ext4", "-o", "ro,nodev,nosuid", "/dev/sr0", ROOT))
    assert info.value.code == "rescue.live_medium"


def test_inspect_only_sessions_refuse_rw_mounts() -> None:
    host = HostPolicy(make_ctx(allow_write=False))
    assert ok(host, "mount", "-t", "ext4", "-o", "ro,nodev,nosuid,noexec", "/dev/vda2", ROOT) == "mount"
    with pytest.raises(RescueError) as info:
        host.validate(("mount", "-t", "ext4", "-o", "rw,nodev,nosuid", "/dev/vda2", ROOT))
    assert info.value.code == "rescue.needs_write"


def test_probe_mounts_are_read_only_noexec_and_in_the_probe_directory() -> None:
    probe = "/run/gentoo-rescue/probe/vda2"
    ctx = make_ctx(probing=True, session_root="")
    host = HostPolicy(ctx)
    opts = "ro,noload,nodev,nosuid,noexec"
    assert ok(host, "mount", "-t", "ext4", "-o", opts, "/dev/nvme0n1p9", probe) == "mount"
    assert ok(host, "umount", probe) == "mount"
    refused(host, "mount", "-t", "ext4", "-o", "rw,nodev,nosuid,noexec", "/dev/nvme0n1p9", probe)
    refused(host, "mount", "-t", "ext4", "-o", "ro,nodev,nosuid", "/dev/nvme0n1p9", probe)  # exec not disabled
    refused(host, "mount", "-t", "ext4", "-o", opts, "/dev/nvme0n1p9", "/run/gentoo-rescue/probe/../../../mnt")
    with pytest.raises(RescueError):
        host.validate(("mount", "-t", "ext4", "-o", opts, "/dev/sr0", probe))
    # outside a scan only selected devices may be probed
    session = HostPolicy(make_ctx())
    assert ok(session, "mount", "-t", "vfat", "-o", opts, "/dev/vda1", probe) == "mount"
    refused(session, "mount", "-t", "ext4", "-o", opts, "/dev/nvme0n1p9", probe)


def test_cryptsetup_open_reads_the_passphrase_from_stdin_only() -> None:
    ctx = make_ctx()
    ctx.devices.add("/dev/vda5")
    host = HostPolicy(ctx)
    name = "rescue-1f2e3d4c5b6a"
    assert ok(host, "cryptsetup", "open", "--type", "luks", "--key-file=-", "/dev/vda5", name) == "device"
    assert ok(host, "cryptsetup", "open", "--type", "luks", "--key-file=-", "--readonly", "/dev/vda5", name) == "device"
    assert ok(host, "cryptsetup", "open", "--type", "luks", "--key-file=-", "--test-passphrase", "/dev/vda5") == "device"
    assert ok(host, "cryptsetup", "open", "--type", "luks", f"--key-file={ROOT}/etc/keys/home.key", "/dev/vda5", name) == "device"
    refused(host, "cryptsetup", "open", "--type", "luks", "--key-file=-", "/dev/vda5", "cryptroot")  # mapper name is ours only
    refused(host, "cryptsetup", "open", "--type", "luks", "--key-file=-", "/dev/vda5")
    refused(host, "cryptsetup", "open", "--type", "luks", "--key-file=/etc/shadow", "/dev/vda5", name)
    refused(host, "cryptsetup", "open", "--type", "luks", "--key-file=-", "--allow-discards", "/dev/vda5", name)
    refused(host, "cryptsetup", "open", "--type", "plain", "--key-file=-", "/dev/vda5", name)
    refused(host, "cryptsetup", "open", "--type", "luks", "/dev/vda5", name)  # a passphrase argument/prompt is not allowed
    refused(host, "cryptsetup", "open", "--type", "luks", "--key-file=-", "/dev/vda9", name)
    for verb in ("luksFormat", "luksAddKey", "luksRemoveKey", "luksKillSlot", "luksErase", "luksHeaderRestore", "reencrypt", "resize", "convert", "repair"):
        refused(host, "cryptsetup", verb, "/dev/vda5")
    with pytest.raises(RescueError) as info:
        host.validate(("cryptsetup", "open", "--type", "luks", "--key-file=-", "/dev/sdb", name))
    assert info.value.code == "rescue.live_medium"


def test_cryptsetup_close_only_for_owned_mappings() -> None:
    ctx = make_ctx()
    host = HostPolicy(ctx)
    refused(host, "cryptsetup", "close", "rescue-1f2e3d4c5b6a")
    ctx.owned_maps.add("rescue-1f2e3d4c5b6a")
    assert ok(host, "cryptsetup", "close", "rescue-1f2e3d4c5b6a") == "device"
    refused(host, "cryptsetup", "close", "cryptroot")


def test_lvm_and_md_forms() -> None:
    ctx = make_ctx()
    host = HostPolicy(ctx)
    assert ok(host, "vgchange", "-ay", "--nohints", "vg0") == "device"
    refused(host, "vgchange", "-an", "vg0")  # not owned
    ctx.owned_vgs.add("vg0")
    assert ok(host, "vgchange", "-an", "--nohints", "vg0") == "device"
    refused(host, "vgchange", "-ay", "--activationmode", "partial", "vg0")
    refused(host, "vgchange", "-ay", "-vg0")
    refused(host, "vgchange", "-ay", "vg0", "vg1")
    refused(host, "vgchange", "--lockstart")
    assert ok(host, "mdadm", "--assemble", "--scan", "--readonly", f"--config={ctx.mdadm_conf}") == "device"
    refused(host, "mdadm", "--assemble", "--scan", "--readonly")
    refused(host, "mdadm", "--assemble", "--scan", "--readonly", "--config=/etc/mdadm.conf")
    refused(host, "mdadm", "--create", "/dev/md0", "--level=1")
    refused(host, "mdadm", "--zero-superblock", "/dev/vda2")
    refused(host, "mdadm", "--grow", "/dev/md0")
    refused(host, "mdadm", "/dev/md0", "--fail", "/dev/vda2")
    assert ok(host, "mdadm", "--readwrite", "/dev/vda2") == "device"
    refused(host, "mdadm", "--stop", "/dev/md127")
    ctx.owned_md.add("/dev/md127")
    assert ok(host, "mdadm", "--stop", "/dev/md127") == "device"


def test_filesystem_check_forms() -> None:
    host = HostPolicy(make_ctx())
    assert ok(host, "e2fsck", "-n", "-f", "/dev/vda2") == "device"
    assert ok(host, "e2fsck", "-f", "-p", "/dev/vda2") == "device"
    assert ok(host, "xfs_repair", "-n", "/dev/vda2") == "device"
    assert ok(host, "xfs_repair", "-L", "/dev/vda2") == "device"
    assert ok(host, "fsck.vfat", "-n", "/dev/vda1") == "device"
    assert ok(host, "fsck.f2fs", "--dry-run", "/dev/vda2") == "device"
    assert ok(host, "btrfs", "check", "--readonly", "/dev/vda2") == "device"
    assert ok(host, "btrfs", "rescue", "zero-log", "/dev/vda2") == "device"
    refused(host, "e2fsck", "-b", "8193", "/dev/vda2")
    refused(host, "e2fsck", "-fy", "/dev/vda9")
    refused(host, "xfs_repair", "-d", "/dev/vda2")
    refused(host, "btrfs", "check", "--init-csum-tree", "/dev/vda2")
    refused(host, "btrfs", "rescue", "chunk-recover", "/dev/vda2")
    refused(host, "btrfs", "device", "add", "/dev/vda9", "/mnt")
    refused(host, "btrfs", "filesystem", "resize", "max", ROOT)
    refused(host, "btrfs", "subvolume", "delete", ROOT + "/x")
    assert ok(host, "btrfs", "device", "scan") == "inspect"
    assert ok(host, "btrfs", "--format", "json", "subvolume", "list", "-a", ROOT) == "inspect"
    assert ok(host, "btrfs", "subvolume", "get-default", "/run/gentoo-rescue/probe/vda2") == "inspect"
    refused(host, "btrfs", "subvolume", "list", "/")
    with pytest.raises(RescueError):
        host.validate(("e2fsck", "-n", "-f", "/dev/sr0"))


def test_efibootmgr_forms() -> None:
    host = HostPolicy(make_ctx())
    assert ok(host, "efibootmgr", "-v") == "inspect"
    assert ok(host, "efibootmgr") == "inspect"
    assert ok(host, "efibootmgr", "--create", "--disk", "/dev/vda", "--part", "1", "--label", "Gentoo", "--loader", "\\EFI\\Gentoo\\grubx64.efi") == "efi"
    assert ok(host, "efibootmgr", "--create-only", "--disk", "/dev/vda", "--part", "1", "--label", "Rescue test", "--loader", "\\EFI\\BOOT\\BOOTX64.EFI") == "efi"
    assert ok(host, "efibootmgr", "--create", "--disk", "/dev/vda", "--part", "1", "--label", "Gentoo", "--loader", "\\vmlinuz", "--unicode", "root=UUID=1 initrd=\\initramfs") == "efi"
    assert ok(host, "efibootmgr", "--bootnum", "0003", "--delete-bootnum") == "efi"
    assert ok(host, "efibootmgr", "--bootorder", "0003,0001") == "efi"
    assert ok(host, "efibootmgr", "--bootnext", "0001") == "efi"
    assert ok(host, "efibootmgr", "--bootnum", "0001", "--active") == "efi"
    refused(host, "efibootmgr", "--create", "--disk", "/dev/sdz", "--part", "1", "--label", "x", "--loader", "\\a.efi")  # foreign disk
    refused(host, "efibootmgr", "--bootnum", "ZZZZ", "--delete-bootnum")
    refused(host, "efibootmgr", "--bootorder", "0003;0001")
    refused(host, "efibootmgr", "--label", "bad;label", "--create", "--disk", "/dev/vda")
    refused(host, "efibootmgr", "--driver")
    refused(host, "efibootmgr", "--write-signature")
    refused(host, "efibootmgr", "--loader", "/etc/passwd", "--create", "--disk", "/dev/vda")
    refused(host, "efibootmgr", "--create", "--disk", "/dev/vda", "--part", "1", "--label", "x", "--loader", "\\a.efi", "stray")
    refused(host, "efibootmgr", "--create", "--disk", "/dev/vda", "--part", "1", "--label", "x", "--loader", "\\a.efi", "--unicode", "-rm")


def test_live_side_grub_install_is_constrained_to_the_session() -> None:
    ctx = make_ctx()
    host = HostPolicy(ctx)
    base = ["grub-install", f"--boot-directory={ROOT}/boot", "--target=i386-pc", "--recheck", "/dev/vda"]
    assert ok(host, *base) == "device"
    efi = ["grub-install", f"--boot-directory={ROOT}/boot", f"--efi-directory={ROOT}/efi", "--target=x86_64-efi", "--bootloader-id=Gentoo", "--removable", "--no-nvram"]
    assert ok(host, *efi) == "device"
    refused(host, "grub-install", "--target=i386-pc", "/dev/vda")  # no boot directory: would write the live /boot
    refused(host, "grub-install", f"--boot-directory={ROOT}/boot", "--target=i386-pc", "/dev/sdz")
    refused(host, "grub-install", "--boot-directory=/boot", "--target=i386-pc", "/dev/vda")
    refused(host, "grub-install", f"--boot-directory={ROOT}/../boot", "--target=i386-pc", "/dev/vda")
    refused(host, "grub-install", f"--boot-directory={ROOT}/boot", "--target=i386-pc", "--directory=/usr/lib/grub", "/dev/vda")
    with pytest.raises(RescueError):
        host.validate(("grub-install", f"--boot-directory={ROOT}/boot", "--target=i386-pc", "/dev/sdb"))


def test_timeout_wraps_only_allowed_commands(host: HostPolicy) -> None:
    assert ok(host, "timeout", "--kill-after=5", "30", "lsblk", "--json") == "inspect"
    refused(host, "timeout", "30", "mkfs.ext4", "/dev/vda2")
    refused(host, "timeout", "x", "lsblk")
    refused(host, "timeout", "30")


def chroot_argv(*command: str, root: str = ROOT) -> tuple[str, ...]:
    return (*CHROOT_WRAPPER_ARGV, *CHROOT_CREDENTIALS, root, "/usr/bin/env", "-i", "PATH=" + safety.CHROOT_PATH, *command)


def test_the_chroot_wrapper_is_the_only_shell_form(host: HostPolicy) -> None:
    assert host.validate(chroot_argv("grub-mkconfig", "-o", "/boot/grub/grub.cfg")) == "chroot"
    refused(host, "sh", "-c", "rm -rf /")
    refused(host, "sh", "-c", safety.CHROOT_WRAPPER, "other", *CHROOT_CREDENTIALS, ROOT, "/usr/bin/env", "-i")
    refused(host, *CHROOT_WRAPPER_ARGV, "--userspec=1000:1000", "--groups=", ROOT, "/usr/bin/env", "-i", "x")  # credentials must drop to root:root
    refused(host, *CHROOT_WRAPPER_ARGV, "--userspec=0:0", ROOT, "/usr/bin/env", "-i", "x")  # supplementary groups must be cleared
    refused(host, *chroot_argv("x", root="/mnt/gentoo"))
    refused(host, *chroot_argv("x", root="/"))
    refused(host, *CHROOT_WRAPPER_ARGV, *CHROOT_CREDENTIALS, ROOT, "/bin/bash")
    refused(host, "chroot", ROOT, "/bin/sh")


def test_chroot_policy_uses_the_action_tool_set() -> None:
    ctx = make_ctx()
    policy = ChrootPolicy(ctx, esp_mountpoints=("/boot/efi",))
    tools = {"grub-install", "grub-mkconfig", "dracut", "bootctl", "chpasswd", "emerge", "cryptsetup", "mdadm", "systemctl", "touch"}
    policy.validate(("grub-mkconfig", "-o", "/boot/grub/grub.cfg"), tools)
    policy.validate(("dracut", "--force", "--regenerate-all"), tools)
    policy.validate(("grub-install", "--target=x86_64-efi", "--efi-directory=/boot/efi", "--bootloader-id=Gentoo", "--recheck"), tools)
    policy.validate(("grub-install", "--target=i386-pc", "--recheck", "/dev/vda"), tools)
    policy.validate(("bootctl", "--esp-path=/boot/efi", "install"), tools)
    for command in (
        ("mkfs.ext4", "/dev/vda2"), ("sgdisk", "-Z", "/dev/vda"), ("dd", "if=/dev/zero", "of=/dev/vda"), ("mount", "/dev/vda2", "/mnt"),
        ("umount", "/boot"), ("reboot",), ("poweroff",), ("parted", "/dev/vda"), ("wipefs", "-a", "/dev/vda2"), ("losetup", "-f"),
        ("rm", "-rf", "/boot"), ("bash", "-c", "x"), ("python3", "-c", "x"),
    ):
        with pytest.raises(SafetyViolation):
            policy.validate(command, tools | {command[0]})
    with pytest.raises(SafetyViolation):
        policy.validate(("lsblk",), tools)  # not in the tool set
    with pytest.raises(SafetyViolation):
        policy.validate(("grub-install", "--target=i386-pc", "/dev/sdz"), tools)
    with pytest.raises(SafetyViolation):
        policy.validate(("grub-install", "--target=x86_64-efi", "--efi-directory=/home", "--recheck"), tools)
    with pytest.raises(SafetyViolation):
        policy.validate(("bootctl", "--esp-path=/home", "install"), tools)
    with pytest.raises(SafetyViolation):
        policy.validate(("cryptsetup", "luksFormat", "/dev/vda5"), tools)
    with pytest.raises(SafetyViolation):
        policy.validate(("mdadm", "--create", "/dev/md0"), tools)
    with pytest.raises(SafetyViolation):
        policy.validate(("systemctl", "isolate", "rescue.target"), tools)
    with pytest.raises(SafetyViolation):
        policy.validate(("emerge", "--oneshot", "--root=/dev/vdz9", "x"), tools)  # a device argument that is not selected
    with pytest.raises(SafetyViolation):
        policy.validate((), tools)


def test_device_allow_list_resolves_aliases(tmp_path: Path) -> None:
    real = tmp_path / "dm-3"
    real.write_text("")
    alias = tmp_path / "mapper-root"
    alias.symlink_to(real)
    devices = DeviceAllowList()
    devices.add(str(real))
    assert devices.allows(str(alias)) and devices.allows(str(real))
    devices.mark_live(str(real))
    assert not devices.allows(str(alias)) and devices.is_live(str(alias))
    assert not devices.allows("/dev/definitely-absent")


def test_check_mount_destination_requires_a_plain_directory_below_the_root(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "sess"
    (root / "home").mkdir(parents=True)
    (root / "file").write_text("x")
    (root / "ln").symlink_to("home")
    (tmp_path / "outside").mkdir()
    (root / "evil").symlink_to(tmp_path / "outside")
    check_mount_destination(str(root / "home"), str(root))
    check_mount_destination(str(root), str(root))
    for bad in (root / "file", root / "ln", root / "evil", root / "missing", tmp_path / "outside"):
        with pytest.raises(SafetyViolation):
            check_mount_destination(str(bad), str(root))
    with pytest.raises(SafetyViolation):
        check_mount_destination(str(root) + "/../outside", str(root))
    with pytest.raises(SafetyViolation):
        check_mount_destination(str(root / "home"), "")


def test_no_module_except_safety_names_destructive_tools() -> None:
    package = Path(safety.__file__).parent
    forbidden = ("mkfs", "mkswap", "sgdisk", "sfdisk", "wipefs", "luksFormat", " dd ", "parted")
    offenders = []
    for path in package.rglob("*.py"):
        if path.name == "safety.py":
            continue
        text = path.read_text()
        for word in forbidden:
            if word in text:
                offenders.append((path.name, word))
    assert not offenders


def test_target_paths_are_only_resolved_in_paths_py() -> None:
    """realpath()/Path.resolve() on target-derived text is the R1 bug; every use elsewhere is annotated as a host path."""
    package = Path(safety.__file__).parent
    offenders = []
    for path in package.rglob("*.py"):
        lines = path.read_text().splitlines()
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            pathlib_resolve = node.func.attr == "resolve" and not node.args  # Path.resolve(); RootFS.resolve(path) has an argument
            os_realpath = node.func.attr == "realpath"
            if (pathlib_resolve or os_realpath) and "# host-path" not in lines[node.lineno - 1]:
                offenders.append(f"{path.name}:{node.lineno}: {lines[node.lineno - 1].strip()}")
    assert not offenders, offenders
