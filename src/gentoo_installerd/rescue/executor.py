"""Running a confirmed plan on the session's worker thread (review R6, R7, R15).

Steps run in order; cancel is honoured *between* steps (a command is never killed unless the user forces it); the cleanup
steps always run; a chroot smoke test decides whether the system's own programs or the live-side fallback are used; and every
post-check result is reported, never silently turned into success.
"""

from __future__ import annotations

import io
import tarfile
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from gentoo_installer.layout import human_size

from . import efi as efimod
from .audit import tail
from .builder import SMOKE_ID
from .cmdline import contains_live_arguments, effective_text
from .errors import RescueError
from .fstab import resolve_all
from .ops import Operation
from .paths import RootFS
from .plan import Plan, Step
from .session import RescueSession
from .view import SessionView

if TYPE_CHECKING:  # pragma: no cover
    from .manager import RescueManager

LOG_RATE = 50  # log lines per second per operation
MAX_BACKUP_BYTES = 64 << 20  # what the ESP backup holds at most
MAX_BACKUP_FILE = 16 << 20  # a single file above this is left out: firmware capsules and recovery images, not boot loaders


class PlanExecutor:
    def __init__(self, manager: RescueManager, session: RescueSession, plan: Plan, op: Operation, secrets: dict[str, str] | None = None) -> None:
        self.manager = manager
        self.session = session
        self.plan = plan
        self.op = op
        self.secrets = secrets or {}
        self.chroot_state = ""  # "" until the smoke test ran, then "ok" or "broken"
        self.killed = False  # a forced cancel stopped a command
        self.results: list[dict[str, Any]] = []
        self._streamed = 0
        self._window = 0.0
        self._count = 0
        self._dropped = 0

    # ---- output -----------------------------------------------------------------------------------------------------------------------
    def _log(self, text: str) -> None:
        now = time.monotonic()
        if now - self._window > 1.0:
            self._window, self._count = now, 0
            if self._dropped:
                self.op.log(f"... {self._dropped} lines not shown (see the audit log)")
                self._dropped = 0
        for line in text.splitlines():
            if self._count >= LOG_RATE:
                self._dropped += 1
                continue
            self._count += 1
            self.op.log(line)

    def _stream(self, line: str) -> None:
        self._streamed += 1
        self._log(line)

    def _should_run(self, step: Step) -> bool:
        if step.when == "chroot_ok":
            return self.chroot_state != "broken"
        if step.when == "chroot_broken":
            return self.chroot_state == "broken"
        return True

    # ---- one step ------------------------------------------------------------------------------------------------------------------------
    def _run_step(self, step: Step, index: int, total: int, *, cleanup: bool = False) -> bool:
        runner = self.session.runner
        runner.scope.plan, runner.scope.step = self.plan.id, step.id
        progress = (index / max(total, 1)) * 100
        if not self._should_run(step):
            self.op.stage("step", f"Skipped: {step.display}", progress, step=step.id, status="skipped")
            self.results.append({"id": step.id, "status": "skipped", "rc": None, "duration": 0.0})
            return True
        self.op.stage("step", step.display, progress, step=step.id, status="running", index=index + 1, total=total, atomic=step.atomic)
        started = time.monotonic()
        rc, output, error = 0, "", ""
        self._streamed = 0
        try:
            if step.id == SMOKE_ID:
                rc, output = self._smoke(step)
            elif step.handler == "backup-esp":
                rc, output = self._backup_esp(step)
            else:
                rc, output = self.session.run_step(step, runner, self.secrets)
        except RescueError as exc:
            rc, error = 1, exc.message
            output = exc.message
            self.killed = self.killed or exc.short == "cancelled"
        except OSError as exc:
            rc, error = 1, f"{exc.strerror or exc}"
            output = error
        duration = round(time.monotonic() - started, 2)
        if output and not self._streamed:  # a runner that cannot stream (or a step that is not a command) reports its output at the end
            self._log(tail(output, 4000))
        ok = rc in step.ok_rc and not error
        if step.id == SMOKE_ID:
            ok = True
        status = "completed" if ok else "failed"
        self.op.stage("step", step.display, progress, step=step.id, status=status, rc=rc, duration=duration, **({"error": error} if error else {}))
        self.results.append({"id": step.id, "status": status, "rc": rc, "duration": duration, **({"error": error} if error else {})})
        if not ok and step.on_fail == "continue":
            self.plan.warnings.append(f"{step.display} did not succeed (exit code {rc}{': ' + error if error else ''}); the plan continued")
            return True
        return ok

    def _smoke(self, step: Step) -> tuple[int, str]:
        """Can the system run its own programs? (missing loader or libc, wrong architecture, noexec, SELinux): review R15."""
        result = self.session.runner.chroot(["true"], tools={"true"}, timeout=60, check=False)
        self.chroot_state = "ok" if result.returncode == 0 else "broken"
        if self.chroot_state == "broken":
            self.plan.warnings.append("This system cannot run its own programs here (missing loader or libc, wrong architecture or a damaged system). Steps that need them are skipped; "
                                      "the live system's tools are used where the plan has a fallback.")
            return 0, f"The system cannot run its own programs: {tail(result.stdout, 300).strip() or 'chroot failed'}"
        return 0, ""

    # ---- internal steps that need the executor ---------------------------------------------------------------------------------------------
    # (the session handles check-idle, resolv-bind and cmdline-bind; the ESP backup lives here because it writes a tar archive)
    def _backup_esp(self, step: Step) -> tuple[int, str]:
        backup = backup_esp(self.session, step.args["mountpoint"])
        if backup.skipped:
            shown = ", ".join(backup.skipped[:3]) + (f" and {len(backup.skipped) - 3} more" if len(backup.skipped) > 3 else "")
            self.plan.warnings.append(f"Files over {MAX_BACKUP_FILE >> 20} MiB, and what does not fit in {MAX_BACKUP_BYTES >> 20} MiB, are not copied into the backup of the EFI partition: {shown}.")
        return 0, f"saved {backup.path}"

    # ---- the plan ----------------------------------------------------------------------------------------------------------------------------
    def execute(self) -> dict[str, Any]:
        self.session.live_output = self._stream
        try:
            return self._execute()
        finally:
            self.session.live_output = None

    def _execute(self) -> dict[str, Any]:
        steps = self.plan.steps
        total = len(steps) + len(self.plan.cleanup)
        outcome = "success"
        failed_group = ""
        index = 0
        for step in steps:
            if self.op.cancel_requested.is_set():
                outcome = "cancelled"
                break
            ok = self._run_step(step, index, total)
            index += 1
            if not ok:
                outcome, failed_group = "failed", step.group
                break
        if self.killed:
            outcome = "cancelled"
        self.session.cancel.clear()  # the cleanup must be able to run after a forced cancel
        if outcome in {"failed", "cancelled"} and failed_group in {"enter-write", "leave-write"}:
            self._rollback()
        for step in self.plan.cleanup:
            self._run_step(step, index, total, cleanup=True)
            index += 1
        checks: list[dict[str, Any]] = []
        if outcome == "success":
            self.session.bump()  # plans made before this one are stale
            checks = self._post_checks()
            if any(not check["ok"] for check in checks):
                outcome = "warning"
        elif outcome == "cancelled":
            self.session.bump()
        self.session.audit.record("result", plan=self.plan.id, outcome=outcome, steps=len(self.results), sync=True)
        return {
            "plan_id": self.plan.id, "outcome": outcome, "steps": self.results, "post_checks": checks, "warnings": self.plan.warnings, "next_hints": self.plan.next_hints if outcome in {"success", "warning"} else
            ["Fix the problem shown above and make a new plan."] if outcome == "failed" else [], "session": {"mode": self.session.mode, "generation": self.session.generation},
        }

    def _rollback(self) -> None:
        """A failed read-write transition returns to the read-only mount that existed before (nothing was written)."""
        self.op.stage("rollback", "Returning to the read-only mount")
        try:
            self.session.remove_virtual()
            for entry in reversed([e for e in self.session.ledger.entries if e.kind in {"mount", "bind", "cmdline"}]):
                self.session._unmount_entry(entry, force=True, tries=1)
            for device in self.session.plan.devices():
                self.session.runner.execute(("blockdev", "--setro", device), timeout=20)
                for entry in self.session.ledger.of_kind("blockro"):
                    if entry.data["device"] == device:
                        entry.data["ro_now"] = True
            from .fsprobe import mount_readonly

            spec = self.session.plan.root
            rung = mount_readonly(self.session.runner, spec.device, spec.fstype, self.session.root, extra=spec.options, subvolid=spec.subvolid, noexec=not self.session.allow_write,
                                  available=self.manager.env.host.filesystems() or None)
            self.session.ledger.add("mount", path=self.session.root, mountpoint="/", device=spec.device, fs=spec.fstype, tier=1, rw=False, rung=rung, recursive=False)
            for item in self.session.plan.selected:
                self.session._mount_item(item, self.manager.load_graph(), lambda *_a, **_k: None)
            self.session.bump()
            self.plan.warnings.append("The read-write mount failed; the system is mounted read-only again and nothing was written. Run a file system check.")
        except RescueError as exc:
            self.plan.warnings.append(f"Returning to the read-only mount failed: {exc.message}")

    # ---- post checks ---------------------------------------------------------------------------------------------------------------------------
    def _post_checks(self) -> list[dict[str, Any]]:
        view = self.manager.view_for(self.session)
        view._cache_key = -1
        results = []
        for check in self.plan.post_checks:
            try:
                ok, text = self._check(view, check)
            except (RescueError, OSError) as exc:
                ok, text = False, f"{check.get('kind')}: {exc}"
            results.append({"ok": ok, "text": text, "kind": check.get("kind")})
        return results

    def _check(self, view: SessionView, check: dict[str, Any]) -> tuple[bool, str]:
        kind = check["kind"]
        if kind == "file_exists":
            ok = view.exists(check["path"])
            return ok, f"{check['path']} exists" if ok else f"{check['path']} is missing"
        if kind == "file_min_size":
            size = view.size(check["path"])
            ok = size >= check["bytes"]
            return ok, f"{check['path']} is {size / 1024:.1f} KiB" if ok else f"{check['path']} is only {size} bytes (expected at least {check['bytes']})"
        if kind == "grep":
            import re

            text = view.read(check["path"], 1 << 20)
            ok = re.search(check["pattern"], text, re.M) is not None
            return ok, f"{check['path']} contains the expected text" if ok else f"{check['path']} does not contain /{check['pattern']}/"
        if kind == "no_live_args":
            ok = not contains_live_arguments(effective_text(view.read(check["path"], 1 << 20)))
            return ok, f"{check['path']} has no live-medium arguments" if ok else f"{check['path']} still contains live-medium arguments"
        if kind == "esp_file":
            listing = view.listdir(check["dir"])
            ok = any(name.lower() == check["name"].lower() for name in listing)
            return ok, f"{check['dir']}/{check['name']} exists" if ok else f"{check['dir']}/{check['name']} is missing"
        if kind == "efi_entry":
            state = efimod.parse_efibootmgr(*self._efi_text())
            wanted = check.get("loader", "").lower().replace("\\", "/").lstrip("/")
            ok = state.usable and any(wanted in entry.loader.lower().replace("\\", "/") or wanted in entry.label.lower() for entry in state.entries)
            return ok, "the firmware lists a boot entry for this system" if ok else "the firmware does not list a boot entry for this system (the fallback path still works)"
        if kind == "fstab_resolves":
            ignore = set(check.get("ignore", []))
            view._cache.pop("fstab", None)
            view._cache.pop("resolved", None)
            bad = [item for item in resolve_all(view.fstab(), view.graph, crypttab=view.crypttab()) if item.status in {"unresolved", "ambiguous"} and item.entry.lineno not in ignore]
            return not bad, "every fstab entry now points at a disk that exists" if not bad else f"still unresolved: {', '.join(item.entry.file for item in bad)}"
        return False, f"unknown check {kind}"

    def _efi_text(self) -> tuple[str, int]:
        result = self.session.runner.execute(("efibootmgr", "-v"), timeout=20)
        return result.stdout, result.returncode


@dataclass
class EspBackup:
    path: str
    skipped: list[str] = field(default_factory=list)  # 'EFI/HP/capsule.cap (66.0 MiB)': what was left out


def backup_esp(session: RescueSession, mountpoint: str) -> EspBackup:
    """Archive <ESP>/EFI into the repaired system (internal step handler).

    A safety net, not a condition: an ESP with Windows and an OEM's firmware capsules is bigger than any backup should be, and that must not stop a repair.
    Files above ``MAX_BACKUP_FILE`` are left out; of the rest the smallest go in first (boot loaders before payloads) until ``MAX_BACKUP_BYTES`` is full.
    """
    with session.fs(writable=True) as fs:
        files: list[tuple[int, str]] = []
        buffer = io.BytesIO()
        with fs.sub(mountpoint) as esp:
            def walk(directory: str) -> None:
                for name in esp.listdir_or(directory):
                    path = f"{directory.rstrip('/')}/{name}"
                    if esp.is_dir(path) and not esp.is_symlink(path):
                        walk(path)
                    elif esp.is_file(path) and not esp.is_symlink(path):
                        files.append((esp.stat(path).st_size, path))

            walk("/EFI")
            chosen: list[str] = []
            skipped: list[str] = []
            total = 0
            for size, path in sorted(files):
                if size > MAX_BACKUP_FILE or total + size > MAX_BACKUP_BYTES:
                    skipped.append(f"{path.lstrip('/')} ({human_size(size)})")
                else:
                    chosen.append(path)
                    total += size
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                for path in sorted(chosen):
                    data = esp.read_bytes(path, MAX_BACKUP_FILE)
                    info = tarfile.TarInfo(path.lstrip("/"))
                    info.size = len(data)
                    info.mtime = int(session.env.clock())
                    info.mode = 0o644
                    archive.addfile(info, io.BytesIO(data))
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(session.env.clock()))
        result = fs.write_atomic(f"/var/lib/gentoo-rescue/backups/esp-{stamp}.tar.gz", buffer.getvalue(), mode=0o600, uid=0, gid=0, parents=True)
        session.audit.record("file", op="backup-esp", path=result.path, after=result.after_sha256, skipped=skipped, sync=True)
        return EspBackup(result.path, sorted(skipped))
