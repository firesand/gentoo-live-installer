"""Plans: the only thing that can change a disk. Built by an adapter, shown to the user, confirmed, then executed.

A plan is a list of steps with the exact command each runs. Its digest covers every argument, file operation, the session,
the root file system and the session generation, so a plan that was shown is the plan that runs (or is refused as stale).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import NeedsConfirmation, PlanStale, RescueError

RISKS = ("info", "low", "medium", "high", "dangerous")
BACKUP_PHRASE = "I HAVE A BACKUP"
PLAN_TTL = 600.0
MAX_PLANS = 8


@dataclass
class Step:
    id: str
    scope: str  # host | chroot | file
    display: str
    argv: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    timeout: int = 300
    ok_rc: tuple[int, ...] = (0,)
    on_fail: str = "abort"  # abort | continue
    stdin: str | None = None  # None | "secret:<name>" | "text:<literal>"
    env: dict[str, str] = field(default_factory=dict)
    tools: tuple[str, ...] = ()  # chroot: command names this step may use
    file_op: dict[str, Any] | None = None  # {"op": write|remove|touch|mkdir, "path", "mode", "content", "expect_sha256", "backup"}
    atomic: bool = True  # False: a kill mid-step can leave the system half-written (review R6)
    effect: dict[str, Any] | None = None  # ledger/session bookkeeping applied after the step succeeds
    mountpoint: str = ""  # for mount steps: the in-root mount point, re-validated right before the mount (review R2)
    group: str = ""  # steps that belong to a visible sub-sequence (for example "enter-write")
    handler: str = ""  # internal steps: the named routine the session runs
    when: str = ""  # "" always | "chroot_ok" | "chroot_broken": decided by the chroot smoke test (review R15)
    stdin_template: str = "{secret}"  # how a secret stdin is formed, for example "root:{secret}\n" for chpasswd
    args: dict[str, Any] = field(default_factory=dict)  # arguments of an internal step

    def public(self) -> dict[str, Any]:
        result: dict[str, Any] = {"id": self.id, "scope": self.scope, "display": self.display, "writes": list(self.writes), "timeout": self.timeout, "atomic": self.atomic}
        if self.argv:
            result["argv"] = list(self.argv)
        if self.file_op:
            op = dict(self.file_op)
            if "content" in op:
                op["content_sha256"] = hashlib.sha256(op["content"].encode()).hexdigest()
                op["content"] = op["content"] if len(op["content"]) <= 4096 else op["content"][:4096] + "\n..."
            result["file_op"] = op
        if self.stdin and self.stdin.startswith("secret:"):
            result["secret"] = self.stdin.split(":", 1)[1]
        if self.group:
            result["group"] = self.group
        if self.handler:
            result["handler"] = self.handler
        if self.when:
            result["when"] = self.when
        return result

    def digest_view(self) -> dict[str, Any]:
        view: dict[str, Any] = {"id": self.id, "scope": self.scope, "argv": list(self.argv), "env": self.env, "stdin": self.stdin if not (self.stdin or "").startswith("text:") else "text:" + hashlib.sha256((self.stdin or "").encode()).hexdigest(),
                                "ok_rc": list(self.ok_rc), "on_fail": self.on_fail, "mountpoint": self.mountpoint, "handler": self.handler, "when": self.when,
                                "stdin_template": self.stdin_template, "args": self.args}
        if self.file_op:
            op = dict(self.file_op)
            if "content" in op:
                op["content"] = hashlib.sha256(op["content"].encode()).hexdigest()
            view["file_op"] = op
        return view


@dataclass
class Plan:
    session_id: str
    action: str
    title: str
    summary: str
    risk: str
    steps: list[Step]
    cleanup: list[Step] = field(default_factory=list)
    post_checks: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    preconditions: list[dict[str, Any]] = field(default_factory=list)
    needs: dict[str, Any] = field(default_factory=lambda: {"write": False, "network": False, "uefi_nvram": False, "unmounted_device": None})
    diff: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    secrets_needed: tuple[str, ...] = ()
    root_device: str = ""
    root_uuid: str = ""
    generation: int = 0
    mode: str = "ro"
    next_hints: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # things worth knowing before the plan is confirmed (not warnings)
    id: str = field(default_factory=lambda: "p" + secrets.token_hex(3))
    created: float = field(default_factory=time.time)
    expires: float = 0.0
    digest: str = ""

    def __post_init__(self) -> None:
        if self.risk not in RISKS:
            raise ValueError(f"Unknown risk {self.risk}")
        if not self.expires:
            self.expires = self.created + PLAN_TTL
        self.digest = self.compute_digest()

    def compute_digest(self) -> str:
        document = {
            "action": self.action, "params": self.params, "session": self.session_id, "root": [self.root_device, self.root_uuid], "mode": self.mode, "generation": self.generation,
            "steps": [step.digest_view() for step in self.steps], "cleanup": [step.digest_view() for step in self.cleanup], "risk": self.risk,
        }
        return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

    @property
    def confirm(self) -> dict[str, Any]:
        """What the client must collect before ``rescue.run`` (review: each write action is confirmed separately)."""
        device_name = self.root_device.rsplit("/", 1)[-1]
        return {
            "risk": self.risk, "required": self.risk != "info", "review": self.risk in {"medium", "high", "dangerous"},
            "typed": device_name if self.risk in {"high", "dangerous"} else "", "phrase": BACKUP_PHRASE if self.risk == "dangerous" else "",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.id, "digest": self.digest, "session_id": self.session_id, "action": self.action, "title": self.title, "summary": self.summary, "risk": self.risk,
            "needs": self.needs, "preconditions": self.preconditions, "steps": [step.public() for step in self.steps], "cleanup": [step.public() for step in self.cleanup],
            "post_checks": self.post_checks, "warnings": self.warnings, "notes": self.notes, "diff": self.diff, "confirm": self.confirm, "expires": self.expires, "generation": self.generation,
            "secrets": list(self.secrets_needed), "params": self.params,
        }

    def failed_preconditions(self) -> list[str]:
        return [str(item.get("text", "")) for item in self.preconditions if not item.get("ok", True)]


class PlanStore:
    """Plans the server has shown, awaiting confirmation. A plan runs once."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._plans: dict[str, Plan] = {}

    def add(self, plan: Plan) -> Plan:
        self.purge()
        while len(self._plans) >= MAX_PLANS:
            self._plans.pop(next(iter(self._plans)))
        self._plans[plan.id] = plan
        return plan

    def purge(self) -> None:
        now = self.clock()
        for key in [key for key, plan in self._plans.items() if plan.expires < now]:
            del self._plans[key]

    def get(self, plan_id: str) -> Plan | None:
        return self._plans.get(plan_id)

    def listing(self) -> list[dict[str, Any]]:
        self.purge()
        return [{"plan_id": plan.id, "action": plan.action, "expires": plan.expires} for plan in self._plans.values()]

    def take(self, plan_id: str, digest: str, *, generation: int, root_uuid: str, confirm: bool, typed: str = "", phrase: str = "") -> Plan:
        """Validate a confirmation and remove the plan so it cannot run twice."""
        plan = self._plans.get(plan_id)
        if plan is None:
            raise RescueError("not_found", "Unknown or already used plan; ask for a new plan", plan_id=plan_id)
        if plan.expires < self.clock():
            self._plans.pop(plan_id, None)
            raise RescueError("plan_expired", "This plan expired; ask for a new plan")
        if digest != plan.digest:
            raise RescueError("plan_mismatch", "The plan being confirmed is not the plan that was shown", plan_id=plan_id)
        if plan.generation != generation or (plan.root_uuid and root_uuid and plan.root_uuid != root_uuid):
            self._plans.pop(plan_id, None)
            raise PlanStale("The system changed since this plan was made; ask for a new plan")
        requirement = plan.confirm
        if requirement["required"] and not confirm:
            raise NeedsConfirmation("This plan changes the system and needs confirmation", missing="confirm")
        if requirement["typed"] and typed != requirement["typed"]:
            raise NeedsConfirmation(f"Type {requirement['typed']} to confirm", missing="typed", expected=requirement["typed"])
        if requirement["phrase"] and phrase != requirement["phrase"]:
            raise NeedsConfirmation(f"Type {requirement['phrase']} to confirm", missing="phrase", expected=requirement["phrase"])
        return self._plans.pop(plan_id)
