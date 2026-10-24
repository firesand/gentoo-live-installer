from __future__ import annotations

import os
import queue
import re
import shlex
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from gentoo_installer.manifest import SECRET_KEYS

LogSink = Callable[[str], None]


class RunnerError(RuntimeError):
    def __init__(self, argv: Sequence[str], returncode: int, output: str = "") -> None:
        self.argv = tuple(argv)
        self.returncode = returncode
        self.output = output
        super().__init__(f"Command failed ({returncode}): {format_command(argv)}" + (f": {output[-1000:]}" if output.strip() else ""))


class InstallCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        secret: bool = False,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandResult: ...


def redact_url(text: str) -> str:
    pattern = re.compile(r"https?://[^\s/'\"]+:[^\s/@'\"]+@")
    return pattern.sub("https://<redacted>@", text)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    result = redact_url(text)
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        result = result.replace(secret, "<redacted>")
    for key in SECRET_KEYS | {"passphrase", "secret", "token"}:
        result = re.sub(rf'(?i)("{re.escape(key)}"\s*:\s*")[^"]*("?)', rf"\g<1><redacted>\2", result)
        result = re.sub(rf"(?i)(--?{re.escape(key)}(?:=|\s+))\S+", rf"\g<1><redacted>", result)
    return result


def format_command(argv: Sequence[str], secrets: Iterable[str] = ()) -> str:
    return redact_text(shlex.join([str(arg) for arg in argv]), secrets)


def validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if not argv or any(not isinstance(arg, str) or not arg or "\x00" in arg or "\n" in arg or "\r" in arg for arg in argv):
        raise ValueError("Commands must be non-empty, single-line argument arrays")
    return tuple(argv)


class SubprocessRunner:
    def __init__(self, log: LogSink | None = None, cancel: threading.Event | None = None, secrets: Iterable[str] = ()) -> None:
        self.log = log or (lambda _line: None)
        self.cancel = cancel or threading.Event()
        self.secrets = tuple(secrets)
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    def run_cleanup(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        """Release resources even after the installation cancellation event is set."""
        return SubprocessRunner(self.log, secrets=self.secrets).run(argv, **kwargs)

    def run(self, argv: Sequence[str], *, stdin: str | None = None, secret: bool = False, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> CommandResult:
        args = validate_argv(argv)
        if self.cancel.is_set():
            raise InstallCancelled("Installation cancelled")
        display = "<redacted command>" if secret else format_command(args, self.secrets)
        self.log(f"$ {display}")
        process_env = os.environ.copy()
        if env:
            process_env.update(env)
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=process_env,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
            shell=False,
        )
        with self._lock:
            self._process = process
        lines: list[str] = []
        output_queue: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                output_queue.put(line)
            output_queue.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        if stdin is not None:
            try:
                assert process.stdin is not None
                process.stdin.write(stdin)
                process.stdin.close()
            except BrokenPipeError:
                pass
        finished = False
        try:
            while not finished or process.poll() is None:
                if self.cancel.is_set() and process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                    raise InstallCancelled("Installation cancelled")
                try:
                    line = output_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    finished = True
                    continue
                clean = line.rstrip("\n")
                lines.append(clean)
                self.log(redact_text(clean, self.secrets) if not secret else "<redacted output>")
            returncode = process.wait()
            reader.join(timeout=1)
            output = "\n".join(lines)
            if returncode:
                raise RunnerError(args if not secret else ("<redacted>",), returncode, redact_text(output, self.secrets) if not secret else "")
            return CommandResult(args, returncode, "<redacted output>" if secret else output)
        finally:
            with self._lock:
                self._process = None
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()


class DryRunRunner:
    def __init__(self, log: LogSink | None = None) -> None:
        self.log = log or (lambda _line: None)
        self.commands: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str], *, stdin: str | None = None, secret: bool = False, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> CommandResult:
        args = validate_argv(argv)
        self.commands.append(args)
        self.log("$ [dry-run] " + ("<redacted command>" if secret else format_command(args)))
        return CommandResult(args, 0, "")
