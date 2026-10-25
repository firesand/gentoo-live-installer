"""The repair window's rules that need no display: describing systems, grouping actions, confirmations, following a run, the terminal command."""

from __future__ import annotations

import shlex

import pytest

from gentoo_installer.rescue_ui import messages, model
from gentoo_installer.rescue_ui.model import Confirmation, MountChoice, RunTracker
from gentoo_installerd.rescue.plan import BACKUP_PHRASE, RISKS

DEVICES = [
    {"path": "/dev/nvme0n1", "type": "disk", "model": "Samsung SSD 990 PRO 1TB", "size": 1000 * 2**30, "role": "other"},
    {"path": "/dev/nvme0n1p3", "type": "part", "model": "", "size": 400 * 2**30, "role": "luks_locked", "fstype": "crypto_LUKS"},
    {"path": "/dev/nvme0n1p5", "type": "part", "model": "", "size": 100 * 2**30, "role": "filesystem", "fstype": "ext4"},
    {"path": "/dev/sda", "type": "disk", "model": "Kingston A400", "size": 480 * 2**30, "role": "other"},
    {"path": "/dev/sda12", "type": "part", "model": "", "size": 2**30, "role": "filesystem", "fstype": "ext4"},
    {"path": "/dev/sdb", "type": "disk", "model": "USB stick", "size": 16 * 2**30, "role": "other", "live_medium": True},
]


def candidate(**overrides):
    base = {
        "id": "c14efd32b", "device": "/dev/nvme0n1p5", "stack": ["/dev/nvme0n1p5"], "fstype": "ext4", "kind": "root", "warnings": [],
        "os": {"pretty_name": "Gentoo Linux", "name": "Gentoo", "id": "gentoo"},
        "boot": {"loader": "grub", "kernels": [{"image": True, "version": "6.18.57-gentoo-dist-bin"}]},
    }
    base.update(overrides)
    return base


# ---- describing systems ----------------------------------------------------------------------------------------------------------
def test_a_system_is_described_with_name_disk_file_system_and_encryption() -> None:
    row = model.describe_system(candidate(), DEVICES)
    assert row.title == "Gentoo Linux" and row.device == "/dev/nvme0n1p5" and not row.encrypted
    assert row.lines[0] == "On the disk Samsung SSD 990 PRO 1TB (1000.0 GiB), partition /dev/nvme0n1p5 (100.0 GiB)"
    assert row.lines[1] == "File system: ext4 · Not encrypted · Boot loader: GRUB · 1 kernel"


def test_an_encrypted_lvm_system_is_described_from_the_partition_at_the_bottom() -> None:
    row = model.describe_system(candidate(device="/dev/mapper/vg0-root", stack=["/dev/nvme0n1p3", "luks:/dev/mapper/rescue-1f2e3d4c5b6a", "lvm:vg0-root"]), DEVICES)
    assert row.encrypted and "partition /dev/nvme0n1p3 (400.0 GiB)" in row.lines[0]
    assert "Encrypted (LUKS)" in row.lines[1] and "LVM volume" in row.lines[1]


def test_warnings_snapshots_and_missing_disk_data_are_kept() -> None:
    row = model.describe_system(candidate(kind="snapshot", warnings=["This subvolume is read-only"], boot={}), [])
    assert row.snapshot and row.warnings == ["This subvolume is read-only"] and row.lines[0].startswith("On disk, partition /dev/nvme0n1p5")
    assert "0 kernels" in row.lines[1] and "Snapshot" in row.lines[1]


@pytest.mark.parametrize("info, expected", [
    ({"pretty_name": "Debian GNU/Linux 12 (bookworm)", "version_id": "12"}, "Debian GNU/Linux 12 (bookworm)"),
    ({"pretty_name": "Fedora Linux", "version_id": "41"}, "Fedora Linux 41"),
    ({"name": "Arch Linux"}, "Arch Linux"),
    ({}, "Linux"),
])
def test_the_distribution_name_carries_its_version(info, expected) -> None:
    assert model.os_name(info) == expected


def test_the_disk_of_a_partition_is_found_from_its_name() -> None:
    assert model.parent_disk(DEVICES, "/dev/nvme0n1p5")["model"].startswith("Samsung")
    assert model.parent_disk(DEVICES, "/dev/sda12")["model"] == "Kingston A400"
    assert model.parent_disk(DEVICES, "/dev/nvme0n1") is None and model.parent_disk(DEVICES, "/dev/mapper/vg0-root") is None


def test_locked_volumes_inactive_groups_and_arrays_come_from_the_scan() -> None:
    scan = {"devices": DEVICES, "stacks": {"luks": [{"device": "/dev/nvme0n1p3", "version": 2, "hints": ["TPM2 token"]}], "vgs": [{"name": "vg0", "active": False}, {"name": "vg1", "active": True}],
                                          "arrays": [{"uuid": "u1", "state": "not_assembled"}, {"uuid": "u2", "state": "assembled"}]}}
    [volume] = model.locked_volumes(scan)
    assert (volume.device, volume.size, volume.version, volume.hints) == ("/dev/nvme0n1p3", "400.0 GiB", 2, ["TPM2 token"])
    assert [group["name"] for group in model.inactive_groups(scan)] == ["vg0"]
    assert [array["uuid"] for array in model.unassembled_arrays(scan)] == ["u1"]


def test_the_live_medium_is_never_part_of_the_disk_overview() -> None:
    lines = model.disk_overview(DEVICES)
    assert len(lines) == 2 and not any("USB stick" in line for line in lines)
    assert "nvme0n1p5: ext4 100.0 GiB" in lines[0]


@pytest.mark.parametrize("environment, expected", [
    ({"uefi": True, "secure_boot": False, "network": {"ip": True}}, "Started in UEFI mode · Secure Boot is off · online"),
    ({"uefi": False}, "Started in legacy BIOS mode"),
    ({"uefi": True, "secure_boot": True, "network": {"ip": False}}, "Started in UEFI mode · Secure Boot is on · offline"),
])
def test_the_firmware_line(environment, expected) -> None:
    assert model.environment_text(environment) == expected


# ---- mounting ---------------------------------------------------------------------------------------------------------------------
def test_mount_rows_start_with_the_root_and_mark_data_as_optional() -> None:
    plan = {"root": {"device": "/dev/nvme0n1p5", "fstype": "btrfs", "rootflags": "subvolid=256"},
            "items": [{"mountpoint": "/efi", "device": "/dev/nvme0n1p1", "fstype": "vfat", "tier_name": "boot", "selected": True},
                      {"mountpoint": "/home", "device": "/dev/sdb1", "fstype": "ext4", "tier_name": "data", "selected": False, "needs_mkdir": True}]}
    rows = model.mount_rows(plan)
    assert [(row.mountpoint, row.kind, row.optional, row.selected) for row in rows] == [("/", "The system", False, True), ("/efi", "Boot files", False, True), ("/home", "Your data (optional)", True, False)]
    assert rows[0].note == "subvolume 256" and rows[2].note == "the folder is made when repairs are enabled"


def test_the_real_mount_repeats_the_dry_run_request_exactly() -> None:
    choice = MountChoice("c1", "s1", allow_write=False, include_tier4=["/home"], overrides=[{"mountpoint": "/efi", "device": "/dev/sda1"}])
    assert set(choice.request()) == {"candidate", "scan_id", "allow_write", "include_tier4", "overrides"}  # nothing in the window asks the engine to unmount another mount
    dry = {**choice.request(), "dry_run": True}
    real = {**choice.request(), "plan_digest": "abc"}
    assert {key: value for key, value in dry.items() if key != "dry_run"} == {key: value for key, value in real.items() if key != "plan_digest"}
    choice.include_tier4.append("/data")
    assert choice.request()["include_tier4"] == ["/home", "/data"] and dry["include_tier4"] == ["/home"]  # the request is a copy


# ---- diagnosis and actions -------------------------------------------------------------------------------------------------------
def test_findings_are_shown_errors_first_and_counted() -> None:
    findings = [{"id": "D22", "severity": "info"}, {"id": "D05", "severity": "error"}, {"id": "D13", "severity": "warn"}, {"id": "D02", "severity": "error"}]
    assert [item["id"] for item in model.sorted_findings(findings)] == ["D05", "D02", "D13", "D22"]
    assert model.finding_counts(findings) == {"error": 2, "warn": 1, "info": 1}


def specs(*ids):
    return [{"id": action, "title": action, "available": True} for action in ids]


def test_actions_are_grouped_for_a_person_and_the_recommended_repair_stands_alone() -> None:
    groups = model.group_actions(specs("repair.recommended", "acct.password", "boot.initramfs", "boot.bootloader", "efi.add", "fs.check", "fstab.fix", "pkg.finish", "session.enter_write", "future.thing"))
    assert [group.key for group in groups] == ["boot", "startup", "fstab", "efi", "filesystem", "accounts", "packages", "advanced", "other"]
    assert [item["id"] for item in groups[0].actions] == ["boot.bootloader"] and [item["id"] for item in groups[-1].actions] == ["future.thing"]
    assert "repair.recommended" not in {item["id"] for group in groups for item in group.actions}


def test_the_nvidia_group_only_appears_when_a_repair_in_it_can_be_used() -> None:
    names = ("gfx.nvidia_rebuild", "gfx.nvidia_fbdev_off", "gfx.nouveau_switch")
    off = [{"id": name, "title": name, "available": False, "reason": "no NVIDIA driver"} for name in names]
    groups = model.group_actions(specs("boot.config") + off)
    assert [group.key for group in groups] == ["boot"] and not any(item["id"] in names for group in groups for item in group.actions)  # greyed-out buttons would only be noise
    on = specs("boot.config") + off[:2] + [{"id": names[2], "title": "x", "available": True}]
    graphics = next(group for group in model.group_actions(on) if group.key == "graphics")
    assert [item["id"] for item in graphics.actions] == list(names)


def test_options_come_first_only_when_a_value_is_needed_or_a_choice_matters() -> None:
    catalogue = {item["id"]: item for item in [
        {"id": "boot.bootloader", "params": [{"name": "loader", "type": "enum", "choices": ["grub"], "default": ""}]},
        {"id": "acct.password", "params": [{"name": "username", "type": "text", "required": True}]},
        {"id": "fs.check", "params": [{"name": "mode", "type": "enum", "choices": ["check", "repair"], "default": "check"}]},
        {"id": "boot.initramfs", "params": [{"name": "generic", "type": "bool", "default": False}]},
        {"id": "boot.config"},
    ]}
    asked = {name: model.asks_first(spec) for name, spec in catalogue.items()}
    assert asked == {"boot.bootloader": False, "acct.password": True, "fs.check": True, "boot.initramfs": False, "boot.config": False}
    assert model.asks_first(catalogue["boot.initramfs"], hardware_changed=True)
    params = model.params_of(catalogue["fs.check"])
    assert (params[0].name, params[0].choices, params[0].default, params[0].required) == ("mode", ["check", "repair"], "check", False)


# ---- the confirmation of a plan ------------------------------------------------------------------------------------------------
def plan_with(risk: str, device: str = "/dev/nvme0n1p5") -> dict:
    """The confirm block exactly as Plan.confirm builds it."""
    name = device.rsplit("/", 1)[-1]
    return {"confirm": {"risk": risk, "required": risk != "info", "review": risk in {"medium", "high", "dangerous"}, "typed": name if risk in {"high", "dangerous"} else "",
                        "phrase": BACKUP_PHRASE if risk == "dangerous" else ""}}


def test_nothing_is_demanded_for_a_plan_that_only_looks() -> None:
    assert Confirmation.of(plan_with("info")).satisfied(False, "", "")


def test_a_low_risk_plan_needs_only_the_click_that_runs_it() -> None:
    confirm = Confirmation.of(plan_with("low"))
    assert confirm.required and not confirm.review and confirm.satisfied(False, "", "")


def test_a_medium_plan_needs_the_review_tick() -> None:
    confirm = Confirmation.of(plan_with("medium"))
    assert not confirm.satisfied(False, "", "") and confirm.satisfied(True, "", "")


def test_a_high_plan_needs_the_review_tick_and_the_device_name() -> None:
    confirm = Confirmation.of(plan_with("high"))
    assert confirm.typed == "nvme0n1p5"
    assert not confirm.satisfied(True, "", "") and not confirm.satisfied(True, "nvme0n1", "") and not confirm.satisfied(False, "nvme0n1p5", "")
    assert confirm.satisfied(True, "nvme0n1p5", "")


def test_a_dangerous_plan_also_needs_the_backup_phrase_word_for_word() -> None:
    confirm = Confirmation.of(plan_with("dangerous"))
    assert confirm.phrase == "I HAVE A BACKUP"
    assert not confirm.satisfied(True, "nvme0n1p5", "") and not confirm.satisfied(True, "nvme0n1p5", "i have a backup")
    assert confirm.satisfied(True, "nvme0n1p5", "I HAVE A BACKUP")


def test_failed_conditions_and_secrets_block_a_run() -> None:
    plan = {"preconditions": [{"ok": True, "text": "fine"}, {"ok": False, "text": "/boot is full"}], "secrets": ["password"]}
    assert model.failed_preconditions(plan) == ["/boot is full"]
    assert not model.secrets_ready(plan, {}) and not model.secrets_ready(plan, {"password": ("a", "b")}) and not model.secrets_ready(plan, {"password": ("", "")})
    assert not model.secrets_ready(plan, {"password": ("a\nb", "a\nb")})
    assert model.secrets_ready(plan, {"password": ("s3cret", "s3cret")}) and model.secrets_ready({}, {})


# ---- following a run ---------------------------------------------------------------------------------------------------------------
PLAN = {"steps": [{"id": "w-idle", "display": "Check that no terminal is open", "atomic": True, "group": "enter-write"},
                  {"id": "c1", "display": "chroot /mnt/rescue/r1 grub-mkconfig", "atomic": False}],
        "cleanup": [{"id": "f2", "display": "remove the temporary file", "atomic": True}]}


def stage(step, status, seq, **data):
    return {"type": "stage", "stage": "step", "job_id": "o1", "seq": seq, "message": step, "data": {"step": step, "status": status, **data}}


def test_a_run_is_followed_step_by_step_and_a_non_atomic_step_is_noticed() -> None:
    run = RunTracker(PLAN)
    assert [step.status for step in run.steps] == ["pending"] * 3 and not run.non_atomic_running and run.percent == 0
    run.feed(stage("w-idle", "running", 1, atomic=True))
    assert run.running.id == "w-idle" and not run.non_atomic_running
    run.feed(stage("w-idle", "completed", 2, rc=0))
    run.feed(stage("c1", "running", 3, atomic=False))
    assert run.non_atomic_running and run.running.id == "c1" and run.percent == 50
    run.feed({"type": "log", "seq": 4, "message": "Generating grub configuration file ..."})
    run.feed(stage("c1", "completed", 5, rc=0))
    assert not run.non_atomic_running and run.percent == 100 and run.log == ["Generating grub configuration file ..."]
    run.feed({"type": "result", "seq": 6, "data": {"plan_id": "p1", "outcome": "success", "steps": [], "post_checks": []}})
    run.feed({"type": "completed", "seq": 7, "data": {"success": True}})
    assert run.completed and run.success and run.outcome == "success" and run.job_id == "o1" and run.last_seq == 7


def test_events_seen_before_a_reconnect_are_not_applied_twice() -> None:
    run = RunTracker(PLAN)
    assert run.feed({"type": "log", "seq": 1, "message": "a"}) and run.feed({"type": "log", "seq": 2, "message": "b"})
    assert not run.feed({"type": "log", "seq": 2, "message": "b"}) and not run.feed({"type": "log", "seq": 1, "message": "a"})
    assert run.feed({"type": "log", "seq": 3, "message": "c"}) and run.log == ["a", "b", "c"]


def test_a_failed_step_keeps_its_exit_code_and_error_and_unknown_steps_are_added() -> None:
    run = RunTracker(PLAN)
    run.feed(stage("c1", "failed", 1, rc=1, error="grub-mkconfig: not found"))
    run.feed(stage("extra", "skipped", 2))
    [failed] = run.failed_steps()
    assert (failed.id, failed.rc, failed.error) == ("c1", 1, "grub-mkconfig: not found")
    assert run.steps[-1].id == "extra" and run.steps[-1].status == "skipped"
    run.feed({"type": "error", "seq": 3, "error": "rescue.command_failed", "message": "boom", "data": {"code": "rescue.command_failed"}})
    assert run.error is not None and run.error["data"]["code"] == "rescue.command_failed"


def test_a_rollback_is_noted() -> None:
    run = RunTracker(PLAN)
    run.feed({"type": "stage", "stage": "rollback", "seq": 1, "message": "Returning to the read-only mount"})
    assert run.notes == ["Returning to the read-only mount"]


# ---- the terminal --------------------------------------------------------------------------------------------------------------------
def test_the_terminal_command_uses_the_windows_own_socket_and_asks_nothing_again() -> None:
    assert model.terminal_argv("/run/gentoo-installer/server.sock", "r1a2b3c", "/dev/nvme0n1p5") == [
        "gentoo-rescue", "--socket", "/run/gentoo-installer/server.sock", "shell", "r1a2b3c", "--simple"]  # no device name, no backup phrase: the window asked once
    writable = model.terminal_argv("/s", "r1", "/dev/sda2", writable=True, sudo=True)
    assert writable[:2] == ["sudo", "gentoo-rescue"] and writable[-2:] == ["--simple", "--writable"]
    assert model.terminal_argv("/s", "r1", host=True)[-2:] == ["--simple", "--host"]


def test_the_shell_line_keeps_the_window_open_after_the_tool_ends() -> None:
    line = model.shell_line(["gentoo-rescue", "--socket", "/run/a b/server.sock", "shell", "r1"])
    first, *rest = line.split("; ")
    assert shlex.split(first) == ["gentoo-rescue", "--socket", "/run/a b/server.sock", "shell", "r1"]
    assert "read -r _unused" in line and model.HOLD_TEXT in line
    assert model.terminal_emulator_argv("xfce4-terminal", "Repair", line) == ["--title", "Repair", "-x", "sh", "-c", line]
    assert model.terminal_emulator_argv("xterm", "Repair", line) == ["-e", "sh", "-c", line]


# ---- texts ---------------------------------------------------------------------------------------------------------------------------
def test_error_texts_explain_the_next_step_and_unknown_codes_show_the_engine_message() -> None:
    assert "sudo gentoo-rescue" in messages.friendly_error("proto.unsupported_action", "Action must be inspect")
    assert messages.friendly_error("rescue.live_medium", "x") == messages.LIVE_MEDIUM_NOTICE
    assert messages.friendly_error("rescue.something_new", "Plain engine text.") == "Plain engine text."
    assert messages.friendly_error("rescue.something_new", "") == "Something went wrong. Please try again."
    assert "network icon" in messages.friendly_error("rescue.needs_network", "The computer has no route to the internet")
    reasons = messages.friendly_error("rescue.precondition_failed", "a; b", {"details": {"reasons": ["/boot is full", "no network"]}})
    assert "• /boot is full" in reasons and "• no network" in reasons


def test_the_backup_phrase_is_the_one_the_engine_asks_for() -> None:
    assert model.BACKUP_PHRASE == BACKUP_PHRASE


def test_every_risk_has_a_title_and_a_sentence() -> None:
    assert set(messages.RISK_TITLES) == set(RISKS) == set(messages.RISK_TEXTS)


def test_only_gentoo_systems_can_be_opened_and_the_others_are_named_once():
    found = [
        {"id": "c1", "device": "/dev/sda2", "kind": "root", "supported": True, "os": {"id": "gentoo"}},
        {"id": "c2", "device": "/dev/sda2", "kind": "snapshot", "supported": True, "os": {"id": "gentoo"}},
        {"id": "c3", "device": "/dev/sdb2", "kind": "root", "supported": False, "os": {"id": "ubuntu"}},
        {"id": "c4", "device": "/dev/sdb2", "kind": "snapshot", "supported": False, "os": {"id": "ubuntu"}},  # history of a system that is named already
        {"id": "c5", "device": "/dev/sdc1", "kind": "root", "os": {"id": "gentoo"}},  # an engine that does not say anything about support opens it as before
    ]
    openable, others = model.split_candidates(found)
    assert [item["id"] for item in openable] == ["c1", "c2", "c5"] and [item["id"] for item in others] == ["c3"]
    assert model.describe_system(found[2], []).supported is False and model.describe_system(found[0], []).supported is True and model.describe_system(found[4], []).supported is True
