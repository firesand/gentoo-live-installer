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

from gentoo_installerd.rescue.osrelease import NOT_GENTOO

from . import fakeroot as fr
from . import lsblk_fixtures as lf
from .apifake import FakeRegistry
from .flow import uefi_gentoo_lab


class Run:
    """One invocation of the CLI against a lab; every invocation gets a fresh manager that adopts saved sessions (--local)."""

    def __init__(self, lab, shell_spawn=None) -> None:
        self.lab = lab
        self.shell_spawn = shell_spawn

    def __call__(self, *argv: str, answers: list[str] | None = None, secrets: list[str] | None = None, interactive: bool = False, stdin: str | None = None, monkeypatch=None):
        out, err = io.StringIO(), io.StringIO()
        scripted, hidden = list(answers or []), list(secrets or [])
        console = cli.Console(out=out, err=err, ask=lambda _p: scripted.pop(0) if scripted else "", secret=lambda _p: hidden.pop(0) if hidden else "", interactive=interactive)
        manager = self.lab.manager(auto_adopt=True)
        manager.shell_spawn = self.shell_spawn
        transport = LocalTransport(RescueApi(manager))
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


def test_scan_lists_a_system_that_is_not_gentoo_by_name_without_a_number_or_an_id(lab_factory) -> None:
    lab, _, run = prepared(lab_factory)  # Gentoo on the NVMe disk, a Debian on the second disk
    code, out, err = run("scan")
    assert code == 0, err
    lines = out.splitlines()
    gentoo = next(line for line in lines if "Gentoo Linux" in line)
    debian = next(line for line in lines if "Debian GNU/Linux" in line)
    assert "/dev/nvme0n1p5" in gentoo and "/dev/sdb1" in debian
    assert gentoo.lstrip().startswith("1) Gentoo Linux") and re.search(r"\[c[0-9a-f]{8}\]", gentoo)
    assert debian.lstrip().startswith("-  Debian GNU/Linux 13 (trixie)") and debian.endswith("Not a Gentoo system - this repair tool only supports Gentoo") and not re.search(r"\[c[0-9a-f]{8}\]", debian)
    code, out, _ = run("--json", "scan")
    sdb = next(item for item in next(json.loads(line) for line in out.splitlines() if '"candidates"' in line)["data"]["candidates"] if item["device"] == "/dev/sdb1")
    assert sdb["supported"] is False


def test_a_system_that_is_not_gentoo_cannot_be_mounted(lab_factory) -> None:
    lab, _, run = prepared(lab_factory)
    scan = next(json.loads(line) for line in run("--json", "scan")[1].splitlines() if '"candidates"' in line)["data"]
    other = next(item["id"] for item in scan["candidates"] if item["device"] == "/dev/sdb1")
    for system in ("/dev/sdb1", other):
        code, out, err = run("mount", system, "--yes")
        assert code == 7 and "Not a Gentoo system - this repair tool only supports Gentoo" in err and not lab.system.mounted_targets()
        assert run("mount", system, "--dry-run")[0] == 7


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
    code, out, err = run("status", "r000000")
    assert code == 5 and "There is no repair session r000000" in err
    assert run("diagnose", "r000000")[0] == 5
    code, out, err = run("mount", "/dev/sdb1:subvolid=x", "--yes")
    assert code == 2 and "subvolid=NUMBER" in err and "Traceback" not in out + err and not lab.system.mounted_targets()
    assert run("mount", "/dev/sdb1:subvolid=256", "--yes")[0] == 2  # an ext4 file system has no subvolumes
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
    assert code == 7 and "Not a Gentoo system - this repair tool only supports Gentoo" in err  # a system that is not Gentoo: refused, as "unsupported"


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
    assert code == 4 and "Nothing was run." in out  # the device name must be typed first (high risk)
    assert not [c for c in lab.system.calls if c.secret]
    code, out, err = run("password", sid, "root", answers=["nvme0n1p5"], secrets=["first", "second"], interactive=True)
    assert code == 2 and "The two password entries differ. Nothing was run." in err and "first" not in out + err
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
    assert code == 0 and "boot.bootloader" in out and "session.enter_write" in out and "Enable repairs" in out


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
    assert code == 0 and "1 session(s) mounted." in out and sid in out and "leftover" not in out  # a session of an earlier command is not a leftover
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


def test_the_wizard_lists_a_system_that_is_not_gentoo_but_gives_it_no_number(lab_factory) -> None:
    lab, _, run = prepared(lab_factory)  # one Gentoo (1) and a Debian, which would have been 2
    code, out, err = run("wizard", answers=["2", "q"], interactive=True)
    assert code == 0, err
    assert "1) Gentoo Linux" in out and "-  Debian GNU/Linux 13 (trixie)" in out and "Not a Gentoo system - this repair tool only supports Gentoo" in out
    assert "Please type one of the numbers shown." in out and not lab.system.mounted_targets()
    only = lab_factory(lf.plain_sata_mbr(), name="only")
    only.add_tree("/dev/sda1", "ubuntu")
    code, out, err = Run(only)("wizard", answers=["1", "q"], interactive=True)
    assert code == 0, err
    assert "No Gentoo system was found." in out and "-  Ubuntu 24.04.1 LTS" in out and "Please type one of the numbers shown." in out and not only.system.mounted_targets()  # there is no 1 to choose


def test_a_snapshot_of_a_system_that_is_not_gentoo_is_not_listed_again() -> None:
    """A Btrfs machine with Timeshift or snapper has a dozen snapshots of the same Ubuntu: one line says what the system is."""
    scan = {"candidates": [
        {"id": "c1", "device": "/dev/sda2", "kind": "root", "fstype": "btrfs", "supported": True, "os": {"pretty_name": "Gentoo Linux"}, "warnings": []},
        {"id": "c2", "device": "/dev/sdb2", "kind": "root", "fstype": "btrfs", "supported": False, "os": {"pretty_name": "Ubuntu 24.04.1 LTS"}, "warnings": [NOT_GENTOO]},
        {"id": "c3", "device": "/dev/sdb2", "kind": "snapshot", "fstype": "btrfs", "supported": False, "os": {"pretty_name": "Ubuntu 24.04.1 LTS"}, "warnings": [NOT_GENTOO]},
    ]}
    out = io.StringIO()
    shown = cli.render_systems(cli.Console(out=out, err=io.StringIO(), interactive=False), scan)
    assert [item["id"] for item in shown] == ["c1"] and out.getvalue().count("Ubuntu 24.04.1 LTS") == 1 and out.getvalue().count(NOT_GENTOO) == 1


def test_help_says_what_the_tool_is_for(capsys) -> None:
    assert cli.main(["--help"], console=cli.Console(out=io.StringIO(), err=io.StringIO(), interactive=False)) == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Repair a Gentoo system that does not start" in help_text and "Other Linux distributions are listed by a scan but not supported" in help_text


def test_old_daemons_are_reported_clearly() -> None:
    class Old:
        def call(self, request):
            yield {"type": "error", "message": "Action must be inspect, start, status, or cancel", "error": "Action must be inspect, start, status, or cancel"}

        def shell(self, request):
            raise NotImplementedError

    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["scan"], transport=Old(), console=cli.Console(out=out, err=err, interactive=False))
    assert code == 1 and "does not support repair mode" in err.getvalue()


def test_mount_has_no_option_that_pretends_to_unmount_another_mount(lab_factory, capsys) -> None:
    """--unmount-existing only silenced the check and unmounted nothing; the probe mount then failed anyway."""
    lab, _tree, run = prepared(lab_factory)
    code, _out, _err = run("mount", "--unmount-existing", "/run/media/gentoo/root", "--dry-run", "--auto")
    assert code == 2 and "unrecognized arguments: --unmount-existing" in capsys.readouterr().err and not lab.system.commands_named("mount")
    run("mount", "--help")
    assert "--unmount-existing" not in capsys.readouterr().out


def test_mount_auto_picks_the_only_gentoo_and_refuses_when_there_are_several(lab_factory) -> None:
    lab, _, run = prepared(lab_factory)  # Gentoo on the NVMe disk and a Debian on the second disk: the Debian is listed, but it is not a choice
    code, out, err = run("mount", "--auto", "--yes")
    assert code == 0, err
    assert "Mounted at" in out and len(lab.system.mounted_targets()) == 2 and not any("sdb1" in target for target in lab.system.mounted_targets())
    run("cleanup")
    code, out, err = run("mount", "--yes")
    assert code == 2 and "Say which system to mount" in err
    two = lab_factory(lf.multiboot_three(), name="two")
    two.add_tree("/dev/nvme0n1p2", "gentoo", firmware="uefi")
    two.add_tree("/dev/nvme0n1p3", "gentoo", firmware="uefi", root_uuid=fr.HOME_UUID)
    code, out, err = Run(two)("mount", "--auto", "--yes")
    assert code == 2 and "Several Gentoo systems were found" in err and "/dev/nvme0n1p2" in out and "/dev/nvme0n1p3" in out and not two.system.mounted_targets()
    single = lab_factory(lf.plain_sata_mbr())
    single.add_tree("/dev/sda1", "gentoo")
    code, out, err = Run(single)("mount", "--auto", "--yes")
    assert code == 0, err
    assert "Mounted at" in out and single.system.mounted_targets()


def test_mount_auto_does_not_pick_a_system_that_is_not_gentoo(lab_factory) -> None:
    lab = lab_factory(lf.plain_sata_mbr())
    lab.add_tree("/dev/sda1", "ubuntu")
    code, out, err = Run(lab)("mount", "--auto", "--yes")
    assert code == 5 and "No Gentoo system was found" in err and "Not a Gentoo system - this repair tool only supports Gentoo" in out and not lab.system.mounted_targets()


def test_mount_auto_explains_an_empty_scan(lab_factory) -> None:
    lab = lab_factory(lf.windows_only())
    code, out, err = Run(lab)("mount", "--auto", "--yes")
    assert code == 5 and "No Gentoo system was found" in err and "unlock" in err and not lab.system.mounted_targets()


class SpawnedShell:
    """Stands in for subprocess.Popen: the terminal of a session is a program on the console, never started by a test."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))

        class Process:
            pid = 4321
            returncode = 0

            def wait(self_inner):
                return 0

        return Process()


def test_chroot_and_umount_are_other_names_for_shell_and_unmount(lab_factory, capsys) -> None:
    lab, tree, _ = prepared(lab_factory)
    spawned = SpawnedShell()
    run = Run(lab, shell_spawn=spawned)
    sid = mount(run)
    code, out, err = run("chroot", sid)
    assert code == 4 and "Type nvme0n1p5 to confirm" in err and not spawned.calls  # a terminal needs the typed device name, like the daemon form
    code, out, err = run("chroot", sid, "--typed", "nvme0n1p5")
    assert code == 0, err
    argv = spawned.calls[0]
    assert argv[:3] == ["setsid", "--ctty", "script"] and "READ-ONLY" in capsys.readouterr().out  # the console form prints its banner on the terminal itself
    assert "rescue-chroot --userspec=0:0 --groups=" in argv[argv.index("--command") + 1]
    code, out, err = run("umount", sid)
    assert code == 0 and "Unmounted" in out and lab.system.mounted_targets() == []


def test_grub_repair_is_the_bootloader_plan_with_the_usual_confirmation(lab_factory) -> None:
    lab, tree, run = prepared(lab_factory)
    sid = mount(run)
    code, out, err = run("grub-repair", sid)
    digest = re.search(r"digest ([0-9a-f]{64})", out).group(1)
    assert code == 4 and "grub-install" in out and "--typed nvme0n1p5" in err and not lab.system.chroot_calls
    code, out, err = run("grub-repair", sid, "--confirm-plan", digest, "--typed", "nvme0n1p5")
    assert code == 0, err
    assert any(command[0] == "grub-install" for _root, _env, command in lab.system.chroot_calls) and "Done." in out
    code, out, err = run("grub-repair", sid, "--param", "firmware=bios")
    assert code == 4 and "--target=i386-pc" in out  # the parameters of the action are passed through


def test_the_wizard_offers_one_fix_for_everything_that_was_found(lab_factory) -> None:
    lab, tree, run = prepared(lab_factory, initramfs=False)
    (tree / "boot/grub/grub.cfg").unlink()
    code, out, err = run("wizard", answers=["1", "y", "0", "y", "u"], interactive=True)
    assert code == 0, err
    assert "0) Fix everything recommended *" in out and "Fix everything recommended   [risk: MEDIUM]" in out
    assert f"dracut --force /boot/initramfs-{fr.GENTOO_KVER}.img {fr.GENTOO_KVER}" in out and "--regenerate-all" not in out  # only the image that is missing is rebuilt
    assert "grub-mkconfig -o /boot/grub/grub.cfg" in out and "Done." in out
    assert (tree / "boot/grub/grub.cfg").exists() and (tree / f"boot/initramfs-{fr.GENTOO_KVER}.img").exists() and lab.system.mounted_targets() == []
