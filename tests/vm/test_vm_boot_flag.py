"""The boot flag of a BIOS install: strict BIOS firmware starts only a disk whose protective MBR (GPT) or one partition (DOS) is flagged.

QEMU's SeaBIOS never asks for the flag, so a VM cannot show whether it is there. The harness reads the first sector of the installed disk
in the installed system (scripts/vm_probes.py). The sectors in tests/vm/data/mbr-*.bin were written by parted and sgdisk with the commands of
the storage stage (README-mbr.txt), so the decoder is tested against what those tools really leave.
"""
from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import vm_probes
from gentoo_installer.manifest import InstallManifest
from tests.vm.helpers import PUBLIC_KEY, load_vm_module

vm = load_vm_module()
DATA = Path(__file__).resolve().parent / "data"


def sector(name: str) -> bytes:
    return (DATA / f"mbr-{name}.bin").read_bytes()


def manifest_of(name: str = "bios", **changes) -> InstallManifest:
    manifest = vm.build_manifest(vm.SCENARIO_MAP[name], PUBLIC_KEY)
    for path, value in changes.items():
        section, key = path.split("__")
        setattr(getattr(manifest, section), key, value)
    return manifest


def report_for(name: str, partition: int = 2, holder: str = "/") -> dict:
    return {"holder": holder, "source": f"/dev/vda{partition}", "partition": partition, "disk": "/dev/vda", **vm_probes.decode_mbr(sector(name))}


# --- reading the first sector --------------------------------------------------------------------------------------------

def test_the_decoder_reads_what_parted_and_sgdisk_leave_in_the_first_sector():
    flagged = vm_probes.decode_mbr(sector("gpt-pmbr-boot"))
    assert flagged["signature"] == "55aa" and flagged["entries"][0]["type"] == 0xEE and flagged["entries"][0]["status"] == 0x80
    assert [entry["status"] for entry in flagged["entries"]] == [0x80, 0, 0, 0]
    assert vm_probes.decode_mbr(sector("gpt-no-flag"))["entries"][0]["status"] == 0
    root = vm_probes.decode_mbr(sector("dos-root-active"))
    assert [(entry["number"], entry["status"], entry["type"]) for entry in root["entries"]] == [(1, 0, 0x82), (2, 0x80, 0x83), (3, 0, 0), (4, 0, 0)]
    assert root["entries"][0]["start"] == 2048 and root["entries"][0]["sectors"] == 512 * 2048  # 1 MiB in, 512 MiB of swap
    assert [entry["status"] for entry in vm_probes.decode_mbr(sector("dos-boot-active"))["entries"]] == [0x80, 0, 0, 0]


# --- the verdict ---------------------------------------------------------------------------------------------------------

def test_a_gpt_disk_needs_the_boot_flag_in_its_protective_mbr():
    assert vm_probes.boot_flag_problems(report_for("gpt-pmbr-boot", 3)) == []
    problems = vm_probes.boot_flag_problems(report_for("gpt-no-flag", 3))
    assert len(problems) == 1 and "protective MBR" in problems[0] and "0x00" in problems[0] and "no bootable device" in problems[0]


def test_a_dos_disk_needs_its_boot_or_root_partition_to_be_the_active_one():
    assert vm_probes.boot_flag_problems(report_for("dos-root-active", 2)) == []
    assert vm_probes.boot_flag_problems(report_for("dos-boot-active", 1, holder="/boot")) == []
    none = vm_probes.boot_flag_problems(report_for("dos-no-flag", 2))
    assert len(none) == 1 and "no partition" in none[0] and "active" in none[0]
    wrong = vm_probes.boot_flag_problems(report_for("dos-swap-active", 2))
    assert len(wrong) == 1 and "partition 1 is active" in wrong[0] and "/ is on partition 2" in wrong[0]
    boot_wrong = vm_probes.boot_flag_problems(report_for("dos-root-active", 2, holder="/boot"))  # /boot on partition 2, flag on 2: fine
    assert boot_wrong == []
    assert "/boot is on partition 1" in vm_probes.boot_flag_problems(report_for("dos-root-active", 1, holder="/boot"))[0]


def test_two_active_partitions_and_a_status_byte_the_firmware_would_refuse_are_problems():
    raw = bytearray(sector("dos-root-active"))
    raw[446] = 0x80  # partition 1 active as well
    problems = vm_probes.boot_flag_problems({"holder": "/", "partition": 2, "disk": "/dev/vda", **vm_probes.decode_mbr(bytes(raw))})
    assert len(problems) == 1 and "2 partitions are active" in problems[0]
    raw[446] = 0x01  # neither 0x00 nor 0x80
    assert any("0x01" in problem for problem in vm_probes.boot_flag_problems({"holder": "/", "partition": 2, "disk": "/dev/vda", **vm_probes.decode_mbr(bytes(raw))}))


def test_a_logical_partition_cannot_be_active_and_a_sector_without_signature_boots_nothing():
    assert "logical partition" in " ".join(vm_probes.boot_flag_problems(report_for("dos-root-active", 5)))
    raw = bytearray(sector("dos-root-active"))
    raw[510:512] = b"\0\0"
    assert "boot signature" in " ".join(vm_probes.boot_flag_problems({"holder": "/", "partition": 2, "disk": "/dev/vda", **vm_probes.decode_mbr(bytes(raw))}))


def test_the_probe_judges_the_report_the_snippet_prints():
    good = vm_probes.parse_boot_flag_probe(0, json.dumps(report_for("dos-root-active")) + "\n")
    assert good.name == "boot-flag" and good.success and not good.advisory
    bad = vm_probes.parse_boot_flag_probe(0, json.dumps(report_for("gpt-no-flag", 3)))
    assert not bad.success and not bad.advisory and "protective MBR" in bad.error
    unreadable = vm_probes.parse_boot_flag_probe(1, json.dumps({"error": "/dev/mapper/gentoo-root is not on a partition"}))
    assert not unreadable.success and "could not be read" in unreadable.error and "not on a partition" in unreadable.error
    assert "no JSON" in vm_probes.parse_boot_flag_probe(0, "hello").error
    nosudo = vm_probes.parse_boot_flag_probe(1, "sudo: a password is required")
    assert not nosudo.success and nosudo.advisory and "cannot use sudo" in nosudo.error


# --- the program that runs in the installed system ----------------------------------------------------------------------------

def guest(tmp_path: Path, *, mounts: dict[str, str], layout: dict[str, str], image: str, stacked: dict[str, list[str]] | None = None) -> subprocess.CompletedProcess:
    """Run the snippet as the guest would, with a findmnt that answers from ``mounts`` and a sysfs/dev tree built from ``layout`` {partition: disk}."""
    sys_class, dev = tmp_path / "class" / "block", tmp_path / "dev"
    sys_class.mkdir(parents=True)
    dev.mkdir()
    for number, (partition, disk) in enumerate(layout.items(), start=1):
        directory = tmp_path / "devices" / disk / partition
        directory.mkdir(parents=True)
        (directory / "partition").write_text(f"{partition.removeprefix(disk).lstrip('p')}\n")
        (sys_class / partition).symlink_to(directory)
    for name, slaves in (stacked or {}).items():
        directory = tmp_path / "devices" / "virtual" / name
        (directory / "slaves").mkdir(parents=True)
        for slave in slaves:
            (directory / "slaves" / slave).symlink_to(sys_class / slave)
        (sys_class / name).symlink_to(directory)
    for disk in set(layout.values()):
        (dev / disk).write_bytes(sector(image) + b"\0" * 512)
    findmnt = tmp_path / "bin" / "findmnt"
    findmnt.parent.mkdir()
    findmnt.write_text("#!/bin/sh\ncase \"$3\" in\n" + "".join(f"  {mountpoint}) echo '{source}';;\n" for mountpoint, source in mounts.items())
                       + "  *) exit 1;;\nesac\n")
    findmnt.chmod(findmnt.stat().st_mode | stat.S_IXUSR)
    argv = shlex.split(vm_probes.boot_flag_command())
    assert argv[:5] == ["sudo", "-S", "-p", "", "python3"]
    return subprocess.run([sys.executable, *argv[5:], str(sys_class), str(dev)], capture_output=True, text=True, check=False,
                          env={**os.environ, "PATH": f"{findmnt.parent}:{os.environ['PATH']}"})


def test_the_program_finds_the_partition_of_the_root_and_reads_the_disk_it_is_on(tmp_path):
    done = guest(tmp_path, mounts={"/": "/dev/vda2"}, layout={"vda1": "vda", "vda2": "vda"}, image="dos-root-active")
    assert done.returncode == 0, done.stdout + done.stderr
    report = json.loads(done.stdout.splitlines()[-1])
    assert (report["holder"], report["source"], report["partition"], report["disk"]) == ("/", "/dev/vda2", 2, "/dev/vda")
    assert [entry["status"] for entry in report["entries"]] == [0, 0x80, 0, 0]
    assert vm_probes.parse_boot_flag_probe(0, done.stdout).success


def test_a_separate_boot_partition_is_the_one_that_must_be_active(tmp_path):
    done = guest(tmp_path, mounts={"/boot": "/dev/nvme0n1p1", "/": "/dev/dm-1"},
                 layout={"nvme0n1p1": "nvme0n1", "nvme0n1p2": "nvme0n1"}, image="dos-boot-active", stacked={"dm-1": ["nvme0n1p2"]})
    report = json.loads(done.stdout.splitlines()[-1])
    assert done.returncode == 0 and (report["holder"], report["partition"], report["disk"]) == ("/boot", 1, "/dev/nvme0n1")
    assert vm_probes.parse_boot_flag_probe(0, done.stdout).success


def test_a_btrfs_subvolume_is_still_a_device_to_the_program(tmp_path):
    done = guest(tmp_path, mounts={"/": "/dev/vda2[/@]"}, layout={"vda1": "vda", "vda2": "vda"}, image="dos-root-active")
    report = json.loads(done.stdout.splitlines()[-1])
    assert done.returncode == 0 and (report["holder"], report["source"], report["partition"]) == ("/", "/dev/vda2", 2)


def test_a_root_on_a_logical_volume_is_followed_down_to_the_partition_that_holds_it(tmp_path):
    done = guest(tmp_path, mounts={"/": "/dev/dm-2"}, layout={"sda1": "sda", "sda2": "sda"}, image="dos-root-active",
                 stacked={"dm-1": ["sda2"], "dm-2": ["dm-1"]})  # dm-2 (the volume) sits on dm-1 (the container) on sda2
    report = json.loads(done.stdout.splitlines()[-1])
    assert done.returncode == 0 and (report["partition"], report["disk"]) == (2, "/dev/sda")
    assert vm_probes.parse_boot_flag_probe(0, done.stdout).success


def test_a_root_that_is_not_on_a_partition_is_reported_not_guessed(tmp_path):
    done = guest(tmp_path, mounts={"/": "/dev/loop0"}, layout={"vda1": "vda"}, image="dos-root-active")
    assert done.returncode == 1 and "not on a partition" in json.loads(done.stdout.splitlines()[-1])["error"]
    assert "could not be read" in vm_probes.parse_boot_flag_probe(done.returncode, done.stdout).error


def test_the_program_says_what_it_is_in_a_comment_so_the_fake_lab_can_recognise_it():
    assert vm_probes.BOOT_FLAG_MARK in vm_probes.boot_flag_command()
    assert vm_probes.boot_flag_command().startswith("sudo -S -p '' python3 -c ")


# --- which installs get the probe ---------------------------------------------------------------------------------------------

def probe_names(name: str, **changes) -> set[str]:
    manifest = manifest_of(name, **changes)
    return {probe[0] for probe in vm.installed_system_probes(manifest, vm.SCENARIO_MAP[name])}


@pytest.mark.parametrize("name", ["bios", "bios-sata-minimal", "bios-gpt", "sata-bios"])
def test_every_guided_bios_install_reads_the_boot_flag(name):
    assert "boot-flag" in probe_names(name)


@pytest.mark.parametrize("name", ["openrc", "uefi-luks", "uefi-nvme-luks-openrc", "usb-portable", "manual-two-disks"])
def test_uefi_installs_and_manual_layouts_do_not(name):
    assert "boot-flag" not in probe_names(name)


def test_a_bios_install_in_manual_mode_is_not_held_to_the_guided_flag_rules():
    """A manual layout keeps a flag that is already there and flags nothing next to an EFI System Partition."""
    assert "boot-flag" not in probe_names("bios", storage__mode="manual")


def test_the_probe_runs_with_the_test_users_password_for_sudo():
    probes = {probe[0]: probe for probe in vm.installed_system_probes(manifest_of("bios"), vm.SCENARIO_MAP["bios"])}
    name, command, parser, stdin = probes["boot-flag"]
    assert command == vm_probes.boot_flag_command() and stdin == (vm.TEST_PASSWORD + "\n").encode() and parser is vm_probes.parse_boot_flag_probe
