"""``gentoo-rescue-mount [--writable] [--unmount]``: open the broken Gentoo system at ``/mnt/gentoo``, read-only first.

A thin wrapper over the rescue engine (the same code as ``gentoo-rescue``): scan, ask for the passphrase of every locked
(LUKS) disk (typed here, as root, hidden), activate LVM and RAID, choose the Gentoo system, mount it by its own fstab and
only read-only, then link the session root at ``/mnt/gentoo`` (the Handbook's path). Run it as root on tty1: the passphrase is
never typed into an agent.

``--writable`` goes on to the engine's own plan and typed confirmation for read-write; without it the system stays read-only
until somebody confirms a repair plan (``gentoo-rescue run``). ``--unmount`` releases everything and removes the link.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from gentoo_installerd.rescue import cli
from gentoo_installerd.rescue.client import RescueClient, RescueClientError, Transport

from . import TARGET_LINK

MAX_ROUNDS = 8  # unlock / activate rounds: a nested stack (RAID in LUKS in LVM) needs a few, never an endless loop


def parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gentoo-rescue-mount", description="Open the Gentoo system on this PC at /mnt/gentoo, read-only first.")
    parser.add_argument("--writable", action="store_true", help="afterwards ask the repair engine for read-write (its plan and typed confirmation)")
    parser.add_argument("--unmount", action="store_true", help="release the system and remove /mnt/gentoo")
    parser.add_argument("--system", help="system id from 'gentoo-rescue scan' (default: the one Gentoo system, or a menu)")
    return parser.parse_args(list(argv))


def cli_args(*argv: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(list(argv))


def make_link(link: str, target: str) -> str:
    """Point ``link`` at the session root. Returns "" or a sentence saying why it could not be done (a real folder with content is never touched)."""
    path = Path(link)
    try:
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            path.rmdir()  # only an empty folder
        elif path.exists():
            return f"{link} exists and is not a link or an empty folder; the system is at {target}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
    except OSError as exc:
        return f"could not create {link} ({exc.strerror or exc}); the system is at {target}"
    return ""


def remove_link(link: str, base_dir: str) -> None:
    """Remove our link and put back the empty folder the live system had there: /mnt/gentoo is also the installer's mount point."""
    path = Path(link)
    if path.is_symlink() and os.path.realpath(path).startswith(os.path.realpath(base_dir) + os.sep):
        path.unlink()
    if not path.exists() and not path.is_symlink() and path.parent.is_dir():
        try:
            path.mkdir(mode=0o755)
        except OSError:
            pass


def sessions_of(ctx: cli.Context) -> list[dict[str, Any]]:
    return list(ctx.client.result("rescue.session", session_id="").get("sessions", []))


def open_stack(ctx: cli.Context) -> dict[str, Any]:
    """Scan, then unlock locked disks and activate LVM/RAID the person agrees to, until nothing more can be opened."""
    scan: dict[str, Any] = ctx.client.result("rescue.scan", probe=True, include_loop=False, network=False)
    for _ in range(MAX_ROUNDS):
        changed = False
        for node in scan.get("devices", []):
            if node.get("role") == "luks_locked" and ctx.console.ask(f"{node['path']} is encrypted. Unlock it now? [Y/n] ").lower() not in {"n", "no"}:
                try:
                    cli.cmd_unlock(ctx, cli_args("unlock", node["path"]))
                    changed = True
                except RescueClientError as exc:
                    ctx.console.warn(f"gentoo-rescue-mount: {exc.message}")
        for vg in scan.get("stacks", {}).get("vgs", []):
            if not vg.get("active"):
                cli.cmd_activate(ctx, cli_args("activate", "lvm", vg["name"]))
                changed = True
        for array in scan.get("stacks", {}).get("arrays", []):
            if array.get("state") == "not_assembled":
                cli.cmd_activate(ctx, cli_args("activate", "md", array["uuid"]))
                changed = True
        if not changed:
            break
        scan = ctx.client.result("rescue.scan", probe=True, include_loop=False, network=False)
    return scan


def choose(ctx: cli.Context, scan: dict[str, Any], wanted: str | None) -> dict[str, Any] | None:
    found = [item for item in scan.get("candidates", []) if item.get("supported", True) and item.get("kind") != "snapshot"]
    if wanted:
        return next((item for item in found if item["id"] == wanted), None)
    if len(found) == 1:
        return found[0]
    if not found:
        return None
    while True:
        answer = ctx.console.ask("Which one? Type its number (Enter to stop): ")
        if not answer:
            return None
        number = cli.typed_number(answer)
        if number is not None and 1 <= number <= len(found):
            return found[number - 1]
        ctx.console.say("Type one of the numbers above.")


def run(ctx: cli.Context, args: argparse.Namespace, link: str = TARGET_LINK, base_dir: str = cli.DEFAULT_SESSION_BASE) -> int:
    say = ctx.console.say
    if args.unmount:
        for session in sessions_of(ctx):
            cli.cmd_unmount(ctx, cli_args("unmount", session["session_id"]))
        remove_link(link, base_dir)
        return cli.EXIT_OK
    existing = sessions_of(ctx)
    if existing:
        session = existing[0]
        say(f"A system is already open (session {session['session_id']}).")
    else:
        scan = open_stack(ctx)
        say("Systems found:")
        cli.render_systems(ctx.console, scan)
        candidate = choose(ctx, scan, args.system)
        if candidate is None:
            say("No Gentoo system was opened. Nothing was changed.")
            return cli.EXIT_NOT_FOUND
        # A person at the console sees the plan and answers "Mount read-only? [Y/n]". Without a terminal (approved on the approver's
        # console through gentoo-rescue-do, where nobody can answer) running the command was the yes: the mount is read-only, and
        # read-write keeps the engine's typed confirmation (--writable). Before, such a run mounted nothing and ended with status 4.
        code = cli.cmd_mount(ctx, cli_args("mount", candidate["id"], *(() if ctx.console.interactive else ("--yes",))))
        if code != cli.EXIT_OK:
            return code
        sessions = sessions_of(ctx)
        if not sessions:
            say("Nothing was mounted.")
            return cli.EXIT_OK
        session = sessions[-1]
    sid = session["session_id"]
    info = ctx.client.result("rescue.session", session_id=sid)
    path = info.get("path") or str(Path(base_dir) / sid)
    try:  # the agents (user rescue-ai) walk through the base folder to /mnt/gentoo: let them, and nothing more than that
        mode = os.stat(base_dir).st_mode
        if not mode & 0o001:
            os.chmod(base_dir, (mode & 0o7777) | 0o005)
    except OSError:
        pass
    problem = make_link(link, path)
    if args.writable:
        say("Read-write needs the engine's own plan and a typed confirmation:")
        code = cli.cmd_run(ctx, cli_args("run", sid, "session.enter_write"))
        if code != cli.EXIT_OK:
            say("The system stays read-only.")
    say("")
    say(f"Your Gentoo system is open at {link if not problem else path}" + (" (read-only)" if info.get("mode", "ro") != "rw" else " (read-write)") + f", session {sid}.")
    if problem:
        say(f"  ! {problem}")
    say(f"  look around:    ls {link}/etc")
    say(f"  a shell inside: gentoo-chroot {sid}      (the Handbook's chroot; read-only unless you add --writable)")
    say(f"  repair:         gentoo-rescue diagnose {sid}, then plan and run: nothing is written before you confirm a plan")
    say("  when finished:  gentoo-rescue-mount --unmount")
    return cli.EXIT_OK


def main(argv: Sequence[str] | None = None, *, transport: Transport | None = None, console: cli.Console | None = None, link: str = TARGET_LINK,
         base_dir: str | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parse(arguments)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else cli.EXIT_USAGE
    console = console or cli.Console()
    if transport is None:
        if os.geteuid() != 0:
            console.warn("gentoo-rescue-mount runs as root (sudo gentoo-rescue-mount): the disk passphrase is typed in this terminal and never goes to an agent.")
            return cli.EXIT_FAILED
        chosen = cli.select_transport(cli_args("status"), arguments, console)
        if isinstance(chosen, int):
            return chosen
        transport = chosen
    ctx = cli.Context(RescueClient(transport, cli.event_printer(console)), console, transport)
    try:
        return run(ctx, args, link, base_dir or cli_args("status").base_dir)
    except RescueClientError as exc:
        console.warn(f"gentoo-rescue-mount: {exc.message}")
        return cli.exit_code_for(exc)
    except KeyboardInterrupt:
        console.warn("\nInterrupted.")
        return cli.EXIT_INTERRUPTED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
