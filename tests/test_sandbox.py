"""scripts/sandbox.sh: whatever runs inside cannot change the host system (its files, disks, services or network)."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "scripts/sandbox.sh"


def run(*argv: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(SANDBOX), *argv], capture_output=True, text=True, timeout=timeout, cwd=ROOT)


def _usable() -> str:
    if shutil.which("bwrap") is None:
        return "bubblewrap is not installed"
    probe = run("true")
    return "" if probe.returncode == 0 else f"bubblewrap cannot create namespaces here: {probe.stderr.strip()[:200]}"


REASON = _usable()
pytestmark = pytest.mark.skipif(bool(REASON), reason=REASON or "usable")


def test_the_host_file_system_is_read_only() -> None:
    result = run("sh", "-c", "echo x > /etc/gentoo-installer-sandbox-test")
    assert result.returncode != 0 and "Read-only file system" in result.stderr
    assert not Path("/etc/gentoo-installer-sandbox-test").exists()


def test_no_disk_or_other_device_exists_inside() -> None:
    devices = set(run("ls", "/dev").stdout.split())
    assert devices <= {"core", "fd", "full", "null", "ptmx", "pts", "random", "shm", "stderr", "stdin", "stdout", "tty", "urandom", "zero"}
    assert not any(name.startswith(("sd", "nvme", "vd", "hd", "mmcblk", "loop", "dm-", "md", "sr", "dri", "input", "kvm")) for name in devices)


def test_there_is_no_network_without_net() -> None:
    script = "import socket\ns = socket.socket()\ns.settimeout(3)\ntry:\n    s.connect(('1.1.1.1', 53))\n    print('connected')\nexcept OSError as exc:\n    print('blocked', exc.errno)"
    assert run("python3", "-c", script).stdout.startswith("blocked")


def test_home_is_a_fresh_tmpfs_and_nothing_written_there_stays(tmp_path: Path) -> None:
    marker = f".gentoo-installer-sandbox-{os.getpid()}"
    result = run("sh", "-c", f'echo x > "$HOME/{marker}" && ls -a "$HOME"')
    assert result.returncode == 0 and marker in result.stdout
    assert not (Path.home() / marker).exists()


def test_the_checkout_stays_writable_and_the_status_is_passed_on() -> None:
    target = ROOT / ".cache" / f"sandbox-test-{os.getpid()}"
    try:
        assert run("sh", "-c", f"echo ok > '{target}'").returncode == 0
        assert target.read_text() == "ok\n"
    finally:
        target.unlink(missing_ok=True)
    assert run("sh", "-c", "exit 7").returncode == 7


def test_root_cannot_be_gained_and_host_services_are_out_of_reach() -> None:
    status = run("sh", "-c", "grep -E '^(CapEff|CapPrm|CapBnd|NoNewPrivs):' /proc/self/status").stdout
    assert "CapEff:\t0000000000000000" in status and "CapBnd:\t0000000000000000" in status and "NoNewPrivs:\t1" in status
    assert run("sudo", "-n", "true").returncode != 0
    sockets = run("sh", "-c", "ls /run/dbus/system_bus_socket /var/run/docker.sock /run/docker.sock 2>&1; echo \"$DBUS_SESSION_BUS_ADDRESS|$SSH_AUTH_SOCK|$DISPLAY\"").stdout
    assert sockets.count("No such file or directory") == 3 and sockets.strip().endswith("||")
    groups = run("id", "-G").stdout.split()
    assert str(os.getgid()) in groups and len(set(groups) - {str(os.getgid()), "65534"}) == 0  # the docker group is gone


def test_it_marks_the_inside_and_keeps_cargo_out_of_the_users_home() -> None:
    env = run("sh", "-c", 'echo "$GENTOO_INSTALLER_SANDBOX $CARGO_HOME"').stdout.split()
    assert env == ["1", str(ROOT / ".cache/cargo-home")]


def test_usage_errors() -> None:
    assert run().returncode == 2
    assert run("--writable", "/nonexistent-directory", "true").returncode == 2
    assert run("--no-such-option", "true").returncode == 2


# --- memory: /tmp on disk, cleaned up; the command's stdin and signals ---------------------------------------------------


def test_tmp_is_on_disk_below_the_checkout_and_removed_afterwards() -> None:
    """Eleven parallel suites with RAM-backed /tmp once filled the PC's memory: /tmp must be a disk directory."""
    result = run("sh", "-c", "stat -f -c %T /tmp /var/tmp; echo data > /tmp/x; ls -d /tmp/x")
    types = result.stdout.split()[:2]
    assert "tmpfs" not in types and result.stdout.strip().endswith("/tmp/x")
    deadline = time.monotonic() + 10  # a watcher removes the directory within about a second after the command ends
    while any((path / "tmp/x").exists() for path in (ROOT / ".cache/sandbox-tmp").glob("run.*")) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not any((path / "tmp/x").exists() for path in (ROOT / ".cache/sandbox-tmp").glob("run.*"))


def test_a_scratch_directory_of_a_dead_run_is_removed_by_the_next_run() -> None:
    """A killed run leaves its directory with nobody holding its lock; a fresh or locked one is never touched."""
    base = ROOT / ".cache/sandbox-tmp"
    base.mkdir(parents=True, exist_ok=True)
    stale = base / f"run.stale-test-{os.getpid()}"
    (stale / "tmp").mkdir(parents=True, exist_ok=True)
    os.utime(stale, (time.time() - 600, time.time() - 600))
    fresh = base / f"run.fresh-test-{os.getpid()}"
    (fresh / "tmp").mkdir(parents=True, exist_ok=True)
    try:
        assert run("true").returncode == 0
        assert not stale.exists() and fresh.exists()
    finally:
        shutil.rmtree(fresh, ignore_errors=True)
        shutil.rmtree(stale, ignore_errors=True)


def test_a_nested_sandbox_keeps_the_outer_runs_tmp() -> None:
    result = run("sh", "-c", "echo outer > /tmp/mark; scripts/sandbox.sh true; cat /tmp/mark")
    assert result.returncode == 0 and result.stdout.strip() == "outer", result.stderr


def test_stdin_reaches_the_command() -> None:
    result = subprocess.run([str(SANDBOX), "cat"], input="hello\n", capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert result.returncode == 0 and result.stdout == "hello\n"


def test_terminating_the_sandbox_ends_the_command_inside() -> None:
    process = subprocess.Popen([str(SANDBOX), "sleep", "300"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    process.terminate()
    assert process.wait(timeout=30) != 0
    time.sleep(0.5)
    commands = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout.splitlines()
    assert "sleep 300" not in [line.strip() for line in commands]


def test_ctrl_c_still_reaches_programs_inside() -> None:
    """bubblewrap must not be started as a background job: that would make everything inside ignore SIGINT."""
    script = "import signal; print(signal.getsignal(signal.SIGINT) is signal.default_int_handler)"
    assert run("python3", "-c", script).stdout.strip() == "True"
    assert run("sh", "-c", "grep SigIgn /proc/self/status").stdout.split()[1] == "0000000000000000"


# --- scripts/test-slot.sh --------------------------------------------------------------------------------------------------

SLOT = ROOT / "scripts/test-slot.sh"


def slot_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {**os.environ, "XDG_CACHE_HOME": str(tmp_path), **extra}


def test_slots_let_only_the_configured_number_of_commands_run_at_once(tmp_path: Path) -> None:
    log = tmp_path / "log"
    script = f"echo start >> {log}; sleep 2; echo end >> {log}"
    env = slot_env(tmp_path, GENTOO_TEST_SLOTS="1")
    first = subprocess.Popen([str(SLOT), "sh", "-c", script], env=env, stderr=subprocess.PIPE, text=True)
    time.sleep(0.5)
    second = subprocess.Popen([str(SLOT), "sh", "-c", script], env=env, stderr=subprocess.PIPE, text=True)
    assert first.wait(timeout=30) == 0 and second.wait(timeout=30) == 0
    assert log.read_text().split() == ["start", "end", "start", "end"]  # never two at once
    assert "waiting for a free one" in second.stderr.read()


def test_a_slot_passes_the_exit_status_on_and_ends_a_command_that_runs_too_long(tmp_path: Path) -> None:
    assert subprocess.run([str(SLOT), "sh", "-c", "exit 5"], env=slot_env(tmp_path), timeout=30).returncode == 5
    started = time.monotonic()
    result = subprocess.run([str(SLOT), "sleep", "60"], env=slot_env(tmp_path, GENTOO_TEST_TIMEOUT="1"), timeout=90)
    assert result.returncode == 124 and time.monotonic() - started < 30


def test_check_sh_holds_a_slot_while_it_runs() -> None:
    text = (ROOT / "scripts/check.sh").read_text()
    assert 'exec scripts/test-slot.sh scripts/sandbox.sh "$0" "$@"' in text
