"""The only way rescue code starts a process.

``RescueRunner`` wraps the daemon's ``Runner`` (``SubprocessRunner`` in production, a recording
fake in tests). Every command passes the host policy before a process exists, is audited with a
redacted command line, and can be given a timeout (enforced with coreutils ``timeout`` so a dying
disk cannot hang the daemon). Commands inside a target are built here too: they always run as
root:root with no supplementary groups, umask 022, cwd ``/``, a clean environment and the
distribution's own directories first on ``PATH`` (review R5, R21).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from gentoo_installerd.runner import CommandResult, InstallCancelled, Runner, RunnerError, format_command

from .audit import AuditLog, NullAudit, tail
from .errors import RescueError, SafetyViolation
from .safety import CHROOT_CREDENTIALS, CHROOT_PATH, CHROOT_WRAPPER_ARGV, ChrootPolicy, HostPolicy, PolicyContext

DEFAULT_CHROOT_ENV = (
    ("PATH", CHROOT_PATH), ("HOME", "/root"), ("LANG", "C"), ("LC_ALL", "C"), ("TERM", "xterm"), ("SHELL", "/bin/sh"),
)
KILL_AFTER = 5


@dataclass
class StepScope:
    """Plan/step labels copied into audit records."""

    plan: str = ""
    step: str = ""


@dataclass
class RescueRunner:
    base: Runner
    ctx: PolicyContext
    audit: AuditLog = field(default_factory=NullAudit)
    esp_mountpoints: tuple[str, ...] = ()
    scope: StepScope = field(default_factory=StepScope)

    def __post_init__(self) -> None:
        self.host = HostPolicy(self.ctx)
        self.chroot_policy = ChrootPolicy(self.ctx, esp_mountpoints=self.esp_mountpoints)

    # ---- host commands ----------------------------------------------------------
    def execute(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        secret: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        label: str = "host",
    ) -> CommandResult:
        """Run a validated host command and return its result without raising on a non-zero exit."""
        args = tuple(argv)
        try:
            kind = self.host.validate(args)
        except RescueError as exc:
            self.audit.record("refused", argv=self._shown(args, secret), reason=exc.message, sync=True)
            raise
        actual = args
        if timeout:
            actual = ("timeout", f"--kill-after={KILL_AFTER}", str(timeout), *args)
        started = time.monotonic()
        try:
            result = self.base.run(actual, stdin=stdin, secret=secret, env=env)
            code, output = result.returncode, result.stdout
        except RunnerError as exc:
            code, output = exc.returncode, exc.output
            result = CommandResult(actual, code, output)
        except InstallCancelled as exc:  # a forced cancel killed the command (or was already pending)
            self.audit.record("cancel", argv=self._shown(args, secret), plan=self.scope.plan, step=self.scope.step, sync=True)
            raise RescueError("cancelled", "The command was stopped. Programs such as dracut or grub-install may have left files half-written.") from exc
        record = {"scope": label, "class": kind, "argv": self._shown(args, secret), "rc": code, "duration": round(time.monotonic() - started, 2),
                  "plan": self.scope.plan, "step": self.scope.step, "output_tail": "" if secret else tail(output)}
        if stdin is not None:
            record["stdin"] = "<secret>" if secret else "<text>"
        self.audit.record("exec", **record)
        return CommandResult(args, code, output)

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        secret: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        ok_rc: Iterable[int] = (0,),
        code: str = "command_failed",
        label: str = "host",
    ) -> CommandResult:
        """Like ``execute`` but raise ``RescueError(command_failed)`` unless the exit code is accepted."""
        result = self.execute(argv, stdin=stdin, secret=secret, env=env, timeout=timeout, label=label)
        if result.returncode not in set(ok_rc):
            shown = "<redacted command>" if secret else format_command(argv)
            raise RescueError(code, f"{shown} failed with exit code {result.returncode}" + ("" if secret or not result.stdout.strip() else f": {tail(result.stdout, 400)}"),
                              rc=result.returncode, argv=[] if secret else list(argv), output=tail(result.stdout, 600) if not secret else "")
        return result

    def try_run(self, argv: Sequence[str], **kwargs: object) -> CommandResult | None:
        """Run a command that is allowed to fail; None when it did."""
        try:
            return self.run(argv, **kwargs)  # type: ignore[arg-type]
        except RescueError as exc:
            if exc.short in {"safety_violation", "live_medium"}:
                raise
            return None

    # ---- commands inside the target --------------------------------------------------
    def chroot_argv(self, command: Sequence[str], *, extra_env: Mapping[str, str] | None = None) -> tuple[str, ...]:
        root = self.ctx.session_root
        if not root:
            raise SafetyViolation("No session root is mounted")
        env = dict(DEFAULT_CHROOT_ENV)
        if extra_env:
            env.update(extra_env)
        pairs = [f"{key}={value}" for key, value in env.items()]
        return (*CHROOT_WRAPPER_ARGV, *CHROOT_CREDENTIALS, root, "/usr/bin/env", "-i", *pairs, *command)

    def chroot(
        self,
        command: Sequence[str],
        *,
        tools: Iterable[str],
        extra_env: Mapping[str, str] | None = None,
        stdin: str | None = None,
        secret: bool = False,
        timeout: int | None = None,
        ok_rc: Iterable[int] = (0,),
        check: bool = True,
    ) -> CommandResult:
        try:
            self.chroot_policy.validate(command, tools)
        except RescueError as exc:
            self.audit.record("refused", argv=self._shown(tuple(command), secret), reason=exc.message, sync=True)
            raise
        argv = self.chroot_argv(command, extra_env=extra_env)
        if check:
            return self.run(argv, stdin=stdin, secret=secret, timeout=timeout, ok_rc=ok_rc, label="chroot")
        return self.execute(argv, stdin=stdin, secret=secret, timeout=timeout, label="chroot")

    # ---- helpers --------------------------------------------------------------------
    @staticmethod
    def _shown(argv: Sequence[str], secret: bool) -> list[str]:
        return ["<redacted>"] if secret else [str(item) for item in argv]
