import json

import pytest

from gentoo_installer import blockdev


def lsblk_json(devices):
    return json.dumps({"blockdevices": devices})


RAID1 = {"name": "md0", "path": "/dev/md0", "maj:min": "9:0", "type": "raid1", "size": 1000, "fstype": "ext4", "mountpoints": [None]}
MACHINE = [
    {"name": "sda", "path": "/dev/sda", "maj:min": "8:0", "type": "disk", "size": 2000, "serial": "SER-A", "model": "Disk A",
     "mountpoints": [None], "children": [
         {"name": "sda1", "path": "/dev/sda1", "maj:min": "8:1", "type": "part", "size": 1000, "fstype": "linux_raid_member",
          "mountpoints": [None], "children": [RAID1]},
     ]},
    {"name": "sdb", "path": "/dev/sdb", "maj:min": "8:16", "type": "disk", "size": 2000, "wwn": "0x5000", "model": "Disk B",
     "children": [
         {"name": "sdb1", "path": "/dev/sdb1", "maj:min": "8:17", "type": "part", "size": 1000, "fstype": "linux_raid_member",
          "children": [RAID1]},
     ]},
    {"name": "sdc", "path": "/dev/sdc", "maj:min": "8:32", "type": "disk", "size": 3000000000, "model": "USB Stick",
     "rm": True, "hotplug": True, "tran": "usb", "label": "GENTOO_INSTALLER_AMD64", "fstype": "iso9660",
     "children": [
         {"name": "sdc2", "path": "/dev/sdc2", "maj:min": "8:34", "type": "part", "size": 4000, "fstype": "vfat"},
     ]},
    {"name": "vda", "path": "/dev/vda", "maj:min": "253:0", "type": "disk", "size": 85899345920, "model": "",
     "mountpoints": [], "children": [
         {"name": "vda1", "path": "/dev/vda1", "maj:min": "253:1", "type": "part", "size": 1000, "mountpoints": ["/mnt/x", None]},
     ]},
]


@pytest.fixture
def roots():
    return blockdev.parse_lsblk(lsblk_json(MACHINE))


def test_lsblk_argv_requests_every_column_the_parser_reads():
    argv = blockdev.lsblk_argv(all_devices=True)
    assert argv[:3] == ("lsblk", "--json", "--bytes")
    assert "MAJ:MIN" in argv[4] and "MOUNTPOINTS" in argv[4]
    assert argv[-1] == "--all"


def test_null_mountpoints_do_not_count_as_mounted(roots):
    sda = blockdev.find(roots, "/dev/sda")
    assert sda.mountpoints == ()
    assert not sda.mounted
    assert blockdev.find(roots, "/dev/vda").mounted


def test_raid_member_listed_under_two_parents_is_one_device(roots):
    md0 = blockdev.find(roots, "/dev/md0")
    assert md0.type == "raid1"
    assert [disk.path for disk in blockdev.parent_disks(roots, md0)] == ["/dev/sda", "/dev/sdb"]
    assert [node.path for node in blockdev.all_nodes(roots)].count("/dev/md0") == 1


def test_find_reports_absent_devices(roots):
    with pytest.raises(LookupError):
        blockdev.find(roots, "/dev/sdz")


def test_find_reports_genuinely_ambiguous_paths():
    clash = [
        {"name": "x", "path": "/dev/x", "maj:min": "1:1", "type": "disk"},
        {"name": "x", "path": "/dev/x", "maj:min": "1:2", "type": "disk"},
    ]
    with pytest.raises(ValueError):
        blockdev.find(blockdev.parse_lsblk(lsblk_json(clash)), "/dev/x")


def test_identity_prefers_serial_then_wwn(roots):
    assert blockdev.find(roots, "/dev/sda").identity == blockdev.Identity("serial", "SER-A")
    assert blockdev.find(roots, "/dev/sdb").identity == blockdev.Identity("wwn", "0x5000")
    assert blockdev.find(roots, "/dev/vda").identity is None


def test_path_identity_for_serial_less_disks(roots):
    vda = blockdev.find(roots, "/dev/vda")
    identity = blockdev.path_identity(vda, "pci-0000:00:04.0")
    assert identity == blockdev.Identity("path", "pci-0000:00:04.0|85899345920|")
    assert blockdev.identity_matches(vda, "path", identity.value, "pci-0000:00:04.0")
    assert not blockdev.identity_matches(vda, "path", identity.value, "pci-0000:00:05.0")
    with pytest.raises(ValueError):
        blockdev.path_identity(vda, "")


def test_identity_matches_serial_and_wwn(roots):
    sda = blockdev.find(roots, "/dev/sda")
    assert blockdev.identity_matches(sda, "serial", "SER-A")
    assert blockdev.identity_matches(sda, "", "SER-A")
    assert not blockdev.identity_matches(sda, "serial", "")
    assert not blockdev.identity_matches(sda, "serial", "OTHER")


def test_live_medium_found_by_dracut_label_and_marks_its_disk(roots):
    cmdline = "BOOT_IMAGE=/boot/gentoo root=live:CDLABEL=GENTOO_INSTALLER_AMD64 rd.live.image quiet"
    keys = blockdev.live_medium_keys(roots, cmdline=cmdline)
    assert blockdev.find(roots, "/dev/sdc").key in keys
    assert blockdev.find(roots, "/dev/sda").key not in keys


def test_live_medium_found_by_mount_source_marks_parent_disk(roots):
    keys = blockdev.live_medium_keys(roots, sources=["/dev/sdc2"])
    assert {blockdev.find(roots, "/dev/sdc2").key, blockdev.find(roots, "/dev/sdc").key} <= keys


def test_unknown_live_source_is_ignored(roots):
    assert blockdev.live_medium_keys(roots, sources=["/dev/loop9"], cmdline="quiet") == set()


def test_removable_and_transport_fields(roots):
    stick = blockdev.find(roots, "/dev/sdc")
    assert stick.removable and stick.hotplug and stick.transport == "usb"
