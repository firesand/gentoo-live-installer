"""The whole wizard on every captured test machine (tests/hardware/fixtures): desktops, laptops, VMs, old BIOS PCs.

The installer must not depend on the author's PC: each machine has another firmware, memory size, disk layout and
network adapters, and the wizard has to come out with a manifest the daemon accepts for every one of them.
"""

from __future__ import annotations

import pytest

from gentoo_installer.manifest import InstallManifest
from gentoo_installer.textui.inventory import MachineView

from tests.hardware.helpers import FIXTURE_NAMES
from tests.textui.support import END, FakeHost, Script, accounts, inspect_reply, region, run_main, to_disk_step

MACHINES_WITH_A_DISK = [name for name in FIXTURE_NAMES if name != "intel-rst-hidden-nvme"]


def answers_for(view: MachineView) -> tuple[list[tuple[str, object]], str]:
    """The answers that install on the first usable disk with every default; returns (script, device)."""
    entry = next(entry for entry in view.disk_entries() if entry.usable)
    steps: list[tuple[str, object]] = [*to_disk_step(), ("Step 4 of 9: Disk", "1")]
    if not entry.has_identity:
        steps.append(("neither a serial number nor a WWN", "y"))
    if entry.disk.removable or entry.disk.hotplug:
        steps.append(("start this system on other computers", "n"))
    steps += [("Step 5 of 9", ""), ("Encrypt", ""), ("Step 6 of 9", ""), ("init system", ""), *accounts(), *region(), ("type the disk name exactly", entry.disk.path), ("The installation", "4")]
    return steps, entry.disk.path


@pytest.mark.parametrize("machine", MACHINES_WITH_A_DISK)
def test_the_wizard_installs_on_this_machine(installer, host: FakeHost, machine: str) -> None:
    reply = inspect_reply(machine)
    view = MachineView(reply)
    steps, device = answers_for(view)
    fake = installer(reply)
    script = Script(steps)
    result = run_main(["--socket", fake.path], script, host)
    script.assert_finished()
    assert result.code == 0, script.text
    sent = fake.manifests[0]
    manifest = InstallManifest.from_dict(sent)
    manifest.require_valid()
    assert manifest.storage.device == device and manifest.storage.firmware == ("uefi" if view.uefi else "bios")
    assert manifest.storage.mode == "erase" and manifest.storage.device_id
    machine_kind = view.profile.machine.hypervisor
    assert manifest.options.microcode is (not machine_kind)  # a virtual machine needs no microcode; real hardware does
    if not view.profile.bluetooth.present:
        assert manifest.options.bluetooth is False


@pytest.mark.parametrize("machine", MACHINES_WITH_A_DISK)
def test_every_machine_gets_a_readable_hardware_page_and_review(installer, host: FakeHost, machine: str) -> None:
    reply = inspect_reply(machine)
    view = MachineView(reply)
    steps, _ = answers_for(view)
    script = Script([*steps[:-2], ("Step 9 of 9", END)])
    fake = installer(reply)
    run_main(["--socket", fake.path], script, host)
    text = script.text
    assert view.profile.summary().split(" - ")[0] in text.replace("\n  ", " ")  # the hardware page names the processor
    assert "What will happen to the disks:" in text and "Chosen for this computer:" in text
    assert max(len(line) for line in text.splitlines()) <= 78


def test_a_laptop_without_a_wired_adapter_is_guided_to_wifi(installer, host: FakeHost) -> None:
    reply = inspect_reply("thinkpad-intel-nvidia-hybrid", network=False)
    fake = installer(reply)
    script = Script([("Step 1 of 9", ""), ("Internet: NOT connected", "n"), ("NOT connected", END)])
    run_main(["--socket", fake.path], script, host)
    assert host.called("run") == [("run", ("nmtui",))]
    assert "wlp0s20f3: Wi-Fi" in script.text


def test_a_computer_whose_disk_is_hidden_by_the_firmware_is_told_what_to_do(installer, host: FakeHost) -> None:
    fake = installer(inspect_reply("intel-rst-hidden-nvme"))
    script = Script([*to_disk_step(), ("No disk can be installed to", END)])
    result = run_main(["--socket", fake.path], script, host)
    text = script.flat
    assert result.code == 130
    assert "SATA Operation to AHCI" in text  # the hardware page said it
    assert "/dev/sdb (USB DISK 3.0) is not offered: it holds the live installer medium" in text
    assert "storage mode (RAID/Intel RST/VMD) in the firmware settings" in text
    assert not [request for request in fake.requests if request["action"] == "start"]


def test_the_desktop_with_six_disks_lists_them_all_and_warns_about_windows(installer, host: FakeHost) -> None:
    fake = installer(inspect_reply("asus-desktop-rtx4090"))
    script = Script([*to_disk_step(), ("Step 4 of 9: Disk", END)])
    run_main(["--socket", fake.path], script, host)
    for disk in ("/dev/nvme0n1", "/dev/nvme1n1", "/dev/nvme2n1", "/dev/sda", "/dev/sdb", "/dev/sdc"):
        assert f"{disk} - " in script.text
    assert "Looks like Windows is installed here." in script.text  # sda holds an NTFS partition
    assert "WD_BLACK SN850X 4000GB" in script.text and "NVMe" in script.text
