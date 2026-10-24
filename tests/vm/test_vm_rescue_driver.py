"""scripts/vm_rescue.py: the non-interactive repair of a Gentoo with gentoo-rescue, and what is checked around it.

The driver is also run against the real ``gentoo-rescue`` command line, on the rescue tests' laboratory (a fake kernel and fake
tools, tests/rescue), so the harness reads the output the tool really prints.
"""
from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import vm_rescue
from gentoo_installerd.rescue import cli
from gentoo_installerd.rescue.api import RescueApi
from gentoo_installerd.rescue.client import LocalTransport
from tests.rescue.flow import uefi_gentoo_lab
from tests.rescue.lab import Lab


# --- the base image of --target-image ------------------------------------------------------------------------------------

def test_a_base_image_is_fingerprinted_by_size_time_and_its_first_mebibyte(tmp_path):
    base = tmp_path / "base.qcow2"
    base.write_bytes(b"A" * (3 << 20))
    before = vm_rescue.fingerprint(base)
    assert before["size"] == 3 << 20 and len(before["head_sha256"]) == 64
    assert vm_rescue.fingerprint_problems(before, vm_rescue.fingerprint(base)) == []
    with base.open("r+b") as stream:
        stream.seek(10)
        stream.write(b"B")
    os.utime(base, ns=(1, 1))
    after = vm_rescue.fingerprint(base)
    assert vm_rescue.fingerprint_problems(before, after) == ["the modification time of the base image changed", "the first MiB of the base image changed"]
    with base.open("ab") as stream:
        stream.write(b"C")
    assert "the size of the base image changed" in vm_rescue.fingerprint_problems(before, vm_rescue.fingerprint(base))
    with base.open("r+b") as stream:  # a write beyond the first MiB changes size or time, never nothing
        stream.seek(2 << 20)
        stream.write(b"Z")
    assert vm_rescue.fingerprint_problems(after, vm_rescue.fingerprint(base))


# --- the arguments of the live medium ----------------------------------------------------------------------------------------

def test_arguments_of_the_live_medium_are_found():
    line = "BOOT_IMAGE=/boot/gentoo root=live:CDLABEL=GENTOO rd.live.dir=/ rd.live.squashimg=image.squashfs cdroot nox"
    assert vm_rescue.live_arguments(line) == ["cdroot", "rd.live.dir=/", "rd.live.squashimg=image.squashfs", "root=live:CDLABEL=GENTOO"]
    assert vm_rescue.live_arguments("root=UUID=1-2 ro console=ttyS0") == []


# --- the boot files of the repaired system ------------------------------------------------------------------------------

def run_snippet(snippet: str, *arguments: str, env: dict | None = None) -> tuple[int, str]:
    result = subprocess.run([sys.executable, "-I", "-c", snippet, *arguments], capture_output=True, text=True, check=False, env={**os.environ, **(env or {})})
    return result.returncode, result.stdout


def system_tree(root: Path, *, menu: str = "menuentry 'Gentoo' { linux /vmlinuz root=UUID=1 ro }\n" * 12, entries: dict | None = None) -> Path:
    (root / "boot" / "grub").mkdir(parents=True)
    (root / "boot" / "grub" / "grub.cfg").write_text(menu)
    (root / "etc" / "default").mkdir(parents=True)
    (root / "etc" / "default" / "grub").write_text('GRUB_CMDLINE_LINUX="console=ttyS0"\n')
    for name, text in (entries or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def boot_report(root: Path) -> dict:
    code, output = run_snippet(vm_rescue.BOOT_FILES_SNIPPET, json.dumps({"root": str(root), "files": list(vm_rescue.BOOT_FILES), "pattern": vm_rescue.LIVE_PATTERN}))
    return vm_rescue.parse_boot_files(code, output)


def test_a_clean_system_has_boot_files_without_live_arguments(tmp_path):
    report = boot_report(system_tree(tmp_path))
    assert report["checked"] == ["/boot/grub/grub.cfg", "/etc/default/grub"] and report["matches"] == []
    assert vm_rescue.boot_file_problems(report) == []


def test_live_arguments_in_a_menu_entry_or_the_defaults_are_reported_with_file_and_line(tmp_path):
    root = system_tree(tmp_path, menu="menuentry 'x' {\n linux /boot/gentoo root=live:CDLABEL=GENTOO ro\n}\n" * 3,
                       entries={"boot/loader/entries/a.conf": "title A\noptions root=UUID=1 rd.live.image\n"})
    problems = vm_rescue.boot_file_problems(boot_report(root))
    assert any("/boot/grub/grub.cfg:2 carries an argument of the live medium" in problem for problem in problems)
    assert any("/boot/loader/entries/a.conf:2" in problem for problem in problems)


def test_a_repaired_system_without_any_menu_file_proves_nothing(tmp_path):
    (tmp_path / "etc" / "default").mkdir(parents=True)
    (tmp_path / "etc" / "default" / "grub").write_text("GRUB_TIMEOUT=1\n")
    problems = vm_rescue.boot_file_problems(boot_report(tmp_path))
    assert problems and "no boot menu file" in problems[0]


def test_files_that_point_out_of_the_system_are_not_read(tmp_path):
    outside = tmp_path / "outside.cfg"
    outside.write_text("root=live:CDLABEL=SECRET\n")
    root = system_tree(tmp_path / "root")
    (root / "boot" / "loader" / "entries").mkdir(parents=True)
    (root / "boot" / "loader" / "entries" / "evil.conf").symlink_to(outside)
    report = boot_report(root)
    assert report["matches"] == [] and report["skipped"] == [{"file": "/boot/loader/entries/evil.conf", "why": "it points outside the system"}]


def test_the_boot_files_command_is_one_argument_vector_for_the_live_python(tmp_path):
    words = shlex.split(vm_rescue.boot_files_command("/mnt/rescue/r1a2b3"))
    assert words[:2] == ["python3", "-c"] and json.loads(words[-1])["root"] == "/mnt/rescue/r1a2b3"
    assert vm_rescue.parse_boot_files(0, "noise\n" + json.dumps({"checked": [], "matches": []}))["checked"] == []
    with pytest.raises(ValueError, match="no report"):
        vm_rescue.parse_boot_files(1, "Traceback ...")


# --- what is left behind --------------------------------------------------------------------------------------------------

def test_the_cleanup_report_lists_mounts_sessions_read_only_nodes_and_volumes(tmp_path):
    root = tmp_path
    (root / "proc/self").mkdir(parents=True)
    (root / "proc/self/mountinfo").write_text("36 25 0:30 / /mnt/rescue/r1 rw - ext4 /dev/vda2 rw\n37 25 0:31 / /run rw - tmpfs tmpfs rw\n")
    (root / "run/gentoo-rescue/sessions").mkdir(parents=True)
    (root / "run/gentoo-rescue/sessions/r1.json").write_text("{}")
    for name, ro in (("vda", "0"), ("vda1", "1"), ("vdb", "1")):
        (root / "sys/class/block" / name).mkdir(parents=True)
        (root / "sys/class/block" / name / "ro").write_text(ro + "\n")
    (root / "dev/mapper").mkdir(parents=True)
    (root / "dev/mapper/rescue-1").write_text("")
    code, output = run_snippet(vm_rescue.CLEANUP_SNIPPET, "/dev/vda", env={"HARNESS_FS_ROOT": str(root)})
    assert code == 1
    assert vm_rescue.cleanup_problems(code, output) == [
        "still mounted: /mnt/rescue/r1", "session state left behind: r1.json", "still marked read-only: /dev/vda1", "device-mapper volume left open: rescue-1"]


def test_a_clean_machine_has_nothing_left_behind(tmp_path):
    (tmp_path / "proc/self").mkdir(parents=True)
    (tmp_path / "proc/self/mountinfo").write_text("37 25 0:31 / /run rw - tmpfs tmpfs rw\n")
    code, output = run_snippet(vm_rescue.CLEANUP_SNIPPET, "/dev/vda", env={"HARNESS_FS_ROOT": str(tmp_path)})
    assert code == 0 and vm_rescue.cleanup_problems(code, output) == []
    assert "no cleanup report" in vm_rescue.cleanup_problems(1, "boom")[0]


# --- reading the output of the command line ---------------------------------------------------------------------------------

OUTPUT = "\n".join([
    json.dumps({"type": "stage", "stage": "devices", "message": "devices"}),
    "Mount plan (digest abcdef012345):",
    json.dumps({"type": "result", "message": "rescue.mount", "data": {"mount_plan": {"digest": "a"}, "candidate": {}}}),
    json.dumps({"type": "result", "message": "rescue.mount", "data": {"session_id": "r1a2b3", "path": "/mnt/rescue/r1a2b3"}}),
    "{not json at all",
    json.dumps({"type": "completed", "data": {"success": True}}),
])


def test_json_events_are_read_between_the_lines_of_human_text():
    events = vm_rescue.parse_cli_output(OUTPUT)
    assert [event["type"] for event in events] == ["stage", "result", "result", "completed"]
    assert vm_rescue.last_result(events, "session_id") == {"session_id": "r1a2b3", "path": "/mnt/rescue/r1a2b3"}
    assert vm_rescue.last_result(events, "mount_plan")["candidate"] == {} and vm_rescue.last_result(events, "digest") is None


def test_the_error_of_a_failed_command_is_found():
    events = vm_rescue.parse_cli_output(json.dumps({"type": "error", "error": "rescue.busy", "message": "Another step is running", "data": {"code": "rescue.busy"}}))
    assert vm_rescue.error_of(events) == {"code": "rescue.busy", "message": "Another step is running"}
    assert vm_rescue.error_of([]) == {"code": "", "message": ""}


def test_devices_are_matched_to_their_disk_by_the_kernel_naming_rule():
    assert vm_rescue.device_on_disk("/dev/vda1", "/dev/vda") and vm_rescue.device_on_disk("/dev/nvme0n1p3", "/dev/nvme0n1")
    assert vm_rescue.device_on_disk("/dev/vda", "/dev/vda")
    assert not vm_rescue.device_on_disk("/dev/vdb1", "/dev/vda") and not vm_rescue.device_on_disk("/dev/vda10x", "/dev/vda")
    assert not vm_rescue.device_on_disk("/dev/nvme0n2p1", "/dev/nvme0n1") and not vm_rescue.device_on_disk("/dev/sdaa1", "/dev/sda")


def test_the_flags_of_run_follow_the_risk_of_the_plan():
    medium = {"digest": "d" * 64, "confirm": {"typed": "", "phrase": ""}}
    high = {"digest": "d" * 64, "confirm": {"typed": "vda2", "phrase": ""}}
    dangerous = {"digest": "d" * 64, "confirm": {"typed": "vda2", "phrase": "I HAVE A BACKUP"}}
    assert vm_rescue.confirmation_flags(medium) == ["--confirm-plan", "d" * 64]
    assert vm_rescue.confirmation_flags(high) == ["--confirm-plan", "d" * 64, "--typed", "vda2"]
    assert vm_rescue.confirmation_flags(dangerous)[-1] == "--i-have-a-backup"


def test_a_plan_touches_the_firmware_when_it_says_so_or_runs_a_tool_that_does():
    assert vm_rescue.plan_touches_nvram({"needs": {"uefi_nvram": True}, "steps": []})
    assert vm_rescue.plan_touches_nvram({"needs": {}, "steps": [{"argv": ["efibootmgr", "--create"]}]})
    assert vm_rescue.plan_touches_nvram({"needs": {}, "steps": [{"argv": ["grub-install", "--target=x86_64-efi", "--efi-directory=/efi"]}]})
    assert not vm_rescue.plan_touches_nvram({"needs": {}, "steps": [{"argv": ["grub-install", "--target=x86_64-efi", "--no-nvram"]}, {"argv": ["grub-mkconfig", "-o", "/boot/grub/grub.cfg"]}, {}]})
    assert not vm_rescue.plan_touches_nvram({"needs": {}, "steps": [{"argv": ["grub-install", "--target=i386-pc", "/dev/vda"]}]})
    assert vm_rescue.plan_touches_nvram({"needs": {}, "steps": [{"argv": ["bootctl", "install"]}]})
    assert not vm_rescue.plan_touches_nvram({"needs": {}, "steps": [{"argv": ["bootctl", "--variables=no", "install"]}]})


@pytest.mark.parametrize(("mode", "finding", "command"), [("grub-cfg", "D05", "grub-mkconfig -o /boot/grub/grub.cfg"), ("initramfs", "D02", "dracut --force"), ("fstab-uuid", "D07", "write /etc/fstab (a backup)")])
def test_the_diagnosis_and_the_plan_must_show_the_damage_that_was_done(mode, finding, command):
    plan = {"steps": [{"display": command, "argv": command.split()}]}
    assert vm_rescue.plan_expectation_problems(mode, [{"id": finding}], plan) == []
    problems = vm_rescue.plan_expectation_problems(mode, [{"id": "D13"}], {"steps": [{"display": "touch /x"}]})
    assert len(problems) == 2 and "diagnosis does not mention" in problems[0] and "no command of the plan repairs it" in problems[1]
    assert vm_rescue.plan_expectation_problems("none", [], None) == []


# --- the driver on a script of answers ---------------------------------------------------------------------------------------

class Script:
    """A run() that answers each command from a list and remembers what was asked."""

    def __init__(self, *answers: tuple[int, str]) -> None:
        self.answers = list(answers)
        self.asked: list[tuple[str, str, int]] = []

    def __call__(self, command: str, log_name: str, timeout: int) -> tuple[int, str]:
        self.asked.append((command, log_name, timeout))
        return self.answers.pop(0)


def result(data: dict, message: str = "x") -> str:
    return json.dumps({"type": "result", "message": message, "data": data}) + "\n"


def test_the_commands_follow_the_transport_that_was_asked_for():
    assert vm_rescue.RescueDriver(Script()).argv("scan") == ["gentoo-rescue", "--json", "scan"]
    assert vm_rescue.RescueDriver(Script(), transport="local").argv("scan") == ["gentoo-rescue", "--local", "--json", "scan"]
    assert vm_rescue.RescueDriver(Script(), transport="daemon").argv("scan")[:3] == ["gentoo-rescue", "--socket", "/run/gentoo-installer/server.sock"]
    with pytest.raises(ValueError, match="unknown transport"):
        vm_rescue.RescueDriver(Script(), transport="ssh")


def test_a_failing_command_names_the_exit_status_and_the_tools_own_words():
    error = json.dumps({"type": "error", "error": "rescue.busy", "message": "Another step is still running in this session", "data": {"code": "rescue.busy"}})
    driver = vm_rescue.RescueDriver(Script((6, error)))
    with pytest.raises(vm_rescue.RescueFlowError, match=r"gentoo-rescue scan failed with exit status 6 \(busy\): Another step is still running"):
        driver.scan()
    with pytest.raises(vm_rescue.RescueFlowError, match="no output"):
        vm_rescue.RescueDriver(Script((1, ""))).scan()


def test_a_scan_without_a_result_is_an_error():
    with pytest.raises(vm_rescue.RescueFlowError, match="printed no scan result"):
        vm_rescue.RescueDriver(Script((0, "Systems found:\n"))).scan()


SCAN = {"candidates": [
    {"id": "c1", "device": "/dev/vda1", "kind": "root", "os": {"id": "gentoo"}},
    {"id": "c2", "device": "/dev/vda3", "kind": "snapshot", "os": {"id": "gentoo"}},
    {"id": "c3", "device": "/dev/vdb1", "kind": "root", "os": {"id": "gentoo"}}], "live_medium": ["/dev/sr0"]}


def test_the_system_under_test_is_the_one_on_the_guarded_disk_with_the_expected_os():
    driver = vm_rescue.RescueDriver(Script())
    assert driver.choose(SCAN, disk="/dev/vda", os_id="gentoo")["id"] == "c1"
    with pytest.raises(vm_rescue.RescueFlowError, match="no debian system on /dev/vda; it found: /dev/vda1 \\(gentoo, root\\)"):
        driver.choose(SCAN, disk="/dev/vda", os_id="debian")
    with pytest.raises(vm_rescue.RescueFlowError, match="no gentoo system on /dev/vdc"):
        driver.choose(SCAN, disk="/dev/vdc", os_id="gentoo")
    twin = {**SCAN, "candidates": [*SCAN["candidates"], {"id": "c4", "device": "/dev/vda2", "kind": "root", "os": {"id": "gentoo"}}]}
    with pytest.raises(vm_rescue.RescueFlowError, match="several gentoo systems"):
        driver.choose(twin, disk="/dev/vda", os_id="gentoo")


def test_the_live_medium_must_never_be_offered_as_a_system():
    leaking = {**SCAN, "candidates": [*SCAN["candidates"], {"id": "c9", "device": "/dev/sr0", "kind": "root", "os": {"id": "gentoo"}}]}
    with pytest.raises(vm_rescue.RescueFlowError, match="lists the live medium"):
        vm_rescue.RescueDriver(Script()).choose(leaking, disk="/dev/vda", os_id="gentoo")


def test_a_system_that_was_mounted_writable_before_any_plan_ran_is_an_error():
    session = result({"session_id": "r1a2b3", "path": "/mnt/rescue/r1a2b3", "mode": "rw"})
    with pytest.raises(vm_rescue.RescueFlowError, match="mounted the system 'rw', not read-only"):
        vm_rescue.RescueDriver(Script((0, session))).mount({"id": "c1"})
    assert vm_rescue.RescueDriver(Script((0, result({"session_id": "r1a2b3", "mode": "ro"})))).mount({"id": "c1"})["mode"] == "ro"


def test_the_plan_is_only_taken_when_nothing_in_it_is_blocked():
    blocked = result({"digest": "d", "preconditions": [{"ok": False, "text": "The computer has no network route"}], "steps": []})
    with pytest.raises(vm_rescue.RescueFlowError, match="cannot run here: The computer has no network route"):
        vm_rescue.RescueDriver(Script((0, blocked))).plan("r1", "repair.recommended")


def test_a_repair_that_ends_in_a_warning_or_a_failed_step_is_not_a_success():
    plan = {"digest": "d" * 64, "confirm": {"typed": "", "phrase": ""}}
    warning = result({"outcome": "warning", "steps": [], "post_checks": [{"ok": False, "text": "/boot/grub/grub.cfg is 0 bytes"}]})
    with pytest.raises(vm_rescue.RescueFlowError, match="outcome 'warning': check failed: /boot/grub/grub.cfg is 0 bytes"):
        vm_rescue.RescueDriver(Script((0, warning))).execute_plan("r1", "repair.recommended", plan)
    failed = result({"outcome": "failed", "steps": [{"id": "boot-config-c1", "status": "failed", "rc": 1}], "post_checks": []})
    with pytest.raises(vm_rescue.RescueFlowError, match=r"step boot-config-c1 \(exit 1\) failed"):
        vm_rescue.RescueDriver(Script((1, failed))).execute_plan("r1", "repair.recommended", plan)


def test_the_run_gets_the_longer_time_and_exactly_the_confirmation_of_its_plan():
    script = Script((0, result({"outcome": "success", "steps": [], "post_checks": []})))
    driver = vm_rescue.RescueDriver(script, run_timeout=99)
    driver.execute_plan("r1", "boot.config", {"digest": "d" * 64, "confirm": {"typed": "vda1", "phrase": ""}})
    [(command, log, timeout)] = script.asked
    assert shlex.split(command)[-7:] == ["run", "r1", "boot.config", "--confirm-plan", "d" * 64, "--typed", "vda1"]
    assert log == "rescue-run.log" and timeout == 99


def test_leftovers_after_the_unmount_fail_the_repair():
    leftover = result({"released": [], "leftovers": ["/mnt/rescue/r1/boot"]})
    with pytest.raises(vm_rescue.RescueFlowError, match="left mounts behind: /mnt/rescue/r1/boot"):
        vm_rescue.RescueDriver(Script((0, leftover))).unmount("r1")


def test_every_command_gets_its_own_log_so_a_second_plan_never_mixes_with_the_first():
    script = Script((0, result({"candidates": []})), (0, result({"candidates": []})))
    driver = vm_rescue.RescueDriver(script, prefix="rescue-initramfs")
    driver.scan()
    driver.scan()
    assert [asked[1] for asked in script.asked] == ["rescue-initramfs-scan.log", "rescue-initramfs-scan-2.log"]
    assert driver.log_of("scan") == "rescue-initramfs-scan-2.log" and driver.log_of("run") == "rescue-initramfs-run.log"


# --- the driver against the real command line ---------------------------------------------------------------------------------

def lab_runner(lab: Lab):
    """run() for the driver that executes the real gentoo-rescue command line (cli.main) on the rescue tests' fake system."""
    def run(command: str, log_name: str, timeout: int) -> tuple[int, str]:
        argv = shlex.split(command)
        assert argv[:2] == ["gentoo-rescue", "--json"], argv
        out, err = io.StringIO(), io.StringIO()
        console = cli.Console(out=out, err=err, ask=lambda _p: "", secret=lambda _p: "", interactive=False)
        code = cli.main(argv[1:], transport=LocalTransport(RescueApi(lab.manager(auto_adopt=True))), console=console)
        return code, out.getvalue() + err.getvalue()
    return run


@pytest.fixture
def make_lab(tmp_path):
    counter = {"n": 0}

    def make(breakage=None, **options):
        counter["n"] += 1

        def factory(lsblk, **kwargs):
            return Lab(tmp_path, lsblk, name=f"lab{counter['n']}", **kwargs)

        lab, tree = uefi_gentoo_lab(factory, **options)
        if breakage:
            breakage(tree)
        return lab, tree

    return make


def remove_menu(tree: Path) -> None:
    (tree / "boot/grub/grub.cfg").unlink(missing_ok=True)


def remove_initramfs(tree: Path) -> None:
    for path in (tree / "boot").glob("initramfs*"):
        path.unlink()


@pytest.mark.parametrize(("mode", "breakage", "command"), [
    ("grub-cfg", remove_menu, "grub-mkconfig"),
    ("initramfs", remove_initramfs, "dracut"),
])
def test_a_gentoo_with_a_missing_boot_file_is_repaired_the_way_the_harness_drives_it(make_lab, mode, breakage, command):
    lab, tree = make_lab(breakage)
    driver = vm_rescue.RescueDriver(lab_runner(lab))
    scan = driver.scan()
    system = driver.choose(scan, disk="/dev/nvme0n1", os_id="gentoo")
    assert system["device"] == "/dev/nvme0n1p5" and system["os"]["id"] == "gentoo"
    session = driver.mount(system)
    assert session["session_id"].startswith("r") and Path(session["path"]).name == session["session_id"]
    findings = driver.diagnose(session["session_id"])
    plan = driver.plan(session["session_id"], "repair.recommended")
    assert vm_rescue.plan_expectation_problems(mode, findings["findings"], plan) == [], findings
    assert any(command in text for text in vm_rescue.plan_commands(plan))
    assert not vm_rescue.plan_touches_nvram(plan)
    outcome = driver.execute_plan(session["session_id"], "repair.recommended", plan)
    assert outcome["outcome"] == "success" and all(check["ok"] for check in outcome["post_checks"])
    assert isinstance(driver.audit(session["session_id"]), str)
    assert vm_rescue.boot_file_problems(boot_report(Path(session["path"]))) == [], "the mounted, repaired system has a boot menu without live arguments"
    released = driver.unmount(session["session_id"])
    assert released["leftovers"] == []
    assert [entry["step"] for entry in driver.commands] == ["scan", "mount", "diagnose", "plan", "run", "audit", "unmount"]


def test_a_menu_file_that_is_repaired_gets_the_live_arguments_out_of_the_boot_files_check(make_lab):
    lab, tree = make_lab(remove_menu)
    driver = vm_rescue.RescueDriver(lab_runner(lab))
    session = driver.mount(driver.choose(driver.scan(), disk="/dev/nvme0n1", os_id="gentoo"))
    plan = driver.plan(session["session_id"], "repair.recommended")
    driver.execute_plan(session["session_id"], "repair.recommended", plan)
    mounted = Path(session["path"])
    assert (mounted / "boot" / "grub" / "grub.cfg").exists()
    assert vm_rescue.boot_file_problems(boot_report(mounted)) == []
    (mounted / "boot" / "grub" / "grub.cfg").write_text("menuentry 'x' {\n linux /boot/gentoo root=live:CDLABEL=GENTOO\n}\n" * 20)
    assert any("root=live:CDLABEL=GENTOO" in problem for problem in vm_rescue.boot_file_problems(boot_report(mounted)))
    driver.unmount(session["session_id"])
    assert tree.exists()


def test_a_system_that_is_not_gentoo_is_found_by_the_scan_and_refused_by_the_mount(make_lab):
    """What the harness sees when the disk holds another distribution: the scan names it, and the tool will not open it (exit status 7)."""
    lab, tree = make_lab(None)
    driver = vm_rescue.RescueDriver(lab_runner(lab))
    scan = driver.scan()
    other = driver.choose(scan, disk="/dev/sdb", os_id="debian")
    assert other["supported"] is False and other["os"]["pretty_name"].startswith("Debian")
    with pytest.raises(vm_rescue.RescueFlowError, match=r"gentoo-rescue mount failed with exit status 7 \(unsupported\): Not a Gentoo system - this repair tool only supports Gentoo"):
        driver.mount(other)
    assert not lab.system.mounted_targets() and not lab.system.chroot_calls


def test_a_failing_repair_command_is_a_failed_repair(make_lab):
    lab, _tree = make_lab(remove_menu)
    lab.system.chroot_failures["grub-mkconfig"] = (1, "grub-mkconfig: error: cannot find a GRUB drive")
    driver = vm_rescue.RescueDriver(lab_runner(lab))
    session = driver.mount(driver.choose(driver.scan(), disk="/dev/nvme0n1", os_id="gentoo"))
    plan = driver.plan(session["session_id"], "repair.recommended")
    with pytest.raises(vm_rescue.RescueFlowError, match="outcome 'failed': step boot-config-c1 .exit 1. failed"):
        driver.execute_plan(session["session_id"], "repair.recommended", plan)
