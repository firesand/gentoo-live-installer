import json
import subprocess

import pytest

from gentoo_installer.system_info import DiskInfo, discover_disks, format_size


def test_disk_sizes_use_all_binary_units():
    assert format_size(2 * 1024**4) == "2.0 TiB"
    assert format_size(64 * 1024**3) == "64.0 GiB"
    assert DiskInfo("/dev/sda", "Test", 2 * 1024**4).size_text == "2.0 TiB"


@pytest.mark.parametrize(
    ("mountpoints", "children", "expected"),
    [
        ([None], [], False),
        (["", None], [], False),
        (["/media/live"], [], True),
        ([None], [{"mountpoints": ["/boot"], "children": []}], True),
        ([None], [{"mountpoints": [None], "children": [
            {"mountpoints": ["/mnt/target"], "children": []},
        ]}], True),
    ],
)
def test_discover_disks_counts_only_nonempty_mountpoints_recursively(
    monkeypatch, mountpoints, children, expected
):
    record = {
        "name": "vda", "path": "/dev/vda", "type": "disk", "model": "QEMU",
        "size": 16 * 1024**3, "serial": "GENTOO-VM-DISPOSABLE",
        "mountpoints": mountpoints, "children": children,
    }
    monkeypatch.setattr(
        subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0,
            stdout=json.dumps({"blockdevices": [record]}), stderr=""),
    )
    disks = discover_disks()
    assert len(disks) == 1
    assert disks[0].mounted is expected
