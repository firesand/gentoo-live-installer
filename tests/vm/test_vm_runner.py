"""T4: the harness's orchestration (processes, sockets, SSH, files, exit codes) against fake binaries.

Everything runs for real except QEMU and ssh themselves: ``tests/vm/fakes`` provides stand-ins
for qemu-system-x86_64, qemu-img, ssh and ssh-keygen, and ``tests/vm/fake_env.py`` the guest rules.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests.vm.fake_env import EVENT_STREAM, FAILED_STREAM, Lab, guest_rules, process_alive
from tests.vm.helpers import load_vm_module

vm = load_vm_module()
ROOT = Path(__file__).resolve().parents[2]
PROBES = {"init", "hostname", "user", "network", "dns", "portage", "hwmonux", "portagehub"}


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return Lab(vm, tmp_path, monkeypatch)


def pids(run_dir: Path) -> list[int]:
    return [int(path.read_text()) for path in run_dir.glob("fake-qemu-pid-*.txt")]


def test_cli_run_of_the_bios_scenario_passes_and_cleans_up(lab, capsys):
    lab.rules = guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios") == 0
    run = lab.run_dir()
    result = lab.result()
    assert result["success"] is True
    assert PROBES | {"network-static"} == {probe["name"] for probe in result["probes"]}
    assert all(probe["success"] for probe in result["probes"])
    assert {probe["name"] for probe in result["companion_probes"]} == {"hwmonux", "portagehub"}
    assert f"PASS bios: result {run / 'result.json'}" in capsys.readouterr().out
    # Two VMs were started: the live ISO first, then the installed disk alone.
    live = json.loads((run / "fake-qemu-argv-live.json").read_text())
    target = json.loads((run / "fake-qemu-argv-target.json").read_text())
    assert any("media=cdrom" in argument for argument in live)
    assert not any("media=cdrom" in argument for argument in target)
    assert all(argument != "if=pflash" for argument in live)  # BIOS: no OVMF
    # Nothing is left running or lying around.
    assert not any(process_alive(pid) for pid in pids(run))
    for name in ("serial.sock", "qmp.sock", "qemu-live.pid", "qemu-target.pid", "disk.qcow2", "OVMF_VARS.fd"):
        assert not (run / name).exists(), name
    assert (run / "manifest.json").is_file() and (run / "serial-target.log").exists()
    # The manifest and the real backend command went through SSH as root.
    uploaded = json.loads((run / "fake-guest" / "manifest.json").read_text())
    assert uploaded["storage"]["firmware"] == "bios" and uploaded["accounts"]["hostname"] == "gentoo-vm-bios"
    remote = [call["remote"] for call in lab.ssh_calls()]
    assert "gentoo-installerd --manifest /run/gentoo-installer-vm-manifest.json --yes" in remote
    assert all(call["matched"] is not None for call in lab.ssh_calls()), "an unexpected guest command ran"


def test_uefi_run_shares_one_firmware_variable_store_between_both_boots(lab):
    lab.rules = guest_rules(vm, "openrc")
    assert lab.run("--scenario", "openrc", "--keep") == 0
    run = lab.run_dir()
    argv = {phase: json.loads((run / f"fake-qemu-argv-{phase}.json").read_text()) for phase in ("live", "target")}
    for phase in argv:
        assert f"if=pflash,format=raw,readonly=on,file={lab.firmware.code}" in argv[phase]
        assert f"if=pflash,format=raw,file={run / 'OVMF_VARS.fd'}" in argv[phase]
    assert (run / "OVMF_VARS.fd").read_bytes() == b"vars"
    assert (run / "disk.qcow2").is_file()  # --keep leaves the disk and the firmware copy


def test_ssh_that_never_answers_fails_within_the_boot_timeout_and_stops_qemu(lab, capsys):
    lab.rules = [{"user": "^root$", "match": "^true$", "rc": 255}]
    started = time.monotonic()
    assert lab.run("--scenario", "bios", "--boot-timeout", "2") == 1
    assert time.monotonic() - started < 30
    run = lab.run_dir()
    assert "SSH did not become ready within 2s" in lab.result()["error"]
    assert "FAIL bios" in capsys.readouterr().err
    assert not any(process_alive(pid) for pid in pids(run))
    assert (run / "disk.qcow2").is_file()  # a failed run keeps its artifacts


def test_a_failed_install_keeps_the_evidence_and_stops_every_vm(lab):
    lab.rules = guest_rules(vm, "bios", install=FAILED_STREAM)
    assert lab.run("--scenario", "bios") == 1
    run = lab.run_dir()
    result = lab.result()
    assert result["success"] is False and "emerge failed: simulated" in result["error"]
    assert (run / "install.log").is_file() and (run / "install-result.json").is_file()
    assert not any(process_alive(pid) for pid in pids(run))
    assert not (run / "qemu-live.pid").exists()
    captures = json.loads((run / "failure-screenshots.json").read_text())
    assert [capture["vm"] for capture in captures] == ["live"] and captures[0]["success"] == "true"
    assert not (run / "qemu-target.pid").exists()  # the installed disk was never booted


def test_probe_failures_are_reported_by_name(lab, capsys):
    lab.rules = guest_rules(vm, "bios", extra=[{"user": "^vmtest$", "match": r"^hostname$", "stdout": "wrong-host\n"}])
    assert lab.run("--scenario", "bios") == 1
    assert "hostname: Expected" in lab.result()["error"]
    assert "Target verification failed" in capsys.readouterr().err


def test_a_luks_scenario_is_unlocked_over_serial_without_an_operator(lab):
    prompt = "Please enter passphrase for disk root (gentoo-root): (press TAB for no echo)"
    lab.qemu_config.update(serial_text_target=prompt, unlock_line=vm.TEST_LUKS_PASSPHRASE)
    lab.rules = guest_rules(vm, "uefi-luks")
    assert lab.run("--scenario", "uefi-luks") == 0
    run = lab.run_dir()
    assert (run / "fake-serial-input.txt").read_text() == vm.TEST_LUKS_PASSPHRASE + "\n"
    receipt = json.loads((run / "luks-unlock-receipt.json").read_text())
    assert receipt["method"] == "serial" and receipt["attempts"] == 1 and "passphrase for disk root" in receipt["matched_text"]
    assert lab.result()["luks_unlock"]["method"] == "serial"
    uploaded = json.loads((run / "fake-guest" / "manifest.json").read_text())
    assert uploaded["system"]["kernel_cmdline_extra"] == "console=tty0 console=ttyS0,115200n8"


def test_a_luks_prompt_that_never_appears_fails_instead_of_typing_blindly(lab):
    lab.qemu_config.update(serial_text_target="OVMF BdsDxe: loading Boot0008 Gentoo\n", unlock_line="x")
    lab.rules = guest_rules(vm, "uefi-luks")
    assert lab.run("--scenario", "uefi-luks", "--luks-timeout", "2") == 1
    run = lab.run_dir()
    assert "passphrase prompt" in lab.result()["error"]
    assert not (run / "fake-serial-input.txt").exists()


def test_all_keeps_going_after_a_failing_scenario_and_returns_one(lab, monkeypatch, capsys):
    monkeypatch.setattr(vm, "ALL_SCENARIOS", ("openrc", "bios"))
    lab.rules = [{"run_dir": "/openrc-", "user": "^root$", "match": r"^gentoo-installerd ", "events": FAILED_STREAM},
                 *guest_rules(vm, "bios")]
    assert lab.run("--all") == 1
    captured = capsys.readouterr()
    assert "FAIL openrc" in captured.err and "PASS bios" in captured.out
    assert len(lab.run_dirs()) == 2


def test_an_unexpected_exception_in_one_scenario_is_a_failure_not_a_crash(lab, monkeypatch, capsys):
    lab.rules = guest_rules(vm, "bios")

    def explode(self):
        raise KeyError("internal harness bug")

    monkeypatch.setattr(vm.ScenarioRunner, "verify", explode)
    assert lab.run("--scenario", "bios") == 1
    assert "FAIL bios" in capsys.readouterr().err


# --- preflight ---------------------------------------------------------------------------------

def preflight_error(lab, capsys, *arguments, drop=()):
    argv = lab.argv(*arguments)
    for flag in drop:
        index = argv.index(flag)
        del argv[index:index + 2]
    with pytest.raises(SystemExit) as raised:
        vm.main(argv)
    assert raised.value.code == 2
    return capsys.readouterr().err


def test_preflight_refuses_a_missing_control_channel(lab, capsys):
    error = preflight_error(lab, capsys, "--scenario", "bios", drop=("--ssh-private-key",))
    assert "Refusing to run without" in error
    assert lab.run_dirs() == []


def test_preflight_refuses_a_busy_ssh_port(lab, capsys):
    import socket
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        lab.port = holder.getsockname()[1]
        error = preflight_error(lab, capsys, "--scenario", "bios")
    assert "unavailable" in error and lab.run_dirs() == []


def test_preflight_refuses_uefi_scenarios_without_ovmf(lab, capsys, monkeypatch):
    monkeypatch.setattr(vm, "detect_ovmf", lambda candidates=None: None)
    assert "OVMF was not found" in preflight_error(lab, capsys, "--scenario", "openrc")


def test_preflight_allows_the_bios_scenario_without_ovmf(lab, monkeypatch):
    monkeypatch.setattr(vm, "detect_ovmf", lambda candidates=None: None)
    lab.rules = guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios") == 0


def test_preflight_requires_root_ssh_for_the_live_environment(lab, capsys):
    assert "use --ssh-user root" in preflight_error(lab, capsys, "--scenario", "bios", "--ssh-user", "vmtest")


def test_preflight_names_a_missing_executable(lab, capsys):
    error = preflight_error(lab, capsys, "--scenario", "bios", "--qemu-binary", "/nonexistent/qemu")
    assert "Required executable not found: /nonexistent/qemu" in error


def test_preflight_requires_an_iso(lab, capsys):
    argv = [a for a in lab.argv("--scenario", "bios")]
    index = argv.index("--iso")
    del argv[index:index + 2]
    with pytest.raises(SystemExit) as raised:
        vm.main(argv)
    assert raised.value.code == 2 and "--iso is required" in capsys.readouterr().err


# --- signals (T6) ----------------------------------------------------------------------------------

DRIVER = textwrap.dedent("""
    import sys
    from pathlib import Path
    sys.path.insert(0, {root!r})
    from tests.vm.helpers import load_vm_module
    vm = load_vm_module()
    vm.VM_ROOT = Path({vm_root!r})
    firmware = vm.OvmfFirmware(Path({code!r}), Path({variables!r}))
    vm.detect_ovmf = lambda candidates=None: firmware
    raise SystemExit(vm.main(sys.argv[1:]))
""")


@pytest.mark.slow
@pytest.mark.parametrize(("signum", "status"), [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)])
def test_a_signal_stops_the_run_with_the_conventional_status_and_no_qemu_left(lab, signum, status):
    lab.rules = guest_rules(vm, "bios", extra=[
        {"user": "^root$", "match": r"^gentoo-installerd ", "sleep": 120, "events": EVENT_STREAM}])
    lab.write()
    script = DRIVER.format(root=str(ROOT), vm_root=str(lab.root), code=str(lab.firmware.code),
                           variables=str(lab.firmware.variables))
    harness = subprocess.Popen([sys.executable, "-c", script, *lab.argv("--scenario", "bios")],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            dirs = lab.run_dirs()
            if dirs and (dirs[0] / "fake-guest" / "manifest.json").exists():
                break  # the live VM is up and the install command is running
            time.sleep(0.1)
        else:
            raise AssertionError("the harness never reached the install step")
        run = lab.run_dirs()[0]
        qemu = pids(run)
        assert qemu and all(process_alive(pid) for pid in qemu)
        harness.send_signal(signum)
        _, stderr = harness.communicate(timeout=30)
        assert harness.returncode == status, stderr
        assert "Interrupted" in stderr
        deadline = time.monotonic() + 10
        while any(process_alive(pid) for pid in qemu) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not any(process_alive(pid) for pid in qemu)
        result = json.loads((run / "result.json").read_text())
        assert result["success"] is False and result["interrupted"] == int(signum)
    finally:
        harness.kill()
        harness.wait()
        for run in lab.run_dirs():
            for pid in pids(run):
                if process_alive(pid):
                    os.kill(pid, signal.SIGKILL)
