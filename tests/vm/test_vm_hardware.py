"""TD-N3 / ISO-NEW-5: the QEMU machine shape. Defaults keep the original command line; every option is
checked as an argument list (nothing here starts QEMU)."""
from __future__ import annotations

import argparse
import struct
from pathlib import Path

import pytest

import vm_hw
from gentoo_installer.manifest import InstallManifest
from tests.vm.helpers import PUBLIC_KEY, load_vm_module

vm = load_vm_module()

# The command lines of the original, single-shape harness (captured from the commit before the
# hardware options existed). The tools in build/ and the operators' habits depend on them.
GOLDEN = {
    'openrc-live': ['qemu-system-x86_64', '-name', 'gentoo-installer-openrc', '-machine', 'q35,accel=kvm:tcg', '-m', '4096', '-smp', '2', '-drive', 'file=RUN/disk.qcow2,if=none,id=target,format=qcow2', '-device', 'virtio-blk-pci,drive=target,serial=GENTOO-VM-DISPOSABLE', '-drive', 'file=ISO,media=cdrom,readonly=on,format=raw', '-drive', 'if=pflash,format=raw,readonly=on,file=CODE', '-drive', 'if=pflash,format=raw,file=RUN/OVMF_VARS.fd', '-boot', 'order=d,strict=on', '-netdev', 'user,id=net0,hostfwd=tcp:127.0.0.1:40222-:22', '-device', 'virtio-net-pci,netdev=net0', '-device', 'virtio-rng-pci', '-vga', 'virtio', '-display', 'none', '-monitor', 'none', '-chardev', 'socket,id=serial0,path=RUN/serial.sock,server=on,wait=off', '-serial', 'chardev:serial0', '-qmp', 'unix:RUN/qmp.sock,server=on,wait=off', '-no-reboot'],
    'openrc-target': ['qemu-system-x86_64', '-name', 'gentoo-installer-openrc', '-machine', 'q35,accel=kvm:tcg', '-m', '4096', '-smp', '2', '-drive', 'file=RUN/disk.qcow2,if=none,id=target,format=qcow2', '-device', 'virtio-blk-pci,drive=target,serial=GENTOO-VM-DISPOSABLE', '-drive', 'if=pflash,format=raw,readonly=on,file=CODE', '-drive', 'if=pflash,format=raw,file=RUN/OVMF_VARS.fd', '-boot', 'order=c,strict=on', '-netdev', 'user,id=net0,hostfwd=tcp:127.0.0.1:40222-:22', '-device', 'virtio-net-pci,netdev=net0', '-device', 'virtio-rng-pci', '-vga', 'virtio', '-display', 'none', '-monitor', 'none', '-chardev', 'socket,id=serial0,path=RUN/serial.sock,server=on,wait=off', '-serial', 'chardev:serial0', '-qmp', 'unix:RUN/qmp.sock,server=on,wait=off', '-no-reboot'],
    'bios-live': ['qemu-system-x86_64', '-name', 'gentoo-installer-bios', '-machine', 'q35,accel=kvm:tcg', '-m', '4096', '-smp', '2', '-drive', 'file=RUN/disk.qcow2,if=none,id=target,format=qcow2', '-device', 'virtio-blk-pci,drive=target,serial=GENTOO-VM-DISPOSABLE', '-drive', 'file=ISO,media=cdrom,readonly=on,format=raw', '-boot', 'order=d,strict=on', '-netdev', 'user,id=net0,hostfwd=tcp:127.0.0.1:40222-:22', '-device', 'virtio-net-pci,netdev=net0', '-device', 'virtio-rng-pci', '-vga', 'virtio', '-display', 'none', '-monitor', 'none', '-chardev', 'socket,id=serial0,path=RUN/serial.sock,server=on,wait=off', '-serial', 'chardev:serial0', '-qmp', 'unix:RUN/qmp.sock,server=on,wait=off', '-no-reboot'],
    'bios-target': ['qemu-system-x86_64', '-name', 'gentoo-installer-bios', '-machine', 'q35,accel=kvm:tcg', '-m', '4096', '-smp', '2', '-drive', 'file=RUN/disk.qcow2,if=none,id=target,format=qcow2', '-device', 'virtio-blk-pci,drive=target,serial=GENTOO-VM-DISPOSABLE', '-boot', 'order=c,strict=on', '-netdev', 'user,id=net0,hostfwd=tcp:127.0.0.1:40222-:22', '-device', 'virtio-net-pci,netdev=net0', '-device', 'virtio-rng-pci', '-vga', 'virtio', '-display', 'none', '-monitor', 'none', '-chardev', 'socket,id=serial0,path=RUN/serial.sock,server=on,wait=off', '-serial', 'chardev:serial0', '-qmp', 'unix:RUN/qmp.sock,server=on,wait=off', '-no-reboot'],
}


@pytest.fixture
def paths(sock_dir, monkeypatch):
    """A run directory short enough for its sockets to appear by absolute path in the command line."""
    root = sock_dir / "build" / "vm"
    run = root / "run"
    run.mkdir(parents=True)
    iso = sock_dir / "installer.iso"
    iso.write_bytes(b"iso")
    code, variables = sock_dir / "code.fd", sock_dir / "vars.fd"
    code.write_bytes(b"c")
    variables.write_bytes(b"v")
    monkeypatch.setattr(vm, "VM_ROOT", root)
    return {"iso": iso, "disk": run / "disk.qcow2", "run": run, "firmware": vm.OvmfFirmware(code, variables), "code": code}


def command(paths, scenario="openrc", *, live=True, hardware=None, **kwargs):
    return vm.qemu_command(
        vm.SCENARIO_MAP[scenario] if isinstance(scenario, str) else scenario,
        iso=paths["iso"], disk=paths["disk"], run_dir=paths["run"], live=live,
        ssh_host="127.0.0.1", ssh_port=40222, memory_mib=4096, cpus=2, firmware=paths["firmware"],
        hardware=hardware, **kwargs)


def normalized(argv, paths):
    return [a.replace(str(paths["run"]), "RUN").replace(str(paths["iso"]), "ISO").replace(str(paths["code"]), "CODE") for a in argv]


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_default_command_line_is_exactly_the_original(paths, key):
    scenario, phase = key.split("-")
    argv = command(paths, scenario, live=phase == "live")
    assert normalized(argv, paths) == GOLDEN[key]


def args_after(argv, flag):
    return [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == flag]


@pytest.mark.parametrize(("bus", "expected"), [
    ("virtio", ["virtio-blk-pci,drive=target,serial=GENTOO-VM-DISPOSABLE"]),
    ("sata", ["ich9-ahci,id=ahci0", "ide-hd,bus=ahci0.0,drive=target,serial=GENTOO-VM-DISPOSABLE"]),
    ("scsi", ["virtio-scsi-pci,id=scsi0", "scsi-hd,bus=scsi0.0,drive=target,serial=GENTOO-VM-DISPOSABLE"]),
    ("nvme", ["nvme,drive=target,serial=GENTOO-VM-DISPOSABLE"]),
    ("usb", ["qemu-xhci,id=xhci0", "usb-storage,bus=xhci0.0,drive=target,serial=GENTOO-VM-DISPOSABLE"]),
])
def test_every_disk_bus_attaches_the_target_with_its_serial(paths, bus, expected):
    argv = command(paths, hardware=vm_hw.VMHardware(disk_bus=bus))
    devices = args_after(argv, "-device")
    assert [d for d in devices if d in expected or d.startswith(("virtio-blk", "ide-hd", "scsi-hd", "usb-storage", "nvme"))] == expected
    assert f"file={paths['disk']},if=none,id=target,format=qcow2" in args_after(argv, "-drive")


def test_4kn_sectors_reach_every_bus_that_supports_them(paths):
    for bus in (bus for bus in vm_hw.DISK_BUSES if bus != "sata"):
        argv = command(paths, hardware=vm_hw.VMHardware(disk_bus=bus, sector_size=4096))
        joined = " ".join(argv)
        assert "logical_block_size=4096,physical_block_size=4096" in joined, bus
    nvme = args_after(command(paths, hardware=vm_hw.VMHardware(disk_bus="nvme", sector_size=4096)), "-device")
    assert "nvme-ns,drive=target,bus=nvme0,nsid=1,logical_block_size=4096,physical_block_size=4096" in nvme
    assert "nvme,id=nvme0,serial=GENTOO-VM-DISPOSABLE" in nvme


def test_4kn_sata_is_refused_because_qemu_cannot_create_it(paths):
    with pytest.raises(vm.HarnessError, match="only have 512-byte logical sectors"):
        command(paths, hardware=vm_hw.VMHardware(disk_bus="sata", sector_size=4096))


def test_a_disk_without_serial_leaves_the_property_out(paths):
    argv = command(paths, hardware=vm_hw.VMHardware(disk_serial=False))
    assert "virtio-blk-pci,drive=target" in args_after(argv, "-device")
    assert "GENTOO-VM-DISPOSABLE" not in " ".join(argv)


def test_usb_target_can_be_removable(paths):
    devices = args_after(command(paths, hardware=vm_hw.VMHardware(disk_bus="usb", removable=True)), "-device")
    assert "usb-storage,bus=xhci0.0,drive=target,serial=GENTOO-VM-DISPOSABLE,removable=on" in devices


@pytest.mark.parametrize(("hardware", "message"), [
    (vm_hw.VMHardware(disk_bus="nvme", disk_serial=False), "only supported on the virtio bus"),
    (vm_hw.VMHardware(disk_bus="floppy"), "Unknown disk bus"),
    (vm_hw.VMHardware(sector_size=1024), "512 or 4096"),
    (vm_hw.VMHardware(removable=True), "needs --disk-bus usb"),
    (vm_hw.VMHardware(cpu="host;rm -rf /"), "Invalid --cpu"),
    (vm_hw.VMHardware(extra_disks=(8,)), "between 64 MiB and 2 TiB"),
    (vm_hw.VMHardware(nic="ne2k"), "Unknown NIC"),
    (vm_hw.VMHardware(smbios="mainframe"), "Unknown SMBIOS preset"),
])
def test_impossible_machines_are_refused_before_qemu_starts(paths, hardware, message):
    with pytest.raises(vm.HarnessError, match=message):
        command(paths, hardware=hardware)


@pytest.mark.parametrize(("nic", "device"), [("virtio", "virtio-net-pci,netdev=net0"), ("e1000e", "e1000e,netdev=net0"),
                                             ("rtl8139", "rtl8139,netdev=net0")])
def test_network_card_models(paths, nic, device):
    argv = command(paths, hardware=vm_hw.VMHardware(nic=nic))
    assert device in args_after(argv, "-device")
    assert "user,id=net0,hostfwd=tcp:127.0.0.1:40222-:22" in args_after(argv, "-netdev")


@pytest.mark.parametrize("vga", vm_hw.VGAS)
def test_display_adapters(paths, vga):
    assert args_after(command(paths, hardware=vm_hw.VMHardware(vga=vga)), "-vga") == [vga]


def test_cpu_machine_and_clock_options(paths):
    argv = command(paths, hardware=vm_hw.VMHardware(cpu="Haswell", machine="pc", rtc="localtime"))
    assert args_after(argv, "-machine") == ["pc,accel=kvm:tcg"]
    assert args_after(argv, "-cpu") == ["Haswell"]
    assert args_after(argv, "-rtc") == ["base=localtime"]
    assert "-cpu" not in command(paths) and "-rtc" not in command(paths)


def test_extra_disks_are_raw_files_in_the_run_directory_with_their_own_serials(paths):
    argv = command(paths, hardware=vm_hw.VMHardware(extra_disks=(256, 1024)))
    drives = args_after(argv, "-drive")
    assert f"file={paths['run'] / 'extra-1.img'},if=none,id=extra1,format=raw" in drives
    assert f"file={paths['run'] / 'extra-2.img'},if=none,id=extra2,format=raw" in drives
    devices = args_after(argv, "-device")
    assert "virtio-blk-pci,drive=extra1,serial=GENTOO-VM-EXTRA-1" in devices
    assert "virtio-blk-pci,drive=extra2,serial=GENTOO-VM-EXTRA-2" in devices
    sata = args_after(command(paths, hardware=vm_hw.VMHardware(disk_bus="sata", extra_disks=(256,))), "-device")
    assert "ide-hd,bus=ahci1.0,drive=extra1,serial=GENTOO-VM-EXTRA-1" in sata and "ich9-ahci,id=ahci1" in sata


@pytest.mark.parametrize("options", [
    {}, {"disk_bus": "nvme"}, {"disk_bus": "sata", "extra_disks": (128,)}, {"disk_bus": "usb", "removable": True},
    {"smbios": "laptop-lenovo"}, {"secure_boot": True}, {"disk_bus": "scsi", "sector_size": 4096},
])
def test_no_hardware_option_ever_names_a_host_device(paths, options):
    argv = command(paths, hardware=vm_hw.VMHardware(**options))
    assert not [a for a in argv if "/dev/" in a], argv
    for index, value in enumerate(argv):
        if value == "-drive" and "file=" in argv[index + 1] and "if=pflash" not in argv[index + 1]:
            path = Path(argv[index + 1].split("file=", 1)[1].split(",", 1)[0])
            assert paths["run"] in path.parents or path == paths["iso"], path  # only run-directory files and the ISO


def test_the_cd_can_win_the_boot_order_over_an_installed_disk(paths):
    argv = command(paths, cd_first=True)
    assert f"file={paths['iso']},if=none,id=cd0,media=cdrom,readonly=on,format=raw" in args_after(argv, "-drive")
    assert "ide-cd,drive=cd0,bootindex=0" in args_after(argv, "-device")
    assert not any(a.startswith(f"file={paths['iso']},media=cdrom") for a in args_after(argv, "-drive"))
    assert not any("cdrom" in a for a in args_after(command(paths, live=False, cd_first=True), "-drive"))


# --- SMBIOS identity ------------------------------------------------------------------------------

def test_vmware_strings_escape_the_comma_in_the_vendor_name(paths):
    argv = command(paths, hardware=vm_hw.VMHardware(smbios="vmware"))
    smbios = args_after(argv, "-smbios")
    assert "type=1,manufacturer=VMware,, Inc.,product=VMware Virtual Platform,version=None" in smbios
    assert "type=0,vendor=Phoenix Technologies LTD" in smbios


def test_a_laptop_preset_supplies_a_notebook_chassis_through_a_file(paths):
    argv = command(paths, hardware=vm_hw.VMHardware(smbios="laptop-lenovo"), nonce="abc123")
    smbios = args_after(argv, "-smbios")
    assert "type=1,manufacturer=LENOVO,product=20XW0055GE,version=ThinkPad X1 Carbon Gen 9" in smbios
    chassis = paths["run"] / "smbios-chassis.bin"
    assert f"file={chassis}" in smbios and "type=11,value=org.gentoo-installer.vm-nonce=abc123" in smbios
    data = chassis.read_bytes()
    assert data[0] == 3 and data[1] == 0x16 and data[5] == 10  # type 3, formatted length, chassis type 10 = notebook
    strings = data[data[1]:].split(b"\0")
    assert strings[0] == b"LENOVO" and data.endswith(b"\0\0")


def test_chassis_blob_string_indexes_follow_the_strings_present():
    blob = vm_hw.chassis_blob(31, "HP", version="", serial="42")
    assert (blob[4], blob[5], blob[6], blob[7], blob[8]) == (1, 31, 0, 2, 0)
    assert blob[blob[1]:] == b"HP\x0042\x00\x00"
    assert struct.unpack_from("<BBH", blob) == (3, 0x16, 0x0300)


def test_the_nonce_is_added_only_when_given(paths):
    assert "type=11" not in " ".join(command(paths))
    assert "type=11,value=org.gentoo-installer.vm-nonce=feed" in args_after(command(paths, nonce="feed"), "-smbios")


# --- firmware -------------------------------------------------------------------------------------

def test_secure_boot_needs_smm_and_secure_flash(paths):
    argv = command(paths, hardware=vm_hw.VMHardware(secure_boot=True))
    assert args_after(argv, "-machine") == ["q35,smm=on,accel=kvm:tcg"]
    assert "driver=cfi.pflash01,property=secure,value=on" in args_after(argv, "-global")


def test_a_readonly_variable_store_is_mapped_readonly(paths):
    argv = command(paths, hardware=vm_hw.VMHardware(ovmf_vars="readonly"))
    assert f"if=pflash,format=raw,readonly=on,file={paths['run'] / 'OVMF_VARS.fd'}" in args_after(argv, "-drive")


def test_the_variable_store_can_be_seeded_from_a_template(paths, tmp_path):
    template = tmp_path / "enrolled.fd"
    template.write_bytes(b"enrolled keys")
    command(paths, vars_template=template)
    assert (paths["run"] / "OVMF_VARS.fd").read_bytes() == b"enrolled keys"


def test_secure_boot_firmware_pairs_are_searched_separately():
    plain = {code.name for code, _ in vm.ovmf_candidates()}
    secure = {code.name for code, _ in vm.ovmf_candidates(secure=True)}
    assert "OVMF_CODE.fd" in plain and "OVMF_CODE.secboot.fd" in secure and not plain & secure
    assert any(str(code).startswith("/usr/share/edk2-ovmf") for code, _ in vm.ovmf_candidates())


# --- command line overrides -----------------------------------------------------------------------

def parse(*arguments):
    return vm.build_parser().parse_args(["--scenario", "openrc", "--iso", "x.iso", *arguments])


def test_options_left_out_keep_the_scenarios_own_machine():
    scenario = vm.SCENARIO_MAP["nvme-uefi"]
    assert vm.apply_overrides(scenario, parse()) == scenario


def test_options_override_the_scenarios_machine_and_firmware():
    args = parse("--disk-bus", "sata", "--no-disk-serial", "--sector-size", "4096", "--nic", "rtl8139", "--vga", "std",
                 "--cpu", "EPYC", "--smbios", "desktop-asus", "--extra-disk", "256M", "--extra-disk", "2G",
                 "--firmware", "bios", "--ovmf-vars", "readonly", "--rtc", "localtime", "--machine", "pc")
    scenario = vm.apply_overrides(vm.SCENARIO_MAP["openrc"], args)
    hardware = scenario.hardware
    assert (hardware.disk_bus, hardware.disk_serial, hardware.sector_size, hardware.nic, hardware.vga) == ("sata", False, 4096, "rtl8139", "std")
    assert (hardware.cpu, hardware.smbios, hardware.extra_disks, hardware.ovmf_vars, hardware.rtc, hardware.machine) == (
        "EPYC", "desktop-asus", (256, 2048), "readonly", "localtime", "pc")
    assert scenario.firmware == "bios" and scenario.resolved_table == "dos"
    problems = vm.hardware_problems(scenario, "cli")
    assert any("only supported on the virtio bus" in problem for problem in problems)
    assert any("--ovmf-vars only applies to UEFI" in problem for problem in problems)


@pytest.mark.parametrize(("value", "mib"), [("256M", 256), ("2G", 2048), ("3", 3072), ("512m", 512)])
def test_extra_disk_sizes(value, mib):
    assert vm.disk_size_mib(value) == mib


def test_extra_disk_size_must_be_a_size():
    with pytest.raises(argparse.ArgumentTypeError):
        vm.disk_size_mib("big")


def test_a_headless_machine_cannot_run_the_graphical_installer_or_a_keystroke_unlock():
    headless = vm.apply_overrides(vm.SCENARIO_MAP["openrc"], parse("--vga", "none"))
    assert vm.hardware_problems(headless, "cli") == []
    assert any("graphical installer" in p for p in vm.hardware_problems(headless, "gui"))
    luks = vm.apply_overrides(vm.SCENARIO_MAP["uefi-luks"], parse("--vga", "none"))
    assert any("keystroke LUKS unlock" in p for p in vm.hardware_problems(luks, "cli"))


def test_secure_boot_is_a_uefi_feature():
    scenario = vm.apply_overrides(vm.SCENARIO_MAP["bios"], parse("--secure-boot"))
    assert any("needs UEFI" in p for p in vm.hardware_problems(scenario, "cli"))


def test_main_rejects_impossible_combinations_before_touching_anything(tmp_path, capsys):
    with pytest.raises(SystemExit) as raised:
        vm.main(["--scenario", "openrc", "--iso", str(tmp_path / "x.iso"), "--disk-bus", "nvme", "--no-disk-serial"])
    assert raised.value.code == 2
    assert "only supported on the virtio bus" in capsys.readouterr().err


# --- the catalogue --------------------------------------------------------------------------------

def test_all_still_runs_exactly_the_four_validated_scenarios():
    assert vm.ALL_SCENARIOS == ("openrc", "systemd", "uefi-luks", "bios")
    assert set(vm.SCENARIOS) == set(vm.SCENARIO_MAP) and len(vm.SCENARIOS) > 8


@pytest.mark.parametrize("name", sorted(vm.SCENARIO_MAP))
def test_every_scenario_is_a_valid_machine_and_a_valid_manifest(name):
    scenario = vm.SCENARIO_MAP[name]
    assert scenario.hardware.problems() == [] and vm.hardware_problems(scenario, "cli") == []
    manifest = vm.build_manifest(scenario, PUBLIC_KEY)
    assert manifest.validate() == []
    assert InstallManifest.from_json(manifest.to_json()).to_dict() == manifest.to_dict()
    assert manifest.accounts.hostname == f"gentoo-vm-{name}"
    if not scenario.defaults:
        assert manifest.storage.firmware == scenario.firmware and manifest.storage.table == scenario.resolved_table
        assert manifest.storage.filesystem == scenario.filesystem and manifest.system.bootloader == scenario.bootloader
        assert manifest.hardware.target == scenario.hw_target and manifest.hardware.guest_tools == scenario.guest_tools


def test_the_only_rescue_scenario_installs_a_gentoo_first_and_breaks_it_three_ways():
    """Only Gentoo is repaired, so there is no scenario that boots another distribution's image."""
    scenario = vm.SCENARIO_MAP["rescue-gentoo"]
    assert scenario.repair == "cli" and not scenario.in_all and scenario.breaks == ("grub-cfg", "initramfs", "fstab-uuid")
    assert all(mode in vm.vm_rescue.BREAK_MODES for mode in scenario.breaks)
    assert sorted(name for name in vm.SCENARIO_MAP if name.startswith("rescue-")) == ["rescue-gentoo"]
    assert not hasattr(scenario, "image") and not hasattr(vm.vm_rescue, "IMAGES")


def test_the_validated_scenarios_stay_free_of_hypervisor_tooling_and_fixed_by_hand():
    for name in vm.ALL_SCENARIOS:
        manifest = vm.build_manifest(vm.SCENARIO_MAP[name], PUBLIC_KEY)
        assert manifest.hardware.guest_tools == "none" and manifest.portage.make_opts == "-j2"
        assert manifest.hardware.target == "this" and manifest.storage.device_id_kind == ""


def test_new_scenarios_exercise_what_their_names_promise():
    scenarios = vm.SCENARIO_MAP
    assert scenarios["bios-gpt"].firmware == "bios" and scenarios["bios-gpt"].resolved_table == "gpt"
    assert scenarios["openrc-luks"].luks and scenarios["openrc-luks"].init == "openrc"
    assert scenarios["uefi-fresh-nvram"].hardware.ovmf_vars == "fresh-on-target"
    assert scenarios["decoy-disk"].decoy and scenarios["decoy-disk"].hardware.extra_disks
    assert scenarios["nvme-uefi"].hardware.disk_bus == "nvme" and scenarios["sata-bios"].hardware.disk_bus == "sata"
    assert not scenarios["serial-less"].hardware.disk_serial
    assert scenarios["uefi-4kn"].hardware.sector_size == 4096
    assert scenarios["usb-portable"].hw_target == "portable" and scenarios["usb-portable"].hardware.removable
    assert scenarios["laptop-smbios"].hardware.smbios.startswith("laptop-")
    assert scenarios["vmware-guest"].hardware.smbios == "vmware"
    assert scenarios["reinstall"].reinstall and scenarios["small-disk"].expect_refusal
    assert not any(scenarios[name].in_all for name in set(scenarios) - {"openrc", "systemd", "uefi-luks", "bios"})


def test_scenario_listing_names_every_scenario(capsys):
    assert vm.main(["--list-scenarios"]) == 0
    listing = capsys.readouterr().out
    assert all(name in listing for name in vm.SCENARIO_MAP) and "nvme" in listing


@pytest.mark.parametrize(("device", "boot", "root"), [("/dev/vda", "/dev/vda1", "/dev/vda2"),
                                                       ("/dev/nvme0n1", "/dev/nvme0n1p1", "/dev/nvme0n1p2")])
def test_the_manual_scenario_names_the_partitions_the_guest_tool_creates(device, boot, root):
    manifest = vm.build_manifest(vm.SCENARIO_MAP["manual-two-disks"], PUBLIC_KEY, device=device)
    assert manifest.validate() == []
    assert manifest.storage.mode == "manual" and manifest.storage.device == device
    assert [(p.device, p.mountpoint) for p in manifest.storage.partitions] == [(boot, "/efi"), (root, "/")]
    bios = vm.apply_overrides(vm.SCENARIO_MAP["manual-two-disks"], vm.build_parser().parse_args(
        ["--scenario", "manual-two-disks", "--iso", "x.iso", "--firmware", "bios"]))
    assert [p.mountpoint for p in vm.build_manifest(bios, PUBLIC_KEY).storage.partitions] == ["/"]
