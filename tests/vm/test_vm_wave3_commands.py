"""The QEMU command lines and small helpers behind the Wave 3 features (no process is started)."""
from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path

import pytest

import vm_iso
from tests.vm.helpers import load_vm_module

vm = load_vm_module()


@pytest.fixture
def paths(tmp_path, monkeypatch):
    root = tmp_path / "build" / "vm"
    run = root / "run"
    run.mkdir(parents=True)
    iso = tmp_path / "installer.iso"
    iso.write_bytes(b"iso")
    monkeypatch.setattr(vm, "VM_ROOT", root)
    return iso, run / "disk.qcow2", run


def direct(tmp_path: Path, cmdline: str = "root=live:CDLABEL=X cdroot nox") -> vm_iso.DirectBoot:
    kernel, initrd = tmp_path / "kernel", tmp_path / "initrd"
    kernel.write_bytes(b"k")
    initrd.write_bytes(b"i")
    return vm_iso.DirectBoot(vm_iso.BootEntry("Text", (), "/boot/gentoo", "/boot/gentoo.igz", tuple(cmdline.split())), kernel, initrd, cmdline)


def command(paths, scenario="bios", **options):
    iso, disk, run = paths
    return vm.qemu_command(vm.SCENARIO_MAP[scenario], iso=iso, disk=disk, run_dir=run, ssh_host="127.0.0.1", ssh_port=40222, memory_mib=4096, cpus=2, **options)


def test_a_menu_entry_is_started_with_its_kernel_initramfs_and_arguments_and_the_iso_stays_attached(paths, tmp_path):
    boot = direct(tmp_path)
    argv = command(paths, live=True, direct_boot=boot)
    assert argv[argv.index("-kernel") + 1] == str(boot.kernel) and argv[argv.index("-initrd") + 1] == str(boot.initrd)
    assert argv[argv.index("-append") + 1] == "root=live:CDLABEL=X cdroot nox"
    assert f"file={paths[0]},media=cdrom,readonly=on,format=raw" in argv


def test_only_the_live_boot_is_started_that_way(paths, tmp_path):
    argv = command(paths, live=False, direct_boot=direct(tmp_path))
    assert "-kernel" not in argv and "-append" not in argv


def test_a_directly_booted_kernel_and_a_cd_with_a_boot_index_would_clash_so_the_cd_gets_none(paths, tmp_path):
    with_entry = command(paths, live=True, cd_first=True, direct_boot=direct(tmp_path))
    assert not any("bootindex" in part for part in with_entry)
    without = command(paths, live=True, cd_first=True)
    assert "ide-cd,drive=cd0,bootindex=0" in without and "-kernel" not in without


def test_paths_with_commas_cannot_reach_the_command_line(paths, tmp_path):
    odd = tmp_path / "a,b"
    odd.mkdir()
    boot = direct(odd)
    with pytest.raises(vm.HarnessError, match="commas or control characters"):
        command(paths, live=True, direct_boot=boot)


def test_the_boot_entry_comes_from_the_command_line_or_the_scenario():
    runner = vm.ScenarioRunner.__new__(vm.ScenarioRunner)
    runner.args = argparse.Namespace(boot_entry=None)
    runner.scenario = vm.SCENARIO_MAP["text-install"]
    assert runner.boot_entry_query() == "text"
    runner.args = argparse.Namespace(boot_entry="serial")
    assert runner.boot_entry_query() == "serial"
    runner.scenario = vm.SCENARIO_MAP["bios"]
    runner.args = argparse.Namespace()
    assert runner.boot_entry_query() == ""


def test_the_install_mode_comes_from_the_command_line_the_scenario_or_defaults_to_the_daemon():
    runner = vm.ScenarioRunner.__new__(vm.ScenarioRunner)
    runner.scenario = vm.SCENARIO_MAP["text-install"]
    runner.args = argparse.Namespace(install_mode=None)
    assert runner.install_mode() == "text"
    runner.args = argparse.Namespace(install_mode="gui")
    assert runner.install_mode() == "gui"
    runner.scenario, runner.args = vm.SCENARIO_MAP["bios"], argparse.Namespace(install_mode=None)
    assert runner.install_mode() == "cli"
    assert vm.hardware_problems(vm.SCENARIO_MAP["text-install"], None) == [] and vm.hardware_problems(dataclasses.replace(vm.SCENARIO_MAP["bios"], hardware=vm.VMHardware(vga="none")), "gui")


def test_a_success_the_disk_cannot_boot_from_is_not_one_and_the_final_event_is_found():
    events = '{"type": "completed", "data": {"success": true, "bootable": false, "degraded": ["x"]}}\n'
    result = vm.parse_install_result(0, events)
    assert result.success is False and "not a bootable system" in result.error
    assert vm.completed_data(result.events) == {"success": True, "bootable": False, "degraded": ["x"]}
    assert vm.parse_install_result(0, '{"type": "completed", "data": {"success": true}}\n').success is True  # an older daemon says nothing about it
    assert vm.completed_data([]) == {}


def test_a_serial_snapshot_is_everything_written_so_far(tmp_path):
    channel = vm.SerialChannel(tmp_path / "s.sock", tmp_path / "s.log")
    channel.buffer = "boot\n"
    assert channel.snapshot() == "boot\n"


def test_the_manual_layout_rows_for_a_second_disk_come_after_the_first_disks_rows():
    rows = vm.manual_partitions("/dev/vda", "uefi", "/dev/vdb")
    assert [(row.device, row.mountpoint, row.filesystem) for row in rows] == [
        ("/dev/vda1", "/efi", "vfat"), ("/dev/vda2", "/", "ext4"), ("/dev/vdb2", "/home", "ext4"), ("/dev/vdb1", "", "swap")]
    assert [row.device for row in vm.manual_partitions("/dev/nvme0n1", "bios", "/dev/nvme1n1")] == ["/dev/nvme0n1p2", "/dev/nvme1n1p2", "/dev/nvme1n1p1"]
    assert len(vm.manual_partitions("/dev/vda", "uefi")) == 2


def test_the_failure_logs_shown_include_the_last_console_and_rescue_command(tmp_path):
    for name in ("install.log", "serial-baseline.log", "serial-final-uefi.log", "rescue-grub-cfg-run.log", "other.txt"):
        (tmp_path / name).write_text(name)
    import os
    os.utime(tmp_path / "serial-baseline.log", (1, 1))
    shown = [path.name for path in vm.failure_logs(tmp_path)]
    assert shown == ["install.log", "serial-target.log", "qemu-live.log", "serial-final-uefi.log", "rescue-grub-cfg-run.log"]
