"""Storage preparation for the installer: partitioning, formatting, encryption, mounting, cleanup.

StorageManager.prepare() is the destructive part of an installation, so it checks everything
that can be checked before the first command that writes. The order matters:

 1. the manifest pins the target disk (storage.device_id), or the operator explicitly accepts
    an unverified disk;
 2. one lsblk inventory; the live installer medium is found (mount source and kernel command
    line) and refused;
 3. the declared identity (serial, WWN, or the user-approved by-path identity) matches the
    disk; in manual mode every disk a partition row lives on is approved, and the selected
    disk holds one of the rows;
 4. nothing on the target is mounted;
 5. the layout fits the disk (capacity planner) and preserved partitions are what the
    manifest says (declared filesystem, BIOS boot partition for GPT);
 6. LVM / md members are looked up explicitly (live auto-activation is off, lsblk shows
    nothing active) and active volumes are listed: they are refused unless the manifest
    consents to deactivating them;
 7. the names this installer creates (the LUKS mapping, the root volume's device-mapper name, VG gentoo)
    are free;
 8. consented volumes are released and the identity is checked once more;
 9. only then: wipefs, partition table, partitions, signature clean-up, mkfs, mounts. Legacy BIOS
    disks also get the boot flag that strict BIOS firmware looks for (protective MBR or active partition).
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import re
import signal
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from gentoo_installer import blockdev
from gentoo_installer import layout as guided_layout
from gentoo_installer.manifest import InstallManifest, Partition

from .inspect import BY_PATH_DIR, MD_MEMBER_TYPES, PROC_CMDLINE, Disk, Discovery, VolumeSet, find_disk, find_volume_sets, holders, parent_disks, resolve_by_path
from .runner import Runner, RunnerError

BIOS_BOOT_GUID = "21686148-6449-6e6f-744e-656564454649"
ESP_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
# Filesystems that may legitimately sit behind a declared type (ext4 mounts ext2/ext3).
COMPATIBLE_FSTYPES = {"ext4": {"ext4", "ext3", "ext2"}, "xfs": {"xfs"}, "btrfs": {"btrfs"}, "f2fs": {"f2fs"}, "vfat": {"vfat"}, "swap": {"swap"}}
# cryptsetup(8) RETURN CODES
CRYPTSETUP_HINTS = {
    1: "invalid parameters",
    2: "no permission or wrong passphrase",
    3: "out of memory (the argon2 key derivation needs RAM; free memory or add swap and retry)",
    4: "wrong device specified",
    5: "device already exists or is busy",
}
BTRFS_MOUNT_OPTIONS = "compress=zstd:3"
MANUAL_TARGET_TYPES = frozenset({"part", "crypt", "lvm", "md", "raid0", "raid1", "raid4", "raid5", "raid6", "raid10", "dm", "mpath"})
XFS_CONFIG_DIR = "/usr/share/xfsprogs/mkfs"
# ext4 below this size gets the "small" profile (the Handbook: -T small keeps the inode tables from eating a small partition).
SMALL_EXT4_BYTES = 8 * 1024**3
STICKY_MOUNTS = frozenset({"/tmp", "/var/tmp"})
SWAP_FILE = "/swapfile"
BTRFS_SWAP_DIRECTORY = "/swap"
MIN_BOOT_MIB = 256
MIN_ESP_MIB = 64
GIB = 1024**3
# A release command (umount, swapoff, cryptsetup close, ...) that hangs on a dead disk must not hang the cleanup.
CLEANUP_TIMEOUT_SECONDS = 120.0


def cryptsetup_hint(argv: Sequence[str], error: BaseException) -> str:
    """Plain-English reason for a failed cryptsetup command ("" for any other command or exit status)."""
    code = getattr(error, "returncode", None)
    if argv and argv[0] == "cryptsetup" and code in CRYPTSETUP_HINTS:
        return f"cryptsetup {argv[1] if len(argv) > 1 else ''} failed ({code}: {CRYPTSETUP_HINTS[code]})"
    return ""


class StorageError(RuntimeError):
    """A storage precondition failed or a step went wrong; ``code`` is machine-readable (storage.*)."""

    def __init__(self, message: str, code: str = "storage.error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LayoutEntry:
    device: str
    mountpoint: str
    filesystem: str
    format: bool = True
    # Extra mount options, appended to the fstab defaults (for example "subvol=@,compress=zstd:3").
    options: str = ""
    # The fstab "file system" column when it is not UUID=/device: a swap file inside the target
    # (for example "/swapfile"). ``device`` is then the file's path on the live system.
    source: str = ""


class StorageManager:
    def __init__(
        self,
        runner: Runner,
        target_root: str | Path = "/mnt/gentoo",
        *,
        allow_unverified_disk: bool = False,
        cleanup_retries: int = 3,
        cleanup_delay: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        by_path_dir: Path | str = BY_PATH_DIR,
        cmdline_path: Path | str = PROC_CMDLINE,
        proc_root: Path | str = "/proc",
        dev_root: Path | str = "/dev",
        kill: Callable[[int, int], None] = os.kill,
        xfs_config_dir: Path | str = XFS_CONFIG_DIR,
    ) -> None:
        self.runner = runner
        self.target_root = Path(target_root)
        self.allow_unverified_disk = allow_unverified_disk
        self.cleanup_retries = max(1, cleanup_retries)
        self.cleanup_delay = cleanup_delay
        self._sleep = sleep
        self.by_path_dir = Path(by_path_dir)
        self.cmdline_path = Path(cmdline_path)
        self.proc_root = Path(proc_root)
        self.dev_root = Path(dev_root)
        self._kill = kill
        self.xfs_config_dir = Path(xfs_config_dir)
        self.layout: list[LayoutEntry] = []
        self.owned_mounts: list[Path] = []
        self.owned_swaps: list[str] = []
        self.owned_maps: list[str] = []
        self.owned_vgs: list[str] = []
        # Human-readable findings for the install log (warnings that do not stop the install).
        self.notes: list[str] = []
        self._signatures: dict[str, tuple[Any, ...]] = {}
        self._stack: dict[str, str] = {}
        self._target_nodes: list[Disk] = []
        self._labels: dict[str, str] = {}
        self._sector_size = 512
        self._subvolumes: list[str] = []

    # ------------------------------------------------------------------ preparation

    def memory_kib(self) -> int:
        """MemTotal of this computer (0 when it cannot be read): the Handbook's swap sizes depend on it."""
        try:
            for line in (self.proc_root / "meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            pass
        return 0

    def resolved(self, manifest: InstallManifest) -> InstallManifest:
        """The manifest with the swap size worked out ("recommended" and "hibernate" follow the Handbook table for this computer's memory)."""
        storage = guided_layout.resolve_swap(manifest.storage, self.memory_kib())
        return manifest if storage is manifest.storage else dataclasses.replace(manifest, storage=storage)

    def prepare(self, manifest: InstallManifest, *, dry_run: bool = False) -> list[LayoutEntry]:
        self.target_path("/")
        problem = guided_layout.swap_kind_problem(manifest.storage, manifest.hardware.hibernate)
        if problem:
            raise StorageError(problem, "storage.layout")
        manifest = self.resolved(manifest)
        self.manifest = manifest
        storage = manifest.storage
        self.notes = []
        self._check_static(manifest)
        if dry_run:
            self.layout = self._erase_layout(dry_run=True) if storage.mode == "erase" else self._manual_layout(dry_run=True)
            self._create_swap_file(dry_run=True)
            return self.layout
        if self.mount_source(self.target_root):
            raise StorageError("Target directory is already mounted; use verified resume", "storage.mounted")
        if not storage.device_id and not self.allow_unverified_disk:
            raise StorageError(
                "The manifest does not pin the target disk: set storage.device_id to the disk's serial number or WWN. "
                "A disk with neither can be pinned by its bus connection (storage.device_id_kind \"path\", "
                "storage.device_id \"<by-path link name>|<size in bytes>|<model>\"; the installer window offers this), "
                "or start gentoo-installerd with --allow-unverified-disk to accept the kernel name alone",
                "storage.unpinned",
            )
        discovery = self._discovery()
        disks = discovery.disks()
        live = discovery.live_medium_keys(disks)
        erase = storage.mode == "erase"
        targets = [storage.device] if erase else [p.device for p in storage.partitions]
        nodes = [self._find(disks, device) for device in targets]
        roots = self._touched_roots(disks, targets, nodes)
        selected = self._find(disks, storage.device)
        self._refuse_unusable_roots(roots, live)
        for device, node in zip(targets, nodes, strict=True):
            self._refuse_mounted(device, node)
        if erase and nodes[0].type not in {"disk", "loop"}:
            raise StorageError("Erase mode requires a whole disk", "storage.target")
        by_path = self._verify_identities(disks, selected, roots, erase)
        if erase:
            self._check_capacity(nodes[0])
            rewritten = [nodes[0]]
        else:
            self._check_manual(disks, nodes, selected, roots)
            rewritten = [node for partition, node in zip(storage.partitions, nodes, strict=True) if partition.format]
        self._check_name_collisions(rewritten)
        disks = self._release_volumes(rewritten, disks)
        for root in roots.values():
            if root.type in {"disk", "loop"}:
                self._signatures[root.path] = (root.serial, root.wwn, root.size, by_path.get(root.path, ""))
        self._target_nodes = rewritten if erase else []
        self._sector_size = (nodes[0].log_sec or 512) if erase else 512
        # Everything above is checked before the first command that writes.
        self.revalidate()
        self.layout = self._erase_layout() if erase else self._manual_layout()
        self.revalidate()
        fresh = self._discovery().disks()
        for entry in self.layout:
            if not entry.mountpoint or entry.source:
                continue
            if find_disk(fresh, entry.device).mountpoints:
                raise StorageError(f"Refusing to mount an already mounted target: {entry.device}", "storage.mounted")
        if not erase:
            self._mark_manual_disk_bootable(fresh)
        self._mount_layout(dry_run=False)
        self._create_swap_file(dry_run=False)
        return self.layout

    def _discovery(self) -> Discovery:
        return Discovery(self.runner, cmdline_path=self.cmdline_path, by_path_dir=self.by_path_dir)

    def _check_static(self, manifest: InstallManifest) -> None:
        """Checks that need no device (and so also run for a preview)."""
        storage = manifest.storage
        problem = storage.raid_problem()
        if problem:
            raise StorageError(problem, "storage.raid")
        if storage.separate_home and storage.home_mib <= 0:
            raise StorageError("A separate home partition needs a positive size", "storage.layout")
        if storage.fstab_ident == "partuuid" and storage.mode == "erase" and storage.table == "dos":
            raise StorageError("Naming filesystems by partition UUID needs a GPT partition table; choose the filesystem UUID or the label", "storage.layout")
        if storage.boot_noauto and not guided_layout.has_boot_partition(storage) and not any(p.mountpoint == "/boot" for p in storage.partitions):
            raise StorageError("Mounting /boot only when needed (noauto) needs a /boot partition of its own", "storage.layout")
        if storage.mode == "manual" and manifest.system.initramfs != "dracut" and any(p.mountpoint == "/usr" for p in storage.partitions):
            raise StorageError("A separate /usr partition needs an initramfs (dracut) to be mounted before the system starts", "storage.layout")
        if storage.mode == "manual" and manifest.system.bootloader == "grub" and any(p.mountpoint == "/" and p.filesystem == "f2fs" for p in storage.partitions) \
                and not any(p.mountpoint == "/boot" for p in storage.partitions):
            raise StorageError("GRUB cannot reliably read an F2FS root: keep /boot on a separate ext4 partition, or choose another root filesystem", "storage.layout")
        problem = guided_layout.swap_kind_problem(storage, manifest.hardware.hibernate)  # again, now that "recommended" has a size: an unencrypted swap partition must never be created
        if problem:
            raise StorageError(problem, "storage.layout")
        problems = guided_layout.table_problems(storage)
        if problems:
            raise StorageError(problems[0], "storage.layout")
        if storage.mode == "manual" and storage.luks and not any(p.mountpoint == "/boot" for p in storage.partitions):
            raise StorageError("Manual LUKS installation requires a separate unencrypted /boot partition", "storage.layout")
        if storage.mode == "manual" and (storage.luks or storage.lvm):
            if any(p.mountpoint == "/" and not p.format for p in storage.partitions):
                raise StorageError("Preserved root cannot create a new LUKS/LVM stack; select an already opened volume with stack creation disabled", "storage.layout")

    def _find(self, disks: Sequence[Disk], device: str) -> Disk:
        try:
            return blockdev.find(disks, device)
        except LookupError:
            raise StorageError(f"Target device {device} was not found; it may have been unplugged or renamed since it was selected", "storage.absent") from None
        except ValueError:
            raise StorageError(f"Target device {device} is ambiguous: two different devices share that path", "storage.ambiguous") from None

    def _touched_roots(self, disks: Sequence[Disk], targets: Sequence[str], nodes: Sequence[Disk]) -> dict[str, Disk]:
        """Whole disks (and loop devices) the targets live on, by path."""
        roots: dict[str, Disk] = {}
        for device, node in zip(targets, nodes, strict=True):
            owners = parent_disks(disks, node) or ([node] if node.type in {"disk", "loop"} else [])
            if not owners:
                raise StorageError(f"Cannot tell which disk {device} lives on", "storage.target")
            for owner in owners:
                roots[owner.path] = owner
        return roots

    def _refuse_unusable_roots(self, roots: dict[str, Disk], live: set[str]) -> None:
        for root in roots.values():
            problem = blockdev.install_target_problem(root, live_keys=live, allow_loop=True)
            if problem:
                code = "storage.live_medium" if problem == "live_medium" else "storage.target"
                advice = "; choose another disk (installing onto it would destroy the installer)" if problem == "live_medium" else ""
                raise StorageError(f"{root.path} {blockdev.TARGET_PROBLEMS[problem]}{advice}", code)

    def _refuse_mounted(self, requested: str, node: Disk) -> None:
        mounted = sorted({mount for sub in node.walk() for mount in sub.mountpoints if mount != "[SWAP]"})
        if mounted:
            raise StorageError(f"Target is mounted or contains a live filesystem: {requested} ({', '.join(mounted)}); unmount it first", "storage.mounted")

    # ---- identity and approvals

    def _path_identity_ok(self, disk: Disk, value: str, disks: Sequence[Disk]) -> bool:
        """A by-path identity: the link resolves to this disk, size and model match, and no other disk does."""
        name = value.split("|", 1)[0]
        if resolve_by_path(name, self.by_path_dir) != os.path.realpath(disk.path):
            return False
        if not blockdev.identity_matches(disk, "path", value, name):
            return False
        claimants = [
            other for other in disks
            if other.type == "disk" and resolve_by_path(name, self.by_path_dir) == os.path.realpath(other.path) and blockdev.identity_matches(other, "path", value, name)
        ]
        return len(claimants) == 1

    def _identity_ok(self, disk: Disk, disks: Sequence[Disk], kind: str, value: str) -> bool:
        if not value:
            return False
        if disk.type == "loop" and value == disk.path:
            return True  # loop devices have no serial; tests and disk images pin them by path
        if kind == "path":
            return self._path_identity_ok(disk, value, disks)
        return blockdev.identity_matches(disk, kind, value)

    def _approval_ok(self, disk: Disk, disks: Sequence[Disk], approval: str) -> bool:
        if "|" in approval:
            return self._path_identity_ok(disk, approval, disks)
        return self._identity_ok(disk, disks, "serial", approval)

    def _require_unique(self, disk: Disk, disks: Sequence[Disk], kind: str, value: str) -> None:
        """A serial or WWN that two attached disks share (cheap USB bridges, cloned drives) does not tell them apart."""
        if kind == "path" or disk.type == "loop":
            return
        twins = [other for other in disks if other.type == "disk" and other.key != disk.key and blockdev.identity_matches(other, kind, value)]
        if twins:
            raise StorageError(
                f"{disk.path} cannot be told apart from {', '.join(twin.path for twin in twins)}: they report the same serial number or WWN ({value}). "
                "Pin the disk by its bus connection instead (storage.device_id_kind \"path\"), or disconnect the other disk",
                "storage.identity",
            )

    def _identity_error(self, disk: Disk) -> StorageError:
        hint = ""
        if not disk.serial and not disk.wwn and disk.type != "loop":
            hint = (
                f" ({disk.path} has no serial number or WWN; pin it by its bus connection instead: "
                "storage.device_id_kind \"path\" with storage.device_id \"<by-path link name>|<size>|<model>\")"
            )
        return StorageError("Target disk identity does not match the approved manifest" + hint, "storage.identity")

    def _verify_identities(self, disks: Sequence[Disk], selected: Disk, roots: dict[str, Disk], erase: bool) -> dict[str, str]:
        """Check the pin on the selected disk and the approval of every other disk touched.

        Returns the by-path name that approved each path-identified disk (kept in the resume receipt).
        """
        storage = self.manifest.storage
        by_path: dict[str, str] = {}
        override = self.allow_unverified_disk
        pin = storage.device_id
        if selected.type in {"disk", "loop"}:
            if pin and not self._identity_ok(selected, disks, storage.device_id_kind, pin):
                raise self._identity_error(selected)
            if not pin and not override:
                raise self._identity_error(selected)
            if pin:
                self._require_unique(selected, disks, storage.device_id_kind, pin)
            if pin and storage.device_id_kind == "path":
                by_path[selected.path] = pin.split("|", 1)[0]
        elif not override and not any(self._identity_ok(root, disks, storage.device_id_kind, pin) for root in roots.values()):
            # An assembled array or volume was picked: the pin has to approve one of the disks under it.
            raise self._identity_error(selected)
        if erase:
            return by_path
        selected_path = os.path.realpath(selected.path)
        for root in roots.values():
            if override or os.path.realpath(root.path) == selected_path or (pin and self._identity_ok(root, disks, storage.device_id_kind, pin)):
                continue
            for approval in storage.additional_device_ids:
                if self._approval_ok(root, disks, approval):
                    if "|" in approval:
                        by_path[root.path] = approval.split("|", 1)[0]
                    else:
                        self._require_unique(root, disks, "serial", approval)
                    break
            else:
                raise StorageError(
                    f"The manual layout touches {root.path} ({root.model or 'unknown model'}, {guided_layout.human_size(root.size)}, "
                    f"{guided_layout.identity_text(root)}), which was not approved: add its serial number or WWN to "
                    "storage.additional_device_ids (the installer window does this when you pick a partition on that disk)",
                    "storage.not_approved",
                )
        if not override and selected.type in {"disk", "loop"} and selected_path not in {os.path.realpath(path) for path in roots}:
            raise StorageError(
                f"The selected disk {storage.device} holds none of the manual partitions; select the disk that carries the boot partition or the root",
                "storage.target",
            )
        return by_path

    # ---- capacity and preserved partitions

    def _check_capacity(self, disk: Disk) -> None:
        errors, warnings = guided_layout.capacity_problems(self.manifest, disk.size, disk.log_sec or 512)
        if errors:
            raise StorageError(errors[0], "storage.capacity")
        self.notes.extend(warnings)

    def _check_manual(self, disks: Sequence[Disk], nodes: Sequence[Disk], selected: Disk, roots: dict[str, Disk]) -> None:
        storage = self.manifest.storage
        rows = storage.partitions
        for node in nodes:
            if node.type not in MANUAL_TARGET_TYPES:
                raise StorageError(f"Manual target is not a usable partition or mapped volume: {node.path}", "storage.target")
        root_node = nodes[next(i for i, row in enumerate(rows) if row.mountpoint == "/")]
        if not storage.luks and any(parent.type == "crypt" and any(sub.key == root_node.key for sub in parent.walk()) for disk in disks for parent in disk.walk()):
            raise StorageError("Preserving a pre-opened encrypted root needs explicit boot configuration and is not supported", "storage.layout")
        if storage.luks:
            boot_node = nodes[next(i for i, row in enumerate(rows) if row.mountpoint == "/boot")]
            if any(parent.type == "crypt" and any(sub.key == boot_node.key for sub in parent.walk()) for disk in disks for parent in disk.walk()):
                raise StorageError("The separate /boot partition must be unencrypted", "storage.layout")
        for row, node in zip(rows, nodes, strict=True):
            self._check_row(row, node)
        boot_disk = selected
        if storage.firmware == "bios" and boot_disk.pttype == "gpt" and not any(sub.parttype.lower() == BIOS_BOOT_GUID for sub in boot_disk.walk()):
            raise StorageError(
                f"Legacy BIOS boot from the GPT disk {boot_disk.path} needs a \"BIOS boot\" partition (type EF02, about 1 MiB); "
                "create one first (for example with GParted) or select UEFI",
                "storage.bios_boot",
            )

    def _check_row(self, row: Partition, node: Disk) -> None:
        if not row.format and row.filesystem != "none":
            allowed = COMPATIBLE_FSTYPES.get(row.filesystem, {row.filesystem})
            if node.fstype not in allowed:
                found = node.fstype or "no recognisable filesystem"
                raise StorageError(
                    f"{row.device} is kept (format=false) and declared {row.filesystem}, but it contains {found}; format it or correct the declaration",
                    "storage.preserved",
                )
        if row.mountpoint in {"/efi", "/boot/efi"}:
            if row.filesystem != "vfat":
                raise StorageError(f"The EFI System Partition {row.device} must be vfat, not {row.filesystem}", "storage.layout")
            if node.size < MIN_ESP_MIB * guided_layout.MIB:
                raise StorageError(f"The EFI System Partition {row.device} is only {guided_layout.human_size(node.size)}; at least {MIN_ESP_MIB} MiB are needed", "storage.capacity")
            if node.size < 256 * guided_layout.MIB and (self.manifest.system.bootloader != "grub" or self.manifest.system.uki):
                self.notes.append(f"The EFI System Partition {row.device} is small ({guided_layout.human_size(node.size)}) for kernels and initramfs images; 512 MiB or more is recommended")
        if row.mountpoint == "/boot" and node.size < MIN_BOOT_MIB * guided_layout.MIB:
            raise StorageError(f"{row.device} is too small for /boot ({guided_layout.human_size(node.size)}); at least {MIN_BOOT_MIB} MiB are needed for kernels and initramfs images", "storage.capacity")
        if row.mountpoint == "/":
            if row.filesystem not in {"ext4", "xfs", "btrfs", "f2fs"}:
                raise StorageError(f"The root filesystem {row.device} must be ext4, XFS, Btrfs or F2FS, not {row.filesystem}", "storage.layout")
            minimum = guided_layout.minimum_root_mib(self.manifest) * guided_layout.MIB
            if node.size < minimum:
                raise StorageError(
                    f"The root partition {row.device} is {guided_layout.human_size(node.size)}, but a {self.manifest.system.desktop} installation needs at least "
                    f"{guided_layout.human_size(minimum)}. Choose a larger partition or a lighter desktop.",
                    "storage.capacity",
                )
            if node.size < guided_layout.recommended_root_mib(self.manifest) * guided_layout.MIB:
                self.notes.append(f"The root partition {row.device} is {guided_layout.human_size(node.size)}; {guided_layout.recommended_root_mib(self.manifest) // 1024} GiB or more is recommended so updates and builds have room")

    # ---- holders, signatures, name collisions

    def _check_name_collisions(self, rewritten: Sequence[Disk]) -> None:
        """The names this installer creates (the LUKS mapping, the root volume's device-mapper name, VG gentoo) must be free.

        Checked before the first write; anything that lives on the devices about to be rewritten
        does not count, it disappears with them (and is released first).
        """
        s = self.manifest.storage
        releasing = {holder.name for node in rewritten for holder in holders(node)}
        wanted: list[tuple[str, str]] = []  # (device-mapper name, the command that frees it)
        if s.luks:
            mapper = guided_layout.luks_mapper_name(s)
            wanted.append((mapper, f"cryptsetup close {mapper}"))
        if s.lvm:
            wanted.append(("gentoo-root", "dmsetup remove gentoo-root"))  # LVM names the volume <group>-<volume>
        for name, command in wanted:
            if (self.dev_root / "mapper" / name).exists() and name not in releasing:
                raise StorageError(f"A device-mapper volume named {name} is already active; close it ({command}) and retry", "storage.collision")
        if s.lvm:
            try:
                listing = self.runner.run(("pvs", "--noheadings", "--separator", ":", "-o", "pv_name,vg_name")).stdout
            except (RunnerError, OSError):
                return  # no LVM tools or no PVs: nothing can collide
            erased = {os.path.realpath(n.path) for node in rewritten for n in node.walk()}
            for line in listing.splitlines():
                pv, _, group = line.strip().partition(":")
                if group == "gentoo" and os.path.realpath(pv) not in erased:
                    raise StorageError(f"An LVM volume group named 'gentoo' already exists on {pv}; rename it (vgrename gentoo <new-name>) or remove it before installing", "storage.collision")

    def _describe_holder(self, holder: Disk, node: Disk, disks: Sequence[Disk]) -> str:
        own = {parent.path for parent in parent_disks(disks, node)}
        others = [parent.path for parent in parent_disks(disks, holder) if parent.path not in own]
        spans = f" (also uses {', '.join(others)})" if others else ""
        return f"{holder.type} {holder.path}{spans}"

    def _release_volumes(self, rewritten: Sequence[Disk], disks: list[Disk]) -> list[Disk]:
        """Refuse, or with the manifest's consent deactivate, everything alive on the devices to be rewritten.

        Covers active LVM / dm-crypt / md volumes and active swap (lsblk shows them) and LVM / md sets
        that span other disks (found with pvs and mdadm --examine, because the live system does not
        assemble or activate them by itself any more) and Btrfs / ZFS / bcachefs pools with a member on
        another disk (lsblk shows the same filesystem UUID on every member).
        """
        consent = self.manifest.storage.release_holders
        found = [(node, holder) for node in rewritten for holder in holders(node)]
        swaps = [sub.path for node in rewritten for sub in node.walk() if "[SWAP]" in sub.mountpoints]
        sets: list[VolumeSet | blockdev.FilesystemPool] = [*find_volume_sets(self.runner, disks, rewritten), *blockdev.filesystem_pools(disks, rewritten)]
        spanning = [item for item in sets if item.spans_other_disks]
        for item in sets:
            if not item.spans_other_disks:
                self.notes.append(f"The target holds {item.describe()}; it is destroyed together with the disk")
        if not (found or swaps or spanning):
            return disks
        if not consent:
            reasons = []
            if found:
                reasons.append("Active volumes sit on the disk or partition that would be rewritten: " + "; ".join(self._describe_holder(h, n, disks) for n, h in found))
            if swaps:
                reasons.append("Swap is active on " + ", ".join(swaps))
            for item in spanning:
                reasons.append(
                    f"It is a member of the {item.describe()}: erasing it would also destroy data stored on {', '.join(item.outside)}"
                )
            raise StorageError(
                " ".join(reasons) + ". Deactivate or remove them first (swapoff, vgchange -an, cryptsetup close, mdadm --stop), or allow the installer "
                "to switch them off and erase them: tick the box in the installer window, answer the question in the text installer, "
                "or pass --release-holders to an unattended run",
                "storage.holders",
            )
        for item in spanning:
            self.notes.append(f"Consent given: erasing a member of the {item.describe()}")
        for path in swaps:
            self._run(("swapoff", path))
        volume_groups: set[str] = set()
        containers: list[Disk] = []
        for _, holder in found:
            if holder.type == "lvm":
                group = self._volume_group_of(holder)
                if group not in volume_groups:
                    volume_groups.add(group)
                    self._deactivate_group(group)
            elif holder.type == "crypt":
                self._run(("cryptsetup", "close", holder.name))
            elif holder.type == "md":
                containers.append(holder)  # firmware-RAID containers stop after their volumes
            elif holder.type.startswith("raid"):
                self._run(("mdadm", "--stop", holder.path))
            else:
                self._run(("dmsetup", "remove", "--retry", holder.name))
        for container in containers:
            self._run(("mdadm", "--stop", container.path))
        self._run(("udevadm", "settle"))
        fresh = self._discovery().disks()
        for node in rewritten:
            current = find_disk(fresh, node.path)
            if holders(current) or any("[SWAP]" in sub.mountpoints for sub in current.walk()):
                raise StorageError(f"Volumes on {node.path} are still active after deactivation; stop them manually and retry", "storage.holders")
        return fresh

    def _volume_group_of(self, holder: Disk) -> str:
        """The volume group of an active logical volume, read from its device-mapper name.

        Not with lvs: it cannot resolve the hidden and layered devices of thin pools, snapshots and RAID
        volumes (/dev/mapper/vg0-pool-tpool). dmsetup splitname only parses the name and knows the layers.
        """
        try:
            group = self.runner.run(("dmsetup", "splitname", "--noheadings", "-o", "vg_name", holder.name, "LVM")).stdout.strip()
        except (RunnerError, OSError):
            group = ""
        if not group:
            raise StorageError(
                f"Cannot determine the volume group of {holder.path}; deactivate it yourself (vgchange -an <volume group>) and try again",
                "storage.holders",
            )
        return group

    def _deactivate_group(self, group: str) -> None:
        try:
            self.runner.run(("vgchange", "-an", group))
        except RunnerError as exc:
            raise StorageError(
                f"Cannot deactivate the LVM volume group {group}: something still uses it. Stop that, run \"vgchange -an {group}\" yourself and try again",
                "storage.holders",
            ) from exc

    # ------------------------------------------------------------------ layout

    def _partition_device(self, disk: str, number: int) -> str:
        disk = os.path.realpath(disk)
        return f"{disk}p{number}" if disk[-1:].isdigit() else f"{disk}{number}"

    def _old_partitions(self, disk: str) -> list[str]:
        canonical = os.path.realpath(disk)
        for node in self._target_nodes:
            if os.path.realpath(node.path) == canonical:
                # The disk's own partitions only (lsblk lists MBR logical partitions flat next to the others). The
                # partitions of an md array or a multipath map sit deeper and are gone once that volume is released.
                # An MBR extended-partition container shows up as a 1 KiB "part"; it holds no signature to wipe.
                return [child.path for child in node.children if child.type == "part" and child.size >= guided_layout.MIB]
        return []

    def use_btrfs_subvolumes(self) -> bool:
        """Whether the guided Btrfs layout uses the @ and @home subvolumes (every guided Btrfs install does)."""
        s = self.manifest.storage
        return s.mode == "erase" and s.filesystem == "btrfs"

    def _erase_layout(self, *, dry_run: bool = False) -> list[LayoutEntry]:
        s = self.manifest.storage
        disk = s.device
        entries: list[LayoutEntry] = []
        created: list[str] = []
        number = 1
        self._parted_start = 1
        # Signatures inside the partitions that are about to disappear must not survive: a stale
        # md/LVM/LUKS label at a reused offset would be auto-assembled and keep the new partition busy,
        # and mkfs only clears the first few sectors. Partitions get a plain `wipefs --all`: wipefs(8)
        # erases a mounted device only with --force, so a mount that slipped past the guards is
        # refused instead of corrupted.
        for old in self._old_partitions(disk):
            self._run(("wipefs", "--all", old))
        if s.table == "gpt":
            self._run(("sgdisk", "--zap-all", disk))
        else:
            # --force is required to erase a partition-table signature; `mklabel msdos` leaves a stale backup GPT behind.
            self._run(("wipefs", "--all", "--force", disk))
            self._run(("parted", "--script", disk, "mklabel", "msdos"))
        if s.firmware == "uefi":
            efi = self._partition_device(disk, number)
            self._new_partition(number, f"+{guided_layout.ESP_MIB}MiB", "EF00", "EFI")
            entries.append(LayoutEntry(efi, "/efi", "vfat"))
            created.append(efi)
            number += 1
        elif s.table == "gpt":
            self._new_partition(number, f"+{guided_layout.BIOS_BOOT_MIB}MiB", "EF02", "BIOS")
            number += 1
        boot_number = 0
        if guided_layout.has_boot_partition(s):
            boot = self._partition_device(disk, number)
            self._new_partition(number, f"+{guided_layout.BOOT_MIB}MiB", "8300", "boot")
            entries.append(LayoutEntry(boot, "/boot", "ext4"))
            created.append(boot)
            boot_number = number
            number += 1
        swap_device = ""
        home_device = ""
        if guided_layout.swap_partition_mib(s):
            swap_device = self._partition_device(disk, number)
            self._new_partition(number, f"+{guided_layout.swap_partition_mib(s)}MiB", "8200", "swap")
            created.append(swap_device)
            number += 1
        if s.separate_home:
            home_device = self._partition_device(disk, number)
            self._new_partition(number, f"+{s.home_mib}MiB", self._linux_type("home"), "home")
            created.append(home_device)
            number += 1
        root_device = self._partition_device(disk, number)
        self._new_partition(number, "0", self._linux_type("root"), "root")
        created.append(root_device)
        self._run(("partprobe", disk))
        self._run(("udevadm", "settle"))
        if s.firmware == "bios":
            self._mark_disk_bootable(disk, boot_number or number)
        for device in created:
            self._run(("wipefs", "--all", device))
        self._run(("udevadm", "settle"))
        root_device = self._prepare_stack(root_device, "root")
        subvolumes = self.use_btrfs_subvolumes()
        self._subvolumes = []
        if subvolumes:
            self._subvolumes = ["@"] + ([] if home_device else ["@home"])
            entries.append(LayoutEntry(root_device, "/", s.filesystem, options=f"subvol=@,{BTRFS_MOUNT_OPTIONS}"))
            if not home_device:
                entries.append(LayoutEntry(root_device, "/home", s.filesystem, format=False, options=f"subvol=@home,{BTRFS_MOUNT_OPTIONS}"))
        else:
            entries.append(LayoutEntry(root_device, "/", s.filesystem))
        if swap_device:
            entries.append(LayoutEntry(swap_device, "", "swap"))
        if home_device:
            entries.append(LayoutEntry(home_device, "/home", s.filesystem))
        self._labels = self._guided_labels(entries) if s.fstab_ident == "label" else {}
        self._format(entries)
        return entries

    @staticmethod
    def _guided_labels(entries: Sequence[LayoutEntry]) -> dict[str, str]:
        """Filesystem labels for a guided install whose fstab refers to labels (the first entry of a device decides: / before /home on Btrfs)."""
        labels: dict[str, str] = {}
        for entry in entries:
            name = guided_layout.FSTAB_LABELS.get("swap" if entry.filesystem == "swap" else entry.mountpoint, "")
            if name and entry.device not in labels:
                labels[entry.device] = name
        return labels

    def _mark_disk_bootable(self, disk: str, partition: int) -> None:
        """Legacy BIOS: flag the new disk the way BIOS firmware that insists on a bootable entry expects.

        GPT: the boot flag of the protective MBR. MBR: the active flag of ``partition`` (the one that
        holds /boot, else the root). sgdisk and `parted mkpart` leave both off and grub-install
        --target=i386-pc only writes boot.img and core.img, so a strict BIOS says "no bootable device".
        Never for UEFI: it does not need either, and some UEFI firmware ignores a disk whose protective
        MBR is active.
        """
        if self.manifest.storage.table == "gpt":
            self._run(("parted", "--script", disk, "disk_set", "pmbr_boot", "on"))
        else:
            self._run(("parted", "--script", disk, "set", str(partition), "boot", "on"))
        self._run(("partprobe", disk))
        self._run(("udevadm", "settle"))

    def _mark_manual_disk_bootable(self, disks: Sequence[Disk]) -> None:
        """Legacy BIOS with a manual layout: the same flags as a guided one, but only where they cannot hurt.

        The disk holds partitions the installer does not touch, so a flag that is already there stays and
        a disk that UEFI firmware may also start from (it carries an EFI System Partition) is left alone.
        Nothing here may stop the installation: without the flag most computers still start.
        """
        s = self.manifest.storage
        if s.firmware != "bios":
            return
        command: tuple[str, ...]
        try:
            disk = find_disk(disks, s.device)
            if disk.type not in {"disk", "loop"}:
                return  # an assembled array or volume has no partition table of its own
            if disk.pttype == "gpt":
                if any(sub.parttype.lower() == ESP_GUID for sub in disk.walk()):
                    return
                command = ("parted", "--script", s.device, "disk_set", "pmbr_boot", "on")
                done = f"Legacy BIOS: the boot flag of the protective MBR of {s.device} is set, as some BIOS firmware requires it"
            elif disk.pttype == "dos":
                partition = self._boot_partition_number(disk)
                if partition is None:
                    return
                listing = self.runner.run(("parted", "--script", "--machine", s.device, "print")).stdout
                if self._active_partitions(listing):
                    return  # for example Windows' own: its flag stays
                command = ("parted", "--script", s.device, "set", str(partition), "boot", "on")
                done = f"Legacy BIOS: {self._partition_device(s.device, partition)} is now the active (bootable) partition of {s.device}, as some BIOS firmware requires it"
            else:
                return
            self.runner.run(command)
            self.runner.run(("partprobe", s.device))
            self.runner.run(("udevadm", "settle"))
            self.notes.append(done)
        except (RunnerError, OSError, ValueError) as exc:
            reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            self.notes.append(
                f"Could not set the boot flag on {s.device} ({reason}). If the computer does not start from this disk, "
                "set the boot flag with a partitioning tool such as GParted"
            )

    def _boot_partition_number(self, disk: Disk) -> int | None:
        """Number of the primary partition of ``disk`` that holds /boot (else the root); None when it is not one."""
        rows = self.manifest.storage.partitions
        row = next((r for r in rows if r.mountpoint == "/boot"), None) or next((r for r in rows if r.mountpoint == "/"), None)
        if row is None:
            return None
        match = re.fullmatch(re.escape(os.path.realpath(disk.path)) + r"p?(\d+)", os.path.realpath(row.device))
        return int(match.group(1)) if match and 1 <= int(match.group(1)) <= 4 else None  # logical partitions cannot be active

    @staticmethod
    def _active_partitions(parted_listing: str) -> list[str]:
        """Numbers of the partitions that `parted --script --machine DISK print` shows with the boot (active) flag."""
        active: list[str] = []
        for line in parted_listing.splitlines():
            fields = line.strip().rstrip(";").split(":")
            if len(fields) >= 7 and fields[0].isdigit() and "boot" in {flag.strip() for flag in fields[-1].split(",")}:
                active.append(fields[0])
        return active

    def _linux_type(self, role: str) -> str:
        """GPT type code of the root ("root") or /home ("home") partition.

        With systemd-boot, the EFI stub or a Unified Kernel Image the Handbook uses the Discoverable Partitions
        codes (8304 Linux x86-64 root, 8302 /home); GRUB keeps the generic 8300. A partition that holds a LUKS or
        LVM container is not a filesystem, so the root keeps 8300 then.
        """
        s = self.manifest.storage
        system = self.manifest.system
        discoverable = system.bootloader in {"systemd-boot", "efi-stub"} or system.uki
        if not discoverable or (role == "root" and (s.luks or s.lvm)):
            return "8300"
        return "8304" if role == "root" else "8302"

    def _new_partition(self, number: int, size: str, typecode: str, name: str) -> None:
        s = self.manifest.storage
        if s.table == "gpt":
            self._run(("sgdisk", f"--new={number}:0:{size}", f"--typecode={number}:{typecode}", f"--change-name={number}:{name}", s.device))
        else:
            fs_type = "fat32" if typecode == "EF00" else "linux-swap" if typecode == "8200" else "ext4"
            start = self._parted_start
            if size == "0":
                end = "100%"
            else:
                size_mib = int(size.removeprefix("+").removesuffix("MiB"))
                self._parted_start += size_mib
                end = f"{self._parted_start}MiB"
            self._run(("parted", "--script", s.device, "mkpart", "primary", fs_type, f"{start}MiB", end))
            if typecode == "EF00":
                self._run(("parted", "--script", s.device, "set", str(number), "esp", "on"))

    def _manual_layout(self, *, dry_run: bool = False) -> list[LayoutEntry]:
        rows = self.manifest.storage.partitions
        entries = [LayoutEntry(p.device, p.mountpoint, p.filesystem, p.format) for p in rows]
        roots = [index for index, entry in enumerate(entries) if entry.mountpoint == "/"]
        if len(roots) != 1:
            raise StorageError("Exactly one root mountpoint is required", "storage.layout")
        devices = [os.path.realpath(entry.device) for entry in entries]
        if len(set(devices)) != len(devices):
            raise StorageError("Manual layout contains duplicate target devices", "storage.layout")
        mountpoints = [os.path.normpath(entry.mountpoint) for entry in entries if entry.mountpoint]
        if len(set(mountpoints)) != len(mountpoints):
            raise StorageError("Manual layout contains duplicate mountpoints", "storage.layout")
        self._labels = {row.device: row.label for row in rows if row.label and row.format}
        index = roots[0]
        root = entries[index]
        if self.manifest.storage.luks or self.manifest.storage.lvm:
            mapped = self._prepare_stack(root.device, "root")
            entries[index] = LayoutEntry(mapped, "/", root.filesystem, root.format)
            if root.device in self._labels:
                self._labels[mapped] = self._labels.pop(root.device)
        self._format(entries)
        return entries

    def _prepare_stack(self, device: str, name: str) -> str:
        s = self.manifest.storage
        current = device
        if s.luks:
            mapper = guided_layout.luks_mapper_name(s, name)
            if (self.dev_root / "mapper" / mapper).exists():
                raise StorageError(f"Mapper already exists: {mapper}", "storage.collision")
            self._stack["luks_device"] = current
            self._stack["mapper"] = mapper
            self._run(("cryptsetup", "luksFormat", "--batch-mode", "--type", "luks2", "--key-file=-", current), stdin=s.luks_passphrase, secret=True)
            self._run(("cryptsetup", "open", "--key-file=-", current, mapper), stdin=s.luks_passphrase, secret=True)
            current = f"/dev/mapper/{mapper}"
            self.owned_maps.append(mapper)
        if s.lvm:
            vg = "gentoo"
            self._run(("pvcreate", "--force", current))
            self._run(("vgcreate", vg, current))
            self.owned_vgs.append(vg)
            self._stack["vg"] = vg
            self._stack["pv_device"] = current
            self._run(("lvcreate", "--yes", "--extents", "100%FREE", "--name", name, vg))
            current = f"/dev/{vg}/{name}"
        return current

    def _format(self, entries: Iterable[LayoutEntry]) -> None:
        done: set[str] = set()
        for entry in entries:
            if not entry.format or entry.source or entry.device in done:
                continue
            done.add(entry.device)
            label = self._labels.get(entry.device, "")
            commands: dict[str, tuple[str, ...] | None] = {
                "ext4": ("mkfs.ext4", "-F", *self._ext4_profile(entry.device), *(("-L", label) if label else ()), entry.device),
                "xfs": ("mkfs.xfs", "-f", *self._xfs_options(), *(("-L", label) if label else ()), entry.device),
                "btrfs": ("mkfs.btrfs", "-f", *(("-L", label) if label else ()), entry.device),
                "f2fs": ("mkfs.f2fs", "-f", *(("-l", label) if label else ()), entry.device),
                "vfat": ("mkfs.vfat", "-F", "32", *(("-S", "4096") if self._sector_size == 4096 else ()), *(("-n", label) if label else ()), entry.device),
                "swap": ("mkswap", *(("-L", label) if label else ()), entry.device),
                "none": None,
            }
            if entry.filesystem not in commands:
                raise StorageError(f"Unsupported filesystem: {entry.filesystem}", "storage.layout")
            command = commands[entry.filesystem]
            if command:
                self._run(command)

    def _device_bytes(self, device: str) -> int:
        """Size of a block device in bytes (0 when it cannot be read)."""
        try:
            return int(self.runner.run(("blockdev", "--getsize64", device)).stdout.strip().splitlines()[0])
        except (RunnerError, OSError, ValueError, IndexError):
            return 0

    def _ext4_profile(self, device: str) -> tuple[str, ...]:
        """``-T small`` for an ext4 filesystem below 8 GiB (the Handbook's "small ext4 partitions"): fewer inodes, more room for files.

        ``-T small`` means 1 KiB blocks, and on those e2fsprogs 1.47 makes a 1 MiB orphan file that the kernel refuses ("orphan file
        too big", the mount fails with "Structure needs cleaning"): the first real VM run of a separate /boot showed it on 6.18. The
        orphan file only speeds up deleting many open files at once, which /boot never does, so it is left out.
        """
        size = self._device_bytes(device)
        return ("-T", "small", "-O", "^orphan_file") if 0 < size < SMALL_EXT4_BYTES else ()

    def _xfs_options(self) -> tuple[str, ...]:
        """The newest mkfs.xfs configuration xfsprogs ships (lts_*.conf), when there is one: the Handbook's recommended defaults."""
        try:
            configs = [path for path in self.xfs_config_dir.glob("lts_*.conf") if path.is_file()]
        except OSError:
            return ()
        if not configs:
            return ()

        def version(path: Path) -> tuple[int, ...]:
            return tuple(int(part) for part in re.findall(r"\d+", path.stem))

        return ("-c", f"options={max(configs, key=version)}")

    # ------------------------------------------------------------------ mounting

    def _mount_command(self, entry: LayoutEntry, destination: Path) -> tuple[str, ...]:
        return ("mount", *(("-o", entry.options) if entry.options else ()), entry.device, str(destination))

    def _mount_layout(self, *, dry_run: bool) -> None:
        roots = [entry for entry in self.layout if entry.mountpoint == "/"]
        ordered = roots + sorted((entry for entry in self.layout if entry.mountpoint not in {"", "/"}), key=lambda entry: Path(entry.mountpoint).parts)
        for entry in ordered:
            destination = self.target_path(entry.mountpoint)
            if not dry_run:
                destination.mkdir(parents=True, exist_ok=True)
            if entry in roots and self._subvolumes and not dry_run:
                self._create_btrfs_subvolumes(entry, destination)
            self._run(self._mount_command(entry, destination))
            if not dry_run:
                if os.path.normpath(entry.mountpoint) in STICKY_MOUNTS:
                    os.chmod(destination, 0o1777)  # the Handbook: chmod 1777, or every user could delete the others' files
                self.owned_mounts.append(destination)
                if entry in roots:
                    # Before any other mount point is created inside it, while the root still shows only what it held.
                    self._guard_existing_system()
        for entry in self.layout:
            if entry.filesystem == "swap" and not entry.source:
                self._run(("swapon", entry.device))
                if not dry_run:
                    self.owned_swaps.append(entry.device)

    def _create_btrfs_subvolumes(self, root: LayoutEntry, destination: Path) -> None:
        """Create @ (and @home) on the new filesystem and make @ its default subvolume.

        The top level is mounted only for this. Setting the default subvolume means a plain
        root=UUID=... mounts @ even where no rootflags=subvol=@ is given (initramfs, any bootloader).
        """
        self._run(("mount", root.device, str(destination)))
        self.owned_mounts.append(destination)
        for name in self._subvolumes:
            self._run(("btrfs", "subvolume", "create", str(destination / name)))
        self._run(("btrfs", "subvolume", "set-default", str(destination / "@")))
        self._run(("umount", str(destination)))
        self.owned_mounts.remove(destination)

    def _create_swap_file(self, *, dry_run: bool) -> None:
        """Swap of an encrypted install lives in a file inside the encrypted root, never in the clear."""
        s = self.manifest.storage
        if not guided_layout.swap_in_root(s) or s.mode != "erase":
            return
        btrfs = s.filesystem == "btrfs"
        relative = f"{BTRFS_SWAP_DIRECTORY}{SWAP_FILE}" if btrfs else SWAP_FILE
        path = self.target_path(relative)
        if btrfs:
            self._run(("btrfs", "subvolume", "create", str(self.target_path(BTRFS_SWAP_DIRECTORY))))
            self._run(("btrfs", "filesystem", "mkswapfile", "--size", f"{guided_layout.disk_swap_mib(s)}m", str(path)))
        else:
            self._run(("mkswap", "--file", "--size", f"{guided_layout.disk_swap_mib(s)}MiB", str(path)))
        self._run(("swapon", str(path)))
        entry = LayoutEntry(str(path), "", "swap", True, source=relative)
        self.layout.append(entry)
        if not dry_run:
            self.owned_swaps.append(str(path))

    def _guard_existing_system(self) -> None:
        """A kept (format=false) root that already holds files is overwritten by the stage3 extraction.

        Refuse unless the manifest consents, and then keep a copy of /etc (and SSH keys) first.
        """
        s = self.manifest.storage
        root = next((entry for entry in self.layout if entry.mountpoint == "/"), None)
        if s.mode != "manual" or root is None or root.format:
            return
        base = self.target_path("/")
        try:
            names = sorted(entry.name for entry in os.scandir(base) if entry.name != "lost+found")
        except OSError as exc:
            raise StorageError(f"Cannot read the kept root filesystem {root.device}: {exc}", "storage.existing_system") from exc
        if not names:
            return
        if not s.overwrite_existing_system:
            shown = ", ".join(names[:8]) + (", ..." if len(names) > 8 else "")
            raise StorageError(
                f"The root partition {root.device} is kept (not formatted) but already contains files ({shown}). The new system would be "
                "extracted over it, replacing its /etc, accounts and boot files. To keep and repair a Gentoo system that is on it, use the rescue tools instead. "
                "To install over it, confirm the replacement in the installer, or format the partition.",
                "storage.existing_system",
            )
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        keys = [f"home/{entry.name}/.ssh" for entry in _scandir(base / "home") if (base / "home" / entry.name / ".ssh").is_dir()]
        keys += ["root/.ssh"] if (base / "root" / ".ssh").is_dir() else []
        wanted = [name for name in ("etc", *keys) if (base / name).exists()]
        if not wanted:
            return
        directory = self.target_path("/root")
        directory.mkdir(parents=True, exist_ok=True)
        archive = directory / f"pre-install-backup-{stamp}.tar.zst"
        try:
            self._run(("tar", "--zstd", "-cpf", str(archive), "-C", str(base), *wanted))
        except RunnerError as exc:
            raise StorageError(f"The safety backup of the existing system could not be written ({exc}); nothing was replaced", "storage.existing_system") from exc
        self.notes.append(f"Existing system found on {root.device}: /etc and SSH keys were saved to /root/{archive.name} before it is replaced")

    def target_path(self, mountpoint: str) -> Path:
        if not self.target_root.is_absolute() or self.target_root.resolve() == Path("/"):
            raise StorageError("Target root must be an absolute, non-root directory", "storage.target")
        if not mountpoint.startswith("/") or "\x00" in mountpoint or ".." in Path(mountpoint).parts:
            raise StorageError("Invalid mountpoint", "storage.target")
        candidate = self.target_root / mountpoint.lstrip("/")
        if any(path.is_symlink() for path in (candidate, *candidate.parents)):
            raise StorageError("Mountpoint or target root contains a symlink", "storage.target")
        root = os.path.realpath(self.target_root)
        resolved = os.path.realpath(candidate)
        if os.path.commonpath((root, resolved)) != root:
            raise StorageError("Mountpoint escapes target root", "storage.target")
        return Path(resolved)

    # ------------------------------------------------------------------ identity checks while installing

    def _signature_matches(self, node: Disk, signature: Sequence[Any]) -> bool:
        if [node.serial, node.wwn, node.size] != list(signature[:3]):
            return False
        saved_by_path = signature[3] if len(signature) > 3 else ""
        if saved_by_path:
            return resolve_by_path(str(saved_by_path), self.by_path_dir) == os.path.realpath(node.path)
        return True

    def revalidate(self) -> None:
        if not self._signatures:
            return
        disks = self._discovery().disks()
        for device, signature in self._signatures.items():
            node = self._find(disks, device)
            if not self._signature_matches(node, signature):
                raise StorageError(f"Disk identity changed during installation: {device}", "storage.identity")
            if self.manifest.storage.mode == "erase" and any(child.mountpoints and child.path != device for child in node.walk()):
                raise StorageError(f"Unexpected mounted filesystem on target: {device}", "storage.mounted")

    # ------------------------------------------------------------------ cleanup

    def cleanup(self) -> None:
        # A job cancellation must not cancel release of resources it acquired.
        run = getattr(self.runner, "run_cleanup", self.runner.run)
        errors: list[str] = []
        if self.owned_mounts:
            self._terminate_chroot_processes()

        def drain(owned: list, command: Callable[[Any], tuple[str, ...]], *, is_mount: bool = False, select: Callable[[Any], bool] = lambda _: True) -> None:
            for resource in list(reversed(owned)):
                if not select(resource):
                    continue
                failure = self._release(run, resource, command(resource), is_mount=is_mount)
                if failure is None:
                    owned.remove(resource)
                else:
                    errors.append(failure)

        swapoff = lambda value: ("swapoff", str(value))  # noqa: E731
        # A swap file lives on the target filesystem: it has to be released before that is unmounted.
        drain(self.owned_swaps, swapoff, select=self._inside_target)
        drain(self.owned_mounts, lambda value: ("umount", "-R", str(value)), is_mount=True)
        drain(self.owned_swaps, swapoff, select=lambda path: not self._inside_target(path))
        drain(self.owned_vgs, lambda value: ("vgchange", "-an", str(value)))
        drain(self.owned_maps, lambda value: ("cryptsetup", "close", str(value)))
        if errors:
            raise StorageError("Cleanup failed: " + "; ".join(errors), "storage.cleanup")

    def _inside_target(self, path: str) -> bool:
        root = os.path.realpath(self.target_root)
        return os.path.realpath(path) == root or os.path.realpath(path).startswith(root + os.sep)

    def _release(self, run: Callable[..., object], resource: object, argv: tuple[str, ...], *, is_mount: bool) -> str | None:
        """Run one release command with retries; virtual filesystems may fall back to a lazy unmount.

        Returns None on success, otherwise the failure text (naming the processes still inside the target).
        """
        last: Exception | None = None
        for attempt in range(self.cleanup_retries):
            try:
                run(argv, timeout=CLEANUP_TIMEOUT_SECONDS)
                return None
            except Exception as exc:  # noqa: BLE001 - report every failure, keep releasing the rest
                last = exc
                if attempt + 1 < self.cleanup_retries:
                    self._sleep(self.cleanup_delay * (attempt + 1))
        # Only the virtual filesystems the installer bound into the target may be detached lazily;
        # the target root itself never is (a lazy unmount would let cryptsetup close under live files).
        if is_mount and self._is_virtual_mount(Path(str(resource))):
            try:
                run(("sync",), timeout=CLEANUP_TIMEOUT_SECONDS)
                run(("umount", "-R", "-l", str(resource)), timeout=CLEANUP_TIMEOUT_SECONDS)
                return None
            except Exception as exc:  # noqa: BLE001
                last = exc
        busy = self._busy_report() if is_mount else ""
        reason = cryptsetup_hint(argv, last) if last is not None else ""
        return f"{reason or last}{busy}"

    def _is_virtual_mount(self, mount: Path) -> bool:
        return mount.parent == self.target_root and mount.name in {"dev", "proc", "sys", "run"}

    def _chroot_processes(self) -> list[tuple[int, str]]:
        """(pid, command) of processes whose root directory is the target (leftover gpg-agent, dirmngr, ...)."""
        found: list[tuple[int, str]] = []
        try:
            wanted = os.path.realpath(self.target_root)
            for entry in self.proc_root.iterdir():
                if not entry.name.isdigit() or int(entry.name) == os.getpid():
                    continue
                try:
                    if os.path.realpath(os.readlink(entry / "root")) == wanted:
                        found.append((int(entry.name), (entry / "comm").read_text().strip()))
                except OSError:
                    continue
        except OSError:
            pass
        return sorted(found)  # by pid: the order of a directory listing depends on the file system (tmpfs lists newest first)

    def _busy_report(self) -> str:
        users = [f"{pid}:{name}" for pid, name in self._chroot_processes()]
        return f" (processes still inside {self.target_root}: {', '.join(users)})" if users else ""

    def _terminate_chroot_processes(self) -> None:
        """Ask leftover chroot processes to exit, then kill them: they would keep the target busy."""
        for pid, _ in self._chroot_processes():
            try:
                self._kill(pid, signal.SIGTERM)
            except OSError:
                pass
        if self._chroot_processes():
            self._sleep(min(3.0, max(self.cleanup_delay, 0.0) * 3))
            for pid, _ in self._chroot_processes():
                try:
                    self._kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    # ------------------------------------------------------------------ receipts for resume

    def _uuid(self, device: str) -> str:
        try:
            value = self.runner.run(("blkid", "-s", "UUID", "-o", "value", device)).stdout.strip()
        except RunnerError as exc:
            raise StorageError(
                f"Cannot establish filesystem identity: {device} is missing or has no filesystem signature "
                "(an assembled RAID array or an LVM volume has to be activated first: mdadm --assemble --scan, vgchange -ay)",
                "storage.resume",
            ) from exc
        if not value or len(value.splitlines()) != 1:
            raise StorageError(f"Cannot establish filesystem identity: {device}", "storage.resume")
        return value

    def snapshot(self) -> dict:
        """Identity receipts needed to resume without ever recreating storage."""
        stack = dict(self._stack)
        if "luks_device" in stack:
            stack["luks_uuid"] = self._uuid(stack["luks_device"])
        if "vg" in stack:
            stack["vg_uuid"] = self.runner.run(("pvs", "--noheadings", "-o", "vg_uuid", stack["pv_device"])).stdout.strip()
            if not stack["vg_uuid"]:
                raise StorageError("Cannot establish volume group identity", "storage.resume")
        return {
            "version": 2,
            "signatures": {device: list(value) for device, value in self._signatures.items()},
            "uuids": {e.device: self._uuid(e.device) for e in self.layout if not e.source},
            "stack": stack,
            "md": self._md_receipt(),
            # The checkpoint's layout.json only keeps device/mountpoint/filesystem/format; options and
            # swap-file sources are restored from here.
            "entries": [asdict(entry) for entry in self.layout],
        }

    def _md_receipt(self) -> list[dict[str, Any]]:
        """The assembled md arrays the layout lives on, with the members to assemble them again.

        The live system does not assemble arrays by itself, so after a reboot a resume has to do it explicitly.
        """
        arrays: list[dict[str, Any]] = []
        block = [entry for entry in self.layout if not entry.source]
        if not block:
            return arrays
        disks = self._discovery().disks()
        for entry in block:
            try:
                node = find_disk(disks, entry.device)
            except ValueError:
                continue
            if not (node.type.startswith("raid") or node.type == "md"):
                continue
            members = sorted({
                sub.path for root in disks for sub in root.walk()
                if sub.fstype in MD_MEMBER_TYPES and any(child.key == node.key for child in sub.walk()[1:])
            })
            try:
                detail = self.runner.run(("mdadm", "--detail", "--export", node.path)).stdout
            except (RunnerError, OSError):
                continue
            uuid = next((line.split("=", 1)[1].strip() for line in detail.splitlines() if line.startswith("MD_UUID=")), "")
            if uuid and members:
                arrays.append({"device": node.path, "uuid": uuid, "members": members})
        return arrays

    def _assemble_arrays(self, arrays: object) -> None:
        """Assemble the saved md arrays that are not present (explicitly: automatic assembly is disabled)."""
        if not isinstance(arrays, list) or not arrays:
            return
        disks = self._discovery().disks()
        for array in arrays:
            device, uuid, members = str(array.get("device", "")), str(array.get("uuid", "")), [str(m) for m in array.get("members", [])]
            try:
                find_disk(disks, device)
                continue  # already assembled
            except ValueError:
                pass
            missing = [member for member in members if not any(os.path.realpath(node.path) == os.path.realpath(member) for root in disks for node in root.walk())]
            if not device or not uuid or not members or missing:
                raise StorageError(
                    f"The RAID array {device or 'of this installation'} is not assembled and its members are not all present"
                    + (f" (missing: {', '.join(missing)})" if missing else "")
                    + "; reconnect the disks and resume again",
                    "storage.resume",
                )
            self._run(("mdadm", "--assemble", f"--uuid={uuid}", device, *members))
            self._run(("udevadm", "settle"))

    def restore(self, manifest: InstallManifest, layout: list[LayoutEntry], receipt: dict) -> None:
        """Reopen and mount saved devices. There is deliberately no format fallback."""
        self.target_path("/")
        self.manifest = manifest
        signatures = receipt.get("signatures")
        uuids = receipt.get("uuids")
        if not isinstance(signatures, dict) or not signatures or not isinstance(uuids, dict):
            raise StorageError("Resume needs saved disk and filesystem identities; storage will not be recreated", "storage.resume")
        layout = self._layout_from_receipt(layout, receipt)
        if sum(e.mountpoint == "/" for e in layout) != 1 or len({e.mountpoint for e in layout if e.mountpoint}) != sum(bool(e.mountpoint) for e in layout):
            raise StorageError("Invalid saved mount layout", "storage.resume")
        disks = self._discovery().disks()
        for device, signature in signatures.items():
            try:
                node = find_disk(disks, device)
            except ValueError as exc:
                raise StorageError(f"Saved disk {device} is not present any more: {exc}", "storage.identity") from exc
            if not self._signature_matches(node, list(signature)):
                raise StorageError(f"Saved disk identity changed: {device}", "storage.identity")
        self._assemble_arrays(receipt.get("md"))
        stack = receipt.get("stack", {})
        if "luks_device" in stack:
            if self._uuid(stack["luks_device"]) != stack.get("luks_uuid"):
                raise StorageError("Saved LUKS identity changed", "storage.resume")
            mapper = stack["mapper"]
            try:
                status = self.runner.run(("cryptsetup", "status", mapper)).stdout
            except RunnerError as exc:
                if exc.returncode != 4:
                    raise
                self._run(("cryptsetup", "open", "--key-file=-", stack["luks_device"], mapper), stdin=manifest.storage.luks_passphrase, secret=True)
                self.owned_maps.append(mapper)
            else:
                backing = next((line.split(":", 1)[1].strip() for line in status.splitlines() if line.strip().startswith("device:")), "")
                if not backing or os.path.realpath(backing) != os.path.realpath(stack["luks_device"]):
                    raise StorageError("Existing mapper is not the saved LUKS device", "storage.resume")
                self.owned_maps.append(mapper)
        if "vg" in stack:
            actual = self.runner.run(("pvs", "--noheadings", "-o", "vg_uuid", stack["pv_device"])).stdout.strip()
            if not actual or actual != stack.get("vg_uuid"):
                raise StorageError("Saved volume group identity changed", "storage.resume")
            self._run(("vgchange", "-ay", stack["vg"]))
            self.owned_vgs.append(stack["vg"])
        # Validate every filesystem before taking ownership of any existing mount.
        disks = self._discovery().disks()
        block_entries = [entry for entry in layout if not entry.source]
        for entry in block_entries:
            if not uuids.get(entry.device) or self._uuid(entry.device) != uuids[entry.device]:
                raise StorageError(f"Saved filesystem identity changed: {entry.device}", "storage.resume")
            node = find_disk(disks, entry.device)
            # A Btrfs filesystem with subvolumes is mounted once per layout entry.
            expected = {str(self.target_path(other.mountpoint)) if other.mountpoint else "[SWAP]" for other in block_entries if other.device == entry.device}
            if any(mount not in expected for mount in node.mountpoints):
                raise StorageError(f"Saved filesystem is mounted elsewhere: {entry.device}", "storage.resume")
        ordered = sorted((e for e in block_entries if e.mountpoint), key=lambda e: Path(e.mountpoint).parts)
        for entry in ordered:
            destination = self.target_path(entry.mountpoint)
            source = self.mount_source(destination)
            if source and os.path.realpath(source.split("[", 1)[0]) != os.path.realpath(entry.device):
                raise StorageError(f"Unrelated filesystem mounted at {destination}: {source}", "storage.resume")
        for entry in ordered:
            destination = self.target_path(entry.mountpoint)
            if not self.mount_source(destination):
                destination.mkdir(parents=True, exist_ok=True)
                self._run(self._mount_command(entry, destination))
            self.owned_mounts.append(destination)
        for entry in layout:
            if entry.filesystem != "swap":
                continue
            if entry.source:
                if self._swap_active(entry.device):
                    self.owned_swaps.append(entry.device)
                elif Path(entry.device).is_file():
                    self._run(("swapon", entry.device))
                    self.owned_swaps.append(entry.device)
                continue
            if "[SWAP]" not in find_disk(disks, entry.device).mountpoints:
                self._run(("swapon", entry.device))
                self.owned_swaps.append(entry.device)
        self.layout = layout
        self._stack = stack
        self._signatures = {device: tuple(value) for device, value in signatures.items()}

    def _layout_from_receipt(self, layout: list[LayoutEntry], receipt: dict) -> list[LayoutEntry]:
        """The saved layout with the fields layout.json does not keep (mount options, swap-file source)."""
        saved = receipt.get("entries")
        if not isinstance(saved, list):
            return layout
        try:
            full = [LayoutEntry(**{key: value for key, value in item.items() if key in LayoutEntry.__dataclass_fields__}) for item in saved]
        except (TypeError, AttributeError):
            return layout
        brief = sorted((e.device, e.mountpoint, e.filesystem) for e in layout)
        return full if brief == sorted((e.device, e.mountpoint, e.filesystem) for e in full) else layout

    def _swap_active(self, path: str) -> bool:
        try:
            output = self.runner.run(("swapon", "--noheadings", "--show=NAME")).stdout
        except (RunnerError, OSError):
            return False
        return os.path.realpath(path) in {os.path.realpath(line.strip()) for line in output.splitlines() if line.strip()}

    def mount_source(self, destination: Path) -> str:
        try:
            return self.runner.run(("findmnt", "--mountpoint", str(destination), "--noheadings", "--output", "SOURCE")).stdout.strip()
        except RunnerError as exc:
            if exc.returncode == 1:
                return ""
            raise

    # ------------------------------------------------------------------ plan description

    def inventory(self) -> list[Disk] | None:
        """The current lsblk inventory, or None when it cannot be read (the plan then has no disk details)."""
        try:
            return self._discovery().disks()
        except (RunnerError, ValueError, OSError):
            return None

    def describe(self, manifest: InstallManifest, disks: Sequence[Disk] | None = None) -> guided_layout.StoragePlan:
        """What this manifest will do to storage (see gentoo_installer.layout.plan_storage)."""
        manifest = self.resolved(manifest)
        self.manifest = manifest
        return guided_layout.plan_storage(manifest, disks, btrfs_subvolumes=self.use_btrfs_subvolumes())

    def _run(self, argv: tuple[str, ...], **kwargs: Any) -> None:
        try:
            self.runner.run(argv, **kwargs)
        except RunnerError as exc:
            hint = cryptsetup_hint(argv, exc)
            if hint:
                raise StorageError(hint, "storage.cryptsetup") from exc
            raise


def _scandir(path: Path) -> list[os.DirEntry[str]]:
    try:
        return sorted(os.scandir(path), key=lambda entry: entry.name)
    except OSError:
        return []
