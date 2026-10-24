"""Fakes for the rescue tests: a recording runner and a stateful fake host.

``FakeSystem`` implements the ``Runner`` protocol. It models just enough of a machine for the rescue engine to run
end to end without a device: lsblk output that follows unlock/activation state, mounts that copy a device's file
tree into the mount directory (and back on unmount for read-write mounts), blockdev read-only flags, LUKS, LVM, md,
btrfs listings, efibootmgr, and chroot commands with optional effects on the fake root. Nothing touches a real device.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gentoo_installerd.rescue.safety import CHROOT_CREDENTIALS, CHROOT_WRAPPER_ARGV
from gentoo_installerd.runner import CommandResult, RunnerError, validate_argv

DESTRUCTIVE = ("mkfs", "mkswap", "wipefs", "sgdisk", "sfdisk", "fdisk", "gdisk", "parted", "partprobe", "dd", "shred", "blkdiscard", "pvcreate", "vgcreate", "lvcreate", "lvremove",
               "vgremove", "mke2fs", "resize2fs", "xfs_growfs", "losetup", "swapon", "swapoff")


@dataclass
class Call:
    argv: tuple[str, ...]
    stdin: str | None = None
    secret: bool = False
    env: dict[str, str] | None = None
    timeout: int | None = None


@dataclass
class MountRec:
    device: str
    fstype: str
    options: str
    target: str
    stash: Path | None = None
    bind: bool = False
    subtree: str = ""

    @property
    def rw(self) -> bool:
        return "rw" in self.options.split(",") or ("ro" not in self.options.split(",") and not self.bind)


@dataclass
class LuksSpec:
    uuid: str
    passphrase: str
    version: int = 2
    tokens: tuple[str, ...] = ()
    memory_kib: int = 1048576
    mapper: str = ""


@dataclass
class VgSpec:
    name: str
    uuid: str
    pvs: list[str]
    lvs: list[dict[str, Any]]
    active: bool = False
    missing: int = 0


@dataclass
class MdSpec:
    uuid: str  # colon form as mdadm prints it
    members: list[str]
    level: str = "raid1"
    devices: int = 2
    name: str = "host:root"
    metadata: str = "1.2"
    assembled: bool = False
    device: str = "/dev/md127"


class FakeSystem:
    def __init__(self, tmp: Path, lsblk: str | Callable[[FakeSystem], str] = "", *, filesystems: Sequence[str] = ("ext4", "xfs", "btrfs", "vfat", "f2fs", "jfs", "proc", "tmpfs")) -> None:
        self.tmp = tmp
        self._lsblk = lsblk
        self.calls: list[Call] = []
        self.mounts: dict[str, MountRec] = {}
        self.device_trees: dict[str, Path] = {}
        self.subvolumes: dict[str, dict[int, str]] = {}  # device -> {subvolume id: path inside the top-level tree}
        self.btrfs_text: dict[str, str] = {}
        self.btrfs_default: dict[str, str] = {}
        self.btrfs_ro: dict[str, bool] = {}
        self.btrfs_json = True
        self.ro: dict[str, bool] = {}
        self.luks: dict[str, LuksSpec] = {}
        self.mapped: dict[str, str] = {}  # mapper name -> container device
        self.vgs: dict[str, VgSpec] = {}
        self.md: dict[str, MdSpec] = {}
        self.live_mounts: list[tuple[str, str]] = []
        self.filesystems = set(filesystems)
        self.fail_mount: dict[str, str] = {}
        self.fail_umount: dict[str, tuple[int, str]] = {}  # target -> (rc, message) while "busy"
        self.holders: dict[str, str] = {}
        self.blkid: dict[str, str] = {}
        self.scripted: list[tuple[Callable[[tuple[str, ...]], bool], tuple[int, str]]] = []
        self.chroot_effects: dict[str, Callable[[FakeSystem, Path, tuple[str, ...], dict[str, str]], tuple[int, str]]] = {}
        self.chroot_failures: dict[str, tuple[int, str]] = {}
        self.chroot_calls: list[tuple[Path, dict[str, str], tuple[str, ...]]] = []
        self.chroot_snapshots: list[dict[str, Any]] = []
        self.nvram: dict[str, Any] = {"entries": {}, "order": [], "current": "", "next": "", "usable": True}
        self.settled = 0
        self.fail_at: dict[int, tuple[int, str]] = {}  # call index -> (rc, output)

    # ---- helpers for tests --------------------------------------------------------------------
    @property
    def commands(self) -> list[tuple[str, ...]]:
        return [call.argv for call in self.calls]

    def commands_named(self, name: str) -> list[tuple[str, ...]]:
        return [argv for argv in self.commands if argv[0] == name]

    def commands_with(self, *prefix: str) -> list[tuple[str, ...]]:
        return [argv for argv in self.commands if argv[: len(prefix)] == prefix]

    def script(self, prefix: str | Sequence[str], stdout: str = "", rc: int = 0) -> None:
        wanted = tuple(shlex.split(prefix)) if isinstance(prefix, str) else tuple(prefix)
        self.scripted.insert(0, (lambda argv, w=wanted: argv[: len(w)] == w, (rc, stdout)))

    def attach(self, device: str, tree: Path) -> None:
        self.device_trees[device] = tree

    def is_mounted(self, target: str) -> bool:
        return str(target) in self.mounts

    def mounted_targets(self) -> list[str]:
        return sorted(self.mounts)

    def tree_of(self, device: str) -> Path:
        return self.device_trees[device]

    # ---- Runner protocol ---------------------------------------------------------------------------------
    def run(self, argv: Sequence[str], *, stdin: str | None = None, secret: bool = False, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> CommandResult:
        args = validate_argv(argv)
        timeout = None
        inner = args
        if inner[0] == "timeout":
            rest = [a for a in inner[1:] if not a.startswith("--kill-after=")]
            timeout, inner = int(rest[0]), tuple(rest[1:])
        call = Call(inner, stdin, secret, dict(env) if env else None, timeout)
        self.calls.append(call)
        index = len(self.calls) - 1
        if index in self.fail_at:
            rc, out = self.fail_at[index]
            return self._result(args, rc, out)
        for matcher, (rc, out) in self.scripted:
            if matcher(inner):
                return self._result(args, rc, out)
        handler = getattr(self, "_do_" + re.sub(r"[^a-z0-9]", "_", inner[0].lower()), None)
        if handler is None:
            return self._result(args, 0, "")
        rc, out = handler(inner, call)
        return self._result(args, rc, out)

    @staticmethod
    def _result(args: tuple[str, ...], rc: int, out: str) -> CommandResult:
        if rc != 0:
            raise RunnerError(args, rc, out)
        return CommandResult(args, rc, out)

    # ---- lsblk / findmnt / blockdev / blkid ---------------------------------------------------------------------
    def _do_lsblk(self, argv: tuple[str, ...], call: Call) -> tuple[int, str]:
        return 0, self._lsblk(self) if callable(self._lsblk) else self._lsblk

    def _do_findmnt(self, argv: tuple[str, ...], call: Call) -> tuple[int, str]:
        columns = ["SOURCE", "TARGET"]
        if "-o" in argv:
            columns = argv[argv.index("-o") + 1].split(",")
        paths = [a for a in argv[1:] if a.startswith("/") and (not "-o" in argv or a != argv[argv.index("-o") + 1])]
        rows: list[dict[str, str]] = [{"SOURCE": source, "TARGET": target, "FSTYPE": "iso9660", "OPTIONS": "ro"} for source, target in self.live_mounts]
        for target in sorted(self.mounts):
            rec = self.mounts[target]
            rows.append({"SOURCE": rec.device, "TARGET": target, "FSTYPE": rec.fstype, "OPTIONS": rec.options})
        if "/sys/firmware/efi/efivars" in paths:
            return (0, "rw,nosuid,nodev,noexec,relatime") if self.efivars else (1, "")
        if paths:
            base = paths[0]
            rows = [row for row in rows if row["TARGET"] == base or row["TARGET"].startswith(base + "/")]
            if not rows and "-R" not in argv:
                return 1, ""
        return 0, "".join("\t".join(row[column] for column in columns) + "\n" for row in rows)

    efivars = True

    def _do_blockdev(self, argv: tuple[str, ...], call: Call) -> tuple[int, str]:
        flag, device = argv[1], argv[2]
        if flag == "--getro":
            return 0, "1\n" if self.ro.get(device) else "0\n"
        self.ro[device] = flag == "--setro"
        return 0, ""

    def _do_blkid(self, argv: tuple[str, ...], call: Call) -> tuple[int, str]:
        device = argv[-1]
        return (0, self.blkid[device]) if device in self.blkid else (2, "")

    def _do_udevadm(self, argv: tuple[str, ...], call: Call) -> tuple[int, str]:
        self.settled += 1
        return 0, ""

    # ---- mount / umount -----------------------------------------------------------------------------------------
    def _do_mount(self, argv: tuple[str, ...], call: Call) -> tuple[int, str]:
        args = list(argv[1:])
        if args[:1] == ["--make-rslave"]:
            return 0, ""
        if args[:1] == ["-o"] and "remount" in args[1]:
            target = args[2]
            if target in self.mounts:
                self.mounts[target].options = "ro,bind"
                return 0, ""
            return 32, f"mount: {target}: not mounted"
        if args[0] in ("--bind", "--rbind"):
            source, target = args[1], args[2]
            if not os.path.exists(target):
                return 32, f"mount: {target}: mount point does not exist"
            self.mounts[target] = MountRec(source, "none", args[0][2:], target, bind=True)
            return 0, ""
        fstype = args[1]
        rest = args[2:]
        options = ""
        if rest[0] == "-o":
            options, rest = rest[1], rest[2:]
        device, target = rest
        if fstype in ("proc", "tmpfs", "efivarfs", "pstore"):
            if not os.path.exists(target):
                return 32, f"mount: {target}: mount point does not exist"
            stash = self._stash(target) if fstype in ("tmpfs", "proc") else None
            if fstype == "proc":
                Path(target, "cmdline").write_text("live cmdline root=live:CDLABEL=GENTOO\n")
            self.mounts[target] = MountRec(device, fstype, options, target, stash)
            return 0, ""
        if fstype not in self.filesystems:
            return 32, f"mount: {target}: unknown filesystem type '{fstype}'."
        if device in self.fail_mount:
            return 32, self.fail_mount[device]
        if not os.path.isdir(target):
            return 32, f"mount: {target}: mount point does not exist."
        if device not in self.device_trees:
            return 32, f"mount: {target}: wrong fs type, bad option, bad superblock on {device}."
        if "rw" in options.split(",") and self.ro.get(device):
            return 32, f"mount: {target}: {device} is write-protected, mounting read-only."
        subtree = ""
        if fstype == "btrfs":
            match = re.search(r"subvolid=(\d+)", options)
            subvolid = int(match.group(1)) if match else 5
            subtree = self.subvolumes.get(device, {5: ""}).get(subvolid, "")
            if subvolid not in self.subvolumes.get(device, {5: ""}):
                return 32, f"mount: {target}: wrong fs type, bad option, bad superblock on {device}."
        source = self.device_trees[device] / subtree if subtree else self.device_trees[device]
        stash = self._stash(target)
        shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True, copy_function=shutil.copy2)
        self.mounts[target] = MountRec(device, fstype, options, target, stash, subtree=subtree)
        return 0, ""

    def _stash(self, target: str) -> Path | None:
        entries = list(Path(target).iterdir())
        if not entries:
            return None
        stash = self.tmp / ".stash" / re.sub(r"[^A-Za-z0-9]", "_", target)
        stash.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            shutil.move(str(entry), str(stash / entry.name))
        return stash

    def _do_umount(self, argv: tuple[str, ...], call: Call) -> tuple[int, str]:
        flags = [a for a in argv[1:] if a.startswith("-")]
        target = [a for a in argv[1:] if not a.startswith("-")][0]
        if target not in self.mounts:
            return 32, f"umount: {target}: not mounted."
        children = sorted((t for t in self.mounts if t.startswith(target + "/")), key=len, reverse=True)
        if target in self.fail_umount and "-l" not in flags:
            rc, message = self.fail_umount[target]
            return rc, message
        if children and "-R" not in flags and "-l" not in flags:
            return 32, f"umount: {target}: target is busy."
        for child in children:
            self._unmount_one(child)
        self._unmount_one(target)
        return 0, ""

    def _unmount_one(self, target: str) -> None:
        rec = self.mounts.pop(target)
        directory = Path(target)
        if not rec.bind and rec.fstype not in ("proc", "tmpfs", "efivarfs", "pstore"):
            if rec.rw and rec.device in self.device_trees:
                destination = self.device_trees[rec.device] / rec.subtree if rec.subtree else self.device_trees[rec.device]
                shutil.rmtree(destination, ignore_errors=True)
                shutil.copytree(directory, destination, symlinks=True, copy_function=shutil.copy2)
        if directory.is_dir():
            for entry in list(directory.iterdir()):
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
        if rec.stash is not None:
            for entry in list(rec.stash.iterdir()):
                shutil.move(str(entry), str(directory / entry.name))

    def _do_fuser(self, argv: tuple[str, ...], call: Call) -> tuple[int, str]:
        path = argv[-1]
        text = self.holders.get(path, "")
        return (0, text) if text else (1, "")

    # ---- LUKS ------------------------------------------------------------------------------------------------------
    def _do_cryptsetup(self, argv: tuple[str, ...], call: Call) -> tuple[int, str]:
        sub = argv[1]
        if sub == "isLuks":
            return (0, "") if argv[2] in self.luks else (1, "")
        if sub == "luksDump":
            spec = self.luks.get(argv[-1])
            if not spec:
                return 1, ""
            doc = {"tokens": {str(i): {"type": kind} for i, kind in enumerate(spec.tokens)}, "keyslots": {"0": {"type": "luks2", "kdf": {"type": "argon2id", "memory": spec.memory_kib}}}}
            return 0, json.dumps(doc)
        if sub == "status":
            return (0, "/dev/mapper/x is active.\n") if argv[2] in self.mapped else (4, "")
        if sub == "close":
            name = argv[2]
            if name not in self.mapped:
                return 4, ""
            if any(rec.device == f"/dev/mapper/{name}" for rec in self.mounts.values()):
                return 5, f"Device {name} is still in use."
            del self.mapped[name]
            return 0, ""
        if sub == "open":
            rest = [a for a in argv[2:] if not a.startswith("--") and a != "luks"]
            device = rest[0]
            spec = self.luks.get(device)
            if spec is None:
                return 4, "Device is not a valid LUKS device."
            if (call.stdin or "") != spec.passphrase:
                return 2, "No key available with this passphrase."
            if "--test-passphrase" in argv:
                return 0, ""
            name = rest[1]
            if name in self.mapped:
                return 5, "Device already exists."
            self.mapped[name] = device
            return 0, ""
        return 1, ""

    # ---- LVM ---------------------------------------------------------------------------------------------------------
    def _visible_pvs(self) -> list[tuple[VgSpec, str]]:
        found = []
        for vg in self.vgs.values():
            for pv in vg.pvs:
                name = pv.rsplit("/", 1)[-1]
                if pv.startswith("/dev/mapper/") and name not in self.mapped:
                    continue
                found.append((vg, pv))
        return found

    def _do_pvs(self, argv: tuple[str, ...], call: Call) -> tuple[int, str]:
        rows = [
            {"pv_name": pv, "pv_uuid": "pvuuid-" + pv.rsplit("/", 1)[-1], "vg_name": vg.name, "vg_uuid": vg.uuid, "vg_pv_count": str(len(vg.pvs)), "vg_missing_pv_count": str(vg.missing),
             "pv_size": "1000"}
            for vg, pv in self._visible_pvs()
        ]
        return 0, "  WARNING: stray warning on stderr\n" + json.dumps({"report": [{"pv": rows}]})

    def _do_vgs(self, argv: tuple[str, ...], call: Call) -> tuple[int, str]:
        return 0, json.dumps({"report": [{"vg": [{"vg_name": vg.name} for vg in self.vgs.values()]}]})

    def _do_lvs(self, argv: tuple[str, ...], call: Call) -> tuple[int, str]:
        rows = []
        visible = {vg.name for vg, _pv in self._visible_pvs()}
        for vg in self.vgs.values():
            if vg.name not in visible:
                continue
            for lv in vg.lvs:
                rows.append({"lv_name": lv["name"], "vg_name": vg.name, "lv_path": f"/dev/{vg.name}/{lv['name']}", "lv_attr": "-wi-a-----" if vg.active else "-wi-------",
                             "lv_size": str(lv.get("size", 1000)), "segtype": lv.get("segtype", "linear"), "pool_lv": "", "origin": "", "lv_uuid": "lvuuid"})
        return 0, json.dumps({"report": [{"lv": rows}]})

    def _do_vgchange(self, argv: tuple[str, ...], call: Call) -> tuple[int, str]:
        vg = self.vgs[argv[-1]]
        if "-an" in argv:
            if any(rec.device.startswith(f"/dev/mapper/{vg.name}-") for rec in self.mounts.values()):
                return 5, f"Logical volume {vg.name}/root in use."
            vg.active = False
        else:
            vg.active = True
        return 0, ""

    # ---- md ---------------------------------------------------------------------------------------------------------------
    def _do_mdadm(self, argv: tuple[str, ...], call: Call) -> tuple[int, str]:
        mode = argv[1]
        if mode == "--examine":
            devices = [a for a in argv[2:] if a.startswith("/dev/")]
            if "--scan" in argv:
                lines = []
                for spec in self.md.values():
                    if any(d in spec.members for d in devices):
                        lines.append(f"ARRAY /dev/md/{spec.name.split(':')[-1]} metadata={spec.metadata} UUID={spec.uuid} name={spec.name}")
                return 0, "\n".join(lines) + "\n"
            for spec in self.md.values():
                if devices and devices[0] in spec.members:
                    return 0, f"MD_LEVEL={spec.level}\nMD_DEVICES={spec.devices}\nMD_NAME={spec.name}\nMD_UUID={spec.uuid}\nMD_METADATA={spec.metadata}\n"
            return 1, ""
        if mode == "--assemble":
            conf = [a for a in argv if a.startswith("--config=")][0].split("=", 1)[1]
            text = Path(conf).read_text()
            for spec in self.md.values():
                if spec.uuid in text:
                    spec.assembled = True
            return 0, ""
        if mode == "--stop":
            for spec in self.md.values():
                if spec.device == argv[2]:
                    if any(rec.device == spec.device for rec in self.mounts.values()):
                        return 1, "mdadm: Cannot get exclusive access"
                    spec.assembled = False
            return 0, ""
        return 0, ""

    # ---- btrfs --------------------------------------------------------------------------------------------------------------
    def _btrfs_device_for(self, path: str) -> str:
        best = ""
        for target, rec in self.mounts.items():
            if (path == target or path.startswith(target + "/")) and len(target) > len(best):
                best = target
        return self.mounts[best].device if best else ""

    def _do_btrfs(self, argv: tuple[str, ...], call: Call) -> tuple[int, str]:
        args = list(argv[1:])
        if args[:2] == ["--format", "json"]:
            args = args[2:]
            as_json = True
        else:
            as_json = False
        if args[:2] == ["device", "scan"]:
            return 0, ""
        device = next((self._btrfs_device_for(arg) for arg in reversed(args) if arg.startswith("/") and self._btrfs_device_for(arg)), "")
        if args[:2] == ["subvolume", "list"]:
            if as_json and not self.btrfs_json:
                return 1, "ERROR: unrecognized option '--format'"
            text = self.btrfs_text.get(device, "")
            if as_json:
                doc = {"subvolume-list": [{"id": int(m.group(1)), "top_level": int(m.group(2)), "path": m.group(3)} for m in re.finditer(r"ID (\d+) gen \d+ top level (\d+) path (.+)", text)]}
                return 0, json.dumps(doc)
            return 0, text
        if args[:2] == ["subvolume", "get-default"]:
            return 0, self.btrfs_default.get(device, "ID 5 (FS_TREE)") + "\n"
        if args[:2] == ["property", "get"]:
            path = args[3]
            for target in sorted(self.mounts, key=len, reverse=True):
                if path.startswith(target):
                    relative = path[len(target):].strip("/")
                    return 0, f"ro={'true' if self.btrfs_ro.get((device, relative)) else 'false'}\n"
            return 1, ""
        return 0, ""

    # ---- EFI ---------------------------------------------------------------------------------------------------------------------
    def _do_efibootmgr(self, argv: tuple[str, ...], call: Call) -> tuple[int, str]:
        nvram = self.nvram
        if not nvram["usable"]:
            return 2, "EFI variables are not supported on this system."
        args = list(argv[1:])
        if not args or args in (["-v"], ["--verbose"]):
            return 0, self.efibootmgr_text()
        def value(name: str) -> str:
            return args[args.index(name) + 1]
        if "--delete-bootnum" in args:
            number = value("--bootnum")
            nvram["entries"].pop(number, None)
            nvram["order"] = [item for item in nvram["order"] if item != number]
        elif "--create" in args or "--create-only" in args:
            number = f"{max([int(k, 16) for k in nvram['entries']] + [-1]) + 1:04X}"
            nvram["entries"][number] = {"label": value("--label"), "loader": value("--loader"), "active": True, "disk": value("--disk"), "part": value("--part")}
            if "--create" in args:
                nvram["order"].insert(0, number)
        elif "--bootorder" in args:
            nvram["order"] = value("--bootorder").split(",")
        elif "--bootnext" in args:
            nvram["next"] = value("--bootnext")
        elif "--active" in args:
            nvram["entries"][value("--bootnum")]["active"] = True
        elif "--inactive" in args:
            nvram["entries"][value("--bootnum")]["active"] = False
        return 0, self.efibootmgr_text()

    def efibootmgr_text(self) -> str:
        nvram = self.nvram
        lines = []
        if nvram["next"]:
            lines.append(f"BootNext: {nvram['next']}")
        if nvram["current"]:
            lines.append(f"BootCurrent: {nvram['current']}")
        lines.append("Timeout: 1 seconds")
        lines.append("BootOrder: " + ",".join(nvram["order"]))
        for number, entry in nvram["entries"].items():
            partuuid = entry.get("partuuid", "11111111-2222-3333-4444-555555555555")
            lines.append(f"Boot{number}{'*' if entry['active'] else ' '} {entry['label']}\tHD({entry.get('part', 1)},GPT,{partuuid},0x800,0x100000)/File({entry['loader']})")
        return "\n".join(lines) + "\n"

    # ---- chroot ---------------------------------------------------------------------------------------------------------------------
    def _do_sh(self, argv: tuple[str, ...], call: Call) -> tuple[int, str]:
        if argv[:4] != CHROOT_WRAPPER_ARGV:
            return 127, "sh: unexpected"
        rest = list(argv[4:])
        assert tuple(rest[:2]) == CHROOT_CREDENTIALS, rest[:2]
        root = rest[2]
        assert rest[3:5] == ["/usr/bin/env", "-i"], rest[3:5]
        index = 5
        env: dict[str, str] = {}
        while index < len(rest) and "=" in rest[index] and re.match(r"^[A-Z_]+=", rest[index]):
            key, _, val = rest[index].partition("=")
            env[key] = val
            index += 1
        command = tuple(rest[index:])
        self.chroot_calls.append((Path(root), env, command))
        proc_cmdline = Path(root, "proc/cmdline")
        self.chroot_snapshots.append({"cmdline": proc_cmdline.read_text() if proc_cmdline.exists() else "", "stdin": call.stdin, "secret": call.secret})
        name = command[0].rsplit("/", 1)[-1]
        if root not in self.mounts:
            return 1, "chroot: cannot change root directory"
        if name in self.chroot_failures:
            rc, out = self.chroot_failures[name]
            return rc, out
        if name in self.chroot_effects:
            return self.chroot_effects[name](self, Path(root), command, {"stdin": call.stdin or ""})
        return 0, ""


# ---- fixture-tree helpers ---------------------------------------------------------------------------------------------------------
def make_host(base: Path, *, uefi: bool = True, cmdline: str = "BOOT_IMAGE=/boot/gentoo dokeymap nodhcp secureconsole root=live:CDLABEL=GENTOO_INSTALLER_AMD64 rd.live.dir=/ cdroot",
              mem_total_kib: int = 16 * 1024 * 1024, mem_available_kib: int = 8 * 1024 * 1024, secure_boot: bool | None = False, fw_bits: int = 64,
              filesystems: Sequence[str] = ("ext4", "xfs", "btrfs", "vfat", "f2fs", "jfs")) -> Path:
    (base / "proc").mkdir(parents=True, exist_ok=True)
    (base / "proc/cmdline").write_text(cmdline + "\n")
    (base / "proc/meminfo").write_text(f"MemTotal:       {mem_total_kib} kB\nMemFree:         100 kB\nMemAvailable:   {mem_available_kib} kB\n")
    (base / "proc/filesystems").write_text("".join(f"\t{name}\n" if name not in ("proc", "tmpfs") else f"nodev\t{name}\n" for name in filesystems))
    (base / "sys").mkdir(parents=True, exist_ok=True)
    if uefi:
        efi = base / "sys/firmware/efi"
        (efi / "efivars").mkdir(parents=True, exist_ok=True)
        (efi / "fw_platform_size").write_text(f"{fw_bits}\n")
        if secure_boot is not None:
            (efi / "efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c").write_bytes(b"\x06\x00\x00\x00" + (b"\x01" if secure_boot else b"\x00"))
    return base


def tree_digest(root: Path) -> dict[str, str]:
    """Relative path -> content digest or link text for every entry (to prove a tree was not changed)."""
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            result[relative] = "->" + os.readlink(path)
        elif path.is_file():
            result[relative] = f"{path.stat().st_size}:{stat.S_IMODE(path.stat().st_mode):o}:" + str(hash(path.read_bytes()))
        else:
            result[relative] = "dir"
    return result


def assert_nothing_destructive(system: FakeSystem) -> None:
    offenders = [argv for argv in system.commands if argv[0].split("/")[-1].startswith(DESTRUCTIVE) or argv[0] == "tune2fs" and argv[1] != "-l"]
    assert not offenders, offenders
    for argv in system.commands:
        if argv[0] == "cryptsetup":
            assert argv[1] in {"isLuks", "luksDump", "open", "close", "status"}, argv
        if argv[0] == "mount":
            assert "-a" not in argv, argv
