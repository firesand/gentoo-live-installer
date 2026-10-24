"""TD-N2: the disposable-disk guard identifies the harness's disk without relying on a serial number."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import vm_guard
from tests.vm import guard_fixtures
from tests.vm.helpers import PUBLIC_KEY, load_vm_module

vm = load_vm_module()


def spec(*, serial="GENTOO-VM-DISPOSABLE", bus="virtio", blank=True, extra=0, nonce="cafe", removable=None, sector=512):
    facts = vm_guard.BUS_FACTS[bus]
    common = dict(model=facts["model"], tran=facts["tran"], pci_vendors=list(facts["pci_vendors"]), log_sec=sector)
    sizes = vm_guard.unusual_sizes([40 * 1024] + [256] * extra, 11)
    disks = [vm_guard.DiskExpectation(role="target", size=sizes[0], serial=serial, blank=blank, removable=removable, **common)]
    disks += [vm_guard.DiskExpectation(role=f"decoy-{i}", size=sizes[i], serial=f"GENTOO-VM-EXTRA-{i}", **common)
              for i in range(1, extra + 1)]
    return vm_guard.GuardSpec(disks, nonce)


def test_sizes_are_unusual_distinct_and_whole_mebibytes():
    sizes = vm_guard.unusual_sizes([40 * 1024, 256, 256], salt=5)
    assert len(set(sizes)) == 3
    assert all(size % vm_guard.MIB == 0 for size in sizes)  # valid for 4096-byte sectors too
    assert sizes[0] > 40 * vm_guard.GIB and sizes[0] % vm_guard.GIB != 0  # no real disk has this size
    assert sizes[1] != sizes[2]
    assert vm_guard.unusual_sizes([40 * 1024], 5) == vm_guard.unusual_sizes([40 * 1024], 5)
    assert vm_guard.unusual_sizes([40 * 1024], 5) != vm_guard.unusual_sizes([40 * 1024], 6)


@pytest.mark.parametrize("bus", sorted(vm_guard.BUS_FACTS))
def test_the_harness_disk_on_every_bus_is_accepted(bus):
    expectation = spec(bus=bus, serial="GENTOO-VM-DISPOSABLE")
    report = guard_fixtures.fabricated_report(expectation, bus=bus)
    assert report["ok"], report["problems"]
    assert report["target"]["size"] == expectation.disks[0].size
    assert report["unlisted"] == []  # the CD-ROM and the loop device are not "other disks"


def test_a_disk_without_serial_is_identified_by_size_model_vendor_and_nonce():
    expectation = spec(serial="")
    report = guard_fixtures.fabricated_report(expectation)
    assert report["ok"], report["problems"]
    assert report["target"]["serial"] == ""
    assert "pci-0000:00:07.0" in report["target"]["by_path"] and report["nonce_ok"] is True


@pytest.mark.parametrize(("mutation", "fragment"), [
    ("wrong_size", "No disk with the exact size"),
    ("extra_disk", "Unexpected disk(s) present"),
    ("second_same_size", "2 disks have the exact size"),
    ("wrong_serial", "has serial 'SOME-REAL-DISK'"),
    ("wrong_model", "has model"),
    ("wrong_vendor", "hangs off PCI vendor 0x8086"),
    ("not_blank", "is not blank"),
    ("no_nonce", "not the VM the harness started"),
    ("read_only", "is read-only"),
    ("mounted", "is mounted"),
])
def test_the_guard_refuses_anything_that_is_not_exactly_the_harness_disk(mutation, fragment):
    report = guard_fixtures.fabricated_report(spec(), mutate=mutation)
    assert not report["ok"]
    assert any(fragment in problem for problem in report["problems"]), report["problems"]


def test_a_real_disk_with_the_right_serial_but_the_wrong_size_is_refused():
    """The old guard trusted the serial alone; a spoofed or reused serial must not be enough."""
    report = guard_fixtures.fabricated_report(spec(), mutate="wrong_size")
    assert not report["ok"] and report["target"] is None


def test_a_floppy_drive_and_ram_disks_are_not_other_disks():
    """The i440FX machine ('pc') has an fd0 the guest lists as a disk; zram and loop devices appear on the live system."""
    expectation = spec()
    devices = guard_fixtures.lsblk_tree(expectation)
    for name, size in (("fd0", 4096), ("zram0", 8 << 30), ("ram0", 64 << 20), ("nbd0", 0)):
        devices.append({"name": name, "kname": name, "path": f"/dev/{name}", "type": "disk", "size": size, "mountpoints": [None]})
    report = vm_guard.evaluate(expectation, devices, dmi_strings=[f"{vm_guard.NONCE_KEY}=cafe"])
    assert report["ok"], report["problems"]


def test_each_extra_disk_is_guarded_on_its_own():
    expectation = spec(extra=2)
    ok = guard_fixtures.fabricated_report(expectation)
    assert ok["ok"] and set(ok["disks"]) == {"target", "decoy-1", "decoy-2"}
    assert len({disk["path"] for disk in ok["disks"].values()}) == 3
    bad = guard_fixtures.fabricated_report(expectation, mutate="extra_disk")
    assert not bad["ok"]  # a fourth disk nobody asked for
    allowed = vm_guard.GuardSpec(expectation.disks, expectation.nonce, allow_unlisted=True)
    assert guard_fixtures.fabricated_report(allowed, mutate="extra_disk")["ok"]


def test_blank_expectations_follow_the_install_state():
    assert guard_fixtures.fabricated_report(spec(blank=True), installed=False)["ok"]
    assert not guard_fixtures.fabricated_report(spec(blank=True), installed=True)["ok"]
    assert guard_fixtures.fabricated_report(spec(blank=False), installed=True)["ok"]
    assert not guard_fixtures.fabricated_report(spec(blank=False), installed=False)["ok"]
    assert guard_fixtures.fabricated_report(spec(blank=None), installed=True)["ok"]
    assert guard_fixtures.fabricated_report(spec(blank=True).with_blank("target", None), installed=True)["ok"]


def test_4kn_and_removable_flags_are_checked():
    ok = guard_fixtures.fabricated_report(spec(bus="usb", removable=True, sector=4096), bus="usb")
    assert ok["ok"], ok["problems"]
    wrong_sector = spec(sector=4096)
    assert not guard_fixtures.fabricated_report(wrong_sector, sector=512)["ok"]


def test_nonce_is_unknown_not_failed_when_the_guest_cannot_read_dmi():
    expectation = spec()
    devices = guard_fixtures.lsblk_tree(expectation)
    report = vm_guard.evaluate(expectation, devices, dmi_strings=None)
    assert report["ok"] and report["nonce_ok"] is None  # a non-root caller (the GUI driver) cannot read it


def test_dmi_oem_strings_are_parsed_from_the_raw_record():
    record = bytes([11, 5, 0x0B, 0x00, 2]) + b"org.gentoo-installer.vm-nonce=cafe\0other\0\0"
    assert vm_guard.parse_dmi_strings(record) == ["org.gentoo-installer.vm-nonce=cafe", "other"]
    assert vm_guard.parse_dmi_strings(b"") == []


def dmi_record(kind: int, strings: list[str], handle: int = 0x100) -> bytes:
    body = b"".join(item.encode() + b"\0" for item in strings) or b"\0"
    return bytes([kind, 5]) + handle.to_bytes(2, "little") + bytes([len(strings)]) + body + b"\0"


def test_oem_strings_are_found_in_the_raw_dmi_table():
    table = (dmi_record(1, ["QEMU", "Standard PC"]) + dmi_record(3, ["LENOVO"]) +
             dmi_record(11, [f"{vm_guard.NONCE_KEY}=cafe", "other"]) + dmi_record(127, []))
    assert vm_guard.parse_dmi_table(table) == [f"{vm_guard.NONCE_KEY}=cafe", "other"]
    assert vm_guard.parse_dmi_table(b"") == [] and vm_guard.parse_dmi_table(b"\x0b\x02") == []  # truncated: no crash


def test_the_nonce_is_read_from_the_raw_table_when_dmi_sysfs_is_not_available(tmp_path):
    expectation = spec(extra=0)
    devices = guard_fixtures.lsblk_tree(expectation)
    sysfs, dev = tmp_path / "sys", tmp_path / "dev"
    make_sysfs(sysfs, "vda", "0x1af4")  # no firmware/dmi/entries
    table = sysfs / "firmware" / "dmi" / "tables"
    table.mkdir(parents=True)
    (table / "DMI").write_bytes(dmi_record(11, [f"{vm_guard.NONCE_KEY}=cafe"]) + dmi_record(127, []))
    (dev / "disk" / "by-path").mkdir(parents=True)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, json.dumps({"blockdevices": devices}) if argv[0] == "lsblk" else "", "")

    report = vm_guard.collect(expectation, sysfs=str(sysfs), dev=str(dev), run=fake_run)
    assert report["ok"] and report["nonce_ok"] is True


def test_spec_round_trips_through_json():
    original = spec(extra=1)
    assert vm_guard.GuardSpec.from_json(original.to_json()) == original


def test_guest_command_ships_the_guard_source_and_the_spec():
    expectation = spec()
    command = vm_guard.guest_command(expectation)
    assert command[:2] == ["python3", "-c"] and "def evaluate(" in command[2]
    assert vm_guard.GuardSpec.from_json(command[3]) == expectation
    compile(command[2], "<guard>", "exec")  # the guest must be able to run it


def test_the_report_is_found_in_noisy_ssh_output():
    output = "motd banner\n" + json.dumps({"type": "vm_disk_guard", "ok": True}) + "\n"
    assert vm_guard.parse_report(0, output)["ok"] is True
    with pytest.raises(ValueError, match="no disk-guard report"):
        vm_guard.parse_report(1, "Traceback ...\n")


# --- the guest-side collector, against a fake lsblk and a fake sysfs ---------------------------------

def make_sysfs(root: Path, kname: str, vendor: str, dmi: bytes | None = None) -> None:
    pci = root / "devices" / "pci0000:00" / "0000:00:07.0"
    block = pci / "virtio2" / "block" / kname
    block.mkdir(parents=True)
    (pci / "vendor").write_text(vendor + "\n")
    (root / "class" / "block").mkdir(parents=True)
    (root / "class" / "block" / kname).symlink_to(block)
    (block / "device").symlink_to(pci / "virtio2")
    if dmi is not None:
        entry = root / "firmware" / "dmi" / "entries" / "11-0"
        entry.mkdir(parents=True)
        (entry / "raw").write_bytes(dmi)


def test_the_pci_vendor_of_a_sata_disk_is_the_controller_not_the_scsi_inquiry_string(tmp_path):
    """sda's sysfs path runs through a SCSI device whose 'vendor' file says 'ATA     ': only a 0x.... id is a PCI vendor."""
    sysfs = tmp_path / "sys"
    pci = sysfs / "devices" / "pci0000:00" / "0000:00:1f.2"
    scsi = pci / "ata1" / "host0" / "target0:0:0" / "0:0:0:0"
    block = scsi / "block" / "sda"
    block.mkdir(parents=True)
    (pci / "vendor").write_text("0x8086\n")
    (scsi / "vendor").write_text("ATA     \n")
    (sysfs / "class" / "block").mkdir(parents=True)
    (sysfs / "class" / "block" / "sda").symlink_to(block)
    (block / "device").symlink_to(scsi)
    assert vm_guard._pci_vendor(str(sysfs), "sda") == "0x8086"
    (pci / "vendor").unlink()  # no PCI ancestor with an id at all: unknown, not 'ATA'
    assert vm_guard._pci_vendor(str(sysfs), "sda") == ""


def test_collect_reads_lsblk_sysfs_dmi_and_by_path(tmp_path):
    expectation = spec(extra=0)
    devices = guard_fixtures.lsblk_tree(expectation)
    sysfs, dev = tmp_path / "sys", tmp_path / "dev"
    make_sysfs(sysfs, "vda", "0x1af4", bytes([11, 5, 0, 0, 1]) + f"{vm_guard.NONCE_KEY}=cafe".encode() + b"\0\0")
    (dev / "disk" / "by-path").mkdir(parents=True)
    (dev / "vda").write_text("")
    (dev / "disk" / "by-path" / "pci-0000:00:07.0").symlink_to(dev / "vda")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv[0])
        if argv[0] == "lsblk":
            return subprocess.CompletedProcess(argv, 0, json.dumps({"blockdevices": devices}), "")
        return subprocess.CompletedProcess(argv, 0, "", "")  # wipefs finds no signature

    report = vm_guard.collect(expectation, sysfs=str(sysfs), dev=str(dev), run=fake_run)
    assert report["ok"], report["problems"]
    assert report["target"]["pci_vendor"] == "0x1af4"
    assert report["target"]["by_path"] == ["pci-0000:00:07.0"]
    assert report["nonce_ok"] is True
    assert report["target"]["has_signatures"] is False
    assert calls == ["lsblk", "wipefs"]  # nothing that writes is ever run


def test_collect_reports_a_failing_lsblk():
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "lsblk: not found")

    report = vm_guard.collect(spec(), run=fake_run)
    assert not report["ok"] and "lsblk failed" in report["problems"][0]


# --- how the runner uses it --------------------------------------------------------------------------

def runner_for(tmp_path, scenario, nonce="beef0001"):
    runner = object.__new__(vm.ScenarioRunner)
    runner.args = SimpleNamespace(ssh_user="root", command_timeout=10, disk_size=40)
    runner.scenario = scenario
    runner.run_dir = tmp_path
    runner.nonce = nonce
    return runner


def answer(runner, **options):
    def fake_ssh(user, remote, log_name, timeout, **kwargs):
        log = runner.run_dir / log_name
        log.write_text(guard_fixtures.answer_guard(remote, **options))
        return 0, log
    runner.ssh = fake_ssh


@pytest.mark.parametrize(("scenario", "bus", "device"), [
    ("openrc", "virtio", "/dev/vda"), ("nvme-uefi", "nvme", "/dev/nvme0n1"), ("sata-bios", "sata", "/dev/sda"),
    ("usb-portable", "usb", "/dev/sda"),
])
def test_the_runner_follows_the_disk_the_guard_found_instead_of_assuming_vda(tmp_path, scenario, bus, device):
    runner = runner_for(tmp_path, vm.SCENARIO_MAP[scenario])
    answer(runner, bus=bus)
    report = runner.run_guard("guard.log", blank=True)
    assert runner.target_device == device == report["target"]["path"]
    assert json.loads((tmp_path / "guard.json").read_text())["ok"]
    assert runner.disk_identity() == (device, vm.TARGET_SERIAL, "")


def test_a_disk_without_serial_is_approved_by_its_by_path_identity(tmp_path):
    runner = runner_for(tmp_path, vm.SCENARIO_MAP["serial-less"])
    answer(runner)
    runner.run_guard("guard.log", blank=True)
    device, device_id, kind = runner.disk_identity()
    size = runner.disk_sizes()[0]
    assert (device, kind) == ("/dev/vda", "path")
    assert device_id == f"pci-0000:00:07.0|{size}|"
    manifest = vm.build_manifest(runner.scenario, PUBLIC_KEY, device=device, device_id=device_id, device_id_kind=kind)
    assert manifest.validate() == []
    assert manifest.storage.device_id_kind == "path" and manifest.storage.device_id == device_id


def test_the_runner_refuses_when_the_guard_does_(tmp_path):
    runner = runner_for(tmp_path, vm.SCENARIO_MAP["openrc"])
    answer(runner, mutate="extra_disk")
    with pytest.raises(vm.HarnessError, match="Unexpected disk"):
        runner.run_guard("guard.log", blank=True)
    assert runner.target_device == vm.TARGET_DEVICE  # nothing was resolved from a refused guest


def test_no_report_means_no_install(tmp_path):
    runner = runner_for(tmp_path, vm.SCENARIO_MAP["openrc"])
    runner.ssh = lambda user, remote, log_name, timeout, **kwargs: (255, runner.run_dir / log_name)
    with pytest.raises(vm.HarnessError, match="does not expose the expected disposable disk"):
        runner.run_guard("guard.log", blank=True)


def test_every_guarded_step_names_the_exact_sizes_it_created(tmp_path):
    runner = runner_for(tmp_path, vm.SCENARIO_MAP["decoy-disk"])
    sizes = runner.disk_sizes()
    assert len(sizes) == 2 and sizes[0] > 40 * vm_guard.GIB and sizes[1] > 256 * vm_guard.MIB
    guard = runner.guard_spec(blank=True)
    assert [d.size for d in guard.disks] == sizes and [d.role for d in guard.disks] == ["target", "decoy-1"]
    assert guard.nonce == "beef0001"
    assert runner.disk_sizes() == sizes  # stable for the whole run
