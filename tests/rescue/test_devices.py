"""Device graph, roles and live-medium detection from lsblk fixtures."""

from __future__ import annotations

import pytest

from gentoo_installerd.rescue import devices
from gentoo_installerd.rescue.devices import build_graph, extract_json, rescue_lsblk_argv

from . import lsblk_fixtures as lf

LIVE_CMDLINE = "BOOT_IMAGE=/boot/gentoo dokeymap nodhcp secureconsole root=live:CDLABEL=GENTOO_INSTALLER_AMD64 rd.live.dir=/ rd.live.squashimg=image.squashfs cdroot"


def roles(graph: devices.DeviceGraph) -> dict[str, str]:
    return {node.name: node.role for node in graph}


def test_the_lsblk_command_asks_for_every_column_rescue_needs() -> None:
    argv = rescue_lsblk_argv()
    assert argv[:5] == ("lsblk", "--json", "--bytes", "--all", "--output")
    for column in ("MAJ:MIN", "PKNAME", "PARTN", "FSROOTS", "PARTTYPE", "MOUNTPOINTS", "SERIAL", "WWN", "HOTPLUG", "TRAN"):
        assert column in argv[-1].split(",")


def test_extract_json_skips_warnings_merged_in_front() -> None:
    document = extract_json('WARNING: duplicate PV\nlsblk: warning\n{"blockdevices": []}\ntrailing')
    assert document == {"blockdevices": []}
    assert extract_json('noise [1, 2] {"x": 1}') == [1, 2]
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_plain_mbr_disk_roles_and_extended_container() -> None:
    graph = build_graph(lf.plain_sata_mbr())
    assert roles(graph) == {"sda": "other", "sda1": "filesystem", "sda2": "other", "sda5": "swap", "sda6": "filesystem"}
    sda1 = graph.find("/dev/sda1")
    assert graph.parent_disks(sda1)[0].name == "sda"
    assert sda1.partn == 1 and graph.find("/dev/sda6").partn == 6


def test_gpt_roles_for_windows_and_linux_dual_boot() -> None:
    graph = build_graph(lf.nvme_dualboot())
    assert roles(graph) == {"nvme0n1": "other", "nvme0n1p1": "esp", "nvme0n1p2": "other", "nvme0n1p3": "data", "nvme0n1p4": "data", "nvme0n1p5": "filesystem", "nvme0n1p6": "swap"}
    assert graph.find("/dev/nvme0n1p5").partn == 5


def test_cloud_layouts_have_bios_boot_and_esp() -> None:
    debian = build_graph(lf.debian_cloud())
    assert roles(debian) == {"vda": "other", "vda1": "filesystem", "vda14": "bios_boot", "vda15": "esp"}
    fedora = build_graph(lf.fedora_cloud_btrfs())
    assert roles(fedora)["vda4"] == "filesystem" and roles(fedora)["vda3"] == "filesystem" and roles(fedora)["vda2"] == "esp"


def test_luks_lvm_states_are_distinguished() -> None:
    locked = build_graph(lf.luks_lvm_ext4())
    assert roles(locked)["nvme0n1p3"] == "luks_locked"
    unlocked = build_graph(lf.luks_lvm_ext4(unlocked=True))
    assert roles(unlocked)["nvme0n1p3"] == "luks_unlocked"
    assert roles(unlocked)["rescue-1f2e3d4c5b6a"] == "lvm_pv"
    active = build_graph(lf.luks_lvm_ext4(unlocked=True, activated=True))
    assert roles(active)["vg0-root"] == "filesystem" and roles(active)["vg0-swap"] == "swap"
    root = active.find("/dev/mapper/vg0-root")
    assert [node.name for node in active.ancestors(root)].count("nvme0n1p3") == 1
    assert active.parent_disks(root)[0].name == "nvme0n1"


def test_lvm_without_activation_is_a_physical_volume_only() -> None:
    graph = build_graph(lf.lvm_inactive())
    assert roles(graph)["sda2"] == "lvm_pv"


def test_md_array_and_lv_listed_under_two_parents_are_single_nodes() -> None:
    graph = build_graph(lf.md_raid1_under_lvm())
    names = [node.name for node in graph]
    assert names.count("md127") == 1 and names.count("vgdata-data") == 1
    md = graph.find("/dev/md127")
    assert {parent.name for parent in graph.parents_of(md)} == {"sdb1", "sdc1"}
    assert md.role == "lvm_pv" and md.type == "raid1"
    lv = graph.find("/dev/mapper/vgdata-data")
    assert [parent.name for parent in graph.parents_of(lv)] == ["md127"]
    assert {disk.name for disk in graph.parent_disks(lv)} == {"sdb", "sdc"}
    assert roles(graph)["sdb1"] == "md_member"


def test_unassembled_raid_members_are_listed() -> None:
    graph = build_graph(lf.md_raid1_unassembled())
    assert roles(graph)["sdb1"] == roles(graph)["sdc1"] == "md_member"


def test_btrfs_members_share_a_uuid_but_are_distinct_devices() -> None:
    graph = build_graph(lf.btrfs_multi_device())
    assert len(graph.by_uuid(lf.fr.ROOT_UUID)) == 2


def test_unsupported_filesystems_carry_a_reason() -> None:
    graph = build_graph(lf.exotic_filesystems())
    assert roles(graph)["sda1"] == roles(graph)["sda2"] == roles(graph)["sda3"] == roles(graph)["sda4"] == "unsupported"
    assert "6.13" in graph.find("/dev/sda1").unsupported_reason
    assert "ZFS" in graph.find("/dev/sda2").unsupported_reason
    assert "bcachefs" in graph.find("/dev/sda3").unsupported_reason
    assert "BitLocker" in graph.find("/dev/sda4").unsupported_reason


def test_windows_only_disk_has_no_probe_candidates() -> None:
    graph = build_graph(lf.windows_only())
    assert {node.role for node in graph if node.type == "part"} == {"data", "esp"}


def test_legacy_lsblk_output_is_normalised() -> None:
    graph = build_graph(lf.legacy_null())
    sda1 = graph.find("/dev/sda1")
    assert sda1.mountpoints == () and sda1.ro is False and sda1.partn == 1
    assert graph.find("/dev/sda5").role == "swap"


def test_live_medium_by_mount_source_marks_the_whole_stick() -> None:
    graph = build_graph(lf.usb_live_hybrid(), live_sources=["/dev/sdb1"], cmdline=LIVE_CMDLINE)
    live = {node.name for node in graph if node.live_medium}
    assert {"sdb", "sdb1", "sdb2"} <= live
    assert "sda" not in live and "sda2" not in live
    assert roles(graph)["sdb1"] == "live" and roles(graph)["sdb2"] == "live" and roles(graph)["sda2"] == "filesystem"


def test_live_medium_by_dracut_label_when_copied_to_ram() -> None:
    """With rd.live.ram=1 nothing is mounted any more; the CDLABEL still identifies the stick."""
    graph = build_graph(lf.usb_live_hybrid().replace("/run/initramfs/live", ""), live_sources=[], cmdline=LIVE_CMDLINE + " rd.live.ram=1")
    assert {"sdb", "sdb1", "sdb2"} <= {node.name for node in graph if node.live_medium}
    assert not graph.find("/dev/sda2").live_medium


def test_live_medium_ventoy_device_mapper() -> None:
    graph = build_graph(lf.ventoy_live(), live_sources=["/dev/mapper/ventoy"], cmdline=LIVE_CMDLINE)
    live = {node.name for node in graph if node.live_medium}
    assert {"sdb", "sdb1", "sdb2", "ventoy"} <= live
    assert not graph.find("/dev/sda1").live_medium


def test_no_live_medium_when_nothing_matches() -> None:
    graph = build_graph(lf.plain_sata_mbr(), live_sources=[], cmdline="quiet")
    assert not any(node.live_medium for node in graph)


def test_find_accepts_paths_ids_and_rejects_strangers() -> None:
    graph = build_graph(lf.debian_cloud())
    assert graph.find("/dev/vda1").id == "dvda1"
    assert graph.find("dvda1").path == "/dev/vda1"
    with pytest.raises(LookupError):
        graph.find("/dev/zzz")


def test_stack_and_siblings() -> None:
    graph = build_graph(lf.luks_lvm_ext4(unlocked=True, activated=True))
    root = graph.find("/dev/mapper/vg0-root")
    stack = {node.name for node in graph.stack_of(root)}
    assert stack == {"vg0-root", "rescue-1f2e3d4c5b6a", "nvme0n1p3"}
    siblings = {node.name for node in graph.siblings_on_disks(graph.stack_of(root))}
    assert siblings == {"nvme0n1p1", "nvme0n1p2"}


def test_hotplug_and_transport_are_kept() -> None:
    graph = build_graph(lf.usb_live_hybrid())
    assert graph.find("/dev/sdb").hotplug and graph.find("/dev/sdb").tran == "usb"
    assert not graph.find("/dev/sda").hotplug


def test_cloned_uuids_are_two_devices() -> None:
    graph = build_graph(lf.cloned_uuids())
    assert len(graph.by_uuid(lf.fr.ROOT_UUID)) == 2
    assert {node.name for node in graph.by_uuid(lf.fr.ROOT_UUID.upper())} == {"sda1", "sdb1"}


def test_busy_elsewhere_devices_keep_their_mountpoints() -> None:
    graph = build_graph(lf.desktop_automount())
    assert graph.find("/dev/sda2").mountpoints == ("/run/media/gentoo/home",)
    assert graph.find("/dev/sda2").fsroots == ("/@home",)


def test_node_dicts_are_json_ready() -> None:
    import json
    graph = build_graph(lf.fedora_cloud_btrfs())
    json.dumps(graph.to_dict_list())
    assert graph.find("/dev/vda4").to_dict()["role"] == "filesystem"
