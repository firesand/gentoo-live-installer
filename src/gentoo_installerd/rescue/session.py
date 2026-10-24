"""A rescue session: one mounted system, the ledger of everything acquired for it, and its release.

Lifecycle (docs/contracts.md: nothing is written until the user confirms a repair):

    mount_ro()  ->  MOUNTED_RO  --enter_write steps (part of a confirmed plan)-->  MOUNTED_RW  ->  release()

Every acquisition is recorded in the ledger and persisted (``<state_dir>/sessions/<sid>.json``) so a crash can be undone
by the next start. Mount points inside the target are resolved with ``RootFS`` immediately before every mount (review R2)
and the devices outside the selected stack are flagged read-only in the kernel (review R4).
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from gentoo_installerd.runner import Runner

from . import cmdline as cmdline_mod
from .audit import AuditLog
from .devices import DeviceGraph, DeviceNode
from .errors import RescueError
from .fsprobe import mount_readonly, mount_type
from .hostenv import Host
from .ledger import Ledger, LedgerEntry, write_json_atomic
from .mountplan import TIER_BOOT, TIER_ROOT, TIER_SYSTEM, MountItem, MountPlan, RootSpec
from .paths import RootFS
from .plan import PlanStore, Step
from .runner import RescueRunner
from .safety import DeviceAllowList, PolicyContext, check_mount_destination

STATE_VERSION = 1
LVM_NAME_RE = re.compile(r"^((?:[^-]|--)+)-((?:[^-]|--)+)$")
FUSER_LINE_RE = re.compile(r"^\s*(?:\S+:\s+)?(\S+)\s+(\d+)\s+(\S+)\s+(.+?)\s*$")
VIRTUAL_NAMES = ("proc", "sys", "dev", "run")
PUBLIC_RESOLVERS = ("1.1.1.1", "9.9.9.9")
RESOLV_SOURCES = ("/etc/resolv.conf", "/run/NetworkManager/resolv.conf", "/run/systemd/resolve/resolv.conf")


@dataclass
class SessionEnv:
    base_dir: Path
    state_dir: Path
    audit_dir: Path
    probe_root: Path
    host: Host
    make_runner: Callable[[threading.Event | None], Runner]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    pid: int = field(default_factory=os.getpid)


@dataclass
class ReleaseResult:
    released: list[str] = field(default_factory=list)
    leftovers: list[str] = field(default_factory=list)
    busy_holders: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.leftovers

    def to_dict(self) -> dict[str, Any]:
        return {"released": self.released, "leftovers": self.leftovers, "busy_holders": self.busy_holders, "errors": self.errors}


def parse_fuser(text: str) -> list[dict[str, Any]]:
    holders = []
    for line in text.splitlines():
        match = FUSER_LINE_RE.match(line)
        if match and match.group(1) != "USER":
            holders.append({"user": match.group(1), "pid": int(match.group(2)), "access": match.group(3), "command": match.group(4)})
    return holders


class RescueSession:
    def __init__(self, env: SessionEnv, sid: str, plan: MountPlan, candidate: dict[str, Any], graph: DeviceGraph | None, *, allow_write: bool = True, allow_degraded: bool = False,
                 depends: Iterable[int] = (), pool_devices: Iterable[str] = (), readonly_md: Iterable[str] = ()) -> None:
        self.env = env
        self.sid = sid
        self.root = str(env.base_dir / sid)
        self.plan = plan
        self.candidate = candidate
        self.allow_write = allow_write
        self.allow_degraded = allow_degraded
        self.depends = list(depends)
        self.readonly_md = list(readonly_md)
        self.created = env.clock()
        self.generation = 0
        self.state = "init"
        self.warnings: list[str] = list(plan.warnings)
        self.failed_mounts: list[dict[str, str]] = []
        self.shells: list[dict[str, Any]] = []
        self.plans = PlanStore(env.clock)
        self.cancel = threading.Event()  # set only to kill a running command (forced cancel)
        self.busy: dict[str, str] | None = None
        self.op_lock = threading.Lock()
        self.ledger = Ledger(self.persist)
        self.state_path = env.state_dir / "sessions" / f"{sid}.json"
        self.cmdline_path = env.state_dir / "sessions" / f"{sid}.cmdline"
        self.resolv_path = env.state_dir / "sessions" / f"{sid}.resolv.conf"
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.created))
        self.audit = AuditLog(env.audit_dir / "audit" / f"{stamp}-{sid}.jsonl", sid, clock=env.clock)
        self.ctx = self._make_context(graph, pool_devices)
        self.esp_mountpoints = tuple(item.mountpoint for item in plan.selected if item.tier == TIER_BOOT)
        self.runner = RescueRunner(env.make_runner(self.cancel), self.ctx, self.audit, esp_mountpoints=self.esp_mountpoints)

    # ---- construction helpers --------------------------------------------------------------------
    def _make_context(self, graph: DeviceGraph | None, pool_devices: Iterable[str]) -> PolicyContext:
        devices = DeviceAllowList()
        devices.add(*self.plan.devices(), *pool_devices)
        for path in self.plan.devices():
            try:
                node = graph.find(path) if graph is not None else None
            except LookupError:
                node = None
            if node is None or graph is None:
                continue
            for disk in graph.parent_disks(node):
                devices.add_disks(disk.path)
            for member in graph.stack_of(node):
                devices.add(member.path)
        for node in graph or ():
            if node.live_medium:
                devices.mark_live(node.path)
        ctx = PolicyContext(session_root=self.root, session_id=self.sid, session_base=str(self.env.base_dir), probe_root=str(self.env.probe_root), state_dir=str(self.env.state_dir),
                            devices=devices, allow_write=self.allow_write)
        ctx.bind_sources.add(str(self.cmdline_path))
        ctx.bind_sources.add(str(self.resolv_path))
        for source in RESOLV_SOURCES:
            ctx.bind_sources.add(source)
            ctx.bind_sources.add(os.path.realpath(source))  # host-path: the live system's own resolver file
        return ctx

    def fs(self, *, writable: bool = False) -> RootFS:
        return RootFS(self.root, writable=writable)

    @property
    def mode(self) -> str:
        entry = self.ledger.find("mount", mountpoint="/")
        return "rw" if entry and entry.data.get("rw") else "ro"

    @property
    def mounted(self) -> bool:
        return self.state in {"mounted_ro", "mounted_rw"}

    def bump(self) -> None:
        self.generation += 1
        self.state = "mounted_rw" if self.mode == "rw" else "mounted_ro"
        self.persist()

    # ---- persistence --------------------------------------------------------------------------------
    def to_state(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION, "sid": self.sid, "created": self.created, "pid": self.env.pid, "mode": self.mode, "allow_write": self.allow_write, "state": self.state,
            "generation": self.generation, "root": {"candidate": self.candidate.get("id", ""), "device": self.plan.root.device, "fstype": self.plan.root.fstype, "uuid": self.plan.root.uuid,
                                                      "rootflags": self.plan.root.rootflags, "path": self.root, "os": self.candidate.get("os", {}), "label": self.candidate.get("label", "")},
            "items": [item.to_dict() | {"device": item.device, "extra_devices": item.extra_devices} for item in self.plan.items], "depends": self.depends,
            "ledger": self.ledger.to_state(), "audit": str(self.audit.path or ""), "cmdline": str(self.cmdline_path), "resolv": str(self.resolv_path),
        }

    def persist(self) -> None:
        if self.state in {"closed"}:
            return
        try:
            write_json_atomic(self.state_path, self.to_state())
        except OSError:
            pass  # the state file is for crash recovery; a full /run must not stop a repair

    def forget(self) -> None:
        for path in (self.state_path, self.cmdline_path, self.resolv_path, self.state_path.with_suffix(".mdadm.conf")):
            try:
                path.unlink()
            except OSError:
                pass

    # ---- mounting (read-only phase) -----------------------------------------------------------------------
    def mount_ro(self, graph: DeviceGraph, verify: Callable[[RootFS], None] | None = None, emit: Callable[..., None] | None = None) -> None:
        emit = emit or (lambda *_a, **_k: None)
        os.makedirs(self.root, mode=0o700, exist_ok=False)
        os.makedirs(self.env.state_dir / "sessions", mode=0o700, exist_ok=True)
        self.state = "mounting"
        self.audit.record("mount", message="session started", root=self.plan.root.device, items=[item.mountpoint for item in self.plan.selected], sync=True)
        try:
            emit("stage", "Protecting the other disks", stage="protect")
            self._protect_disks(graph)
            self._flag_selected(graph)
            emit("stage", "Mounting the system read-only", stage="mount")
            self._mount_root()
            with self.fs() as fs:
                if verify:
                    verify(fs)
            for item in self.plan.selected:
                self._mount_item(item, graph, emit)
            self.state = "mounted_ro"
            self.bump()
        except BaseException:
            self.release(force=True)
            raise

    def _flag_ro(self, device: str, purpose: str) -> None:
        was_ro = self.runner.run(("blockdev", "--getro", device), timeout=10).stdout.strip() == "1"
        if not was_ro:
            self.ctx.devices.protect(device)
            self.runner.run(("blockdev", "--setro", device), timeout=10)
        self.ledger.add("blockro", device=device, was_ro=was_ro, ro_now=True, purpose=purpose)

    def _protect_disks(self, graph: DeviceGraph) -> None:
        """BLKROSET every whole disk the user did not select (review R4); hot-plug media and disks in use stay writable."""
        selected = [graph.find(path) for path in self.plan.devices()]
        stack = {member.key for node in selected for member in graph.stack_of(node)}
        selected_disks = {disk.key for node in selected for disk in graph.parent_disks(node)}
        for disk in graph.disks():
            if disk.key in selected_disks or disk.live_medium or disk.hotplug or disk.rm or disk.ro:
                continue
            if any(item.mountpoints for item in [disk, *graph.descendants(disk)]):
                continue  # in use by the live desktop: flags on a mounted disk only cause warnings
            self._flag_ro(disk.path, "protect")
        mounted_nodes = {node.key for node in graph if node.mountpoints}
        for sibling in graph.siblings_on_disks([graph.get(key) for key in stack if graph.get(key)]):  # type: ignore[misc]
            if sibling.ro or sibling.live_medium or sibling.key in mounted_nodes:
                continue
            self._flag_ro(sibling.path, "protect")

    def _flag_selected(self, graph: DeviceGraph) -> None:
        for path in self.plan.devices():
            self.ctx.devices.add(path)
            self._flag_ro(path, "session")

    def _mount_root(self) -> None:
        spec = self.plan.root
        rung = mount_readonly(self.runner, spec.device, spec.fstype, self.root, subvolid=spec.subvolid, noexec=not self.allow_write, allow_degraded=self.allow_degraded,
                              available=self.env.host.filesystems() or None)
        self.ledger.add("mount", path=self.root, mountpoint="/", device=spec.device, fs=spec.fstype, tier=TIER_ROOT, rw=False, rung=rung, recursive=False)
        if rung:
            self.warnings.append("The root file system needed recovery options to mount; some data may be unreadable. Run a file system check.")

    def _mount_item(self, item: MountItem, graph: DeviceGraph, emit: Callable[..., None]) -> None:
        if item.needs_mkdir:
            self.failed_mounts.append({"mountpoint": item.mountpoint, "reason": "the directory does not exist in the read-only system; it is created when repairs are enabled"})
            return
        try:
            with self.fs() as fs:
                destination = fs.mount_destination(item.mountpoint)
            check_mount_destination(destination, self.root)
            rung = mount_readonly(self.runner, item.device, item.fstype, destination, extra=item.options, noexec=not self.allow_write, allow_degraded=self.allow_degraded,
                                  available=self.env.host.filesystems() or None)
        except (RescueError, OSError) as exc:
            reason = exc.message if isinstance(exc, RescueError) else str(exc)
            self.failed_mounts.append({"mountpoint": item.mountpoint, "device": item.device, "reason": reason})
            self.warnings.append(f"{item.mountpoint} could not be mounted from {item.device}: {reason}")
            return
        self.ledger.add("mount", path=destination, mountpoint=item.mountpoint, device=item.device, fs=item.fstype, tier=item.tier, rw=False, rung=rung, recursive=False)
        emit("progress", f"Mounted {item.mountpoint}", stage="mount", mountpoint=item.mountpoint)

    # ---- the steps that switch to read-write and prepare a chroot ----------------------------------------------
    def _mount_entries(self) -> list[LedgerEntry]:
        return [entry for entry in self.ledger.entries if entry.kind in {"mount", "bind", "cmdline"}]

    def virtual_entries(self) -> list[LedgerEntry]:
        return [entry for entry in self.ledger.entries if entry.kind in {"bind", "cmdline"} or entry.data.get("virtual")]

    def has_virtual_mounts(self) -> bool:
        return any(entry.data.get("virtual") for entry in self.ledger.entries)

    def _umount_step(self, entry: LedgerEntry, step_id: str, group: str) -> Step:
        recursive = bool(entry.data.get("recursive"))
        argv = ("umount", "-R", entry.data["path"]) if recursive else ("umount", entry.data["path"])
        return Step(step_id, "host", " ".join(argv), argv, timeout=60, group=group, effect={"kind": "mount_remove", "path": entry.data["path"]})

    def virtual_steps(self, group: str = "virtual") -> list[Step]:
        """Mount proc, sys, dev and a fresh /run, bind the resolver and the synthesized /proc/cmdline (review R2, R3)."""
        r = self.root
        steps: list[Step] = []

        def host(step_id: str, argv: tuple[str, ...], mountpoint: str = "", effect: dict[str, Any] | None = None, writes: tuple[str, ...] = (), display: str = "") -> None:
            steps.append(Step(f"{group}-{step_id}", "host", display or " ".join(argv), argv, writes=writes, timeout=60, mountpoint=mountpoint, effect=effect, group=group))

        host("proc", ("mount", "-t", "proc", "proc", f"{r}/proc"), "/proc", {"kind": "mount_add", "path": f"{r}/proc", "mountpoint": "/proc", "fs": "proc", "virtual": True, "virtual_name": "proc"})
        if self.env.host.uefi() and not self._efivars_mounted():
            host("efivars", ("mount", "-t", "efivarfs", "efivarfs", "/sys/firmware/efi/efivars"), display="mount -t efivarfs efivarfs /sys/firmware/efi/efivars  (live system, once)")
        host("sys", ("mount", "--rbind", "/sys", f"{r}/sys"), "/sys", {"kind": "mount_add", "path": f"{r}/sys", "mountpoint": "/sys", "fs": "rbind", "virtual": True, "recursive": True, "virtual_name": "sys"})
        host("sys-slave", ("mount", "--make-rslave", f"{r}/sys"), "/sys")
        host("dev", ("mount", "--rbind", "/dev", f"{r}/dev"), "/dev", {"kind": "mount_add", "path": f"{r}/dev", "mountpoint": "/dev", "fs": "rbind", "virtual": True, "recursive": True, "virtual_name": "dev"})
        host("dev-slave", ("mount", "--make-rslave", f"{r}/dev"), "/dev")
        host("run", ("mount", "-t", "tmpfs", "-o", "mode=0755,nosuid,nodev", "tmpfs", f"{r}/run"), "/run",
             {"kind": "mount_add", "path": f"{r}/run", "mountpoint": "/run", "fs": "tmpfs", "virtual": True, "virtual_name": "run"})
        steps.append(Step(f"{group}-run-udev-dir", "file", "mkdir /run/udev (in the fresh /run)", file_op={"op": "mkdir", "path": "/run/udev", "mode": 0o755, "parents": True}, group=group, writes=("/run/udev",)))
        host("run-udev", ("mount", "--bind", "/run/udev", f"{r}/run/udev"), "/run/udev", {"kind": "mount_add", "path": f"{r}/run/udev", "mountpoint": "/run/udev", "fs": "bind", "virtual": True, "virtual_name": "run-udev"})
        host("run-udev-ro", ("mount", "-o", "remount,bind,ro", f"{r}/run/udev"), "/run/udev")
        steps.append(Step(f"{group}-resolv", "internal", "Bind the live system's DNS settings onto the system's resolv.conf (the system's own file is not modified)", handler="resolv-bind", group=group))
        steps.append(Step(f"{group}-cmdline", "internal", "Bind a synthesized kernel command line over /proc/cmdline so the live command line cannot reach boot entries", handler="cmdline-bind", group=group))
        return steps

    def _efivars_mounted(self) -> bool:
        result = self.runner.execute(("findmnt", "-n", "-o", "OPTIONS", "/sys/firmware/efi/efivars"), timeout=10)
        return result.returncode == 0 and bool(result.stdout.strip())

    def enter_write_steps(self, *, with_virtual: bool = True) -> list[Step]:
        """The exact commands that turn this read-only mount into a read-write one (shown to the user with the repair)."""
        if not self.allow_write:
            raise RescueError("needs_write", "This session was opened for inspection only; mount the system again with repairs allowed")
        group = "enter-write"
        steps: list[Step] = [Step("w-idle", "internal", "Check that no terminal is open and no other step is running", handler="check-idle", group=group)]
        for index, entry in enumerate(reversed(self._mount_entries())):
            steps.append(self._umount_step(entry, f"w-umount-{index}", group))
        rw_devices = [self.plan.root.device] + [device for item in self.plan.selected if item.tier <= TIER_SYSTEM for device in [item.device, *item.extra_devices]]
        for index, device in enumerate(dict.fromkeys(rw_devices)):
            steps.append(Step(f"w-setrw-{index}", "host", f"blockdev --setrw {device}", ("blockdev", "--setrw", device), timeout=20, group=group,
                              effect={"kind": "blockro", "device": device, "ro_now": False}))
        for index, device in enumerate(self.readonly_md):
            steps.append(Step(f"w-mdrw-{index}", "host", f"mdadm --readwrite {device}", ("mdadm", "--readwrite", device), timeout=30, group=group, ok_rc=(0, 1)))
        steps.extend(self.mount_steps(rw=True, group=group))
        if with_virtual:
            steps.extend(self.virtual_steps(group))
        return steps

    def mount_steps(self, *, rw: bool, group: str, spec: RootSpec | None = None, items: Iterable[MountItem] | None = None, root: bool = True) -> list[Step]:
        """Mount the root and the selected items; tiers 1-3 follow ``rw``, data mounts (tier 4) always stay read-only."""
        spec = spec or self.plan.root
        steps: list[Step] = []
        harden = ["nodev", "nosuid"] + ([] if self.allow_write else ["noexec"])
        if root:
            flag = "rw" if rw else "ro"
            options = [flag, *([f"subvolid={spec.subvolid}"] if spec.subvolid is not None else []), *self._skip_options(spec.fstype, rw), *harden]
            argv = ("mount", "-t", mount_type(spec.fstype), "-o", ",".join(options), spec.device, self.root)
            steps.append(Step(f"{group}-mount-root", "host", " ".join(argv), argv, timeout=120, group=group, mountpoint="/", atomic=False,
                              effect={"kind": "mount_add", "path": self.root, "mountpoint": "/", "device": spec.device, "fs": spec.fstype, "tier": TIER_ROOT, "rw": rw}))
        chosen = list(items) if items is not None else list(self.plan.selected)
        for index, item in enumerate(chosen):
            item_rw = rw and item.tier <= TIER_SYSTEM
            if item_rw:
                steps.extend(self._postponed_mkdir(item, group, index))
            destination = self.root + item.mountpoint
            options = ["rw" if item_rw else "ro", *item.options, *self._skip_options(item.fstype, item_rw), *harden]
            argv = ("mount", "-t", mount_type(item.fstype), "-o", ",".join(options), item.device, destination)
            steps.append(Step(f"{group}-mount-{index}", "host", " ".join(argv), argv, timeout=120, group=group, mountpoint=item.mountpoint, atomic=False,
                              effect={"kind": "mount_add", "path": destination, "mountpoint": item.mountpoint, "device": item.device, "fs": item.fstype, "tier": item.tier, "rw": item_rw}))
        return steps

    @staticmethod
    def _skip_options(fstype: str, rw: bool) -> list[str]:
        if rw:
            return []
        from .fsprobe import RECOVERY_SKIP

        return list(RECOVERY_SKIP.get(fstype, ()))

    def _postponed_mkdir(self, item: MountItem, group: str, index: int) -> list[Step]:
        if not item.needs_mkdir:
            return []
        return [Step(f"{group}-mkdir-{index}", "file", f"mkdir {item.mountpoint}", file_op={"op": "mkdir", "path": item.mountpoint, "mode": 0o755, "parents": True}, group=group, writes=(item.mountpoint,))]

    # ---- executing one step -----------------------------------------------------------------------------------------
    def run_step(self, step: Step, runner: RescueRunner, secrets: dict[str, str] | None = None) -> tuple[int, str]:
        """Run a step with every guard; returns (exit code, output). File and internal steps return 0 or raise."""
        secrets = secrets or {}
        if step.scope == "host":
            self._revalidate(step)
            stdin, secret = self._stdin(step, secrets)
            result = runner.execute(step.argv, stdin=stdin, secret=secret, timeout=step.timeout, env=step.env or None)
            if result.returncode in step.ok_rc and step.effect:
                self.apply_effect(step.effect)
            return result.returncode, result.stdout
        if step.scope == "chroot":
            stdin, secret = self._stdin(step, secrets)
            result = runner.chroot(step.argv, tools=step.tools, extra_env=step.env or None, stdin=stdin, secret=secret, timeout=step.timeout, ok_rc=step.ok_rc, check=False)
            return result.returncode, result.stdout
        if step.scope == "file":
            return 0, self._file_op(step, runner)
        if step.scope == "internal":
            return 0, self._internal(step, runner)
        raise RescueError("internal", f"Unknown step scope {step.scope}")

    @staticmethod
    def _stdin(step: Step, secrets: dict[str, str]) -> tuple[str | None, bool]:
        if not step.stdin:
            return None, False
        kind, _, value = step.stdin.partition(":")
        if kind == "secret":
            if value not in secrets:
                raise RescueError("needs_confirmation", f"The {value} is needed to run this plan", missing=value)
            return secrets[value], True
        return value, False

    def _revalidate(self, step: Step) -> None:
        """Re-resolve the mount point right before the mount and compare it with the plan (review R2)."""
        command = step.argv[0]
        if command == "umount":
            if os.path.exists(step.argv[-1]):
                check_mount_destination(step.argv[-1], self.root)
            return
        if command != "mount" or not step.mountpoint:
            return
        destination = step.argv[-1]
        mountpoint = step.mountpoint
        with self.fs() as fs:
            if mountpoint == "/":
                resolved = self.root
            elif mountpoint in {"/proc", "/sys", "/dev", "/run"}:
                resolved = fs.virtual_destination(mountpoint[1:])
            elif mountpoint == "/proc/cmdline":
                resolved = fs.file_destination(mountpoint)
            elif mountpoint.startswith(("/run/", "/proc/", "/sys/", "/dev/")):
                resolved = fs.mount_destination(mountpoint, reserved=())
            else:
                resolved = fs.mount_destination(mountpoint)
        if resolved != destination:
            raise RescueError("plan_stale", f"The mount point {mountpoint} no longer resolves to {destination}", mountpoint=mountpoint)
        check_mount_destination(destination, self.root, file_ok=mountpoint == "/proc/cmdline")

    def apply_effect(self, effect: dict[str, Any]) -> None:
        kind = effect["kind"]
        if kind == "mount_add":
            data = {key: value for key, value in effect.items() if key != "kind"}
            existing = self.ledger.find("mount", path=effect["path"]) or self.ledger.find("bind", path=effect["path"])
            if existing:
                self.ledger.remove(existing)
            self.ledger.add("bind" if effect.get("fs") in {"bind", "rbind"} else "mount", **data)
        elif kind == "mount_remove":
            for entry in list(self.ledger.entries):
                if entry.kind in {"mount", "bind", "cmdline"} and entry.data.get("path") == effect["path"]:
                    self.ledger.remove(entry)
        elif kind == "blockro":
            for entry in self.ledger.of_kind("blockro"):
                if entry.data["device"] == effect["device"]:
                    entry.data["ro_now"] = effect["ro_now"]
            self.persist()
        self.bump()

    # ---- file and internal steps -----------------------------------------------------------------------------------------
    def _file_op(self, step: Step, runner: RescueRunner) -> str:
        op = dict(step.file_op or {})
        kind = op.get("op")
        virtual = step.group in {"virtual", "enter-write"} and op.get("path", "").startswith("/run")
        if self.mode != "rw" and not virtual:
            raise RescueError("needs_write", "Repairs are not enabled for this session")
        with self.fs(writable=True) as fs:
            if kind == "write":
                suffix = ".gentoo-rescue-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.env.clock())) if op.get("backup") else ""
                result = fs.write_atomic(op["path"], op["content"].encode(), mode=op.get("mode"), uid=op.get("uid"), gid=op.get("gid"), expect_sha256=op.get("expect_sha256"),
                                         backup_suffix=suffix, create=op.get("create", True), parents=bool(op.get("parents")))
                self.audit.record("file", op="write", path=result.path, before=result.before_sha256, after=result.after_sha256, backup=result.backup, plan=runner.scope.plan, step=step.id, sync=True)
                return f"wrote {result.path}"
            if kind == "touch":
                path = fs.touch(op["path"], op.get("mode", 0o644))
                self.audit.record("file", op="touch", path=path, plan=runner.scope.plan, step=step.id)
                return f"touched {path}"
            if kind == "mkdir":
                path = fs.mkdir(op["path"], op.get("mode", 0o755), parents=bool(op.get("parents")))
                self.audit.record("file", op="mkdir", path=path, plan=runner.scope.plan, step=step.id)
                return f"created {path}"
            if kind == "remove":
                removed = fs.remove(op["path"])
                self.audit.record("file", op="remove", path=op["path"], removed=removed, plan=runner.scope.plan, step=step.id)
                return f"removed {op['path']}"
        raise RescueError("internal", f"Unknown file operation {kind}")

    def _internal(self, step: Step, runner: RescueRunner) -> str:
        if step.handler == "check-idle":
            if self.shells:
                raise RescueError("busy", "A terminal is open in this system; close it before enabling repairs", shells=len(self.shells))
            return "idle"
        if step.handler == "resolv-bind":
            return self._bind_resolv(runner)
        if step.handler == "cmdline-bind":
            return self._bind_cmdline(runner)
        raise RescueError("internal", f"Unknown internal step {step.handler}")

    def _resolv_source(self) -> tuple[str, str]:
        """The live system's resolver file, or a synthesized one with public resolvers."""
        for candidate in RESOLV_SOURCES:
            real = self.env.host.path(candidate)
            try:
                if real.is_file() and real.stat().st_size > 0:
                    return os.path.realpath(real), ""  # host-path: the live system's own resolver file
            except OSError:
                continue
        self.resolv_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.resolv_path.write_text("".join(f"nameserver {server}\n" for server in PUBLIC_RESOLVERS))
        return str(self.resolv_path), "No DNS configuration was found on the live system; public resolvers are used inside the repaired system."

    def _bind_resolv(self, runner: RescueRunner) -> str:
        source, warning = self._resolv_source()
        self.ctx.bind_sources.add(source)
        if warning:
            self.warnings.append(warning)
        with self.fs(writable=True) as fs:
            try:
                canonical = fs.resolve("/etc/resolv.conf", missing_ok=True)
                if not fs.exists(canonical):
                    parent = canonical.rsplit("/", 1)[0] or "/"
                    fs.mkdir(parent, parents=True)
                    fs.touch(canonical, 0o644)
                    if not canonical.startswith("/run/"):  # only a file on the root file system has to be removed again
                        self.ledger.add("placeholder", path=canonical)
                destination = fs.file_destination(canonical)
            except (OSError, RescueError) as exc:
                self.warnings.append(f"DNS inside the repaired system is unavailable: {exc}")
                return "skipped"
        check_mount_destination(destination, self.root, file_ok=True)
        runner.run(("mount", "--bind", source, destination), timeout=30)
        self.ledger.add("bind", path=destination, mountpoint=canonical, fs="bind", virtual=True, recursive=False, virtual_name="resolv")
        runner.run(("mount", "-o", "remount,bind,ro", destination), timeout=30)
        self.bump()
        return f"bound {source} onto {canonical}"

    def derive_cmdline(self, graph: DeviceGraph | None = None) -> cmdline_mod.DerivedCmdline:
        spec = self.plan.root
        luks_uuid = lvm_vg = md_uuid = ""
        subvol = (self.candidate.get("subvol") or {}).get("path", "") if spec.subvolid is not None else ""
        if graph is not None:
            try:
                root_node = graph.find(spec.device)
            except LookupError:
                root_node = None
            for node in graph.stack_of(root_node) if root_node is not None else []:
                if node.type == "crypt":
                    for parent in graph.parents_of(node):
                        if parent.fstype == "crypto_LUKS":
                            luks_uuid = luks_uuid or parent.uuid
                elif node.type == "lvm":
                    match = LVM_NAME_RE.match(node.name)
                    if match:
                        lvm_vg = lvm_vg or match.group(1).replace("--", "-")
        with self.fs() as fs:
            return cmdline_mod.derive(fs, spec.uuid, subvol_path=subvol, luks_uuid=luks_uuid, lvm_vg=lvm_vg, md_uuid=md_uuid)

    def _bind_cmdline(self, runner: RescueRunner) -> str:
        derived = self.derive_cmdline()
        self.cmdline_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cmdline_path.write_text(derived.text + "\n")
        self.cmdline_path.chmod(0o644)
        with self.fs() as fs:
            destination = fs.file_destination("/proc/cmdline")
        check_mount_destination(destination, self.root, file_ok=True)
        runner.run(("mount", "--bind", str(self.cmdline_path), destination), timeout=30)
        self.ledger.add("cmdline", path=destination, mountpoint="/proc/cmdline", source=str(self.cmdline_path), derived_from=derived.source, virtual=True, fs="bind")
        self.bump()
        return "bound a synthesized command line"

    def ensure_virtual(self, runner: RescueRunner | None = None) -> bool:
        """Make the system chroot-ready (idempotent); False when its /proc, /sys, /dev or /run cannot be used."""
        if self.has_virtual_mounts():
            return True
        runner = runner or self.runner
        try:
            for step in self.virtual_steps():
                self.run_step(step, runner)
        except (RescueError, OSError) as exc:
            self.warnings.append(f"The system could not be prepared for programs inside it: {exc}")
            self.remove_virtual()
            return False
        return True

    def remove_virtual(self) -> None:
        for entry in reversed(self.ledger.entries):
            if entry.data.get("virtual") or entry.kind == "cmdline":
                self._unmount_entry(entry, force=True, tries=1)

    # ---- release (review: clean release, also after failures) --------------------------------------------------------------------
    def _unmount_entry(self, entry: LedgerEntry, *, force: bool, tries: int = 3) -> tuple[bool, list[dict[str, Any]], str]:
        path = entry.data["path"]
        argv = ("umount", "-R", path) if entry.data.get("recursive") else ("umount", path)
        error = ""
        for attempt in range(tries):
            result = self.runner.execute(argv, timeout=60)
            if result.returncode == 0 or "not mounted" in result.stdout:
                self.ledger.remove(entry)
                return True, [], ""
            error = result.stdout.strip()
            if attempt + 1 < tries:
                self.env.sleep(attempt + 1)
        holders: list[dict[str, Any]] = []
        probe = self.runner.execute(("fuser", "-vmM", path), timeout=15)
        holders = parse_fuser(probe.stdout)
        if force:
            lazy = self.runner.execute(("umount", "-l", path), timeout=60)
            if lazy.returncode == 0:
                self.audit.record("release", message="lazy unmount", path=path, sync=True)
                self.ledger.remove(entry)
                return True, holders, ""
            error = lazy.stdout.strip() or error
        return False, holders, error

    def release(self, *, force: bool = False) -> ReleaseResult:
        """Undo every acquisition of this session in reverse order; stops at the first mount that stays busy."""
        result = ReleaseResult()
        self.state = "closing"
        for shell in list(self.shells):
            self.shells.remove(shell)
        if self.mode == "rw":
            for entry in self.ledger.of_kind("placeholder"):
                try:
                    with self.fs(writable=True) as fs:
                        data = fs.read_bytes(entry.data["path"], 1) if fs.exists(entry.data["path"]) else b""
                        if not data:
                            fs.remove(entry.data["path"])
                except (OSError, RescueError):
                    pass
                self.ledger.remove(entry)
        else:
            for entry in self.ledger.of_kind("placeholder"):
                self.ledger.remove(entry)
        self.runner.execute(("sync",), timeout=120)
        for entry in reversed(self._mount_entries()):
            ok, holders, error = self._unmount_entry(entry, force=force)
            if ok:
                result.released.append(entry.data["path"])
                continue
            result.leftovers.append(entry.data["path"])
            result.busy_holders = holders
            if error:
                result.errors.append(f"{entry.data['path']}: {error}")
            break
        if result.leftovers:
            self.audit.record("release", message="incomplete", leftovers=result.leftovers, sync=True)
            self.persist()
            return result
        for entry in reversed(self.ledger.of_kind("blockro")):
            device = entry.data["device"]
            if entry.data.get("ro_now") and not entry.data.get("was_ro"):
                self.ctx.devices.protect(device)
                restore = self.runner.execute(("blockdev", "--setrw", device), timeout=20)
                if restore.returncode != 0:
                    result.errors.append(f"could not make {device} writable again: {restore.stdout.strip()}")
                    continue
            self.ledger.remove(entry)
            result.released.append(f"blockro:{device}")
        try:
            os.rmdir(self.root)
        except OSError:
            pass
        self.state = "closed"
        self.audit.record("release", message="session released", released=len(result.released), sync=True)
        self.forget()
        return result

    # ---- reporting ------------------------------------------------------------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        return {
            "session_id": self.sid, "state": self.state, "mode": self.mode, "allow_write": self.allow_write, "generation": self.generation, "path": self.root,
            "root": {"device": self.plan.root.device, "fstype": self.plan.root.fstype, "uuid": self.plan.root.uuid, "rootflags": self.plan.root.rootflags},
            "os": self.candidate.get("os", {}), "mounts": [dict(entry.to_dict()) for entry in self.ledger.entries if entry.kind in {"mount", "bind", "cmdline"}],
            "failed_mounts": self.failed_mounts, "warnings": self.warnings, "shells": list(self.shells), "busy": self.busy, "plans": self.plans.listing(),
            "audit": str(self.audit.path or ""),
        }

    def write_check(self) -> None:
        if not self.allow_write:
            raise RescueError("needs_write", "This session was opened for inspection only")
        if self.state not in {"mounted_ro", "mounted_rw"}:
            raise RescueError("bad_state", f"The session is {self.state}")


def session_from_state(env: SessionEnv, raw: dict[str, Any], graph: DeviceGraph | None = None) -> RescueSession:
    """Re-create a session object from its state file (adoption after a restart, or in --local mode)."""
    root = raw["root"]
    spec = RootSpec(root["device"], root["fstype"], root.get("uuid", ""), root.get("rootflags", ""))
    items = [MountItem(item["mountpoint"], item["device"], item["fstype"], [o for o in item.get("options", "").split(",") if o], int(item["tier"]), item.get("source", "fstab"),
                       selected=item.get("selected", True), needs_mkdir=item.get("needs_mkdir", False), extra_devices=item.get("extra_devices", [])) for item in raw.get("items", [])]
    plan = MountPlan(spec, items, allow_write=raw.get("allow_write", True))
    candidate = {"id": root.get("candidate", ""), "os": root.get("os", {}), "label": root.get("label", "")}
    session = RescueSession(env, raw["sid"], plan, candidate, graph, allow_write=raw.get("allow_write", True), depends=raw.get("depends", []))
    session.created = raw.get("created", session.created)
    session.generation = int(raw.get("generation", 0))
    session.state = raw.get("state", "mounted_ro")
    session.ledger = Ledger.from_state(raw.get("ledger", []), session.persist)
    return session
