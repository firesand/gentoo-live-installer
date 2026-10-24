"""Fakes and a stage harness for the Wave 3 target tests (no tests in this module).

``StageMixin`` builds an ``Installer`` on temporary directories whose runner is scripted, so a
stage can be called directly. ``PortageRunner`` is a ScriptedRunner that behaves like the parts of
Portage the target stages depend on: ``emerge --noreplace`` skips a package that is installed, and
a rebuild applies the USE flags the installer wrote to ``/etc/portage/package.use``.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from pathlib import Path

from gentoo_installer.manifest import InstallManifest
from gentoo_installerd.install import Installer
from gentoo_installerd.runner import RunnerError
from gentoo_installerd.storage import LayoutEntry

from tests.backend.helpers import FakeInspector
from tests.backend.target_helpers import ScriptedRunner, target_manifest

UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SWAP_UUID = "55555555-6666-7777-8888-999999999999"
UEFI_LAYOUT = [LayoutEntry("/dev/sda3", "/", "ext4"), LayoutEntry("/dev/sda1", "/efi", "vfat")]


class StageMixin:
    """Mixed into a unittest.TestCase: ``make()`` gives an Installer on temporary directories."""

    base: Path
    target: Path
    runner: ScriptedRunner
    events: list

    def make(self, m: InstallManifest | None = None, runner: ScriptedRunner | None = None, *, layout: list[LayoutEntry] | None = None,
             plan: object | None = None, dry_run: bool = False, runner_factory: Callable[[Path], ScriptedRunner] | None = None,
             **kwargs: object) -> Installer:
        """``runner_factory(target)`` builds the runner for a fake that needs to know where the target tree is."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)  # type: ignore[attr-defined]
        self.base = Path(temporary.name)
        self.runner = runner or (runner_factory(self.base / "target") if runner_factory else ScriptedRunner())
        self.events = []
        installer = Installer(m or target_manifest(), runner=self.runner, target_root=self.base / "target", state_dir=self.base / "state",
                              cache_dir=self.base / "cache", dry_run=dry_run, inspector=FakeInspector(), emit=self.events.append, **kwargs)  # type: ignore[arg-type]
        self.addCleanup(lambda: installer._log_file and installer._log_file.close())  # type: ignore[attr-defined]
        installer.storage.layout = list(layout if layout is not None else UEFI_LAYOUT)
        installer.configurer.sleep = lambda seconds: None
        if plan is not None:
            installer.hardware_plan = plan
        self.target = self.base / "target"
        self.target.mkdir(exist_ok=True)
        return installer

    def chroot_lines(self) -> list[str]:
        prefix = f"chroot {self.target} "
        return [line.removeprefix(prefix) for line in self.runner.lines() if line.startswith(prefix)]


class PortageRunner(ScriptedRunner):
    """ScriptedRunner plus a model of the stage3 packages systemd, systemd-utils and what their USE flags install.

    The stage3 has ``sys-apps/systemd-utils`` (OpenRC) or ``sys-apps/systemd`` (systemd) built with the default
    flags only. ``emerge --noreplace PKG`` does nothing for an installed package ("Nothing to merge"), a rebuild
    (``--update --newuse``) builds it with the flags of ``etc/portage/package.use/*`` and installs the files the
    ebuild installs for them. Running ``bootctl`` or ``ukify`` in the chroot fails with exit 127 when the file is
    missing, as it does in the real target.
    """

    # flag -> files (relative to the target) the ebuilds of systemd and systemd-utils install for it
    FILES = {
        "boot": ["usr/bin/bootctl", "usr/lib/systemd/boot/efi/systemd-bootx64.efi", "usr/lib/systemd/boot/efi/systemd-bootia32.efi",
                 "usr/lib/systemd/boot/efi/linuxx64.efi.stub", "usr/lib/systemd/boot/efi/linuxia32.efi.stub"],
        "ukify": ["usr/bin/ukify"],
        "cryptsetup": ["usr/lib/systemd/systemd-cryptsetup"],
    }

    def __init__(self, target: Path, installed: tuple[str, ...] = ("sys-apps/systemd-utils",), files: dict[str, list[str]] | None = None) -> None:
        super().__init__()
        self.target = target
        self.installed = set(installed)
        self.files = self.FILES if files is None else files
        self.rebuilds: list[str] = []

    def flags_for(self, atom: str) -> set[str]:
        flags: set[str] = set()
        directory = self.target / "etc/portage/package.use"
        for path in sorted(directory.glob("*")) if directory.is_dir() else []:
            for line in path.read_text().splitlines():
                parts = line.split()
                if parts and parts[0] == atom:
                    for flag in parts[1:]:
                        flags.discard(flag.lstrip("-"))
                        if not flag.startswith("-"):
                            flags.add(flag)
        return flags

    def install_files(self, atom: str) -> None:
        for flag in self.flags_for(atom):
            for name in self.files.get(flag, []):
                path = self.target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x")

    def run(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        args = tuple(argv)
        if args[:2] == ("chroot", str(self.target)) and len(args) > 2:
            command = args[2:]
            if command[0] == "emerge":
                self.model_emerge(command[1:])
            elif command[0] in {"bootctl", "ukify"}:
                result = super().run(argv, **kwargs)  # recorded like any other command
                if not (self.target / "usr/bin" / command[0]).is_file():
                    raise RunnerError(args, 127, f"chroot: failed to run command '{command[0]}': No such file or directory")
                images = list((self.target / "usr/lib/systemd/boot/efi").glob("systemd-boot*.efi"))
                if command[0] == "bootctl" and "install" in command and not images:
                    raise RunnerError(args, 1, 'Failed to open "/usr/lib/systemd/boot/efi/systemd-bootx64.efi": No such file or directory')
                return result
        return super().run(argv, **kwargs)

    def model_emerge(self, arguments: tuple[str, ...]) -> None:
        atoms = [a for a in arguments if re.fullmatch(r"[a-z0-9-]+/[A-Za-z0-9_+.-]+", a)]
        rebuild = "--newuse" in arguments or "--changed-use" in arguments
        for atom in atoms:
            if "--pretend" in arguments:
                continue
            if atom in self.installed and not rebuild:
                continue  # "Nothing to merge": --noreplace skips an installed package
            self.installed.add(atom)
            if atom in ("sys-apps/systemd", "sys-apps/systemd-utils"):
                self.rebuilds.append(atom)
                self.install_files(atom)


# --- block device trees and the LVM / mdadm tools that describe them ------------------------------------------------

GIB = 1024**3
MD_UUID = "7b6b4ce2:c0a8a3f6:8f2d9ac8:1fcb0b2e"
ROOT_FS_UUID = "cccccccc-dddd-eeee-ffff-000000000000"


def node(name: str, kind: str, maj_min: str, *, path: str | None = None, fstype: str = "", children: tuple[dict, ...] = (), mountpoints: tuple[str, ...] = (),
         size: int = 100 * GIB) -> dict:
    """One lsblk --json node with the columns the installer asks for."""
    return {"name": name, "kname": name, "path": path or f"/dev/{name}", "maj:min": maj_min, "type": kind, "size": size, "ro": False, "rm": False, "hotplug": False,
            "fstype": fstype or None, "mountpoints": list(mountpoints) or [None], "children": list(children) or None}


def lsblk_json(*disks: dict) -> str:
    import json

    return json.dumps({"blockdevices": list(disks)})


def mapper(name: str, maj_min: str, *, fstype: str = "ext4", mountpoints: tuple[str, ...] = (), kind: str = "lvm", children: tuple[dict, ...] = ()) -> dict:
    return node(name, kind, maj_min, path=f"/dev/mapper/{name}", fstype=fstype, mountpoints=mountpoints, children=children)


def lvm_tree(*, thin: bool = False) -> str:
    """sda: ESP, /boot, and a partition that is the physical volume of vg0 (root, home and swap volumes)."""
    if thin:
        root = mapper("vg0-root", "253:6", mountpoints=("/mnt/gentoo",))
        pool = mapper("vg0-pool-tpool", "253:4", fstype="", children=(root, mapper("vg0-home", "253:7")))
        volumes = (mapper("vg0-pool_tdata", "253:3", fstype="", children=(pool,)),)
    else:
        volumes = (mapper("vg0-root", "253:0", mountpoints=("/mnt/gentoo",)), mapper("vg0-home", "253:1"), mapper("vg0-swap", "253:2", fstype="swap"))
    return lsblk_json(node("sda", "disk", "8:0", children=(
        node("sda1", "part", "8:1", fstype="vfat", mountpoints=("/mnt/gentoo/efi",)),
        node("sda2", "part", "8:2", fstype="ext4", mountpoints=("/mnt/gentoo/boot",)),
        node("sda3", "part", "8:3", fstype="LVM2_member", children=volumes))))


def md_tree(*, lvm_on_top: bool = False, level: str = "raid1") -> str:
    """sda and sdb each have a RAID member partition; together they are /dev/md0 (the root, or the physical volume of vg0)."""
    if lvm_on_top:
        array = node("md0", level, "9:0", fstype="LVM2_member", children=(mapper("vg0-root", "253:0", mountpoints=("/mnt/gentoo",)),))
    else:
        array = node("md0", level, "9:0", fstype="ext4", mountpoints=("/mnt/gentoo",))
    return lsblk_json(
        node("sda", "disk", "8:0", children=(node("sda1", "part", "8:1", fstype="vfat", mountpoints=("/mnt/gentoo/efi",)), node("sda3", "part", "8:3", fstype="linux_raid_member", children=(array,)))),
        node("sdb", "disk", "8:16", children=(node("sdb3", "part", "8:19", fstype="linux_raid_member", children=(array,)),)))


def multipath_tree() -> str:
    return lsblk_json(node("sda", "disk", "8:0", children=(
        node("sda1", "part", "8:1", fstype="vfat", mountpoints=("/mnt/gentoo/efi",)),
        mapper("mpatha", "253:0", fstype="", kind="mpath", children=(mapper("mpatha2", "253:2", kind="part", mountpoints=("/mnt/gentoo",)),)))))


class LayerRunner(ScriptedRunner):
    """ScriptedRunner that answers lvs and mdadm the way the real tools print (padded columns, colon-separated UUIDs)."""

    def __init__(self, volumes: dict[str, tuple[str, str, str]] | None = None, arrays: dict[str, dict] | None = None) -> None:
        super().__init__()
        self.volumes = volumes or {}     # device path -> (volume group, logical volume, segment type)
        self.arrays = arrays or {}       # array path -> {"uuid": ..., "level": "raid1", "members": [...]}

    def run(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        args = tuple(argv)
        result = super().run(argv, **kwargs)
        if args[:1] == ("lvs",):
            vg, lv, segtype = self.volumes.get(args[-1], ("", "", ""))
            if not lv:
                raise RunnerError(args, 5, f'  Failed to find logical volume "{args[-1]}"')
            # lvm warns about descriptors it inherited, on stderr, which the runner mixes into the output
            noise = "File descriptor 3 (pipe:[48151]) leaked on lvs invocation. Parent PID 4242: python3\n"
            assert "--nameprefixes" in args, "without --nameprefixes the columns are padded text that the warning above breaks"
            return type(result)(args, 0, noise + f"  LVM2_VG_NAME='{vg}' LVM2_LV_NAME='{lv}' LVM2_SEGTYPE='{segtype}'\n")
        if args[:2] == ("mdadm", "--examine"):
            member = args[-1]
            for path, array in self.arrays.items():
                if member in array["members"]:
                    return type(result)(args, 0, f"MD_LEVEL={array['level']}\nMD_DEVICES=2\nMD_METADATA=1.2\nMD_UUID={array['uuid']}\nMD_NAME=host:0\nMD_DEVICE_ROLE=0\n")
            raise RunnerError(args, 1, f"mdadm: No md superblock detected on {member}.")
        if args[:2] == ("mdadm", "--detail"):
            array = self.arrays.get(args[-1])
            if array is None:
                raise RunnerError(args, 1, f"mdadm: cannot open {args[-1]}: No such file or directory")
            if "--scan" in args:
                return type(result)(args, 0, f"ARRAY {args[-1]} metadata=1.2 name=host:0 UUID={array['uuid']}\n")
            return type(result)(args, 0, f"MD_LEVEL={array['level']}\nMD_DEVICES=2\nMD_METADATA=1.2\nMD_UUID={array['uuid']}\nMD_DEVNAME=0\nMD_NAME=host:0\n")
        return result
