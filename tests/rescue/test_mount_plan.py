"""Mount planning from the target's own fstab: tiers, ordering, option filtering, refusals."""

from __future__ import annotations

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


def test_devices_mounted_elsewhere_are_refused_unless_confirmed(tmp_path: Path) -> None:
    with pytest.raises(RescueError) as info:
        make_plan(tmp_path, "gentoo", lf.desktop_automount(), "/dev/sda1", fstab="")
    assert info.value.short == "mounted_elsewhere" and "/run/media/gentoo/root" in info.value.message
    plan, _ = make_plan(tmp_path / "ok", "gentoo", lf.desktop_automount(), "/dev/sda1", fstab="", unmount_existing=["/run/media/gentoo/root"])
    assert plan.root.device == "/dev/sda1"
    for forbidden in ("/", "/boot", "/mnt/gentoo", "/mnt/rescue/r123456", "/run/initramfs/live", "/home/user"):
        text = lf.desktop_automount().replace("/run/media/gentoo/root", forbidden)
        with pytest.raises(RescueError):
            make_plan(tmp_path / forbidden.strip("/").replace("/", "_"), "gentoo", text, "/dev/sda1", fstab="", unmount_existing=[forbidden])


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
