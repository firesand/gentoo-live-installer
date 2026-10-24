import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from tests.vm.helpers import PUBLIC_KEY, load_vm_module


vm = load_vm_module()


def vm_paths(tmp_path, monkeypatch):
    root = tmp_path / "build" / "vm"
    run_dir = root / "run"
    run_dir.mkdir(parents=True)
    iso = tmp_path / "installer.iso"
    iso.write_bytes(b"iso")
    monkeypatch.setattr(vm, "VM_ROOT", root)
    return iso, run_dir / "disk.qcow2", run_dir


def test_qemu_command_uses_only_disposable_disk_and_loopback_forward(tmp_path, monkeypatch):
    iso, disk, run_dir = vm_paths(tmp_path, monkeypatch)
    command = vm.qemu_command(
        vm.SCENARIO_MAP["bios"], iso=iso, disk=disk, run_dir=run_dir,
        live=True, ssh_host="127.0.0.1", ssh_port=40222, memory_mib=2048, cpus=2,
    )

    assert isinstance(command, list)
    assert command[0] == "qemu-system-x86_64"
    assert f"file={disk},if=none,id=target,format=qcow2" in command
    assert f"virtio-blk-pci,drive=target,serial={vm.TARGET_SERIAL}" in command
    assert "q35,accel=kvm:tcg" in command
    assert f"file={iso},media=cdrom,readonly=on,format=raw" in command
    assert "user,id=net0,hostfwd=tcp:127.0.0.1:40222-:22" in command
    assert not any("/dev/sda" in argument or "/dev/nvme" in argument for argument in command)
    target = vm.qemu_command(
        vm.SCENARIO_MAP["bios"], iso=iso, disk=disk, run_dir=run_dir,
        live=False, ssh_host="127.0.0.1", ssh_port=40222, memory_mib=2048, cpus=2,
    )
    assert not any("media=cdrom" in argument for argument in target)


@pytest.mark.parametrize("host", ["192.0.2.1", "example.com", "::1", "127.0.0.1;touch /tmp/pwned"])
def test_non_loopback_or_injected_endpoints_are_refused(host):
    with pytest.raises(vm.HarnessError):
        vm.validate_loopback(host)


def test_disk_outside_vm_root_is_refused(tmp_path, monkeypatch):
    iso, _, run_dir = vm_paths(tmp_path, monkeypatch)
    with pytest.raises(vm.HarnessError, match="outside"):
        vm.qemu_command(
            vm.SCENARIO_MAP["bios"], iso=iso, disk=tmp_path / "host.qcow2", run_dir=run_dir,
            live=True, ssh_host="127.0.0.1", ssh_port=40222, memory_mib=2048, cpus=2,
        )


def test_disk_symlink_and_qemu_option_injection_are_refused(tmp_path, monkeypatch):
    iso, disk, run_dir = vm_paths(tmp_path, monkeypatch)
    backing = run_dir / "existing.qcow2"
    backing.write_bytes(b"untouched")
    disk.symlink_to(backing)
    with pytest.raises(vm.HarnessError, match="symlinks"):
        vm.validate_command_paths(iso, disk, run_dir)
    disk.unlink()
    with pytest.raises(vm.HarnessError, match="commas"):
        vm.validate_command_paths(iso, run_dir / "disk,readonly=off.qcow2", run_dir)
    assert backing.read_bytes() == b"untouched"


def test_missing_control_channel_is_refused_before_tools_or_qemu(tmp_path):
    args = SimpleNamespace(ssh_host=None, ssh_port=None, ssh_private_key=None)
    with patch.object(vm.shutil, "which") as which:
        with pytest.raises(vm.HarnessError, match="Refusing to run without"):
            vm.preflight(args, [vm.SCENARIO_MAP["openrc"]])
    which.assert_not_called()


def test_ssh_options_disable_passwords_and_forwarding(tmp_path):
    args = argparse.Namespace(
        ssh_binary="ssh", ssh_private_key=Path("/tmp/test-key"), ssh_port=40222,
        ssh_host="127.0.0.1",
    )
    command = vm.ssh_command(args, tmp_path / "known_hosts", "root", "gentoo-installerd --manifest /run/m --yes")
    joined = " ".join(command)
    assert "BatchMode=yes" in joined
    assert "IdentitiesOnly=yes" in joined
    assert "PasswordAuthentication=no" in joined
    assert "ClearAllForwardings=yes" in joined
    assert "StrictHostKeyChecking=accept-new" in joined
    assert command[-2:] == ["--", "gentoo-installerd --manifest /run/m --yes"]


@pytest.mark.parametrize(("mode", "expected"), [(None, "serial"), ("serial", "serial"), ("external", "external")])
def test_luks_unlock_mode_defaults_to_serial_and_accepts_external(mode, expected, tmp_path):
    argv = ["--scenario", "uefi-luks", "--iso", str(tmp_path / "installer.iso")]
    if mode is not None:
        argv.extend(("--luks-unlock", mode))
    assert vm.build_parser().parse_args(argv).luks_unlock == expected


def test_luks_unlock_mode_rejects_unknown_values(tmp_path):
    with pytest.raises(SystemExit):
        vm.build_parser().parse_args([
            "--scenario", "uefi-luks", "--iso", str(tmp_path / "installer.iso"),
            "--luks-unlock", "vga",
        ])


def test_check_resume_flag_is_opt_in(tmp_path):
    base = ["--scenario", "openrc", "--iso", str(tmp_path / "installer.iso")]
    assert vm.build_parser().parse_args(base).check_resume is False
    assert vm.build_parser().parse_args(base + ["--check-resume"]).check_resume is True


@pytest.mark.parametrize(("selection", "expected"), [
    (None, "both"), ("both", "both"), ("portagehub", "portagehub"), ("none", "none"),
])
def test_companion_selection_option_defaults_to_existing_both(tmp_path, selection, expected):
    base = ["--scenario", "openrc", "--iso", str(tmp_path / "installer.iso")]
    if selection is not None:
        base.extend(("--companions", selection))
    assert vm.build_parser().parse_args(base).companions == expected


def test_companion_selection_rejects_unknown_value(tmp_path):
    with pytest.raises(SystemExit):
        vm.build_parser().parse_args([
            "--scenario", "openrc", "--iso", str(tmp_path / "installer.iso"),
            "--companions", "hwmonux",
        ])


def test_cancel_then_resume_is_opt_in_gui_only_and_exclusive(tmp_path):
    base = ["--scenario", "openrc", "--iso", str(tmp_path / "installer.iso")]
    assert vm.build_parser().parse_args(base).cancel_then_resume is False
    args = vm.build_parser().parse_args(base + ["--install-mode", "gui", "--cancel-then-resume"])
    assert args.cancel_then_resume is True
    with pytest.raises(SystemExit):
        vm.main(base + ["--cancel-then-resume"])
    with pytest.raises(SystemExit):
        vm.main(base + ["--install-mode", "gui", "--cancel-then-resume", "--check-resume"])


def test_external_luks_setup_does_not_wait_for_terminal_input(tmp_path, monkeypatch, capsys):
    runner = object.__new__(vm.ScenarioRunner)
    runner.args = SimpleNamespace(boot_timeout=321)
    runner.run_dir = tmp_path

    class Target:
        def screendump(self, output):
            output.write_bytes(b"P6\n")
            return output

    class NoTerminal:
        @property
        def isatty(self):
            raise AssertionError("external unlock must not inspect or wait on stdin")

    monkeypatch.setattr(vm.sys, "stdin", NoTerminal())
    runner.prepare_external_luks_unlock(Target())
    instructions = capsys.readouterr().out
    assert "luks-unlock-screen.ppm" in instructions
    assert "qmp.sock" in instructions
    assert "321s" in instructions
    assert "external operator-controlled VGA" in (runner.run_dir / "luks-unlock-method.txt").read_text()


def test_gui_environment_waits_for_delayed_xfce_session_and_keeps_attempts(tmp_path, monkeypatch):
    clock = {"now": 0.0}
    fake_time = SimpleNamespace(
        monotonic=lambda: clock["now"],
        sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    monkeypatch.setattr(vm, "time", fake_time)
    calls = []

    def probe(attempt, timeout):
        calls.append((attempt, timeout))
        log = tmp_path / f"attempt-{attempt}.log"
        if attempt < 3:
            log.write_text("No usable gentoo XFCE session yet")
            return 1, log
        log.write_text(json.dumps({"type": "gui_environment", "environment": {
            "DISPLAY": ":0", "XAUTHORITY": "/home/gentoo/.Xauthority"}}))
        return 0, log

    env = vm.poll_gui_environment(probe, timeout=5, command_timeout=10, run_dir=tmp_path)
    assert env["DISPLAY"] == ":0"
    assert calls == [(1, 5), (2, 3), (3, 1)]
    summary = (tmp_path / "gui-environment.log").read_text()
    assert "attempt 1:" in summary and "attempt 2:" in summary and "attempt 3: ready" in summary
    assert all((tmp_path / f"attempt-{index}.log").is_file() for index in (1, 2, 3))


def test_gui_environment_timeout_is_bounded_and_preserves_failure_evidence(tmp_path, monkeypatch):
    clock = {"now": 0.0}
    fake_time = SimpleNamespace(
        monotonic=lambda: clock["now"],
        sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    monkeypatch.setattr(vm, "time", fake_time)
    calls = []

    def probe(attempt, timeout):
        calls.append((attempt, timeout))
        log = tmp_path / f"attempt-{attempt}.log"
        log.write_text("No usable gentoo XFCE session")
        return 1, log

    with pytest.raises(vm.HarnessError, match="within 5s"):
        vm.poll_gui_environment(probe, timeout=5, command_timeout=10, run_dir=tmp_path)
    assert len(calls) == 3
    assert all(0 < timeout <= 5 for _, timeout in calls)
    assert "No usable gentoo XFCE session" in (tmp_path / "gui-environment.log").read_text()


def test_gui_environment_limits_wait_to_120_seconds_and_respects_command_timeout(tmp_path, monkeypatch):
    runner = object.__new__(vm.ScenarioRunner)
    runner.args = SimpleNamespace(ssh_user="root", boot_timeout=900, command_timeout=17)
    runner.run_dir = tmp_path
    observed = {}

    def poll(probe, *, timeout, command_timeout, run_dir):
        observed.update(timeout=timeout, command_timeout=command_timeout, run_dir=run_dir)
        return {"DISPLAY": ":0"}

    monkeypatch.setattr(vm, "poll_gui_environment", poll)
    assert runner.gui_environment() == {"DISPLAY": ":0"}
    assert observed == {"timeout": 120, "command_timeout": 17, "run_dir": tmp_path}


def test_failure_screenshots_capture_only_live_vms_and_keep_qmp_errors(tmp_path):
    runner = object.__new__(vm.ScenarioRunner)
    runner.run_dir = tmp_path

    class Process:
        def __init__(self, status):
            self.status = status

        def poll(self):
            return self.status

    class Guest:
        def __init__(self, label, status, error=None):
            self.label = label
            self.process = Process(status)
            self.error = error

        def screendump(self, output):
            if self.error:
                raise RuntimeError(self.error)
            output.write_bytes(b"P6\n1 1\n255\n\x00\x00\x00")
            return output

    runner.vms = [Guest("live", None), Guest("target", None, "QMP unavailable"), Guest("stopped", 0)]
    captures = runner.capture_failure_screenshots()
    assert captures == [
        {"vm": "live", "path": str(tmp_path / "failure-live.ppm"), "success": "true"},
        {"vm": "target", "success": "false", "error": "QMP unavailable"},
    ]
    assert (tmp_path / "failure-live.ppm").read_bytes().startswith(b"P6\n1 1\n255\n")
    assert json.loads((tmp_path / "failure-screenshots.json").read_text()) == captures


def test_corrupt_saved_manifest_does_not_bypass_vm_cleanup_or_result_saving(tmp_path, monkeypatch):
    runner = object.__new__(vm.ScenarioRunner)
    runner.args = SimpleNamespace(qemu_img="qemu-img", command_timeout=5, disk_size=40, keep=False,
                                  ssh_user="root", cancel_then_resume=False)
    runner.scenario = vm.SCENARIO_MAP["openrc"]
    runner.run_dir = tmp_path
    runner.disk = tmp_path / "disk.qcow2"
    runner.iso = tmp_path / "installer.iso"
    runner.vms = []
    runner.logs = []
    (tmp_path / "installer.iso").write_bytes(b"iso")
    cleaned = []

    class Guest:
        def cleanup(self):
            cleaned.append(True)

    guest = Guest()
    runner.start_vm = lambda live: runner.vms.append(guest) or guest
    runner.wait_for_ssh = lambda *_args: None

    def fail_install(**_kwargs):
        (tmp_path / "manifest.json").write_text("not json")
        raise vm.HarnessError("simulated install failure")

    runner.install = fail_install
    runner.capture_failure_screenshots = lambda: []
    monkeypatch.setattr(vm, "validate_command_paths", lambda *_args: None)
    monkeypatch.setattr(vm, "run_logged", lambda *_args, **_kwargs: 0)

    with pytest.raises(vm.HarnessError, match="simulated install failure"):
        runner.run()

    assert cleaned == [True]
    saved = json.loads((tmp_path / "result.json").read_text())
    assert saved["error"] == "simulated install failure"
    assert "Could not read saved manifest metadata" in saved["companion_metadata_error"]


def test_main_does_not_print_pass_or_return_zero_for_false_runner_result(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    class FalseRunner:
        def __init__(self, *_args):
            self.run_dir = run_dir

        def run(self):
            return {"success": False, "error": "saved manifest metadata unreadable"}

    monkeypatch.setattr(vm, "preflight", lambda *_args: None)
    monkeypatch.setattr(vm, "public_key_from_private", lambda *_args: "ssh-ed25519 test")
    monkeypatch.setattr(vm, "ScenarioRunner", FalseRunner)
    result = vm.main(["--scenario", "openrc", "--iso", str(tmp_path / "installer.iso")])
    captured = capsys.readouterr()

    assert result == 1
    assert "PASS openrc" not in captured.out
    assert "saved manifest metadata unreadable" in captured.err


def test_check_resume_runs_original_manifest_as_root_after_disk_guard(tmp_path, monkeypatch):
    runner = object.__new__(vm.ScenarioRunner)
    runner.args = SimpleNamespace(ssh_user="root", command_timeout=10, install_timeout=30)
    runner.run_dir = tmp_path
    calls = []
    storage = vm.parse_lsblk_snapshot(json.dumps({
        "blockdevices": [{"path": "/dev/vda", "type": "disk", "uuid": "", "fstype": "", "partuuid": "",
            "parttype": "", "pttype": "gpt", "ptuuid": "table-id", "children": [
                {"path": "/dev/vda1", "type": "part", "uuid": "root-id", "fstype": "ext4",
                 "partuuid": "part-id", "parttype": "linux", "pttype": "", "ptuuid": ""}]}]}))

    def fake_ssh(user, remote, log_name, timeout, **kwargs):
        calls.append((user, remote, log_name, timeout))
        log = tmp_path / log_name
        if log_name == "resume.log":
            log.write_text('\n'.join((
                '{"type":"stage","stage":"storage","data":{"status":"skipped"}}',
                '{"type":"completed","data":{"success":true}}',
            )))
        return 0, log

    runner.ssh = fake_ssh
    monkeypatch.setattr(runner, "capture_storage_snapshot", lambda name: storage)
    report = runner.check_resume()
    assert report["success"] is True
    assert calls[0][1].startswith("python3 -c ")
    assert calls[1] == ("root", "gentoo-installerd --manifest /run/gentoo-installer-vm-manifest.json --yes --resume",
                        "resume.log", 30)
    assert (tmp_path / "resume-result.json").is_file()


def test_check_resume_refuses_nonroot_before_guest_access(tmp_path):
    runner = object.__new__(vm.ScenarioRunner)
    runner.args = SimpleNamespace(ssh_user="gentoo")
    runner.run_dir = tmp_path
    runner.ssh = Mock(side_effect=AssertionError("must not connect"))
    with pytest.raises(vm.HarnessError, match="requires root SSH"):
        runner.check_resume()
    runner.ssh.assert_not_called()


def test_key_derivation_and_logged_commands_use_mocked_subprocesses(tmp_path):
    completed = Mock(returncode=0, stdout=PUBLIC_KEY + "\n", stderr="")
    with patch.object(vm.subprocess, "run", return_value=completed) as run:
        assert vm.public_key_from_private("ssh-keygen", tmp_path / "id") == PUBLIC_KEY
        run.assert_called_once()
        assert "shell" not in run.call_args.kwargs

    completed = Mock(returncode=0)
    with patch.object(vm.subprocess, "run", return_value=completed) as run:
        code = vm.run_logged(("qemu-img", "create", "-f", "qcow2", "disk.qcow2", "40G"), tmp_path / "log", 10)
    assert code == 0
    assert run.call_args.args[0] == ["qemu-img", "create", "-f", "qcow2", "disk.qcow2", "40G"]
    assert run.call_args.kwargs["timeout"] == 10
    assert "shell" not in run.call_args.kwargs


def test_install_invokes_real_backend_cli(tmp_path):
    runner = object.__new__(vm.ScenarioRunner)
    runner.args = SimpleNamespace(ssh_user="root", command_timeout=10, install_timeout=20)
    runner.scenario = vm.SCENARIO_MAP["openrc"]
    runner.public_key = PUBLIC_KEY
    runner.run_dir = tmp_path
    calls = []

    def fake_ssh(user, remote, log_name, timeout, **kwargs):
        calls.append((user, remote, timeout))
        log = tmp_path / log_name
        if log_name == "install.log":
            log.write_text('{"type":"completed","data":{"success":true}}\n')
        return 0, log

    runner.ssh = fake_ssh
    runner.install()
    assert calls[2] == (
        "root", "gentoo-installerd --manifest /run/gentoo-installer-vm-manifest.json --yes", 20,
    )
    assert "--dry-run" not in calls[2][1]
    assert "GENTOO-VM-DISPOSABLE" in calls[0][1]
