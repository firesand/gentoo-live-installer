"""T9 / T7 / DELTA-7: socket paths, SSH isolation and the XFCE wait."""
from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.vm_qmp import SUN_PATH_MAX, connect_unix
from tests.vm.helpers import load_vm_module

vm = load_vm_module()


def test_unix_sockets_work_from_deep_directories(tmp_path):
    deep = tmp_path
    for part in ("a" * 40, "b" * 40, "c" * 40):
        deep = deep / part
    deep.mkdir(parents=True)
    path = deep / "qmp.sock"
    assert len(os.fsencode(path)) > SUN_PATH_MAX
    listener = socket.socket(socket.AF_UNIX)
    cwd = os.getcwd()
    os.chdir(deep)
    try:
        listener.bind("qmp.sock")  # a server can only bind through a short relative name
    finally:
        os.chdir(cwd)
    listener.listen(1)
    with connect_unix(path, timeout=2):
        server, _ = listener.accept()
        server.close()
    listener.close()


def test_short_paths_connect_directly(sock_dir):
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(sock_dir / "s.sock"))
    listener.listen(1)
    with connect_unix(sock_dir / "s.sock", timeout=2):
        listener.accept()[0].close()
    listener.close()


def test_socket_arguments_stay_absolute_unless_they_exceed_sun_path():
    assert vm.socket_argument(Path("/r"), "qmp.sock") == "/r/qmp.sock"
    assert vm.socket_argument(Path("/" + "d" * 120), "qmp.sock") == "qmp.sock"


def test_qemu_command_uses_relative_sockets_for_deep_run_directories(tmp_path, monkeypatch):
    root = tmp_path / "build" / "vm"
    deep = root / ("d" * 120)
    deep.mkdir(parents=True)
    iso = tmp_path / "installer.iso"
    iso.write_bytes(b"iso")
    monkeypatch.setattr(vm, "VM_ROOT", root)
    command = vm.qemu_command(vm.SCENARIO_MAP["bios"], iso=iso, disk=deep / "disk.qcow2", run_dir=deep, live=True,
                              ssh_host="127.0.0.1", ssh_port=40222, memory_mib=2048, cpus=2)
    joined = " ".join(command)
    assert "path=serial.sock," in joined and "unix:qmp.sock," in joined


def test_qemu_command_keeps_absolute_sockets_when_they_fit(sock_dir, monkeypatch):
    root = sock_dir / "build" / "vm"
    run = root / "run"
    run.mkdir(parents=True)
    iso = sock_dir / "installer.iso"
    iso.write_bytes(b"iso")
    monkeypatch.setattr(vm, "VM_ROOT", root)
    command = vm.qemu_command(vm.SCENARIO_MAP["bios"], iso=iso, disk=run / "disk.qcow2", run_dir=run, live=True,
                              ssh_host="127.0.0.1", ssh_port=40222, memory_mib=2048, cpus=2)
    assert str(run / "qmp.sock") in " ".join(command)  # operator tooling identifies the VM by it


def test_serial_connect_reports_a_dead_qemu_immediately(tmp_path):
    dead = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    dead.wait()
    channel = vm.SerialChannel(tmp_path / "serial.sock", tmp_path / "serial.log", dead)
    started = time.monotonic()
    with pytest.raises(vm.HarnessError, match="QEMU exited with status 3"):
        channel.connect(timeout=30)
    assert time.monotonic() - started < 5


def test_qmp_reader_reports_a_socket_closed_before_the_guest_answers(sock_dir):
    from scripts import vm_qmp
    path = sock_dir / "q.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(1)

    import threading

    def serve():
        connection, _ = listener.accept()
        connection.close()  # no greeting at all

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    with pytest.raises(RuntimeError, match="closed"):
        vm_qmp.execute(path, "query-status")
    thread.join(2)
    listener.close()


# --- SSH options (T7) ------------------------------------------------------------------------

def ssh_arguments(tmp_path):
    args = argparse.Namespace(ssh_binary="ssh", ssh_private_key=Path("/tmp/k"), ssh_port=22, ssh_host="127.0.0.1")
    return vm.ssh_base_arguments(args, tmp_path / "known", "root")


def test_ssh_ignores_user_config_and_connection_sharing(tmp_path):
    command = ssh_arguments(tmp_path)
    assert command[1:3] == ["-F", "/dev/null"]
    joined = " ".join(command)
    for option in ("ControlMaster=no", "ControlPath=none", "LogLevel=ERROR"):
        assert option in joined


def test_ssh_tolerates_a_stalled_guest_for_many_minutes_but_not_forever(tmp_path):
    joined = " ".join(ssh_arguments(tmp_path))
    interval = int(re.search(r"ServerAliveInterval=(\d+)", joined).group(1))
    count = int(re.search(r"ServerAliveCountMax=(\d+)", joined).group(1))
    assert 900 <= interval * count <= 3600  # r18: an OpenRC guest thrashed for a long time under memory pressure


# --- XFCE wait (DELTA-7) ---------------------------------------------------------------------

def test_gui_timeout_option_defaults_to_120_and_is_configurable(tmp_path, monkeypatch):
    base = ["--scenario", "openrc", "--iso", str(tmp_path / "x.iso")]
    assert vm.build_parser().parse_args(base).gui_timeout == 120
    assert vm.build_parser().parse_args(base + ["--gui-timeout", "600"]).gui_timeout == 600
    runner = object.__new__(vm.ScenarioRunner)
    runner.args = SimpleNamespace(ssh_user="root", boot_timeout=900, command_timeout=17, gui_timeout=600)
    runner.run_dir = tmp_path
    seen = {}
    monkeypatch.setattr(vm, "poll_gui_environment", lambda probe, **kw: seen.update(kw) or {})
    runner.gui_environment()
    assert seen["timeout"] == 600
    runner.args.boot_timeout = 300  # never longer than the boot timeout
    runner.gui_environment()
    assert seen["timeout"] == 300
