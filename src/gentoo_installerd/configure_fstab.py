"""STOR: fstab, mount options and the existing LVM/RAID/LUKS stack of a manual layout."""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from gentoo_installer import blockdev
from gentoo_installer.manifest import InstallManifest

from .configure_base import ConfigurerBase
from .inspect import MD_MEMBER_TYPES
from .runner import Runner, RunnerError
from .storage import LayoutEntry


def render_fstab(layout: Sequence[LayoutEntry], uuids: Mapping[str, str], *, btrfs_discard: bool = False) -> str:
    """The target's /etc/fstab.

    An entry is named by ``LayoutEntry.source`` when it has one (a swap file inside the target),
    else by UUID, else by its device path. ``LayoutEntry.options`` (Btrfs ``subvol=...``) is
    appended to the default mount options. ``btrfs_discard`` adds asynchronous TRIM to every
    Btrfs mount (mount options are per filesystem, so all subvolumes get the same).
    """
    lines = ["# <file system> <mount point> <type> <options> <dump> <pass>"]
    for entry in sorted(layout, key=lambda item: (item.mountpoint != "/", item.mountpoint)):
        uuid = uuids.get(entry.device, "")
        source = entry.source or (f"UUID={uuid}" if uuid else entry.device)
        if entry.filesystem == "swap":
            lines.append(f"{source} none swap sw 0 0")
        elif entry.mountpoint:
            # fsck.btrfs is a stub that only prints a notice, so btrfs is never checked at boot.
            pass_number = 0 if entry.filesystem == "btrfs" else 1 if entry.mountpoint == "/" else 2
            options = "defaults,noatime" if entry.filesystem != "vfat" else "defaults,umask=0077"
            extra = [entry.options] if entry.options else []
            if btrfs_discard and entry.filesystem == "btrfs":
                extra.append("discard=async")
            lines.append(f"{source} {entry.mountpoint} {entry.filesystem} {','.join([options, *extra])} 0 {pass_number}")
    return "\n".join(lines) + "\n"


def root_stack_problems(manifest: InstallManifest, runner: Runner) -> list[str]:
    """A manual root on a device type the initramfs cannot assemble, or on a volume or array that cannot be named for it.

    Looked up in the live device tree before anything is formatted; the kernel stage checks again for the layout it built.
    """
    try:
        output = runner.run(blockdev.lsblk_argv()).stdout
        disks = blockdev.parse_lsblk(output[output.index("{"):] if "{" in output else output)
    except (RunnerError, OSError, ValueError, KeyError, TypeError, AttributeError):
        return []  # no tree to judge by: the kernel stage looks again
    layout = [LayoutEntry(row.device, row.mountpoint, row.filesystem) for row in manifest.storage.partitions]
    stack = detect_existing_stack(runner, layout, disks)
    return [stack.unsupported] if stack.unsupported else []


@dataclass(frozen=True)
class ExistingStack:
    """LVM volumes and md arrays under the layout that the installer did not create.

    A manual layout may sit on a volume group or array that was activated before the installation. The manifest
    cannot say so (storage.lvm and storage.raid_level mean "create one"), so Configurer.detect_stack() reads it from
    the device tree. The initramfs needs the tools and the activation arguments of what the root (and the swap to
    resume from) sit on; every other volume is activated by the init system once the system is up.
    """

    lvm: bool = False  # a layout device is on LVM: lvm2 is needed
    md: bool = False  # a layout device is on an md array: mdadm is needed
    thin: bool = False  # a logical volume is thin or cached: lvm2 needs its thin flag
    root_lvm: bool = False
    root_md: str = ""  # RAID level of the array under the root ("1", "5", "10"; "md" when unknown), "" when the root is not on one
    lvs: tuple[str, ...] = ()  # "vg/lv" of the volumes the initramfs has to activate (rd.lvm.lv)
    md_uuids: tuple[str, ...] = ()  # arrays the initramfs has to assemble (rd.md.uuid)
    arrays: tuple[str, ...] = ()  # ARRAY lines of every array under the layout, for /etc/mdadm.conf
    unsupported: str = ""  # why the root cannot be booted: a device type the initramfs cannot assemble, or a name that could not be found out

    @property
    def early_lvm(self) -> bool:
        return self.root_lvm or bool(self.lvs)

    @property
    def early_md(self) -> bool:
        return bool(self.root_md) or bool(self.md_uuids)


# Device-mapper types below a root that the installer cannot make bootable (multipath, linear/mirror maps, ...).
UNSUPPORTED_ROOT_TYPES = frozenset({"dm", "mpath"})


# What goes onto the kernel command line and into mdadm.conf comes from tools that print user-chosen names: only plain text passes.
LVM_NAME_RE = re.compile(r"[A-Za-z0-9+_.-]+/[A-Za-z0-9+_.-]+")


MD_UUID_RE = re.compile(r"[0-9a-fA-F:-]{32,40}")


MDADM_ARRAY_RE = re.compile(r"ARRAY [\x21-\x7e]+( [\x21-\x7e]+)*")


def _is_md(node: blockdev.BlockDevice) -> bool:
    """lsblk calls an md array by its RAID level (raid1, raid10, linear) or plain md."""
    return node.type == "md" or node.type == "linear" or node.type.startswith("raid")


def detect_existing_stack(runner: Runner, layout: Sequence[LayoutEntry], disks: Sequence[blockdev.BlockDevice], resume_device: str | None = None,
                          notify: Callable[[str], None] = lambda message: None) -> ExistingStack:
    """What the layout sits on that the installer did not create: LVM volumes and md arrays (see ExistingStack).

    ``disks`` is the live device tree (lsblk). Only read-only lookups (lvs, mdadm --examine/--detail) are run, and
    only for devices that really sit on a volume or an array.
    """
    everything = blockdev.all_nodes(disks)
    # device -> whether the initramfs needs it (the root, and the swap that hibernation resumes from)
    wanted: dict[str, bool] = {}
    root_device = ""
    for entry in layout:
        if entry.source:
            continue  # a swap file is named by its path inside the root
        wanted[entry.device] = wanted.get(entry.device, False) or entry.mountpoint == "/" or entry.device == resume_device
        root_device = entry.device if entry.mountpoint == "/" else root_device
    lvm = md = thin = root_lvm = False
    root_md = unsupported = ""
    lvs: list[str] = []
    uuids: list[str] = []
    arrays: list[str] = []
    for device, early in wanted.items():
        try:
            node = blockdev.find(disks, device)
        except (LookupError, ValueError):
            continue
        # the device and everything it sits on: partitions, arrays, volumes, disks
        chain = [other for other in everything if any(sub.key == node.key for sub in other.walk())]
        on_arrays = [other for other in chain if _is_md(other)]
        lvm = lvm or any(other.type == "lvm" for other in chain)
        md = md or bool(on_arrays)
        if device == root_device:
            root_lvm = any(other.type == "lvm" for other in chain)
            root_md = on_arrays[-1].type.removeprefix("raid") if on_arrays else ""
            bad = next((other for other in chain if other.type in UNSUPPORTED_ROOT_TYPES), None)
            if bad is not None:
                unsupported = (f"The root filesystem sits on a {bad.type} device ({bad.path}), which the installer cannot make bootable. "
                               "It can boot from a partition, an LVM volume, an md RAID array or the encrypted volume it creates itself.")
        if node.type == "lvm":
            name, segtype = _lvm_volume(runner, node, notify)
            thin = thin or "thin" in segtype or "cache" in segtype
            if early and name:
                lvs.append(name)
            elif device == root_device:
                unsupported = unsupported or f"Could not find out which LVM volume group the root volume {node.path} belongs to, so the initramfs could not activate it."
        for array in on_arrays:
            arrays.extend(_md_scan(runner, array))
            if early:
                uuids.append(_md_uuid(runner, array, everything, notify))
                if not uuids[-1] and device == root_device:
                    unsupported = unsupported or f"Could not read the UUID of the RAID array {array.path} under the root, so the initramfs could not assemble it."
    return ExistingStack(lvm=lvm, md=md, thin=thin, root_lvm=root_lvm, root_md=root_md, lvs=tuple(dict.fromkeys(lvs)),
                         md_uuids=tuple(dict.fromkeys(uuid for uuid in uuids if uuid)), arrays=tuple(dict.fromkeys(arrays)), unsupported=unsupported)


def _lvm_volume(runner: Runner, node: blockdev.BlockDevice, notify: Callable[[str], None]) -> tuple[str, str]:
    """("vg/lv", segment type) of a logical volume; the name is "" when it cannot be found out."""
    try:
        # name=value pairs: the runner mixes stderr into the output, and lvm likes to warn about leaked descriptors
        output = runner.run(("lvs", "--noheadings", "--nameprefixes", "-o", "vg_name,lv_name,segtype", node.path)).stdout
    except (RunnerError, OSError):
        output = ""
    fields = dict(re.findall(r"LVM2_(VG_NAME|LV_NAME|SEGTYPE)='([^']*)'", output))
    name = f"{fields.get('VG_NAME', '')}/{fields.get('LV_NAME', '')}"
    if LVM_NAME_RE.fullmatch(name):
        return name, fields.get("SEGTYPE", "")
    # /dev/mapper/<vg>-<lv>: a dash inside a name is doubled
    parts = re.split(r"(?<!-)-(?!-)", os.path.basename(node.path))
    name = "/".join(part.replace("--", "-") for part in parts)
    if len(parts) == 2 and LVM_NAME_RE.fullmatch(name):
        return name, ""
    notify(f"warning: could not tell which LVM volume {node.path} is, so the initramfs may not activate it")
    return "", ""


def _md_uuid(runner: Runner, array: blockdev.BlockDevice, everything: Sequence[blockdev.BlockDevice], notify: Callable[[str], None]) -> str:
    """The UUID dracut's rd.md.uuid wants: from a member as dracut itself reads it (a firmware-RAID volume has another UUID than its container)."""
    members = [other for other in everything if other.fstype in MD_MEMBER_TYPES and any(sub.key == array.key for sub in other.walk()[1:])]
    for argv in ([("mdadm", "--examine", "--export", members[0].path)] if members else []) + [("mdadm", "--detail", "--export", array.path)]:
        try:
            output = runner.run(argv).stdout
        except (RunnerError, OSError):
            continue
        uuid = next((line.partition("=")[2].strip() for line in output.splitlines() if line.startswith("MD_UUID=")), "")
        if MD_UUID_RE.fullmatch(uuid):
            return uuid
    notify(f"warning: could not read the UUID of the RAID array {array.path}, so the initramfs may not assemble it")
    return ""


def _md_scan(runner: Runner, array: blockdev.BlockDevice) -> list[str]:
    """The ARRAY line(s) mdadm prints for an assembled array, for /etc/mdadm.conf."""
    try:
        output = runner.run(("mdadm", "--detail", "--scan", array.path)).stdout
    except (RunnerError, OSError):
        return []
    return [line.strip() for line in output.splitlines() if MDADM_ARRAY_RE.fullmatch(line.strip())]


class FstabMixin(ConfigurerBase):
    def filesystem_atoms(self, manifest: InstallManifest, layout: Sequence[LayoutEntry] = ()) -> list[str]:
        """Userland for the filesystems in use, so the installed system can check and repair them."""
        filesystems = {entry.filesystem for entry in layout}
        filesystems.add(manifest.storage.filesystem)
        for partition in manifest.storage.partitions:
            filesystems.add(partition.filesystem)
        atoms: list[str] = []
        if "vfat" in filesystems or manifest.storage.firmware == "uefi":
            atoms.append("sys-fs/dosfstools")
        if "xfs" in filesystems:
            atoms.append("sys-fs/xfsprogs")
        if "btrfs" in filesystems:
            atoms.append("sys-fs/btrfs-progs")
        return atoms

    def storage_tool_atoms(self, manifest: InstallManifest, stack: ExistingStack | None = None) -> list[str]:
        """Tools dracut needs on the target to build an initramfs for the storage stack."""
        atoms: list[str] = []
        if manifest.storage.luks:
            atoms.append("sys-fs/cryptsetup")
        if manifest.storage.lvm or (stack is not None and stack.lvm):
            atoms.append("sys-fs/lvm2")
        if manifest.storage.raid_level or (stack is not None and stack.md):
            atoms.append("sys-fs/mdadm")
        return atoms

    # ----- LVM volumes and md arrays that existed before the installation ------------------------------

    def detect_stack(self, layout: Sequence[LayoutEntry], disks: Sequence[blockdev.BlockDevice] | None, resume_device: str | None = None) -> ExistingStack:
        """What the layout sits on that the installer did not create (see detect_existing_stack); a preview, or a missing tree, finds nothing."""
        if self.dry_run or not disks:
            return ExistingStack()
        return detect_existing_stack(self.runner, layout, disks, resume_device, self.notify)

    def prepare_stack_packages(self, stack: ExistingStack) -> None:
        """USE flags for the tools the existing volumes need; before they are emerged (lvm2[-lvm] has no lvm command, and the thin tools need thin)."""
        if stack.lvm:
            self.write("etc/portage/package.use/installer-storage", "sys-fs/lvm2 lvm" + (" thin" if stack.thin else "") + "\n")

    def finish_stack(self, manifest: InstallManifest, stack: ExistingStack) -> None:
        """After lvm2 and mdadm are installed: the array list for the installed system and, on OpenRC, the services that activate the other volumes at boot.

        The initramfs activates the root through its arguments. Home, swap or /boot on other volumes of the same group
        or array are activated by the lvm and mdraid services (systemd does it from udev rules and generators).
        """
        if stack.arrays:
            self.write("etc/mdadm.conf", "# Arrays this system was installed on, written by the Gentoo installer.\n" + "\n".join(stack.arrays) + "\n")
        if manifest.system.init != "openrc":
            return
        for name, needed in (("lvm", stack.lvm), ("mdraid", stack.md)):
            if not needed:
                continue
            if not self.service_installed(name, "openrc"):
                self.notify(f"warning: Service {name} was not enabled because its package is not installed")
                continue
            self.chroot(("rc-update", "add", name, "boot"))
