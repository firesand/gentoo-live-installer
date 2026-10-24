"""RescueManager: scan, unlock, mount, sessions, release and crash recovery.

One manager per daemon (or per ``gentoo-rescue --local`` invocation). It owns the pool ledger (LUKS mappings, volume groups
and arrays opened before a session exists), the sessions, and the operations. Install and rescue never run at the same
time: ``install_busy`` is a callable that returns a reason while an installation runs, and ``install_blocker`` tells the
installer which devices rescue still holds.
"""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from .audit import AuditLog
from .devices import DeviceGraph, DeviceNode
from .errors import PlanStale, RescueError
from .fstab import Fstab, parse_crypttab
from .hostenv import Environment, check_network, collect_environment
from .ledger import Ledger, write_json_atomic
from .mountplan import MountPlan, RootSpec, plan_mounts
from .ops import Operation, OperationTable
from .plan import Plan, PlanStore
from .paths import RootFS
from .runner import RescueRunner
from .safety import PolicyContext
from .scan import ScanOptions, ScanResult, Scanner, load_graph, probe_mounted, candidate_id
from .session import LVM_NAME_RE, ReleaseResult, RescueSession, SessionEnv, session_from_state
from .stack import StackManager

POOL_VERSION = 1
KEEP_SCANS = 3
ROOTFLAGS_RE = re.compile(r"subvolid=\d{1,10}")


class RescueManager:
    def __init__(self, env: SessionEnv, *, install_busy: Callable[[], str | None] | None = None, auto_adopt: bool = False,
                 network_check: Callable[[], dict[str, bool]] | None = None) -> None:
        self.env = env
        self.network_check = network_check or check_network
        self.install_busy = install_busy or (lambda: None)
        self.auto_adopt = auto_adopt
        self.sessions: dict[str, RescueSession] = {}
        self.ops = OperationTable()
        self.lock = threading.RLock()
        self.pool_lock = threading.Lock()
        self.pool_path = env.state_dir / "pool.json"
        self.pool_ledger = Ledger(self._persist_pool)
        self.pool_ctx = PolicyContext(session_base=str(env.base_dir), probe_root=str(env.probe_root), state_dir=str(env.state_dir), probing=True)
        self.audit = AuditLog(env.audit_dir / "audit" / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(env.clock()))}-daemon.jsonl", "daemon", clock=env.clock)
        self._scans: dict[str, ScanResult] = {}
        self._views: dict[str, Any] = {}
        self._shell_managers: dict[str, Any] = {}
        self.shell_spawn: Callable[..., Any] | None = None
        self._load_pool()

    # ---- persistence of the pool ---------------------------------------------------------------------------
    def _persist_pool(self) -> None:
        try:
            if len(self.pool_ledger):
                write_json_atomic(self.pool_path, {"version": POOL_VERSION, "ledger": self.pool_ledger.to_state()})
            elif self.pool_path.exists():
                self.pool_path.unlink()
        except OSError:
            pass

    def _load_pool(self) -> None:
        try:
            raw = json.loads(self.pool_path.read_text())
        except (OSError, ValueError):
            return
        self.pool_ledger = Ledger.from_state(raw.get("ledger", []), self._persist_pool)
        for entry in self.pool_ledger:
            if entry.kind == "luks":
                self.pool_ctx.owned_maps.add(entry.data["name"])
            elif entry.kind == "vg":
                self.pool_ctx.owned_vgs.add(entry.data["name"])
            elif entry.kind == "md":
                self.pool_ctx.owned_md.add(entry.data["device"])

    # ---- helpers -----------------------------------------------------------------------------------------------
    def pool_runner(self, cancel: threading.Event | None = None) -> RescueRunner:
        return RescueRunner(self.env.make_runner(cancel), self.pool_ctx, self.audit)

    def check_not_busy(self) -> None:
        reason = self.install_busy()
        if reason:
            raise RescueError("busy_install", f"An installation is running: {reason}. Repairs cannot run at the same time.", reason=reason)

    @contextlib.contextmanager
    def exclusive_pool(self) -> Iterator[None]:
        if not self.pool_lock.acquire(blocking=False):
            raise RescueError("busy", "Another scan, unlock or mount is still running")
        try:
            yield
        finally:
            self.pool_lock.release()

    def new_op(self, kind: str, session_id: str = "", request_id: str = "") -> Operation:
        return self.ops.add(Operation(kind, session_id, request_id, clock=self.env.clock))

    def load_graph(self, runner: RescueRunner | None = None) -> DeviceGraph:
        return load_graph(runner or self.pool_runner(), self.pool_ctx, self.env.host)

    def settle(self, runner: RescueRunner) -> Callable[[], None]:
        return lambda: runner.execute(("udevadm", "settle", "--timeout=10"), timeout=20) and None

    def stack(self, runner: RescueRunner) -> StackManager:
        return StackManager(runner, self.pool_ctx, self.pool_ledger, lambda: self.load_graph(runner), self.env.host, self.settle(runner))

    def environment(self, runner: RescueRunner | None = None, *, network: bool = False) -> Environment:
        return collect_environment(self.env.host, runner or self.pool_runner(), network=network)

    # ---- scan ---------------------------------------------------------------------------------------------------------------
    def scan(self, op: Operation | None, options: ScanOptions) -> dict[str, Any]:
        self.check_not_busy()
        with self.exclusive_pool():
            runner = self.pool_runner(op.force_cancel if op else None)
            emit = self._emitter(op)
            scanner = Scanner(runner, self.pool_ctx, self.env.host, self.env.probe_root, emit=emit, clock=self.env.clock, sleep=self.env.sleep)
            result = scanner.scan(options)
            self._scans[result.scan_id] = result
            while len(self._scans) > KEEP_SCANS:
                self._scans.pop(next(iter(self._scans)))
            self.audit.record("scan", scan=result.scan_id, candidates=len(result.candidates))
            return result.to_dict()

    @staticmethod
    def _emitter(op: Operation | None) -> Callable[..., None]:
        def emit(kind: str, message: str = "", **data: Any) -> None:
            if op is None:
                return
            stage = data.pop("stage", "")
            progress = data.pop("progress", None)
            op.emit(kind if kind in {"stage", "progress", "log", "status"} else "log", message, stage=stage, progress=progress, data=data)

        return emit

    # ---- unlock / activate -------------------------------------------------------------------------------------------------------
    def unlock(self, op: Operation | None, params: dict[str, Any]) -> dict[str, Any]:
        self.check_not_busy()
        device = str(params.get("device", ""))
        if not device.startswith("/dev/"):
            raise RescueError("bad_request", "A device path is required")
        passphrase = params.get("passphrase")
        keyfile = self._keyfile_path(params)
        with self.exclusive_pool():
            runner = self.pool_runner(op.force_cancel if op else None)
            graph = self.load_graph(runner)
            try:
                node = graph.find(device)
            except LookupError as exc:
                raise RescueError("not_found", f"{device} was not found", device=device) from exc
            stack = self.stack(runner)
            result = stack.unlock(node, passphrase if isinstance(passphrase, str) else None, readonly=bool(params.get("readonly")), test_only=bool(params.get("test_only")), keyfile=keyfile)
            del passphrase
            self.audit.record("unlock", device=device, ours=result.get("ours"), mapper=result.get("mapper", ""), sync=True)
            if params.get("test_only"):
                return result
            fresh = Scanner(runner, self.pool_ctx, self.env.host, self.env.probe_root, sleep=self.env.sleep)
            after = fresh.scan(ScanOptions(probe=True))
            mapper = result.get("mapper", "")
            result["nodes"] = [node.to_dict() for node in after.graph if node.path == mapper or any(p.path == mapper for p in after.graph.parents_of(node))]
            result["new_candidates"] = [item for item in after.candidates if any(mapper and mapper in label for label in item["stack"])]
            self._scans[after.scan_id] = after
            return result

    def _keyfile_path(self, params: dict[str, Any]) -> str:
        keyfile = str(params.get("keyfile", ""))
        if not keyfile:
            return ""
        session = self.session(str(params.get("keyfile_session", "")))
        with session.fs() as fs:
            info = fs.stat(keyfile)
            if info.st_size > 8 << 20 or not info.st_mode & 0o100000:
                raise RescueError("bad_request", "The key file must be a regular file of at most 8 MiB")
            return fs.host_path(keyfile)

    def activate(self, op: Operation | None, params: dict[str, Any]) -> dict[str, Any]:
        self.check_not_busy()
        kind = params.get("kind")
        with self.exclusive_pool():
            runner = self.pool_runner(op.force_cancel if op else None)
            stack = self.stack(runner)
            if kind == "lvm":
                name = str(params.get("vg", ""))
                if not name:
                    raise RescueError("bad_request", "A volume group name is required")
                result = stack.activate_vg(name)
            elif kind == "md":
                uuids = [str(item) for item in params.get("uuids", [])]
                if not uuids:
                    raise RescueError("bad_request", "At least one array UUID is required")
                result = stack.assemble_md(uuids, degraded_ok=bool(params.get("degraded_ok")))
            else:
                raise RescueError("bad_request", "kind must be lvm or md")
            self.audit.record("activate", what=kind, params={key: value for key, value in params.items() if key in {"vg", "uuids"}}, sync=True)
            fresh = Scanner(runner, self.pool_ctx, self.env.host, self.env.probe_root, sleep=self.env.sleep).scan(ScanOptions(probe=True))
            self._scans[fresh.scan_id] = fresh
            result["new_candidates"] = fresh.candidates
            result["nodes"] = fresh.graph.to_dict_list()
            return result

    # ---- mounting ------------------------------------------------------------------------------------------------------------------
    def _candidate(self, params: dict[str, Any], graph: DeviceGraph) -> tuple[dict[str, Any], DeviceNode]:
        cid = str(params.get("candidate", ""))
        candidate: dict[str, Any] | None = None
        if cid:
            for scan in ([self._scans.get(str(params.get("scan_id", "")))] if params.get("scan_id") else []) + list(reversed(list(self._scans.values()))):
                if scan is None:
                    continue
                try:
                    candidate = dict(scan.candidate(cid))
                    break
                except KeyError:
                    continue
            if candidate is None:
                raise RescueError("not_found", "That system is not in a recent scan; scan again", candidate=cid)
        else:
            device = str(params.get("device", ""))
            if not device.startswith("/dev/"):
                raise RescueError("bad_request", "A candidate id or a device path is required")
            try:
                found = graph.find(device)
            except LookupError as exc:
                raise RescueError("not_found", f"{device} was not found", device=device) from exc
            rootflags = str(params.get("rootflags", ""))
            if rootflags and not (found.fstype == "btrfs" and ROOTFLAGS_RE.fullmatch(rootflags)):
                raise RescueError("bad_request", "The root flags must be empty or subvolid=NUMBER (btrfs only)", rootflags=rootflags)
            candidate = {"id": candidate_id(found.uuid, rootflags, found.path), "device": found.path, "fstype": found.fstype, "uuid": found.uuid, "label": found.label, "rootflags": rootflags,
                         "os": {}, "subvol": None, "kind": "root"}
        try:
            node = graph.find(candidate["device"])
        except LookupError as exc:
            raise RescueError("not_found", f"{candidate['device']} is no longer present", device=candidate["device"]) from exc
        if node.live_medium:
            raise RescueError("live_medium", "This is the medium the live system is running from. It is hidden and can never be modified.", device=node.path)
        if node.unsupported_reason:
            raise RescueError("unsupported_fs", node.unsupported_reason, device=node.path, reason=node.unsupported_reason)
        if node.role == "luks_locked":
            raise RescueError("bad_request", "This volume is encrypted. Unlock it first.", device=node.path)
        if node.role != "filesystem" and node.fstype not in {"ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "jfs"}:
            raise RescueError("bad_request", f"{node.path} does not hold a file system rescue can mount", device=node.path)
        if candidate.get("uuid") and node.uuid and candidate["uuid"] != node.uuid:
            raise PlanStale("The disk behind this system changed since the scan; scan again")
        if candidate.get("kind") == "snapshot" and params.get("allow_write", True):
            raise RescueError("unsupported_action", "Snapshots are read-only history. Repair the active system, or mount the snapshot with repairs disabled.", device=node.path)
        return candidate, node

    def plan_mount(self, op: Operation | None, params: dict[str, Any], graph: DeviceGraph | None = None) -> tuple[MountPlan, dict[str, Any], DeviceGraph, DeviceNode]:
        runner = self.pool_runner(op.force_cancel if op else None)
        graph = graph or self.load_graph(runner)
        candidate, node = self._candidate(params, graph)
        rootflags = candidate.get("rootflags", "")
        spec = RootSpec(node.path, node.fstype, node.uuid, rootflags, node.id)
        with probe_mounted(runner, self.env.host, self.env.probe_root, node, subvolid=spec.subvolid, sleep=self.env.sleep, graph=graph) as (target, rung):
            with RootFS(target) as fs:
                plan = plan_mounts(
                    spec, fs, Fstab.parse(fs.read_text_or("/etc/fstab")), parse_crypttab(fs.read_text_or("/etc/crypttab")), graph, include_tier4=params.get("include_tier4", []),
                    overrides=params.get("overrides", []), allow_write=bool(params.get("allow_write", True)), esp_default_mountpoint="/efi" if candidate.get("os", {}).get("family") == "gentoo" else "/boot/efi",
                    unmount_existing=params.get("unmount_existing", []))
        env = self.environment(runner)
        if env.uefi is False and any(item.get("mountpoint") in {"/efi", "/boot/efi"} for item in [it.to_dict() for it in plan.items]):
            plan.warnings.append("This live session started in legacy BIOS mode but the system boots with UEFI. Boot entries cannot be written from here; the fallback boot path still works.")
        return plan, candidate, graph, node

    def mount(self, op: Operation | None, params: dict[str, Any]) -> dict[str, Any]:
        self.check_not_busy()
        with self.exclusive_pool():
            plan, candidate, graph, node = self.plan_mount(op, params)
            if params.get("dry_run"):
                return {"mount_plan": {**plan.to_dict(), "commands": self._command_preview(plan)}, "candidate": candidate}
            digest = str(params.get("plan_digest", ""))
            if digest != plan.digest:
                raise PlanStale("The mount plan changed since you reviewed it; review it again", expected=plan.digest)
            return self._start_session(op, params, plan, candidate, graph, node)

    def _command_preview(self, plan: MountPlan) -> list[str]:
        harden = "nodev,nosuid"
        lines = [f"blockdev --setro {device}" for device in plan.devices()]
        from .fsprobe import RECOVERY_SKIP, mount_type

        lines.append(f"mount -t {mount_type(plan.root.fstype)} -o ro,{','.join([*([plan.root.rootflags] if plan.root.rootflags else []), *RECOVERY_SKIP.get(plan.root.fstype, ())] + [harden])} {plan.root.device} <session root>")
        for item in plan.selected:
            options = ["ro", *item.options, *RECOVERY_SKIP.get(item.fstype, ()), harden]
            lines.append(f"mount -t {mount_type(item.fstype)} -o {','.join(options)} {item.device} <session root>{item.mountpoint}")
        return lines

    def _dependencies(self, graph: DeviceGraph, plan: MountPlan) -> tuple[list[int], list[str], list[str]]:
        nodes = {}
        for path in plan.devices():
            try:
                for member in graph.stack_of(graph.find(path)):
                    nodes[member.key] = member
            except LookupError:
                continue
        paths = {node.path for node in nodes.values()}
        depends: list[int] = []
        readonly_md: list[str] = []
        pool_devices: list[str] = []
        for entry in self.pool_ledger:
            if entry.kind == "luks" and f"/dev/mapper/{entry.data['name']}" in paths:
                depends.append(entry.id)
            elif entry.kind == "vg":
                for node in nodes.values():
                    match = LVM_NAME_RE.match(node.name) if node.type == "lvm" else None
                    if match and match.group(1).replace("--", "-") == entry.data["name"]:
                        depends.append(entry.id)
                        break
            elif entry.kind == "md" and entry.data["device"] in paths:
                depends.append(entry.id)
                readonly_md.append(entry.data["device"])
        return sorted(set(depends)), readonly_md, pool_devices

    def _start_session(self, op: Operation | None, params: dict[str, Any], plan: MountPlan, candidate: dict[str, Any], graph: DeviceGraph, node: DeviceNode) -> dict[str, Any]:
        sid = "r" + secrets.token_hex(3)
        depends, readonly_md, pool_devices = self._dependencies(graph, plan)
        session = RescueSession(self.env, sid, plan, candidate, graph, allow_write=bool(params.get("allow_write", True)), allow_degraded=bool(params.get("allow_degraded")),
                                depends=depends, pool_devices=pool_devices, readonly_md=readonly_md)
        spec = plan.root

        def verify(fs: RootFS) -> None:
            again = plan_mounts(spec, fs, Fstab.parse(fs.read_text_or("/etc/fstab")), parse_crypttab(fs.read_text_or("/etc/crypttab")), graph, include_tier4=params.get("include_tier4", []),
                                overrides=params.get("overrides", []), allow_write=session.allow_write, esp_default_mountpoint="/efi" if candidate.get("os", {}).get("family") == "gentoo" else "/boot/efi",
                                unmount_existing=params.get("unmount_existing", []), own_root=session.root)
            if again.digest != plan.digest:
                raise PlanStale("The system's fstab changed while it was being mounted; review the mount plan again")

        with self.lock:
            self.sessions[sid] = session
        try:
            session.mount_ro(graph, verify, self._emitter(op))
        except BaseException:
            with self.lock:
                self.sessions.pop(sid, None)
            raise
        self.audit.record("mount", sid=sid, device=spec.device, sync=True)
        return self.describe_new_session(session, graph, op)

    def describe_new_session(self, session: RescueSession, graph: DeviceGraph, op: Operation | None) -> dict[str, Any]:
        from .actions import describe_session

        info = session.info()
        info.update(describe_session(self, session, graph))
        return info

    # ---- sessions ---------------------------------------------------------------------------------------------------------------------
    def session(self, sid: str) -> RescueSession:
        with self.lock:
            found = self.sessions.get(sid)
        if found is not None:
            return found
        if self.auto_adopt and sid:
            adopted = self.adopt(sid)
            if adopted is not None:
                return adopted
        raise RescueError("not_found", f"There is no repair session {sid or '(none given)'}", session_id=sid)

    def adopt(self, sid: str) -> RescueSession | None:
        path = self.env.state_dir / "sessions" / f"{sid}.json"
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        graph: DeviceGraph | None
        try:
            graph = self.load_graph()
        except RescueError:
            graph = None
        session = session_from_state(self.env, raw, graph)
        with self.lock:
            self.sessions[sid] = session
        return session

    def session_summary(self, sid: str = "") -> dict[str, Any]:
        if sid:
            return self.session(sid).info()
        if self.auto_adopt:  # without a daemon every command is a new process: the systems mounted by earlier commands are sessions, not leftovers
            for item in self.orphans():
                if item["sid"] and item["state"] == "alive":
                    self.adopt(item["sid"])
        with self.lock:
            sessions = [session.info() for session in self.sessions.values()]
        return {"sessions": sessions, "orphans": self.orphans(), "pool": [entry.to_dict() for entry in self.pool_ledger]}

    def status(self) -> dict[str, Any]:
        """The ``rescue`` key of the daemon's status reply."""
        with self.lock:
            sessions = [{"session_id": s.sid, "state": s.state, "mode": s.mode, "device": s.plan.root.device} for s in self.sessions.values()]
        return {"api": 1, "sessions": sessions, "pool": len(self.pool_ledger), "running": [op.summary() for op in self.ops.running()]}

    def install_blocker(self) -> str | None:
        """Why an installation must not start: devices that rescue still holds."""
        with self.lock:
            sessions = list(self.sessions.values())
        if sessions:
            held = ", ".join(session.plan.root.device for session in sessions)
            return f"A repair session holds {held}; unmount it first"
        if len(self.pool_ledger):
            return "Unlocked or activated storage from a repair session is still open; release it first"
        return None

    # ---- release --------------------------------------------------------------------------------------------------------------------------
    def unmount(self, op: Operation | None, params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("session_id", ""))
        force = bool(params.get("force"))
        if not sid:
            return self._release_pool(op, force)
        session = self.session(sid)
        if session.shells and not force:
            raise RescueError("busy", "A terminal is still open in this system. Close it, or choose Force unmount.", shells=len(session.shells))
        if not session.op_lock.acquire(blocking=False):
            raise RescueError("busy", "A step is still running in this session", busy=session.busy)
        try:
            session.busy = {"op": op.id if op else "", "what": "unmount"}
            if session.shells:
                from .shell import close_shells

                close_shells(session)
            emit = self._emitter(op)
            report = ""
            if params.get("save_report", True) and session.mode == "rw":
                report = self._save_report(session)
            emit("stage", "Unmounting", stage="mounts")
            result = session.release(force=force)
            payload = result.to_dict()
            if report:
                payload["report"] = report
            if result.complete:
                with self.lock:
                    self.sessions.pop(sid, None)
                payload["stack"] = self._release_stack(session, op)
            elif not force:
                payload["hint"] = "Close the programs listed under busy_holders, or choose Force unmount."
            return payload
        finally:
            session.busy = None
            session.op_lock.release()

    def _save_report(self, session: RescueSession) -> str:
        """Copy the audit log into the repaired system (only onto a persistent file system; review R7d)."""
        if session.audit.path is None or session.mode != "rw":
            return ""
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(session.created))
        try:
            with session.fs(writable=True) as fs:
                directory = fs.resolve("/var/log/gentoo-rescue", missing_ok=True)
                if directory.startswith(("/run/", "/tmp/", "/dev/")) or directory == "/run":
                    return ""
                data = session.audit.text("jsonl").encode()
                result = fs.write_atomic(f"{directory}/{stamp}-{session.sid}.jsonl", data, mode=0o600, uid=0, gid=0, parents=True)
                return result.path
        except (OSError, RescueError):
            return ""

    def _release_stack(self, session: RescueSession, op: Operation | None) -> list[str]:
        released: list[str] = []
        runner = self.pool_runner()
        stack = self.stack(runner)
        for entry_id in sorted(session.depends, reverse=True):
            with self.lock:
                others = [other for other in self.sessions.values() if other is not session and entry_id in other.depends]
            if others:
                continue
            entry = next((item for item in self.pool_ledger if item.id == entry_id), None)
            if entry is None:
                continue
            try:
                stack.release(entry.kind, entry.data)
            except RescueError as exc:
                released.append(f"{entry.kind}:{entry.data.get('name') or entry.data.get('device')} kept: {exc.message}")
                continue
            self.pool_ledger.remove(entry)
            released.append(f"{entry.kind}:{entry.data.get('name') or entry.data.get('device')}")
        return released

    def _release_pool(self, op: Operation | None, force: bool) -> dict[str, Any]:
        with self.lock:
            if any(s.mounted for s in self.sessions.values()):
                raise RescueError("busy", "Unmount the repair sessions first")
        runner = self.pool_runner()
        stack = self.stack(runner)
        released, errors = [], []
        for entry in reversed(list(self.pool_ledger)):
            try:
                stack.release(entry.kind, entry.data)
                self.pool_ledger.remove(entry)
                released.append(f"{entry.kind}:{entry.data.get('name') or entry.data.get('device')}")
            except RescueError as exc:
                errors.append(f"{entry.kind}: {exc.message}")
        return {"released": released, "leftovers": [e.to_dict() for e in self.pool_ledger], "errors": errors}

    def shutdown(self, timeout: float = 25.0) -> list[str]:
        """Release every session without force (shells hung up); anything left stays in the state files for recovery."""
        deadline = time.monotonic() + timeout
        problems: list[str] = []
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            if time.monotonic() > deadline:
                problems.append(f"{session.sid}: out of time")
                continue
            try:
                if session.shells:
                    from .shell import close_shells

                    close_shells(session)
                result = session.release()
                if result.complete:
                    with self.lock:
                        self.sessions.pop(session.sid, None)
                    self._release_stack(session, None)
                else:
                    problems.append(f"{session.sid}: {', '.join(result.leftovers)} still busy")
            except Exception as exc:  # a failing release must not block the shutdown of the others
                problems.append(f"{session.sid}: {exc}")
        return problems

    # ---- crash recovery ----------------------------------------------------------------------------------------------------------------------
    def orphans(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        directory = self.env.state_dir / "sessions"
        with self.lock:
            known = set(self.sessions)
        runner = self.pool_runner()
        for path in sorted(directory.glob("r*.json")) if directory.is_dir() else []:
            try:
                raw = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            sid = raw.get("sid", path.stem)
            if sid in known:
                continue
            root = raw.get("root", {}).get("path", str(self.env.base_dir / sid))
            listing = runner.execute(("findmnt", "-R", "-n", "-l", "-o", "TARGET", root), timeout=15)
            mounts = [line.strip() for line in listing.stdout.splitlines() if line.strip()] if listing.returncode == 0 else []
            ledger = raw.get("ledger", [])
            found.append({"sid": sid, "state": "alive" if mounts else "stale", "mounts": mounts, "mappings": [e["name"] for e in ledger if e.get("kind") == "luks"],
                          "vgs": [e["name"] for e in ledger if e.get("kind") == "vg"], "created": raw.get("created"), "device": raw.get("root", {}).get("device", "")})
        if len(self.pool_ledger):
            found.append({"sid": "", "state": "pool", "mounts": [], "mappings": [e.data["name"] for e in self.pool_ledger.of_kind("luks")],
                          "vgs": [e.data["name"] for e in self.pool_ledger.of_kind("vg")], "arrays": [e.data["device"] for e in self.pool_ledger.of_kind("md")]})
        listing = runner.execute(("findmnt", "-R", "-n", "-l", "-o", "TARGET", str(self.env.base_dir)), timeout=15)
        if listing.returncode == 0:
            known_roots = {item["sid"] for item in found} | known
            unknown = sorted({line.strip().split("/")[len(self.env.base_dir.parts)] for line in listing.stdout.splitlines()
                              if line.strip().startswith(str(self.env.base_dir) + "/") and len(line.strip().split("/")) > len(self.env.base_dir.parts)} - known_roots)
            found.extend({"sid": sid, "state": "unknown", "mounts": [], "mappings": [], "vgs": []} for sid in unknown)
        return found

    def recover(self, op: Operation | None, params: dict[str, Any]) -> dict[str, Any]:
        action = params.get("do")
        sid = str(params.get("sid", ""))
        if not action:
            return {"orphans": self.orphans()}
        if action == "adopt":
            session = self.adopt(sid)
            if session is None:
                raise RescueError("not_found", f"There is no saved session {sid}", session_id=sid)
            return session.info()
        if action == "cleanup":
            if params.get("all"):
                return self.cleanup_all(force=bool(params.get("force", True)))
            if not sid:
                return self._release_pool(op, bool(params.get("force")))
            session = self.sessions.get(sid) or self.adopt(sid)
            if session is None:
                raise RescueError("not_found", f"There is no saved session {sid}", session_id=sid)
            result = session.release(force=True)
            payload = result.to_dict()
            if result.complete:
                with self.lock:
                    self.sessions.pop(sid, None)
                payload["stack"] = self._release_stack(session, op)
            return payload
        raise RescueError("bad_request", "do must be adopt or cleanup")

    def cleanup_all(self, force: bool = True) -> dict[str, Any]:
        """Release every saved session and the pool (``gentoo-rescue cleanup``)."""
        summary: dict[str, Any] = {"sessions": {}, "pool": {}}
        for item in self.orphans():
            if item["sid"]:
                try:
                    summary["sessions"][item["sid"]] = self.recover(None, {"do": "cleanup", "sid": item["sid"], "force": force})
                except RescueError as exc:
                    summary["sessions"][item["sid"]] = {"error": exc.message}
        with self.lock:
            live = list(self.sessions.values())
        for session in live:
            summary["sessions"][session.sid] = self.unmount(None, {"session_id": session.sid, "force": force, "save_report": False})
        summary["pool"] = self._release_pool(None, force)
        return summary


    # ---- views, plans, queries -----------------------------------------------------------------------------------------------------------------------
    def view_for(self, session: RescueSession) -> Any:
        from .view import SessionView

        cached = self._views.get(session.sid)
        if cached is not None:
            return cached
        env = collect_environment(self.env.host, session.runner)
        graphs: dict[str, Any] = {}

        def provider() -> DeviceGraph:
            key = session.generation
            if graphs.get("key") != key:
                graphs["graph"] = load_graph(session.runner, session.ctx, self.env.host)
                graphs["key"] = key
            return graphs["graph"]

        view = SessionView(session, provider, env)
        self._views[session.sid] = view
        return view

    def plan(self, params: dict[str, Any]) -> dict[str, Any]:
        from .actions import build_plan

        session = self.session(str(params.get("session_id", "")))
        if session.state not in {"mounted_ro", "mounted_rw"}:
            raise RescueError("bad_state", f"The session is {session.state}; mount the system first")
        action = str(params.get("action_id", ""))
        view = self.view_for(session)
        plan = build_plan(self, session, view, action, dict(params.get("params") or {}))
        session.plans.add(plan)
        session.audit.record("plan", plan=plan.id, action=action, risk=plan.risk, digest=plan.digest, steps=[step.public() for step in plan.steps], params=plan.params, sync=True)
        return plan.to_dict()

    def _confirmed_plan(self, session: RescueSession, params: dict[str, Any]) -> Plan:
        from .actions import build_plan

        confirm = bool(params.get("confirm"))
        digest = str(params.get("digest", ""))
        if params.get("plan_id"):
            return session.plans.take(str(params["plan_id"]), digest, generation=session.generation, root_uuid=session.plan.root.uuid, confirm=confirm, typed=str(params.get("typed", "")),
                                      phrase=str(params.get("phrase", "")))
        if params.get("action_id"):  # stateless: rebuild the plan and require the digest the user was shown (separate CLI invocations)
            rebuilt = build_plan(self, session, self.view_for(session), str(params["action_id"]), dict(params.get("params") or {}))
            store = PlanStore(self.env.clock)
            store.add(rebuilt)
            return store.take(rebuilt.id, digest, generation=session.generation, root_uuid=session.plan.root.uuid, confirm=confirm, typed=str(params.get("typed", "")),
                              phrase=str(params.get("phrase", "")))
        raise RescueError("bad_request", "plan_id (or action_id and params) and digest are required")

    def run_plan(self, op: Operation, params: dict[str, Any]) -> dict[str, Any]:
        from .executor import PlanExecutor

        self.check_not_busy()
        session = self.session(str(params.get("session_id", "")))
        plan = self._confirmed_plan(session, params)
        failed = plan.failed_preconditions()
        if failed:
            raise RescueError("precondition_failed", "; ".join(failed), reasons=failed)
        secrets_in = {key: value for key, value in (params.get("secrets") or {}).items() if isinstance(value, str)}
        for name in plan.secrets_needed:
            if name not in secrets_in:
                raise RescueError("needs_confirmation", f"The {name} is needed to run this plan", missing=name)
        if plan.needs.get("network"):
            status = self.network_check()
            if not status["ip"] or not status["dns"]:
                raise RescueError("needs_network", "This repair needs the network: " + ("the computer has no route to the internet" if not status["ip"] else "name lookup (DNS) does not work"), network=status)
        if not session.op_lock.acquire(blocking=False):
            raise RescueError("busy", "Another step is still running in this session", busy=session.busy)
        try:
            session.busy = {"op": op.id, "what": f"plan {plan.id}"}
            session.audit.record("confirm", plan=plan.id, client=params.get("_peer", {}), typed_len=len(str(params.get("typed", ""))), sync=True)
            session.cancel.clear()
            result = PlanExecutor(self, session, plan, op, secrets_in).execute()
            self._views.pop(session.sid, None)
            op.publish(result, f"{plan.title}: {result['outcome']}")
            return result
        finally:
            session.busy = None
            session.op_lock.release()

    def query(self, params: dict[str, Any]) -> dict[str, Any]:
        from .actions import run_query

        session = self.session(str(params.get("session_id", "")))
        name = str(params.get("query", ""))
        if session.state not in {"mounted_ro", "mounted_rw"}:
            raise RescueError("bad_state", "The session is not mounted")
        return {"query": name, "data": run_query(self, session, name, dict(params.get("params") or {}))}

    def cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        op = self.ops.get(str(params.get("job_id", ""))) if params.get("job_id") else None
        if op is None and params.get("session_id"):
            running = [item for item in self.ops.running() if item.session_id == params["session_id"]]
            op = running[-1] if running else None
        if op is None or op.success is not None:
            raise RescueError("not_found", "Nothing is running that can be cancelled")
        force = bool(params.get("force"))
        op.request_cancel(force=force)
        if force and op.session_id:
            self.session(op.session_id).cancel.set()
        return {"cancelling": op.id, "force": force, "message": "The current command is stopped; programs such as dracut or grub-install may leave files half-written." if force
                else "The plan stops after the step that is running."}

    def audit_text(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self.session(str(params.get("session_id", "")))
        fmt = "text" if params.get("format") == "text" else "jsonl"
        name = (session.audit.path.name if session.audit.path else f"{session.sid}.jsonl")
        return {"name": name, "text": session.audit.text(fmt, int(params.get("tail", 0)))}

    def operation_for(self, params: dict[str, Any]) -> Operation:
        op = self.ops.get(str(params.get("job_id", ""))) if params.get("job_id") else None
        if op is None and params.get("session_id"):
            op = self.ops.latest(str(params["session_id"]))
        if op is None:
            raise RescueError("not_found", "There is no such operation; it may have been forgotten")
        return op


    # ---- terminals ---------------------------------------------------------------------------------------------------------------------------------
    def shell_for(self, session: RescueSession) -> Any:
        from .shell import ShellManager

        if session.sid not in self._shell_managers:
            self._shell_managers[session.sid] = ShellManager(session, spawn=self.shell_spawn, clock=self.env.clock) if self.shell_spawn else ShellManager(session, clock=self.env.clock)
        return self._shell_managers[session.sid]

    def open_shell(self, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self.check_not_busy()
        session = self.session(str(params.get("session_id", "")))
        return self.shell_for(session).open_pty(params)

    def run_shell_here(self, params: dict[str, Any]) -> int:
        self.check_not_busy()
        session = self.session(str(params.get("session_id", "")))
        return self.shell_for(session).run_here(params)
