"""The wider scenario matrix (TD-N3, STOR-NEW-6, ISO-NEW-5) through the fake lab: what each dimension
changes in the QEMU command line, the guard, the manifest and the verdict."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.vm.fake_env import EVENT_STREAM, FAILED_STREAM, Lab, guest_rules
from tests.vm.helpers import load_vm_module

vm = load_vm_module()


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return Lab(vm, tmp_path, monkeypatch)


def argv_of(run: Path, phase: str) -> list[str]:
    return json.loads((run / f"fake-qemu-argv-{phase}.json").read_text())


def uploaded(run: Path) -> dict:
    return json.loads((run / "fake-guest" / "manifest.json").read_text())


def test_disks_are_created_with_exact_unusual_sizes_not_round_gigabytes(lab):
    lab.rules = guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios", "--keep", "--disk-size", "20") == 0
    created = [json.loads(line) for line in lab.qemu_img_log.read_text().splitlines()]
    [(_, _, image_format, path, size)] = [entry[:5] for entry in created]
    assert image_format == "qcow2" and path.endswith("disk.qcow2")
    assert int(size) > 20 * 2**30 and int(size) % 2**30 != 0  # whole GiB plus a few MiB: no real disk has this size
    assert lab.result()["disk"] == {"device": "/dev/vda", "bytes": int(size)}


def test_the_guard_runs_before_every_destructive_step_and_names_the_nonce(lab):
    lab.rules = guest_rules(vm, "bios")
    assert lab.run("--scenario", "bios", "--check-resume") == 0
    calls = lab.ssh_calls()
    kinds = ["guard" if "vm_disk_guard" in call["remote"] else call["remote"][:30] for call in calls if call["user"] == "root"]
    upload = next(index for index, kind in enumerate(kinds) if kind.startswith("cat > /run/gentoo-installer-vm"))
    assert kinds.index("guard") < upload  # the install guard comes first
    assert kinds.count("guard") == 2  # and the resume guard
    live = argv_of(lab.run_dir(), "live")
    [nonce] = [part.split("=", 3)[-1] for part in live if part.startswith("type=11,value=org.gentoo-installer.vm-nonce=")]
    specs = [json.loads(call["remote"].rsplit("'", 2)[-2]) for call in calls if "vm_disk_guard" in call["remote"]]
    assert {spec["nonce"] for spec in specs} == {nonce}  # the VM and the guard agree on this run's nonce


@pytest.mark.parametrize(("flags", "bus", "device", "marker"), [
    (["--disk-bus", "nvme", "--nic", "e1000e", "--vga", "std"], "nvme", "/dev/nvme0n1", "nvme,drive=target"),
    (["--disk-bus", "sata", "--machine", "pc"], "sata", "/dev/sda", "ide-hd,bus=ahci0.0"),
    (["--disk-bus", "nvme", "--sector-size", "4096"], "nvme", "/dev/nvme0n1", "nvme-ns,drive=target"),
])
def test_other_hardware_changes_the_machine_the_guard_and_the_device_in_the_manifest(lab, flags, bus, device, marker):
    sector = 4096 if "4096" in flags else None
    lab.rules = guest_rules(vm, "openrc", guard_options={"bus": bus, "sector": sector})
    assert lab.run("--scenario", "openrc", "--keep", *flags) == 0
    run = lab.run_dir()
    assert marker in " ".join(argv_of(run, "live")) and marker in " ".join(argv_of(run, "target"))
    manifest = uploaded(run)
    assert manifest["storage"]["device"] == device and manifest["storage"]["device_id"] == vm.TARGET_SERIAL
    assert json.loads((run / "result.json").read_text())["hardware"]["disk_bus"] == bus


def test_a_disk_without_serial_is_installed_with_a_by_path_identity(lab):
    lab.rules = guest_rules(vm, "serial-less")
    assert lab.run("--scenario", "serial-less", "--keep") == 0
    run = lab.run_dir()
    size = lab.result()["disk"]["bytes"]
    storage = uploaded(run)["storage"]
    assert storage["device_id_kind"] == "path" and storage["device_id"] == f"pci-0000:00:07.0|{size}|"
    assert vm.TARGET_SERIAL not in " ".join(argv_of(run, "live"))


def test_an_unexpected_extra_disk_stops_the_run_before_anything_is_written(lab):
    lab.rules = guest_rules(vm, "bios", guard_options={"mutate": "extra_disk"})
    assert lab.run("--scenario", "bios") == 1
    run = lab.run_dir()
    assert "Unexpected disk(s) present" in lab.result()["error"]
    assert not (run / "fake-guest" / "manifest.json").exists()  # the manifest was never uploaded


# --- decoys ----------------------------------------------------------------------------------------

def test_a_decoy_disk_that_survives_the_install_passes(lab):
    lab.rules = guest_rules(vm, "decoy-disk")
    assert lab.run("--scenario", "decoy-disk", "--keep") == 0
    run = lab.run_dir()
    result = lab.result()
    prepared = result["decoys"]["decoy-1"]
    assert prepared["sha256"] == hashlib.sha256((run / "extra-1.img").read_bytes()).hexdigest()
    assert any("extra-1.img" in part for part in argv_of(run, "target"))  # still attached for the installed boot
    qemu_img = [json.loads(line) for line in lab.qemu_img_log.read_text().splitlines()]
    assert [entry[2] for entry in qemu_img] == ["qcow2", "raw"]  # the decoy is a raw file the host can checksum


def test_the_installer_writing_to_a_decoy_is_caught_in_the_guest(lab):
    lab.rules = guest_rules(vm, "decoy-disk", install=EVENT_STREAM, extra=[
        {"user": "^root$", "match": r"^gentoo-installerd ", "events": EVENT_STREAM, "write": {"path": "extra-1.img", "text": "overwritten"}}])
    assert lab.run("--scenario", "decoy-disk") == 1
    assert "changed during the installation" in lab.result()["error"]


def test_a_decoy_changed_while_the_installed_system_booted_is_caught_on_the_host(lab):
    lab.rules = guest_rules(vm, "decoy-disk", extra=[
        {"user": "^vmtest$", "match": r"^hostname$", "stdout": "gentoo-vm-decoy-disk\n", "write": {"path": "extra-1.img", "text": "scribbled"}}])
    assert lab.run("--scenario", "decoy-disk") == 1
    assert "decoy image extra-1.img changed" in lab.result()["error"]


# --- reinstall, refusal, fresh NVRAM ---------------------------------------------------------------

def test_reinstall_runs_the_installer_twice_with_a_different_layout(lab):
    lab.rules = guest_rules(vm, "reinstall", swap_mib=768)
    assert lab.run("--scenario", "reinstall", "--keep") == 0
    run = lab.run_dir()
    first = json.loads((run / "manifest-first.json").read_text())
    second = json.loads((run / "manifest.json").read_text())
    assert (first["storage"]["swap_mib"], second["storage"]["swap_mib"]) == (512, 768)
    installs = [call for call in lab.ssh_calls() if call["remote"].startswith("gentoo-installerd ")]
    assert len(installs) == 2
    guards = [json.loads(call["remote"].rsplit("'", 2)[-2]) for call in lab.ssh_calls() if "vm_disk_guard" in call["remote"]]
    assert [g["disks"][0]["blank"] for g in guards] == [True, False]  # the second install starts on a used disk


def test_a_scenario_that_must_be_refused_passes_when_the_installer_refuses_and_writes_nothing(lab):
    refusal = [{"type": "error", "error": "The selected disk is too small: at least 16 GiB is required"},
               {"type": "completed", "data": {"success": False}, "message": "refused"}]
    lab.rules = guest_rules(vm, "small-disk", install=refusal)
    assert lab.run("--scenario", "small-disk", "--keep") == 0
    run = lab.run_dir()
    result = lab.result()
    assert result["success"] is True and "too small" in result["refusal"]["refusal"]
    assert not (run / "fake-qemu-argv-target.json").exists()  # nothing installed, so nothing booted
    created = [json.loads(line) for line in lab.qemu_img_log.read_text().splitlines()]
    assert 6 * 2**30 < int(created[0][4]) < 7 * 2**30


def test_accepting_a_disk_that_had_to_be_refused_is_a_failure(lab):
    lab.rules = guest_rules(vm, "small-disk")
    assert lab.run("--scenario", "small-disk") == 1
    assert "accepted a disk it should have refused" in lab.result()["error"]


def test_a_refusal_that_already_ran_destructive_commands_is_a_failure(lab):
    refusal = [{"type": "log", "message": "$ mkfs.ext4 -F /dev/vda2"},
               {"type": "error", "error": "The selected disk is too small"},
               {"type": "completed", "data": {"success": False}, "message": "refused"}]
    lab.rules = guest_rules(vm, "small-disk", install=refusal)
    assert lab.run("--scenario", "small-disk") == 1
    assert "destructive commands ran before the refusal" in lab.result()["error"]


def test_the_installed_disk_boots_with_an_empty_nvram_when_asked(lab):
    lab.rules = guest_rules(vm, "uefi-fresh-nvram", extra=[
        {"user": "^root$", "match": r"^gentoo-installerd ", "events": EVENT_STREAM,
         "write": {"path": "OVMF_VARS.fd", "text": "Boot0008 Gentoo"}}])
    assert lab.run("--scenario", "uefi-fresh-nvram", "--keep") == 0
    run = lab.run_dir()
    assert (run / "OVMF_VARS.live.fd").read_text() == "Boot0008 Gentoo"  # what the installer registered
    assert (run / "OVMF_VARS.fd").read_bytes() == b"vars"  # the target saw the pristine store
    live, target = argv_of(run, "live"), argv_of(run, "target")
    store = f"if=pflash,format=raw,file={run / 'OVMF_VARS.fd'}"
    assert store in live and store in target


def test_the_shared_nvram_is_the_default(lab):
    lab.rules = guest_rules(vm, "openrc", extra=[
        {"user": "^root$", "match": r"^gentoo-installerd ", "events": EVENT_STREAM,
         "write": {"path": "OVMF_VARS.fd", "text": "Boot0008 Gentoo"}}])
    assert lab.run("--scenario", "openrc", "--keep") == 0
    run = lab.run_dir()
    assert (run / "OVMF_VARS.fd").read_text() == "Boot0008 Gentoo" and not (run / "OVMF_VARS.live.fd").exists()


def test_firmware_can_be_overridden_per_run(lab):
    lab.rules = guest_rules(vm, "openrc")
    lab.rules.insert(0, {"user": "^vmtest$", "match": r"^test -d /sys/firmware/efi", "stdout": "bios\n"})
    assert lab.run("--scenario", "openrc", "--firmware", "bios", "--keep") == 0
    run = lab.run_dir()
    assert not any("pflash" in part for part in argv_of(run, "live"))
    storage = uploaded(run)["storage"]
    assert (storage["firmware"], storage["table"]) == ("bios", "dos")


def test_listing_scenarios_does_not_need_an_iso_or_a_lab(capsys):
    assert vm.main(["--list-scenarios"]) == 0
    assert "uefi-fresh-nvram" in capsys.readouterr().out
