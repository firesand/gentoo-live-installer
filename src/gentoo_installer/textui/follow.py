"""Follow an installation: progress on the screen, reconnecting, and what to do afterwards.

``InstallFollower`` sends ``start`` (or ``attach``) to the daemon and shows the events: the step,
the overall percentage (``Event.progress`` is always the overall percent, contract I1) and the
log lines. The daemon runs the installation on its own, so a lost connection is not the end of
it: the follower asks for the status and re-attaches with ``attach {after_seq}``. Ctrl-C asks
whether to keep watching, stop watching (the installation goes on) or cancel it.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, TextIO

from ..daemon_client import event_data
from ..layout import human_size
from .console import Console, GoBack, Option, Quit, clip, sanitize
from .daemon import Daemon
from .host import Host

DEFAULT_LOG_PATH = "/var/lib/gentoo-installer/install.log"
STAGE_ORDER = (
    "preflight", "storage", "stage3", "mounts", "repository", "portage", "kernel", "target",
    "base", "bootloader", "desktop", "validation", "cleanup",
)
STAGE_LABELS = {
    "preflight": "Checking the installation environment",
    "storage": "Preparing disks and filesystems",
    "stage3": "Downloading and verifying Gentoo",
    "mounts": "Preparing the new system",
    "repository": "Fetching the Portage tree",
    "portage": "Configuring Portage",
    "kernel": "Installing firmware and the kernel",
    "target": "Configuring accounts and network",
    "base": "Installing the base system",
    "bootloader": "Installing the bootloader",
    "desktop": "Installing the desktop and applications",
    "validation": "Checking the installed system",
    "cleanup": "Finishing and unmounting",
    "packages": "Installing applications",  # daemons that still have one packages stage
}
# Used only when an event carries no overall percentage (it always should, contract I1).
STAGE_WEIGHTS = {
    "preflight": 2, "storage": 8, "stage3": 12, "mounts": 1, "repository": 5, "portage": 2,
    "kernel": 20, "target": 2, "base": 28, "bootloader": 6, "desktop": 12, "validation": 2, "cleanup": 0,
}
MERGE_LISTING = re.compile(r"^(?:>>>|---|<<<) /")
RECONNECT_DELAYS = (1.0, 2.0, 4.0, 5.0, 5.0, 5.0)
REPLAY_TAIL = 12
DOWNLOAD_STEP = 10  # percent between two "downloaded" lines
WAIT_FOR_OLD_SERVICE = 2000  # polls of 5 seconds (almost three hours) for a service that cannot re-attach


def stage_label(stage: str) -> str:
    return STAGE_LABELS.get(stage, stage.replace("_", " ").capitalize() or "Working")


@dataclass
class Outcome:
    """How a followed installation ended."""

    state: str  # success | failed | cancelled | busy | invalid | detached | unreachable
    message: str = ""
    code: str = ""
    stage: str = ""
    resumable: bool = False
    bootable: bool = False
    retryable: bool = False
    degraded: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    log_path: str = ""
    last_seq: int = 0
    percent: float = 0.0

    @property
    def ok(self) -> bool:
        return self.state == "success"


# --- what is shown --------------------------------------------------------------------------------


class TextSink:
    """Events as readable lines. Output is plain scrolling text: nothing is redrawn, so it works on any terminal."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.percent = 0.0
        self._stage = ""
        self._last_message = ""
        self._last_package = 0
        self._last_download = -DOWNLOAD_STEP
        self._skipped_replay = 0
        self._replay: Deque[str] = deque(maxlen=REPLAY_TAIL)
        self._merged = 0

    # the follower calls these
    def note(self, text: str) -> None:
        self.console.para(text)

    def warn(self, text: str) -> None:
        self.console.warn(text)

    def event(self, event: dict[str, Any], *, replay: bool = False) -> None:
        kind = event.get("type")
        data = event_data(event)
        message = str(event.get("message") or "")
        progress = event.get("progress")
        if isinstance(progress, (int, float)):
            self.percent = max(self.percent, min(100.0, float(progress)))
        if kind == "log":
            self._log(message, replay)
            return
        if replay and kind == "progress":
            return  # the history of an installation that is already under way: steps and problems only
        self.flush_replay()
        if kind == "stage":
            self._stage_event(event, data)
        elif kind == "progress":
            self._progress_event(event, data, message)
        elif kind == "status":
            if data.get("warning"):
                self.console.warn(message or "The installer reported a warning.")
            elif data.get("status") == "running" and not self._stage:
                self.console.say("The installation has started.")
        elif kind == "error":
            where = f" ({stage_label(str(event.get('stage')))})" if event.get("stage") else ""
            self.console.error(f"{message or event.get('error') or 'The installation reported an error.'}{where}")

    def flush_replay(self) -> None:
        if self._skipped_replay or self._replay:
            if self._skipped_replay:
                self.console.say(f"... {self._skipped_replay} earlier log lines are not shown (the log has them) ...")
            for line in self._replay:
                self.console.say(line)
        self._skipped_replay = 0
        self._replay.clear()

    # rendering
    def _prefix(self) -> str:
        return f"[{self.percent:3.0f}%]"

    def _stage_event(self, event: dict[str, Any], data: dict[str, Any]) -> None:
        stage = str(event.get("stage") or "")
        status = str(data.get("status") or "")
        if status in {"completed"}:
            return
        label = stage_label(stage)
        if status == "skipped":
            self.console.say(f"{self._prefix()} {label} (already done)")
            return
        self._stage = stage
        self._last_package = 0
        self._last_download = -DOWNLOAD_STEP
        self.console.say(self.console.style(f"{self._prefix()} {label}", "bold"))

    def _progress_event(self, event: dict[str, Any], data: dict[str, Any], message: str) -> None:
        package, packages = data.get("package"), data.get("packages")
        if isinstance(package, int) and isinstance(packages, int) and packages:
            if package != self._last_package:
                self._last_package = package
                self.console.say(f"{self._prefix()}   Package {package} of {packages}")
            return
        downloaded, total = data.get("downloaded"), data.get("total")
        if isinstance(downloaded, (int, float)) and isinstance(total, (int, float)) and total > 0:
            percent = int(downloaded * 100 / total)
            if percent >= self._last_download + DOWNLOAD_STEP or percent == 100:
                self._last_download = percent
                self.console.say(f"{self._prefix()}   Downloaded {human_size(int(downloaded))} of {human_size(int(total))}")
            return
        if message and message != self._last_message and "inspection" not in data:
            self._last_message = message
            self.console.say(f"{self._prefix()}   {clip(message, self.console.width - 9)}")

    def _log(self, message: str, replay: bool) -> None:
        for line in sanitize(message).rstrip().split("\n"):
            if MERGE_LISTING.match(line):
                self._merged += 1
                continue
            text = clip(line.rstrip(), self.console.width)
            lowered = line.lower()
            if lowered.startswith(("error", "!!!", "fatal")):
                text = self.console.style(text, "red")
            elif lowered.startswith("warning"):
                text = self.console.style(text, "yellow")
            if replay:
                if len(self._replay) == self._replay.maxlen:
                    self._skipped_replay += 1
                self._replay.append(text)
            else:
                self.flush_replay()
                self.console.say(text)


class JsonSink:
    """Every event as one JSON line on the machine-readable stream (--json-events)."""

    def __init__(self, stream: TextIO, console: Console) -> None:
        self.stream = stream
        self.console = console
        self.percent = 0.0

    def note(self, text: str) -> None:
        self.console.para(text)

    def warn(self, text: str) -> None:
        self.console.warn(text)

    def event(self, event: dict[str, Any], *, replay: bool = False) -> None:
        self.stream.write(json.dumps(event, sort_keys=True) + "\n")
        self.stream.flush()

    def flush_replay(self) -> None:
        return None


# --- following ------------------------------------------------------------------------------------


@dataclass
class _Run:
    """What has been seen of the job so far."""

    last_seq: int = 0
    percent: float = 0.0
    stage: str = ""
    job_id: str = ""
    errors: list[dict[str, Any]] = field(default_factory=list)
    completed: dict[str, Any] | None = None
    degraded: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    events: int = 0


class InstallFollower:
    """Run ``start`` or ``attach`` and show the installation until the daemon says it is over."""

    def __init__(self, daemon: Daemon, console: Console, host: Host, sink: TextSink | JsonSink, *, interactive: bool = True, quiet_until: int = 0) -> None:
        self.daemon = daemon
        self.console = console
        self.host = host
        self.sink = sink
        self.interactive = interactive
        self.quiet_until = quiet_until
        self.run_state = _Run()

    # --- the loop --------------------------------------------------------------------------------

    def run(self, request: dict[str, Any]) -> Outcome:
        # From here on "Nothing has been changed on your disks" would be untrue.
        self.console.quit_note = "The installation keeps running in the background. To follow it again, run: gentoo-installer-text --attach"
        outcome = self._run(request)
        if outcome.state != "detached":
            self.console.quit_note = "" if outcome.ok else "The installation stopped; the disk may be partly installed (the log says how far it got)."
        return outcome

    def _run(self, request: dict[str, Any]) -> Outcome:
        self.run_state = _Run()
        failures = 0
        final_attach = False
        while True:
            received = 0
            silent = False
            try:
                for event in self.daemon.stream(request):
                    received += 1
                    failures = 0
                    if self._handle(event):
                        return self._finish()
            except KeyboardInterrupt:
                self.console.say()
                if self._interrupted() == "detach":
                    return self._partial("detached", "You stopped watching. The installation is still running.")
                request = self._attach_request()
                continue
            except OSError as exc:
                silent = isinstance(exc, TimeoutError)  # connected, but nothing was reported for a long time
            verdict = self._after_drop(request, received, failures, final_attach, silent)
            if isinstance(verdict, Outcome):
                return verdict
            failures, final_attach = verdict
            request = self._attach_request()

    def _attach_request(self) -> dict[str, Any]:
        return {"action": "attach", "after_seq": self.run_state.last_seq}

    def _after_drop(self, request: dict[str, Any], received: int, failures: int, final_attach: bool, silent: bool = False) -> Outcome | tuple[int, bool]:
        """The stream ended without a ``completed`` event: find out what the daemon is doing."""
        state = self.daemon.probe()
        if state.reachable:
            job = state.job_status
            if job == "running" and not state.supports("attach.v1"):
                return self._wait_for_old_service()
            if job == "running" and silent:
                self.sink.note(f"Nothing has been reported for {self.daemon.read_timeout:.0f} seconds; the installation is still running (long builds are quiet). Waiting on...")
                return 0, final_attach
            if job == "running":
                self.sink.note("The connection to the installer service was lost, but the installation is still running. Reconnecting...")
                self.host.sleep(1.0)
                return 0, final_attach
            if request.get("action") == "start" and not received and job in {"", "idle"}:
                return self._partial("failed", "The installer service closed the connection before it started the installation.", code="proto.closed")
            if not final_attach and state.supports("attach.v1"):
                return 0, True  # the job is over: one more attach delivers the events we missed, including the last one
            return self._partial("failed", str(state.status.get("error") or "The installer service stopped reporting before the installation finished."))
        if failures >= len(RECONNECT_DELAYS):
            return self._partial("unreachable", state.message or "The installer service cannot be reached.")
        delay = RECONNECT_DELAYS[failures]
        self.sink.note(f"Cannot reach the installer service ({state.message or 'no answer'}). Trying again in {delay:.0f} seconds...")
        self.host.sleep(delay)
        return failures + 1, final_attach

    def _wait_for_old_service(self) -> Outcome:
        """A service without ``attach`` cannot replay events: wait until its status says the job is over."""
        self.sink.note("The connection was lost. This installer service cannot send the missed progress again, so waiting for the installation to end...")
        waited = 0
        while waited < WAIT_FOR_OLD_SERVICE:
            self.host.sleep(5.0)
            waited += 1
            state = self.daemon.probe()
            if state.reachable and state.job_status != "running":
                status = state.status
                if state.job_status == "completed":
                    return Outcome("success", "The installation finished.", percent=100.0, bootable=True, log_path=str(status.get("log_path") or DEFAULT_LOG_PATH),
                                   degraded=list(self.run_state.degraded), warnings=list(self.run_state.warnings))
                outcome = self._partial("cancelled" if state.job_status == "cancelled" else "failed", str(status.get("error") or "The installation did not finish."))
                outcome.resumable = bool(status.get("resumable"))
                return outcome
        return self._partial("unreachable", "The installer service did not report an end of the installation.")

    # --- one event ----------------------------------------------------------------------------------

    def _handle(self, event: dict[str, Any]) -> bool:
        """Show one event. True when it is the job's ``completed`` event."""
        run = self.run_state
        kind = event.get("type")
        seq = event.get("seq")
        if isinstance(seq, int):
            if seq <= run.last_seq and kind != "completed":
                return False  # replayed after a reconnect
            run.last_seq = max(run.last_seq, seq)
        run.events += 1
        data = event_data(event)
        if event.get("job_id"):
            run.job_id = str(event["job_id"])
        progress = event.get("progress")
        if isinstance(progress, (int, float)):
            run.percent = max(run.percent, min(100.0, float(progress)))
        if event.get("stage") and kind in {"stage", "progress"}:
            run.stage = str(event["stage"])
        replay = isinstance(seq, int) and seq <= self.quiet_until
        self.sink.event(event, replay=replay)
        if kind == "error":
            run.errors.append(event)
        elif kind == "completed":
            run.completed = event
            self.sink.flush_replay()
            return True
        elif kind == "progress":
            for item in data.get("degraded") or []:
                if str(item) not in run.degraded:
                    run.degraded.append(str(item))
        elif kind == "status" and data.get("warning") and event.get("message"):
            run.warnings.append(str(event["message"]))
        return False

    # --- Ctrl-C ---------------------------------------------------------------------------------------

    def _interrupted(self) -> str:
        """The user pressed Ctrl-C while watching: 'watch' (go on), 'detach' (stop watching)."""
        if not self.interactive:
            return "detach"
        console = self.console
        console.para("The installation runs inside the installer service, so it keeps going even if this program stops.")
        try:
            choice = console.menu(
                [
                    Option("Keep watching"),
                    Option("Stop watching; the installation continues in the background"),
                    Option("Cancel the installation", detail="It stops at the next safe point. A disk that was already changed stays partly installed; you can resume or start over."),
                ],
                default=0, back=False, prompt="What do you want to do",
            )
        except (KeyboardInterrupt, GoBack):
            return "watch"
        except Quit:
            return "detach"
        if choice.index == 1:
            return "detach"
        if choice.index == 2 and console.confirm("Really cancel the installation?", default=False, back=False):
            accepted, text = self.daemon.cancel()
            console.say(text if accepted else f"The cancellation was not accepted: {text}")
        return "watch"

    # --- the result -------------------------------------------------------------------------------------

    def _partial(self, state: str, message: str, *, code: str = "") -> Outcome:
        run = self.run_state
        return Outcome(state, message, code=code, stage=run.stage, last_seq=run.last_seq, percent=run.percent,
                       degraded=list(run.degraded), warnings=list(run.warnings), log_path=self._log_path())

    def _log_path(self) -> str:
        try:
            return str(self.daemon.status().get("log_path") or "") or DEFAULT_LOG_PATH
        except OSError:
            return DEFAULT_LOG_PATH

    def _finish(self) -> Outcome:
        run = self.run_state
        done = event_data(run.completed or {})
        failing = run.errors[-1] if run.errors else {}
        detail = event_data(failing)
        degraded = list(dict.fromkeys([*run.degraded, *[str(item) for item in done.get("degraded") or []]]))
        warnings = list(dict.fromkeys([*run.warnings, *[str(item) for item in done.get("warnings") or []]]))
        outcome = Outcome("success", str((run.completed or {}).get("message") or ""), last_seq=run.last_seq, percent=run.percent,
                          degraded=degraded, warnings=warnings, log_path=self._log_path())
        if done.get("success") is True:
            outcome.percent = 100.0
            outcome.bootable = True
            return outcome
        code = str(detail.get("code") or "")
        outcome.state = {"install.cancelled": "cancelled", "install.busy": "busy", "install.invalid_manifest": "invalid"}.get(code, "failed")
        outcome.code = code
        outcome.stage = str(failing.get("stage") or run.stage)
        outcome.message = str(failing.get("error") or failing.get("message") or (run.completed or {}).get("message") or "The installation failed.")
        outcome.retryable = bool(detail.get("retryable"))
        outcome.resumable = bool(detail.get("resumable") or done.get("resumable"))
        outcome.bootable = bool(detail.get("bootable") or done.get("bootable"))
        if not outcome.resumable:
            outcome.resumable = self._status_flag("resumable")
        return outcome

    def _status_flag(self, name: str) -> bool:
        try:
            return bool(self.daemon.status().get(name))
        except OSError:
            return False
