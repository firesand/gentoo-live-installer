"""Which disks a install may touch: the identity pin (S4, G3), manual-mode approvals (S1), the live medium."""

from __future__ import annotations

import json

import pytest

from gentoo_installer import blockdev
from gentoo_installer.manifest import InstallManifest, Partition
from gentoo_installerd.storage import StorageError

from tests.backend.helpers import manifest
from tests.backend.test_storage_support import (
    ESP,
    FakeDisk,
    GiB,
    Holder,
    MiB,
    StatefulRunner,
    first_write,
    link_by_path,
    make_manager,
)


def erase(device="/dev/vda", device_id="SER", **changes) -> InstallManifest:
    m = manifest()
    m.storage.device = device
    m.storage.device_id = device_id
    for name, value in changes.items():
        setattr(m.storage, name, value)
    return m


def vda(**kwargs) -> FakeDisk:
    return FakeDisk("/dev/vda", 80 * GiB, **{"serial": "SER", "wwn": "WWN-1", **kwargs})


def no_writes(runner: StatefulRunner) -> None:
    assert first_write(runner.commands) is None, [c for c in runner.commands if c[0] != "lsblk"]


class TestPinRequirement:
    def test_kernel_name_as_the_pin_does_not_match_a_real_disk(self, tmp_path):
        runner = StatefulRunner([vda()])
        with pytest.raises(StorageError, match="identity") as caught:
            make_manager(runner, tmp_path).prepare(erase(device_id="/dev/vda"))
        assert caught.value.code == "storage.identity"
        no_writes(runner)

    @pytest.mark.parametrize("pin", ["SER", "WWN-1"])
    def test_serial_or_wwn_pins_the_disk(self, tmp_path, pin):
        runner = StatefulRunner([vda()])
        make_manager(runner, tmp_path).prepare(erase(device_id=pin))
        assert ("sgdisk", "--zap-all", "/dev/vda") in runner.commands

    def test_wrong_pin_is_refused_before_any_write(self, tmp_path):
        runner = StatefulRunner([vda()])
        with pytest.raises(StorageError, match="identity does not match"):
            make_manager(runner, tmp_path).prepare(erase(device_id="OTHER"))
        no_writes(runner)

    def test_unpinned_manifest_is_refused_before_the_disk_is_even_listed(self, tmp_path):
        runner = StatefulRunner([vda()])
        with pytest.raises(StorageError, match="does not pin the target disk") as caught:
            make_manager(runner, tmp_path).prepare(erase(device_id=""))
        assert caught.value.code == "storage.unpinned"
        assert "storage.device_id_kind" in str(caught.value) and "--allow-unverified-disk" in str(caught.value)
        assert not any(c[0] == "lsblk" for c in runner.commands)

    def test_unpinned_manifest_is_allowed_only_by_the_explicit_operator_override(self, tmp_path):
        runner = StatefulRunner([vda(serial="", wwn="")])
        make_manager(runner, tmp_path, allow_unverified_disk=True).prepare(erase(device_id=""))
        assert ("sgdisk", "--zap-all", "/dev/vda") in runner.commands

    def test_the_override_does_not_excuse_a_wrong_pin(self, tmp_path):
        runner = StatefulRunner([vda()])
        with pytest.raises(StorageError, match="identity"):
            make_manager(runner, tmp_path, allow_unverified_disk=True).prepare(erase(device_id="OTHER"))
        no_writes(runner)

    def test_loop_device_may_be_pinned_by_its_path(self, tmp_path):
        runner = StatefulRunner([FakeDisk("/dev/loop7", 80 * GiB, kind="loop")])
        make_manager(runner, tmp_path).prepare(erase(device="/dev/loop7", device_id="/dev/loop7"))
        assert ("sgdisk", "--zap-all", "/dev/loop7") in runner.commands

    def test_identity_is_rechecked_after_volumes_are_released_and_before_the_first_write(self, tmp_path):
        lv = Holder("vg0-root", "lvm", vg="vg0")
        runner = StatefulRunner([vda(partitions=[{"num": 1, "size": 40 * GiB, "fstype": "LVM2_member", "holders": [lv]}])])

        def swap_disk(r, argv):  # the disk is exchanged while the volume group is being released
            r.disks["/dev/vda"].serial, r.disks["/dev/vda"].wwn = "ANOTHER", "ANOTHER-WWN"

        runner.on("vgchange -an", swap_disk)
        m = erase()
        m.storage.release_holders = True
        with pytest.raises(StorageError, match="Disk identity changed"):
            make_manager(runner, tmp_path).prepare(m)
        no_writes(runner)

    def test_a_disk_swapped_during_partitioning_is_detected_after_the_layout(self, tmp_path):
        runner = StatefulRunner([vda()])

        def swap_disk(r, argv):
            r.disks["/dev/vda"].serial = "ANOTHER"

        runner.on("sgdisk --zap-all", swap_disk)
        with pytest.raises(StorageError, match="Disk identity changed"):
            make_manager(runner, tmp_path).prepare(erase())
        assert not any(c[0] == "mount" for c in runner.commands)


def serial_less_vda(**kwargs) -> FakeDisk:
    return FakeDisk("/dev/vda", 80 * GiB, model="QEMU HARDDISK", **kwargs)


def path_pin(runner: StatefulRunner, disk: str = "/dev/vda", link: str = "pci-0000:00:04.0") -> str:
    node = blockdev.find(blockdev.parse_lsblk(runner.lsblk_json()), disk)
    return blockdev.path_identity(node, link).value


class TestSharedSerials:
    def twins(self):
        return [vda(), FakeDisk("/dev/vdb", 80 * GiB, serial="SER", wwn="WWN-2")]

    def test_a_serial_two_disks_share_cannot_pin_either_of_them(self, tmp_path):
        runner = StatefulRunner(self.twins())
        with pytest.raises(StorageError, match="/dev/vda cannot be told apart from /dev/vdb.*device_id_kind"):
            make_manager(runner, tmp_path).prepare(erase())
        no_writes(runner)

    def test_the_unique_wwn_of_the_same_disks_still_works(self, tmp_path):
        runner = StatefulRunner(self.twins())
        make_manager(runner, tmp_path).prepare(erase(device_id="WWN-1"))
        assert ("sgdisk", "--zap-all", "/dev/vda") in runner.commands

    def test_the_same_goes_for_manual_approvals(self, tmp_path):
        disks = machine()
        disks[1].serial = "SER-A"
        with pytest.raises(StorageError, match="cannot be told apart"):
            make_manager(StatefulRunner(disks), tmp_path).prepare(manual(ESP_ROW, ROOT_ON_B, additional_device_ids=["SER-A"]))


class TestPathIdentity:
    """Disks without serial or WWN (virtio-blk without serial=, some USB bridges) are pinned by bus path, size and model."""

    def prepare(self, tmp_path, runner, **changes):
        link_by_path(tmp_path, "pci-0000:00:04.0", "/dev/vda")
        m = erase(device_id=path_pin(runner), device_id_kind="path", **changes)
        return make_manager(runner, tmp_path).prepare(m)

    def test_a_serial_less_disk_installs_with_its_path_identity(self, tmp_path):
        runner = StatefulRunner([serial_less_vda()])
        self.prepare(tmp_path, runner)
        assert ("sgdisk", "--zap-all", "/dev/vda") in runner.commands

    def test_without_a_path_identity_the_refusal_says_how_to_get_one(self, tmp_path):
        runner = StatefulRunner([serial_less_vda()])
        with pytest.raises(StorageError, match="no serial number or WWN.*device_id_kind"):
            make_manager(runner, tmp_path).prepare(erase(device_id="SER"))
        no_writes(runner)

    def test_the_by_path_link_must_point_at_this_disk(self, tmp_path):
        runner = StatefulRunner([serial_less_vda(), FakeDisk("/dev/vdb", 80 * GiB, model="QEMU HARDDISK")])
        link_by_path(tmp_path, "pci-0000:00:05.0", "/dev/vdb")
        pin = blockdev.path_identity(blockdev.find(blockdev.parse_lsblk(runner.lsblk_json()), "/dev/vda"), "pci-0000:00:05.0").value
        with pytest.raises(StorageError, match="identity"):
            make_manager(runner, tmp_path).prepare(erase(device_id=pin, device_id_kind="path"))
        no_writes(runner)

    @pytest.mark.parametrize("size,model", [(80 * GiB + 512, "QEMU HARDDISK"), (80 * GiB, "OTHER MODEL")])
    def test_size_and_model_must_match_exactly(self, tmp_path, size, model):
        runner = StatefulRunner([serial_less_vda()])
        link_by_path(tmp_path, "pci-0000:00:04.0", "/dev/vda")
        with pytest.raises(StorageError, match="identity"):
            make_manager(runner, tmp_path).prepare(erase(device_id=f"pci-0000:00:04.0|{size}|{model}", device_id_kind="path"))
        no_writes(runner)

    def test_a_missing_link_is_refused(self, tmp_path):
        runner = StatefulRunner([serial_less_vda()])
        with pytest.raises(StorageError, match="identity"):
            make_manager(runner, tmp_path).prepare(erase(device_id=path_pin(runner), device_id_kind="path"))
        no_writes(runner)

    def test_a_partition_link_never_identifies_the_disk(self, tmp_path):
        runner = StatefulRunner([serial_less_vda()])
        link_by_path(tmp_path, "pci-0000:00:04.0-part1", "/dev/vda1")
        pin = f"pci-0000:00:04.0-part1|{80 * GiB}|QEMU HARDDISK"
        with pytest.raises(StorageError, match="identity"):
            make_manager(runner, tmp_path).prepare(erase(device_id=pin, device_id_kind="path"))

    def test_the_live_medium_is_never_accepted_even_with_a_valid_path_identity(self, tmp_path):
        runner = StatefulRunner([serial_less_vda(partitions=[{"num": 1, "size": 4 * GiB, "fstype": "iso9660", "label": "GENTOO_INSTALLER_AMD64"}])])
        (tmp_path / "cmdline").write_text("BOOT_IMAGE=/boot/gentoo root=live:CDLABEL=GENTOO_INSTALLER_AMD64 rd.live.image\n")
        link_by_path(tmp_path, "pci-0000:00:04.0", "/dev/vda")
        with pytest.raises(StorageError, match="live installer medium") as caught:
            make_manager(runner, tmp_path).prepare(erase(device_id=path_pin(runner), device_id_kind="path"))
        assert caught.value.code == "storage.live_medium"
        no_writes(runner)

    def test_a_disk_with_a_serial_may_also_use_a_path_identity(self, tmp_path):
        runner = StatefulRunner([vda(model="QEMU HARDDISK")])
        self.prepare(tmp_path, runner)
        assert ("sgdisk", "--zap-all", "/dev/vda") in runner.commands

    def test_the_receipt_remembers_the_bus_path_and_resume_checks_it(self, tmp_path):
        runner = StatefulRunner([serial_less_vda()])
        manager = make_manager(runner, tmp_path)
        link_by_path(tmp_path, "pci-0000:00:04.0", "/dev/vda")
        m = erase(device_id=path_pin(runner), device_id_kind="path")
        manager.prepare(m)
        receipt = json.loads(json.dumps(manager.snapshot()))
        assert receipt["signatures"]["/dev/vda"] == ["", "", 80 * GiB, "pci-0000:00:04.0"]
        manager.cleanup()
        again = make_manager(runner, tmp_path)
        again.restore(m, manager.layout, receipt)
        assert {str(p) for p in again.owned_mounts} == {str(tmp_path / "target"), str(tmp_path / "target" / "efi")}
        # the same disk on another bus slot is not the same disk
        (tmp_path / "by-path" / "pci-0000:00:04.0").unlink()
        link_by_path(tmp_path, "pci-0000:00:09.0", "/dev/vda")
        moved = make_manager(StatefulRunner([serial_less_vda()]), tmp_path)
        with pytest.raises(StorageError, match="Saved disk identity changed"):
            moved.restore(m, manager.layout, receipt)

    def test_old_three_element_receipts_still_restore(self, tmp_path):
        runner = StatefulRunner([vda()])
        manager = make_manager(runner, tmp_path)
        manager.prepare(erase())
        receipt = json.loads(json.dumps(manager.snapshot()))
        receipt["signatures"] = {"/dev/vda": ["SER", "WWN-1", 80 * GiB]}
        receipt.pop("entries")
        manager.cleanup()
        make_manager(runner, tmp_path).restore(erase(), manager.layout, receipt)


class TestTargetsThatAreNotDisks:
    @pytest.mark.parametrize(
        ("disk", "fragment"),
        [
            (FakeDisk("/dev/zram0", 8 * GiB, serial="Z"), "virtual or placeholder"),
            (FakeDisk("/dev/vda", 0, serial="SER"), "no media"),
            (FakeDisk("/dev/vda", 80 * GiB, serial="SER", ro=True), "read-only"),
            (FakeDisk("/dev/mmcblk0boot0", 4 * MiB, serial="SER"), "eMMC boot"),
        ],
    )
    def test_refused_before_any_write(self, tmp_path, disk, fragment):
        runner = StatefulRunner([disk])
        with pytest.raises(StorageError, match=fragment):
            make_manager(runner, tmp_path).prepare(erase(device=disk.path))
        no_writes(runner)


class TestLiveMedium:
    def stick(self) -> FakeDisk:
        return FakeDisk("/dev/sdb", 16 * GiB, serial="STICK", partitions=[{"num": 1, "size": 4 * GiB, "fstype": "iso9660", "label": "GENTOO_INSTALLER_AMD64"}])

    def test_the_disk_holding_the_medium_is_found_by_its_mount_source(self, tmp_path):
        runner = StatefulRunner([vda(), self.stick()])
        runner.findmnt_sources["/run/initramfs/live"] = "/dev/sdb1"
        with pytest.raises(StorageError, match="live installer medium") as caught:
            make_manager(runner, tmp_path).prepare(erase(device="/dev/sdb", device_id="STICK"))
        assert caught.value.code == "storage.live_medium"
        no_writes(runner)

    def test_a_medium_copied_to_ram_is_found_by_the_label_on_the_kernel_command_line(self, tmp_path):
        runner = StatefulRunner([vda(), self.stick()])
        (tmp_path / "cmdline").write_text("root=live:CDLABEL=GENTOO_INSTALLER_AMD64 rd.live.image rd.live.ram=1\n")
        with pytest.raises(StorageError, match="/dev/sdb .*live installer medium"):
            make_manager(runner, tmp_path).prepare(erase(device="/dev/sdb", device_id="STICK"))
        no_writes(runner)

    def test_other_disks_are_still_installable_while_the_medium_is_attached(self, tmp_path):
        runner = StatefulRunner([vda(), self.stick()])
        (tmp_path / "cmdline").write_text("root=live:CDLABEL=GENTOO_INSTALLER_AMD64 rd.live.image\n")
        make_manager(runner, tmp_path).prepare(erase())
        assert ("sgdisk", "--zap-all", "/dev/vda") in runner.commands


def machine(*, second_serial="SER-B", second_wwn="", second_model="WDC DISK") -> list[FakeDisk]:
    return [
        FakeDisk("/dev/sda", 500 * GiB, serial="SER-A", wwn="WWN-A", model="Samsung SSD", partitions=[
            {"num": 1, "size": 512 * MiB, "fstype": "vfat", "parttype": ESP},
            {"num": 2, "size": 400 * GiB, "fstype": "ntfs", "label": "Windows"},
        ]),
        FakeDisk("/dev/sdb", 1000 * GiB, serial=second_serial, wwn=second_wwn, model=second_model, partitions=[
            {"num": 1, "size": 100 * GiB, "fstype": "ext4"},
            {"num": 2, "size": 50 * GiB, "fstype": "ntfs", "label": "Data"},
        ]),
    ]


def manual(*rows: Partition, device="/dev/sda", device_id="SER-A", **changes) -> InstallManifest:
    m = manifest()
    m.storage.mode = "manual"
    m.storage.device = device
    m.storage.device_id = device_id
    m.storage.swap_mib = 0
    m.storage.partitions = list(rows)
    for name, value in changes.items():
        setattr(m.storage, name, value)
    return m


ESP_ROW = Partition("/dev/sda1", "/efi", "vfat", format=False)
ROOT_ON_B = Partition("/dev/sdb1", "/", "ext4", format=True)


class TestManualApprovals:
    def test_a_row_on_a_disk_nobody_approved_is_refused_before_any_write(self, tmp_path):
        runner = StatefulRunner(machine())
        with pytest.raises(StorageError, match="was not approved") as caught:
            make_manager(runner, tmp_path).prepare(manual(ESP_ROW, ROOT_ON_B))
        message = str(caught.value)
        assert caught.value.code == "storage.not_approved"
        assert "/dev/sdb" in message and "WDC DISK" in message and "…SER-B"[-6:] in message and "additional_device_ids" in message
        no_writes(runner)

    def test_additional_device_ids_approve_a_second_disk(self, tmp_path):
        runner = StatefulRunner(machine())
        make_manager(runner, tmp_path).prepare(manual(ESP_ROW, ROOT_ON_B, additional_device_ids=["SER-B"]))
        assert ("mkfs.ext4", "-F", "/dev/sdb1") in runner.commands

    def test_a_wwn_counts_as_an_approval(self, tmp_path):
        runner = StatefulRunner(machine(second_serial="", second_wwn="0x5000ABC"))
        make_manager(runner, tmp_path).prepare(manual(ESP_ROW, ROOT_ON_B, additional_device_ids=["0x5000ABC"]))
        assert ("mkfs.ext4", "-F", "/dev/sdb1") in runner.commands

    def test_an_approval_for_another_disk_does_not_help(self, tmp_path):
        runner = StatefulRunner(machine())
        with pytest.raises(StorageError, match="was not approved"):
            make_manager(runner, tmp_path).prepare(manual(ESP_ROW, ROOT_ON_B, additional_device_ids=["SER-C", "SER-A"]))
        no_writes(runner)

    def test_the_kernel_name_is_not_an_approval(self, tmp_path):
        runner = StatefulRunner(machine())
        with pytest.raises(StorageError, match="was not approved"):
            make_manager(runner, tmp_path).prepare(manual(ESP_ROW, ROOT_ON_B, additional_device_ids=["/dev/sdb"]))

    def test_a_serial_less_second_disk_is_approved_by_its_path_identity(self, tmp_path):
        runner = StatefulRunner(machine(second_serial=""))
        link_by_path(tmp_path, "pci-0000:00:1f.2-ata-2", "/dev/sdb")
        node = blockdev.find(blockdev.parse_lsblk(runner.lsblk_json()), "/dev/sdb")
        approval = blockdev.path_identity(node, "pci-0000:00:1f.2-ata-2").value
        make_manager(runner, tmp_path).prepare(manual(ESP_ROW, ROOT_ON_B, additional_device_ids=[approval]))
        assert ("mkfs.ext4", "-F", "/dev/sdb1") in runner.commands
        manager = make_manager(StatefulRunner(machine(second_serial="")), tmp_path)
        with pytest.raises(StorageError, match="was not approved"):
            manager.prepare(manual(ESP_ROW, ROOT_ON_B, additional_device_ids=[approval.replace("|", "|1")]))

    def test_the_selected_disk_must_hold_one_of_the_rows(self, tmp_path):
        runner = StatefulRunner(machine())
        with pytest.raises(StorageError, match="holds none of the manual partitions"):
            make_manager(runner, tmp_path).prepare(manual(Partition("/dev/sdb1", "/", "ext4"), Partition("/dev/sdb2", "/efi", "vfat", format=False), additional_device_ids=["SER-B"]))
        no_writes(runner)

    def test_a_wrong_pin_on_the_selected_disk_keeps_the_identity_mismatch_message(self, tmp_path):
        runner = StatefulRunner(machine())
        with pytest.raises(StorageError, match="Target disk identity does not match the approved manifest"):
            make_manager(runner, tmp_path).prepare(manual(ESP_ROW, ROOT_ON_B, device_id="NOT-A", additional_device_ids=["SER-B"]))
        no_writes(runner)

    def test_the_override_waives_the_approval_but_not_the_other_guards(self, tmp_path):
        runner = StatefulRunner(machine())
        make_manager(runner, tmp_path, allow_unverified_disk=True).prepare(manual(ESP_ROW, ROOT_ON_B))
        assert ("mkfs.ext4", "-F", "/dev/sdb1") in runner.commands
        mounted = StatefulRunner(machine())
        mounted.mounts["/mnt/elsewhere"] = ("/dev/sdb1", "")
        with pytest.raises(StorageError, match="mounted"):
            make_manager(mounted, tmp_path, allow_unverified_disk=True).prepare(manual(ESP_ROW, ROOT_ON_B))
        stick = machine() + [FakeDisk("/dev/sdc", 16 * GiB, serial="STICK", partitions=[{"num": 1, "size": 4 * GiB, "fstype": "ext4"}])]
        live = StatefulRunner(stick)
        live.findmnt_sources["/run/initramfs/live"] = "/dev/sdc1"
        with pytest.raises(StorageError, match="live installer medium"):
            make_manager(live, tmp_path, allow_unverified_disk=True).prepare(manual(ESP_ROW, Partition("/dev/sdc1", "/", "ext4")))

    def test_an_assembled_array_needs_every_member_disk_approved(self, tmp_path):
        disks = machine()
        array = Holder("md0", "raid1", path="/dev/md0", size=100 * GiB)
        for disk, number in ((disks[0], 3), (disks[1], 3)):
            disk.parts.append({"num": number, "size": 100 * GiB, "fstype": "linux_raid_member", "holders": [array]})
        rows = (ESP_ROW, Partition("/dev/md0", "/", "ext4", format=False))
        runner = StatefulRunner(disks)
        with pytest.raises(StorageError, match="/dev/sdb .*was not approved"):
            make_manager(runner, tmp_path).prepare(manual(*rows, device="/dev/sda"))
        no_writes(runner)
        make_manager(StatefulRunner(machine_with_array()), tmp_path).prepare(manual(*rows, additional_device_ids=["SER-B"]))

    def test_an_install_onto_an_assembled_array_pins_one_member_and_approves_the_other(self, tmp_path):
        rows = (ESP_ROW, Partition("/dev/md0", "/", "ext4", format=False))
        m = manual(*rows, device="/dev/md0", device_id="SER-A", additional_device_ids=["SER-B"], raid_level="1")
        make_manager(StatefulRunner(machine_with_array()), tmp_path).prepare(m)
        runner = StatefulRunner(machine_with_array())
        with pytest.raises(StorageError, match="Target disk identity does not match the approved manifest"):
            make_manager(runner, tmp_path).prepare(manual(*rows, device="/dev/md0", device_id="NOT-A-MEMBER", additional_device_ids=["SER-B"], raid_level="1"))
        no_writes(runner)

    def test_erase_mode_needs_a_whole_disk(self, tmp_path):
        runner = StatefulRunner(machine())
        with pytest.raises(StorageError, match="Erase mode requires a whole disk"):
            make_manager(runner, tmp_path).prepare(erase(device="/dev/sdb1", device_id="SER-B"))
        no_writes(runner)

    def test_rows_must_exist_on_this_machine(self, tmp_path):
        with pytest.raises(StorageError, match="was not found"):
            make_manager(StatefulRunner(machine()), tmp_path).prepare(manual(ESP_ROW, Partition("/dev/sdq9", "/", "ext4")))


def machine_with_array() -> list[FakeDisk]:
    disks = machine()
    array = Holder("md0", "raid1", path="/dev/md0", size=100 * GiB)
    for disk in disks:
        disk.parts.append({"num": 3, "size": 100 * GiB, "fstype": "linux_raid_member", "holders": [array]})
    return disks
