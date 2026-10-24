"""A stateful FAKE runner (no subprocess, no devices) for end-to-end StorageManager tests.

It models just enough of lsblk, sgdisk, parted, wipefs, mkfs, mount, swap, LVM, dm-crypt, md and
btrfs for the storage safety rules to run for real: partitions appear after sgdisk, active
holders (LVM, dm-crypt, md) make a partition busy until they are stopped, and a busy partition can
be neither wiped nor formatted, exactly what the kernel enforces. Nothing here touches a device.

The module has no tests of its own; test_storage_*.py import it.
"""

from __future__ import annotations

import json
import re
from typing import Callable

from gentoo_installerd.runner import CommandResult, RunnerError, validate_argv

MiB = 2**20
GiB = 2**30
BIOS_BOOT = "21686148-6449-6e6f-744e-656564454649"
ESP = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
LINUX = "0fc63daf-8483-4772-8e79-3d69d8477de4"
SWAP = "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f"

# Commands that change what is stored on a disk (or in a device-mapper stack). Releasing an
# active volume (vgchange -an, cryptsetup close, mdadm --stop, swapoff) is deliberately not here.
WRITE_EXECUTABLES = {"sgdisk", "wipefs", "mkswap", "pvcreate", "vgcreate", "lvcreate"}


def is_write(argv: tuple[str, ...]) -> bool:
    exe = argv[0]
    return (
        exe.startswith("mkfs.")
        or exe in WRITE_EXECUTABLES
        or (exe == "parted" and any(word in argv for word in ("mklabel", "mkpart", "set", "disk_set")))
        or (exe == "cryptsetup" and "luksFormat" in argv)
        or (exe == "btrfs" and "mkswapfile" in argv)
    )


def first_write(commands: list[tuple[str, ...]]) -> int | None:
    return next((index for index, argv in enumerate(commands) if is_write(argv)), None)


class Holder:
    """An active stacked device (type lvm/crypt/raid1/md/dm) sitting on a partition."""

    def __init__(self, name: str, kind: str, *, path: str | None = None, vg: str = "", mounts: tuple[str, ...] = (), children: tuple["Holder", ...] = (), size: int = GiB, fstype: str = "ext4", sticky: bool = False) -> None:
        self.name, self.kind, self.vg, self.mounts, self.children, self.size, self.fstype = name, kind, vg, mounts, children, size, fstype
        self.sticky = sticky  # refuses to be deactivated (something keeps it open)
        self.path = path or (f"/dev/mapper/{name}" if kind in {"lvm", "crypt", "dm"} else f"/dev/{name}")
        self.active = True

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


class FakeDisk:
    def __init__(
        self,
        path: str,
        size: int,
        *,
        serial: str = "",
        wwn: str = "",
        model: str = "FAKE DISK",
        pttype: str = "gpt",
        kind: str = "disk",
        partitions: list[dict] | None = None,
        log_sec: int = 512,
        ro: bool = False,
        removable: bool = False,
    ) -> None:
        self.path, self.size, self.serial, self.wwn, self.model, self.pttype, self.kind = path, size, serial, wwn, model, pttype, kind
        self.log_sec, self.ro, self.removable = log_sec, ro, removable
        self.pmbr_boot = False  # GPT: the protective MBR entry carries the boot flag (parted disk_set pmbr_boot on)
        self.parts = [dict(p) for p in (partitions or [])]  # num, start, size, fstype, parttype, label, mounts, holders, flags
        for part in self.parts:
            part.setdefault("start", 1 * MiB)
            part.setdefault("holders", [])
            part.setdefault("mounts", [])

    def free_start(self) -> int:
        return max([1 * MiB] + [p["start"] + p["size"] for p in self.parts])

    def name(self) -> str:
        return self.path.split("/")[-1]

    def part_path(self, num: int) -> str:
        return f"{self.path}p{num}" if self.path[-1:].isdigit() else f"{self.path}{num}"


class StatefulRunner:
    def __init__(self, disks: list[FakeDisk], *, fail: dict[str, tuple[int, str]] | None = None) -> None:
        self.disks = {d.path: d for d in disks}
        self.commands: list[tuple[str, ...]] = []
        self.inputs: list[str | None] = []
        self.secrets: list[bool] = []
        self.options: list[dict] = []  # timeout/capture/root_identity of every call
        self.fail = fail or {}
        self.mapper: dict[str, dict] = {}
        self.mounts: dict[str, tuple[str, str]] = {}  # destination -> (source, options)
        self.swaps: set[str] = set()
        self.lvs: list[dict] = []
        self.pvs: list[tuple[str, str]] = []  # (pv device, vg) that exist outside the fake disks' own partitions
        self.pv_device = ""
        self.mdadm_examine: dict[str, str] = {}
        self.mdadm_detail: dict[str, str] = {}
        self.files: dict[str, dict] = {}  # swap files created by mkswap --file / btrfs mkswapfile
        self.uuids: dict[str, str] = {}
        self.hooks: list[tuple[str, Callable[["StatefulRunner", tuple[str, ...]], None]]] = []
        self._ids = iter(range(1, 10000))
        self._majmin: dict[str, str] = {}
        self.findmnt_sources: dict[str, str] = {}

    # -- helpers ----------------------------------------------------------------------
    def on(self, prefix: str, callback: Callable[["StatefulRunner", tuple[str, ...]], None]) -> None:
        """Run callback(runner, argv) after a command starting with prefix has succeeded (to change the world mid-install)."""
        self.hooks.append((prefix, callback))

    def majmin(self, path: str) -> str:
        return self._majmin.setdefault(path, f"{8 if path.startswith('/dev/sd') else 253}:{next(self._ids)}")

    def uuid_of(self, device: str) -> str:
        return self.uuids.setdefault(device, f"UUID-{device.strip('/').replace('/', '-')}")

    def part(self, dev: str):
        for d in self.disks.values():
            for p in d.parts:
                if d.part_path(p["num"]) == dev:
                    return d, p
        return None, None

    def all_holders(self):
        for d in self.disks.values():
            for p in d.parts:
                for h in p.get("holders", []):
                    yield from h.walk()

    def busy(self, part: dict) -> bool:
        return any(h.active for h in part.get("holders", []))

    def exists(self, device: str) -> bool:
        if device in self.disks or self.part(device)[1] is not None or any(h.path == device for h in self.all_holders()):
            return True
        return any(lv["path"] == device for lv in self.lvs) or (device.startswith("/dev/mapper/") and device.rsplit("/", 1)[1] in self.mapper)

    def _mounted_at(self, device: str) -> list[str]:
        return [dest for dest, (source, _) in self.mounts.items() if source == device]

    # -- lsblk -----------------------------------------------------------------------
    def _node(self, path: str, kind: str, size: int, **extra) -> dict:
        node = {"name": path.split("/")[-1], "path": path, "maj:min": self.majmin(path), "type": kind, "size": size, "serial": None, "wwn": None,
                "fstype": None, "mountpoints": [None], "children": []}
        node.update(extra)
        return node

    def _holder_json(self, h: Holder) -> dict:
        mounts = list(h.mounts) + self._mounted_at(h.path)
        node = self._node(h.path, h.kind, h.size, mountpoints=mounts or [None], fstype=h.fstype or None)
        node["name"] = h.name
        node["children"] = [self._holder_json(c) for c in h.children if c.active]
        return node

    def _lv_nodes(self, backing: str) -> list[dict]:
        out = []
        for lv in self.lvs:
            if lv["pv"] == backing:
                mounts = self._mounted_at(lv["path"]) + (["[SWAP]"] if lv["path"] in self.swaps else [])
                out.append(self._node(lv["path"], "lvm", GiB, fstype=lv.get("fstype"), mountpoints=mounts or [None]))
        return out

    def lsblk_json(self) -> str:
        roots = []
        for d in self.disks.values():
            kids = []
            for p in d.parts:
                dev = d.part_path(p["num"])
                mounts = self._mounted_at(dev) + list(p.get("mounts", [])) + (["[SWAP]"] if dev in self.swaps else [])
                children = [self._holder_json(h) for h in p.get("holders", []) if h.active] + self._lv_nodes(dev)
                for name, info in self.mapper.items():
                    if info["backing"] == dev:
                        mapper_path = f"/dev/mapper/{name}"
                        crypt = self._node(mapper_path, "crypt", p["size"], fstype=info.get("fstype"),
                                           mountpoints=self._mounted_at(mapper_path) + (["[SWAP]"] if mapper_path in self.swaps else []) or [None])
                        crypt["children"] = self._lv_nodes(mapper_path)
                        children.append(crypt)
                kids.append(self._node(dev, "part", p["size"], fstype=p.get("fstype"), parttype=p.get("parttype"), label=p.get("label"),
                                       mountpoints=mounts or [None], children=children))
            roots.append(self._node(d.path, d.kind, d.size, model=d.model, serial=d.serial or None, wwn=d.wwn or None, pttype=d.pttype or None,
                                    ro=d.ro, rm=d.removable, **{"log-sec": d.log_sec}, children=kids))
        return json.dumps({"blockdevices": roots})

    # -- the Runner protocol ----------------------------------------------------------------
    def run(self, argv, *, stdin=None, secret=False, env=None, cwd=None, timeout=None, capture=True, root_identity=False):
        args = validate_argv(argv)
        self.commands.append(args)
        self.inputs.append(stdin)
        self.secrets.append(secret)
        self.options.append({"timeout": timeout, "capture": capture, "root_identity": root_identity})
        text = " ".join(args)
        for prefix, (code, output) in self.fail.items():
            if text.startswith(prefix):
                raise RunnerError(args, code, output)
        result = self._dispatch(args)
        for prefix, callback in self.hooks:
            if text.startswith(prefix):
                callback(self, args)
        return result

    def run_cleanup(self, argv, **kwargs):
        return self.run(argv, **kwargs)

    def ok(self, args, out: str = "") -> CommandResult:
        return CommandResult(args, 0, out)

    def _dispatch(self, args: tuple[str, ...]) -> CommandResult:
        exe = args[0]
        handler = getattr(self, "_cmd_" + exe.replace(".", "_").replace("-", "_"), None)
        if handler is not None:
            return handler(args)
        if exe.startswith("mkfs."):
            return self._mkfs(args)
        return self.ok(args)

    def _cmd_lsblk(self, args):
        return self.ok(args, self.lsblk_json())

    def _cmd_findmnt(self, args):
        dest = args[args.index("--mountpoint") + 1]
        if dest in self.findmnt_sources:
            return self.ok(args, self.findmnt_sources[dest] + "\n")
        if dest in self.mounts:
            return self.ok(args, self.mounts[dest][0] + "\n")
        raise RunnerError(args, 1, "")

    def _cmd_udevadm(self, args):
        return self.ok(args)

    def _cmd_partprobe(self, args):
        return self.ok(args)

    def _cmd_sync(self, args):
        return self.ok(args)

    def _cmd_tar(self, args):
        return self.ok(args)

    def _cmd_blkid(self, args):
        dev = args[-1]
        if "--probe" in args:
            d, p = self.part(dev)
            fstype = p.get("fstype") if p is not None else (self.mapper.get(dev.rsplit("/", 1)[-1], {}).get("fstype") if dev.startswith("/dev/mapper/") else None)
            if not fstype:
                raise RunnerError(args, 2, "")
            return self.ok(args, fstype + "\n")
        if dev in self.files or self.exists(dev):
            return self.ok(args, self.uuid_of(dev) + "\n")
        raise RunnerError(args, 2, "")

    def _cmd_sgdisk(self, args):
        disk = self.disks[args[-1]]
        if args[1] == "--zap-all":
            disk.parts.clear()
            disk.pttype = "gpt"
            disk.pmbr_boot = False  # a fresh protective MBR: sgdisk writes its entry with status 0x00 (mbr.cc: "unbootable")
            return self.ok(args, "GPT data structures destroyed!")
        m = re.match(r"--new=(\d+):0:(.+)", args[1])
        if m:
            num, size = int(m.group(1)), m.group(2)
            start = disk.free_start()
            avail = disk.size - 1 * MiB - start  # leave 1 MiB for the backup GPT
            length = avail if size == "0" else int(size.removeprefix("+").removesuffix("MiB")) * MiB
            if length > avail or length <= 0:
                raise RunnerError(args, 4, f"Could not create partition {num} from {start // 512} to {(start + length) // 512}\nError encountered; not saving changes.")
            code = re.search(r"--typecode=\d+:(\w+)", " ".join(args))
            guid = {"EF02": BIOS_BOOT, "EF00": ESP, "8200": SWAP}.get(code.group(1) if code else "", LINUX)
            disk.parts.append({"num": num, "start": start, "size": length, "fstype": None, "parttype": guid, "mounts": [], "holders": []})
        return self.ok(args)

    def _parted_print(self, disk: FakeDisk) -> str:
        """`parted --script --machine DISK print`: one line for the disk, one per partition (flags last, joined by ", ")."""
        lines = ["BYT;", f"{disk.path}:{disk.size}B:file:{disk.log_sec}:{disk.log_sec}:{'msdos' if disk.pttype == 'dos' else disk.pttype}::{'pmbr_boot' if disk.pmbr_boot else ''};"]
        for part in sorted(disk.parts, key=lambda p: p["num"]):
            end = part["start"] + part["size"] - 1
            lines.append(f"{part['num']}:{part['start']}B:{end}B:{part['size']}B:{part.get('fstype') or ''}::{', '.join(sorted(part.get('flags', ())))};")
        return "\n".join(lines) + "\n"

    def _cmd_parted(self, args):
        if "--machine" in args:
            return self.ok(args, self._parted_print(self.disks[args[-2]]))
        disk = self.disks[args[2]]
        if args[3] == "mklabel":
            disk.parts.clear()
            disk.pttype = "dos"
            disk.pmbr_boot = False
        elif args[3] == "set":  # parted DISK set NUMBER FLAG on|off
            part = next((p for p in disk.parts if p["num"] == int(args[4])), None)
            if part is None:
                raise RunnerError(args, 1, "Error: Partition doesn't exist.")
            flags = part.setdefault("flags", set())
            if args[6] == "on":
                flags.add(args[5])
            else:
                flags.discard(args[5])
        elif args[3] == "disk_set":  # parted DISK disk_set FLAG on|off
            if disk.pttype != "gpt" or args[4] != "pmbr_boot":
                raise RunnerError(args, 1, f"parted: invalid token: {args[4]}")
            disk.pmbr_boot = args[5] == "on"
        elif args[3] == "mkpart":
            def mib(x: str) -> int:
                return disk.size if x == "100%" else int(x.removesuffix("MiB")) * MiB

            start, end = mib(args[6]), mib(args[7])
            if disk.pttype == "dos" and end > (2**32 - 1) * disk.log_sec:
                raise RunnerError(args, 1, "Error: partition length exceeds the msdos-partition-table-imposed maximum of 4294967295 sectors")
            if end > disk.size or end <= start:
                raise RunnerError(args, 1, "Error: unable to satisfy all constraints on the partition.")
            disk.parts.append({"num": len(disk.parts) + 1, "start": start, "size": end - start, "fstype": None, "parttype": "0x83", "mounts": [], "holders": []})
        return self.ok(args)

    def _cmd_wipefs(self, args):
        dev = args[-1]
        d, p = self.part(dev)
        if p is not None:
            if self.busy(p):
                raise RunnerError(args, 1, f"wipefs: error: {dev}: probing initialization failed: Device or resource busy")
            if self._mounted_at(dev) and "--force" not in args:
                raise RunnerError(args, 1, f"wipefs: error: {dev}: probing initialization failed: Device or resource busy")
            p["fstype"] = None
            p["holders"] = []
        elif dev in self.disks:
            if "--force" not in args and any(self.busy(q) for q in self.disks[dev].parts):
                raise RunnerError(args, 1, "busy")
            self.disks[dev].pttype = ""
        return self.ok(args)

    def _mkfs(self, args):
        dev = args[-1]
        d, p = self.part(dev)
        if p is not None and self.busy(p):
            raise RunnerError(args, 1, f"{dev} is apparently in use by the system; will not make a filesystem here!")
        fs = args[0].split(".", 1)[1]
        self._set_fs(dev, fs)
        self.uuids[dev] = f"UUID-{dev.strip('/').replace('/', '-')}-{next(self._ids)}"
        return self.ok(args)

    def _set_fs(self, dev: str, fs: str):
        d, p = self.part(dev)
        if p is not None:
            p["fstype"] = fs
        elif dev.startswith("/dev/mapper/") and dev.rsplit("/", 1)[1] in self.mapper:
            self.mapper[dev.rsplit("/", 1)[1]]["fstype"] = fs
        else:
            for lv in self.lvs:
                if lv["path"] == dev:
                    lv["fstype"] = fs

    def _cmd_mkswap(self, args):
        if "--file" in args:
            path = args[-1]
            self.files[path] = {"kind": "swapfile"}
            self.uuids[path] = f"SWAPFILE-{next(self._ids)}"
            return self.ok(args)
        dev = args[-1]
        d, p = self.part(dev)
        if p is not None and self.busy(p):
            raise RunnerError(args, 1, f"{dev} is apparently in use by the system; will not make a filesystem here!")
        self._set_fs(dev, "swap")
        return self.ok(args)

    def _cmd_btrfs(self, args):
        if args[1:3] == ("filesystem", "mkswapfile"):
            self.files[args[-1]] = {"kind": "swapfile"}
            self.uuids[args[-1]] = f"SWAPFILE-{next(self._ids)}"
        return self.ok(args)

    def _cmd_swapon(self, args):
        if "--show=NAME" in args:
            return self.ok(args, "\n".join(sorted(self.swaps)) + ("\n" if self.swaps else ""))
        self.swaps.add(args[-1])
        return self.ok(args)

    def _cmd_swapoff(self, args):
        self.swaps.discard(args[-1])
        return self.ok(args)

    def _cmd_mount(self, args):
        options = args[args.index("-o") + 1] if "-o" in args else ""
        device, dest = args[-2], args[-1]
        if not self.exists(device):
            raise RunnerError(args, 32, f"mount: {dest}: special device {device} does not exist.")
        self.mounts[dest] = (device, options)
        return self.ok(args)

    def _cmd_umount(self, args):
        dest = args[-1]
        self.mounts = {m: v for m, v in self.mounts.items() if not (m == dest or m.startswith(dest + "/"))}
        return self.ok(args)

    def _cmd_lvs(self, args):
        for h in self.all_holders():
            if h.path == args[-1] and h.kind == "lvm":
                return self.ok(args, f"  {h.vg}\n")
        raise RunnerError(args, 5, "Failed to find logical volume")

    def _cmd_pvs(self, args):
        if "pv_name,vg_name" in args:
            lines = [f"  {dev}:{vg}" for dev, vg in self.pvs]
            lines += [f"  {lv['pv']}:{lv['vg']}" for lv in self.lvs if lv["pv"]]
            return self.ok(args, "\n".join(dict.fromkeys(lines)) + ("\n" if lines else ""))
        return self.ok(args, "vg-uuid-1\n")

    def _cmd_vgchange(self, args):
        if args[1] == "-an":
            for h in self.all_holders():
                if h.kind == "lvm" and h.vg == args[2] and not h.sticky:
                    h.active = False
        return self.ok(args)

    def _cmd_cryptsetup(self, args):
        sub = args[1]
        if sub == "close":
            for h in self.all_holders():
                if h.kind == "crypt" and h.name == args[2] and not h.sticky:
                    if any(child.active for child in h.children):
                        raise RunnerError(args, 5, f"Device {args[2]} is still in use.")
                    h.active = False
            self.mapper.pop(args[2], None)
        elif sub == "luksFormat":
            d, p = self.part(args[-1])
            if p is not None:
                p["fstype"] = "crypto_LUKS"
        elif sub == "open":
            self.mapper[args[-1]] = {"backing": args[-2]}
        elif sub == "status":
            name = args[2]
            if name not in self.mapper:
                raise RunnerError(args, 4, "inactive")
            return self.ok(args, f"/dev/mapper/{name} is active.\n  device:  {self.mapper[name]['backing']}\n")
        return self.ok(args)

    def _cmd_mdadm(self, args):
        if args[1] == "--stop":
            for h in self.all_holders():
                if h.path == args[2] and not h.sticky:
                    h.active = False
        elif args[1] == "--examine":
            return self.ok(args, self.mdadm_examine.get(args[2], ""))
        elif args[1] == "--detail":
            return self.ok(args, self.mdadm_detail.get(args[-1], ""))
        elif args[1] == "--assemble":
            for h in self.all_holders():
                if h.path == args[3]:
                    h.active = True
        return self.ok(args)

    def _cmd_dmsetup(self, args):
        if args[1] == "remove":
            for h in self.all_holders():
                if h.name == args[-1] and not h.sticky:
                    h.active = False
        return self.ok(args)

    def _cmd_pvcreate(self, args):
        self.pv_device = args[-1]
        return self.ok(args)

    def _cmd_vgcreate(self, args):
        return self.ok(args)

    def _cmd_lvcreate(self, args):
        name, vg = args[args.index("--name") + 1], args[-1]
        self.lvs.append({"vg": vg, "name": name, "path": f"/dev/{vg}/{name}", "pv": self.pv_device})
        return self.ok(args)


def commands_text(runner: StatefulRunner, skip: tuple[str, ...] = ("lsblk", "findmnt", "blkid")) -> list[str]:
    return [" ".join(c) for c in runner.commands if c[0] not in skip]


def root_partition_size(runner: StatefulRunner, disk: str) -> int:
    """Size of the last partition on a fake disk (the root the partitioner left)."""
    return runner.disks[disk].parts[-1]["size"]


def make_manager(runner, tmp_path, **kwargs):
    """A StorageManager whose root, /dev, /proc and by-path directory live in tmp_path."""
    from gentoo_installerd.storage import StorageManager

    for name in ("target", "dev/mapper", "proc", "by-path"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    options = {
        "by_path_dir": tmp_path / "by-path",
        "cmdline_path": tmp_path / "cmdline",
        "proc_root": tmp_path / "proc",
        "dev_root": tmp_path / "dev",
        "sleep": lambda seconds: None,
        "kill": lambda pid, sig: None,
    }
    options.update(kwargs)
    return StorageManager(runner, tmp_path / "target", **options)


def link_by_path(tmp_path, name: str, device: str) -> None:
    """Create by-path/<name> -> device inside tmp_path (a dangling link to the fake device path is enough)."""
    (tmp_path / "by-path").mkdir(exist_ok=True)
    (tmp_path / "by-path" / name).symlink_to(device)
