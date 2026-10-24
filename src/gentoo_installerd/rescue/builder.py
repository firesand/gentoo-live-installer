"""PlanBuilder: what an adapter or an action uses to describe a repair as an ordered, reviewable list of steps."""

from __future__ import annotations

import re
import shlex
from typing import Any, Iterable, Sequence

from .errors import PreconditionFailed, RescueError, Unsupported
from .hostenv import Environment
from .plan import Plan, Step
from .view import SessionView

SMOKE_ID = "smoke"
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}\$?$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]{0,80}$")
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:@/=-]{0,120}$")


class PlanBuilder:
    def __init__(self, view: SessionView, action: str, params: dict[str, Any], *, environment: Environment | None = None, efi_state: Any = None, clock: Any = None) -> None:
        self.view = view
        self.session = view._session
        self.runner = self.session.runner
        self.action = action
        self.params = dict(params)
        self.env = environment or view.environment
        self.efi_state = efi_state
        self.root = self.session.root
        self.title = action
        self.summary = ""
        self.risk = "medium"
        self.steps: list[Step] = []
        self.cleanup: list[Step] = []
        self.post_checks: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.preconditions: list[dict[str, Any]] = []
        self.needs: dict[str, Any] = {"write": True, "network": False, "uefi_nvram": False, "unmounted_device": None}
        self.diff = ""
        self.hints: list[str] = []
        self.secrets: tuple[str, ...] = ()
        self.uses_chroot = False
        self.chroot_tools: set[str] = set()
        self._n = 0

    # ---- step helpers ------------------------------------------------------------------------------------------------------
    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def host(self, argv: Sequence[str], *, display: str = "", writes: Iterable[str] = (), timeout: int = 120, ok_rc: Sequence[int] = (0,), on_fail: str = "abort", atomic: bool = True,
             when: str = "", cleanup: bool = False, effect: dict[str, Any] | None = None) -> Step:
        step = Step(self._id("h"), "host", display or shlex.join(argv), tuple(argv), tuple(writes), timeout, tuple(ok_rc), on_fail, atomic=atomic, when=when, effect=effect)
        (self.cleanup if cleanup else self.steps).append(step)
        return step

    def chroot(self, argv: Sequence[str], *, tools: Iterable[str] | None = None, display: str = "", writes: Iterable[str] = (), timeout: int = 900, env: dict[str, str] | None = None,
               ok_rc: Sequence[int] = (0,), on_fail: str = "abort", atomic: bool = False, when: str = "", stdin: str | None = None, stdin_template: str = "{secret}",
               cleanup: bool = False) -> Step:
        self.uses_chroot = True
        names = tuple(tools) if tools else (argv[0],)
        self.chroot_tools.update(names)
        step = Step(self._id("c"), "chroot", display or f"chroot {self.root} {shlex.join(argv)}", tuple(argv), tuple(writes), timeout, tuple(ok_rc), on_fail, stdin=stdin, env=env or {},
                    tools=names, atomic=atomic, when=when, stdin_template=stdin_template)
        (self.cleanup if cleanup else self.steps).append(step)
        return step

    def file(self, op: dict[str, Any], display: str, *, writes: Iterable[str] = (), cleanup: bool = False, group: str = "") -> Step:
        step = Step(self._id("f"), "file", display, writes=tuple(writes), file_op=op, group=group)
        (self.cleanup if cleanup else self.steps).append(step)
        return step

    def internal(self, handler: str, display: str, args: dict[str, Any] | None = None, *, writes: Iterable[str] = (), on_fail: str = "abort", cleanup: bool = False) -> Step:
        step = Step(self._id("i"), "internal", display, writes=tuple(writes), handler=handler, args=args or {}, on_fail=on_fail)
        (self.cleanup if cleanup else self.steps).append(step)
        return step

    def write_file(self, path: str, content: str, *, mode: int | None = None, backup: bool = True, display: str = "", create: bool = True) -> Step:
        """Replace a file; the digest of what the plan was made from is checked again before writing (review R7)."""
        previous = self.view.sha256(path) if self.view.exists(path) else ""
        op = {"op": "write", "path": path, "content": content, "backup": backup, "create": create, "expect_sha256": previous}
        if mode is not None:
            op["mode"] = mode
        return self.file(op, display or f"write {path}", writes=(path,))

    # ---- annotations ------------------------------------------------------------------------------------------------------------
    def precondition(self, ok: bool, text: str) -> None:
        self.preconditions.append({"ok": bool(ok), "text": text})

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)

    def check(self, kind: str, **fields: Any) -> None:
        self.post_checks.append({"kind": kind, **fields})

    # ---- validation of parameters (review R11) --------------------------------------------------------------------------------------
    def choice(self, name: str, allowed: Iterable[str], default: str = "") -> str:
        value = str(self.params.get(name, default))
        if value not in set(allowed) and value != default:
            raise RescueError("bad_request", f"{name} must be one of {', '.join(sorted(set(allowed)))}", param=name)
        return value

    def versions(self, name: str, known: Iterable[str]) -> list[str]:
        """Kernel versions the user selected, each of which must be one the system really has."""
        selected = self.params.get(name) or []
        if not isinstance(selected, list):
            raise RescueError("bad_request", f"{name} must be a list", param=name)
        allowed = set(known)
        for item in selected:
            if not isinstance(item, str) or not VERSION_RE.match(item) or item not in allowed:
                raise RescueError("bad_request", f"{item!r} is not one of the installed kernels", param=name)
        return list(dict.fromkeys(selected))

    # ---- assembling the plan -------------------------------------------------------------------------------------------------------
    def build(self) -> Plan:
        if any(not item["ok"] for item in self.preconditions) and self.params.get("_strict"):
            raise PreconditionFailed([item["text"] for item in self.preconditions if not item["ok"]])
        steps = list(self.steps)
        prefix: list[Step] = []
        session = self.session
        writes = self.needs.get("write", True)
        if writes and session.mode == "ro":
            prefix = session.enter_write_steps(with_virtual=self.uses_chroot)
        elif self.uses_chroot and not session.has_virtual_mounts():
            prefix = session.virtual_steps("virtual")
        if self.uses_chroot:
            smoke = Step(SMOKE_ID, "chroot", f"chroot {self.root} true   (checks that the system can run its own programs)", ("true",), tools=("true",), timeout=60, on_fail="continue", atomic=True)
            prefix.append(smoke)
        suffix: list[Step] = []
        selinux, _kind = self.view.selinux()
        if writes and selinux and steps:
            suffix.append(Step(self._id("f"), "file", "create /.autorelabel (SELinux relabels the files on the next boot)", file_op={"op": "touch", "path": "/.autorelabel", "mode": 0o644}, writes=("/.autorelabel",)))
            self.warn("This system uses SELinux: the next boot relabels the files and takes longer.")
        all_steps = prefix + steps + suffix
        if not all_steps:
            raise Unsupported("There is nothing to do for this action on this system")
        self.needs["chroot"] = self.uses_chroot
        return Plan(session_id=session.sid, action=self.action, title=self.title, summary=self.summary, risk=self.risk, steps=all_steps, cleanup=self.cleanup, post_checks=self.post_checks,
                    warnings=self.warnings, preconditions=self.preconditions, needs=self.needs, diff=self.diff, params=self._public_params(), secrets_needed=self.secrets,
                    root_device=self.view.root_device, root_uuid=self.view.root_uuid, generation=session.generation, mode=session.mode, next_hints=self.hints)

    def _public_params(self) -> dict[str, Any]:
        return {key: value for key, value in self.params.items() if not key.startswith("_") and key not in {"secrets", "password", "passphrase"}}
