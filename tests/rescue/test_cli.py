"""gentoo-rescue: exit codes, confirmation rules, separate invocations, the guided wizard."""

from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

import pytest

from gentoo_installerd.rescue import cli
from gentoo_installerd.rescue.api import RescueApi
from gentoo_installerd.rescue.client import LocalTransport, RescueClientError

from . import fakeroot as fr
from . import lsblk_fixtures as lf
from .apifake import FakeRegistry
from .flow import uefi_gentoo_lab


class Run:
    """One invocation of the CLI against a lab; every invocation gets a fresh manager that adopts saved sessions (--local)."""

    def __init__(self, lab) -> None:
        self.lab = lab

    def __call__(self, *argv: str, answers: list[str] | None = None, secrets: list[str] | None = None, interactive: bool = False, stdin: str | None = None, monkeypatch=None):
        out, err = io.StringIO(), io.StringIO()
        scripted, hidden = list(answers or []), list(secrets or [])
        console = cli.Console(out=out, err=err, ask=lambda _p: scripted.pop(0) if scripted else "", secret=lambda _p: hidden.pop(0) if hidden else "", interactive=interactive)
        transport = LocalTransport(RescueApi(self.lab.manager(auto_adopt=True)))
        if stdin is not None and monkeypatch is not None:
            monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        code = cli.main(list(argv), transport=transport, console=console)
        return code, out.getvalue(), err.getvalue()


def prepared(lab_factory, **options):
    lab, tree = uefi_gentoo_lab(lab_factory, **options)
    return lab, tree, Run(lab)


def system_id(run: Run) -> str:
    code, out, _ = run("scan")
    assert code == 0
    return re.search(r"nvme0n1p5\s+.*\[(c[0-9a-f]{8})\]", out).group(1)


def mount(run: Run) -> str:
    cid = system_id(run)
    code, out, err = run("mount", cid, "--yes")
    assert code == 0, err
    return re.search(r"session (r[0-9a-f]{6})", out).group(1)


def test_scan_lists_the_system_and_hides_the_live_medium(lab_factory) -> None:
    lab, _, run = prepared(lab_factory)
    code, out, err = run("scan")
    assert code == 0 and "Firmware: UEFI" in out and "Gentoo Linux" in out and "/dev/nvme0n1p5" in out and "nvme0n1p5" in out
    code, out, _ = run("--json", "scan")
    events = [json.loads(line) for line in out.splitlines()]
    assert code == 0 and events[-1]["type"] == "completed" and any(e["type"] == "result" for e in events)


def test_mount_shows_the_plan_and_needs_yes_when_not_interactive(lab_factory) -> None:
    lab, _, run = prepared(lab_factory)
    cid = system_id(run)
    code, out, err = run("mount", cid, "--dry-run")
    assert code == 0 and "Mount plan" in out and "/efi" in out and "blockdev --setro" in out and not lab.system.mounted_targets()
    code, out, err = run("mount", cid)
    assert code == 4 and "Re-run with --yes" in err and re.search(r"digest [0-9a-f]{64}", err) and not lab.system.mounted_targets()
    code, out, err = run("mount", cid, "--yes")
    assert code == 0 and "Mounted at" in out and "read-only" in out and lab.system.mounted_targets()


def test_the_plan_prints_the_exact_commands_and_the_flags_to_run_it(lab_factory) -> None:
    lab, tree, run = prepared(lab_factory)
    (tree / "boot/grub/grub.cfg").unlink()
    sid = mount(run)
    code, out, _ = run("plan", sid, "boot.config")
    assert code == 0 and "[risk: MEDIUM]" in out and "grub-mkconfig -o /boot/grub/grub.cfg" in out
    digest = re.search(r"digest ([0-9a-f]{64})", out).group(1)
    assert f"gentoo-rescue run {sid} boot.config --confirm-plan {digest}" in out
    assert not lab.system.chroot_calls  # planning runs nothing of the system


def test_run_without_a_confirmation_runs_nothing(lab_factory) -> None:
    lab, tree, run = prepared(lab_factory)
    sid = mount(run)
    code, out, err = run("run", sid, "boot.config")
    assert code == 4 and "Nothing was run. To run exactly this plan, add: --confirm-plan" in err and not lab.system.chroot_calls
    code, out, err = run("run", sid, "boot.config", "--confirm-plan", "0" * 64)
    assert code == 4 and "digest differs" in err and not lab.system.chroot_calls


def test_run_with_the_digest_executes_in_a_separate_invocation(lab_factory) -> None:
    lab, tree, run = prepared(lab_factory)
    (tree / "boot/grub/grub.cfg").unlink()
    sid = mount(run)
    _, out, _ = run("plan", sid, "boot.config")
    digest = re.search(r"digest ([0-9a-f]{64})", out).group(1)
    code, out, err = run("run", sid, "boot.config", "--confirm-plan", digest)
    assert code == 0, err
    assert "Done." in out and "[ok] /boot/grub/grub.cfg" in out and ("grub-mkconfig", "-o", "/boot/grub/grub.cfg") in [c for _r, _e, c in lab.system.chroot_calls]
    code, out, _ = run("status", sid)
    assert "rw" in out
    code, out, err = run("unmount", sid)
    assert code == 0 and "Unmounted" in out and lab.system.mounted_targets() == []
    assert (tree / "boot/grub/grub.cfg").exists()


def test_high_risk_runs_need_typed_and_dangerous_ones_the_backup_flag(lab_factory) -> None:
    lab, tree, run = prepared(lab_factory)
    sid = mount(run)
    _, out, _ = run("plan", sid, "boot.bootloader")
    digest = re.search(r"digest ([0-9a-f]{64})", out).group(1)
    code, _, err = run("run", sid, "boot.bootloader", "--confirm-plan", digest)
    assert code == 4 and "--typed nvme0n1p5" in err and not lab.system.chroot_calls
    code, _, err = run("run", sid, "boot.bootloader", "--confirm-plan", digest, "--typed", "nvme0n1p5")
    assert code == 0
    code, out, err = run("fsck", sid, "--force")
    assert code == 4 and "--typed nvme0n1p5" in err
    digest = re.search(r"digest ([0-9a-f]{64})", run("plan", sid, "fs.check", "--param", "mode=repair_force")[1]).group(1)
    code, _, err = run("fsck", sid, "--force", "--confirm-plan", digest, "--typed", "nvme0n1p5")
    assert code == 4 and "--i-have-a-backup" in err
    code, out, err = run("fsck", sid, "--force", "--confirm-plan", digest, "--typed", "nvme0n1p5", "--i-have-a-backup")
    assert code == 0 and ("e2fsck", "-f", "-y", "/dev/nvme0n1p5") in lab.system.commands


def test_interactive_confirmation(lab_factory) -> None:
    lab, tree, run = prepared(lab_factory)
    (tree / "boot/grub/grub.cfg").unlink()
    sid = mount(run)
    code, out, _ = run("run", sid, "boot.config", answers=["n"], interactive=True)
    assert code == 4 and "Run these" in out and "Nothing was run." in out and not lab.system.chroot_calls
    code, out, _ = run("run", sid, "boot.config", answers=["y"], interactive=True)
    assert code == 0 and "Done." in out
    code, out, _ = run("run", sid, "boot.bootloader", answers=["wrong"], interactive=True)
    assert code == 4 and 'Type "nvme0n1p5"' in out
    code, out, _ = run("run", sid, "boot.bootloader", answers=["nvme0n1p5"], interactive=True)
    assert code == 0


def test_exit_codes(lab_factory) -> None:
    lab, tree, run = prepared(lab_factory)
    assert run("status", "r000000")[0] == 5 or run("diagnose", "r000000")[0] == 5
    assert run("mount", "/dev/sdb1:subvolid=x")[0] in {1, 2, 5, 7}
    assert run("mount", "not-a-system")[0] == 2
    assert run("scan", "--bogus")[0] == 2
    assert run()[0] == 2  # no command and not interactive
    sid = mount(run)
    assert run("plan", sid, "no.such.action")[0] == 2
    assert run("plan", sid, "efi.delete", "--param", "bootnum=ZZZZ")[0] == 2
    assert run("plan", sid, "efi.delete", "--param", "bootnum=0042")[0] == 5
    assert run("plan", sid, "fstab.fix")[0] == 7  # nothing to fix on a healthy system: reported as not applicable
    assert run("plan", sid, "session.leave_write")[0] == 7
    code, _, err = run("mount", "/dev/sdb1", "--yes")
    assert code in {0, 6}


def test_a_live_medium_is_refused_with_exit_3(lab_factory) -> None:
    lab = lab_factory(lf.usb_live_hybrid())
    lab.system.live_mounts.append(("/dev/sdb1", "/run/initramfs/live"))
    run = Run(lab)
    code, out, err = run("mount", "/dev/sdb1", "--yes")
    assert code == 3 and "live system is running from" in err and not lab.system.mounted_targets()


def test_unlock_reads_the_passphrase_from_standard_input_and_never_argv(lab_factory, monkeypatch) -> None:
    from .fakes import LuksSpec, VgSpec

    lab = lab_factory(lambda s: lf.luks_lvm_ext4(unlocked=bool(s.mapped), activated=s.vgs["vg0"].active))
    lab.system.luks["/dev/nvme0n1p3"] = LuksSpec(fr.LUKS_UUID, "hunter2 with spaces")
    lab.system.vgs["vg0"] = VgSpec("vg0", "u", ["/dev/mapper/rescue-1f2e3d4c5b6a"], [{"name": "root"}])
    lab.add_tree("/dev/mapper/vg0-root", "gentoo", firmware="uefi")
    run = Run(lab)
    code, out, err = run("unlock", "/dev/nvme0n1p3", "--passphrase-stdin", stdin="wrong\n", monkeypatch=monkeypatch)
    assert code == 1 and "did not unlock" in err and "keyboard layout" in err
    code, out, err = run("unlock", "/dev/nvme0n1p3", "--passphrase-stdin", stdin="hunter2 with spaces\n", monkeypatch=monkeypatch)
    assert code == 0 and "Unlocked as /dev/mapper/rescue-1f2e3d4c5b6a" in out
    assert all("hunter2" not in arg for command in lab.system.commands for arg in command)
    code, out, err = run("unlock", "/dev/nvme0n1p3", secrets=["hunter2 with spaces"])
    assert code == 0
    code, out, err = run("activate", "lvm", "vg0")
    assert code == 0 and "rare cases LVM repairs" in out and ("vgchange", "-ay", "--nohints", "vg0") in lab.system.commands
    code, out, _ = run("status")
    assert "leftover: (stored volumes)" in out
    code, out, _ = run("unmount")
    assert code == 0 and lab.system.mapped == {}


def test_password_asks_twice_and_never_prints_it(lab_factory) -> None:
    lab, tree, run = prepared(lab_factory)
    sid = mount(run)
    code, out, err = run("password", sid, "root", answers=["y"], secrets=["a", "b"], interactive=True)
    assert code == 4 or code == 2
    assert not [c for c in lab.system.calls if c.secret]
    code, out, err = run("password", sid, "root", "--confirm-plan", re.search(r"digest ([0-9a-f]{64})", run("plan", sid, "acct.password", "--param", "username=root")[1]).group(1), "--typed", "nvme0n1p5", secrets=["N3w-pass", "N3w-pass"])
    assert code == 0 and "N3w-pass" not in out + err
    assert [c for c in lab.system.calls if c.secret][0].stdin == "root:N3w-pass\n"


def test_efi_commands(lab_factory) -> None:
    lab, tree, run = prepared(lab_factory)
    lab.system.nvram["entries"]["0000"] = {"label": "Windows Boot Manager", "loader": "\\EFI\\Microsoft\\Boot\\bootmgfw.efi", "active": True, "part": "1"}
    lab.system.nvram["order"] = ["0000"]
    sid = mount(run)
    code, out, _ = run("efi", "list", sid)
    assert code == 0 and "Windows Boot Manager" in out and "Boot order: 0000" in out
    digest = re.search(r"digest ([0-9a-f]{64})", run("plan", sid, "efi.add", "--param", "loader=\\EFI\\Gentoo\\grubx64.efi", "--param", "label=Gentoo")[1]).group(1)
    code, out, err = run("efi", "add", sid, "--loader", "\\EFI\\Gentoo\\grubx64.efi", "--label", "Gentoo", "--confirm-plan", digest)
    assert code == 0 and "Done." in out
    code, out, _ = run("efi", "list", sid)
    assert "Gentoo" in out
    bios = lab_factory(lf.debian_cloud(), uefi=False, name="bios")
    bios.add_tree("/dev/vda1", "gentoo", firmware="bios")
    brun = Run(bios)
    cid = re.search(r"\[(c[0-9a-f]{8})\]", brun("scan")[1]).group(1)
    code, out, _ = brun("mount", cid, "--yes")
    bsid = re.search(r"session (r[0-9a-f]{6})", out).group(1)
    code, out, _ = brun("efi", "list", bsid)
    assert code == 7 and "legacy BIOS mode" in out


def test_logs_and_the_journal_confirmation(lab_factory) -> None:
    lab, tree, run = prepared(lab_factory)
    sid = mount(run)
    code, out, _ = run("logs", sid)
    assert code == 0 and "/var/log/messages" in out and "kernel log of the boot that failed" in out
    code, out, _ = run("logs", sid, "--source", "/var/log/messages", "--lines", "5")
    assert code == 0 and "localhost kernel: boot" in out
    code, out, err = run("logs", sid, "--source", "/etc/shadow")
    assert code == 3 and "not one of the logs" in err
    code, out, err = run("logs", sid, "--journal")
    assert code == 4 and "runs the system's own journalctl" in err and not lab.system.chroot_calls
    code, out, _ = run("logs", sid, "--pstore")
    assert code == 0 and "ramoops" in out
    code, out, _ = run("diagnose", sid)
    assert code == 0 and "Diagnosis" in out
    code, out, _ = run("actions", sid)
    assert code == 0 and "boot.bootloader" in out and "enable repairs" in out.lower() or "session.enter_write" in out


def test_unmount_reports_what_keeps_it_busy(lab_factory) -> None:
    lab, tree, run = prepared(lab_factory)
    sid = mount(run)
    root = lab.base / sid
    lab.system.fail_umount[str(root / "efi")] = (32, "umount: busy: target is busy.")
    lab.system.holders[str(root / "efi")] = "                     USER        PID ACCESS COMMAND\n" + f"{root}/efi:      gentoo     4321 ..c.. thunar\n"
    code, out, err = run("unmount", sid)
    assert code == 6 and "Still in use by:" in out and "thunar" in out and "--force" in out
    code, out, _ = run("unmount", sid, "--force")
    assert code == 0 and lab.system.mounted_targets() == []


def test_cleanup_releases_what_a_crashed_run_left(lab_factory) -> None:
    lab, tree, run = prepared(lab_factory)
    sid = mount(run)
    code, out, _ = run("status")
    assert code == 0 and sid in out or "1 session" in out
    # a new process: the session exists only in its state file
    code, out, _ = run("cleanup")
    assert code == 0 and "released" in out and lab.system.mounted_targets() == [] and not any(lab.system.ro.values())
    assert not list((lab.state_dir / "sessions").glob("*.json"))


def test_the_local_mode_needs_root(lab_factory, monkeypatch) -> None:
    out, err = io.StringIO(), io.StringIO()
    console = cli.Console(out=out, err=err, interactive=False)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert cli.main(["--local", "scan"], console=console) == 1 and "needs root" in err.getvalue()
    err.truncate(0)
    err.seek(0)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    assert cli.main(["--socket", "/nonexistent/socket", "scan"], console=console) == 1 and "not reachable" in err.getvalue()


def test_parsers() -> None:
    assert cli.parse_params(["generic=yes", "kernels=a,b", "x=1=2", "unlock=false"]) == {"generic": True, "kernels": ["a", "b"], "x": "1=2", "unlock": False}
    assert cli.parse_choices(["3=replace:UUID=abc", "5=nofail"]) == {"3": {"action": "replace", "spec": "UUID=abc"}, "5": {"action": "nofail"}}
    for bad in (["noequals"],):
        with pytest.raises(ValueError):
            cli.parse_params(bad)
    with pytest.raises(ValueError):
        cli.parse_choices(["x=nofail"])


def test_the_wizard_walks_through_a_repair(lab_factory) -> None:
    lab, tree, run = prepared(lab_factory)
    (tree / "boot/grub/grub.cfg").unlink()
    code, out, err = run("wizard", answers=["1", "y", "2", "y", "u"], interactive=True)
    assert code == 0, err
    assert "nothing is changed until you confirm" in out and "Mount plan" in out and "What now?" in out and "Regenerate the boot menu *" in out
    assert "Done." in out and "Unmounted. You can remove the live medium" in out
    assert lab.system.mounted_targets() == [] and (tree / "boot/grub/grub.cfg").exists()
    kept, _, _ = run("wizard", "--keep-mounted", answers=["1", "y", "u"], interactive=True)
    assert lab.system.mounted_targets()  # --keep-mounted leaves the system for a terminal session
    run("cleanup")


def test_the_wizard_quits_cleanly(lab_factory) -> None:
    lab, tree, run = prepared(lab_factory)
    code, out, _ = run("wizard", answers=["q"], interactive=True)
    assert code == 0 and not lab.system.mounted_targets()
    code, out, _ = run("wizard", answers=["x", "99", "q"], interactive=True)
    assert "Please type one of the numbers" in out


def test_old_daemons_are_reported_clearly() -> None:
    class Old:
        def call(self, request):
            yield {"type": "error", "message": "Action must be inspect, start, status, or cancel", "error": "Action must be inspect, start, status, or cancel"}

        def shell(self, request):
            raise NotImplementedError

    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["scan"], transport=Old(), console=cli.Console(out=out, err=err, interactive=False))
    assert code == 1 and "does not support repair mode" in err.getvalue()
