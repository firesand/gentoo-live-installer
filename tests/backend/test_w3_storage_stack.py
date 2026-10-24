"""STOR-2: an encrypted root with LVM on top of the container.

LVM names the device of a logical volume <vg>-<lv>, so the volume `root` of the volume group `gentoo`
is /dev/mapper/gentoo-root. The dm-crypt mapping under it used the same name, the kernel refuses a
second device-mapper device with a name in use, and the install died at lvcreate after the disk was
repartitioned and LUKS-formatted. With LVM on top the mapping now has its own name, from one helper
that storage and the initramfs configuration both use.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from gentoo_installer import layout as guided_layout
from gentoo_installer.manifest import InstallManifest, Partition
from gentoo_installerd.configure import Configurer
from gentoo_installerd.runner import RunnerError
from gentoo_installerd.storage import LayoutEntry, StorageError

from tests.backend.helpers import FakeRunner
from tests.backend.test_storage_identity import ESP_ROW, erase, machine, manual, no_writes, vda
from tests.backend.test_storage_support import GiB, Holder, StatefulRunner, commands_text, make_manager

PASSPHRASE = "long enough passphrase"
UUID = "12345678-1234-1234-1234-123456789abc"


def encrypted(**changes) -> InstallManifest:
    return erase(luks=True, luks_passphrase=PASSPHRASE, **changes)


def prepared(tmp_path, **changes):
    runner = StatefulRunner([vda()])
    manager = make_manager(runner, tmp_path)
    manager.prepare(encrypted(**changes))
    return runner, manager


class TestTheMappingName:
    def test_luks_alone_keeps_gentoo_root(self):
        m = encrypted()
        assert guided_layout.luks_mapper_name(m.storage) == "gentoo-root"

    def test_with_lvm_on_top_the_mapping_gets_another_name(self):
        m = encrypted(lvm=True)
        assert guided_layout.luks_mapper_name(m.storage) == "gentoo-crypt"
        # never the name LVM gives its logical volume /dev/gentoo/root
        assert guided_layout.luks_mapper_name(m.storage) != "gentoo-root"


class TestGuidedLuksWithLvm:
    def test_the_install_gets_through_the_whole_stack(self, tmp_path):
        runner, manager = prepared(tmp_path, lvm=True, swap_mib=0)
        text = commands_text(runner)
        assert "cryptsetup open --key-file=- /dev/vda3 gentoo-crypt" in text
        assert "pvcreate --force /dev/mapper/gentoo-crypt" in text
        assert "vgcreate gentoo /dev/mapper/gentoo-crypt" in text
        assert "lvcreate --yes --extents 100%FREE --name root gentoo" in text
        assert next(e.device for e in manager.layout if e.mountpoint == "/") == "/dev/gentoo/root"
        assert ("mkfs.ext4", "-F", "/dev/gentoo/root") in runner.commands
        assert manager.owned_maps == ["gentoo-crypt"] and manager.owned_vgs == ["gentoo"]

    def test_the_receipt_names_the_mapping_and_resume_opens_it_by_that_name(self, tmp_path):
        runner, manager = prepared(tmp_path, lvm=True, swap_mib=0)
        receipt = json.loads(json.dumps(manager.snapshot()))
        assert receipt["stack"]["mapper"] == "gentoo-crypt" and receipt["stack"]["vg"] == "gentoo"
        manager.cleanup()
        runner.commands.clear()
        again = make_manager(runner, tmp_path)
        brief = [LayoutEntry(e.device, e.mountpoint, e.filesystem, e.format) for e in manager.layout]
        again.restore(encrypted(lvm=True, swap_mib=0), brief, receipt)
        assert ("cryptsetup", "open", "--key-file=-", "/dev/vda3", "gentoo-crypt") in runner.commands
        assert again.owned_maps == ["gentoo-crypt"]

    def test_cleanup_closes_the_volume_group_and_then_the_mapping(self, tmp_path):
        runner, manager = prepared(tmp_path, lvm=True, swap_mib=0)
        runner.commands.clear()
        manager.cleanup()
        text = commands_text(runner)
        assert text.index("vgchange -an gentoo") < text.index("cryptsetup close gentoo-crypt")

    def test_the_default_swap_file_works_on_top_of_the_stack_too(self, tmp_path):
        runner, manager = prepared(tmp_path, lvm=True)
        assert any(e.source == "/swapfile" for e in manager.layout)

    def test_a_manual_layout_uses_the_same_names(self, tmp_path):
        runner = StatefulRunner(machine())
        rows = (ESP_ROW, Partition("/dev/sdb1", "/", "ext4", format=True), Partition("/dev/sdb2", "/boot", "ext4", format=True))
        m = manual(*rows, additional_device_ids=["SER-B"], luks=True, luks_passphrase=PASSPHRASE, lvm=True)
        manager = make_manager(runner, tmp_path)
        manager.prepare(m)
        assert ("cryptsetup", "open", "--key-file=-", "/dev/sdb1", "gentoo-crypt") in runner.commands
        assert manager.snapshot()["stack"]["mapper"] == "gentoo-crypt"

    def test_luks_without_lvm_is_unchanged(self, tmp_path):
        runner, manager = prepared(tmp_path, swap_mib=0)
        assert ("cryptsetup", "open", "--key-file=-", "/dev/vda3", "gentoo-root") in runner.commands
        assert next(e.device for e in manager.layout if e.mountpoint == "/") == "/dev/mapper/gentoo-root"


class TestTheInitramfsUsesTheSameName:
    def configure(self, m: InstallManifest) -> str:
        runner = FakeRunner()
        runner.respond("blkid -s UUID -o value /dev/vda3", UUID + "\n")
        runner.respond("blkid", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Configurer(runner, root).configure_initramfs(m, "/dev/gentoo/root" if m.storage.lvm else "/dev/mapper/gentoo-root", "/dev/vda3")
            return (root / "etc/kernel/cmdline").read_text()

    @pytest.mark.parametrize("lvm", [False, True])
    def test_rd_luks_name_is_the_mapping_the_storage_stage_created(self, tmp_path, lvm):
        runner, manager = prepared(tmp_path, lvm=lvm, swap_mib=0)
        created = manager.snapshot()["stack"]["mapper"]
        cmdline = self.configure(encrypted(lvm=lvm, swap_mib=0))
        assert f"rd.luks.name={UUID}={created}" in cmdline.split()

    def test_the_lvm_root_is_still_activated_by_name(self):
        cmdline = self.configure(encrypted(lvm=True)).split()
        assert "rd.lvm.lv=gentoo/root" in cmdline
        assert f"rd.luks.name={UUID}=gentoo-crypt" in cmdline


class TestNameCollisions:
    """Both names this stack takes must be free: the mapping and, with LVM, the volume's own device-mapper name."""

    def active(self, tmp_path, runner, *names):
        manager = make_manager(runner, tmp_path)
        for name in names:
            (tmp_path / "dev" / "mapper" / name).write_text("")
        return manager

    def test_an_active_gentoo_crypt_is_refused_before_any_write(self, tmp_path):
        runner = StatefulRunner([vda()])
        manager = self.active(tmp_path, runner, "gentoo-crypt")
        with pytest.raises(StorageError, match="gentoo-crypt is already active") as caught:
            manager.prepare(encrypted(lvm=True))
        assert caught.value.code == "storage.collision"
        no_writes(runner)

    def test_a_device_named_gentoo_root_blocks_the_logical_volume_even_without_encryption(self, tmp_path):
        # a mapping left by an earlier LUKS-only attempt: lvcreate could not activate gentoo/root next to it
        runner = StatefulRunner([vda()])
        manager = self.active(tmp_path, runner, "gentoo-root")
        with pytest.raises(StorageError, match="gentoo-root is already active") as caught:
            manager.prepare(erase(lvm=True))
        assert caught.value.code == "storage.collision"
        no_writes(runner)

    def test_with_both_the_old_gentoo_root_still_blocks_the_volume(self, tmp_path):
        runner = StatefulRunner([vda()])
        manager = self.active(tmp_path, runner, "gentoo-root")
        with pytest.raises(StorageError, match="gentoo-root is already active"):
            manager.prepare(encrypted(lvm=True))
        no_writes(runner)

    def test_a_luks_only_install_only_needs_the_mapping_name(self, tmp_path):
        runner = StatefulRunner([vda()])
        manager = self.active(tmp_path, runner, "gentoo-crypt")  # not in the way without LVM
        manager.prepare(encrypted(swap_mib=0))

    def test_the_old_install_being_released_does_not_count(self, tmp_path):
        old = Holder("gentoo-crypt", "crypt")
        disk = vda(partitions=[{"num": 1, "size": 40 * GiB, "fstype": "crypto_LUKS", "holders": [old]}])
        runner = StatefulRunner([disk])
        manager = self.active(tmp_path, runner, "gentoo-crypt")
        with pytest.raises(StorageError, match="Mapper already exists"):  # the fake leaves the /dev node: the later guard protects the new open
            manager.prepare(encrypted(lvm=True, release_holders=True, swap_mib=0))
        assert ("cryptsetup", "close", "gentoo-crypt") in runner.commands


def test_the_fake_refuses_what_the_kernel_refuses():
    """Pins the device-mapper rule the tests above rely on (a name is used once), so the fake cannot drift."""
    runner = StatefulRunner([vda(partitions=[{"num": 1, "size": 40 * GiB}])])
    runner.run(("cryptsetup", "open", "--key-file=-", "/dev/vda1", "gentoo-root"))
    runner.run(("pvcreate", "--force", "/dev/mapper/gentoo-root"))
    runner.run(("vgcreate", "gentoo", "/dev/mapper/gentoo-root"))
    with pytest.raises(RunnerError, match="Failed to activate new LV"):
        runner.run(("lvcreate", "--yes", "--extents", "100%FREE", "--name", "root", "gentoo"))
