"""Mount planning from the target's own fstab: tiers, ordering, option filtering, refusals."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gentoo_installerd.rescue.devices import build_graph
from gentoo_installerd.rescue.errors import RescueError
from gentoo_installerd.rescue.fstab import Fstab, parse_crypttab
from gentoo_installerd.rescue.mountplan import RootSpec, filter_options, plan_mounts, tier_of
from gentoo_installerd.rescue.paths import RootFS

from . import fakeroot as fr
from . import lsblk_fixtures as lf


def make_plan(tmp_path: Path, family: str, lsblk: str, root_device: str, *, fstab: str | None = None, crypttab: str = "", mounts: list[str] | None = None, rmdirs: tuple[str, ...] = (), **kwargs):
    root = fr.make_root(tmp_path / "root", family, fstab=fstab, firmware="uefi") if fstab is not None else fr.make_root(tmp_path / "root", family, firmware="uefi")
    for directory in rmdirs:
        (root / directory.lstrip("/")).rmdir()
    for mountpoint in mounts or []:
        (root / mountpoint.lstrip("/")).mkdir(parents=True, exist_ok=True)
    graph = build_graph(lsblk)
    node = graph.find(root_device)
    spec = RootSpec(node.path, node.fstype, node.uuid, kwargs.pop("rootflags", ""), node.id)
    text = (root / "etc/fstab").read_text() if (root / "etc/fstab").exists() else ""
    with RootFS(root) as fs:
        return plan_mounts(spec, fs, Fstab.parse(text), parse_crypttab(crypttab), graph, **kwargs), root


def test_tiers() -> None:
    assert [tier_of(p) for p in ("/", "/boot", "/boot/efi", "/efi", "/boot/grub2/x86_64-efi", "/usr", "/var/log", "/var", "/home", "/srv", "/mnt/data")] == [1, 2, 2, 2, 2, 3, 3, 3, 4, 4, 4]


def test_gentoo_uefi_plan(tmp_path: Path) -> None:
    plan, _ = make_plan(tmp_path, "gentoo", lf.nvme_dualboot(), "/dev/nvme0n1p5", mounts=["/efi"])
    assert [(item.mountpoint, item.device, item.fstype, item.tier, item.selected) for item in plan.items] == [("/efi", "/dev/nvme0n1p1", "vfat", 2, True)]
    assert plan.items[0].options == ["umask=0077"] and not plan.items[0].needs_mkdir
    assert [entry["mountpoint"] for entry in plan.unresolved] == []  # swap is not a mount
    assert plan.root.device == "/dev/nvme0n1p5"


def test_quoted_and_by_id_entries_are_mounted_like_any_other(tmp_path: Path, monkeypatch) -> None:
    """An fstab written from blkid output (UUID="...") or with /dev/disk/by-id names used to leave /boot/efi and /usr unmounted."""
    from gentoo_installerd.rescue import fstab as fstab_module

    monkeypatch.setattr(fstab_module, "default_dev_link_resolver", lambda path: "/dev/nvme0n1p1" if path == "/dev/disk/by-id/nvme-Samsung_SSD_990_PRO_1TB_S7NZ0000000001-part1" else None)
    fstab = f'UUID="{fr.ROOT_UUID}" / ext4 defaults 0 1\n/dev/disk/by-id/nvme-Samsung_SSD_990_PRO_1TB_S7NZ0000000001-part1 /efi vfat umask=0077 0 2\n'
    plan, _ = make_plan(tmp_path, "gentoo", lf.nvme_dualboot(), "/dev/nvme0n1p5", fstab=fstab, mounts=["/efi"])
    assert [(item.mountpoint, item.device) for item in plan.selected] == [("/efi", "/dev/nvme0n1p1")] and plan.unresolved == []


def internal_nvme_and_usb_sda() -> str:
    """The internal disk is an NVMe here; an external USB drive with ext4 backups happens to be /dev/sda."""
    nvme = lf.node("nvme0n1", "disk", 500 * lf.GiB, maj_min="259:0", tran="nvme", model="Samsung SSD 990 PRO", pttype="gpt", children=[
        lf.part("nvme0n1", 1, 512 * lf.MiB, 259, 0, sep="p", fstype="vfat", uuid="AAAA-1111", parttype=lf.ESP, pttype="gpt"),
        lf.part("nvme0n1", 2, 400 * lf.GiB, 259, 0, sep="p", fstype="ext4", uuid=fr.ROOT_UUID, parttype=lf.LINUX_ROOT, pttype="gpt"),
    ])
    usb = lf.node("sda", "disk", 2000 * lf.GiB, maj_min="8:0", tran="usb", model="Ultra Backup", hotplug=True, rm=True, pttype="gpt", children=[
        lf.part("sda", 1, 2000 * lf.GiB, 8, 0, fstype="ext4", uuid="99999999-8888-4777-8666-555555555555", label="backups", parttype=lf.LINUX_FS, pttype="gpt"),
    ])
    return lf.lsblk(nvme, usb)


def test_a_kernel_name_that_is_another_disk_here_is_never_mounted_on_its_own(tmp_path: Path) -> None:
    """The system was installed when its disk was /dev/sda. In this live session /dev/sda is a USB backup drive: /dev/sda1 must not become /boot, writable."""
    fstab = "/dev/sda2 / ext4 defaults 0 1\n/dev/sda1 /boot ext4 defaults 0 2\n"
    plan, _ = make_plan(tmp_path, "gentoo", internal_nvme_and_usb_sda(), "/dev/nvme0n1p2", fstab=fstab, mounts=["/boot"])
    assert plan.items == [] and plan.devices() == ["/dev/nvme0n1p2"]
    [entry] = plan.unresolved
    assert (entry["mountpoint"], entry["spec"], entry["status"], entry["device"], entry["tier"]) == ("/boot", "/dev/sda1", "unstable", "/dev/sda1", 2)
    assert all(word in entry["reason"] for word in ("kernel name", "Ultra Backup", "2.0 TiB", "ext4", "backups", "another disk"))
    first = plan.suggestions[0]  # the device itself is offered as a choice, so the window can show an Add button for it
    assert (first["mountpoint"], first["device"]) == ("/boot", "/dev/sda1") and "/dev/sda1" in first["reason"] and "only if it is the right one" in first["reason"]
    assert {"mountpoint": "/boot/efi", "device": "/dev/nvme0n1p1", "reason": "EFI system partition on the same disk"} in plan.suggestions  # and what this system's own disk has
    # the person can still choose it: the override replaces the doubt
    chosen, _ = make_plan(tmp_path / "chosen", "gentoo", internal_nvme_and_usb_sda(), "/dev/nvme0n1p2", fstab=fstab, mounts=["/boot"], overrides=[{"mountpoint": "/boot", "device": "/dev/sda1"}])
    assert [(item.mountpoint, item.device, item.source, item.selected) for item in chosen.items] == [("/boot", "/dev/sda1", "user", True)]
    assert chosen.unresolved == [] and chosen.suggestions == [] and chosen.digest != plan.digest


def test_the_same_names_on_the_systems_own_disk_are_mounted_as_before(tmp_path: Path) -> None:
    """A hand-built Gentoo, Slackware or Debian: every entry a kernel name, everything on one disk."""
    fstab = "/dev/sda1 /efi vfat umask=0077 0 2\n/dev/sda2 /boot ext4 noatime 0 2\n/dev/sda3 / xfs noatime 0 1\n/dev/sda4 none swap sw 0 0\n"
    plan, _ = make_plan(tmp_path, "gentoo", lf.xfs_boot_esp(), "/dev/sda3", fstab=fstab, mounts=["/efi", "/boot"])
    assert [(item.mountpoint, item.device, item.source, item.selected) for item in plan.items] == [("/efi", "/dev/sda1", "fstab", True), ("/boot", "/dev/sda2", "fstab", True)]
    assert plan.unresolved == [] and plan.suggestions == []


def test_a_kernel_name_on_a_disk_that_another_entry_names_by_uuid_is_trusted(tmp_path: Path) -> None:
    """/boot or /var on a second disk is a normal layout; the UUID entry says that disk belongs to the system."""
    nvme, usb = json.loads(internal_nvme_and_usb_sda())["blockdevices"]
    usb["children"].append(lf.part("sda", 2, 100 * lf.GiB, 8, 0, fstype="ext4", uuid="77777777-6666-4555-8444-333333333333", label="var", parttype=lf.LINUX_FS, pttype="gpt"))
    text = lf.lsblk(nvme, usb)
    fstab = f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\nUUID=99999999-8888-4777-8666-555555555555 /srv ext4 defaults 0 2\n/dev/sda2 /var ext4 defaults 0 2\n"
    plan, _ = make_plan(tmp_path, "gentoo", text, "/dev/nvme0n1p2", fstab=fstab, mounts=["/var", "/srv"])
    assert {item.mountpoint: (item.device, item.selected) for item in plan.items} == {"/var": ("/dev/sda2", True), "/srv": ("/dev/sda1", False)} and plan.unresolved == []
    alone, _ = make_plan(tmp_path / "alone", "gentoo", text, "/dev/nvme0n1p2", fstab=fstab.replace("UUID=99999999-8888-4777-8666-555555555555 /srv ext4 defaults 0 2\n", ""), mounts=["/var"])
    assert alone.items == [] and [(entry["mountpoint"], entry["status"]) for entry in alone.unresolved] == [("/var", "unstable")]


def test_a_kernel_name_whose_file_system_type_does_not_fit_is_not_trusted_even_on_the_same_disk(tmp_path: Path) -> None:
    fstab = f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\n/dev/sda6 /var xfs defaults 0 2\n"
    plan, _ = make_plan(tmp_path, "gentoo", lf.plain_sata_mbr(), "/dev/sda1", fstab=fstab, mounts=["/var"])
    assert plan.items == []
    [entry] = plan.unresolved
    assert entry["status"] == "unstable" and "xfs" in entry["reason"] and "ext4" in entry["reason"]
    # ext2, ext3 and ext4 are one family, and a stable name is judged by its UUID, not by column three
    fine, _ = make_plan(tmp_path / "fine", "gentoo", lf.plain_sata_mbr(), "/dev/sda1", fstab=fstab.replace("xfs", "ext3"), mounts=["/var"])
    assert [item.mountpoint for item in fine.selected] == ["/var"]
    by_uuid, _ = make_plan(tmp_path / "uuid", "gentoo", lf.plain_sata_mbr(), "/dev/sda1", fstab=fstab.replace("/dev/sda6", f"UUID={fr.HOME_UUID}"), mounts=["/var"])
    assert [item.mountpoint for item in by_uuid.selected] == ["/var"]


def test_a_numbered_md_array_is_not_trusted_on_its_own_either(tmp_path: Path) -> None:
    """/dev/md0 and /dev/md127 are handed out in the order arrays are found; they change between boots and between computers."""
    nvme, _usb = json.loads(internal_nvme_and_usb_sda())["blockdevices"]
    members = json.loads(lf.md_raid1_under_lvm())["blockdevices"]
    array = lf.node("md0", "raid1", 100 * lf.GiB, maj_min="9:0", fstype="ext4", uuid="55555555-4444-4333-8222-111111111111", label="data", path="/dev/md0")
    for disk in members:
        disk["children"][0]["children"] = [json.loads(json.dumps(array))]
    text = lf.lsblk(nvme, *members)
    fstab = f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\n/dev/md0 /var ext4 defaults 0 2\n"
    plan, _ = make_plan(tmp_path, "gentoo", text, "/dev/nvme0n1p2", fstab=fstab, mounts=["/var"])
    assert plan.items == [] and [(entry["mountpoint"], entry["status"], entry["device"]) for entry in plan.unresolved] == [("/var", "unstable", "/dev/md0")]
    # the root is on that array (a RAID root): its member disks are the system's own
    root_on_md, _ = make_plan(tmp_path / "md", "gentoo", lf.md_raid1_root(), "/dev/md127", fstab=f"/dev/md127 / ext4 defaults 0 1\n/dev/sda1 /efi vfat umask=0077 0 2\n", mounts=["/efi"])
    assert [(item.mountpoint, item.device) for item in root_on_md.selected] == [("/efi", "/dev/sda1")] and root_on_md.unresolved == []


def test_data_mounts_are_only_offered_so_a_kernel_name_there_is_listed_not_refused(tmp_path: Path) -> None:
    """Tier 4 is never mounted on its own and always read-only; the person sees the device and ticks it."""
    fstab = f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\n/dev/sda1 /backups ext4 defaults 0 2\n"
    plan, _ = make_plan(tmp_path, "gentoo", internal_nvme_and_usb_sda(), "/dev/nvme0n1p2", fstab=fstab, mounts=["/backups"])
    assert [(item.mountpoint, item.device, item.tier, item.selected) for item in plan.items] == [("/backups", "/dev/sda1", 4, False)] and plan.unresolved == []


def test_ubuntu_lvm_plan_orders_parents_first(tmp_path: Path) -> None:
    fstab = (f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\nUUID={fr.ESP_UUID} /boot/efi vfat umask=0077 0 1\nUUID={fr.BOOT_UUID} /boot ext4 defaults 0 2\n"
             "/dev/mapper/ubuntu--vg-ubuntu--lv /home ext4 defaults 0 2\n")
    plan, _ = make_plan(tmp_path, "ubuntu", lf.ubuntu_lvm(), "/dev/mapper/ubuntu--vg-ubuntu--lv", fstab=fstab, mounts=["/boot", "/boot/efi", "/home"])
    assert [item.mountpoint for item in plan.items] == ["/boot", "/home", "/boot/efi"]  # the same depth keeps fstab order, /boot/efi comes after its parent
    order = [item.mountpoint for item in plan.selected]
    assert order.index("/boot") < order.index("/boot/efi")
    home = [item for item in plan.items if item.mountpoint == "/home"]
    assert len(home) == 1 and home[0].selected is False


def test_tier4_data_is_listed_but_off_until_ticked(tmp_path: Path) -> None:
    fstab = f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\nUUID={fr.HOME_UUID} /home ext4 defaults 0 2\n"
    plan, _ = make_plan(tmp_path, "gentoo", lf.plain_sata_mbr(), "/dev/sda1", fstab=fstab, mounts=["/home"])
    home = [item for item in plan.items if item.mountpoint == "/home"][0]
    assert home.tier == 4 and home.selected is False and home.device == "/dev/sda6"
    ticked, _ = make_plan(tmp_path / "again", "gentoo", lf.plain_sata_mbr(), "/dev/sda1", fstab=fstab, mounts=["/home"], include_tier4=["/home"])
    assert [item.mountpoint for item in ticked.selected] == ["/home"]
    assert plan.digest != ticked.digest


def test_noauto_boot_is_still_planned(tmp_path: Path) -> None:
    fstab = f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\nUUID={fr.BOOT_UUID} /boot ext4 noauto,noatime 0 2\n"
    plan, _ = make_plan(tmp_path, "gentoo", lf.ubuntu_lvm(), "/dev/mapper/ubuntu--vg-ubuntu--lv", fstab=fstab, mounts=["/boot"])
    boot = plan.selected[0]
    assert boot.mountpoint == "/boot" and "noauto" not in boot.options and "noatime" not in boot.options
    assert "noauto" in " ".join(plan.ignored_options)


def test_option_allow_list_keeps_subvol_and_drops_the_rest(tmp_path: Path) -> None:
    fstab = (f"UUID={fr.ROOT_UUID} / btrfs subvol=root,compress=zstd:1 0 0\nUUID={fr.BOOT_UUID} /boot ext4 defaults,discard,x-systemd.automount,nobarrier,data=journal 0 2\n"
             f"UUID={fr.ESP_UUID} /boot/efi vfat umask=0077,shortname=winnt,iocharset=ascii,_netdev,user,owner 0 2\n")
    plan, _ = make_plan(tmp_path, "fedora", lf.fedora_cloud_btrfs(), "/dev/vda4", fstab=fstab, mounts=["/boot", "/boot/efi"])
    options = {item.mountpoint: item.options for item in plan.items}
    assert options["/boot"] == []
    assert options["/boot/efi"] == ["umask=0077", "shortname=winnt", "iocharset=ascii"]
    dropped = " ".join(plan.ignored_options)
    for name in ("discard", "x-systemd.automount", "nobarrier", "data", "_netdev", "user", "owner"):
        assert name in dropped


def test_btrfs_subvol_options_pass_through_when_safe() -> None:
    carried, dropped = filter_options(["subvol=/@/boot/grub2/x86_64-efi", "compress=zstd", "subvolid=258"], "btrfs")
    assert carried == ["subvol=/@/boot/grub2/x86_64-efi", "subvolid=258"] and dropped == ["compress"]
    for bad in ("subvol=../x", "subvol=$(x)", "subvol=a;b", "subvol=a/../b", "subvol=", "subvol=/"):
        with pytest.raises(ValueError):
            filter_options([bad], "btrfs")


def test_unusual_subvolume_names_become_unresolved(tmp_path: Path) -> None:
    fstab = f"UUID={fr.ROOT_UUID} / btrfs defaults 0 0\nUUID={fr.BOOT_UUID} /boot ext4 subvol=a;b 0 2\n"
    plan, _ = make_plan(tmp_path, "fedora", lf.fedora_cloud_btrfs(), "/dev/vda4", fstab=fstab, mounts=["/boot"])
    assert plan.items == [] and plan.unresolved[0]["status"] == "unusable" and "unusual" in plan.unresolved[0]["reason"]


def test_dead_and_ambiguous_entries_are_unresolved_not_guessed(tmp_path: Path) -> None:
    fstab = f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\nUUID={fr.DEAD_UUID} /boot ext4 defaults 0 2\nUUID={fr.HOME_UUID} /home ext4 defaults 0 2\n"
    plan, _ = make_plan(tmp_path, "gentoo", lf.plain_sata_mbr(), "/dev/sda1", fstab=fstab, mounts=["/boot", "/home"])
    assert [(entry["mountpoint"], entry["status"]) for entry in plan.unresolved] == [("/boot", "unresolved")]
    cloned, _ = make_plan(tmp_path / "c", "gentoo", lf.cloned_uuids(), "/dev/sda1", fstab=f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\nUUID={fr.ROOT_UUID} /boot ext4 defaults 0 2\n", mounts=["/boot"])
    assert cloned.unresolved[0]["status"] == "ambiguous"


def test_needs_unlock_entries_name_the_container(tmp_path: Path) -> None:
    fstab = f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\n/dev/mapper/cryptdata /data ext4 defaults 0 2\n"
    crypttab = f"cryptdata UUID={fr.LUKS_UUID} none luks\n"
    plan, _ = make_plan(tmp_path, "gentoo", lf.luks_lvm_ext4(), "/dev/nvme0n1p2", fstab=fstab, crypttab=crypttab, mounts=["/data"])
    assert plan.unresolved[0]["status"] == "needs_unlock" and plan.unresolved[0]["container"] == "/dev/nvme0n1p3"


def test_user_overrides_replace_fstab_choices_and_are_checked(tmp_path: Path) -> None:
    fstab = f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\nUUID={fr.DEAD_UUID} /boot ext4 defaults 0 2\n"
    plan, _ = make_plan(tmp_path, "gentoo", lf.ubuntu_lvm(), "/dev/mapper/ubuntu--vg-ubuntu--lv", fstab=fstab, mounts=["/boot"],
                        overrides=[{"mountpoint": "/boot", "device": "/dev/sda2"}])
    assert [(item.mountpoint, item.device, item.source) for item in plan.items] == [("/boot", "/dev/sda2", "user")] and plan.unresolved == []
    for bad in ({"mountpoint": "/", "device": "/dev/sda2"}, {"mountpoint": "/proc", "device": "/dev/sda2"}, {"mountpoint": "/run/x", "device": "/dev/sda2"},
                {"mountpoint": "/boot/../etc", "device": "/dev/sda2"}):
        with pytest.raises(RescueError):
            make_plan(tmp_path / "o", "gentoo", lf.ubuntu_lvm(), "/dev/mapper/ubuntu--vg-ubuntu--lv", fstab=fstab, mounts=["/boot"], overrides=[bad])
    with pytest.raises(RescueError) as info:
        make_plan(tmp_path / "o2", "gentoo", lf.ubuntu_lvm(), "/dev/mapper/ubuntu--vg-ubuntu--lv", fstab=fstab, mounts=["/boot"], overrides=[{"mountpoint": "/boot", "device": "/dev/nope"}])
    assert info.value.short == "not_found"


def test_override_of_the_live_medium_or_swap_is_refused(tmp_path: Path) -> None:
    text = lf.usb_live_hybrid()
    graph_kwargs = {"live_sources": ["/dev/sdb1"], "cmdline": "root=live:CDLABEL=GENTOO_INSTALLER_AMD64"}
    graph = build_graph(text, **graph_kwargs)
    root = fr.make_root(tmp_path / "r", "gentoo")
    node = graph.find("/dev/sda2")
    spec = RootSpec(node.path, node.fstype, node.uuid, "", node.id)
    with RootFS(root) as fs, pytest.raises(RescueError) as info:
        plan_mounts(spec, fs, Fstab.parse(""), [], graph, overrides=[{"mountpoint": "/boot/efi", "device": "/dev/sdb2"}])
    assert info.value.short == "live_medium"
    with pytest.raises(RescueError):
        make_plan(tmp_path / "s", "gentoo", lf.plain_sata_mbr(), "/dev/sda1", fstab="", overrides=[{"mountpoint": "/swapdir", "device": "/dev/sda5"}])


def test_virtual_mount_areas_in_fstab_are_ignored_with_a_warning(tmp_path: Path) -> None:
    fstab = f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\nUUID={fr.BOOT_UUID} /run/evil ext4 defaults 0 2\nUUID={fr.BOOT_UUID} /proc ext4 defaults 0 2\nUUID={fr.BOOT_UUID} /boot/../../etc ext4 defaults 0 2\n"
    plan, _ = make_plan(tmp_path, "gentoo", lf.ubuntu_lvm(), "/dev/mapper/ubuntu--vg-ubuntu--lv", fstab=fstab)
    assert plan.items == []
    assert sum("reserved" in warning for warning in plan.warnings) == 2
    assert any("'..'" in entry["reason"] for entry in plan.unresolved)


def test_duplicate_mountpoints_use_the_first_entry(tmp_path: Path) -> None:
    fstab = f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\nUUID={fr.BOOT_UUID} /boot ext4 defaults 0 2\nUUID={fr.BOOT_UUID} /boot ext4 ro 0 2\n"
    plan, _ = make_plan(tmp_path, "gentoo", lf.ubuntu_lvm(), "/dev/mapper/ubuntu--vg-ubuntu--lv", fstab=fstab, mounts=["/boot"])
    assert len(plan.items) == 1 and any("more than once" in warning for warning in plan.warnings)


def test_missing_mountpoint_directories_are_postponed(tmp_path: Path) -> None:
    plan, _ = make_plan(tmp_path, "gentoo", lf.nvme_dualboot(), "/dev/nvme0n1p5", mounts=[], rmdirs=("/efi",))
    assert plan.items[0].needs_mkdir is True


def test_a_mount_point_below_another_mount_is_not_judged_by_the_root_file_system(tmp_path: Path) -> None:
    """With a separate /boot the directory efi lives on the /boot partition; the empty /boot of the root file system says nothing about it."""
    fstab = f"UUID={fr.ROOT_UUID} / ext4 defaults 0 1\nUUID={fr.BOOT_UUID} /boot ext4 defaults 0 2\nUUID={fr.ESP_UUID} /boot/efi vfat umask=0077 0 2\n"
    plan, _ = make_plan(tmp_path, "gentoo", lf.ubuntu_lvm(), "/dev/mapper/ubuntu--vg-ubuntu--lv", fstab=fstab, mounts=["/boot"])
    assert [(item.mountpoint, item.needs_mkdir) for item in plan.selected] == [("/boot", False), ("/boot/efi", False)]
    # below nothing that is mounted, the root file system is the only place the directory can be
    alone, _ = make_plan(tmp_path / "alone", "gentoo", lf.ubuntu_lvm(), "/dev/mapper/ubuntu--vg-ubuntu--lv", fstab=fstab.replace(f"UUID={fr.BOOT_UUID} /boot ext4 defaults 0 2\n", ""))
    assert [(item.mountpoint, item.needs_mkdir) for item in alone.selected] == [("/boot/efi", True)]


def test_devices_mounted_elsewhere_are_refused_and_the_message_says_how_to_free_them(tmp_path: Path) -> None:
    """Nothing in the program unmounts another mount, so nothing may pretend to: the person frees the disk first (the old --unmount-existing only silenced this check)."""
    import inspect

    with pytest.raises(RescueError) as info:
        make_plan(tmp_path, "gentoo", lf.desktop_automount(), "/dev/sda1", fstab="")
    assert info.value.short == "mounted_elsewhere" and "/run/media/gentoo/root" in info.value.message
    assert "umount /run/media/gentoo/root" in info.value.message and "eject" in info.value.message and "confirm" not in info.value.message
    assert info.value.details["mountpoint"] == "/run/media/gentoo/root" and info.value.details["device"] == "/dev/sda1"
    assert "unmount_existing" not in inspect.signature(plan_mounts).parameters
    data = json.loads(lf.desktop_automount())
    data["blockdevices"][0]["children"][0]["mountpoints"] = []
    plan, _ = make_plan(tmp_path / "freed", "gentoo", json.dumps(data), "/dev/sda1", fstab="")
    assert plan.root.device == "/dev/sda1"


def test_missing_fstab_suggests_the_esp_on_the_roots_disk(tmp_path: Path) -> None:
    plan, _ = make_plan(tmp_path, "gentoo", lf.nvme_dualboot(), "/dev/nvme0n1p5", fstab=None or "", esp_default_mountpoint="/efi")
    assert plan.items == [] and plan.suggestions == [{"mountpoint": "/efi", "device": "/dev/nvme0n1p1", "reason": "EFI system partition on the same disk"}]


def test_btrfs_multi_device_members_are_part_of_the_plan_devices(tmp_path: Path) -> None:
    fstab = f"UUID={fr.ROOT_UUID} / btrfs subvol=@,noatime 0 0\n"
    plan, _ = make_plan(tmp_path, "arch", lf.btrfs_multi_device(), "/dev/sda1", fstab=fstab, rootflags="subvolid=256")
    assert plan.root.rootflags == "subvolid=256" and plan.root.extra_devices == ["/dev/sdb1"]
    assert plan.devices() == ["/dev/sda1", "/dev/sdb1"]  # every member is flagged, made writable and allowed together (review R7c)
    assert plan.members_of("/dev/sda1") == ["/dev/sda1", "/dev/sdb1"] and plan.members_of("/dev/sdz9") == ["/dev/sdz9"]
    assert plan.to_dict()["root"]["extra_devices"] == ["/dev/sdb1"]
    other, _ = make_plan(tmp_path / "x", "arch", lf.btrfs_multi_device(), "/dev/sda1", fstab=fstab, rootflags="subvolid=256")
    assert other.digest == plan.digest


def test_xfs_external_log_is_found_by_uuid_and_never_taken_for_the_file_system(tmp_path: Path) -> None:
    fstab = f"UUID={fr.ROOT_UUID} / xfs noatime 0 1\n"
    plan, _ = make_plan(tmp_path, "gentoo", lf.xfs_external_log(), "/dev/sda2", fstab=fstab)
    assert plan.root.options == ["logdev=/dev/sdb1"] and plan.root.extra_devices == ["/dev/sdb1"] and plan.devices() == ["/dev/sda2", "/dev/sdb1"]
    assert any("keeps its log on /dev/sdb1" in warning for warning in plan.warnings)
    graph = build_graph(lf.xfs_external_log())
    assert [node.path for node in graph.by_uuid(fr.ROOT_UUID)] == ["/dev/sda2"]  # the log shares the UUID but is not a candidate for UUID= lookups
    assert {node.path for node in graph.by_uuid(fr.ROOT_UUID, include_logs=True)} == {"/dev/sda2", "/dev/sdb1"}


def test_a_renamed_log_device_is_repaired_and_a_missing_one_is_dropped_with_a_warning(tmp_path: Path) -> None:
    renamed, _ = make_plan(tmp_path, "gentoo", lf.xfs_external_log(), "/dev/sda2", fstab=f"UUID={fr.ROOT_UUID} / xfs logdev=/dev/sdz9,noatime 0 1\n")
    assert renamed.root.options == ["logdev=/dev/sdb1"] and any("is now /dev/sdb1" in warning for warning in renamed.warnings)
    present, _ = make_plan(tmp_path / "p", "gentoo", lf.xfs_external_log(), "/dev/sda2", fstab=f"UUID={fr.ROOT_UUID} / xfs logdev=/dev/sdb1 0 1\n")
    assert present.root.options == ["logdev=/dev/sdb1"] and not [w for w in present.warnings if "log" in w]
    plain, _ = make_plan(tmp_path / "q", "gentoo", lf.plain_sata_mbr(), "/dev/sda1", fstab=f"UUID={fr.ROOT_UUID} / ext4 logdev=/dev/sdz9,noatime 0 1\n")
    assert plain.root.options == [] and plain.root.extra_devices == []
    assert [w for w in plain.warnings if "option logdev=/dev/sdz9 names a device that is not present here" in w]


def test_digest_is_stable_and_changes_with_the_plan(tmp_path: Path) -> None:
    first, _ = make_plan(tmp_path, "gentoo", lf.nvme_dualboot(), "/dev/nvme0n1p5", mounts=["/efi"])
    second, _ = make_plan(tmp_path / "b", "gentoo", lf.nvme_dualboot(), "/dev/nvme0n1p5", mounts=["/efi"])
    assert first.digest == second.digest and len(first.digest) == 64
    third, _ = make_plan(tmp_path / "c", "gentoo", lf.nvme_dualboot(), "/dev/nvme0n1p5", mounts=["/efi"], allow_write=False)
    assert third.digest != first.digest
    assert first.to_dict()["digest"] == first.digest
