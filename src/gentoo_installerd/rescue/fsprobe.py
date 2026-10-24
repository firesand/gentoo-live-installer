"""File system capabilities and read-only mount ladders (review R16: types come from blkid, never from fstab)."""

from __future__ import annotations

from typing import Sequence

from .errors import RescueError
from .runner import RescueRunner

# What the live kernel is asked to mount: ext3 is served by the ext4 driver (there is no EXT3_FS symbol).
MOUNT_TYPE = {"ext2": "ext2", "ext3": "ext4", "ext4": "ext4", "xfs": "xfs", "btrfs": "btrfs", "f2fs": "f2fs", "jfs": "jfs", "vfat": "vfat", "exfat": "exfat", "ntfs": "ntfs3",
              "ntfs3": "ntfs3", "hfsplus": "hfsplus"}
RECOVERY_SKIP = {"ext2": (), "ext3": ("noload",), "ext4": ("noload",), "xfs": ("norecovery", "nouuid"), "btrfs": ("rescue=nologreplay",), "f2fs": ("norecovery",), "jfs": ()}
CHECK_TOOLS = {
    "ext2": "e2fsck", "ext3": "e2fsck", "ext4": "e2fsck", "xfs": "xfs_repair", "btrfs": "btrfs", "vfat": "fsck.vfat", "f2fs": "fsck.f2fs", "exfat": "fsck.exfat", "jfs": "jfs_fsck",
    "ntfs": "ntfsfix",
}


def mount_type(fstype: str, available: Sequence[str] | set[str] | None = None) -> str:
    """The -t argument for a file system found by blkid."""
    wanted = MOUNT_TYPE.get(fstype, fstype)
    if wanted == "ext2" and available is not None and "ext2" not in available:
        return "ext4"  # EXT4_USE_FOR_EXT2 builds register only ext4 on some kernels
    return wanted


def ro_ladder(fstype: str, *, allow_degraded: bool = False, subvolid: int | None = None) -> list[list[str]]:
    """Option sets to try in order; each is read-only and skips journal replay."""
    skip = list(RECOVERY_SKIP.get(fstype, ()))
    if fstype != "btrfs":
        return [["ro", *skip]]
    base = ["ro"] + ([f"subvolid={subvolid}"] if subvolid is not None else [])
    rungs = [base + ["rescue=nologreplay"], base + ["rescue=nologreplay:usebackuproot"], base + ["rescue=nologreplay:ibadroots:idatacsums"]]
    if allow_degraded:
        rungs.append(rungs[-1] + ["degraded"])
    return rungs


def classify_mount_failure(output: str, fstype: str, device: str) -> RescueError:
    text = output.lower()
    if "unknown filesystem type" in text or "unknown fs type" in text:
        return RescueError("unsupported_fs", f"This live kernel cannot mount {fstype or 'this'} file systems", reason="unknown filesystem type", fstype=fstype)
    if "duplicate fsid" in text or "already mounted" in text and "btrfs" in fstype:
        return RescueError("duplicate_fsid", f"Another disk with the same {fstype} file system ID is connected; attach only one of them", fstype=fstype)
    if "busy" in text:
        return RescueError("mounted_elsewhere", f"{device} is in use by another mount", output=output[-300:])
    if "bad superblock" in text or "can't read superblock" in text or "wrong fs type" in text or "structure needs cleaning" in text:
        return RescueError("degraded", f"{device} has a damaged {fstype} file system; run a file system check", output=output[-300:])
    return RescueError("command_failed", f"Mounting {device} failed: {output.strip()[-300:] or 'no message'}", output=output[-300:])


def mount_readonly(
    runner: RescueRunner,
    device: str,
    fstype: str,
    target: str,
    *,
    extra: Sequence[str] = (),
    noexec: bool = True,
    allow_degraded: bool = False,
    subvolid: int | None = None,
    available: Sequence[str] | set[str] | None = None,
    timeout: int = 30,
) -> int:
    """Mount read-only along the ladder; returns the rung that worked (0 is the preferred one)."""
    last: RescueError | None = None
    hardening = ["nodev", "nosuid"] + (["noexec"] if noexec else [])
    for rung, options in enumerate(ro_ladder(fstype, allow_degraded=allow_degraded, subvolid=subvolid)):
        opts = ",".join([*options, *extra, *hardening])
        result = runner.execute(("mount", "-t", mount_type(fstype, available), "-o", opts, device, target), timeout=timeout)
        if result.returncode == 0:
            return rung
        last = classify_mount_failure(result.stdout, fstype, device)
        if last.short in {"unsupported_fs", "mounted_elsewhere", "duplicate_fsid"}:
            break
    assert last is not None
    raise last
