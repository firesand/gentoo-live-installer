"""Before the first destructive command: active volumes (S3), spanning LVM/md sets (H12), stale signatures,
name collisions and capacity (S5)."""

from __future__ import annotations

import pytest

from gentoo_installer import layout as guided_layout
from gentoo_installerd.storage import StorageError

from tests.backend.helpers import manifest
from tests.backend.test_storage_support import (
    FakeDisk,
    GiB,
    Holder,
    MiB,
    StatefulRunner,
    commands_text,
    first_write,
    make_manager,
    root_partition_size,
)
from tests.backend.test_storage_identity import erase, no_writes, vda


def lvm_stack(*, mounted: bool = False, sticky: bool = False) -> Holder:
    """partition -> LUKS -> LV vg0-root, as a previous Linux install leaves it active."""
    lv = Holder("vg0-root", "lvm", vg="vg0", mounts=("/mnt/old",) if mounted else (), sticky=sticky)
    return Holder("luks-old", "crypt", children=(lv,))


def used_disk(*holders: Holder, **kwargs) -> FakeDisk:
    return vda(partitions=[{"num": 1, "size": 1 * GiB, "fstype": "vfat"}, {"num": 2, "size": 40 * GiB, "fstype": "crypto_LUKS", "holders": list(holders)}], **kwargs)


def consent(m=None):
    m = m or erase()
    m.storage.release_holders = True
    return m


class TestActiveVolumes:
    def test_active_volumes_are_refused_without_consent_and_nothing_is_written(self, tmp_path):
        runner = StatefulRunner([used_disk(lvm_stack())])
        with pytest.raises(StorageError, match="Active volumes sit on the disk") as caught:
            make_manager(runner, tmp_path).prepare(erase())
        assert caught.value.code == "storage.holders"
        assert "lvm /dev/mapper/vg0-root" in str(caught.value) and "crypt /dev/mapper/luks-old" in str(caught.value)
        # the way out is told in the words of the clients, not by the name of a manifest field
        assert "release_holders" not in str(caught.value) and "storage." not in str(caught.value)
        assert "tick the box in the installer window" in str(caught.value) and "--release-holders" in str(caught.value)
        no_writes(runner)
        assert not any(c[0] in {"vgchange", "cryptsetup", "mdadm", "dmsetup"} for c in runner.commands)

    def test_consent_stops_children_first_then_wipes_then_partitions(self, tmp_path):
        runner = StatefulRunner([used_disk(lvm_stack())])
        make_manager(runner, tmp_path).prepare(consent())
        text = commands_text(runner)
        order = [text.index(item) for item in ("vgchange -an vg0", "cryptsetup close luks-old", "wipefs --all /dev/vda1", "wipefs --all /dev/vda2", "sgdisk --zap-all /dev/vda")]
        assert order == sorted(order), text
        assert text.index("udevadm settle") < text.index("wipefs --all /dev/vda1")
        # the group comes from the device-mapper name: lvs fails for the layered devices of thin pools (STOR-7)
        assert ("dmsetup", "splitname", "--noheadings", "-o", "vg_name", "vg0-root", "LVM") in runner.commands
        assert not any(c[0] == "lvs" for c in runner.commands)

    def test_one_vgchange_per_volume_group(self, tmp_path):
        two = Holder("luks-old", "crypt", children=(Holder("vg0-root", "lvm", vg="vg0"), Holder("vg0-home", "lvm", vg="vg0")))
        runner = StatefulRunner([used_disk(two)])
        make_manager(runner, tmp_path).prepare(consent())
        assert [c for c in runner.commands if c[0] == "vgchange"] == [("vgchange", "-an", "vg0")]

    def test_consent_does_not_hide_a_stubborn_volume(self, tmp_path):
        runner = StatefulRunner([used_disk(Holder("vg0-root", "lvm", vg="vg0", sticky=True))])
        with pytest.raises(StorageError, match="still active after deactivation"):
            make_manager(runner, tmp_path).prepare(consent())
        no_writes(runner)

    def test_a_volume_that_keeps_its_container_busy_stops_the_install_with_the_cryptsetup_reason(self, tmp_path):
        runner = StatefulRunner([used_disk(lvm_stack(sticky=True))])
        with pytest.raises(StorageError, match="cryptsetup close failed \\(5: device already exists or is busy\\)"):
            make_manager(runner, tmp_path).prepare(consent())
        no_writes(runner)

    def test_a_mounted_volume_stays_refused_even_with_consent(self, tmp_path):
        runner = StatefulRunner([used_disk(lvm_stack(mounted=True))])
        with pytest.raises(StorageError, match="mounted"):
            make_manager(runner, tmp_path).prepare(consent())
        no_writes(runner)
        assert not any(c[0] in {"vgchange", "cryptsetup"} for c in runner.commands)

    def test_md_arrays_are_stopped_and_firmware_containers_last(self, tmp_path):
        volume = Holder("md126", "raid1")
        container = Holder("md127", "md", children=(volume,))
        runner = StatefulRunner([vda(partitions=[{"num": 1, "size": 40 * GiB, "fstype": "isw_raid_member", "holders": [container]}])])
        runner.mdadm_examine["/dev/vda1"] = "  Magic : Intel Raid ISM Cfg Sig.\n  UUID : imsm-1\n"
        make_manager(runner, tmp_path).prepare(consent())
        stops = [c for c in runner.commands if c[:2] == ("mdadm", "--stop")]
        assert stops == [("mdadm", "--stop", "/dev/md126"), ("mdadm", "--stop", "/dev/md127")]

    def test_other_device_mapper_nodes_are_removed(self, tmp_path):
        runner = StatefulRunner([vda(partitions=[{"num": 1, "size": 40 * GiB, "fstype": "ext4", "holders": [Holder("mpatha", "mpath")]}])])
        make_manager(runner, tmp_path).prepare(consent())
        assert ("dmsetup", "remove", "--retry", "mpatha") in runner.commands

    def test_active_swap_on_the_target_is_released_only_with_consent(self, tmp_path):
        runner = StatefulRunner([vda(partitions=[{"num": 1, "size": 4 * GiB, "fstype": "swap"}])])
        runner.swaps.add("/dev/vda1")
        with pytest.raises(StorageError, match="Swap is active on /dev/vda1"):
            make_manager(runner, tmp_path).prepare(erase())
        no_writes(runner)
        make_manager(runner, tmp_path).prepare(consent())
        text = commands_text(runner)
        assert text.index("swapoff /dev/vda1") < text.index("sgdisk --zap-all /dev/vda")

    def test_manual_mode_applies_the_same_rules_to_the_rows_that_are_formatted(self, tmp_path):
        from tests.backend.test_storage_identity import ESP_ROW, machine, manual
        from gentoo_installer.manifest import Partition

        disks = machine()
        disks[1].parts[0]["holders"] = [Holder("vgdata-lv", "lvm", vg="vgdata")]
        runner = StatefulRunner(disks)
        rows = (ESP_ROW, Partition("/dev/sdb1", "/", "ext4", format=True))
        with pytest.raises(StorageError, match="Active volumes"):
            make_manager(runner, tmp_path).prepare(manual(*rows, additional_device_ids=["SER-B"]))
        no_writes(runner)
        both = manual(*rows, additional_device_ids=["SER-B"], release_holders=True)
        make_manager(runner, tmp_path).prepare(both)
        assert ("vgchange", "-an", "vgdata") in runner.commands
        assert ("mkfs.ext4", "-F", "/dev/sdb1") in runner.commands


class TestSpanningSets:
    """The live system does not assemble arrays or activate volume groups on its own, so lsblk shows nothing
    active: pvs, blkid and mdadm --examine have to find the sets the disk belongs to."""

    def two_disks(self, **kwargs):
        a = vda(partitions=[{"num": 1, "size": 40 * GiB, "fstype": kwargs.pop("fstype", "LVM2_member")}])
        b = FakeDisk("/dev/vdb", 80 * GiB, serial="SER-B", partitions=[{"num": 1, "size": 40 * GiB, "fstype": a.parts[0]["fstype"]}])
        return a, b

    def test_a_volume_group_spanning_another_disk_needs_consent(self, tmp_path):
        a, b = self.two_disks()
        runner = StatefulRunner([a, b])
        runner.pvs = [("/dev/vda1", "vg0"), ("/dev/vdb1", "vg0")]
        with pytest.raises(StorageError, match="LVM volume group vg0.*also on /dev/vdb1.*destroy data stored on /dev/vdb1") as caught:
            make_manager(runner, tmp_path).prepare(erase())
        assert caught.value.code == "storage.holders"
        no_writes(runner)
        manager = make_manager(runner, tmp_path)
        manager.prepare(consent())
        assert any("Consent given" in note and "vg0" in note for note in manager.notes)

    def test_a_volume_group_wholly_on_the_target_is_destroyed_with_it_without_a_prompt(self, tmp_path):
        a, b = self.two_disks()
        b.parts[0]["fstype"] = "ext4"
        runner = StatefulRunner([a, b])
        runner.pvs = [("/dev/vda1", "vg0")]
        manager = make_manager(runner, tmp_path)
        manager.prepare(erase())
        assert any("vg0" in note and "destroyed together with the disk" in note for note in manager.notes)

    def test_an_md_array_with_a_member_on_another_disk_needs_consent(self, tmp_path):
        a, b = self.two_disks(fstype="linux_raid_member")
        runner = StatefulRunner([a, b])
        runner.mdadm_examine = {"/dev/vda1": "   Array UUID : aaaa:bbbb\n", "/dev/vdb1": "   Array UUID : aaaa:bbbb\n"}
        with pytest.raises(StorageError, match="RAID array aaaa:bbbb.*also on /dev/vdb1"):
            make_manager(runner, tmp_path).prepare(erase())
        no_writes(runner)
        make_manager(runner, tmp_path).prepare(consent())
        assert ("mdadm", "--examine", "/dev/vdb1") in runner.commands

    def test_a_member_of_a_different_array_does_not_count(self, tmp_path):
        a, b = self.two_disks(fstype="linux_raid_member")
        runner = StatefulRunner([a, b])
        runner.mdadm_examine = {"/dev/vda1": "   Array UUID : aaaa:bbbb\n", "/dev/vdb1": "   Array UUID : cccc:dddd\n"}
        make_manager(runner, tmp_path).prepare(erase())

    def test_a_leaf_without_a_signature_in_the_inventory_is_probed_explicitly(self, tmp_path):
        a, b = self.two_disks()
        a.parts[0]["fstype"] = None  # the udev database has not seen it
        runner = StatefulRunner([a, b])
        runner.pvs = [("/dev/vda1", "vg0"), ("/dev/vdb1", "vg0")]
        # blkid --probe reads the partition itself; the fake answers from its own table, so give it a signature there
        a.parts[0]["fstype"] = "LVM2_member"
        a_json_hidden = runner.lsblk_json

        def without_fstype():
            import json

            data = json.loads(a_json_hidden())
            data["blockdevices"][0]["children"][0]["fstype"] = None
            return json.dumps(data)

        runner.lsblk_json = without_fstype
        with pytest.raises(StorageError, match="volume group vg0"):
            make_manager(runner, tmp_path).prepare(erase())
        assert ("blkid", "--probe", "--output", "value", "--match-tag", "TYPE", "/dev/vda1") in runner.commands


class TestStaleSignatures:
    def test_new_partitions_are_wiped_after_the_table_is_written_and_before_mkfs(self, tmp_path):
        runner = StatefulRunner([vda()])
        make_manager(runner, tmp_path).prepare(erase())
        text = commands_text(runner)
        probe = text.index("partprobe /dev/vda")
        wipes = [text.index(f"wipefs --all /dev/vda{n}") for n in (1, 2, 3)]
        assert all(probe < index < text.index("mkfs.vfat -F 32 /dev/vda1") for index in wipes)
        assert text[probe + 1] == "udevadm settle" and text[wipes[-1] + 1] == "udevadm settle"

    def test_old_partitions_are_wiped_before_the_table_is_destroyed(self, tmp_path):
        old = vda(partitions=[{"num": n, "size": 10 * GiB, "fstype": "ext4"} for n in (1, 2, 3)])
        runner = StatefulRunner([old])
        make_manager(runner, tmp_path).prepare(erase())
        text = commands_text(runner)
        zap = text.index("sgdisk --zap-all /dev/vda")
        assert [text.index(f"wipefs --all /dev/vda{n}") for n in (1, 2, 3)] == [0, 1, 2]
        assert zap == 3 + text[3:].index("sgdisk --zap-all /dev/vda")

    def test_an_mbr_extended_container_is_not_wiped_but_its_logical_partitions_are(self, tmp_path):
        old = vda(pttype="dos", partitions=[
            {"num": 1, "size": 10 * GiB, "fstype": "ext4"},
            {"num": 2, "size": 1024, "fstype": None},  # the extended container lsblk shows as a 1 KiB "part"
            {"num": 5, "size": 10 * GiB, "fstype": "ext4"},
        ])
        runner = StatefulRunner([old])
        make_manager(runner, tmp_path).prepare(erase())
        wiped_before = [c[-1] for c in runner.commands[: next(i for i, c in enumerate(runner.commands) if c[0] in {"sgdisk", "parted"})] if c[0] == "wipefs"]
        assert wiped_before == ["/dev/vda1", "/dev/vda5"]

    def test_a_dos_erase_clears_the_stale_backup_gpt_before_mklabel_and_only_force_wipes_the_disk(self, tmp_path):
        runner = StatefulRunner([vda(partitions=[{"num": 1, "size": 10 * GiB, "fstype": "ext4"}])])
        make_manager(runner, tmp_path).prepare(erase(table="dos", firmware="bios"))
        text = commands_text(runner)
        assert text.index("wipefs --all --force /dev/vda") < text.index("parted --script /dev/vda mklabel msdos")
        assert [c for c in runner.commands if c[0] == "wipefs" and "--force" in c] == [("wipefs", "--all", "--force", "/dev/vda")]

    def test_dry_run_lists_the_wipes_without_an_inventory(self, tmp_path):
        runner = StatefulRunner([vda()])
        make_manager(runner, tmp_path).prepare(erase(), dry_run=True)
        assert runner.commands[0] == ("sgdisk", "--zap-all", "/dev/vda")


class TestNameCollisions:
    def test_an_active_mapper_with_our_name_is_refused_before_any_write(self, tmp_path):
        runner = StatefulRunner([vda()])
        manager = make_manager(runner, tmp_path)
        (tmp_path / "dev" / "mapper" / "gentoo-root").write_text("")
        m = erase(luks=True, luks_passphrase="long enough passphrase")
        with pytest.raises(StorageError, match="gentoo-root is already active"):
            manager.prepare(m)
        no_writes(runner)

    def test_the_mapper_being_released_is_not_a_collision(self, tmp_path):
        old = Holder("gentoo-root", "crypt")
        runner = StatefulRunner([vda(partitions=[{"num": 1, "size": 40 * GiB, "fstype": "crypto_LUKS", "holders": [old]}])])
        manager = make_manager(runner, tmp_path)
        (tmp_path / "dev" / "mapper" / "gentoo-root").write_text("")
        m = erase(luks=True, luks_passphrase="long enough passphrase", release_holders=True)
        with pytest.raises(StorageError, match="Mapper already exists"):
            manager.prepare(m)  # released, but the fake leaves the /dev node: the later guard still protects the new open
        assert ("cryptsetup", "close", "gentoo-root") in runner.commands

    def test_a_volume_group_named_gentoo_on_another_disk_is_refused(self, tmp_path):
        other = FakeDisk("/dev/vdb", 80 * GiB, serial="SER-B", partitions=[{"num": 1, "size": 40 * GiB, "fstype": "LVM2_member"}])
        runner = StatefulRunner([vda(), other])
        runner.pvs = [("/dev/vdb1", "gentoo")]
        with pytest.raises(StorageError, match="volume group named 'gentoo' already exists on /dev/vdb1"):
            make_manager(runner, tmp_path).prepare(erase(lvm=True))
        no_writes(runner)

    def test_a_volume_group_named_gentoo_on_the_erased_disk_is_fine(self, tmp_path):
        old = vda(partitions=[{"num": 1, "size": 40 * GiB, "fstype": "LVM2_member"}])
        runner = StatefulRunner([old])
        runner.pvs = [("/dev/vda1", "gentoo")]
        make_manager(runner, tmp_path).prepare(erase(lvm=True))
        assert ("vgcreate", "gentoo", "/dev/vda3") in runner.commands


class TestCapacity:
    def test_the_default_install_on_a_small_disk_is_refused_before_anything_is_written(self, tmp_path):
        runner = StatefulRunner([vda()])
        runner.disks["/dev/vda"].size = 8 * GiB
        with pytest.raises(StorageError, match="disk is too small for this layout: 8.0 GiB minus EFI system partition 1.0 GiB, swap 4.0 GiB leaves 3.0 GiB") as caught:
            make_manager(runner, tmp_path).prepare(erase())
        assert caught.value.code == "storage.capacity"
        no_writes(runner)
        assert not any(c[0] in {"wipefs", "sgdisk"} for c in runner.commands)

    def test_an_oversized_home_partition_is_refused_with_no_destructive_command(self, tmp_path):
        runner = StatefulRunner([vda()])
        with pytest.raises(StorageError, match="/home 70.0 GiB"):
            make_manager(runner, tmp_path).prepare(erase(separate_home=True, home_mib=70 * 1024))
        no_writes(runner)

    def test_a_dos_table_cannot_address_a_4_tib_disk(self, tmp_path):
        runner = StatefulRunner([FakeDisk("/dev/vda", 4 * 1024**4, serial="SER")])
        with pytest.raises(StorageError, match="cannot address more than 2 TiB.*choose GPT"):
            make_manager(runner, tmp_path).prepare(erase(table="dos", firmware="bios"))
        no_writes(runner)

    def test_a_4k_native_disk_addresses_more_with_dos(self, tmp_path):
        runner = StatefulRunner([FakeDisk("/dev/vda", 4 * 1024**4, serial="SER", log_sec=4096)])
        make_manager(runner, tmp_path).prepare(erase(table="dos", firmware="bios"))
        assert ("parted", "--script", "/dev/vda", "mklabel", "msdos") in runner.commands

    def test_a_disk_that_fits_installs_and_warns_when_roomy_but_not_recommended(self, tmp_path):
        runner = StatefulRunner([FakeDisk("/dev/vda", 34 * GiB, serial="SER")])
        manager = make_manager(runner, tmp_path)
        manager.prepare(erase())
        assert any("40 GiB or more is recommended for kde" in note for note in manager.notes)

    def test_vfat_on_a_4k_native_disk_gets_an_explicit_sector_size(self, tmp_path):
        runner = StatefulRunner([FakeDisk("/dev/vda", 80 * GiB, serial="SER", log_sec=4096)])
        make_manager(runner, tmp_path).prepare(erase())
        assert ("mkfs.vfat", "-F", "32", "-S", "4096", "/dev/vda1") in runner.commands

    @pytest.mark.parametrize("size_gib", [40, 100, 700])
    @pytest.mark.parametrize(
        "changes",
        [
            {},
            {"swap_mib": 0},
            {"firmware": "bios"},
            {"firmware": "bios", "table": "dos"},
            {"luks": True, "luks_passphrase": "long enough passphrase"},
            {"separate_home": True, "home_mib": 8 * 1024},
            {"firmware": "bios", "table": "dos", "swap_mib": 2048, "separate_home": True, "home_mib": 4096},
        ],
    )
    def test_the_estimate_never_exceeds_what_the_partitioner_really_leaves_for_root(self, tmp_path, size_gib, changes):
        disk = FakeDisk("/dev/vda", size_gib * GiB, serial="SER")
        runner = StatefulRunner([disk])
        m = erase(**changes)
        m.system.desktop = "minimal"
        make_manager(runner, tmp_path).prepare(m)
        plan = guided_layout.guided_plan(m.storage, size_gib * GiB)
        real_mib = root_partition_size(runner, "/dev/vda") // MiB
        assert 0 <= real_mib - plan.root_mib <= 3, (plan, real_mib)
