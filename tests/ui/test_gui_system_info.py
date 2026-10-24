"""system_info: disk discovery on the shared blockdev parser, identity, probes, merging."""

from __future__ import annotations

import json
import ssl
import subprocess
import time

import pytest

from gentoo_installer import system_info
from gentoo_installer.daemon_client import DaemonState
from gentoo_installer.manifest import InstallManifest, Partition
from gentoo_installer.system_info import DiskInfo, PartitionInfo, SystemInfo

GIB = 1024**3
ESP = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
MSDATA = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
LINUX = "0fc63daf-8483-4772-8e79-3d69d8477de4"


MINORS: dict[str, int] = {}


def node(name, type_="disk", size=0, **extra):
    minor = MINORS.setdefault(name, len(MINORS) + 1)  # md0 repeats under both members: same device
    record = {
        "name": name, "kname": name, "path": f"/dev/{name}", "maj:min": extra.pop("majmin", f"8:{minor}"),
        "type": type_, "size": size, "ro": False, "rm": False, "hotplug": False, "mountpoints": [None],
    }
    record.update(extra)
    return record


def lsblk_fixture():
    sda = node("sda", size=500 * GIB, model="Samsung SSD 870", serial="S5Y1NX0T123456", tran="sata", pttype="gpt", children=[
        node("sda1", "part", 260 * 1024**2, fstype="vfat", parttype=ESP, partlabel="EFI system partition", label="SYSTEM"),
        node("sda2", "part", 450 * GIB, fstype="ntfs", parttype=MSDATA, label="Windows"),
        node("sda3", "part", 40 * GIB, fstype="ext4", parttype=LINUX, label="old-gentoo"),
    ])
    return {"blockdevices": [
        sda,
        node("sdb", size=64 * GIB, model="QEMU HARDDISK", pttype="", tran="virtio"),  # no serial, no WWN
        node("sdc", size=0, model="SD reader"),  # empty card reader
        node("zram0", size=4 * GIB, mountpoints=["[SWAP]"]),
        node("loop0", "loop", 2 * GIB),
        node("sr0", "rom", 700 * 1024**2, model="DVD"),
        node("mmcblk0boot0", size=4 * 1024**2, ro=True),
        node("sdd", size=16 * GIB, model="USB stick", serial="USB123", rm=True, tran="usb", fstype="iso9660", label="GENTOO_INSTALLER_AMD64", children=[
            node("sdd1", "part", 16 * GIB, fstype="iso9660", label="GENTOO_INSTALLER_AMD64", mountpoints=["/run/initramfs/live"]),
        ]),
        node("sde", size=128 * GIB, model="Backup", wwn="0x5000c500a1b2c3d4", children=[
            node("sde1", "part", 64 * GIB, children=[node("md0", "raid1", 64 * GIB, fstype="ext4")]),
            node("sde2", "part", 64 * GIB, children=[node("md0", "raid1", 64 * GIB, fstype="ext4")]),
        ]),
    ]}


@pytest.fixture
def fake_lsblk(monkeypatch, tmp_path):
    def run(argv, *args, **kwargs):
        if argv[0] == "lsblk":
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(lsblk_fixture()), stderr="")
        if argv[0] == "findmnt":
            return subprocess.CompletedProcess(argv, 0, stdout="/dev/sdd1\n", stderr="")
        raise OSError(argv[0])

    monkeypatch.setattr(subprocess, "run", run)
    by_path = tmp_path / "by-path"
    by_path.mkdir()
    (by_path / "pci-0000:00:04.0-ata-1").symlink_to("/dev/sdb")
    (by_path / "pci-0000:00:04.0-ata-1-part1").symlink_to("/dev/sdb1")
    (by_path / "virtio-pci-0000:00:04.0").symlink_to("/dev/sdb")
    monkeypatch.setattr(system_info, "BY_PATH_DIR", by_path)


def test_discovery_filters_zram_loop_rom_boot_partitions_and_empty_card_readers(fake_lsblk):
    paths = [disk.path for disk in system_info.discover_disks()]
    assert paths == ["/dev/sda", "/dev/sdb", "/dev/sdd", "/dev/sde"]


def test_identity_text_and_missing_identity(fake_lsblk):
    disks = {disk.path: disk for disk in system_info.discover_disks()}
    assert disks["/dev/sda"].identity_text == "serial …123456"
    assert disks["/dev/sda"].has_identity and disks["/dev/sda"].stable_id == "S5Y1NX0T123456"
    assert disks["/dev/sde"].identity_text.startswith("WWN …")
    assert not disks["/dev/sdb"].has_identity
    assert disks["/dev/sdb"].identity_text == "no serial/WWN"


def test_path_identity_uses_the_first_whole_disk_link_size_and_raw_model(fake_lsblk):
    disk = next(d for d in system_info.discover_disks() if d.path == "/dev/sdb")
    # the alphabetically first link, never a '-part' link
    assert disk.by_path == "pci-0000:00:04.0-ata-1"
    assert disk.path_identity() == f"pci-0000:00:04.0-ata-1|{64 * GIB}|QEMU HARDDISK"
    # the manifest validator accepts exactly this shape
    manifest = InstallManifest()
    manifest.storage.device = disk.path
    manifest.storage.device_id_kind = "path"
    manifest.storage.device_id = disk.path_identity()
    assert not [e for e in manifest.validate_sections()["storage"] if "serial" in e]


def test_path_identity_matches_the_daemon_side_function(fake_lsblk):
    from gentoo_installer import blockdev

    disk = next(d for d in system_info.discover_disks() if d.path == "/dev/sdb")
    raw = next(item for item in lsblk_fixture()["blockdevices"] if item["name"] == "sdb")
    block = blockdev.BlockDevice.from_lsblk(raw)
    assert blockdev.identity_matches(block, "path", disk.path_identity(), disk.by_path)


def test_disk_without_a_by_path_link_cannot_form_a_path_identity():
    assert DiskInfo("/dev/vdz", "x", GIB).path_identity() == ""


def test_live_medium_is_found_by_mount_source_and_label(fake_lsblk):
    disks = {disk.path: disk for disk in system_info.discover_disks()}
    assert disks["/dev/sdd"].live_medium
    assert not disks["/dev/sda"].live_medium
    assert not disks["/dev/sdd"].selectable


def test_live_medium_is_found_by_dracut_label_after_copy_to_ram(fake_lsblk, monkeypatch):
    monkeypatch.setattr(system_info, "_mount_sources", lambda: [])
    monkeypatch.setattr(system_info, "_kernel_cmdline", lambda: "quiet root=live:CDLABEL=GENTOO_INSTALLER_AMD64 rd.live.ram")
    assert {d.path for d in system_info.discover_disks() if d.live_medium} == {"/dev/sdd"}


def test_contents_text_lists_partitions_and_blank_disks(fake_lsblk):
    disks = {disk.path: disk for disk in system_info.discover_disks()}
    text = disks["/dev/sda"].contents_text
    assert text.startswith("3 partitions: vfat 260.0 MiB “SYSTEM”, ntfs 450.0 GiB “Windows”")
    assert disks["/dev/sdb"].contents_text == "blank (no partition table)"
    assert DiskInfo("/dev/x", "m", GIB, partitions=[PartitionInfo("/dev/x1", GIB, "ext4")]).contents_text == "1 partition: ext4 1.0 GiB"


def test_partition_kinds(fake_lsblk):
    info = SystemInfo("x86_64", 8, True, system_info.discover_disks(), True, True, [])
    assert info.find_partition("/dev/sda1").is_esp
    assert info.find_partition("/dev/sda2").is_windows
    assert not info.find_partition("/dev/sda3").is_windows
    assert info.find_partition("/dev/nothing") is None
    assert info.find_disk("/dev/sdb").path == "/dev/sdb"


def test_stacked_volumes_are_listed_once_and_reported_as_active(fake_lsblk):
    disk = next(d for d in system_info.discover_disks() if d.path == "/dev/sde")
    assert [p.path for p in disk.partitions].count("/dev/md0") == 1
    assert disk.active_volumes == ["raid1 /dev/md0"]
    assert "md0" not in disk.contents_text


def test_discovery_survives_a_missing_lsblk(monkeypatch):
    def broken(*args, **kwargs):
        raise FileNotFoundError("lsblk")

    monkeypatch.setattr(subprocess, "run", broken)
    assert system_info.discover_disks() == []


def test_pending_info_has_instant_facts_only():
    info = SystemInfo.pending()
    assert not info.inspected and info.source == "pending"
    assert info.network is None and info.disks == []
    assert info.architecture


def test_required_tools_are_a_superset_of_the_daemon_preflight():
    from gentoo_installerd.inspect import REQUIRED_TOOLS

    assert set(REQUIRED_TOOLS) <= set(system_info.REQUIRED_TOOLS)


def test_tools_for_selection_follow_the_daemon_rules():
    manifest = InstallManifest()
    manifest.storage.firmware = "uefi"
    manifest.storage.luks = True
    manifest.storage.filesystem = "btrfs"
    manifest.system.bootloader = "efi-stub"
    assert system_info.tools_for_selection(manifest) == ["cryptsetup", "efibootmgr", "mkfs.btrfs", "mkfs.vfat", "swapoff", "swapon"]
    manifest.storage.mode = "manual"
    manifest.storage.partitions = [Partition("/dev/sda1", "/", "xfs", format=True), Partition("/dev/sda2", "/home", "ext4", format=False)]
    assert "mkfs.xfs" in system_info.tools_for_selection(manifest)
    assert "mkfs.ext4" not in system_info.tools_for_selection(manifest)
    missing = system_info.missing_for_selection(manifest, {"cryptsetup": False, "mkfs.xfs": True, "mkfs.btrfs": True, "mkfs.vfat": True, "swapon": True, "swapoff": True, "efibootmgr": True})
    assert missing == ["cryptsetup"]


# --- network and clock ----------------------------------------------------------------------


def test_probe_reports_the_skew_of_a_reachable_mirror(monkeypatch):
    monkeypatch.setattr(system_info, "_head_date", lambda host, proxy, timeout, context: 3600.0)
    probe = system_info.probe_mirror("mirror.example")
    assert probe.reachable and probe.tls_ok and probe.skew_seconds == 3600.0


def test_probe_with_a_clock_that_breaks_tls_still_reports_the_skew(monkeypatch):
    def head_date(host, proxy, timeout, context):
        if context.verify_mode == ssl.CERT_REQUIRED:
            error = ssl.SSLCertVerificationError(1, "certificate verify failed: certificate is not yet valid")
            error.verify_message = "certificate is not yet valid"
            raise error
        return 400 * 86400.0

    monkeypatch.setattr(system_info, "_head_date", head_date)
    probe = system_info.probe_mirror("mirror.example")
    assert probe.reachable and probe.tls_ok is False
    assert probe.skew_seconds == 400 * 86400.0
    assert "clock" in probe.error


def test_probe_of_an_unreachable_mirror(monkeypatch):
    def head_date(host, proxy, timeout, context):
        raise OSError("Network is unreachable")

    monkeypatch.setattr(system_info, "_head_date", head_date)
    probe = system_info.probe_mirror("mirror.example")
    assert not probe.reachable and "unreachable" in probe.error


def test_probe_returns_by_the_deadline_when_the_resolver_hangs(monkeypatch):
    monkeypatch.setattr(system_info, "_head_date", lambda *args: time.sleep(30))
    started = time.monotonic()
    probe = system_info.probe_mirror("mirror.example", deadline=0.4)
    assert time.monotonic() - started < 3
    assert not probe.reachable and "timed out" in probe.error


def test_proxy_is_used_for_the_probe_tunnel():
    host, port, headers = system_info._proxy_parts("http://alice:S3cret@proxy.corp:3128")
    assert (host, port) == ("proxy.corp", 3128)
    assert headers["Proxy-Authorization"].startswith("Basic ")
    assert system_info._proxy_parts("") is None
    assert system_info._proxy_parts("socks5://proxy:1080") is None


@pytest.mark.parametrize(
    ("skew", "tls", "expected"),
    [(5, True, "ok"), (-100, True, "ok"), (3000, True, "warning"), (-3 * 86400, True, "error"), (600, False, "error")],
)
def test_clock_status_thresholds(skew, tls, expected):
    info = SystemInfo("x86_64", 8, True, [], True, False, [], clock_skew=skew, tls_ok=tls)
    status, message = info.clock_status()
    assert status == expected, message


def test_clock_status_for_a_tls_failure_without_a_date():
    status, message = SystemInfo("x86_64", 8, True, [], True, False, [], tls_ok=False).clock_status()
    assert status == "error" and "clock" in message


# --- merging the daemon's inspect reply -------------------------------------------------------


def test_merge_inspection_prefers_the_daemons_view_of_tools_network_and_warnings():
    local = SystemInfo("x86_64", 8, False, [DiskInfo("/dev/sda", "d", 64 * GIB, serial="s")], False, False, [])
    data = {
        "architecture": "x86_64", "memory_bytes": 4 * GIB, "uefi": True, "network": True,
        "tools": {"sgdisk": True, "parted": False, "partprobe": False, "mkfs.ext4": True},
        "errors": ["Missing required tools: parted, partprobe"],
        "warnings": [{"code": "hw.fw_sb_enabled", "severity": "warning", "message": "Secure Boot is on", "action": "restart_firmware"}, "junk"],
        "hardware": {"clock": {"ntp_synced": True}},
        "clock": {"skew_seconds": 4000.0, "source": "https-date", "tls_ok": True},
        "disks": [{"name": "vdz", "path": "/dev/vdz", "type": "disk", "size": GIB}],
    }
    state = DaemonState("running")
    merged = system_info.merge_inspection(local, data, state)
    assert merged.source == "daemon" and merged.daemon is state
    assert merged.uefi and merged.network and merged.memory_gib == 4
    assert merged.missing_tools == ["parted", "partprobe"]
    assert [w["code"] for w in merged.warnings] == ["hw.fw_sb_enabled"]
    assert merged.clock_skew == 4000.0 and not merged.synchronized_clock
    assert [d.path for d in merged.disks] == ["/dev/sda"]  # local lsblk wins when it found disks


def test_the_daemons_by_path_and_live_medium_win_over_the_guis_guess():
    local = SystemInfo("x86_64", 8, True, [
        DiskInfo("/dev/vda", "QEMU", 64 * GIB, by_path="gui-guess"),
        DiskInfo("/dev/sdb", "Stick", 16 * GIB, serial="u"),
    ], True, True, [])
    data = {
        "architecture": "x86_64",
        "disks": [{"name": "vda", "path": "/dev/vda", "type": "disk", "size": 64 * GIB, "by_path": "pci-0000:00:04.0"}],
        "hardware": {"disks": [{"path": "/dev/sdb", "live_medium": True}, {"path": "/dev/vda", "live_medium": False}]},
    }
    merged = system_info.merge_inspection(local, data)
    by_path = {disk.path: disk for disk in merged.disks}
    assert by_path["/dev/vda"].by_path == "pci-0000:00:04.0" and not by_path["/dev/vda"].live_medium
    assert by_path["/dev/sdb"].live_medium and not by_path["/dev/sdb"].selectable


def test_merge_inspection_falls_back_to_the_daemons_disks():
    local = SystemInfo("x86_64", 8, True, [], True, True, [])
    data = {"architecture": "x86_64", "disks": [{"name": "vdz", "path": "/dev/vdz", "type": "disk", "size": 64 * GIB, "serial": "abc", "children": []}]}
    merged = system_info.merge_inspection(local, data)
    assert [d.path for d in merged.disks] == ["/dev/vdz"]
    assert merged.disks[0].serial == "abc"


def test_inspect_with_daemon_is_local_only_in_dev_mode():
    local = SystemInfo("x86_64", 8, True, [], True, True, [])
    info = system_info.inspect_with_daemon("/nonexistent", local=lambda: local, dev_mode=True)
    assert info.daemon.kind == "dev" and info.inspected


def test_inspect_with_daemon_without_a_service_keeps_the_local_result(tmp_path):
    local = SystemInfo("x86_64", 8, True, [DiskInfo("/dev/sda", "d", GIB)], True, True, [])
    info = system_info.inspect_with_daemon(str(tmp_path / "missing.sock"), local=lambda: local, dev_mode=False)
    assert info.daemon.kind == "not-running" and info.source == "local"
    assert [d.path for d in info.disks] == ["/dev/sda"]


def test_a_daemon_that_rejects_the_manifest_is_asked_again_without_it(fake_daemon):
    def handler(request):
        if request["action"] == "inspect" and "manifest" in request:
            return [{"type": "error", "error": "Unknown inspect field: manifest"}]
        if request["action"] == "inspect":
            return [{"type": "status", "data": {"architecture": "x86_64", "memory_bytes": 8 * GIB, "uefi": False, "network": True, "tools": {}, "errors": []}},
                    {"type": "completed", "data": {"success": True}}]
        return [{"type": "status", "data": {"status": "idle"}}]

    daemon = fake_daemon(handler)
    local = SystemInfo("x86_64", 2, True, [], False, False, [])
    info = system_info.inspect_with_daemon(daemon.path, local=lambda: local, dev_mode=False, request_payload={"manifest": {"storage": {}}})
    assert info.source == "daemon" and info.uefi is False and info.network
    assert [("manifest" in r) for r in daemon.requests if r["action"] == "inspect"] == [True, False]


def test_inspect_with_daemon_merges_the_reply_of_a_live_daemon(fake_daemon):
    def handler(request):
        if request["action"] == "inspect":
            return [
                {"type": "status", "data": {"architecture": "x86_64", "memory_bytes": 8 * GIB, "uefi": True, "network": True, "tools": {"sgdisk": True}, "errors": []}},
                {"type": "completed", "data": {"success": True}},
            ]
        return [{"type": "status", "data": {"status": "idle"}}]

    daemon = fake_daemon(handler)
    local = SystemInfo("x86_64", 2, False, [], False, False, [])
    info = system_info.inspect_with_daemon(daemon.path, local=lambda: local, dev_mode=False, request_payload={"manifest": {"storage": {}}})
    assert info.source == "daemon" and info.network and info.uefi
    inspect_request = next(r for r in daemon.requests if r["action"] == "inspect")
    assert inspect_request["manifest"] == {"storage": {}}
