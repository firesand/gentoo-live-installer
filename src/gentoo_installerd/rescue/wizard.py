"""The guided text interface for machines where the GUI cannot start (a console-only boot entry, a broken desktop)."""

from __future__ import annotations

import argparse
from typing import Any

from .cli import (
    EXIT_CONFIRM, EXIT_FAILED, EXIT_OK, Context, cmd_run, render_findings, render_mount_plan, render_systems, typed_number,
)
from .client import RescueClientError

MENU = (
    ("0", "Fix everything recommended", "repair.recommended"),
    ("1", "Fix the disk list (fstab)", "fstab.fix"),
    ("2", "Regenerate the boot menu", "boot.config"),
    ("3", "Reinstall the boot loader", "boot.bootloader"),
    ("4", "Add a firmware boot entry", "efi.add"),
    ("5", "Rebuild the initramfs", "boot.initramfs"),
    ("6", "Reinstall the kernel", "boot.kernel"),
    ("7", "Check a file system", "fs.check"),
    ("8", "Reset a password", "acct.password"),
)


def _namespace(session: str, action: str, **extra: Any) -> argparse.Namespace:
    values: dict[str, Any] = {"session": session, "action": action, "param": [], "choose": None, "confirm_plan": None, "typed": None, "i_have_a_backup": False, "yes_i_reviewed": False}
    values.update(extra)
    return argparse.Namespace(**values)


def run_wizard(ctx: Context, args: argparse.Namespace) -> int:
    console = ctx.console
    console.say("Repair a Gentoo system that does not start: nothing is changed until you confirm a repair.")
    session = ""
    try:
        while True:
            scan = ctx.client.result("rescue.scan", probe=True)
            env = scan.get("environment", {})
            console.say(f"Firmware: {'UEFI' if env.get('uefi') else 'legacy BIOS'}   Secure Boot: {'on' if env.get('secure_boot') else 'off' if env.get('uefi') else 'n/a'}")
            console.say("Systems found:")
            candidates = render_systems(console, scan)
            locked = [node for node in scan.get("devices", []) if node.get("role") == "luks_locked"]
            console.say("Choose a system [number, u=unlock an encrypted volume, r=rescan, q=quit]: " if candidates or locked else "[r=rescan, u=unlock, q=quit]: ")
            answer = console.ask("").lower()
            if answer in {"q", "quit", ""} and (answer or not console.interactive):
                return EXIT_OK
            if answer in {"r", "rescan"}:
                continue
            if answer in {"u", "unlock"}:
                device = locked[0]["path"] if len(locked) == 1 else console.ask("Device to unlock (for example /dev/nvme0n1p3): ")
                if not device:
                    continue
                for attempt in range(3):
                    try:
                        ctx.client.result("rescue.unlock", device=device, passphrase=console.secret(f"LUKS passphrase for {device} (hidden): "))
                        console.say("Unlocked.")
                        break
                    except RescueClientError as exc:
                        console.say(exc.message)
                        if exc.short != "bad_passphrase":
                            break
                for vg in scan.get("stacks", {}).get("vgs", []):
                    if not vg.get("active") and console.ask(f'Volume group "{vg["name"]}" is not active. Activate it? It makes its logical volumes available. [y/N] ').lower() in {"y", "yes"}:
                        ctx.client.result("rescue.activate", kind="lvm", vg=vg["name"])
                continue
            number = typed_number(answer)
            if number is None or not 1 <= number <= len(candidates):
                console.say("Please type one of the numbers shown.")
                continue
            chosen = candidates[number - 1]
            request = {"candidate": chosen["id"], "scan_id": scan["scan_id"], "allow_write": chosen.get("kind") != "snapshot"}
            dry = ctx.client.result("rescue.mount", dry_run=True, **request)
            render_mount_plan(console, dry["mount_plan"])
            if console.ask("Mount read-only? [Y/n] ").lower() in {"n", "no"}:
                continue
            info = ctx.client.request("rescue.mount", plan_digest=dry["mount_plan"]["digest"], **request).result
            session = info["session_id"]
            console.say(f"Mounted at {info['path']} (read-only).")
            render_findings(console, info.get("findings", []))
            return repair_menu(ctx, session, info)
    finally:
        if session and not getattr(args, "keep_mounted", False):
            try:
                ctx.client.result("rescue.unmount", session_id=session)
                console.say("Unmounted. You can remove the live medium and restart.")
            except RescueClientError as exc:
                console.say(f"Could not unmount everything: {exc.message}")


def repair_menu(ctx: Context, session: str, info: dict[str, Any]) -> int:
    console = ctx.console
    status = EXIT_OK
    available = {a["id"]: a for a in info.get("actions", [])}
    while True:
        console.say("What now?")
        for key, title, action in MENU:
            spec = available.get(action, {})
            note = "" if spec.get("available", True) else f"   (not available: {spec.get('reason', '')})"
            star = " *" if spec.get("recommended") else ""
            console.say(f"  {key}) {title}{star}{note}")
        console.say("  9) Read logs   t) Terminal   d) Diagnosis again   u) Unmount and quit")
        choice = console.ask("> ").lower()
        if choice in {"u", "q", ""} and (choice or not console.interactive):
            return status
        if choice == "d":
            data = ctx.client.result("rescue.query", session_id=session, query="diag.summary")["data"]
            render_findings(console, data["findings"])
            continue
        if choice == "9":
            sources = ctx.client.result("rescue.query", session_id=session, query="logs.sources")["data"]["files"]
            for number, item in enumerate(sources, 1):
                console.say(f"  {number}) {item['path']} ({item['size']} bytes)")
            pick = typed_number(console.ask("Which log? (number, empty to go back) "))
            if pick is not None and 1 <= pick <= len(sources):
                lines = ctx.client.result("rescue.query", session_id=session, query="logs.tail", params={"path": sources[pick - 1]["path"], "lines": 60})["data"]["lines"]
                for line in lines:
                    console.say(line)
            continue
        if choice == "t":
            from .cli import cmd_shell

            cmd_shell(ctx, argparse.Namespace(session=session, writable=False, host=False, typed=None, i_have_a_backup=False))
            continue
        selected = next((item for item in MENU if item[0] == choice), None)
        if selected is None:
            console.say("Please choose one of the entries shown.")
            continue
        extra: dict[str, Any] = {}
        action = selected[2]
        if action == "acct.password":
            extra["param"] = [f"username={console.ask('User name: ')}"]
        try:
            code = cmd_run(ctx, _namespace(session, action, **extra))
        except RescueClientError as exc:
            console.say(f"Not possible: {exc.message}")
            continue
        status = EXIT_OK if code in {EXIT_OK, EXIT_CONFIRM} else EXIT_FAILED
