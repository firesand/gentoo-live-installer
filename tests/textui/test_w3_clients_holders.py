"""Wave 3 review fixes: the text installer shows what is alive on a disk and can give the consent the service asks for (STOR-5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gentoo_installer import blockdev, system_info
from gentoo_installer.textui.console import Console
from gentoo_installer.textui.conclude import conclude
from gentoo_installer.textui.daemon import Daemon
from gentoo_installer.textui.follow import Outcome
from gentoo_installer.textui.inventory import MachineView
from gentoo_installer.textui.wizard import Wizard

from tests.textui.support import (
    FakeHost, FakeInstaller, Script, completed, device_number, error, good_job, inspect_reply, run_main, stage, valid_manifest, walk_to_review,
)

GIB = 1024**3
VDA = {"/dev/vda": "VDA-SERIAL-0001"}
HOLDERS_MESSAGE = (
    "Active volumes sit on the disk or partition that would be rewritten: lvm /dev/mapper/vg0-root. "
    "Deactivate or remove them first (swapoff, vgchange -an, cryptsetup close, mdadm --stop), "
    "or confirm in the installer that they may be deactivated and destroyed (storage.release_holders)"
)


def node(name: str, type: str = "part", size: int = 10 * GIB, **kwargs) -> blockdev.BlockDevice:
    return blockdev.BlockDevice(name=name, path=f"/dev/{name}", type=type, size=size, maj_min=device_number(name), **kwargs)


def busy_disk(name="vdb", serial="VDB-1", *, swap=False, lvm=False, mounted=False) -> dict:
    """A disk lsblk shows with the volumes a hand-activated LVM, swap or a mount leave alive."""
    children = []
    if swap:
        children.append(node(f"{name}1", fstype="swap", mountpoints=("[SWAP]",)))
    if lvm:
        volume = blockdev.BlockDevice(name="vg0-root", path="/dev/mapper/vg0-root", type="lvm", size=20 * GIB, maj_min=device_number("vg0-root"))
        children.append(node(f"{name}2", fstype="LVM2_member", children=(volume,)))
    if mounted:
        children.append(node(f"{name}3", fstype="ext4", mountpoints=("/mnt/data",)))
    return node(name, "disk", 100 * GIB, serial=serial, model="Old disk", children=tuple(children)).to_dict()


def reply_with(*disks: dict) -> dict:
    reply = inspect_reply("qemu-q35-virtio", serials=VDA)
    reply["disks"] = [disk for disk in reply["disks"] if disk["path"] != "/dev/vda"] + list(disks)
    return reply


@pytest.fixture
def start(installer, host: FakeHost):
    def make(steps, reply):
        fake: FakeInstaller = installer(reply)
        script = Script(steps)
        console = Console(script.stdin, script.out, environ={"TERM": "dumb"})
        daemon = Daemon(fake.path)
        wizard = Wizard(console, host, daemon, view=MachineView(fake.reply, local_by_path=host.by_path_names()), state=daemon.probe())
        return wizard, script, fake

    return make


# --- the list is the GUI's list ---------------------------------------------------------------------------------------------------


def test_the_reasons_come_from_one_place_for_both_installers() -> None:
    disk = blockdev.BlockDevice.from_lsblk(busy_disk(swap=True, lvm=True))
    reasons = system_info.holder_reasons_of(disk)
    assert reasons == ["lvm volume /dev/mapper/vg0-root", "swap in use on /dev/vdb1", "/dev/vdb2 is part of an LVM volume group"]
    from gentoo_installer.pages.storage import holder_reasons
    from tests.ui.conftest import sample_info

    info = sample_info()
    info.roots = [disk]
    assert holder_reasons(info, ["/dev/vdb"]) == reasons


def test_the_disk_menu_says_what_is_alive_on_a_disk(start) -> None:
    wizard, script, _ = start([("Which disk", "1"), ("Switch them off", "n"), ("Which disk", "2")], reply_with(busy_disk(swap=True, lvm=True), busy_disk("vdc", "VDC-1")))
    wizard.step_disk()
    flat = script.flat
    assert "In use: lvm volume /dev/mapper/vg0-root; swap in use on /dev/vdb1; /dev/vdb2 is part of an LVM volume group." in flat
    assert "(mounted)" not in flat, "active swap is not a mount"


# --- a disk with swap or volumes can be chosen, with the consent ---------------------------------------------------------------------------


def test_a_disk_with_swap_or_volumes_asks_for_the_consent_and_stores_it(start) -> None:
    wizard, script, _ = start([("Which disk", "1"), ("In use on /dev/vdb", "y")], reply_with(busy_disk(swap=True, lvm=True)))
    wizard.step_disk()
    storage = wizard.manifest.storage
    assert storage.device == "/dev/vdb" and storage.release_holders is True
    assert "their data is lost" in script.flat and "other disks of the same volume group, RAID array, Btrfs or ZFS pool" in script.flat


def test_refusing_the_consent_returns_to_the_disk_menu_and_stores_nothing(start) -> None:
    wizard, script, _ = start([("Which disk", "1"), ("Switch them off", "n"), ("Which disk", "2")], reply_with(busy_disk(swap=True), busy_disk("vdc", "VDC-1")))
    wizard.step_disk()
    storage = wizard.manifest.storage
    assert storage.device == "/dev/vdc" and storage.release_holders is False


def test_a_quiet_disk_asks_nothing(start) -> None:
    wizard, script, _ = start([("Which disk", "1")], reply_with(busy_disk("vdc", "VDC-1")))
    wizard.step_disk()
    assert "Switch them off" not in script.text and wizard.manifest.storage.release_holders is False


def test_the_consent_given_for_one_disk_does_not_pass_to_the_next(start) -> None:
    wizard, script, _ = start(
        [("Which disk", "1"), ("Switch them off", "y"), ("Which disk", "2")], reply_with(busy_disk(swap=True), busy_disk("vdc", "VDC-1")))
    wizard.step_disk()
    assert wizard.manifest.storage.release_holders is True
    again = Script([("Which disk", "2")])  # the user changes the disk later
    wizard.console = Console(again.stdin, again.out, environ={"TERM": "dumb"})
    wizard.step_disk()
    assert wizard.manifest.storage.device == "/dev/vdc" and wizard.manifest.storage.release_holders is False


def test_a_disk_that_is_really_mounted_is_still_refused(start) -> None:
    wizard, script, _ = start([("Which disk", "1"), ("is in use", "2")], reply_with(busy_disk(mounted=True, swap=True), busy_disk("vdc", "VDC-1")))
    wizard.step_disk()
    assert "/dev/vdb is in use (/mnt/data)" in script.flat and "umount" in script.flat
    assert wizard.manifest.storage.device == "/dev/vdc"


# --- when the service asks for the consent ---------------------------------------------------------------------------------------------------


def holders_job() -> list[dict]:
    return [stage("preflight", 0), stage("preflight", 2, "completed"), stage("storage", 2),
            error(HOLDERS_MESSAGE, "storage.holders", "storage", resumable=False), completed(False, resumable=False)]


def serial_installer(installer, **kwargs) -> FakeInstaller:
    return installer(inspect_reply("qemu-q35-virtio", serials=VDA), **kwargs)


def test_the_menu_after_a_refusal_offers_the_consent_and_then_starts_again(installer, host: FakeHost) -> None:
    fake = serial_installer(installer, jobs=[holders_job(), good_job()])
    script = Script([
        *walk_to_review(), ("Step 9 of 9", "/dev/vda"),
        ("No data was written to your disks", "1"),  # Switch them off, erase them together with the disk, and try again
        ("Really switch them off", "y"),
        ("The installation finished successfully.", "4"),  # restart, log, save, quit
    ])
    result = run_main(["--socket", fake.path], script, host)
    script.assert_finished()
    assert result.code == 0
    assert [manifest["storage"]["release_holders"] for manifest in fake.manifests] == [False, True]
    assert fake.resumes == [False, False]
    assert "Resume the installation" not in script.text, "the refusal came before anything was written"
    assert "may be partly installed" not in script.text and "No data was written to your disks" in script.flat


def test_saying_no_changes_nothing_and_the_menu_stays(installer, host: FakeHost) -> None:
    fake = serial_installer(installer, jobs=[holders_job()])
    script = Script([*walk_to_review(), ("Step 9 of 9", "/dev/vda"), ("What now", "1"), ("Really switch them off", "n"), ("What now", "5")])
    result = run_main(["--socket", fake.path], script, host)
    script.assert_finished()
    assert result.code == 1 and len(fake.manifests) == 1 and fake.manifests[0]["storage"]["release_holders"] is False


def test_conclude_offers_it_only_for_that_refusal_and_when_the_settings_are_known(installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    for outcome, known in ((Outcome("failed", HOLDERS_MESSAGE, code="storage.holders", stage="storage"), False), (Outcome("failed", "x", code="install.failed"), True)):
        script = Script([("What now", "1")])
        conclude(Console(script.stdin, script.out, environ={}), host, Daemon(fake.path), outcome, can_resume=known, can_release=known)
        assert "erase them together with the disk" not in script.text


# --- without anybody to ask ---------------------------------------------------------------------------------------------------------------------


def write_manifest(tmp_path: Path) -> str:
    path = tmp_path / "manifest.json"
    path.write_text(valid_manifest().to_json())
    return str(path)


def test_an_unattended_run_that_is_refused_says_which_option_gives_the_consent(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer, jobs=[holders_job()])
    result = run_main(["--socket", fake.path, "--manifest", write_manifest(tmp_path), "--yes", "--confirm-device", "/dev/vda"], Script(), host)
    assert result.code == 1
    assert "--release-holders" in result.out and "No data was written to your disks" in " ".join(result.out.split())
    assert fake.manifests[0]["storage"]["release_holders"] is False


def test_the_option_gives_the_consent_up_front(tmp_path: Path, installer, host: FakeHost) -> None:
    fake = serial_installer(installer)
    result = run_main(["--socket", fake.path, "--manifest", write_manifest(tmp_path), "--yes", "--confirm-device", "/dev/vda", "--release-holders"], Script(), host)
    assert result.code == 0 and fake.manifests[0]["storage"]["release_holders"] is True


def test_the_option_needs_a_settings_file(host: FakeHost) -> None:
    result = run_main(["--release-holders"], Script(), host)
    assert result.code == 2 and "--release-holders needs --manifest" in result.err


def test_the_help_names_the_option(host: FakeHost) -> None:
    assert "--release-holders" in run_main(["--help"], Script(), host).out


def test_a_refusal_after_the_consent_was_given_does_not_offer_the_same_consent_again(tmp_path: Path, installer, host: FakeHost) -> None:
    """'Volumes are still active after deactivation': asking for the consent again would change nothing."""
    settings = valid_manifest()
    settings.storage.release_holders = True
    path = tmp_path / "settings.json"
    path.write_text(settings.to_json())
    fake = serial_installer(installer, jobs=[holders_job()])
    script = Script([("Type the disk name to start", "/dev/vda"), ("What now", "4")])  # summary, log, save, quit
    result = run_main(["--socket", fake.path, "--manifest", str(path)], script, host)
    script.assert_finished()
    assert result.code == 1 and "erase them together with the disk, and try again" not in script.text
