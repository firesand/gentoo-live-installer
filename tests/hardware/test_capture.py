"""The fixture capture tool: it must reproduce what the probe saw and leave personal data behind."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from gentoo_installer import hardware as hw

from tests.hardware.helpers import DERIVED, FIXTURE_NAMES, FIXTURES, fixture_now, fixture_path
from tests.hardware.synthetic import make_edid

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("capture_hardware_fixture_under_test", ROOT / "scripts" / "capture-hardware-fixture.py")
capture_tool = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules[_spec.name] = capture_tool  # type: ignore[union-attr]
_spec.loader.exec_module(capture_tool)  # type: ignore[union-attr]


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_capturing_a_fixture_reproduces_its_profile(name, tmp_path):
    now = fixture_now(name)
    capture_tool.capture(fixture_path(name), tmp_path / "copy", now=now)
    original = hw.probe(fixture_path(name), now=now).to_dict(redact=True)
    copy = hw.probe(tmp_path / "copy", now=now).to_dict(redact=True)
    assert copy == original


def make_live_tree(root: Path) -> None:
    """A tiny /sys built from real symlinks, the way the kernel lays it out."""
    def put(path: str, text: str) -> None:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    nic = "sys/devices/pci0000:00/0000:00:1f.6"
    put(f"{nic}/class", "0x020000\n")
    put(f"{nic}/vendor", "0x8086\n")
    put(f"{nic}/device", "0x15b8\n")
    put(f"{nic}/net/enp0s31f6/carrier", "1\n")
    (root / "sys/bus/pci/drivers/e1000e").mkdir(parents=True)
    os.symlink("../../../bus/pci/drivers/e1000e", root / nic / "driver")
    (root / "sys/bus/pci/devices").mkdir(parents=True, exist_ok=True)
    os.symlink("../../../devices/pci0000:00/0000:00:1f.6", root / "sys/bus/pci/devices/0000:00:1f.6")
    os.symlink("../../../0000:00:1f.6", root / nic / "net/enp0s31f6/device")
    (root / "sys/class/net").mkdir(parents=True, exist_ok=True)
    os.symlink("../../devices/pci0000:00/0000:00:1f.6/net/enp0s31f6", root / "sys/class/net/enp0s31f6")
    os.symlink("../../devices/virtual/net/docker0", root / "sys/class/net/docker0")
    (root / "sys/devices/virtual/net/docker0").mkdir(parents=True)
    put("sys/firmware/efi/fw_platform_size", "64\n")
    put("sys/class/dmi/id/sys_vendor", "ACME\n")
    put("sys/class/dmi/id/product_name", "Workstation\n")
    put("sys/class/dmi/id/chassis_type", "3\n")
    put("sys/class/dmi/id/product_serial", "SECRET-SERIAL\n")  # never read, so never captured
    put("proc/cpuinfo", "processor\t: 0\nvendor_id\t: AuthenticAMD\nmodel name\t: Test CPU\nflags\t\t: fpu lm sse sse2\n\nprocessor\t: 1\nvendor_id\t: AuthenticAMD\n")
    put("proc/meminfo", "MemTotal: 8000000 kB\nMemAvailable: 6000000 kB\n")
    put("proc/cmdline", "BOOT_IMAGE=/vmlinuz root=UUID=1234 cryptdevice=/dev/sda2:secret quiet nomodeset\n")
    put("proc/bus/input/devices", 'I: Bus=0011 Vendor=0001 Product=0001 Version=ab41\nN: Name="Johns Keyboard"\nP: Phys=aa:bb:cc:00:11:22\nU: Uniq=\nH: Handlers=sysrq kbd leds event0\n')


def test_capture_from_a_tree_with_real_symlinks(tmp_path):
    live, copy = tmp_path / "live", tmp_path / "copy"
    make_live_tree(live)
    runner = type("NoTools", (), {"run": lambda self, argv, **k: (_ for _ in ()).throw(OSError("no tools"))})()
    original = hw.probe(live, runner, now=1.0)
    assert [n.iface for n in original.nics] == ["enp0s31f6"] and original.nics[0].driver == "e1000e"
    meta = capture_tool.capture(live, copy, now=1.0)
    assert meta["redacted"] is True
    replay = hw.probe(copy, now=1.0)
    assert replay.to_dict(redact=True) == original.to_dict(redact=True)
    assert not any(p.is_symlink() for p in copy.rglob("*")), "fixtures contain no real symlinks"
    links = json.loads((copy / ".links.json").read_text())
    assert links["/sys/class/net/enp0s31f6"].endswith("/net/enp0s31f6") and links["/sys/class/net/enp0s31f6/device/driver"].endswith("drivers/e1000e")
    assert not (copy / "sys/class/dmi/id/product_serial").exists()


def test_capture_redacts_the_kernel_command_line_and_input_names(tmp_path):
    live, copy = tmp_path / "live", tmp_path / "copy"
    make_live_tree(live)
    capture_tool.capture(live, copy, now=1.0)
    cmdline = (copy / "proc/cmdline").read_text()
    assert "nomodeset" in cmdline and "quiet" in cmdline and "1234" not in cmdline and "secret" not in cmdline
    devices = (copy / "proc/bus/input/devices").read_text()
    assert "Johns" not in devices and 'Name="Keyboard"' in devices and "Phys" not in devices and "aa:bb:cc:00" not in devices
    assert hw.probe(copy, now=1.0).input.keyboard


def test_no_redact_keeps_the_original_names(tmp_path):
    live, copy = tmp_path / "live", tmp_path / "copy"
    make_live_tree(live)
    capture_tool.capture(live, copy, redact=False, now=1.0)
    assert "Johns Keyboard" in (copy / "proc/bus/input/devices").read_text()


def test_capture_cli_refuses_to_overwrite(tmp_path, capsys):
    dest = tmp_path / "out"
    dest.mkdir()
    assert capture_tool.main([str(dest), "--root", str(FIXTURES / "vmware-guest")]) == 1
    assert "--force" in capsys.readouterr().err
    assert capture_tool.main([str(dest), "--root", str(FIXTURES / "vmware-guest"), "--force"]) == 0
    assert hw.probe(dest).machine.hypervisor == "vmware"


def test_edid_serial_is_wiped_and_the_checksum_stays_valid():
    blob = bytearray(make_edid(1920, 1080, 509, 286, "Example", "DEL"))
    blob[12:16] = b"\x78\x56\x34\x12"
    blob[90:93] = b"\x00\x00\x00"
    blob[93] = 0xFF  # serial number descriptor
    blob[95:108] = b"ABC123456789\n"
    blob[127] = (-sum(blob[:127])) & 0xFF
    clean = capture_tool.redact_edid(bytes(blob))
    assert clean[12:16] == b"\x00\x00\x00\x00" and b"ABC123" not in clean
    assert sum(clean[:128]) % 256 == 0
    assert hw.parse_edid(clean)["width"] == 1920 and hw.parse_edid(clean)["name"] == "Example"
    assert capture_tool.redact_edid(b"not an edid") == b"not an edid"


def test_lsblk_redaction_keeps_structure_and_removes_identity():
    raw = {"blockdevices": [{"name": "sda", "serial": "S123", "wwn": "0x500", "uuid": None, "ptuuid": "1111-2222", "label": None, "mountpoints": [None],
                             "children": [{"name": "sda1", "uuid": "aaaa", "partuuid": "bbbb", "label": "My Secret Disk", "partlabel": "backup",
                                           "mountpoints": ["/home/me/stuff", "/"], "fstype": "ext4"},
                                          {"name": "sda2", "label": "GENTOO_INSTALLER_AMD64", "fstype": "iso9660", "mountpoints": ["/run/initramfs/live"]}]}]}
    clean = json.loads(capture_tool.redact_lsblk(json.dumps(raw)))
    disk = clean["blockdevices"][0]
    assert disk["serial"].startswith("REDACTED") and disk["wwn"].startswith("REDACTED") and disk["ptuuid"].startswith("0000")
    first, second = disk["children"]
    assert first["label"] == "LABEL" and first["partlabel"] == "PARTLABEL" and first["uuid"].startswith("0000")
    assert first["mountpoints"] == ["/mnt/redacted", "/"] and first["fstype"] == "ext4"
    assert second["label"] == "GENTOO_INSTALLER_AMD64" and second["mountpoints"] == ["/run/initramfs/live"]  # the live-medium rules still work
    assert capture_tool.redact_lsblk("not json") == "not json"


def test_mac_addresses_and_usb_nic_names_are_replaced_consistently():
    writer = capture_tool.FixtureWriter(Path("/nonexistent"))
    first = writer.scrub_name("ps-controller-battery-90:b6:85:18:8c:ef")
    assert first == "ps-controller-battery-aa:bb:cc:dd:ee:01"
    assert writer.scrub_name("hid-90:B6:85:18:8C:EF-battery") == "hid-aa:bb:cc:dd:ee:01-battery"
    assert writer.scrub_name("enx00e04c680001") == "enx" + "aabbccddee02"
    assert writer.scrub_name("enp3s0") == "enp3s0"


def test_route_table_addresses_are_replaced():
    table = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
             "eth0\t00000000\t0100A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
             "eth0\t0000A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n")
    clean = capture_tool.redact_route(table)
    assert "A8C0" not in clean and "0100A8C0" not in clean
    assert "eth0\t00000000\t0101FEA9" in clean


def test_cpuinfo_is_trimmed_but_keeps_the_thread_count():
    text = "processor\t: 0\nmodel name\t: X\nflags\t\t: lm\n\n" + "".join(f"processor\t: {i}\nmodel name\t: X\nflags\t\t: lm\n\n" for i in range(1, 8))
    trimmed = capture_tool.trim_cpuinfo(text)
    assert hw.parse_cpuinfo(trimmed).threads == 8 and len(trimmed) < len(text)


def test_fixture_directories_hold_no_symlinks():
    for name in FIXTURE_NAMES:
        if name in DERIVED:
            continue
        assert not any(p.is_symlink() for p in (FIXTURES / name).rglob("*")), name


def test_fixture_directories_are_never_empty():
    """git drops empty directories; the capture and the builder leave a .keep marker in them."""
    for name in FIXTURE_NAMES:
        if name in DERIVED:
            continue
        for directory, children, files in os.walk(FIXTURES / name):
            assert children or files, f"{name}: {directory} would vanish from a git checkout"


def test_the_keep_marker_is_invisible_to_the_probe(tmp_path):
    (tmp_path / "sys/class/backlight").mkdir(parents=True)
    (tmp_path / "sys/class/backlight" / hw.KEEP_MARKER).write_text("")
    fs = hw.Fs(tmp_path)
    assert fs.listdir("/sys/class/backlight") == []
    assert hw.probe(tmp_path, now=1.0).power.has_backlight is False
