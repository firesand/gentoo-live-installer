"""What the rescue engine may run (docs/contracts.md: rescue never formats or repartitions).

Two policies guard every command before a process exists:

* the *host* policy (``HostPolicy``) validates programs run on the live system: only the
  read-only inspectors and the narrow mutating forms rescue needs, with every ``/dev``
  argument checked against the device allow-list and every mount destination checked
  against the session root;
* the *chroot* policy (``ChrootPolicy``) validates the command a plan step runs inside
  the target: a per-action tool set minus a universal deny set.

This is the only module that spells the names of destructive tools, on purpose: a lint
test keeps them out of every other file. The policy is the safety net; the user-facing
promise is enforced by what the planners emit, plus BLKROSET on devices the session does
not own (see ``session.py``).
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .errors import RescueError, SafetyViolation

SESSION_ID_RE = re.compile(r"^r[0-9a-f]{6}$")
MAPPER_NAME_RE = re.compile(r"^rescue-[0-9a-f]{12}$")
VG_NAME_RE = re.compile(r"^[A-Za-z0-9+_.-]+$")
BOOTNUM_RE = re.compile(r"^[0-9A-Fa-f]{4}$")
EFI_LABEL_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")
SAFE_SUBVOL_RE = re.compile(r"^/?[A-Za-z0-9._@+:-]+(?:/[A-Za-z0-9._@+:-]+)*$")
DEV_RE = re.compile(r"^/dev/[A-Za-z0-9._:+@/-]+$")

# The one shell form the runner builds itself: umask 022, then chroot with gid 0 and no supplementary groups.
CHROOT_WRAPPER = "umask 022 && exec chroot \"$@\""
CHROOT_WRAPPER_ARGV = ("sh", "-c", CHROOT_WRAPPER, "rescue-chroot")
CHROOT_CREDENTIALS = ("--userspec=0:0", "--groups=")
CHROOT_PATH = "/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin"

DENIED_HOST = frozenset({
    "mkswap", "wipefs", "sgdisk", "sfdisk", "fdisk", "gdisk", "cfdisk", "parted", "partprobe", "dd", "shred",
    "blkdiscard", "hdparm", "pvcreate", "pvremove", "pvresize", "pvmove", "vgcreate", "vgremove", "vgrename",
    "vgextend", "vgreduce", "vgmerge", "vgsplit", "lvcreate", "lvremove", "lvresize", "lvextend", "lvreduce",
    "lvchange", "lvrename", "resize2fs", "xfs_growfs", "e2label", "losetup", "swapon", "swapoff", "mkfs",
    "rm", "cp", "mv", "cat", "tee", "bash", "zsh", "dash", "python", "python3", "perl", "ruby",
    "reboot", "halt", "poweroff", "shutdown", "telinit", "kill", "pkill", "killall",
})
DENIED_HOST_PREFIXES = ("mkfs.", "python3.", "mke2fs", "mkntfs", "mkdosfs")

# Programs that may run inside a target for any plan (the action's own tool set narrows this further).
DENIED_CHROOT = frozenset(DENIED_HOST | {"mount", "umount", "init", "systemctl-isolate", "mdadm-create"})
DENIED_CHROOT_PREFIXES = DENIED_HOST_PREFIXES
CHROOT_DENIED_SUBCOMMANDS = {
    "cryptsetup": {"luksFormat", "luksAddKey", "luksRemoveKey", "luksKillSlot", "luksErase", "reencrypt", "luksHeaderRestore", "erase"},
    "mdadm": {"--create", "-C", "--zero-superblock", "--grow", "-G", "--fail", "--remove", "--add"},
    "systemctl": {"isolate", "reboot", "poweroff", "halt", "kexec", "suspend", "hibernate"},
}

MOUNT_FS = frozenset({"ext2", "ext4", "xfs", "btrfs", "f2fs", "vfat", "exfat", "jfs", "ntfs3", "hfsplus"})
VIRTUAL_FS = frozenset({"proc", "tmpfs", "efivarfs", "pstore"})
PLAIN_MOUNT_OPTS = frozenset({
    "ro", "rw", "nodev", "nosuid", "noexec", "noload", "norecovery", "nouuid", "usebackuproot", "degraded", "utf8",
    "relatime", "noatime", "nosymfollow",
})
VALUE_MOUNT_OPTS: dict[str, re.Pattern[str]] = {
    "subvolid": re.compile(r"^\d+$"),
    "subvol": SAFE_SUBVOL_RE,
    "rescue": re.compile(r"^(?:nologreplay|usebackuproot|ignorebadroots|ibadroots|ignoredatacsums|idatacsums|all)(?::(?:nologreplay|usebackuproot|ignorebadroots|ibadroots|ignoredatacsums|idatacsums))*$"),
    "uid": re.compile(r"^\d+$"), "gid": re.compile(r"^\d+$"),
    "umask": re.compile(r"^[0-7]{1,4}$"), "fmask": re.compile(r"^[0-7]{1,4}$"), "dmask": re.compile(r"^[0-7]{1,4}$"),
    "iocharset": re.compile(r"^[A-Za-z0-9_-]+$"), "codepage": re.compile(r"^\d+$"),
    "shortname": re.compile(r"^(?:mixed|lower|win95|winnt)$"), "mode": re.compile(r"^0?[0-7]{3,4}$"),
    "size": re.compile(r"^\d+[kKmMgG%]?$"), "nr_inodes": re.compile(r"^\d+$"),
    "device": DEV_RE, "logdev": DEV_RE, "rtdev": DEV_RE, "journal_path": DEV_RE, "journal_dev": re.compile(r"^(?:0x)?[0-9a-fA-F]+$"),
}
BIND_OPTION_SETS = ({"remount", "bind", "ro"}, {"remount", "bind", "ro", "nosuid", "nodev", "noexec"})


def program_name(argv0: str) -> str:
    """Basename of a program, accepting only bare names or the standard system directories."""
    if "/" in argv0:
        directory, name = argv0.rsplit("/", 1)
        if directory not in ("/usr/bin", "/usr/sbin", "/bin", "/sbin"):
            raise SafetyViolation(f"Program path {argv0} is not a system directory", argv=[argv0])
        return name
    return argv0


def is_denied(name: str, *, chroot: bool = False) -> bool:
    deny = DENIED_CHROOT if chroot else DENIED_HOST
    prefixes = DENIED_CHROOT_PREFIXES if chroot else DENIED_HOST_PREFIXES
    return name in deny or name.startswith(prefixes)


@dataclass
class DeviceAllowList:
    """Devices a session may open for writing, plus the read-only reference sets.

    ``devices`` are the stack the user selected (partition, crypt mapping, LV, ...) and, after
    the mount plan was confirmed, the fstab-resolved ones. ``disks`` are the parent whole
    disks (needed by grub-install and efibootmgr, never used for mounting). ``protected``
    are devices the session flags read-only on release-and-restore terms (review R4).
    """

    devices: set[str] = field(default_factory=set)
    disks: set[str] = field(default_factory=set)
    protected: set[str] = field(default_factory=set)
    live: set[str] = field(default_factory=set)

    @staticmethod
    def normalize(path: str) -> str:
        return os.path.realpath(path)  # host-path: a /dev node chosen by the caller, not target text

    def add(self, *paths: str) -> None:
        for path in paths:
            self.devices.add(path)
            self.devices.add(self.normalize(path))

    def add_disks(self, *paths: str) -> None:
        for path in paths:
            self.disks.add(path)
            self.disks.add(self.normalize(path))

    def protect(self, *paths: str) -> None:
        for path in paths:
            self.protected.add(path)
            self.protected.add(self.normalize(path))

    def mark_live(self, *paths: str) -> None:
        for path in paths:
            self.live.add(path)
            self.live.add(self.normalize(path))

    def is_live(self, path: str) -> bool:
        return path in self.live or self.normalize(path) in self.live

    def allows(self, path: str) -> bool:
        return not self.is_live(path) and (path in self.devices or self.normalize(path) in self.devices)

    def allows_disk(self, path: str) -> bool:
        return not self.is_live(path) and (path in self.disks or self.normalize(path) in self.disks)

    def may_protect(self, path: str) -> bool:
        return not self.is_live(path) and (path in self.protected or self.normalize(path) in self.protected or self.allows(path))


@dataclass
class PolicyContext:
    """Everything the host policy needs to know about the current session."""

    session_root: str = ""  # /mnt/rescue/<sid>; empty before a session exists (scan, unlock)
    session_id: str = ""
    session_base: str = "/mnt/rescue"
    probe_root: str = "/run/gentoo-rescue/probe"
    state_dir: str = "/run/gentoo-rescue"
    devices: DeviceAllowList = field(default_factory=DeviceAllowList)
    bind_sources: set[str] = field(default_factory=set)  # extra bind sources (resolv.conf, synthesized cmdline)
    owned_vgs: set[str] = field(default_factory=set)
    owned_md: set[str] = field(default_factory=set)
    owned_maps: set[str] = field(default_factory=set)
    probing: bool = False  # scan: read-only probe mounts of any non-live device are allowed
    allow_write: bool = True

    def under(self, path: str, base: str) -> bool:
        return bool(base) and (path == base or path.startswith(base.rstrip("/") + "/"))

    @property
    def mdadm_conf(self) -> str:
        return f"{self.state_dir}/sessions/{self.session_id or 'pool'}.mdadm.conf"


def _fail(message: str, argv: Sequence[str]) -> SafetyViolation:
    return SafetyViolation(message, argv=list(argv))


def _flags_only(args: Sequence[str], allowed: set[str], argv: Sequence[str]) -> None:
    for arg in args:
        if arg not in allowed:
            raise _fail(f"Option {arg} is not allowed here", argv)


class HostPolicy:
    """Validates one host command (a tuple of strings) against the rules in the module docstring."""

    def __init__(self, context: PolicyContext) -> None:
        self.ctx = context

    # ---- entry point -------------------------------------------------------
    def validate(self, argv: Sequence[str]) -> str:
        """Return the command class ('inspect', 'mount', 'device', ...) or raise SafetyViolation."""
        if not argv:
            raise _fail("Empty command", argv)
        name = program_name(argv[0])
        args = tuple(argv[1:])
        if name == "timeout":
            return self._timeout(argv)
        if name == "sh":
            return self._chroot_wrapper(argv)
        if is_denied(name):
            raise _fail(f"{name} is never run by rescue", argv)
        checker = getattr(self, "_" + re.sub(r"[^a-z0-9]", "_", name.lower()), None)
        if checker is None:
            raise _fail(f"{name} is not on the rescue allow list", argv)
        kind = checker(args, argv)
        return str(kind or "inspect")

    # ---- generic helpers -----------------------------------------------------
    def _inspect_device(self, path: str, argv: Sequence[str]) -> None:
        """A device that is only read: any block device except the live medium."""
        if not DEV_RE.match(path) or ".." in path.split("/"):
            raise _fail(f"{path} is not a device path", argv)
        if self.ctx.devices.is_live(path):
            raise RescueError("live_medium", f"{path} is the live medium")

    def _mutate_device(self, path: str, argv: Sequence[str]) -> None:
        if not DEV_RE.match(path) or ".." in path.split("/"):
            raise _fail(f"{path} is not a device path", argv)
        if self.ctx.devices.is_live(path):
            raise RescueError("live_medium", f"{path} is the live medium")
        if not self.ctx.devices.allows(path):
            raise _fail(f"{path} was not selected for this session", argv)

    def _dir_in_session(self, path: str, argv: Sequence[str], *, allow_root: bool = True) -> None:
        root = self.ctx.session_root
        if not root:
            raise _fail("No session root is active", argv)
        if ".." in path.split("/") or not (path == root and allow_root or path.startswith(root + "/")):
            raise _fail(f"{path} is outside the session root", argv)

    def _key_file_in_session(self, path: str, argv: Sequence[str]) -> None:
        """A key file is a file of a mounted system. Unlocking a data disk with one runs before a session of its own exists (no session root yet): then any file below
        a session directory of the session base will do, and nothing else."""
        if self.ctx.session_root:
            self._dir_in_session(path, argv, allow_root=False)
            return
        base = self.ctx.session_base.rstrip("/")
        parts = path[len(base) + 1:].split("/") if base and path.startswith(base + "/") else []
        if len(parts) < 2 or not SESSION_ID_RE.match(parts[0]) or any(part in {"", ".", ".."} for part in parts):
            raise _fail(f"{path} is not a file of a repair session", argv)

    def _dir_in_probe(self, path: str) -> bool:
        return self.ctx.under(path, self.ctx.probe_root) and ".." not in path.split("/")

    def _path_in_scope(self, path: str, argv: Sequence[str]) -> None:
        if self._dir_in_probe(path):
            return
        self._dir_in_session(path, argv)

    # ---- timeout / chroot wrapper -----------------------------------------------
    def _timeout(self, argv: Sequence[str]) -> str:
        rest = list(argv[1:])
        while rest and rest[0].startswith("--kill-after="):
            rest.pop(0)
        if len(rest) < 2 or not re.fullmatch(r"\d{1,5}", rest[0]):
            raise _fail("timeout needs a number of seconds and a command", argv)
        return self.validate(rest[1:])

    def _chroot_wrapper(self, argv: Sequence[str]) -> str:
        if tuple(argv[:4]) != CHROOT_WRAPPER_ARGV:
            raise _fail("sh is only allowed as the fixed chroot wrapper", argv)
        rest = list(argv[4:])
        if tuple(rest[:2]) != CHROOT_CREDENTIALS or len(rest) < 4:
            raise _fail("chroot must drop to gid 0 with no supplementary groups", argv)
        root = rest[2]
        if not self.ctx.session_root or root != self.ctx.session_root:
            raise _fail("chroot target is not the session root", argv)
        if tuple(rest[3:5]) != ("/usr/bin/env", "-i"):
            raise _fail("chroot commands must run in a clean environment", argv)
        return "chroot"

    # ---- read-only inspectors -----------------------------------------------------
    def _lsblk(self, args: Sequence[str], argv: Sequence[str]) -> str:
        for arg in args:
            if arg.startswith("/dev/"):
                self._inspect_device(arg, argv)
        return "inspect"

    def _blkid(self, args: Sequence[str], argv: Sequence[str]) -> str:
        for arg in args:
            if arg.startswith("/dev/"):
                self._inspect_device(arg, argv)
            elif arg in ("-g", "-w", "--garbage-collect") or arg.startswith("--write-cache"):
                raise _fail("blkid may only read", argv)
        return "inspect"

    def _findmnt(self, args: Sequence[str], argv: Sequence[str]) -> str:
        return "inspect"

    def _sync(self, args: Sequence[str], argv: Sequence[str]) -> str:
        return "inspect"

    def _fuser(self, args: Sequence[str], argv: Sequence[str]) -> str:
        flags = [a for a in args if a.startswith("-")]
        if not flags or any(set(flag[1:]) - set("vmM") for flag in flags):
            raise _fail("fuser may only list holders (-v -m -M)", argv)
        for arg in args:
            if not arg.startswith("-"):
                self._path_in_scope(arg, argv)
        return "inspect"

    def _udevadm(self, args: Sequence[str], argv: Sequence[str]) -> str:
        if not args or args[0] not in ("settle", "info"):
            raise _fail("udevadm may only settle or query", argv)
        return "inspect"

    def _dmsetup(self, args: Sequence[str], argv: Sequence[str]) -> str:
        if not args or args[0] not in ("ls", "info", "status", "table"):
            raise _fail("dmsetup may only list devices", argv)
        return "inspect"

    def _smartctl(self, args: Sequence[str], argv: Sequence[str]) -> str:
        allowed = {"-j", "-H", "-A", "-i", "-a", "-x", "--json", "--json=c", "-d"}
        skip = False
        for arg in args:
            if skip:
                skip = False
                continue
            if arg == "-d":
                skip = True
            elif arg.startswith("/dev/"):
                self._inspect_device(arg, argv)
            elif arg not in allowed and not arg.startswith("--json"):
                raise _fail(f"smartctl option {arg} is not allowed", argv)
        return "inspect"

    def _nvme(self, args: Sequence[str], argv: Sequence[str]) -> str:
        if not args or args[0] not in ("smart-log", "id-ctrl", "list"):
            raise _fail("nvme may only read health data", argv)
        for arg in args[1:]:
            if arg.startswith("/dev/"):
                self._inspect_device(arg, argv)
        return "inspect"

    def _dmesg(self, args: Sequence[str], argv: Sequence[str]) -> str:
        return "inspect"

    def _modprobe(self, args: Sequence[str], argv: Sequence[str]) -> str:
        if not args or args[0] not in ("-n", "--dry-run"):
            raise _fail("modprobe is only used with -n", argv)
        return "inspect"

    def _lsinitrd(self, args: Sequence[str], argv: Sequence[str]) -> str:
        for arg in args:
            if not arg.startswith("-"):
                self._dir_in_session(arg, argv, allow_root=False)
        return "inspect"

    def _mokutil(self, args: Sequence[str], argv: Sequence[str]) -> str:
        if args != ("--sb-state",):
            raise _fail("mokutil may only show the Secure Boot state", argv)
        return "inspect"

    def _sbverify(self, args: Sequence[str], argv: Sequence[str]) -> str:
        if not args or args[0] != "--list":
            raise _fail("sbverify may only list signatures", argv)
        for arg in args[1:]:
            self._dir_in_session(arg, argv, allow_root=False)
        return "inspect"

    def _zstd(self, args: Sequence[str], argv: Sequence[str]) -> str:
        if len(args) != 2 or args[0] != "-dc":
            raise _fail("zstd may only decompress to stdout", argv)
        self._dir_in_session(args[1], argv, allow_root=False)
        return "inspect"

    def _dumpe2fs(self, args: Sequence[str], argv: Sequence[str]) -> str:
        if "-h" not in args:
            raise _fail("dumpe2fs is only used with -h", argv)
        for arg in args:
            if arg.startswith("/dev/"):
                self._inspect_device(arg, argv)
        return "inspect"

    def _tune2fs(self, args: Sequence[str], argv: Sequence[str]) -> str:
        if not args or args[0] != "-l":
            raise _fail("tune2fs is only used with -l", argv)
        for arg in args[1:]:
            self._inspect_device(arg, argv)
        return "inspect"

    def _xfs_db(self, args: Sequence[str], argv: Sequence[str]) -> str:
        if "-r" not in args:
            raise _fail("xfs_db must be read-only", argv)
        return "inspect"

    def _ntfsfix(self, args: Sequence[str], argv: Sequence[str]) -> str:
        if "-n" not in args and "--no-action" not in args:
            raise _fail("ntfsfix must not change anything", argv)
        for arg in args:
            if arg.startswith("/dev/"):
                self._inspect_device(arg, argv)
        return "inspect"

    def _pvs(self, args: Sequence[str], argv: Sequence[str]) -> str:
        return "inspect"

    _vgs = _pvs
    _lvs = _pvs

    # ---- block device flags (review R4, R7) ------------------------------------------------
    def _blockdev(self, args: Sequence[str], argv: Sequence[str]) -> str:
        if len(args) != 2 or args[0] not in ("--getro", "--setro", "--setrw"):
            raise _fail("blockdev may only read or set the read-only flag", argv)
        if args[0] == "--getro":
            self._inspect_device(args[1], argv)
            return "inspect"
        if self.ctx.probing and not self.ctx.devices.is_live(args[1]) and DEV_RE.match(args[1]):
            return "device"
        if not DEV_RE.match(args[1]) or ".." in args[1].split("/"):
            raise _fail(f"{args[1]} is not a device path", argv)
        if self.ctx.devices.is_live(args[1]):
            raise RescueError("live_medium", f"{args[1]} is the live medium")
        if not self.ctx.devices.may_protect(args[1]):
            raise _fail(f"{args[1]} is neither selected nor protected by this session", argv)
        return "device"

    # ---- mount / umount ---------------------------------------------------------------------
    def _mount(self, args: Sequence[str], argv: Sequence[str]) -> str:
        args = tuple(args)
        # mount --make-rslave DIR
        if len(args) == 2 and args[0] == "--make-rslave":
            self._dir_in_session(args[1], argv, allow_root=False)
            return "mount"
        # mount -o remount,bind,ro DIR
        if len(args) == 3 and args[0] == "-o" and set(args[1].split(",")) in BIND_OPTION_SETS and "bind" in args[1].split(","):
            self._dir_in_session(args[2], argv, allow_root=False)
            return "mount"
        # mount --bind|--rbind SRC DIR
        if len(args) == 3 and args[0] in ("--bind", "--rbind"):
            allowed = {"/dev", "/sys", "/run/udev"} | self.ctx.bind_sources
            if args[1] not in allowed:
                raise _fail(f"{args[1]} may not be bind-mounted into a session", argv)
            self._dir_in_session(args[2], argv, allow_root=False)
            return "mount"
        # mount -t FS [-o OPTS] DEV DIR
        if len(args) >= 4 and args[0] == "-t":
            fstype = args[1]
            rest = list(args[2:])
            opts = ""
            if rest and rest[0] == "-o":
                if len(rest) < 2:
                    raise _fail("mount -o needs options", argv)
                opts = rest[1]
                rest = rest[2:]
            if len(rest) != 2:
                raise _fail("mount needs a device and a directory", argv)
            device, directory = rest
            return self._typed_mount(fstype, opts, device, directory, argv)
        raise _fail("This mount form is not allowed", argv)

    def _typed_mount(self, fstype: str, opts: str, device: str, directory: str, argv: Sequence[str]) -> str:
        if fstype in VIRTUAL_FS:
            self._virtual_mount(fstype, opts, device, directory, argv)
            return "mount"
        if fstype not in MOUNT_FS:
            raise _fail(f"Filesystem type {fstype} may not be mounted", argv)
        tokens = self._check_mount_options(opts, argv)
        probing = self._dir_in_probe(directory)
        if probing:
            if "ro" not in tokens or "rw" in tokens:
                raise _fail("Probe mounts must be read-only", argv)
            if not {"nodev", "nosuid", "noexec"} <= tokens:
                raise _fail("Probe mounts must be nodev, nosuid and noexec", argv)
            self._inspect_device(device, argv)
            if not self.ctx.probing and not self.ctx.devices.allows(device):
                raise _fail(f"{device} may only be probed during a scan", argv)
            return "mount"
        self._dir_in_session(directory, argv)
        if "nodev" not in tokens:
            raise _fail("Session mounts must be nodev", argv)
        self._mutate_device(device, argv)
        if "rw" in tokens and not self.ctx.allow_write:
            raise RescueError("needs_write", "This session was opened for inspection only")
        for key in ("device", "logdev", "rtdev", "journal_path"):
            for token in opts.split(","):
                if token.startswith(key + "="):
                    self._mutate_device(token.split("=", 1)[1], argv)
        return "mount"

    def _virtual_mount(self, fstype: str, opts: str, device: str, directory: str, argv: Sequence[str]) -> None:
        if fstype == "efivarfs":
            if (device, directory, opts) != ("efivarfs", "/sys/firmware/efi/efivars", ""):
                raise _fail("efivarfs may only be mounted at its standard place", argv)
            return
        if fstype == "pstore":
            if (device, directory, opts) != ("pstore", "/sys/fs/pstore", ""):
                raise _fail("pstore may only be mounted at its standard place", argv)
            return
        if device != fstype:
            raise _fail(f"A {fstype} mount has no device", argv)
        if opts:
            self._check_mount_options(opts, argv)
        self._dir_in_session(directory, argv, allow_root=False)

    def _check_mount_options(self, opts: str, argv: Sequence[str]) -> set[str]:
        tokens: set[str] = set()
        for token in opts.split(",") if opts else []:
            key, _, value = token.partition("=")
            if token in PLAIN_MOUNT_OPTS:
                tokens.add(token)
            elif key in VALUE_MOUNT_OPTS and value and VALUE_MOUNT_OPTS[key].match(value) and ".." not in value.split("/"):
                tokens.add(key)
            else:
                raise _fail(f"Mount option {token} is not allowed", argv)
        if {"ro", "rw"} <= tokens:
            raise _fail("A mount cannot be both ro and rw", argv)
        return tokens

    def _umount(self, args: Sequence[str], argv: Sequence[str]) -> str:
        flags = [a for a in args if a.startswith("-")]
        paths = [a for a in args if not a.startswith("-")]
        if len(paths) != 1 or any(flag not in ("-R", "-l") for flag in flags):
            raise _fail("umount takes one directory and -R/-l", argv)
        path = paths[0]
        if self._dir_in_probe(path):
            return "mount"
        self._dir_in_session(path, argv)
        return "mount"

    # ---- LUKS / LVM / md ------------------------------------------------------------------------
    def _cryptsetup(self, args: Sequence[str], argv: Sequence[str]) -> str:
        if not args:
            raise _fail("cryptsetup needs a subcommand", argv)
        sub, rest = args[0], list(args[1:])
        if sub == "isLuks" and len(rest) == 1:
            self._inspect_device(rest[0], argv)
            return "inspect"
        if sub == "luksDump":
            devices = [a for a in rest if not a.startswith("-")]
            if len(devices) != 1 or any(a not in ("--dump-json-metadata",) for a in rest if a.startswith("-")):
                raise _fail("luksDump takes one device", argv)
            self._inspect_device(devices[0], argv)
            return "inspect"
        if sub == "status" and len(rest) == 1 and MAPPER_NAME_RE.match(rest[0]):
            return "inspect"
        if sub == "close" and len(rest) == 1 and MAPPER_NAME_RE.match(rest[0]):
            if rest[0] not in self.ctx.owned_maps:
                raise _fail(f"{rest[0]} was not opened by this session", argv)
            return "device"
        if sub == "open":
            return self._cryptsetup_open(rest, argv)
        raise _fail(f"cryptsetup {sub} is not allowed", argv)

    def _cryptsetup_open(self, rest: list[str], argv: Sequence[str]) -> str:
        """cryptsetup open --type luks (--key-file=- | --key-file=PATH) [--readonly] [--test-passphrase] DEV [NAME]."""
        tokens = list(rest)
        key = ""
        read_only = test_only = False
        positional: list[str] = []
        have_type = False
        while tokens:
            token = tokens.pop(0)
            if token == "--type":
                if not tokens or tokens.pop(0) != "luks":
                    raise _fail("Only LUKS volumes are opened", argv)
                have_type = True
            elif token == "--type=luks":
                have_type = True
            elif token.startswith("--key-file="):
                key = token.split("=", 1)[1]
            elif token == "--readonly":
                read_only = True
            elif token == "--test-passphrase":
                test_only = True
            elif token.startswith("-"):
                raise _fail(f"cryptsetup open option {token} is not allowed", argv)
            else:
                positional.append(token)
        del read_only
        if not have_type or not key:
            raise _fail("cryptsetup open needs --type luks and --key-file", argv)
        if key != "-":
            self._key_file_in_session(key, argv)
        if test_only:
            if len(positional) != 1:
                raise _fail("cryptsetup open --test-passphrase takes only a device", argv)
        elif len(positional) != 2 or not MAPPER_NAME_RE.match(positional[1]):
            raise _fail("cryptsetup open needs a device and a rescue-<id> mapper name", argv)
        self._mutate_device(positional[0], argv)
        return "device"

    def _vgchange(self, args: Sequence[str], argv: Sequence[str]) -> str:
        flags = [a for a in args if a.startswith("-")]
        names = [a for a in args if not a.startswith("-")]
        if len(names) != 1 or not VG_NAME_RE.match(names[0]) or names[0].startswith("-"):
            raise _fail("vgchange takes one volume group", argv)
        if not flags or flags[0] not in ("-ay", "-an") or any(f not in ("-ay", "-an", "--nohints") for f in flags):
            raise _fail("vgchange may only activate or deactivate", argv)
        if flags[0] == "-an" and names[0] not in self.ctx.owned_vgs:
            raise _fail(f"Volume group {names[0]} was not activated by this session", argv)
        return "device"

    def _mdadm(self, args: Sequence[str], argv: Sequence[str]) -> str:
        if not args:
            raise _fail("mdadm needs a mode", argv)
        mode, rest = args[0], list(args[1:])
        if mode == "--examine":
            for arg in rest:
                if arg.startswith("/dev/"):
                    self._inspect_device(arg, argv)
                elif arg not in ("--export", "--scan", "--verbose", "-s", "-v"):
                    raise _fail(f"mdadm --examine option {arg} is not allowed", argv)
            return "inspect"
        if mode == "--detail":
            for arg in rest:
                if arg.startswith("/dev/"):
                    self._inspect_device(arg, argv)
                elif arg != "--export":
                    raise _fail(f"mdadm --detail option {arg} is not allowed", argv)
            return "inspect"
        if mode == "--assemble":
            expected = ["--scan", "--readonly", f"--config={self.ctx.mdadm_conf}"]
            if sorted(rest) != sorted(expected) and sorted(rest) != sorted(expected + ["--run"]):
                raise _fail("mdadm --assemble must use the session's private config, read-only", argv)
            return "device"
        if mode == "--readwrite" and len(rest) == 1:
            self._mutate_device(rest[0], argv)
            return "device"
        if mode == "--stop" and len(rest) == 1:
            if rest[0] not in self.ctx.owned_md and DeviceAllowList.normalize(rest[0]) not in self.ctx.owned_md:
                raise _fail(f"{rest[0]} was not assembled by this session", argv)
            return "device"
        raise _fail(f"mdadm {mode} is not allowed", argv)

    # ---- file systems --------------------------------------------------------------------------------
    def _btrfs(self, args: Sequence[str], argv: Sequence[str]) -> str:
        rest = list(args)
        if rest[:2] in (["--format", "json"], ["--format", "text"]):
            rest = rest[2:]
        if not rest:
            raise _fail("btrfs needs a subcommand", argv)
        sub = rest[0]
        if sub == "device" and rest[1:2] == ["scan"] and len(rest) == 2:
            return "inspect"
        if sub == "subvolume" and len(rest) >= 3 and rest[1] in ("list", "get-default", "show"):
            path = rest[-1]
            if not (self._dir_in_probe(path) or (self.ctx.session_root and (path == self.ctx.session_root or path.startswith(self.ctx.session_root + "/")))):
                raise _fail("btrfs subvolume queries need a mounted path", argv)
            return "inspect"
        if sub == "filesystem" and rest[1:2] == ["show"]:
            return "inspect"
        if sub == "property" and rest[1:4] == ["get", "-ts", rest[-2]] and rest[-1] == "ro" and len(rest) == 5:
            if not (self._dir_in_probe(rest[3]) or (self.ctx.session_root and (rest[3] == self.ctx.session_root or rest[3].startswith(self.ctx.session_root + "/")))):
                raise _fail("btrfs property queries need a mounted path", argv)
            return "inspect"
        if sub == "check":
            device = rest[-1]
            self._mutate_device(device, argv)
            options = rest[1:-1]
            if options not in (["--readonly"], ["--repair"]):
                raise _fail("btrfs check takes --readonly or --repair", argv)
            return "device"
        if sub == "rescue" and len(rest) == 3 and rest[1] in ("zero-log", "super-recover"):
            self._mutate_device(rest[2], argv)
            return "device"
        raise _fail("This btrfs command is not allowed", argv)

    def _fs_check(self, args: Sequence[str], argv: Sequence[str], flags: set[str], *, needs_flag: bool = False) -> str:
        devices = [a for a in args if a.startswith("/dev/")]
        options = [a for a in args if not a.startswith("/dev/")]
        if len(devices) != 1 or any(o not in flags for o in options):
            raise _fail("This file system check form is not allowed", argv)
        if needs_flag and not options:
            raise _fail("This file system check needs an explicit mode", argv)
        self._mutate_device(devices[0], argv)
        return "device"

    def _e2fsck(self, args: Sequence[str], argv: Sequence[str]) -> str:
        return self._fs_check(args, argv, {"-n", "-f", "-p", "-y"})

    _fsck_ext2 = _fsck_ext3 = _fsck_ext4 = _e2fsck

    def _xfs_repair(self, args: Sequence[str], argv: Sequence[str]) -> str:
        return self._fs_check(args, argv, {"-n", "-L"})

    def _fsck_vfat(self, args: Sequence[str], argv: Sequence[str]) -> str:
        return self._fs_check(args, argv, {"-n", "-a"})

    _fsck_fat = _fsck_vfat

    def _fsck_f2fs(self, args: Sequence[str], argv: Sequence[str]) -> str:
        return self._fs_check(args, argv, {"--dry-run", "-f", "-a"})

    def _fsck_exfat(self, args: Sequence[str], argv: Sequence[str]) -> str:
        return self._fs_check(args, argv, {"-n", "-p"})

    def _jfs_fsck(self, args: Sequence[str], argv: Sequence[str]) -> str:
        return self._fs_check(args, argv, {"-n", "-a"})

    # ---- EFI ---------------------------------------------------------------------------------------------
    EFI_FLAGS = {
        "--create": "write", "-c": "write", "--create-only": "write", "-C": "write", "--delete-bootnum": "write", "-B": "write",
        "--active": "write", "-a": "write", "--inactive": "write", "-A": "write", "--unicode": "unicode", "-u": "unicode",
        "--verbose": "", "-v": "", "--quiet": "", "-q": "",
    }
    EFI_VALUED = {
        "--disk": "--disk", "-d": "--disk", "--part": "--part", "-p": "--part", "--label": "--label", "-L": "--label",
        "--loader": "--loader", "-l": "--loader", "--bootnum": "--bootnum", "-b": "--bootnum", "--bootorder": "--bootorder",
        "-o": "--bootorder", "--bootnext": "--bootnext", "-n": "--bootnext", "--timeout": "--timeout", "-t": "--timeout",
        "--index": "--index", "-I": "--index",
    }

    def _efibootmgr(self, args: Sequence[str], argv: Sequence[str]) -> str:
        """efibootmgr 18: flags, valued options, and (with --unicode) positional boot arguments."""
        tokens = list(args)
        writes = False
        unicode_flag = False
        positional: list[str] = []
        valued: dict[str, str] = {}
        while tokens:
            token = tokens.pop(0)
            key, eq, inline = token.partition("=")
            if token in self.EFI_FLAGS:
                kind = self.EFI_FLAGS[token]
                writes = writes or kind == "write"
                unicode_flag = unicode_flag or kind == "unicode"
            elif key in self.EFI_VALUED and (eq or tokens):
                option = self.EFI_VALUED[key]
                value = inline if eq else tokens.pop(0)
                self._efi_value(option, value, argv)
                valued[option] = value
                writes = writes or option in ("--bootorder", "--bootnext", "--timeout")
            elif not token.startswith("-") and unicode_flag:
                positional.append(token)
            else:
                raise _fail(f"efibootmgr option {token} is not allowed", argv)
        if positional:
            if not unicode_flag or any(item.startswith("-") or "\x00" in item or "\n" in item or len(item) > 2048 for item in positional):
                raise _fail("efibootmgr boot arguments need --unicode and plain text", argv)
        if ("--create" in args or "-c" in args or "--create-only" in args or "-C" in args) and "--loader" in valued and "--disk" not in valued:
            raise _fail("A new boot entry must name its disk", argv)
        return "efi" if writes else "inspect"

    def _efi_value(self, option: str, value: str, argv: Sequence[str]) -> None:
        if option == "--disk":
            if not DEV_RE.match(value) or not self.ctx.devices.allows_disk(value):
                raise _fail(f"{value} is not one of the session's disks", argv)
        elif option in ("--bootnum", "--bootnext"):
            if not BOOTNUM_RE.match(value):
                raise _fail("Boot numbers are four hex digits", argv)
        elif option == "--bootorder":
            if not value or not all(BOOTNUM_RE.match(item) for item in value.split(",")):
                raise _fail("Boot order must be a list of four-digit hex numbers", argv)
        elif option == "--label":
            if not EFI_LABEL_RE.match(value):
                raise _fail("Boot entry labels use letters, digits, space, dot, dash and underscore", argv)
        elif option == "--loader":
            if not re.fullmatch(r"\\[A-Za-z0-9 ._\\-]{1,200}", value):
                raise _fail("Loader must be an EFI path such as \\EFI\\dir\\file.efi", argv)
        elif option in ("--part", "--timeout", "--index"):
            if not value.isdigit():
                raise _fail(f"{option} takes a number", argv)

    # ---- bootloader installed from the live side (review R15) --------------------------------------------------
    def _grub_install(self, args: Sequence[str], argv: Sequence[str]) -> str:
        boot_directory = ""
        disk = ""
        for arg in args:
            if arg.startswith("--boot-directory="):
                boot_directory = arg.split("=", 1)[1]
                self._dir_in_session(boot_directory, argv, allow_root=False)
            elif arg.startswith("--efi-directory="):
                self._dir_in_session(arg.split("=", 1)[1], argv, allow_root=False)
            elif arg.startswith("--target=") and arg.split("=", 1)[1] in ("x86_64-efi", "i386-efi", "i386-pc"):
                continue
            elif arg.startswith("--bootloader-id=") and re.fullmatch(r"[A-Za-z0-9._-]{1,32}", arg.split("=", 1)[1]):
                continue
            elif arg in ("--removable", "--no-nvram", "--recheck", "--no-floppy", "--uefi-secure-boot"):
                continue
            elif arg.startswith("/dev/"):
                disk = arg
                if not self.ctx.devices.allows_disk(arg):
                    raise _fail(f"{arg} is not one of the session's disks", argv)
            else:
                raise _fail(f"grub-install option {arg} is not allowed", argv)
        if not boot_directory:
            raise _fail("Live-side grub-install must name the target's boot directory", argv)
        return "device"

    def _chroot(self, args: Sequence[str], argv: Sequence[str]) -> str:
        raise _fail("chroot is only built by the runner", argv)

    def _script(self, args: Sequence[str], argv: Sequence[str]) -> str:
        raise _fail("script is only used by the terminal launcher", argv)

    _setsid = _script


@dataclass(frozen=True)
class ChrootCommand:
    """A command to run inside the target, validated against an action's tool set."""

    argv: tuple[str, ...]
    tools: frozenset[str]


class ChrootPolicy:
    def __init__(self, context: PolicyContext, *, esp_mountpoints: Iterable[str] = ()) -> None:
        self.ctx = context
        self.esp_mountpoints = tuple(esp_mountpoints)

    def validate(self, argv: Sequence[str], tools: Iterable[str]) -> None:
        if not argv:
            raise SafetyViolation("Empty chroot command")
        name = argv[0].rsplit("/", 1)[-1]
        allowed = set(tools)
        if is_denied(name, chroot=True):
            raise _fail(f"{name} is never run inside a repaired system", argv)
        if name not in allowed:
            raise _fail(f"{name} is not part of this action's tool set", argv)
        denied_subs = CHROOT_DENIED_SUBCOMMANDS.get(name, set())
        if denied_subs & set(argv[1:]):
            raise _fail(f"{name} {' '.join(sorted(denied_subs & set(argv[1:])))} is not allowed", argv)
        for arg in argv[1:]:
            for token in re.split(r"[=,]", arg):
                if token.startswith("/dev/") and not self._device_ok(token):
                    raise _fail(f"{token} was not selected for this session", argv)
        if name in ("grub-install", "grub2-install"):
            self._grub_install(argv)
        elif name == "bootctl":
            for arg in argv[1:]:
                if arg.startswith("--esp-path=") and self.esp_mountpoints and arg.split("=", 1)[1] not in self.esp_mountpoints:
                    raise _fail("bootctl --esp-path must be a mount of this session", argv)

    def _device_ok(self, path: str) -> bool:
        return self.ctx.devices.allows(path) or self.ctx.devices.allows_disk(path)

    def _grub_install(self, argv: Sequence[str]) -> None:
        positional = [a for a in argv[1:] if not a.startswith("-")]
        for arg in argv[1:]:
            if arg.startswith("--efi-directory=") and self.esp_mountpoints and arg.split("=", 1)[1] not in self.esp_mountpoints:
                raise _fail("--efi-directory must be a mount of this session", argv)
        if positional:
            disk = positional[-1]
            if not self.ctx.devices.allows_disk(disk):
                raise _fail(f"{disk} is not one of the session's disks", argv)


def check_mount_destination(destination: str, session_root: str, *, file_ok: bool = False) -> None:
    """Re-check a mount destination immediately before the mount (review R2).

    The destination is a live-system path composed by ``RootFS.mount_destination`` from the
    session root and canonical components; this verifies that on the live system it is still a
    plain directory below the root with no symlink left in it.
    """
    if not session_root or not (destination == session_root or destination.startswith(session_root + "/")):
        raise SafetyViolation(f"{destination} is outside the session root", destination=destination)
    try:
        info = os.lstat(destination)
    except OSError as exc:
        raise SafetyViolation(f"Mount point {destination} does not exist: {exc.strerror}", destination=destination) from exc
    if not (stat.S_ISDIR(info.st_mode) or (file_ok and stat.S_ISREG(info.st_mode))):
        raise SafetyViolation(f"Mount point {destination} is not a directory", destination=destination)
    if os.path.realpath(destination) != destination:  # host-path: composed from the session root, must be symlink free
        raise SafetyViolation(f"Mount point {destination} contains a symbolic link", destination=destination)
