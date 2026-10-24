"""scripts/sandbox.sh: whatever runs inside cannot change the host system (its files, disks, services or network)."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

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
