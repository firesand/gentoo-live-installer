"""The secure-boot-live scenario: the ISO as a USB stick on OVMF with Microsoft's keys, MokManager driven by its serial console text.

The data files are from the first real run (2026-10-27, the RC2 test ISO signed by iso/sign-secure-boot.py): mokmanager-serial.txt is
what OVMF wrote to the serial port while MokManager enrolled the key, live-secure-boot.txt what the live system said afterwards.
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest

import vm_guard
import vm_mok
from tests.vm import guard_fixtures
from tests.vm.helpers import load_vm_module
from tests.vm.test_vm_hardware import command, normalized, paths  # noqa: F401 - paths is a fixture

vm = load_vm_module()
DATA = Path(__file__).parent / "data"
SERIAL = (DATA / "mokmanager-serial.txt").read_bytes().decode(errors="replace")
PROBE = (DATA / "live-secure-boot.txt").read_text()
CERTIFICATE_SHA256 = "cd8ad5a5e9ea0cbd225b6d15a58b733fc4f9266f3f3ec23edbc8b1647cdcd1c4"


def replay(text: str):
    """wait_for and press over a recorded serial log, like SerialChannel.wait_for (case-insensitive, from an offset)."""
    pressed: list[str] = []

    def wait_for(pattern: str, timeout: int, since: int) -> int:
        match = re.compile(pattern, re.IGNORECASE).search(text, since)
        if match is None:
            raise AssertionError(f"the recorded MokManager never showed {pattern!r} after offset {since}")
        return match.end()

    return wait_for, pressed.append, pressed


def test_the_recorded_mokmanager_screens_are_answered_in_order() -> None:
    wait_for, press, pressed = replay(SERIAL)
    answered = vm_mok.enroll(wait_for, press, sleep=lambda seconds: None)
    assert answered == [pattern for pattern, _ in vm_mok.ENROLL_STEPS]
    # OK; a key in the countdown; Enroll key from disk; GENTOO_EFI; the certificate; Continue; Yes; Reboot
    assert pressed == ["ret", "down", "down", "ret", "ret", "down", "ret", "down", "ret", "down", "ret", "ret"]


def test_a_firmware_that_refuses_shim_never_reaches_mokmanager() -> None:
    """RC1 on the ASUS laptop: the firmware says Access Denied before shim; the first step times out with a useful pattern."""
    wait_for, press, _ = replay('BdsDxe: failed to load Boot0001 "UEFI QEMU QEMU USB HARDDRIVE": Access Denied\n')
    with pytest.raises(AssertionError, match="Verification failed"):
        vm_mok.enroll(wait_for, press, sleep=lambda seconds: None)


def test_the_names_mokmanager_shows_are_the_signers_names() -> None:
    from tests.iso.conftest import load_script
    signer = load_script("iso/sign-secure-boot.py", "sign_secure_boot")
    assert vm_mok.CERT_NAME == signer.CERT_NAME
    assert any(signer.EFI_LABEL in pattern for pattern, _ in vm_mok.ENROLL_STEPS)
    assert signer.EFI_LABEL in SERIAL and signer.CERT_NAME in SERIAL


def test_the_live_probe_of_the_real_run_is_clean_and_each_failure_is_named() -> None:
    assert vm_mok.secure_boot_problems(PROBE, CERTIFICATE_SHA256) == []
    other = vm_mok.secure_boot_problems(PROBE, "0" * 64)
    assert len(other) == 1 and "no enrolled key is the disc's certificate" in other[0]
    off = PROBE.replace("SecureBoot enabled", "SecureBoot disabled")
    assert any("SecureBoot enabled" in problem for problem in vm_mok.secure_boot_problems(off, CERTIFICATE_SHA256))
    nothing = vm_mok.secure_boot_problems("SecureBoot enabled\n--- enrolled\n--- export\n--- dmesg\n", CERTIFICATE_SHA256)
    assert len(nothing) == 3  # no key named like the disc's, no exported key, no kernel line


def test_the_scenario_boots_a_usb_stick_with_secure_boot_and_expects_it_in_the_hardware_report() -> None:
    scenario = vm.SCENARIO_MAP["secure-boot-live"]
    assert scenario.live_only and scenario.secure_boot_live and not scenario.in_all
    assert scenario.hardware.secure_boot and scenario.hardware.live_usb and scenario.hardware.vga == "virtio"
    assert dict(scenario.live_expect) == {"firmware.secure_boot": "enabled"}


def test_a_live_usb_stick_replaces_the_cd_and_is_the_first_boot_device(paths) -> None:  # noqa: F811
    argv = normalized(command(paths, "secure-boot-live", live=True), paths)
    assert "media=cdrom" not in " ".join(argv)
    assert argv[argv.index("-machine") + 1] == "q35,smm=on,accel=kvm:tcg" and "driver=cfi.pflash01,property=secure,value=on" in argv
    stick = argv.index("file=ISO,if=none,id=stick,format=raw,readonly=on")
    assert argv[stick - 3:stick - 1] == ["-device", "qemu-xhci,id=livexhci"]
    assert argv[stick + 1:stick + 3] == ["-device", "usb-storage,bus=livexhci.0,drive=stick,removable=on,bootindex=0"]
    # the target disk is not a boot device while the live system runs, and an installed system still boots from its disk
    target = normalized(command(paths, "secure-boot-live", live=False), paths)
    assert "id=stick" not in " ".join(target)


def stick_spec(read_only: bool):
    base = guard_fixtures_spec()
    live = vm_guard.DiskExpectation(role="live-stick", size=2422603776, tran="usb", live_medium=True)
    devices = guard_fixtures.lsblk_tree(base)
    devices.append({"name": "sda", "kname": "sda", "path": "/dev/sda", "type": "disk", "size": 2422603776, "tran": "usb", "ro": read_only,
                    "rm": True, "model": "QEMU HARDDISK", "serial": "1-0000:00:03.0-1", "pttype": "gpt", "fstype": "iso9660",
                    "mountpoints": ["/run/initramfs/live"], "children": []})
    return base, live, devices


def guard_fixtures_spec():
    facts = vm_guard.BUS_FACTS["virtio"]
    size = vm_guard.unusual_sizes([40 * 1024], 11)[0]
    return vm_guard.GuardSpec([vm_guard.DiskExpectation(role="target", size=size, serial="GENTOO-VM-DISPOSABLE", blank=True, model=facts["model"],
                                                        tran=facts["tran"], pci_vendors=list(facts["pci_vendors"]))], "cafe")


def test_the_guard_accepts_the_mounted_read_only_stick_only_as_the_live_medium() -> None:
    base, live, devices = stick_spec(read_only=True)
    without = vm_guard.evaluate(base, devices, dmi_strings=[f"{vm_guard.NONCE_KEY}=cafe"])
    assert not without["ok"] and any("Unexpected disk" in problem for problem in without["problems"])
    spec = vm_guard.GuardSpec([*base.disks, live], base.nonce)
    report = vm_guard.evaluate(spec, devices, dmi_strings=[f"{vm_guard.NONCE_KEY}=cafe"])
    assert report["ok"], report["problems"]
    assert report["target"]["path"] != "/dev/sda", "the stick is never the target"
    _, _, writable = stick_spec(read_only=False)
    refused = vm_guard.evaluate(spec, writable, dmi_strings=[f"{vm_guard.NONCE_KEY}=cafe"])
    assert not refused["ok"] and any("live medium /dev/sda is writable" in problem for problem in refused["problems"])
