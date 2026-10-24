"""``gentoo-rescue``: find, mount, diagnose and repair an installed Linux from a terminal.

Works through the daemon socket, or with ``--local`` (root, no daemon, no GUI). Nothing is changed until a plan has been
shown and confirmed: ``run`` prints the exact commands and either asks interactively or needs ``--confirm-plan <digest>``
(taken from ``plan``), so scripts only ever run what was displayed. Exit codes: 0 ok, 1 failed, 2 usage, 3 refused by the
safety policy, 4 needs confirmation, 5 not found, 6 busy, 7 unsupported, 130 interrupted.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from . import DEFAULT_AUDIT_DIR, DEFAULT_SESSION_BASE, DEFAULT_STATE_DIR
from .api import RescueApi, build_env
from .client import DEFAULT_SOCKET, LocalTransport, RescueClient, RescueClientError, SocketTransport, Transport, attach_terminal
from .errors import RescueError
from .manager import RescueManager

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED, EXIT_CONFIRM, EXIT_NOT_FOUND, EXIT_BUSY, EXIT_UNSUPPORTED, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 5, 6, 7, 130
CODE_EXIT = {
    "safety_violation": EXIT_REFUSED, "live_medium": EXIT_REFUSED, "path_escape": EXIT_REFUSED, "needs_confirmation": EXIT_CONFIRM, "not_found": EXIT_NOT_FOUND, "busy": EXIT_BUSY,
    "busy_install": EXIT_BUSY, "mounted_elsewhere": EXIT_BUSY, "unsupported_action": EXIT_UNSUPPORTED, "unsupported_fs": EXIT_UNSUPPORTED, "unsupported_system": EXIT_UNSUPPORTED,
    "bad_request": EXIT_USAGE, "ambiguous": EXIT_USAGE,
}
BOOL_PARAMS = {"generic", "unlock", "expire", "make_default", "all_disks", "no_entry", "confirm_exec", "kernel", "hardware_changed"}
LIST_PARAMS = {"order", "kernels"}
CANDIDATE_RE = re.compile(r"^c[0-9a-f]{8}$")


class Console:
    """All terminal input and output in one place, so the commands are testable with scripted answers."""

    def __init__(self, out: Any = None, err: Any = None, ask: Callable[[str], str] | None = None, secret: Callable[[str], str] | None = None, interactive: bool | None = None,
                 json_mode: bool = False) -> None:
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self._ask = ask or input
        self._secret = secret or getpass.getpass
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.json_mode = json_mode

    def say(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def warn(self, text: str) -> None:
        print(text, file=self.err, flush=True)

    def ask(self, prompt: str) -> str:
        print(prompt, end="", file=self.out, flush=True)
        return self._ask("").strip()

    def secret(self, prompt: str) -> str:
        return self._secret(prompt)

    def emit(self, event: dict[str, Any]) -> None:
        if self.json_mode:
            self.say(json.dumps(event, sort_keys=True))


def exit_code_for(error: RescueClientError) -> int:
    return CODE_EXIT.get(error.short, EXIT_FAILED)


def parse_params(items: Sequence[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--param needs KEY=VALUE, not {item!r}")
        key, _, value = item.partition("=")
        if key in BOOL_PARAMS:
            params[key] = value.lower() in {"1", "true", "yes", "on"}
        elif key in LIST_PARAMS:
            params[key] = [part for part in value.split(",") if part]
        else:
            params[key] = value
    return params


def parse_choices(items: Sequence[str]) -> dict[str, dict[str, str]]:
    choices: dict[str, dict[str, str]] = {}
    for item in items or []:
        line, _, rest = item.partition("=")
        if not line.isdigit() or not rest:
            raise ValueError(f"--choose needs LINE=ACTION[:SPEC], not {item!r}")
        action, _, spec = rest.partition(":")
        choices[line] = {"action": action, **({"spec": spec} if spec else {})}
    return choices


# ---- rendering ----------------------------------------------------------------------------------------------------------------------------------
def render_systems(console: Console, scan: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = scan.get("candidates", [])
    if not candidates:
        console.say("No installed Linux system was found.")
    for index, item in enumerate(candidates, 1):
        osi = item.get("os", {})
        flags = ["snapshot"] if item.get("kind") == "snapshot" else []
        boot = item.get("boot", {})
        extras = ", ".join(filter(None, [item["fstype"], "/".join(item.get("stack", [])[:-1]) and "encrypted/LVM/RAID" or "", boot.get("loader", ""), *flags]))
        console.say(f"  {index}) {osi.get('pretty_name') or osi.get('name') or 'Linux':<28} {item['device']:<22} {extras}   [{item['id']}]")
        for warning in item.get("warnings", []):
            console.say(f"       ! {warning}")
    for node in scan.get("devices", []):
        if node.get("role") == "luks_locked":
            console.say(f"  locked: {node['path']} (encrypted: use 'gentoo-rescue unlock {node['path']}')")
    for vg in scan.get("stacks", {}).get("vgs", []):
        if not vg.get("active"):
            console.say(f"  inactive volume group: {vg['name']} (use 'gentoo-rescue activate lvm {vg['name']}')")
    for array in scan.get("stacks", {}).get("arrays", []):
        if array.get("state") == "not_assembled":
            console.say(f"  RAID array not assembled: {array['uuid']} (use 'gentoo-rescue activate md {array['uuid']}')")
    for hint in scan.get("hints", []):
        console.say(f"  hint: {hint['text']}")
    return candidates


def render_findings(console: Console, findings: list[dict[str, Any]]) -> None:
    if not findings:
        console.say("Diagnosis: nothing wrong was found.")
        return
    console.say("Diagnosis:")
    marks = {"error": "ERROR", "warn": "warn ", "info": "info "}
    for finding in findings:
        console.say(f"  [{marks.get(finding['severity'], finding['severity'])}] {finding['id']} {finding['title']}")
        console.say(f"          {finding['detail']}")
        for line in finding.get("evidence", [])[:4]:
            console.say(f"          - {line}")
        if finding.get("actions"):
            console.say(f"          fix with: {', '.join(finding['actions'])}")


def render_mount_plan(console: Console, plan: dict[str, Any]) -> None:
    root = plan["root"]
    console.say(f"Mount plan (digest {plan['digest'][:12]}):")
    console.say(f"  /   {root['device']} ({root['fstype']}{', ' + root['rootflags'] if root['rootflags'] else ''})")
    for item in plan["items"]:
        mark = "" if item["selected"] else "  (not selected)"
        console.say(f"  {item['mountpoint']:<12} {item['device']} ({item['fstype']}, {item['tier_name']}){mark}")
    for entry in plan.get("unresolved", []):
        console.say(f"  ? {entry['mountpoint']}: {entry['reason']} (fstab says {entry['spec']})")
    for suggestion in plan.get("suggestions", []):
        console.say(f"  suggestion: mount {suggestion['device']} at {suggestion['mountpoint']} ({suggestion['reason']}): add --mount {suggestion['mountpoint']}={suggestion['device']}")
    for warning in plan.get("warnings", []):
        console.say(f"  ! {warning}")
    if plan.get("commands"):
        console.say("  commands:")
        for command in plan["commands"]:
            console.say(f"    {command}")


def render_plan(console: Console, plan: dict[str, Any]) -> None:
    console.say(f"{plan['title']}   [risk: {plan['risk'].upper()}]   plan {plan['plan_id']}, digest {plan['digest']}")
    if plan.get("summary"):
        console.say(f"  {plan['summary']}")
    for check in plan.get("preconditions", []):
        console.say(f"  [{'ok' if check['ok'] else 'NOT OK'}] {check['text']}")
    console.say("  Commands, in order:")
    for number, step in enumerate(plan["steps"], 1):
        when = {"chroot_ok": "  [if the system's programs run]", "chroot_broken": "  [only if they do not]"}.get(step.get("when", ""), "")
        console.say(f"    {number:>2}. {step['display']}{when}")
    if plan.get("diff"):
        console.say("  Changes to files:")
        for line in plan["diff"].splitlines():
            console.say(f"    {line}")
    if plan.get("cleanup"):
        console.say("  Afterwards:")
        for step in plan["cleanup"]:
            console.say(f"       {step['display']}")
    for warning in plan.get("warnings", []):
        console.say(f"  ! {warning}")


def render_result(console: Console, result: dict[str, Any]) -> None:
    labels = {"success": "Done.", "warning": "Done, but a check did not pass.", "failed": "The repair did not finish.", "cancelled": "Cancelled."}
    console.say(labels.get(result.get("outcome", ""), result.get("outcome", "")))
    for check in result.get("post_checks", []):
        console.say(f"  [{'ok' if check['ok'] else 'NOT OK'}] {check['text']}")
    for step in result.get("steps", []):
        if step["status"] == "failed":
            console.say(f"  step {step['id']} failed (exit code {step.get('rc')}){': ' + step['error'] if step.get('error') else ''}")
    for warning in result.get("warnings", []):
        console.say(f"  ! {warning}")
    for hint in result.get("next_hints", []):
        console.say(f"  next: {hint}")


def event_printer(console: Console) -> Callable[[dict[str, Any]], None]:
    def show(event: dict[str, Any]) -> None:
        if console.json_mode:
            console.emit(event)
            return
        kind = event.get("type")
        data = event.get("data", {})
        if kind == "stage" and data.get("step"):
            if data.get("status") == "running":
                console.say(f"[{data.get('index', '?')}/{data.get('total', '?')}] {event.get('message', '')}")
            elif data.get("status") == "failed":
                console.say(f"      FAILED (exit code {data.get('rc')}) {data.get('error', '')}")
        elif kind == "stage":
            console.say(f"-- {event.get('message') or event.get('stage')}")
        elif kind == "log":
            console.say(f"      {event.get('message', '')}")
        elif kind == "progress" and event.get("message"):
            console.say(f"   {event['message']}")

    return show


# ---- the commands -------------------------------------------------------------------------------------------------------------------------------
class Context:
    def __init__(self, client: RescueClient, console: Console, transport: Transport) -> None:
        self.client = client
        self.console = console
        self.transport = transport


def auto_choice(ctx: Context) -> str:
    """``mount --auto``: the one installed system that was found, or a clear explanation why none was chosen."""
    scan = ctx.client.result("rescue.scan", probe=True)
    roots = [item for item in scan.get("candidates", []) if item.get("kind") != "snapshot"]
    if len(roots) == 1:
        return roots[0]["id"]
    if not roots:
        raise RescueClientError("rescue.not_found", "No installed system was found. Unlock encrypted volumes ('gentoo-rescue unlock DEVICE') or activate LVM/RAID first, then try again.")
    render_systems(ctx.console, scan)
    raise RescueClientError("rescue.ambiguous", "Several systems were found. Choose one and give its id (for example c1a2b3c4d) or its device.")


def resolve_system(ctx: Context, text: str, args: argparse.Namespace) -> dict[str, Any]:
    if not text and getattr(args, "auto", False):
        text = auto_choice(ctx)
    if not text:
        raise RescueClientError("rescue.bad_request", "Say which system to mount: an id from 'scan', a device such as /dev/sda2, or --auto when there is only one")
    if CANDIDATE_RE.match(text):
        scan = ctx.client.result("rescue.scan", probe=True)
        if not any(item["id"] == text for item in scan.get("candidates", [])):
            raise RescueClientError("rescue.not_found", f"{text} is not in this scan; run 'gentoo-rescue scan' again")
        return {"candidate": text, "scan_id": scan["scan_id"]}
    device, _, flags = text.partition(":")
    if not device.startswith("/dev/"):
        raise RescueClientError("rescue.bad_request", "Give a system id from 'scan' (c1234abcd) or a device such as /dev/sda2 or /dev/sda2:subvolid=256")
    return {"device": device, "rootflags": flags}


def cmd_scan(ctx: Context, args: argparse.Namespace) -> int:
    result = ctx.client.result("rescue.scan", probe=not args.no_probe, include_loop=args.all, network=args.network)
    if ctx.console.json_mode:
        return EXIT_OK
    environment = result.get("environment", {})
    ctx.console.say(f"Firmware: {'UEFI' if environment.get('uefi') else 'legacy BIOS'}"
                    + (f" (Secure Boot {'on' if environment.get('secure_boot') else 'off'})" if environment.get("uefi") and environment.get("secure_boot") is not None else "")
                    + (f" - network: {'online' if environment['network'].get('ip') else 'offline'}" if environment.get("network") else ""))
    ctx.console.say("Systems found:")
    render_systems(ctx.console, result)
    if result.get("live_medium"):
        ctx.console.say(f"(hidden, this is the live medium: {', '.join(result['live_medium'])})")
    return EXIT_OK


def read_passphrase(ctx: Context, args: argparse.Namespace, device: str) -> str:
    if args.passphrase_stdin:
        return sys.stdin.readline().rstrip("\n")
    return ctx.console.secret(f"LUKS passphrase for {device} (hidden): ")


def cmd_unlock(ctx: Context, args: argparse.Namespace) -> int:
    if args.keyfile:
        result = ctx.client.result("rescue.unlock", device=args.device, keyfile=args.keyfile, keyfile_session=args.session or "", readonly=args.readonly, test_only=args.test)
    else:
        result = ctx.client.result("rescue.unlock", device=args.device, passphrase=read_passphrase(ctx, args, args.device), readonly=args.readonly, test_only=args.test)
    if result.get("test_only"):
        ctx.console.say("The passphrase is correct.")
        return EXIT_OK
    ctx.console.say(f"Unlocked as {result['mapper']}" + (" (it was already open)" if result.get("preexisting") else ""))
    for hint in result.get("luks", {}).get("hints", []):
        ctx.console.say(f"  note: {hint}")
    for candidate in result.get("new_candidates", []):
        ctx.console.say(f"  found: {candidate['os'].get('pretty_name', 'Linux')} on {candidate['device']} [{candidate['id']}]")
    return EXIT_OK


def cmd_activate(ctx: Context, args: argparse.Namespace) -> int:
    if args.kind == "lvm":
        ctx.console.say("Activating a volume group makes its logical volumes available. In rare cases LVM repairs out-of-date metadata while doing so.")
        result = ctx.client.result("rescue.activate", kind="lvm", vg=args.names[0])
    else:
        result = ctx.client.result("rescue.activate", kind="md", uuids=args.names, degraded_ok=args.degraded)
    for item in result.get("new_candidates", []):
        ctx.console.say(f"  found: {item['os'].get('pretty_name', 'Linux')} on {item['device']} [{item['id']}]")
    ctx.console.say("Done.")
    return EXIT_OK


def cmd_mount(ctx: Context, args: argparse.Namespace) -> int:
    request = {**resolve_system(ctx, args.system, args), "allow_write": not args.inspect_only, "allow_degraded": args.allow_degraded, "include_tier4": args.with_data or [],
               "overrides": [{"mountpoint": item.partition("=")[0], "device": item.partition("=")[2]} for item in args.mount or []], "unmount_existing": args.unmount_existing or []}
    dry = ctx.client.result("rescue.mount", dry_run=True, **request)
    render_mount_plan(ctx.console, dry["mount_plan"])
    if args.dry_run:
        return EXIT_OK
    if not args.yes:
        if not ctx.console.interactive:
            ctx.console.warn(f"Nothing was mounted. Re-run with --yes to mount exactly this plan (digest {dry['mount_plan']['digest']}).")
            return EXIT_CONFIRM
        if ctx.console.ask("Mount read-only? [Y/n] ").lower() in {"n", "no"}:
            return EXIT_OK
    info = ctx.client.request("rescue.mount", plan_digest=dry["mount_plan"]["digest"], **request).result
    ctx.console.say(f"Mounted at {info['path']} (read-only; session {info['session_id']}).")
    for warning in info.get("warnings", []):
        ctx.console.say(f"  ! {warning}")
    render_findings(ctx.console, info.get("findings", []))
    recommended = info.get("recommended", [])
    if recommended:
        ctx.console.say(f"Recommended: {', '.join(recommended)}   (gentoo-rescue plan {info['session_id']} <action>)")
    return EXIT_OK


def cmd_status(ctx: Context, args: argparse.Namespace) -> int:
    result = ctx.client.result("rescue.session", session_id=args.session or "")
    if args.session:
        ctx.console.say(f"Session {result['session_id']}: {result['state']}, {result['mode']}, {result['path']}")
        for mount in result.get("mounts", []):
            ctx.console.say(f"  {mount.get('mountpoint', '/'):<12} {mount.get('device', '')} ({'rw' if mount.get('rw') else 'ro'})")
        return EXIT_OK
    sessions = result.get("sessions", [])
    ctx.console.say(f"{len(sessions)} session(s) mounted." if sessions else "No repair session is mounted.")
    for session in sessions:
        ctx.console.say(f"  {session['session_id']}  {session['mode']}  {session['root']['device']}  {session.get('os', {}).get('pretty_name', '')}")
    for orphan in result.get("orphans", []):
        ctx.console.say(f"  leftover: {orphan['sid'] or '(stored volumes)'} {orphan['state']} - run 'gentoo-rescue cleanup'")
    return EXIT_OK


def cmd_actions(ctx: Context, args: argparse.Namespace) -> int:
    data = ctx.client.result("rescue.query", session_id=args.session, query="actions.list")["data"]
    ctx.console.say(f"Session {args.session}:")
    for spec in data["actions"]:
        mark = "*" if spec["recommended"] else " "
        state = "" if spec["available"] else f"  (not available: {spec['reason']})"
        ctx.console.say(f" {mark} {spec['id']:<22} {spec['risk']:<9} {spec['title']}{state}")
    ctx.console.say("* = recommended for what the diagnosis found")
    return EXIT_OK


def cmd_diagnose(ctx: Context, args: argparse.Namespace) -> int:
    query = "diag.deep" if args.deep else "diag.summary"
    data = ctx.client.result("rescue.query", session_id=args.session, query=query, params={"hardware_changed": args.hardware_changed})["data"]
    render_findings(ctx.console, data["findings"])
    if data.get("recommended"):
        ctx.console.say("Recommended order: " + " -> ".join(data["recommended"]))
    return EXIT_OK


def cmd_plan(ctx: Context, args: argparse.Namespace) -> int:
    plan = ctx.client.result("rescue.plan", session_id=args.session, action_id=args.action, params=action_params(args))
    if ctx.console.json_mode:
        return EXIT_OK
    render_plan(ctx.console, plan)
    confirm = plan["confirm"]
    flags = f"--confirm-plan {plan['digest']}" + (f" --typed {confirm['typed']}" if confirm["typed"] else "") + (" --i-have-a-backup" if confirm["phrase"] else "")
    if plan.get("secrets"):
        flags += " (the " + ", ".join(plan["secrets"]) + " is asked for)"
    ctx.console.say(f"To run exactly this plan: gentoo-rescue run {args.session} {args.action} {flags}")
    return EXIT_OK


def action_params(args: argparse.Namespace) -> dict[str, Any]:
    params = parse_params(getattr(args, "param", None) or [])
    if getattr(args, "choose", None):
        params["choices"] = parse_choices(args.choose)
    return params


def confirm_interactively(ctx: Context, plan: dict[str, Any]) -> tuple[bool, str, str]:
    confirm = plan["confirm"]
    count = len(plan["steps"])
    if confirm["typed"]:
        typed = ctx.console.ask(f'Type "{confirm["typed"]}" to run these {count} commands (Ctrl-C to cancel): ')
        if typed != confirm["typed"]:
            return False, typed, ""
    elif ctx.console.ask(f"Run these {count} commands? [y/N] ").lower() not in {"y", "yes"}:
        return False, "", ""
    else:
        typed = ""
    phrase = ""
    if confirm["phrase"]:
        phrase = ctx.console.ask(f'This can destroy data. Type "{confirm["phrase"]}" to continue: ')
        if phrase != confirm["phrase"]:
            return False, typed, phrase
    return True, typed, phrase


def cmd_run(ctx: Context, args: argparse.Namespace, secrets: dict[str, str] | None = None) -> int:
    params = action_params(args)
    plan = ctx.client.result("rescue.plan", session_id=args.session, action_id=args.action, params=params)
    render_plan(ctx.console, plan)
    confirm = plan["confirm"]
    if args.confirm_plan:
        if args.confirm_plan != plan["digest"]:
            ctx.console.warn("The plan on this system is not the one you confirmed (its digest differs). Nothing was run. Review the new plan above.")
            return EXIT_CONFIRM
        typed, phrase = args.typed or "", ("I HAVE A BACKUP" if args.i_have_a_backup else "")
        if confirm["typed"] and typed != confirm["typed"]:
            ctx.console.warn(f"This plan needs --typed {confirm['typed']}. Nothing was run.")
            return EXIT_CONFIRM
        if confirm["phrase"] and not phrase:
            ctx.console.warn("This plan can destroy data and needs --i-have-a-backup. Nothing was run.")
            return EXIT_CONFIRM
    elif ctx.console.interactive and not args.yes_i_reviewed:
        ok, typed, phrase = confirm_interactively(ctx, plan)
        if not ok:
            ctx.console.say("Nothing was run.")
            return EXIT_CONFIRM
    else:
        flags = f"--confirm-plan {plan['digest']}" + (f" --typed {confirm['typed']}" if confirm["typed"] else "") + (" --i-have-a-backup" if confirm["phrase"] else "")
        ctx.console.warn(f"Nothing was run. To run exactly this plan, add: {flags}")
        return EXIT_CONFIRM
    if plan.get("secrets") and secrets is None:
        secrets = {}
        for name in plan["secrets"]:
            first = ctx.console.secret(f"New {name} (hidden): ")
            if first != ctx.console.secret(f"Repeat the {name}: "):
                ctx.console.warn(f"The two {name} entries differ. Nothing was run.")
                return EXIT_USAGE
            secrets[name] = first
    outcome = ctx.client.request("rescue.run", session_id=args.session, action_id=args.action, params=params, digest=plan["digest"], confirm=True, typed=typed, phrase=phrase,
                                secrets=secrets or {})
    result = outcome.result
    render_result(ctx.console, result)
    return EXIT_OK if result.get("outcome") in {"success", "warning"} else EXIT_FAILED


def cmd_grub_repair(ctx: Context, args: argparse.Namespace) -> int:
    args.action = "boot.bootloader"
    args.choose = None
    return cmd_run(ctx, args)


def cmd_password(ctx: Context, args: argparse.Namespace) -> int:
    args.action = "acct.password"
    args.param = [f"username={args.user}"] + (["expire=true"] if args.expire else [])
    secrets: dict[str, str] | None = None
    if args.password_stdin:
        secrets = {"password": sys.stdin.readline().rstrip("\n")}
    return cmd_run(ctx, args, secrets)


def cmd_efi(ctx: Context, args: argparse.Namespace) -> int:
    if args.efi_command == "list":
        data = ctx.client.result("rescue.query", session_id=args.session, query="efi.list")["data"]
        if not data.get("usable"):
            ctx.console.say(data.get("error", "Boot entries cannot be listed from this session."))
            return EXIT_UNSUPPORTED
        for entry in data["entries"]:
            first = "*" if entry["number"] in data["order"][:1] else " "
            ctx.console.say(f" {first} Boot{entry['number']}{'*' if entry['active'] else ' '} {entry['label']:<28} {entry['loader']}")
        ctx.console.say("Boot order: " + ",".join(data["order"]))
        return EXIT_OK
    mapping = {"add": ("efi.add", ["loader", "label"]), "delete": ("efi.delete", ["bootnum"]), "order": ("efi.order", ["order"]), "next": ("efi.next", ["bootnum"])}
    action, _fields = mapping[args.efi_command]
    args.action = action
    param_items: list[str] = []
    if args.efi_command == "add":
        param_items += [f"loader={args.loader}"] if args.loader else []
        param_items += [f"label={args.label}"] if args.label else []
        param_items += ["make_default=false"] if args.no_default else []
    elif args.efi_command == "order":
        param_items.append("order=" + ",".join(args.numbers))
    else:
        param_items.append(f"bootnum={args.number}")
    args.param = param_items
    return cmd_run(ctx, args)


def cmd_fstab(ctx: Context, args: argparse.Namespace) -> int:
    if args.fstab_command == "show":
        data = ctx.client.result("rescue.query", session_id=args.session, query="fstab.show")["data"]
        for entry in data["entries"]:
            ctx.console.say(f"  line {entry['line']:<3} {entry['mountpoint']:<14} {entry['spec']:<44} {entry['status']}" + (f"  {entry['reason']}" if entry["reason"] else ""))
        return EXIT_OK
    args.action = "fstab.fix"
    args.param = []
    return cmd_run(ctx, args)


def cmd_fsck(ctx: Context, args: argparse.Namespace) -> int:
    args.action = "fs.check"
    args.param = ([f"device={args.device}"] if args.device else []) + [f"mode={'repair_force' if args.force else 'repair' if args.repair else 'check'}"]
    return cmd_run(ctx, args)


def cmd_logs(ctx: Context, args: argparse.Namespace) -> int:
    if args.pstore:
        data = ctx.client.result("rescue.query", session_id=args.session, query="logs.pstore")["data"]
        for entry in data["entries"]:
            ctx.console.say(f"--- {entry['name']} ---\n{entry['text']}")
        ctx.console.say(data["note"] if not data["entries"] else "")
        return EXIT_OK
    if args.live:
        for line in ctx.client.result("rescue.query", session_id=args.session, query="logs.live_dmesg")["data"]["lines"]:
            ctx.console.say(line)
        return EXIT_OK
    if args.journal:
        params = {"confirm_exec": args.allow_run_system_program, "lines": args.lines, "boot": args.boot, "priority": args.priority or "", "kernel": args.kernel}
        data = ctx.client.result("rescue.query", session_id=args.session, query="logs.journal", params=params)["data"]
        for line in data["lines"]:
            ctx.console.say(line)
        return EXIT_OK
    if args.source:
        data = ctx.client.result("rescue.query", session_id=args.session, query="logs.tail", params={"path": args.source, "lines": args.lines})["data"]
        for line in data["lines"]:
            ctx.console.say(line)
        return EXIT_OK
    data = ctx.client.result("rescue.query", session_id=args.session, query="logs.sources")["data"]
    for item in data["files"]:
        ctx.console.say(f"  {item['path']:<40} {item['size']:>10} bytes")
    for journal in data["journal"]:
        ctx.console.say(f"  journal: {journal['path']} ({journal['files']} files; reading it runs the system's own journalctl, use --journal --allow-run-system-program)")
    ctx.console.say("The kernel log of the boot that failed is only stored if that system had a persistent journal or a syslog running; see --pstore for kernel crash records.")
    return EXIT_OK


def cmd_shell(ctx: Context, args: argparse.Namespace) -> int:
    need_name = ""
    if not args.typed:
        info = ctx.client.result("rescue.session", session_id=args.session)
        need_name = info["root"]["device"].rsplit("/", 1)[-1]
        if ctx.console.interactive:
            args.typed = ctx.console.ask(f'Type "{need_name}" to open a terminal in this system: ')
    request = {"action": "rescue.shell", "session_id": args.session, "writable": args.writable, "host": args.host, "confirm": True, "typed": args.typed or "",
               "phrase": "I HAVE A BACKUP" if args.i_have_a_backup else "", "term": os.environ.get("TERM", "xterm")}
    if ctx.console.interactive and args.writable and not args.i_have_a_backup:
        request["phrase"] = ctx.console.ask('A read-write terminal can change anything. Type "I HAVE A BACKUP" to continue: ')
    size = shutil.get_terminal_size((80, 24))
    request.update({"rows": size.lines, "cols": size.columns})
    info, fd = ctx.transport.shell(request)
    if fd >= 0:
        ctx.console.say(info.get("banner", ""))
        attach_terminal(fd)
    return EXIT_OK


def cmd_unmount(ctx: Context, args: argparse.Namespace) -> int:
    try:
        result = ctx.client.result("rescue.unmount", session_id=args.session or "", force=args.force, save_report=not args.no_report)
    except RescueClientError as exc:
        holders = (exc.data.get("details") or {}).get("busy_holders") or []
        if holders:
            ctx.console.say("Still in use by:")
            for holder in holders:
                ctx.console.say(f"  {holder['user']} {holder['pid']} {holder['command']}")
            ctx.console.say("Close those programs and run it again, or add --force.")
        raise
    if result.get("report"):
        ctx.console.say(f"A report was saved in the repaired system: {result['report']}")
    ctx.console.say("Unmounted. You can remove the live medium and restart.")
    return EXIT_OK


def cmd_audit(ctx: Context, args: argparse.Namespace) -> int:
    data = ctx.client.result("rescue.audit", session_id=args.session, format="text" if args.text else "jsonl")
    if args.output:
        Path(args.output).write_text(data["text"])
        ctx.console.say(f"Saved {args.output}")
    else:
        ctx.console.say(data["text"].rstrip("\n"))
    return EXIT_OK


def cmd_cleanup(ctx: Context, args: argparse.Namespace) -> int:
    result = ctx.client.result("rescue.recover", **{"do": "cleanup", "all": True, "force": True})
    sessions = result.get("sessions", {})
    for sid, outcome in sessions.items():
        left = outcome.get("leftovers") or outcome.get("error")
        ctx.console.say(f"  {sid}: " + ("released" if not left else f"still busy: {left}"))
    ctx.console.say("Stored volumes: " + ("released" if not result.get("pool", {}).get("leftovers") else "some could not be released"))
    ctx.console.say("If a disk reports 'read-only device' afterwards, run: blockdev --setrw <device>")
    return EXIT_OK


# ---- argument parser -------------------------------------------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gentoo-rescue", description="Find, mount, diagnose and repair an installed Linux system that no longer boots.")
    parser.add_argument("--socket", help="daemon socket (default: " + DEFAULT_SOCKET + ")")
    parser.add_argument("--local", action="store_true", help="run the rescue engine in this process (needs root; no daemon)")
    parser.add_argument("--json", action="store_true", help="print one JSON event per line")
    parser.add_argument("--base-dir", default=DEFAULT_SESSION_BASE, help=argparse.SUPPRESS)
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help=argparse.SUPPRESS)
    parser.add_argument("--audit-dir", default=DEFAULT_AUDIT_DIR, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")

    wizard = sub.add_parser("wizard", help="guided text interface (the default when run on a terminal)")
    wizard.add_argument("--keep-mounted", action="store_true", help="leave the system mounted when the wizard ends")

    scan = sub.add_parser("scan", help="list installed systems and encrypted volumes (read-only)")
    scan.add_argument("--no-probe", action="store_true")
    scan.add_argument("--all", action="store_true", help="include loop devices")
    scan.add_argument("--network", action="store_true", help="also test the network")

    unlock = sub.add_parser("unlock", help="open an encrypted (LUKS) volume")
    unlock.add_argument("device")
    unlock.add_argument("--passphrase-stdin", action="store_true", help="read the passphrase from standard input instead of asking")
    unlock.add_argument("--keyfile", help="a key file inside a mounted system")
    unlock.add_argument("--session", help="the session that holds the key file")
    unlock.add_argument("--readonly", action="store_true")
    unlock.add_argument("--test", action="store_true", help="only check the passphrase")

    activate = sub.add_parser("activate", help="activate a volume group or assemble a RAID array")
    activate.add_argument("kind", choices=["lvm", "md"])
    activate.add_argument("names", nargs="+", metavar="NAME")
    activate.add_argument("--degraded", action="store_true")

    mount = sub.add_parser("mount", help="mount a system read-only")
    mount.add_argument("system", nargs="?", help="system id from 'scan', or a device such as /dev/sda2[:subvolid=256]")
    mount.add_argument("--auto", action="store_true", help="use the one installed system that was found (refuses when there are several)")
    mount.add_argument("--inspect-only", action="store_true", help="never allow repairs: no program of the system can run")
    mount.add_argument("--mount", action="append", metavar="MP=DEV", help="mount DEV at MP below the system")
    mount.add_argument("--with-data", action="append", metavar="MP", help="also mount this data directory")
    mount.add_argument("--unmount-existing", action="append", metavar="PATH", help="allow unmounting this existing mount of the same device")
    mount.add_argument("--allow-degraded", action="store_true")
    mount.add_argument("--dry-run", action="store_true", help="show the mount plan and its digest only")
    mount.add_argument("--yes", action="store_true", help="mount without asking")

    status = sub.add_parser("status", help="sessions and leftovers")
    status.add_argument("session", nargs="?")

    actions = sub.add_parser("actions", help="what can be done for a session")
    actions.add_argument("session")

    diagnose = sub.add_parser("diagnose", help="what is wrong (no command is run)")
    diagnose.add_argument("session")
    diagnose.add_argument("--deep", action="store_true", help="also look inside the initramfs images")
    diagnose.add_argument("--hardware-changed", action="store_true", help="the disk was moved to a different computer")

    plan = sub.add_parser("plan", help="show the exact commands of a repair; changes nothing")
    plan.add_argument("session")
    plan.add_argument("action")
    plan.add_argument("--param", action="append", metavar="KEY=VALUE")
    plan.add_argument("--choose", action="append", metavar="LINE=ACTION[:SPEC]", help="fstab.fix: what to do with a line")

    run = sub.add_parser("run", help="run a repair after showing its commands")
    run.add_argument("session")
    run.add_argument("action")
    run.add_argument("--param", action="append", metavar="KEY=VALUE")
    run.add_argument("--choose", action="append", metavar="LINE=ACTION[:SPEC]")
    run.add_argument("--confirm-plan", metavar="DIGEST", help="digest printed by 'plan'; runs exactly that plan")
    run.add_argument("--typed", help="the root device name (high-risk plans)")
    run.add_argument("--i-have-a-backup", action="store_true", help="required for plans that can destroy data")
    run.add_argument("--yes-i-reviewed", action="store_true", help=argparse.SUPPRESS)

    grub = sub.add_parser("grub-repair", help="reinstall the boot loader (the same as: run SESSION boot.bootloader)")
    grub.add_argument("session")
    grub.add_argument("--param", action="append", metavar="KEY=VALUE", help="for example firmware=bios, removable=yes, loader=systemd-boot")
    grub.add_argument("--confirm-plan", metavar="DIGEST")
    grub.add_argument("--typed")
    grub.add_argument("--i-have-a-backup", action="store_true")
    grub.add_argument("--yes-i-reviewed", action="store_true", help=argparse.SUPPRESS)

    efi = sub.add_parser("efi", help="UEFI boot entries")
    efi_sub = efi.add_subparsers(dest="efi_command", required=True)
    efi_sub.add_parser("list").add_argument("session")
    for name in ("add", "delete", "order", "next"):
        sp = efi_sub.add_parser(name)
        sp.add_argument("session")
        if name == "add":
            sp.add_argument("--loader")
            sp.add_argument("--label")
            sp.add_argument("--no-default", action="store_true")
        elif name == "order":
            sp.add_argument("numbers", nargs="+")
        else:
            sp.add_argument("number")
        sp.add_argument("--confirm-plan", metavar="DIGEST")
        sp.add_argument("--typed")
        sp.add_argument("--i-have-a-backup", action="store_true")
        sp.add_argument("--yes-i-reviewed", action="store_true", help=argparse.SUPPRESS)

    fstab = sub.add_parser("fstab", help="show or fix /etc/fstab")
    fstab_sub = fstab.add_subparsers(dest="fstab_command", required=True)
    fstab_sub.add_parser("show").add_argument("session")
    fix = fstab_sub.add_parser("fix")
    fix.add_argument("session")
    fix.add_argument("--choose", action="append", metavar="LINE=ACTION[:SPEC]")
    fix.add_argument("--confirm-plan", metavar="DIGEST")
    fix.add_argument("--typed")
    fix.add_argument("--i-have-a-backup", action="store_true")
    fix.add_argument("--yes-i-reviewed", action="store_true", help=argparse.SUPPRESS)

    fsck = sub.add_parser("fsck", help="check (read-only) or repair a file system, unmounted")
    fsck.add_argument("session")
    fsck.add_argument("device", nargs="?")
    fsck.add_argument("--repair", action="store_true")
    fsck.add_argument("--force", action="store_true", help="the repair that may lose data")
    fsck.add_argument("--confirm-plan", metavar="DIGEST")
    fsck.add_argument("--typed")
    fsck.add_argument("--i-have-a-backup", action="store_true")
    fsck.add_argument("--yes-i-reviewed", action="store_true", help=argparse.SUPPRESS)

    password = sub.add_parser("password", help="set a user's password inside the system")
    password.add_argument("session")
    password.add_argument("user")
    password.add_argument("--expire", action="store_true")
    password.add_argument("--password-stdin", action="store_true")
    password.add_argument("--confirm-plan", metavar="DIGEST")
    password.add_argument("--typed")
    password.add_argument("--i-have-a-backup", action="store_true")
    password.add_argument("--yes-i-reviewed", action="store_true", help=argparse.SUPPRESS)

    logs = sub.add_parser("logs", help="read the system's logs (read-only)")
    logs.add_argument("session")
    logs.add_argument("--source", help="a log file such as /var/log/messages")
    logs.add_argument("--lines", type=int, default=100)
    logs.add_argument("--journal", action="store_true", help="use the system's own journalctl")
    logs.add_argument("--allow-run-system-program", action="store_true", help="confirm that journalctl of the system may run as root")
    logs.add_argument("--boot", type=int, default=0)
    logs.add_argument("--priority")
    logs.add_argument("--kernel", action="store_true")
    logs.add_argument("--pstore", action="store_true")
    logs.add_argument("--live", action="store_true", help="show this live session's own kernel warnings")

    shell = sub.add_parser("shell", aliases=["chroot"], help="a terminal in the system (also called chroot)")
    shell.add_argument("session")
    shell.add_argument("--writable", action="store_true")
    shell.add_argument("--host", action="store_true", help="a live-system shell in the mount directory (nothing of the system runs)")
    shell.add_argument("--typed", help="the root device name")
    shell.add_argument("--i-have-a-backup", action="store_true")

    unmount = sub.add_parser("unmount", aliases=["umount"], help="release a session (or the stored volumes when no session is given)")
    unmount.add_argument("session", nargs="?")
    unmount.add_argument("--force", action="store_true")
    unmount.add_argument("--no-report", action="store_true")

    audit = sub.add_parser("audit", help="the log of everything done in a session")
    audit.add_argument("session")
    audit.add_argument("-o", "--output")
    audit.add_argument("--text", action="store_true")

    sub.add_parser("cleanup", help="release everything left behind by a crashed session")
    return parser


COMMANDS: dict[str, Callable[[Context, argparse.Namespace], int]] = {
    "scan": cmd_scan, "unlock": cmd_unlock, "activate": cmd_activate, "mount": cmd_mount, "status": cmd_status, "actions": cmd_actions, "diagnose": cmd_diagnose, "plan": cmd_plan,
    "run": cmd_run, "grub-repair": cmd_grub_repair, "chroot": cmd_shell, "umount": cmd_unmount, "efi": cmd_efi, "fstab": cmd_fstab, "fsck": cmd_fsck, "password": cmd_password, "logs": cmd_logs, "shell": cmd_shell, "unmount": cmd_unmount, "audit": cmd_audit,
    "cleanup": cmd_cleanup,
}


# ---- choosing how to talk to the engine -----------------------------------------------------------------------------------------------------------
def local_transport(args: argparse.Namespace) -> LocalTransport:
    manager = RescueManager(build_env(args.base_dir, args.state_dir, args.audit_dir), auto_adopt=True)
    api = RescueApi(manager)
    return LocalTransport(api)


def select_transport(args: argparse.Namespace, argv: Sequence[str], console: Console) -> Transport | int:
    if args.socket:
        return SocketTransport(args.socket)
    if args.local:
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            console.warn("--local needs root. Try: sudo gentoo-rescue --local ...")
            return EXIT_FAILED
        return local_transport(args)
    sock = SocketTransport(DEFAULT_SOCKET)
    if sock.available():
        return sock
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return local_transport(args)
    if shutil.which("sudo") and subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0:
        os.execvp("sudo", ["sudo", "-n", sys.argv[0], "--local", *argv])
    console.warn("The installer daemon is not reachable and you are not root. Run: sudo gentoo-rescue")
    return EXIT_FAILED


def main(argv: Sequence[str] | None = None, *, transport: Transport | None = None, console: Console | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(arguments)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) and exc.code != 0 else (EXIT_USAGE if exc.code else EXIT_OK)
    console = console or Console(json_mode=args.json)
    console.json_mode = args.json or console.json_mode
    command = args.command or ("wizard" if console.interactive else None)
    if command is None:
        parser.print_usage(console.err)
        return EXIT_USAGE
    if transport is None:
        chosen = select_transport(args, arguments, console)
        if isinstance(chosen, int):
            return chosen
        transport = chosen
    ctx = Context(RescueClient(transport, event_printer(console)), console, transport)
    try:
        if command == "wizard":
            from .wizard import run_wizard

            return run_wizard(ctx, args)
        return COMMANDS[command](ctx, args)
    except RescueClientError as exc:
        console.warn(f"gentoo-rescue: {exc.message}")
        return exit_code_for(exc)
    except ValueError as exc:
        console.warn(f"gentoo-rescue: {exc}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        console.warn("\nInterrupted.")
        return EXIT_INTERRUPTED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
