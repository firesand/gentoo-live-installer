"""Capacity planner and plan description (gentoo_installer.layout)."""

from __future__ import annotations

import json

import pytest

from gentoo_installer import blockdev, layout
from gentoo_installer.manifest import Partition

from tests.backend.helpers import manifest

GiB = 2**30
MiB = 2**20


def guided(**changes):
    m = manifest()
    for name, value in changes.items():
        section, _, field = name.partition("__")
        setattr(getattr(m, section), field, value)
    return m


class TestGuidedBudget:
    def test_uefi_with_swap_partition(self):
        plan = layout.guided_plan(guided().storage, 100 * GiB)
        assert plan.fixed == (("EFI system partition", 1024), ("swap", 4096))
        assert plan.root_mib == 100 * 1024 - 2 - 1024 - 4096
        assert plan.swap_file_mib == 0 and plan.usable_root_mib == plan.root_mib

    def test_bios_gpt_has_a_bios_boot_partition_and_dos_does_not(self):
        gpt = layout.guided_plan(guided(storage__firmware="bios").storage, 50 * GiB)
        dos = layout.guided_plan(guided(storage__firmware="bios", storage__table="dos").storage, 50 * GiB)
        assert gpt.fixed[0] == ("BIOS boot partition", 3)
        assert [name for name, _ in dos.fixed] == ["swap"]

    def test_encrypted_install_gets_a_boot_partition_and_keeps_swap_inside_the_root(self):
        plan = layout.guided_plan(guided(storage__luks=True).storage, 100 * GiB)
        assert [name for name, _ in plan.fixed] == ["EFI system partition", "/boot"]
        assert plan.swap_file_mib == 4096
        assert plan.usable_root_mib == plan.root_mib - 4096

    def test_home_partition_is_carved_out(self):
        plan = layout.guided_plan(guided(storage__separate_home=True, storage__home_mib=20480).storage, 100 * GiB)
        assert ("/home", 20480) in plan.fixed

    def test_no_swap_means_no_swap_anywhere(self):
        plan = layout.guided_plan(guided(storage__swap_mib=0, storage__luks=True).storage, 100 * GiB)
        assert plan.swap_file_mib == 0 and all(name != "swap" for name, _ in plan.fixed)


class TestCapacity:
    def test_default_kde_install_on_a_tiny_disk_is_refused_with_the_arithmetic(self):
        errors, _ = layout.capacity_problems(guided(), 8 * GiB)
        assert len(errors) == 1
        assert "8.0 GiB minus EFI system partition 1.0 GiB, swap 4.0 GiB leaves 3.0 GiB" in errors[0]
        assert "kde installation needs at least 24 GiB" in errors[0]

    def test_a_disk_that_fits_passes_without_warnings(self):
        assert layout.capacity_problems(guided(), 64 * GiB) == ([], [])

    def test_between_minimum_and_recommended_only_warns(self):
        errors, warnings = layout.capacity_problems(guided(), 34 * GiB)
        assert errors == [] and len(warnings) == 1 and "40 GiB or more is recommended for kde" in warnings[0]

    def test_a_lighter_desktop_needs_less(self):
        assert layout.capacity_problems(guided(system__desktop="minimal"), 24 * GiB) == ([], [])
        errors, _ = layout.capacity_problems(guided(system__desktop="minimal"), 12 * GiB)
        assert errors

    def test_building_from_source_needs_more(self):
        assert layout.capacity_problems(guided(portage__binhost=True), 40 * GiB)[0] == []
        errors, _ = layout.capacity_problems(guided(portage__binhost=False), 40 * GiB)
        assert errors and "built from source" in errors[0] and "40 GiB" in errors[0]

    def test_the_swap_file_inside_an_encrypted_root_counts_against_the_root(self):
        m = guided(storage__luks=True, storage__swap_mib=16384)
        # 52 GiB - 1 GiB ESP - 1 GiB boot leaves 50 GiB, minus the 16 GiB swap file: 34 GiB for the system
        assert layout.capacity_problems(m, 52 * GiB)[0] == []
        errors, _ = layout.capacity_problems(m, 38 * GiB)
        assert errors and "swap file 16.0 GiB inside the encrypted root" in errors[0]

    def test_oversized_home_leaves_no_room(self):
        m = guided(storage__separate_home=True, storage__home_mib=50 * 1024)
        errors, _ = layout.capacity_problems(m, 64 * GiB)
        assert errors and "/home 50.0 GiB" in errors[0]

    def test_dos_tables_stop_at_the_32_bit_sector_limit(self):
        m = guided(storage__table="dos", storage__firmware="bios")
        errors, _ = layout.capacity_problems(m, 4 * 1024**4)
        assert any("cannot address more than 2 TiB" in e and "choose GPT" in e for e in errors)
        assert not any("cannot address" in e for e in layout.capacity_problems(m, 1024**4)[0])
        # 4096-byte sectors address 16 TiB
        assert not any("cannot address" in e for e in layout.capacity_problems(m, 4 * 1024**4, log_sec=4096)[0])

    def test_manual_mode_and_unknown_sizes_are_not_checked_here(self):
        assert layout.capacity_problems(guided(storage__mode="manual"), 1 * GiB) == ([], [])
        assert layout.capacity_problems(guided(), 0) == ([], [])

    def test_minimum_sizes_are_ordered_by_desktop(self):
        sizes = [layout.minimum_root_mib(guided(system__desktop=d)) for d in ("minimal", "xfce", "gnome", "kde")]
        assert sizes == sorted(sizes) and sizes[0] == 8 * 1024
        assert layout.minimum_root_mib(guided(system__desktop="mystery")) == layout.MIN_ROOT_MIB["kde"]


def machine():
    numbers = iter(range(1, 1000))

    def device(name, typ, size, **fields):
        return {"name": name, "path": f"/dev/{name}", "maj:min": f"8:{next(numbers)}", "type": typ, "size": size, **fields}

    def disk(name, size, serial, model, *children):
        return device(name, "disk", size, serial=serial, model=model, pttype="gpt", children=list(children))

    def part(name, size, fstype="", label=""):
        return device(name, "part", size, fstype=fstype, label=label)

    return blockdev.parse_lsblk(json.dumps({"blockdevices": [
        disk("sda", 500 * GiB, "S5Y3NG0R103244B", "Samsung SSD 870", part("sda1", 450 * GiB, "ntfs", "Windows"), part("sda2", 260 * MiB, "vfat", "SYSTEM")),
        disk("sdb", 1000 * GiB, "WD-WX11A12345", "WDC WD10EZEX", part("sdb1", 400 * GiB, "ntfs", "Data"), part("sdb2", 50 * GiB, "ext4")),
    ]}))


class TestPlanDescription:
    def test_erase_plan_names_the_disk_and_lists_every_partition(self):
        plan = layout.plan_storage(guided(), machine())
        assert [d.path for d in plan.disks] == ["/dev/sda"]
        text = "\n".join(plan.lines())
        assert "ERASE  /dev/sda (Samsung SSD 870, 500.0 GiB, serial …03244B)" in text
        assert "today: 2 partitions: ntfs 450.0 GiB “Windows”, vfat 260.0 MiB “SYSTEM”" in text
        assert "CREATE  EFI system partition → /efi (vfat, 1.0 GiB)" in text
        assert "CREATE  swap (swap, 4.0 GiB)" in text
        assert "CREATE  /dev/sda3" not in text  # new partitions are not named before they exist
        assert "CREATE  root → / (ext4, the rest)" in text
        assert "Other disks will not be modified." in text

    def test_plan_without_an_inventory_still_lists_the_partitions(self):
        plan = layout.plan_storage(guided(storage__firmware="bios"))
        assert plan.disks[0].description == "/dev/sda"
        assert [v.role for v in plan.disks[0].volumes] == ["bios_boot", "swap", "root"]
        assert any("checked before anything is written" in n for n in plan.notes)

    def test_encrypted_plan_shows_the_swap_file_inside_the_root(self):
        plan = layout.plan_storage(guided(storage__luks=True), machine())
        roles = [(v.role, v.encrypted) for v in plan.disks[0].volumes]
        assert roles == [("efi", False), ("boot", False), ("root", True), ("swapfile", True)]
        assert "swap file" in "\n".join(plan.lines())

    def test_capacity_problems_are_part_of_the_plan(self):
        plan = layout.plan_storage(guided(storage__device="/dev/sda"), machine())
        assert plan.errors == ()
        tiny = layout.plan_storage(guided(system__desktop="kde", storage__swap_mib=490000), machine())
        assert tiny.errors and "Problem: " in "\n".join(tiny.lines())

    def test_btrfs_plan_mentions_the_subvolumes(self):
        text = "\n".join(layout.plan_storage(guided(storage__filesystem="btrfs"), machine()).lines())
        assert "Btrfs subvolumes @ (/) and @home (/home)" in text
        flat = "\n".join(layout.plan_storage(guided(storage__filesystem="btrfs"), machine(), btrfs_subvolumes=False).lines())
        assert "subvolumes" not in flat

    def test_manual_plan_lists_every_row_per_disk_as_format_or_keep(self):
        m = guided(storage__mode="manual", storage__firmware="uefi")
        m.storage.partitions = [
            Partition("/dev/sda2", "/efi", "vfat", format=False),
            Partition("/dev/sdb2", "/", "ext4", format=True, label="gentoo"),
            Partition("/dev/sdb1", "/data", "ntfs", format=False),
        ]
        plan = layout.plan_storage(m, machine())
        assert sorted(d.path for d in plan.disks) == ["/dev/sda", "/dev/sdb"]
        text = "\n".join(plan.lines())
        assert "USE    /dev/sdb (WDC WD10EZEX, 1000.0 GiB, serial …A12345)" in text
        assert "FORMAT  /dev/sdb2 → / (ext4, the rest) — now ext4 50.0 GiB, label “gentoo”" in text
        assert "KEEP    /dev/sda2 → /efi (vfat, the rest) — now vfat 260.0 MiB “SYSTEM”" in text
        assert "Disks and partitions that are not listed will not be modified." in text

    def test_a_kept_root_is_called_out_as_overwritten(self):
        m = guided(storage__mode="manual", storage__firmware="bios")
        m.storage.partitions = [Partition("/dev/sdb2", "/", "ext4", format=False)]
        plan = layout.plan_storage(m, machine())
        assert any("installed into it" in w and "backup of /etc" in w for w in plan.warnings)
        assert "Legacy BIOS: the bootloader is written to the start of /dev/sda." in plan.lines()

    def test_manual_plan_without_an_inventory_guesses_the_parent_disk(self):
        m = guided(storage__mode="manual")
        m.storage.partitions = [Partition("/dev/nvme0n1p2", "/", "ext4"), Partition("/dev/sdb3", "/home", "ext4"), Partition("/dev/mmcblk0p1", "/efi", "vfat")]
        assert sorted(d.path for d in layout.plan_storage(m).disks) == ["/dev/mmcblk0", "/dev/nvme0n1", "/dev/sdb"]

    def test_plan_is_json_serialisable(self):
        assert json.dumps(layout.plan_storage(guided(), machine()).to_dict())

    @pytest.mark.parametrize(("size", "text"), [(0, "0 B"), (1023, "1023 B"), (1536, "1.5 KiB"), (260 * MiB, "260.0 MiB"), (3 * 1024**4, "3.0 TiB")])
    def test_human_size(self, size, text):
        assert layout.human_size(size) == text
