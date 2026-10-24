"""The text installer's disk step names the Btrfs or ZFS pool a disk belongs to, in the words of the window (STOR-4, console half)."""

from __future__ import annotations

import pytest

from gentoo_installer import blockdev
from gentoo_installer.textui.console import Console
from gentoo_installer.textui.daemon import Daemon
from gentoo_installer.textui.inventory import MachineView
from gentoo_installer.textui.wizard import Wizard

from tests.textui.support import FakeHost, FakeInstaller, Script, device_number, inspect_reply

GIB = 1024**3
POOL = "6b1d4c52-2f0a-4c55-9b1e-0a3c1f2e9d77"
OTHER = "0e7a2f90-91d5-4a57-8d55-5b1c6c9a41e3"


@pytest.fixture
def start(installer, host: FakeHost):
    """``start(steps, reply)`` -> (wizard, script, fake service)."""

    def make(steps, reply):
        fake: FakeInstaller = installer(reply)
        script = Script(steps)
        console = Console(script.stdin, script.out, environ={"TERM": "dumb"})
        daemon = Daemon(fake.path)
        wizard = Wizard(console, host, daemon, view=MachineView(fake.reply, local_by_path=host.by_path_names()), state=daemon.probe())
        return wizard, script, fake

    return make


def node(name: str, type: str = "part", size: int = 10 * GIB, **kwargs) -> blockdev.BlockDevice:
    return blockdev.BlockDevice(name=name, path=f"/dev/{name}", type=type, size=size, maj_min=device_number(name), **kwargs)


def reply_with(*disks: dict) -> dict:
    """The reply of the 4 GiB virtual machine with its own disk replaced by ``disks``."""
    reply = inspect_reply("qemu-q35-virtio", serials={"/dev/vda": "VDA-SERIAL-0001"})
    reply["disks"] = [disk for disk in reply["disks"] if disk["path"] != "/dev/vda"] + list(disks)
    return reply


def pool_disk(name: str, serial: str, *, fstype: str = "btrfs", uuid: str = POOL) -> dict:
    """A disk with one partition; lsblk shows the filesystem's own UUID on every member of a multi-device filesystem."""
    return node(name, "disk", 100 * GIB, serial=serial, model="Pool disk", children=(node(f"{name}1", fstype=fstype, uuid=uuid),)).to_dict()


def test_the_disk_menu_and_the_question_name_the_pool(start) -> None:
    wizard, script, _ = start([("Which disk", "1"), ("In use on /dev/vdb", "y")], reply_with(pool_disk("vdb", "VDB-1"), pool_disk("vdc", "VDC-1")))
    wizard.step_disk()
    reason = "/dev/vdb1 is part of a Btrfs filesystem that also uses /dev/vdc1"
    assert f"In use: {reason}." in script.flat  # the line under the disk in the menu
    assert f"In use on /dev/vdb: {reason}." in script.flat  # the question
    assert "Btrfs or ZFS pool" in script.flat  # what the consent covers
    storage = wizard.manifest.storage
    assert storage.device == "/dev/vdb" and storage.release_holders is True


def test_without_the_consent_the_disk_is_not_taken(start) -> None:
    wizard, _, _ = start([("Which disk", "1"), ("Switch them off", "n"), ("Which disk", "2"), ("Switch them off", "n"), ("Which disk", "3")],
                         reply_with(pool_disk("vdb", "VDB-1"), pool_disk("vdc", "VDC-1"), pool_disk("vdd", "VDD-1", uuid=OTHER)))
    wizard.step_disk()
    storage = wizard.manifest.storage
    assert storage.device == "/dev/vdd" and storage.release_holders is False


def test_a_zfs_pool_is_named_the_same_way(start) -> None:
    wizard, script, _ = start([("Which disk", "1"), ("In use on /dev/vdb", "n"), ("Which disk", "3")],
                              reply_with(pool_disk("vdb", "VDB-1", fstype="zfs_member"), pool_disk("vdc", "VDC-1", fstype="zfs_member"), pool_disk("vdd", "VDD-1")))
    wizard.step_disk()
    assert "/dev/vdb1 is part of a ZFS pool that also uses /dev/vdc1" in script.flat


def test_a_filesystem_on_one_disk_asks_nothing(start) -> None:
    wizard, script, _ = start([("Which disk", "1")], reply_with(pool_disk("vdb", "VDB-1")))
    wizard.step_disk()
    assert "In use" not in script.flat and wizard.manifest.storage.release_holders is False


def test_the_two_installers_give_the_same_list(start) -> None:
    from gentoo_installer.pages.storage import holder_reasons
    from tests.ui.conftest import sample_info

    reply = reply_with(pool_disk("vdb", "VDB-1"), pool_disk("vdc", "VDC-1"))
    view = MachineView(reply)
    entry = view.find_disk("/dev/vdb")
    assert entry is not None and entry.holders == ["/dev/vdb1 is part of a Btrfs filesystem that also uses /dev/vdc1"]
    info = sample_info()
    info.roots = [blockdev.BlockDevice.from_lsblk(item) for item in (pool_disk("vdb", "VDB-1"), pool_disk("vdc", "VDC-1"))]
    assert holder_reasons(info, ["/dev/vdb"]) == entry.holders
