"""inspect.py on top of gentoo_installer.blockdev: lookups, filters, identities, probes."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from gentoo_installer import blockdev
from gentoo_installerd import inspect as insp
from gentoo_installerd.inspect import Discovery, Disk, Inspection, ReadOnlyRunner, find_disk, parse_lsblk
from gentoo_installerd.runner import DryRunRunner, RunnerError

from tests.backend.helpers import FakeRunner

GiB = 2**30


def node(name, typ, size, *, maj_min="", path=None, children=(), **fields):
    raw = {"name": name, "path": path or f"/dev/{name}", "type": typ, "size": size, "maj:min": maj_min, "children": list(children)}
    raw.update(fields)
    return raw


def tree(*roots):
    return json.dumps({"blockdevices": list(roots)})


RAID1 = node("md0", "raid1", GiB, maj_min="9:0", fstype="ext4")
TWO_MEMBER_MACHINE = tree(
    node("sda", "disk", 2 * GiB, maj_min="8:0", serial="SER-A", children=[node("sda1", "part", GiB, maj_min="8:1", fstype="linux_raid_member", children=[RAID1])]),
    node("sdb", "disk", 2 * GiB, maj_min="8:16", wwn="0x5000", children=[node("sdb1", "part", GiB, maj_min="8:17", fstype="linux_raid_member", children=[RAID1])]),
)


class TestFindDisk:
    def test_md_array_listed_under_two_parents_is_one_device(self):
        disks = parse_lsblk(TWO_MEMBER_MACHINE)
        assert find_disk(disks, "/dev/md0").type == "raid1"

    def test_lv_spanning_two_physical_volumes_is_one_device(self):
        lv = node("vg0-root", "lvm", GiB, maj_min="253:0", path="/dev/mapper/vg0-root")
        disks = parse_lsblk(tree(
            node("sda", "disk", GiB, maj_min="8:0", serial="A", children=[node("sda1", "part", GiB, maj_min="8:1", children=[lv])]),
            node("sdb", "disk", GiB, maj_min="8:16", serial="B", children=[node("sdb1", "part", GiB, maj_min="8:17", children=[lv])]),
        ))
        assert find_disk(disks, "/dev/mapper/vg0-root").name == "vg0-root"

    def test_distinct_devices_sharing_a_path_stay_ambiguous(self):
        disks = parse_lsblk(tree(node("x", "disk", GiB, maj_min="1:1", path="/dev/x"), node("x", "disk", GiB, maj_min="1:2", path="/dev/x")))
        with pytest.raises(ValueError, match="ambiguous"):
            find_disk(disks, "/dev/x")

    def test_missing_device_is_reported_as_absent(self):
        with pytest.raises(ValueError, match="absent: /dev/sdz"):
            find_disk(parse_lsblk(TWO_MEMBER_MACHINE), "/dev/sdz")

    def test_malformed_lsblk_json_is_rejected(self):
        with pytest.raises(ValueError):
            parse_lsblk('{"blockdevices": "nope"}')

    def test_disk_is_the_blockdev_node_and_still_builds_from_dicts(self):
        assert Disk is blockdev.BlockDevice
        built = Disk.from_dict({"name": "sda", "path": "/dev/sda", "type": "disk", "size": "10", "mountpoints": [None, "/mnt"], "children": [{"name": "sda1", "type": "part"}]})
        assert built.size == 10 and built.mountpoints == ("/mnt",)
        assert built.children[0].path == "/dev/sda1"


class TestParentsAndHolders:
    def test_parent_disks_of_an_md_array_are_both_members(self):
        disks = parse_lsblk(TWO_MEMBER_MACHINE)
        assert [d.path for d in insp.parent_disks(disks, "/dev/md0")] == ["/dev/sda", "/dev/sdb"]
        assert [d.path for d in insp.parent_disks(disks, find_disk(disks, "/dev/sda1"))] == ["/dev/sda"]

    def test_loop_devices_are_parents_too(self):
        disks = parse_lsblk(tree(node("loop3", "loop", GiB, maj_min="7:3", children=[node("loop3p1", "part", GiB, maj_min="259:1")])))
        assert [d.path for d in insp.parent_disks(disks, "/dev/loop3p1")] == ["/dev/loop3"]

    def test_holders_are_listed_children_first_and_once(self):
        lv = node("vg0-root", "lvm", GiB, maj_min="253:1", path="/dev/mapper/vg0-root")
        crypt = node("luks", "crypt", GiB, maj_min="253:0", path="/dev/mapper/luks", children=[lv])
        disks = parse_lsblk(tree(node("sda", "disk", 2 * GiB, maj_min="8:0", children=[node("sda1", "part", GiB, maj_min="8:1", children=[crypt])])))
        assert [h.type for h in insp.holders(disks[0])] == ["lvm", "crypt"]
        md = parse_lsblk(TWO_MEMBER_MACHINE)
        assert [h.path for h in insp.holders(md[0])] == ["/dev/md0"]

    def test_a_disk_without_stacked_devices_has_no_holders(self):
        disks = parse_lsblk(tree(node("sda", "disk", GiB, maj_min="8:0", children=[node("sda1", "part", GiB, maj_min="8:1")])))
        assert insp.holders(disks[0]) == []

    def test_kernel_name_is_never_an_identity_for_a_real_disk(self):
        disk = Disk.from_dict({"name": "sda", "path": "/dev/sda", "type": "disk", "serial": "S1", "wwn": "W1"})
        assert insp.disk_identities(disk) == {"S1", "W1"}
        bare = Disk.from_dict({"name": "vda", "path": "/dev/vda", "type": "disk"})
        assert insp.disk_identities(bare) == set()
        loop = Disk.from_dict({"name": "loop0", "path": "/dev/loop0", "type": "loop"})
        assert insp.disk_identities(loop) == {"/dev/loop0"}


class TestInstallTargets:
    @pytest.mark.parametrize(
        ("raw", "problem"),
        [
            ({"name": "zram0", "type": "disk", "size": GiB}, "virtual"),
            ({"name": "ram0", "type": "disk", "size": GiB}, "virtual"),
            ({"name": "fd0", "type": "disk", "size": 1474560}, "virtual"),
            ({"name": "nbd0", "type": "disk", "size": GiB}, "virtual"),
            ({"name": "sr0", "type": "rom", "size": GiB}, "virtual"),
            ({"name": "loop0", "type": "loop", "size": GiB}, "virtual"),
            ({"name": "mmcblk0boot0", "type": "disk", "size": 4194304, "ro": True}, "firmware_area"),
            ({"name": "mmcblk0rpmb", "type": "disk", "size": 4194304}, "firmware_area"),
            ({"name": "sdc", "type": "disk", "size": 0, "rm": True}, "empty"),
            ({"name": "sdd", "type": "disk", "size": GiB, "ro": True}, "read_only"),
            ({"name": "dm-0", "type": "lvm", "size": GiB}, "not_a_disk"),
            ({"name": "sda", "type": "disk", "size": 500 * GiB}, ""),
            ({"name": "nvme0n1", "type": "disk", "size": 500 * GiB}, ""),
            ({"name": "mmcblk0", "type": "disk", "size": 64 * GiB}, ""),
            ({"name": "vda", "type": "disk", "size": 80 * GiB}, ""),
        ],
    )
    def test_problems(self, raw, problem):
        assert blockdev.install_target_problem(blockdev.BlockDevice.from_lsblk(raw)) == problem

    def test_loop_devices_are_allowed_only_on_request(self):
        loop = blockdev.BlockDevice.from_lsblk({"name": "loop5", "type": "loop", "size": GiB})
        assert blockdev.install_target_problem(loop, allow_loop=True) == ""

    def test_live_medium_is_not_a_target(self):
        stick = blockdev.BlockDevice.from_lsblk({"name": "sdb", "type": "disk", "size": 8 * GiB, "maj:min": "8:16"})
        assert blockdev.install_target_problem(stick, live_keys={"8:16"}) == "live_medium"
        assert [d.name for d in blockdev.install_target_disks([stick], {"8:16"})] == []
        assert [d.name for d in blockdev.install_target_disks([stick], set())] == ["sdb"]
        assert set(blockdev.TARGET_PROBLEMS) >= {"virtual", "empty", "read_only", "live_medium", "firmware_area", "not_a_disk"}


class TestByPathLinks:
    def make(self, tmp_path):
        device = tmp_path / "dev-vda"
        device.write_text("")
        other = tmp_path / "dev-vdb"
        other.write_text("")
        links = tmp_path / "by-path"
        links.mkdir()
        (links / "pci-0000:00:04.0").symlink_to(device)
        (links / "virtio-pci-0000:00:04.0").symlink_to(device)
        (links / "pci-0000:00:04.0-part1").symlink_to(tmp_path / "dev-vda-part1")
        (links / "pci-0000:00:05.0").symlink_to(other)
        return device, other, links

    def test_names_prefer_pci_over_the_virtio_duplicate_and_skip_partitions(self, tmp_path):
        device, _, links = self.make(tmp_path)
        assert insp.by_path_names(str(device), links) == ["pci-0000:00:04.0", "virtio-pci-0000:00:04.0"]

    def test_resolve_by_path(self, tmp_path):
        device, other, links = self.make(tmp_path)
        assert insp.resolve_by_path("pci-0000:00:05.0", links) == str(other)
        assert insp.resolve_by_path("pci-0000:00:09.0", links) == ""
        assert insp.resolve_by_path("../escape", links) == ""
        assert insp.resolve_by_path("", links) == ""

    def test_missing_directory_gives_no_names(self, tmp_path):
        assert insp.by_path_names("/dev/vda", tmp_path / "nope") == []


class TestDiscovery:
    def test_disks_use_the_shared_lsblk_command(self):
        runner = FakeRunner()
        runner.respond("lsblk", TWO_MEMBER_MACHINE)
        Discovery(runner).disks()
        assert runner.commands[0] == blockdev.lsblk_argv()

    def test_preview_runner_reads_real_disks_through_a_read_only_reader(self):
        discovery = Discovery(DryRunRunner())
        assert isinstance(discovery.reader, ReadOnlyRunner)
        with pytest.raises(RunnerError) as refused:
            discovery.reader.run(("sgdisk", "--zap-all", "/dev/sda"))
        assert refused.value.returncode == 126
        with pytest.raises(RunnerError):
            discovery.reader.run(("mdadm", "--stop", "/dev/md0"))

    def test_other_runners_are_used_as_they_are(self):
        runner = FakeRunner()
        assert Discovery(runner).reader is runner

    def test_missing_lsblk_is_a_finding_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(insp, "_network_ready", lambda *a, **k: True)

        class NoLsblk:
            def run(self, argv, **kwargs):
                raise FileNotFoundError("lsblk")

        result = Discovery(NoLsblk()).inspect()
        assert result.disks == []
        assert any(f["code"] == "preflight.disks" and f["severity"] == "error" for f in result.findings)
        assert any("Disk discovery failed" in message for message in result.blocking_errors())

    def test_network_probe_targets_the_configured_mirror_and_proxy(self, monkeypatch):
        calls = []
        monkeypatch.setattr(insp, "_network_ready", lambda host, port, proxy="", timeout=5.0: calls.append((host, port, proxy)) or False)
        runner = FakeRunner()
        runner.respond("lsblk", TWO_MEMBER_MACHINE)
        discovery = Discovery(runner, mirror="https://mirror.example.org:8443/gentoo", proxy="http://proxy.lan:3128")
        result = discovery.inspect()
        assert calls == [("mirror.example.org", 8443, "http://proxy.lan:3128")]
        message = next(f["message"] for f in result.findings if f["code"] == "preflight.network")
        assert "mirror.example.org:8443" in message and "proxy.lan:3128" in message
        discovery.configure(mirror="https://other.example", proxy="")
        discovery.inspect()
        assert calls[-1] == ("other.example", 443, "")

    def test_memory_advice_is_a_warning_with_a_code_not_an_error(self, monkeypatch):
        monkeypatch.setattr(insp, "_network_ready", lambda *a, **k: True)
        monkeypatch.setattr(insp.Path, "read_text", lambda self, *a, **k: "MemTotal:  1048576 kB\n" if str(self) == "/proc/meminfo" else "")
        runner = FakeRunner()
        runner.respond("lsblk", TWO_MEMBER_MACHINE)
        result = Discovery(runner).inspect()
        assert "At least 2 GiB of memory is recommended" in result.warnings
        assert all(f["severity"] == "warning" for f in result.findings if f["code"] == "preflight.memory_low")
        assert "At least 2 GiB of memory is recommended" not in result.blocking_errors()

    def test_inspection_without_findings_keeps_every_legacy_error_blocking(self):
        legacy = Inspection("x86_64", 8 * GiB, True, True, {}, [], ["Something is wrong"])
        assert legacy.blocking_errors() == ["Something is wrong"]
        assert legacy.to_dict()["findings"] == [] and legacy.to_dict()["live_medium"] == []

    def test_live_medium_by_mount_source_and_by_label(self, tmp_path):
        cmdline = tmp_path / "cmdline"
        cmdline.write_text("BOOT_IMAGE=/boot/gentoo root=live:CDLABEL=GENTOO_INSTALLER_AMD64 rd.live.image\n")
        machine = parse_lsblk(tree(
            node("sda", "disk", 100 * GiB, maj_min="8:0", serial="S", children=[node("sda1", "part", GiB, maj_min="8:1")]),
            node("sdb", "disk", 8 * GiB, maj_min="8:16", children=[node("sdb1", "part", GiB, maj_min="8:17", label="GENTOO_INSTALLER_AMD64", fstype="iso9660")]),
            node("sdc", "disk", 8 * GiB, maj_min="8:32", children=[node("sdc1", "part", GiB, maj_min="8:33")]),
        ))
        runner = FakeRunner()
        by_label = Discovery(runner, cmdline_path=cmdline).live_medium_keys(machine)
        assert by_label == {"8:17", "8:16"}
        runner = FakeRunner()
        runner.respond("findmnt --noheadings --output SOURCE --mountpoint /run/initramfs/live", "/dev/sdc1\n")
        by_mount = Discovery(runner, cmdline_path=tmp_path / "missing").live_medium_keys(machine)
        assert by_mount == {"8:33", "8:32"}

    def test_btrfs_style_mount_source_suffix_is_ignored(self, tmp_path):
        machine = parse_lsblk(tree(node("sdb", "disk", 8 * GiB, maj_min="8:16", children=[node("sdb1", "part", GiB, maj_min="8:17")])))
        runner = FakeRunner()
        runner.respond("findmnt --noheadings --output SOURCE --mountpoint /run/initramfs/live", "/dev/sdb1[/live]\n")
        assert Discovery(runner, cmdline_path=tmp_path / "missing").live_medium_keys(machine) == {"8:17", "8:16"}

    def test_inspect_reports_live_medium_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(insp, "_network_ready", lambda *a, **k: True)
        machine = tree(node("sdb", "disk", 8 * GiB, maj_min="8:16", children=[node("sdb1", "part", GiB, maj_min="8:17")]))
        runner = FakeRunner()
        runner.respond("lsblk", machine)
        runner.respond("findmnt --noheadings --output SOURCE --mountpoint /run/initramfs/live", "/dev/sdb1\n")
        result = Discovery(runner, cmdline_path=tmp_path / "missing").inspect()
        assert result.live_medium == ["/dev/sdb", "/dev/sdb1"]


class TestNetworkProbe:
    def serve(self, reply: bytes):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        seen: list[bytes] = []

        def accept():
            while True:
                try:
                    connection, _ = server.accept()
                except OSError:
                    return
                with connection:
                    seen.append(connection.recv(512))
                    connection.sendall(reply)

        threading.Thread(target=accept, daemon=True).start()
        return server, seen

    def test_mirror_endpoint(self):
        assert insp.mirror_endpoint("") == ("distfiles.gentoo.org", 443)
        assert insp.mirror_endpoint("https://mirror.example.org/gentoo") == ("mirror.example.org", 443)
        assert insp.mirror_endpoint("https://mirror.example.org:8443") == ("mirror.example.org", 8443)
        assert insp.mirror_endpoint("https://[bad") == ("distfiles.gentoo.org", 443)

    def test_direct_connection(self):
        server, _ = self.serve(b"")
        try:
            assert insp._network_ready("127.0.0.1", server.getsockname()[1], timeout=2)
        finally:
            server.close()
        assert not insp._network_ready("127.0.0.1", 1, timeout=1)

    def test_connect_through_an_http_proxy(self):
        ok, seen = self.serve(b"HTTP/1.1 200 Connection established\r\n\r\n")
        denied, _ = self.serve(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        auth, _ = self.serve(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        try:
            assert insp._network_ready("mirror.example.org", 443, f"http://127.0.0.1:{ok.getsockname()[1]}", timeout=2)
            assert seen[0].startswith(b"CONNECT mirror.example.org:443 HTTP/1.1")
            assert not insp._network_ready("mirror.example.org", 443, f"127.0.0.1:{denied.getsockname()[1]}", timeout=2)
            assert not insp._network_ready("mirror.example.org", 443, f"http://127.0.0.1:{auth.getsockname()[1]}", timeout=2)
        finally:
            for server in (ok, denied, auth):
                server.close()

    def test_unreachable_proxy_is_a_failed_probe_not_an_exception(self):
        assert insp._network_ready("mirror.example.org", 443, "http://127.0.0.1:1", timeout=1) is False
        assert insp._network_ready("mirror.example.org", 443, "http://", timeout=1) is False

    def test_socks_proxy_only_has_to_answer(self):
        server, _ = self.serve(b"")
        try:
            assert insp._network_ready("mirror.example.org", 443, f"socks5://127.0.0.1:{server.getsockname()[1]}", timeout=2)
        finally:
            server.close()


class TestVolumeSets:
    LVM_MACHINE = tree(
        node("sda", "disk", 10 * GiB, maj_min="8:0", serial="A", children=[node("sda1", "part", 9 * GiB, maj_min="8:1", fstype="LVM2_member")]),
        node("sdb", "disk", 10 * GiB, maj_min="8:16", serial="B", children=[node("sdb1", "part", 9 * GiB, maj_min="8:17", fstype="LVM2_member")]),
        node("sdc", "disk", 10 * GiB, maj_min="8:32", serial="C", children=[node("sdc1", "part", 9 * GiB, maj_min="8:33", fstype="LVM2_member")]),
    )

    def test_vg_spanning_two_disks_is_found_with_pvs(self):
        disks = parse_lsblk(self.LVM_MACHINE)
        runner = FakeRunner()
        runner.respond("pvs", "  /dev/sda1:vg0\n  /dev/sdb1:vg0\n  /dev/sdc1:other\n")
        [found] = insp.find_volume_sets(runner, disks, [disks[0]])
        assert (found.kind, found.name, found.inside, found.outside) == ("lvm", "vg0", ("/dev/sda1",), ("/dev/sdb1",))
        assert found.spans_other_disks and "vg0" in found.describe() and "/dev/sdb1" in found.describe()

    def test_vg_wholly_on_the_disk_does_not_span(self):
        disks = parse_lsblk(self.LVM_MACHINE)
        runner = FakeRunner()
        runner.respond("pvs", "  /dev/sda1:vg0\n  /dev/sdc1:other\n")
        [found] = insp.find_volume_sets(runner, disks, [disks[0]])
        assert not found.spans_other_disks

    def test_pv_unknown_to_pvs_is_reported_without_a_name(self):
        disks = parse_lsblk(self.LVM_MACHINE)
        [found] = insp.find_volume_sets(FakeRunner(), disks, [disks[0]])
        assert found.name == "" and not found.spans_other_disks

    def test_md_array_with_a_member_on_another_disk(self):
        machine = tree(
            node("sda", "disk", 10 * GiB, maj_min="8:0", serial="A", children=[node("sda1", "part", 9 * GiB, maj_min="8:1", fstype="linux_raid_member")]),
            node("sdb", "disk", 10 * GiB, maj_min="8:16", serial="B", children=[node("sdb1", "part", 9 * GiB, maj_min="8:17", fstype="linux_raid_member")]),
            node("sdc", "disk", 10 * GiB, maj_min="8:32", serial="C", children=[node("sdc1", "part", 9 * GiB, maj_min="8:33", fstype="linux_raid_member")]),
        )
        disks = parse_lsblk(machine)
        runner = FakeRunner()
        runner.respond("mdadm --examine /dev/sda1", "/dev/sda1:\n    Array UUID : aaaa:bbbb\n  Raid Level : raid1\n")
        runner.respond("mdadm --examine /dev/sdb1", "/dev/sdb1:\n    Array UUID : aaaa:bbbb\n")
        runner.respond("mdadm --examine /dev/sdc1", "/dev/sdc1:\n    Array UUID : cccc:dddd\n")
        [found] = insp.find_volume_sets(runner, disks, [disks[0]])
        assert (found.kind, found.name, found.outside) == ("md", "aaaa:bbbb", ("/dev/sdb1",))

    def test_leaf_without_a_known_signature_is_probed_with_blkid(self):
        disks = parse_lsblk(tree(node("sda", "disk", 10 * GiB, maj_min="8:0", serial="A")))
        runner = FakeRunner()
        runner.respond("blkid --probe", "LVM2_member\n")
        [found] = insp.find_volume_sets(runner, disks, [disks[0]])
        assert found.kind == "lvm" and found.inside == ("/dev/sda",)
        assert ("blkid", "--probe", "--output", "value", "--match-tag", "TYPE", "/dev/sda") in runner.commands

    def test_blank_disk_has_no_volume_sets(self):
        disks = parse_lsblk(tree(node("sda", "disk", 10 * GiB, maj_min="8:0", serial="A")))
        runner = FakeRunner()
        runner.failures["blkid"] = 2
        assert insp.find_volume_sets(runner, disks, [disks[0]]) == []
