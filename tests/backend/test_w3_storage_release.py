"""Releasing what is alive on the target before it is erased: STOR-6 (partitions of a stopped array),
STOR-7 (layered LVM devices) and STOR-4 (a disk that holds one member of a multi-device Btrfs/ZFS/bcachefs pool)."""

from __future__ import annotations

import pytest

from gentoo_installer import blockdev
from gentoo_installer.manifest import Partition
from gentoo_installerd.storage import StorageError

from tests.backend.test_storage_holders import consent, used_disk
from tests.backend.test_storage_identity import erase, machine, manual, no_writes, vda
from tests.backend.test_storage_support import FakeDisk, GiB, Holder, StatefulRunner, commands_text, dm_split, make_manager

FSID = "5f8c1b52-6a0e-4d57-9a54-0f1d6b6a7e11"


def wiped(runner: StatefulRunner) -> list[str]:
    return [c[-1] for c in runner.commands if c[0] == "wipefs"]


class TestPartitionedArrays:
    """STOR-6: the partitions of an md array vanish when the array is stopped; they must not be wiped from the old list."""

    def test_partitions_of_a_stopped_array_are_not_wiped(self, tmp_path):
        array = Holder("md0", "raid1", children=(Holder("md0p1", "part"), Holder("md0p2", "part")))
        runner = StatefulRunner([vda(partitions=[{"num": 1, "size": 40 * GiB, "fstype": "linux_raid_member", "holders": [array]}])])
        runner.mdadm_examine["/dev/vda1"] = "  Array UUID : aaaa:bbbb\n"
        make_manager(runner, tmp_path).prepare(consent())
        assert ("mdadm", "--stop", "/dev/md0") in runner.commands
        assert not any("md0p" in device for device in wiped(runner)), wiped(runner)
        # the member's own signature is wiped, once, before the table goes
        assert wiped(runner)[0] == "/dev/vda1"
        assert ("sgdisk", "--zap-all", "/dev/vda") in runner.commands

    def test_an_intel_rst_member_without_partitions_of_its_own(self, tmp_path):
        volume = Holder("md126", "raid1", children=(Holder("md126p1", "part"), Holder("md126p2", "part")))
        container = Holder("md127", "md", children=(volume,))
        disk = vda()
        disk.fstype, disk.holders = "isw_raid_member", [container]
        runner = StatefulRunner([disk])
        runner.mdadm_examine["/dev/vda"] = "  Magic : Intel Raid ISM Cfg Sig.\n  UUID : imsm-1\n"
        make_manager(runner, tmp_path).prepare(consent())
        assert [c for c in runner.commands if c[:2] == ("mdadm", "--stop")] == [("mdadm", "--stop", "/dev/md126"), ("mdadm", "--stop", "/dev/md127")]
        assert not any("md12" in device for device in wiped(runner)), wiped(runner)
        assert ("sgdisk", "--zap-all", "/dev/vda") in runner.commands

    def test_the_real_partitions_and_mbr_logical_partitions_are_still_wiped(self, tmp_path):
        old = vda(pttype="dos", partitions=[
            {"num": 1, "size": 10 * GiB, "fstype": "ext4"},
            {"num": 2, "size": 1024, "fstype": None},  # the extended container lsblk shows as a 1 KiB "part"
            {"num": 5, "size": 10 * GiB, "fstype": "ext4"},
        ])
        runner = StatefulRunner([old])
        make_manager(runner, tmp_path).prepare(erase())
        assert wiped(runner)[:2] == ["/dev/vda1", "/dev/vda5"]


class TestLayeredVolumes:
    """STOR-7: lvs cannot resolve /dev/mapper/vg0-pool-tpool (a layer), and the install died on the raw error."""

    def thin_pool(self) -> Holder:
        # lsblk lists the thin volume below the pool's -tpool device; stopping goes children first
        return Holder("vg0-pool-tpool", "lvm", vg="vg0", children=(Holder("vg0-thin1", "lvm", vg="vg0"),))

    def test_a_thin_volume_and_its_pool_layer_are_released_with_one_vgchange(self, tmp_path):
        runner = StatefulRunner([used_disk(self.thin_pool())])
        make_manager(runner, tmp_path).prepare(consent())
        assert [c for c in runner.commands if c[0] == "vgchange"] == [("vgchange", "-an", "vg0")]
        assert not any(c[0] == "lvs" for c in runner.commands)
        assert ("sgdisk", "--zap-all", "/dev/vda") in runner.commands

    def test_when_only_the_pool_layers_are_active_the_group_is_still_found(self, tmp_path):
        # no thin volume is active, so the very first holder is a layered device: the old lvs call failed on every retry
        runner = StatefulRunner([used_disk(Holder("vg0-pool-tpool", "lvm", vg="vg0"), Holder("vg0-pool_tmeta", "lvm", vg="vg0"))])
        make_manager(runner, tmp_path).prepare(consent())
        assert [c for c in runner.commands if c[0] == "vgchange"] == [("vgchange", "-an", "vg0")]

    def test_two_groups_get_one_vgchange_each(self, tmp_path):
        runner = StatefulRunner([used_disk(Holder("vg0-root", "lvm", vg="vg0"), Holder("my--vg-data", "lvm", vg="my-vg"))])
        make_manager(runner, tmp_path).prepare(consent())
        assert sorted(c for c in runner.commands if c[0] == "vgchange") == [("vgchange", "-an", "my-vg"), ("vgchange", "-an", "vg0")]

    def test_the_order_children_first_is_kept_for_a_group_on_a_container(self, tmp_path):
        # volume group -> volume -> LUKS inside it -> volume group on that: the inner group goes first, then the container
        inner = Holder("vg1-data", "lvm", vg="vg1")
        luks = Holder("luks-inner", "crypt", children=(inner,))
        outer = Holder("vg0-root", "lvm", vg="vg0", children=(luks,))
        runner = StatefulRunner([used_disk(outer)])
        make_manager(runner, tmp_path).prepare(consent())
        text = commands_text(runner)
        assert text.index("vgchange -an vg1") < text.index("cryptsetup close luks-inner") < text.index("vgchange -an vg0")

    def test_a_group_that_cannot_be_deactivated_is_reported_with_the_manual_command(self, tmp_path):
        runner = StatefulRunner([used_disk(Holder("vg0-root", "lvm", vg="vg0"))], fail={"vgchange -an vg0": (5, "Logical volume vg0/root contains a filesystem in use.")})
        with pytest.raises(StorageError, match="volume group vg0.*vgchange -an vg0") as caught:
            make_manager(runner, tmp_path).prepare(consent())
        assert caught.value.code == "storage.holders"
        no_writes(runner)

    def test_a_volume_whose_group_cannot_be_read_stops_before_anything_is_written(self, tmp_path):
        runner = StatefulRunner([used_disk(Holder("vg0-root", "lvm", vg="vg0"))], fail={"dmsetup splitname": (127, "dmsetup: command not found")})
        with pytest.raises(StorageError, match="Cannot determine the volume group of /dev/mapper/vg0-root.*vgchange -an") as caught:
            make_manager(runner, tmp_path).prepare(consent())
        assert caught.value.code == "storage.holders"
        no_writes(runner)

    @pytest.mark.parametrize(
        ("name", "parts"),
        [
            ("vg0-thin1", ("vg0", "thin1", "")),
            ("vg0-pool-tpool", ("vg0", "pool", "tpool")),
            ("vg0-pool_tdata", ("vg0", "pool_tdata", "")),
            ("my--vg-lv--x-real", ("my-vg", "lv-x", "real")),
        ],
    )
    def test_the_fake_splits_names_like_dmsetup(self, name, parts):
        """Checked against `dmsetup splitname --noheadings -o vg_name,lv_name,lv_layer NAME LVM` on this machine."""
        assert dm_split(name) == parts


def pool_disks(fstype: str = "btrfs", *, other_uuid: str = FSID, second_fstype: str | None = None) -> list[FakeDisk]:
    target = vda(partitions=[{"num": 1, "size": 1 * GiB, "fstype": "vfat"}, {"num": 2, "size": 40 * GiB, "fstype": fstype, "uuid": FSID}])
    other = FakeDisk("/dev/vdb", 80 * GiB, serial="SER-B", partitions=[{"num": 1, "size": 40 * GiB, "fstype": second_fstype or fstype, "uuid": other_uuid}])
    return [target, other]


class TestMultiDevicePools:
    """STOR-4: one member of a Btrfs/ZFS/bcachefs pool on the disk to be erased, another member on a disk that stays."""

    def test_the_disk_is_refused_without_consent_and_nothing_is_written(self, tmp_path):
        runner = StatefulRunner(pool_disks())
        with pytest.raises(StorageError, match=f"Btrfs.*{FSID}.*members here: /dev/vda2; also on /dev/vdb1.*destroy data stored on /dev/vdb1") as caught:
            make_manager(runner, tmp_path).prepare(erase())
        assert caught.value.code == "storage.holders"
        assert "release_holders" in str(caught.value)
        no_writes(runner)

    def test_consent_lets_it_through_and_is_written_to_the_log(self, tmp_path):
        runner = StatefulRunner(pool_disks())
        manager = make_manager(runner, tmp_path)
        manager.prepare(consent())
        assert any("Consent given" in note and "Btrfs" in note and "/dev/vdb1" in note for note in manager.notes)
        assert ("sgdisk", "--zap-all", "/dev/vda") in runner.commands
        assert not any(c[0] in {"vgchange", "cryptsetup", "mdadm", "dmsetup", "lvs"} for c in runner.commands)  # nothing to deactivate

    @pytest.mark.parametrize("fstype", ["zfs_member", "bcachefs"])
    def test_zfs_and_bcachefs_members_count_too(self, tmp_path, fstype):
        runner = StatefulRunner(pool_disks(fstype))
        with pytest.raises(StorageError, match="also on /dev/vdb1"):
            make_manager(runner, tmp_path).prepare(erase())
        no_writes(runner)

    def test_an_ordinary_single_device_btrfs_needs_no_consent(self, tmp_path):
        # the other disk's Btrfs is a different filesystem (own UUID): nothing is shared
        runner = StatefulRunner(pool_disks(other_uuid="0a0b0c0d-1111-2222-3333-444455556666"))
        manager = make_manager(runner, tmp_path)
        manager.prepare(erase())
        assert ("sgdisk", "--zap-all", "/dev/vda") in runner.commands

    def test_the_same_uuid_on_a_different_kind_of_filesystem_is_not_a_pool(self, tmp_path):
        runner = StatefulRunner(pool_disks(second_fstype="ext4"))
        make_manager(runner, tmp_path).prepare(erase())

    def test_a_pool_wholly_on_the_target_needs_no_consent(self, tmp_path):
        disk = vda(partitions=[{"num": 1, "size": 20 * GiB, "fstype": "btrfs", "uuid": FSID}, {"num": 2, "size": 20 * GiB, "fstype": "btrfs", "uuid": FSID}])
        make_manager(StatefulRunner([disk]), tmp_path).prepare(erase())

    def pooled_machine(self):
        disks = machine()
        disks[1].parts[0].update(fstype="btrfs", uuid=FSID)  # /dev/sdb1 and /dev/sda2 are the two members
        disks[0].parts[1].update(fstype="btrfs", uuid=FSID)
        return disks

    def test_manual_mode_asks_before_it_formats_a_pool_member(self, tmp_path):
        rows = (Partition("/dev/sda1", "/efi", "vfat", format=False), Partition("/dev/sdb1", "/", "ext4", format=True))
        runner = StatefulRunner(self.pooled_machine())
        with pytest.raises(StorageError, match="also on /dev/sda2") as caught:
            make_manager(runner, tmp_path).prepare(manual(*rows, additional_device_ids=["SER-B"]))
        assert caught.value.code == "storage.holders"
        no_writes(runner)
        make_manager(StatefulRunner(self.pooled_machine()), tmp_path).prepare(manual(*rows, additional_device_ids=["SER-B"], release_holders=True))

    def test_manual_mode_does_not_ask_about_a_member_it_keeps(self, tmp_path):
        # /dev/sdb1 is used as it is (format=false): nothing is rewritten, so nothing is lost on /dev/sda2
        rows = (Partition("/dev/sda1", "/efi", "vfat", format=False), Partition("/dev/sdb1", "/", "btrfs", format=False), Partition("/dev/sdb2", "/data", "ext4", format=True))
        runner = StatefulRunner(self.pooled_machine())
        make_manager(runner, tmp_path).prepare(manual(*rows, additional_device_ids=["SER-B"]))
        assert ("mkfs.ext4", "-F", "/dev/sdb2") in runner.commands and ("mkfs.btrfs", "-f", "/dev/sdb1") not in runner.commands


class TestFilesystemPoolsInTheSharedBlockDeviceCode:
    """The GUI asks the same question through gentoo_installer.blockdev (a disk card must show the warning too)."""

    def roots(self, **changes):
        runner = StatefulRunner(pool_disks(**changes))
        return blockdev.parse_lsblk(runner.lsblk_json())

    def test_a_member_with_a_partner_elsewhere_is_reported(self):
        roots = self.roots()
        pools = blockdev.filesystem_pools(roots, [blockdev.find(roots, "/dev/vda")])
        assert [(p.fstype, p.uuid, p.inside, p.outside) for p in pools] == [("btrfs", FSID, ("/dev/vda2",), ("/dev/vdb1",))]
        assert pools[0].spans_other_disks and "Btrfs" in pools[0].describe() and "/dev/vdb1" in pools[0].describe()

    def test_the_other_disk_asked_about_itself_finds_the_first(self):
        roots = self.roots()
        pools = blockdev.filesystem_pools(roots, [blockdev.find(roots, "/dev/vdb")])
        assert [(p.inside, p.outside) for p in pools] == [(("/dev/vdb1",), ("/dev/vda2",))]

    def test_a_filesystem_on_one_device_is_not_a_pool(self):
        roots = self.roots(other_uuid="0a0b0c0d-1111-2222-3333-444455556666")
        assert blockdev.filesystem_pools(roots, [blockdev.find(roots, "/dev/vda")]) == []

    def test_partitions_without_a_uuid_are_ignored(self):
        disks = pool_disks()
        for disk in disks:
            for part in disk.parts:
                part.pop("uuid", None)
        roots = blockdev.parse_lsblk(StatefulRunner(disks).lsblk_json())
        assert blockdev.filesystem_pools(roots, [blockdev.find(roots, "/dev/vda")]) == []
