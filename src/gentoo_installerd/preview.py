"""CORE-06: the files the installation will write, rendered without touching anything.

The review page shows these so a Handbook reader can compare them with their own make.conf and fstab.
Every text comes from the same pure function the daemon writes the file with (``render_make_conf``,
``render_package_use``, ``render_fstab``), so a preview cannot drift from the real file. Before the disk
is prepared nothing has a UUID yet: the fstab preview names each filesystem ``UUID=<placeholder>``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from gentoo_installer import layout as guided_layout
from gentoo_installer.manifest import InstallManifest

from .configure import Configurer, render_fstab
from .configure_portage import render_make_conf, render_package_use
from .runner import DryRunRunner
from .storage import BTRFS_MOUNT_OPTIONS, LayoutEntry

PARTITIONS = "Partition plan (Handbook table)"
FSTAB = "/etc/fstab"
MAKE_CONF = "/etc/portage/make.conf"
PACKAGE_USE = "/etc/portage/package.use/installer"
PACKAGE_USE_HARDWARE = "/etc/portage/package.use/installer-hardware"
BINREPOS = "/etc/portage/binrepos.conf/gentoo.conf"
FSTAB_UUID_PLACEHOLDER = "(assigned when the filesystem is created)"
ROLE_NAMES = {"efi": "EFI system partition", "bios_boot": "BIOS boot partition", "swapfile": "swap file"}


def partition_table(plan: guided_layout.StoragePlan) -> str:
    """The Handbook's partition table (device, mount point, filesystem, size, what happens) for every disk the plan touches."""
    rows = [("Device", "Mount point", "Filesystem", "Size", "Action")]
    for disk in plan.disks:
        rows.append((disk.path, "", (disk.table.upper() + " table") if disk.table else "", "",
                     "erase the disk" if disk.action == "erase" else "use the partitions below"))
        for volume in disk.volumes:
            size = "the rest" if not volume.size_mib else guided_layout.human_size(volume.size_mib * guided_layout.MIB)
            target = volume.device or ROLE_NAMES.get(volume.role, volume.role)
            mount = volume.mountpoint or ("swap" if volume.filesystem == "swap" else "")
            action = {"create": "create", "format": "format", "keep": "keep as it is"}[volume.action]
            rows.append(("  " + target + (" (encrypted)" if volume.encrypted else ""), mount, volume.filesystem, size, action))
    widths = [max(len(row[column]) for row in rows) for column in range(5)]
    return "\n".join("  ".join(cell.ljust(widths[column]) for column, cell in enumerate(row)).rstrip() for row in rows) + "\n"


def layout_entries(manifest: InstallManifest, plan: guided_layout.StoragePlan) -> list[LayoutEntry]:
    """The fstab entries the storage stage would build for this plan (devices are the planned names or a role placeholder)."""
    storage = manifest.storage
    subvolumes = storage.mode == "erase" and storage.filesystem == "btrfs"
    entries: list[LayoutEntry] = []
    has_home = any(volume.mountpoint == "/home" for disk in plan.disks for volume in disk.volumes)
    for disk in plan.disks:
        for volume in disk.volumes:
            device = volume.device or f"<{volume.role} partition>"
            if volume.role == "swapfile":
                entries.append(LayoutEntry(device, "", "swap", source="/swapfile"))
            elif volume.filesystem == "swap":
                entries.append(LayoutEntry(device, "", "swap"))
            elif volume.mountpoint:
                root_on_subvolume = subvolumes and volume.mountpoint == "/"
                options = f"subvol=@,{BTRFS_MOUNT_OPTIONS}" if root_on_subvolume else ""
                entries.append(LayoutEntry(device, volume.mountpoint, volume.filesystem, options=options))
                if root_on_subvolume and not has_home:
                    entries.append(LayoutEntry(device, "/home", volume.filesystem, format=False, options=f"subvol=@home,{BTRFS_MOUNT_OPTIONS}"))
    return entries


def preview_files(manifest: InstallManifest, plan: object | None = None, *, storage_plan: guided_layout.StoragePlan | None = None,
                  layout: Sequence[LayoutEntry] | None = None, uuids: Mapping[str, str] | None = None, make_opts: str | None = None) -> dict[str, str]:
    """{name: text} of the partition table, /etc/fstab, make.conf, package.use and binrepos.conf this installation will write.

    ``plan`` is the hardware plan (``None`` for the generic one), ``storage_plan`` the layout.StoragePlan of the review (without
    it the partition table and fstab are left out). ``layout`` and ``uuids`` replace the placeholder fstab entries, and
    ``make_opts`` the MAKEOPTS the daemon would size for this machine."""
    configurer = Configurer(DryRunRunner(), "/nonexistent-preview-root", dry_run=True)
    jobs = make_opts if make_opts is not None else configurer.effective_make_opts(manifest, plan)
    files: dict[str, str] = {}
    if storage_plan is not None:
        files[PARTITIONS] = partition_table(storage_plan)
    entries = list(layout) if layout is not None else (layout_entries(manifest, storage_plan) if storage_plan is not None else [])
    if entries:
        known = dict(uuids) if uuids is not None else {entry.device: FSTAB_UUID_PLACEHOLDER for entry in entries}
        files[FSTAB] = render_fstab(entries, known)
    files[MAKE_CONF] = render_make_conf(manifest, plan, make_opts=jobs)
    files[PACKAGE_USE] = render_package_use(configurer.package_use_table(manifest, plan))
    hardware_use = getattr(plan, "package_use", None)
    if hardware_use:
        files[PACKAGE_USE_HARDWARE] = render_package_use(hardware_use)
    if manifest.portage.binhost:
        files[BINREPOS] = f"[gentoo]\npriority = 9999\nsync-uri = {manifest.portage.binhost_url}\n"
    return files
