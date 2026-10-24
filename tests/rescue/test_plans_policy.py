"""Property test over every plan the engine can make: whatever the system and the parameters, a plan only contains commands the policies allow.

The plans are built (never run) for several Gentoo systems (OpenRC and systemd, the ESP at /efi and at /boot/efi, GRUB and systemd-boot) in a read-only and in a read-write session. A plan that cannot be made for a system (for example
a boot loader plan for a system without an EFI partition) is simply not there; what is checked is everything that is.
"""

from __future__ import annotations

import itertools
import json

import pytest

from gentoo_installerd.rescue.actions import ACTIONS
from gentoo_installerd.rescue.errors import RescueError
from gentoo_installerd.rescue.plan import Step

from . import fakeroot as fr
from .fakes import DESTRUCTIVE
from .flow import mount_through_the_manager, plan_and_run, uefi_gentoo_lab, unmount

SYSTEMS = [
    pytest.param({"esp_mount": "/efi"}, id="openrc-grub-efi"),
    pytest.param({"esp_mount": "/boot/efi", "init": "systemd"}, id="systemd-grub-boot-efi"),
    pytest.param({"esp_mount": "/efi", "installkernel_use": "dracut systemd-boot\n", "bootctl": True}, id="openrc-systemd-boot"),
]
VARIANTS: dict[str, list[dict]] = {
    "boot.bootloader": [{}, {"removable": "yes"}, {"removable": "no"}, {"firmware": "bios"}, {"firmware": "uefi", "id": "rescued"}, {"loader": "systemd-boot"}],
    "boot.config": [{}], "boot.initramfs": [{}, {"generic": True}], "boot.kernel": [{}, {"mode": "remerge"}], "boot.esp_fallback": [{}],
    "efi.add": [{}, {"label": "My Linux; rm -rf /", "make_default": False}], "efi.delete": [{"bootnum": "0001"}], "efi.order": [{"order": ["0001"]}], "efi.next": [{"bootnum": "0001"}],
    "fstab.fix": [{}], "fs.check": [{}, {"mode": "repair"}, {"mode": "repair_force"}], "acct.password": [{"username": "user"}, {"username": "root", "expire": True}],
    "acct.unlock": [{"username": "user"}], "pkg.finish": [{}], "repair.recommended": [{}], "session.enter_write": [{}], "session.leave_write": [{}],
}
SAFE_ARGV = lambda argv: argv and all(isinstance(arg, str) and arg and not any(ch in arg for ch in "\n\r\0") for arg in argv)  # noqa: E731  what SubprocessRunner accepts


def unhealthy(lab, tree):
    """Make every kind of repair applicable: no menu, an incomplete initramfs, a dead fstab entry, a firmware entry for a file that is gone."""
    (tree / "boot/grub/grub.cfg").unlink(missing_ok=True)
    initramfs = next((tree / "boot").glob("initramfs-*"), None)
    if initramfs is not None:
        initramfs.write_bytes(b"")
    lab.system.nvram.update({"usable": True, "entries": {"0001": {"label": "Old", "loader": "\\EFI\\gone\\grubx64.efi", "active": True, "partuuid": "11111111-2222-3333-4444-555555555555", "part": 1,
                                                                  "disk": "/dev/nvme0n1"}}, "order": ["0001"]})
    (tree / "etc/fstab").write_text((tree / "etc/fstab").read_text() + f"UUID={fr.DEAD_UUID}\t/home\text4\tnoatime\t0 2\n")


def collect(lab_factory, options: dict, *, writable: bool):
    lab, tree = uefi_gentoo_lab(lab_factory, **options)
    unhealthy(lab, tree)
    manager, sid, _info = mount_through_the_manager(lab, candidate_device="/dev/nvme0n1p5")
    if writable:
        plan_and_run(manager, sid, "session.enter_write")
    session = manager.session(sid)
    plans = []
    for action, variants in VARIANTS.items():
        for params in variants:
            try:
                plan = manager.plan({"session_id": sid, "action_id": action, "params": params})
            except RescueError:
                continue  # not applicable to this system or state
            plans.append((action, params, plan, session.plans.get(plan["plan_id"])))
    return lab, manager, sid, session, plans


@pytest.mark.parametrize("options", SYSTEMS)
@pytest.mark.parametrize("writable", [False, True])
def test_every_step_of_every_plan_passes_the_policies(lab_factory, options: dict, writable: bool) -> None:
    lab, manager, sid, session, plans = collect(lab_factory, options, writable=writable)
    assert len(plans) >= (8 if not writable else 10), [item[0] for item in plans]  # the property is not satisfied by an empty set
    for action, params, public, plan in plans:
        for step in [*plan.steps, *plan.cleanup]:
            where = f"{options} {action} {params} {step.id}"
            if step.scope == "host":
                assert SAFE_ARGV(step.argv), where
                assert not step.argv[0].rsplit("/", 1)[-1].startswith(DESTRUCTIVE), where
                session.runner.host.validate(step.argv)  # raises when a command is not on the allow list or touches a device that was not selected
            elif step.scope == "chroot":
                assert SAFE_ARGV(step.argv) and step.tools, where
                session.runner.chroot_policy.validate(step.argv, step.tools)
                session.runner.host.validate(session.runner.chroot_argv(step.argv, extra_env=step.env))  # the full command, with the fixed credentials and environment
            elif step.scope == "file":
                op = step.file_op
                assert op and op["op"] in {"write", "remove", "touch", "mkdir", "copy"}, where
                for key in ("path", "src"):
                    if key in op:
                        assert op[key].startswith("/") and ".." not in op[key].split("/") and "\0" not in op[key], where
                if op["op"] == "write":
                    assert "expect_sha256" in op or op.get("create"), where  # a file that exists is only replaced when it is still what the plan was made from (review R7b)
            else:
                assert step.scope == "internal" and step.handler, where
            assert "password" not in " ".join(step.argv).lower() or step.scope == "host", where
    unmount(manager, sid)


@pytest.mark.parametrize("options", SYSTEMS)
def test_a_plan_is_the_same_plan_when_it_is_made_again(lab_factory, options: dict) -> None:
    """Separate gentoo-rescue commands rely on it: the digest the user confirmed is the digest of the plan made when it runs."""
    lab, manager, sid, session, plans = collect(lab_factory, options, writable=False)
    for action, params, public, _plan in plans:
        again = manager.plan({"session_id": sid, "action_id": action, "params": params})
        assert again["digest"] == public["digest"], (options, action, params)
        assert again["plan_id"] != public["plan_id"]
    unmount(manager, sid)


@pytest.mark.parametrize("options", SYSTEMS)
def test_risk_and_confirmation_follow_what_a_plan_can_change(lab_factory, options: dict) -> None:
    lab, manager, sid, session, plans = collect(lab_factory, options, writable=False)
    for action, params, public, plan in plans:
        writes_boot = any(step.scope in {"host", "chroot"} and step.argv and step.argv[0] in {"grub-install", "bootctl", "emerge", "chpasswd"}
                          for step in plan.steps)
        confirm = public["confirm"]
        assert confirm["risk"] == public["risk"] and confirm["required"] is True
        if public["risk"] in {"high", "dangerous"}:
            assert confirm["typed"] == "nvme0n1p5" and confirm["review"] is True
        if public["risk"] == "dangerous":
            assert confirm["phrase"] == "I HAVE A BACKUP"
        if writes_boot and action != "session.enter_write":
            assert public["risk"] in {"medium", "high", "dangerous"}, (action, params)
        if action in {"session.enter_write", "boot.bootloader", "boot.kernel", "fstab.fix", "acct.password"}:
            assert public["risk"] in {"high", "dangerous"}, action
    unmount(manager, sid)


def test_a_plan_never_carries_a_secret(lab_factory) -> None:
    lab, manager, sid, session, plans = collect(lab_factory, {"esp_mount": "/efi"}, writable=False)
    passwords = [item for item in plans if item[0] == "acct.password"]
    assert passwords
    for _action, _params, public, plan in passwords:
        text = json.dumps(public) + " ".join(" ".join(step.argv) for step in [*plan.steps, *plan.cleanup])
        assert public["secrets"] == ["password"] and "{secret}" not in text.replace("password read from standard input", "")
        stdin_steps = [step for step in plan.steps if step.stdin]
        assert stdin_steps and all(step.stdin == "secret:password" for step in stdin_steps)
    unmount(manager, sid)
