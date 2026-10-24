"""``gentoo-installer-text``: install Gentoo from a text console (contract I5).

Interactive by default: a wizard of numbered questions that works on a text console, over a serial
line and over SSH. For scripts::

    gentoo-installer-text --manifest FILE --yes --confirm-device /dev/vda [--socket PATH] [--json-events]
    gentoo-installer-text --manifest FILE --dry-run

The installation itself runs in the installer service (``gentoo-installerd``); this program asks
the questions, sends the settings through the service's socket and shows the progress. If the
connection is lost it reconnects, and ``--attach`` follows an installation that is already running.

Exit codes: 0 success, 1 the installation failed or was cancelled, 2 wrong options or an invalid
manifest, 3 refused (the typed confirmation is missing or wrong), 4 the installer service cannot
be reached, 130 interrupted (Ctrl-C, or the input ended).
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

from .. import __version__, daemon_client
from ..daemon_client import DaemonState
from ..manifest import SECRET_KEYS, InstallManifest, ManifestError
from . import EXIT_FAILED, EXIT_INTERRUPTED, EXIT_NO_DAEMON, EXIT_OK, EXIT_REFUSED, EXIT_USAGE
from .conclude import conclude, describe, tell_how_to_resume
from .console import Console, InputClosed, Option, Quit
from .daemon import Daemon, DaemonError
from .follow import InstallFollower, JsonSink, Outcome, TextSink, stage_label
from .host import Host
from .inventory import MachineView, apply_disk
from .summary import build_summary, render_summary
from .wizard import Refused, Wizard

PROGRAM = "gentoo-installer-text"
STARTUP_WAIT = (1.0, 2.0, 2.0, 2.0, 3.0)  # seconds to wait for the service to appear (it may still be starting after boot)
EPILOG = """\
examples:
  gentoo-installer-text                       ask the questions, then install
  gentoo-installer-text --manifest FILE       review a saved settings file and install it
  gentoo-installer-text --manifest FILE --dry-run
                                              check the file and show the plan; nothing is started
  gentoo-installer-text --manifest FILE --yes --confirm-device /dev/vda
                                              install without questions (the device name is the
                                              typed confirmation; the disk is ERASED)
  gentoo-installer-text --attach              follow an installation that is already running

exit codes:
  0 success   1 the installation failed or was cancelled   2 wrong options or invalid settings
  3 refused (confirmation missing or wrong)   4 the installer service cannot be reached
  130 interrupted

With --json-events every event of the installer service is printed as one JSON line on standard
output; everything meant for people goes to standard error.
"""


class _Parser(argparse.ArgumentParser):
    """argparse that writes to the streams it was given (so tests and callers can capture it)."""

    def __init__(self, out: TextIO, err: TextIO, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._out, self._err = out, err

    def _print_message(self, message: str, file: Any = None) -> None:
        if message:
            (self._out if file is sys.stdout else self._err).write(message)


def build_parser(out: TextIO | None = None, err: TextIO | None = None) -> argparse.ArgumentParser:
    parser = _Parser(
        out or sys.stdout, err or sys.stderr, prog=PROGRAM, formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Install Gentoo from a text console: numbered questions, no graphics needed. Nothing on a disk changes until you have read a summary and typed the name of the disk.",
        epilog=EPILOG,
    )
    parser.add_argument("--socket", default=daemon_client.DEFAULT_SOCKET, metavar="PATH", help="socket of the installer service (default: %(default)s)")
    parser.add_argument("--manifest", metavar="FILE", help="installation settings (JSON) to use instead of asking the questions")
    parser.add_argument("--yes", action="store_true", help="with --manifest: install without asking anything (needs --confirm-device)")
    parser.add_argument("--confirm-device", metavar="DEVICE", help="with --yes: the target disk, typed exactly as in the manifest (for example /dev/vda); the disk is erased")
    parser.add_argument("--dry-run", action="store_true", help="with --manifest: check the settings and show the plan; start nothing")
    parser.add_argument("--json-events", action="store_true", help="print the service's events as JSON lines on standard output (with --yes, --dry-run or --attach)")
    parser.add_argument("--resume", action="store_true", help="with --manifest: continue an interrupted installation instead of erasing the disk again")
    parser.add_argument("--attach", action="store_true", help="follow the installation that is running (or show how the last one ended)")
    parser.add_argument("--no-color", action="store_true", help="never use colours (NO_COLOR does the same)")
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {__version__}")
    return parser


def check_options(args: argparse.Namespace) -> str:
    """What is wrong with the combination of options ("" when nothing)."""
    if args.yes and not args.manifest:
        return "--yes needs --manifest FILE"
    if args.dry_run and not args.manifest:
        return "--dry-run needs --manifest FILE"
    if args.confirm_device and not args.yes:
        return "--confirm-device only works together with --yes"
    if args.resume and not args.manifest:
        return "--resume needs --manifest FILE (the settings must be the same as before)"
    if args.attach and (args.manifest or args.yes or args.dry_run or args.resume):
        return "--attach cannot be combined with --manifest, --yes, --dry-run or --resume"
    if args.json_events and not (args.yes or args.dry_run or args.attach):
        return "--json-events needs --yes, --dry-run or --attach: the questions of the wizard are for people"
    return ""


# --- the settings file -----------------------------------------------------------------------------------


class ManifestFileError(Exception):
    """The settings file cannot be used."""


PLACEHOLDER_SECRET = "placeholder-secret-1"


def load_manifest_file(path: Path, *, redacted: str = "keep") -> InstallManifest:
    """Read a manifest. A saved copy has no passwords (``<redacted>``).

    ``redacted`` says what to do with them: "keep" (the manifest then fails validation), "blank" (empty, so that
    they can be asked) or "placeholder" (a valid dummy, so that a dry run can check everything else)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestFileError(f"Cannot read {path}: {exc.strerror or exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestFileError(f"{path} is not valid JSON: {exc.msg} (line {exc.lineno})") from exc
    try:
        manifest = InstallManifest.from_dict(raw)
    except ManifestError as exc:
        raise ManifestFileError(f"{path}: {exc}") from exc
    if redacted != "keep":
        for section in (manifest.storage, manifest.system, manifest.portage, manifest.network, manifest.accounts, manifest.options):
            for key in SECRET_KEYS:
                if getattr(section, key, None) == "<redacted>":
                    setattr(section, key, "" if redacted == "blank" else PLACEHOLDER_SECRET)
    return manifest


# --- the run ---------------------------------------------------------------------------------------------------


@dataclass
class Context:
    args: argparse.Namespace
    console: Console
    host: Host
    daemon: Daemon
    json_out: TextIO | None = None

    @property
    def automatic(self) -> bool:
        """True when nobody is asked anything (--yes, --dry-run, --json-events)."""
        return bool(self.args.yes or self.args.dry_run or self.json_out is not None)

    def sink(self) -> TextSink | JsonSink:
        return JsonSink(self.json_out, self.console) if self.json_out is not None else TextSink(self.console)


def exit_code(outcome: Outcome) -> int:
    return {"success": EXIT_OK, "invalid": EXIT_USAGE, "unreachable": EXIT_NO_DAEMON, "detached": EXIT_INTERRUPTED}.get(outcome.state, EXIT_FAILED)


def connect(ctx: Context, *, ask_retry: bool) -> DaemonState | None:
    """The daemon's state, or None when it cannot be reached (after waiting a little, and offering to try again).

    Right after boot the service may still be starting, so a missing or stale socket is waited for first."""
    delays = iter(STARTUP_WAIT)
    announced = False
    while True:
        state = ctx.daemon.probe()
        if state.reachable:
            return state
        delay = next(delays, None) if state.kind in {"not-running", "stale"} else None
        if delay is not None:
            if not announced:
                ctx.console.say("Waiting for the installer service to start...")
                announced = True
            ctx.host.sleep(delay)
            continue
        ctx.console.error(state.message or "The installer service cannot be reached.")
        if not ask_retry:
            return None
        try:
            ctx.console.menu([Option("Try again")], default=0, back=False, help="The installer service does the real work. On the live disc it starts by itself a moment after boot.")
        except Quit:
            return None


def obtain_view(ctx: Context, proxy: str = "") -> tuple[MachineView, str]:
    """Ask the daemon about this computer; fall back to what can be read without root. Returns (view, "daemon" | "local" | "none")."""
    by_path = ctx.host.by_path_names()
    try:
        reply = ctx.daemon.inspect(proxy)
    except (OSError, DaemonError) as exc:
        ctx.console.warn(f"The installer service could not describe this computer ({exc}). Using what can be read without it.")
    else:
        if "hardware" not in reply:  # an old service: add the probe that works without root
            local = ctx.host.local_inspection()
            reply = {**reply, "hardware": local.get("hardware", {}), "warnings": reply.get("warnings") or local.get("warnings", [])}
        return MachineView(reply, local_by_path=by_path), "daemon"
    try:
        local = ctx.host.local_inspection()
    except Exception as exc:  # noqa: BLE001 - nothing may stop the run just because a probe failed
        ctx.console.warn(f"This computer could not be inspected either ({exc}).")
        return MachineView(local_by_path=by_path), "none"
    return MachineView(local, local_by_path=by_path), "local" if local else "none"


def run_install(ctx: Context, request: dict[str, Any], *, quiet_until: int = 0) -> Outcome:
    follower = InstallFollower(ctx.daemon, ctx.console, ctx.host, ctx.sink(), interactive=not ctx.automatic, quiet_until=quiet_until)
    return follower.run(request)


def start_request(manifest: InstallManifest, resume: bool) -> dict[str, Any]:
    return {"action": "start", "manifest": manifest.to_dict(), "resume": resume}


def attach_request(state: DaemonState) -> tuple[dict[str, Any], int]:
    """The request that replays the whole job, and the sequence number up to which the replay is only summarised."""
    seq = state.status.get("seq")
    return {"action": "attach", "after_seq": 0}, seq if isinstance(seq, int) else 0


def follow_existing(ctx: Context, state: DaemonState) -> int:
    """Follow the job the daemon has (running, or the last one) and conclude."""
    request, quiet_until = attach_request(state)
    outcome = run_install(ctx, request, quiet_until=quiet_until)
    action, outcome = conclude_loop(ctx, outcome, None)
    return exit_code(outcome)


def conclude_loop(ctx: Context, outcome: Outcome, manifest: InstallManifest | None) -> tuple[str, Outcome]:
    """Show how it ended and, when somebody is there to ask, offer restart, resume, the log ...

    Returns the last action ("again" = back to the summary, otherwise "done") and the last outcome.
    ``manifest`` is what a resume sends; without it resuming is not offered."""
    if ctx.automatic:
        describe(ctx.console, outcome)
        tell_how_to_resume(ctx.console, outcome)
        return "done", outcome
    while True:
        action = conclude(ctx.console, ctx.host, ctx.daemon, outcome, can_resume=manifest is not None)
        if action == "resume" and manifest is not None:
            outcome = run_install(ctx, start_request(manifest, True))
        elif action == "attach":
            state = ctx.daemon.probe()
            if not state.reachable:
                return "done", Outcome("unreachable", state.message)
            request, quiet_until = attach_request(state)
            outcome = run_install(ctx, request, quiet_until=quiet_until)
        else:
            return action, outcome


# --- unattended -------------------------------------------------------------------------------------------------


def explain_pin(entry_path: str, kind: str) -> str:
    what = {"serial": "its serial number", "wwn": "its WWN", "path": "its hardware path, size and model"}[kind]
    return f"The manifest did not say which physical disk {entry_path} is; it is pinned to {what}, so the installer refuses to continue if another disk shows up under that name."


def run_dry_run(ctx: Context, manifest: InstallManifest) -> int:
    view, source = obtain_view(ctx, manifest.network.proxy)
    summary = build_summary(manifest, view)
    if not manifest.storage.device_id:
        summary.notes.append("The manifest does not pin the disk to a serial number; a real run pins it before it starts.")
    if PLACEHOLDER_SECRET in (manifest.accounts.password, manifest.storage.luks_passphrase):
        summary.notes.append("The passwords in this file are blanked out (saved copies never keep them); a real run asks for them.")
    if source != "daemon":
        summary.notes.append("The installer service did not answer, so this plan uses only what can be read without it." if source == "local"
                             else "This computer could not be inspected, so only the settings were checked.")
    if ctx.json_out is not None:
        ctx.json_out.write(json.dumps({"type": "dry_run", "ok": summary.ok, "source": source, **summary.to_dict()}, sort_keys=True) + "\n")
        ctx.json_out.flush()
    else:
        ctx.console.heading("Dry run: what would happen")
        render_summary(ctx.console, summary)
        ctx.console.say()
        ctx.console.say("Dry run: nothing was started." if summary.ok else "Dry run: this cannot be installed as it is. Nothing was started.")
    return EXIT_OK if summary.ok else EXIT_USAGE


def run_unattended(ctx: Context, manifest: InstallManifest) -> int:
    console, args = ctx.console, ctx.args
    device = manifest.storage.device
    if not args.confirm_device:
        console.error(f"Refused: --yes erases {device}. Add --confirm-device {device} to confirm that this is the disk you mean.")
        return EXIT_REFUSED
    if args.confirm_device != device:
        console.error(f"Refused: --confirm-device {args.confirm_device} is not the disk in the manifest ({device}). Nothing was changed.")
        return EXIT_REFUSED
    state = connect(ctx, ask_retry=False)
    if state is None:
        return EXIT_NO_DAEMON
    if state.job_running:
        console.error("An installation is already running on this computer. Follow it with: gentoo-installer-text --attach")
        return EXIT_FAILED
    if not manifest.storage.device_id:
        view, _ = obtain_view(ctx, manifest.network.proxy)
        entry = view.find_disk(device)
        if entry is None:
            console.error(f"The disk {device} was not found on this computer, so it cannot be identified. Nothing was changed.")
            return EXIT_USAGE
        if not entry.usable:
            console.error(f"{device} cannot be used: it {entry.problem}. Nothing was changed.")
            return EXIT_USAGE
        try:
            kind = apply_disk(manifest.storage, entry, approve_path=True)  # naming the device with --confirm-device is the approval
        except ValueError as exc:
            console.error(f"{exc} Nothing was changed.")
            return EXIT_REFUSED
        console.note(explain_pin(device, kind))
    outcome = run_install(ctx, start_request(manifest, bool(args.resume)))
    return exit_code(conclude_loop(ctx, outcome, manifest)[1])


# --- interactive -----------------------------------------------------------------------------------------------------


def run_wizard(ctx: Context, manifest: InstallManifest | None = None, *, from_file: bool = False) -> int:
    console = ctx.console
    state = connect(ctx, ask_retry=True)
    if state is None:
        return EXIT_NO_DAEMON
    if state.job_running:
        progress = state.status.get("progress")
        percent = f", {progress:.0f}% done" if isinstance(progress, (int, float)) else ""
        console.para(f"An installation is already running on this computer ({stage_label(str(state.status.get('stage') or ''))}{percent}).")
        console.quit_note = "The installation keeps running in the background. To follow it again, run: gentoo-installer-text --attach"
        console.menu([Option("Watch it")], default=0, back=False)
        return follow_existing(ctx, state)
    console.say("Checking this computer...")
    view, _ = obtain_view(ctx, manifest.network.proxy if manifest else "")
    wizard = Wizard(console, ctx.host, ctx.daemon, manifest=manifest, view=view, state=state, from_file=from_file, resume=bool(ctx.args.resume))
    at_review = from_file
    while True:
        plan = wizard.run(start_at_review=at_review)
        if isinstance(plan, Refused):
            console.error(f"{plan.reason} Nothing was changed.")
            return EXIT_REFUSED
        outcome = run_install(ctx, start_request(wizard.manifest, plan.resume))
        action, outcome = conclude_loop(ctx, outcome, wizard.manifest)
        if action != "again":
            return exit_code(outcome)
        wizard.state = ctx.daemon.probe()
        wizard.refresh_view()
        at_review = True


def run_manifest(ctx: Context) -> int:
    args, console = ctx.args, ctx.console
    unattended = bool(args.yes or args.dry_run)
    try:
        manifest = load_manifest_file(Path(args.manifest), redacted="keep" if args.yes and not args.dry_run else "placeholder" if args.dry_run else "blank")
    except ManifestFileError as exc:
        console.error(str(exc))
        return EXIT_USAGE
    if unattended:
        problems = manifest.validate()
        if problems:
            console.error("The settings are not valid:")
            for problem in problems:
                console.para(f"- {problem}", indent=2, hanging=4)
            return EXIT_USAGE
        return run_dry_run(ctx, manifest) if args.dry_run else run_unattended(ctx, manifest)
    return run_wizard(ctx, manifest, from_file=True)


def run_attach(ctx: Context) -> int:
    state = connect(ctx, ask_retry=False)
    if state is None:
        return EXIT_NO_DAEMON
    status = state.status
    if status.get("status") in {None, "", "idle"} and not status.get("seq"):
        ctx.console.error("No installation has run in this session, so there is nothing to follow.")
        return EXIT_FAILED
    return follow_existing(ctx, state)


def run(ctx: Context) -> int:
    if ctx.args.attach:
        return run_attach(ctx)
    if ctx.args.manifest:
        return run_manifest(ctx)
    return run_wizard(ctx)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    host: Host | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    try:
        args = build_parser(out, err).parse_args(argv)
    except SystemExit as exc:  # --help, --version and wrong options
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    problem = check_options(args)
    if problem:
        err.write(f"{PROGRAM}: error: {problem}\n")
        return EXIT_USAGE
    json_mode = bool(args.json_events)
    console = Console(stdin, err if json_mode else out, environ=environ, color=False if args.no_color else None)
    if stdin is None and console.tty_in:
        try:
            import readline  # makes input() editable (arrow keys, history)

            # A serial terminal may not understand the "bracketed paste" codes that readline sends by default.
            readline.parse_and_bind("set enable-bracketed-paste off")
        except ImportError:
            pass
    ctx = Context(args, console, host if host is not None else Host(), Daemon(args.socket), out if json_mode else None)
    try:
        return run(ctx)
    except Quit:
        console.say(" ".join(part for part in ("You left the installer.", console.quit_note) if part))
        return EXIT_INTERRUPTED
    except InputClosed:
        console.say()
        console.say(" ".join(part for part in ("The input ended, so the installer stops.", console.quit_note) if part))
        return EXIT_INTERRUPTED
    except KeyboardInterrupt:
        console.say()
        console.say(" ".join(part for part in ("Interrupted.", console.quit_note) if part))
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - a console user must not be left with a bare traceback and no hint
        traceback.print_exc(file=err)
        console.error(f"The installer stopped because of an unexpected problem ({type(exc).__name__}: {exc}). Nothing more was done; "
                      "the details above help when you report it. If an installation was started it keeps running: follow it with gentoo-installer-text --attach")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
