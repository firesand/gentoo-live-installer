"""T6: the harness must not leave QEMU behind when it is terminated."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests.vm.helpers import load_vm_module

vm = load_vm_module()
ROOT = Path(__file__).resolve().parents[2]


def test_termination_signals_become_keyboard_interrupt():
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        vm.install_termination_handlers()
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with pytest.raises(KeyboardInterrupt) as raised:
                signal.raise_signal(sig)
            assert raised.value.signum == sig
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def test_main_restores_the_previous_signal_handlers():
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    with pytest.raises(SystemExit):
        vm.main(["--scenario", "openrc"])  # argparse error (no --iso) exits early
    assert {s: signal.getsignal(s) for s in before} == before


@pytest.mark.slow
def test_qemu_dies_when_the_harness_is_killed(tmp_path):
    """SIGKILL gives the harness no chance to clean up; PR_SET_PDEATHSIG must still stop QEMU."""
    fake_qemu = textwrap.dedent("""
        import socket, time
        s = socket.socket(socket.AF_UNIX); s.bind("serial.sock"); s.listen(1); time.sleep(300)
    """)
    script = textwrap.dedent(f"""
        import sys, time
        from pathlib import Path
        sys.path.insert(0, {str(ROOT)!r})
        from tests.vm.helpers import load_vm_module
        vm = load_vm_module()
        run_dir = Path(sys.argv[1])
        guest = vm.VMProcess([sys.executable, "-c", {fake_qemu!r}], run_dir, "live")
        guest.start()
        print(guest.process.pid, flush=True)
        time.sleep(300)
    """)
    harness = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    child = None
    state = ""
    try:
        child = int(harness.stdout.readline())
        assert Path(f"/proc/{child}").exists()
        harness.kill()
        harness.wait(timeout=10)
        deadline = time.monotonic() + 10
        while Path(f"/proc/{child}").exists() and time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{child}/stat").read_text().split(") ")[1].split()[0]
            except OSError:
                break
            if state == "Z":  # waiting to be reaped by init; no longer running
                break
            time.sleep(0.1)
        assert not Path(f"/proc/{child}").exists() or state == "Z"
    finally:
        harness.kill()
        if child is not None:
            try:
                os.kill(child, signal.SIGKILL)  # never leave the sleeper behind if the assertion failed
            except ProcessLookupError:
                pass


def fake_proc(tmp_path: Path, pid: int, argv: list[str]) -> Path:
    proc = tmp_path / "proc"
    (proc / str(pid)).mkdir(parents=True, exist_ok=True)
    (proc / str(pid) / "cmdline").write_bytes(b"\0".join(part.encode() for part in argv) + b"\0")
    return proc


def stale_run(tmp_path: Path, name: str = "openrc-1", pid: int = 4242) -> tuple[Path, Path]:
    root = tmp_path / "build" / "vm"
    run = root / name
    run.mkdir(parents=True)
    pid_file = run / "qemu-live.pid"
    pid_file.write_text(f"{pid}\n")
    return root, pid_file


def test_stale_cleanup_finds_only_harness_qemu_processes_of_this_user(tmp_path):
    root, pid_file = stale_run(tmp_path)
    run = pid_file.parent
    qemu = ["/usr/bin/qemu-system-x86_64", "-name", "gentoo-installer-openrc", "-drive", f"file={run}/disk.qcow2"]
    proc = fake_proc(tmp_path, 4242, qemu)
    assert vm.find_stale_qemu(root, proc) == [(pid_file, 4242)]
    # A recycled pid now running something else, or another run's QEMU, is never touched.
    for argv in (["/usr/bin/sleep", "300"],
                 ["/usr/bin/qemu-system-x86_64", "-name", "someone-elses-vm", "-drive", f"file={run}/disk.qcow2"],
                 ["/usr/bin/qemu-system-x86_64", "-name", "gentoo-installer-openrc", "-drive", "file=/elsewhere/disk.qcow2"]):
        proc = fake_proc(tmp_path / "other", 4242, argv)
        assert vm.find_stale_qemu(root, proc) == [], argv
    assert vm.find_stale_qemu(root, tmp_path / "no-proc") == []  # process already gone


def test_stale_cleanup_terminates_then_kills_and_removes_the_pid_file(tmp_path):
    root, pid_file = stale_run(tmp_path)
    run = pid_file.parent
    proc = fake_proc(tmp_path, 4242, ["qemu-system-x86_64", "-name", "gentoo-installer-openrc", "-drive", f"file={run}/disk.qcow2"])
    sent: list[tuple[int, int]] = []
    clock = {"slept": 0.0}

    def kill(pid, sig):
        sent.append((pid, sig))
        if sig == signal.SIGKILL:  # the process only goes away for SIGKILL: it ignored SIGTERM
            (proc / str(pid) / "cmdline").unlink()
            (proc / str(pid)).rmdir()

    def sleep(seconds):
        clock["slept"] += seconds

    assert vm.cleanup_stale(root, proc, kill=kill, sleep=sleep, grace=3.0) == [4242]
    assert sent == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert clock["slept"] == 3.0
    assert not pid_file.exists()


def test_cleanup_stale_flag_runs_without_an_iso_and_without_a_scenario(tmp_path, monkeypatch, capsys):
    root, pid_file = stale_run(tmp_path)
    monkeypatch.setattr(vm, "VM_ROOT", root)
    monkeypatch.setattr(vm, "cleanup_stale", lambda: [])
    assert vm.main(["--cleanup-stale"]) == 0
    assert "Stopped 0 stale QEMU process(es)" in capsys.readouterr().out
