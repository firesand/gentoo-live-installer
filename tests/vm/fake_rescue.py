"""Guest rules for the fake lab that play the live system's ``gentoo-rescue`` command line, and the consoles of cloud images.

The events have the shape the real command prints (tests/vm/test_vm_rescue_driver.py runs the driver against the real one);
here the point is the harness's own orchestration: which VM boots when, what is asserted, what is kept.
"""
from __future__ import annotations

import json

OS = {
    "debian": ("Debian GNU/Linux 13 (trixie)", "debian"),
    "ubuntu": ("Ubuntu 24.04.1 LTS", "debian"),
    "fedora": ("Fedora Linux 44 (Cloud Edition)", "fedora"),
    "arch": ("Arch Linux", "arch"),
    "gentoo": ("Gentoo Linux", "gentoo"),
}
FINDING = {"grub-cfg": ("D05", "The boot menu file is missing", "boot.config"), "initramfs": ("D02", "Kernel 6.8 has no initramfs", "boot.initramfs"),
           "fstab-uuid": ("D07", "fstab refers to disks that are not there", "fstab.fix")}
COMMAND = {"grub-cfg": "update-grub", "initramfs": "update-initramfs -c -k all", "fstab-uuid": "write /etc/fstab (a backup copy is kept beside it)"}
WRITE_ACTIONS = ("repair.recommended", "boot.bootloader", "boot.config", "boot.initramfs", "boot.kernel", "fstab.fix", "pkg.finish")
REFUSAL = "Automatic repairs are not available for Arch Linux in this version. You can inspect the system, read its logs and open a terminal in it."
SESSION = "r1a2b3"


def event(kind: str, message: str = "", **fields) -> str:
    return json.dumps({"type": kind, "message": message, **fields}) + "\n"


def result(data: dict, message: str) -> str:
    return event("result", message, data=data) + event("completed", message, data={"success": True})


def refusal_output() -> str:
    return event("error", REFUSAL, error="rescue.unsupported_action", data={"code": "rescue.unsupported_action"}) + event("completed", REFUSAL, data={"success": False})


def rescue_rules(os_id: str, *, disk: str = "/dev/vda", mode: str = "grub-cfg", typed: str = "", nvram: bool = False, refuse: bool = False,
                 outcome: str = "success", sees_damage: bool = True, live_arguments: bool = False, no_menu_files: bool = False,
                 left_behind: bool = False, systems: list[dict] | None = None) -> list[dict]:
    """Rules for root over SSH: gentoo-rescue (scan, mount, diagnose, plan, run, actions, audit, unmount) and the two checks around it."""
    title, family = OS[os_id]
    system = {"id": "c1234abcd", "device": f"{disk}1", "kind": "root", "fstype": "ext4", "rootflags": "", "os": {"id": os_id, "pretty_name": title, "family": family}}
    finding_id, finding_title, action = FINDING.get(mode, ("D05", "x", "boot.config"))
    wanted = list(FINDING.values()) if mode == "all" else [(finding_id, finding_title, action)]
    findings = [{"id": fid, "severity": "error", "title": ftitle} for fid, ftitle, _a in wanted] if sees_damage and not refuse else []
    commands = list(COMMAND.values()) if mode == "all" else [COMMAND.get(mode, "update-grub")]
    plan = {"digest": "d" * 64, "plan_id": "p1", "risk": "high" if typed else "medium", "action": "repair.recommended", "title": "Fix everything recommended",
            "confirm": {"typed": typed, "phrase": "", "required": True, "review": True, "risk": "medium"},
            "needs": {"uefi_nvram": nvram, "write": True, "chroot": True}, "preconditions": [], "warnings": [], "diff": "",
            "steps": [{"id": f"step-{number}", "display": f"chroot /mnt/rescue/{SESSION} {command}", "argv": command.split()} for number, command in enumerate(commands, 1)]}
    ran = {"outcome": outcome, "steps": [{"id": "boot-config-c1", "status": "completed" if outcome == "success" else "failed", "rc": 0 if outcome == "success" else 1}],
           "post_checks": [{"kind": "file_min_size", "ok": outcome == "success", "text": "/boot/grub/grub.cfg is 1.3 KiB"}], "warnings": []}
    session = {"session_id": SESSION, "path": f"/mnt/rescue/{SESSION}", "mode": "ro", "family": family, "os": system["os"], "findings": findings, "recommended": [item[2] for item in wanted]}
    actions = [{"id": name, "available": not refuse, "reason": REFUSAL if refuse else ""} for name in WRITE_ACTIONS]
    root = {"user": "^root$"}
    rules: list[dict] = [
        {**root, "match": r"^gentoo-rescue (?:--local |--socket \S+ )?--json scan$", "stdout": result({"scan_id": "s1", "candidates": systems if systems is not None else [system],
                                                                                         "live_medium": ["/dev/sr0"]}, "rescue.scan")},
        {**root, "match": r"^gentoo-rescue (?:--local |--socket \S+ )?--json mount c1234abcd --yes$",
         "stdout": "Mount plan (digest abcdef012345):\n" + result({"mount_plan": {"digest": "a"}, "candidate": system}, "rescue.mount")
         + result(session, "rescue.mount") + f"Mounted at /mnt/rescue/{SESSION} (read-only; session {SESSION}).\n"},
        {**root, "match": rf"--json diagnose {SESSION}$", "stdout": result({"query": "diag.summary", "data": {"findings": findings, "recommended": [item[2] for item in wanted] if findings else []}}, "rescue.query")},
        {**root, "match": rf"--json actions {SESSION}$", "stdout": result({"query": "actions.list", "data": {"actions": actions}}, "rescue.query")},
    ]
    if refuse:
        rules += [{**root, "match": rf"--json plan {SESSION} \S+$", "rc": 7, "stdout": refusal_output(), "stderr": f"gentoo-rescue: {REFUSAL}\n"},
                  {**root, "match": rf"--json run {SESSION} \S+ --confirm-plan 0+$", "rc": 7, "stdout": refusal_output(), "stderr": f"gentoo-rescue: {REFUSAL}\n"}]
    else:
        rules += [{**root, "match": rf"--json plan {SESSION} repair\.recommended$", "stdout": result(plan, "rescue.plan")},
                  {**root, "match": rf"--json run {SESSION} repair\.recommended --confirm-plan {'d' * 64}" + (rf" --typed {typed}" if typed else "") + "$",
                   "rc": 0 if outcome == "success" else 1, "stdout": result(plan, "rescue.plan") + result(ran, "Fix everything recommended: " + outcome),
                   "touch": ["repaired"] if outcome == "success" else []}]
    rules += [
        {**root, "match": r"^efibootmgr -v$", "stdout": "BootCurrent: 0001\nBoot0001* UEFI QEMU DVD-ROM\n"},
        {**root, "match": rf"--json audit {SESSION} --text$", "stdout": result({"name": "audit.jsonl", "text": "confirm plan p1\nrun boot-config-c1\n"}, "rescue.audit")},
        {**root, "match": rf"--json unmount {SESSION}$", "stdout": result({"released": [f"/mnt/rescue/{SESSION}"], "leftovers": [], "session_id": SESSION}, "rescue.unmount")},
        {**root, "match": r"import glob, json, os, re, sys", "stdout": json.dumps({
            "checked": [] if no_menu_files else ["/boot/grub/grub.cfg", "/etc/default/grub"],
            "matches": [{"file": "/boot/grub/grub.cfg", "line": 12, "text": "linux /boot/gentoo root=live:CDLABEL=GENTOO"}] if live_arguments else [], "skipped": []}) + "\n"},
        {**root, "match": r"HARNESS_FS_ROOT", "rc": 1 if left_behind else 0,
         "stdout": json.dumps({"mounts": [f"/mnt/rescue/{SESSION}"] if left_behind else [], "sessions": [], "read_only": [], "dm": []}) + "\n"},
    ]
    return rules


def console(prompt: str = "debian login: ") -> str:
    """What a booted cloud image prints on its serial console."""
    return f"[    0.000000] Linux version 6.12\n[  OK  ] Reached target multi-user.target\n\nDebian GNU/Linux 13 {prompt.split()[0]} ttyS0\n\n{prompt}"


BROKEN_GRUB = "error: file '/boot/grub/grub.cfg' not found.\nEntering rescue mode...\ngrub rescue> "
PANIC = "Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)\n"


def cloud_image_serial_rules(prompt: str = "debian login: ", *, broken: str = BROKEN_GRUB, after_repair: str | None = None, relabel: bool = False) -> list[dict]:
    """Serial rules for fake_qemu.py: a healthy image shows its prompt; after the break (rescue-break.json) it shows ``broken`` until the repair
    (the file ``repaired``) has happened, and then ``after_repair`` when given. With ``relabel`` the first UEFI boot after the repair restarts itself
    before the prompt, as the SELinux relabel of Fedora does."""
    rules: list[dict] = [{"phase": "live", "text": ""}, {"phase": "target", "requires": "rescue-break.json", "absent": "repaired", "text": broken}]
    if relabel:
        rules.append({"phase": "target", "firmware": "uefi", "requires": "repaired", "absent": "relabeled", "text": "Relabeling / ...\nreboot: Restarting system\n",
                      "touch": ["relabeled"], "exit_after": 0.3, "exit_code": 0})
    if after_repair is not None:
        rules.append({"phase": "target", "requires": "repaired", "text": after_repair})
    rules.append({"phase": "target", "text": console(prompt)})
    return rules
