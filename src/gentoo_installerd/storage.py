from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from gentoo_installer.manifest import InstallManifest

from .inspect import Disk, Discovery, find_disk
from .runner import Runner, RunnerError


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class LayoutEntry:
    device: str
    mountpoint: str
    filesystem: str
    format: bool = True


class StorageManager:
    def __init__(self, runner: Runner, target_root: str | Path = "/mnt/gentoo") -> None:
        self.runner = runner
        self.target_root = Path(target_root)
        self.layout: list[LayoutEntry] = []
        self.owned_mounts: list[Path] = []
        self.owned_swaps: list[str] = []
        self.owned_maps: list[str] = []
        self.owned_vgs: list[str] = []
        self._signatures: dict[str, tuple[str, str, int]] = {}
        self._stack: dict[str, str] = {}

    def prepare(self, manifest: InstallManifest, *, dry_run: bool = False) -> list[LayoutEntry]:
        self.target_path("/")
        self.manifest = manifest
        storage = manifest.storage
        if storage.raid_level and storage.mode == "erase":
            raise StorageError("Erase mode cannot create RAID from one disk; select a preassembled /dev/md device in manual mode")
        if storage.separate_home and storage.home_mib <= 0:
            raise StorageError("A separate home partition needs a positive size")
        if storage.raid_level and not storage.device.startswith("/dev/md"):
            raise StorageError("RAID installations require a preassembled /dev/md target")
        if storage.mode == "erase" and storage.table == "dos":
            count = 1 + int(storage.firmware == "uefi") + int(storage.swap_mib > 0) + int(storage.separate_home) + int(storage.luks)
            if count > 4:
                raise StorageError("DOS partition tables support at most four primary partitions; select GPT")
        if storage.mode == "manual" and storage.luks and not any(p.mountpoint == "/boot" for p in storage.partitions):
            raise StorageError("Manual LUKS installation requires a separate unencrypted /boot partition")
        if storage.mode == "manual" and (storage.luks or storage.lvm):
            if any(p.mountpoint == "/" and not p.format for p in storage.partitions):
                raise StorageError("Preserved root cannot create a new LUKS/LVM stack; select an already opened volume with stack creation disabled")
        if dry_run:
            self.layout = self._erase_layout(dry_run=True) if storage.mode == "erase" else self._manual_layout(dry_run=True)
            return self.layout
        if self.mount_source(self.target_root):
            raise StorageError("Target directory is already mounted; use verified resume")
        disks = Discovery(self.runner).disks()
        if storage.mode == "manual" and storage.device_id:
            selected = find_disk(disks, storage.device)
            if storage.device_id not in {selected.serial, selected.wwn}:
                raise StorageError("Target disk identity does not match the approved manifest")
        targets = [storage.device] if storage.mode == "erase" else [p.device for p in storage.partitions]
        nodes = [find_disk(disks, device) for device in targets]
        for device, node in zip(targets, nodes, strict=True):
            self._guard_target(device, node)
            for parent in disks:
                if node in parent.walk() and parent.type == "disk":
                    if not parent.serial and not parent.wwn:
                        raise StorageError("Target disk has no stable serial or WWN identity")
                    self._signatures[parent.path] = (parent.serial, parent.wwn, parent.size)
        if storage.mode == "erase":
            if nodes[0].type not in {"disk", "loop"}:
                raise StorageError("Erase mode requires a whole disk")
            self.layout = self._erase_layout()
        else:
            root_device = next(p.device for p in storage.partitions if p.mountpoint == "/")
            root_node = find_disk(disks, root_device)
            if not storage.luks and any(parent.type == "crypt" and root_node in parent.walk() for disk in disks for parent in disk.walk()):
                raise StorageError("Preserving a pre-opened encrypted root needs explicit boot configuration and is not supported")
            if storage.luks:
                boot_node = find_disk(disks, next(p.device for p in storage.partitions if p.mountpoint == "/boot"))
                if any(parent.type == "crypt" and boot_node in parent.walk() for disk in disks for parent in disk.walk()):
                    raise StorageError("The separate /boot partition must be unencrypted")
            for node in nodes:
                if node.type not in {"part", "crypt", "lvm", "raid0", "raid1", "raid5", "raid6", "raid10"}:
                    raise StorageError(f"Manual target is not a usable partition or mapped volume: {node.path}")
            self.layout = self._manual_layout()
        self.revalidate()
        for entry in self.layout:
            if not entry.mountpoint:
                continue
            node = find_disk(Discovery(self.runner).disks(), entry.device)
            if node.mountpoints:
                raise StorageError(f"Refusing to mount an already mounted target: {entry.device}")
        self._mount_layout(dry_run=False)
        return self.layout

    def _guard_target(self, requested: str, node: Disk) -> None:
        if any(child.mountpoints for child in node.walk()):
            mounted = sorted({mount for child in node.walk() for mount in child.mountpoints})
            raise StorageError(f"Target is mounted or contains a live filesystem: {requested} ({', '.join(mounted)})")
        identity = self.manifest.storage.device_id if self.manifest.storage.mode == "erase" else ""
        if identity and identity not in {node.serial, node.wwn, node.path, requested}:
            raise StorageError("Target disk identity does not match the approved manifest")
        if not node.serial and not node.wwn and node.type == "disk":
            raise StorageError("Target disk has no stable serial or WWN identity")
        self._signatures[node.path] = (node.serial, node.wwn, node.size)

    def _partition_device(self, disk: str, number: int) -> str:
        disk = os.path.realpath(disk)
        return f"{disk}p{number}" if disk[-1:].isdigit() else f"{disk}{number}"

    def _erase_layout(self, *, dry_run: bool = False) -> list[LayoutEntry]:
        s = self.manifest.storage
        disk = s.device
        entries: list[LayoutEntry] = []
        number = 1
        self._parted_start = 1
        self._run(("sgdisk", "--zap-all", disk)) if s.table == "gpt" else self._run(("parted", "--script", disk, "mklabel", "msdos"))
        if s.firmware == "uefi":
            efi = self._partition_device(disk, number)
            self._new_partition(number, "+1024MiB", "EF00", "EFI")
            entries.append(LayoutEntry(efi, "/efi", "vfat"))
            number += 1
        elif s.table == "gpt":
            self._new_partition(number, "+3MiB", "EF02", "BIOS")
            number += 1
        if s.luks:
            boot = self._partition_device(disk, number)
            self._new_partition(number, "+1024MiB", "8300", "boot")
            entries.append(LayoutEntry(boot, "/boot", "ext4"))
            number += 1
        swap_device = ""
        home_device = ""
        if s.swap_mib:
            swap_device = self._partition_device(disk, number)
            self._new_partition(number, f"+{s.swap_mib}MiB", "8200", "swap")
            number += 1
        if s.separate_home:
            home_device = self._partition_device(disk, number)
            self._new_partition(number, f"+{s.home_mib}MiB", "8300", "home")
            number += 1
        root_device = self._partition_device(disk, number)
        self._new_partition(number, "0", "8300", "root")
        self._run(("partprobe", disk))
        self._run(("udevadm", "settle"))
        root_device = self._prepare_stack(root_device, "root")
        entries.append(LayoutEntry(root_device, "/", s.filesystem))
        if swap_device:
            entries.append(LayoutEntry(swap_device, "", "swap"))
        if home_device:
            entries.append(LayoutEntry(home_device, "/home", s.filesystem))
        self._format(entries)
        return entries

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
        entries = [LayoutEntry(p.device, p.mountpoint, p.filesystem, p.format) for p in self.manifest.storage.partitions]
        roots = [index for index, entry in enumerate(entries) if entry.mountpoint == "/"]
        if len(roots) != 1:
            raise StorageError("Exactly one root mountpoint is required")
        devices = [os.path.realpath(entry.device) for entry in entries]
        if len(set(devices)) != len(devices):
            raise StorageError("Manual layout contains duplicate target devices")
        mountpoints = [os.path.normpath(entry.mountpoint) for entry in entries if entry.mountpoint]
        if len(set(mountpoints)) != len(mountpoints):
            raise StorageError("Manual layout contains duplicate mountpoints")
        index = roots[0]
        root = entries[index]
        if self.manifest.storage.luks or self.manifest.storage.lvm:
            mapped = self._prepare_stack(root.device, "root")
            entries[index] = LayoutEntry(mapped, "/", root.filesystem, root.format)
        self._format(entries)
        return entries

    def _prepare_stack(self, device: str, name: str) -> str:
        s = self.manifest.storage
        current = device
        if s.luks:
            mapper = f"gentoo-{name}"
            if Path(f"/dev/mapper/{mapper}").exists():
                raise StorageError(f"Mapper already exists: {mapper}")
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
        for entry in entries:
            if not entry.format:
                continue
            commands = {
                "ext4": ("mkfs.ext4", "-F", entry.device),
                "xfs": ("mkfs.xfs", "-f", entry.device),
                "btrfs": ("mkfs.btrfs", "-f", entry.device),
                "vfat": ("mkfs.vfat", "-F", "32", entry.device),
                "swap": ("mkswap", entry.device),
                "none": None,
            }
            command = commands.get(entry.filesystem)
            if entry.filesystem not in commands:
                raise StorageError(f"Unsupported filesystem: {entry.filesystem}")
            if command:
                self._run(command)

    def _mount_layout(self, *, dry_run: bool) -> None:
        roots = [entry for entry in self.layout if entry.mountpoint == "/"]
        ordered = roots + sorted((entry for entry in self.layout if entry.mountpoint not in {"", "/"}), key=lambda entry: Path(entry.mountpoint).parts)
        for entry in ordered:
            destination = self.target_path(entry.mountpoint)
            if not dry_run:
                destination.mkdir(parents=True, exist_ok=True)
            self._run(("mount", entry.device, str(destination)))
            if not dry_run:
                self.owned_mounts.append(destination)
        for entry in self.layout:
            if entry.filesystem == "swap":
                self._run(("swapon", entry.device))
                if not dry_run:
                    self.owned_swaps.append(entry.device)

    def target_path(self, mountpoint: str) -> Path:
        if not self.target_root.is_absolute() or self.target_root.resolve() == Path("/"):
            raise StorageError("Target root must be an absolute, non-root directory")
        if not mountpoint.startswith("/") or "\x00" in mountpoint or ".." in Path(mountpoint).parts:
            raise StorageError("Invalid mountpoint")
        candidate = self.target_root / mountpoint.lstrip("/")
        if any(path.is_symlink() for path in (candidate, *candidate.parents)):
            raise StorageError("Mountpoint or target root contains a symlink")
        root = os.path.realpath(self.target_root)
        resolved = os.path.realpath(candidate)
        if os.path.commonpath((root, resolved)) != root:
            raise StorageError("Mountpoint escapes target root")
        return Path(resolved)

    def revalidate(self) -> None:
        if not self._signatures:
            return
        disks = Discovery(self.runner).disks()
        for device, signature in self._signatures.items():
            node = find_disk(disks, device)
            if (node.serial, node.wwn, node.size) != signature:
                raise StorageError(f"Disk identity changed during installation: {device}")
            if self.manifest.storage.mode == "erase" and any(child.mountpoints and child.path != device for child in node.walk()):
                raise StorageError(f"Unexpected mounted filesystem on target: {device}")

    def cleanup(self) -> None:
        # A job cancellation must not cancel release of resources it acquired.
        run = getattr(self.runner, "run_cleanup", self.runner.run)
        errors: list[str] = []
        for resources, command in (
            (self.owned_mounts, lambda value: ("umount", "-R", str(value))),
            (self.owned_swaps, lambda value: ("swapoff", str(value))),
            (self.owned_vgs, lambda value: ("vgchange", "-an", str(value))),
            (self.owned_maps, lambda value: ("cryptsetup", "close", str(value))),
        ):
            for resource in list(reversed(resources)):
                try:
                    run(command(resource))
                except Exception as exc:
                    errors.append(str(exc))
                else:
                    resources.remove(resource)
        if errors:
            raise StorageError("Cleanup failed: " + "; ".join(errors))

    def _uuid(self, device: str) -> str:
        value = self.runner.run(("blkid", "-s", "UUID", "-o", "value", device)).stdout.strip()
        if not value or len(value.splitlines()) != 1:
            raise StorageError(f"Cannot establish filesystem identity: {device}")
        return value

    def snapshot(self) -> dict:
        """Identity receipts needed to resume without ever recreating storage."""
        stack = dict(self._stack)
        if "luks_device" in stack:
            stack["luks_uuid"] = self._uuid(stack["luks_device"])
        if "vg" in stack:
            stack["vg_uuid"] = self.runner.run(("pvs", "--noheadings", "-o", "vg_uuid", stack["pv_device"])).stdout.strip()
            if not stack["vg_uuid"]:
                raise StorageError("Cannot establish volume group identity")
        return {"signatures": self._signatures, "uuids": {e.device: self._uuid(e.device) for e in self.layout}, "stack": stack}

    def restore(self, manifest: InstallManifest, layout: list[LayoutEntry], receipt: dict) -> None:
        """Reopen and mount saved devices. There is deliberately no format fallback."""
        self.target_path("/")
        self.manifest = manifest
        signatures = receipt.get("signatures")
        uuids = receipt.get("uuids")
        if not isinstance(signatures, dict) or not signatures or not isinstance(uuids, dict):
            raise StorageError("Resume needs saved disk and filesystem identities; storage will not be recreated")
        if sum(e.mountpoint == "/" for e in layout) != 1 or len({e.mountpoint for e in layout if e.mountpoint}) != sum(bool(e.mountpoint) for e in layout):
            raise StorageError("Invalid saved mount layout")
        disks = Discovery(self.runner).disks()
        for device, signature in signatures.items():
            node = find_disk(disks, device)
            if [node.serial, node.wwn, node.size] != list(signature):
                raise StorageError(f"Saved disk identity changed: {device}")
        stack = receipt.get("stack", {})
        if "luks_device" in stack:
            if self._uuid(stack["luks_device"]) != stack.get("luks_uuid"):
                raise StorageError("Saved LUKS identity changed")
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
                    raise StorageError("Existing mapper is not the saved LUKS device")
                self.owned_maps.append(mapper)
        if "vg" in stack:
            actual = self.runner.run(("pvs", "--noheadings", "-o", "vg_uuid", stack["pv_device"])).stdout.strip()
            if not actual or actual != stack.get("vg_uuid"):
                raise StorageError("Saved volume group identity changed")
            self._run(("vgchange", "-ay", stack["vg"]))
            self.owned_vgs.append(stack["vg"])
        # Validate every filesystem before taking ownership of any existing mount.
        disks = Discovery(self.runner).disks()
        for entry in layout:
            if not uuids.get(entry.device) or self._uuid(entry.device) != uuids[entry.device]:
                raise StorageError(f"Saved filesystem identity changed: {entry.device}")
            node = find_disk(disks, entry.device)
            destination = str(self.target_path(entry.mountpoint)) if entry.mountpoint else "[SWAP]"
            if any(mount != destination for mount in node.mountpoints):
                raise StorageError(f"Saved filesystem is mounted elsewhere: {entry.device}")
        ordered = sorted((e for e in layout if e.mountpoint), key=lambda e: Path(e.mountpoint).parts)
        for entry in ordered:
            destination = self.target_path(entry.mountpoint)
            source = self.mount_source(destination)
            if source and os.path.realpath(source) != os.path.realpath(entry.device):
                raise StorageError(f"Unrelated filesystem mounted at {destination}: {source}")
        for entry in ordered:
            destination = self.target_path(entry.mountpoint)
            source = self.mount_source(destination)
            if source and os.path.realpath(source) != os.path.realpath(entry.device):
                raise StorageError(f"Unrelated filesystem mounted at {destination}: {source}")
            if not source:
                destination.mkdir(parents=True, exist_ok=True)
                self._run(("mount", entry.device, str(destination)))
            self.owned_mounts.append(destination)
        for entry in layout:
            if entry.filesystem == "swap":
                node = find_disk(disks, entry.device)
                if "[SWAP]" not in node.mountpoints:
                    self._run(("swapon", entry.device))
                    self.owned_swaps.append(entry.device)
        self.layout = layout
        self._stack = stack
        self._signatures = {device: tuple(value) for device, value in signatures.items()}

    def mount_source(self, destination: Path) -> str:
        try:
            return self.runner.run(("findmnt", "--mountpoint", str(destination), "--noheadings", "--output", "SOURCE")).stdout.strip()
        except RunnerError as exc:
            if exc.returncode == 1:
                return ""
            raise

    def _run(self, argv: tuple[str, ...], **kwargs: object) -> None:
        self.runner.run(argv, **kwargs)

