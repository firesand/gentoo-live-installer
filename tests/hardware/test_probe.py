from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest

from gentoo_installer import hardware as hw
from gentoo_installer.blockdev import parse_lsblk

from tests.hardware.helpers import DERIVED, FIXTURE_NAMES, FIXTURES, fixture_dict, fixture_now, fixture_path, probe_fixture


def codes(name: str) -> set[str]:
    return {w["code"] for w in fixture_dict(name)["warnings"]}


def test_fixtures_are_all_present_and_probe_cleanly():
    for name in FIXTURE_NAMES:
        profile = probe_fixture(name)
        assert profile.errors == [], name
        assert profile.cpu.threads and profile.memory.total_kib, name
        json.dumps(profile.to_dict())  # JSON-safe


def test_to_dict_has_the_documented_top_level_keys():
    expected = {"cpu", "memory", "machine", "firmware", "gpus", "displays", "storage_controllers", "disks", "nics", "power", "input", "audio",
                "bluetooth", "clock", "hypervisor", "warnings", "errors"}
    assert expected <= set(fixture_dict("asus-desktop-rtx4090"))
    for warning in fixture_dict("thinkpad-intel-nvidia-hybrid")["warnings"]:
        assert set(warning) == {"code", "severity", "message", "action"}


def test_this_host_asus_desktop_with_rtx_4090():
    d = fixture_dict("asus-desktop-rtx4090")
    assert d["cpu"]["vendor"] == "intel" and d["cpu"]["threads"] == 32 and d["cpu"]["x86_64_level"] == 3
    assert d["cpu"]["model_name"] == "Intel(R) Core(TM) i9-14900K"
    assert d["machine"]["form_factor"] == "desktop" and d["machine"]["chassis_type"] == 3 and d["machine"]["hypervisor"] == ""
    # the DualSense battery (scope=Device) must not make a desktop a laptop
    assert d["power"]["has_battery"] is False and d["power"]["ac_online"] is None
    assert d["input"]["touchpad"] is False  # the DualSense touchpad is not a laptop touchpad
    assert [g["vendor"] for g in d["gpus"]] == ["nvidia"]
    gpu = d["gpus"][0]
    assert (gpu["generation"], gpu["generation_guess"], gpu["driver"], gpu["boot_vga"], gpu["integrated"]) == ("turing+", False, "nvidia", True, False)
    assert gpu["name"] == "AD102 [GeForce RTX 4090]" and gpu["connected_outputs"] == 2
    assert d["firmware"]["mode"] == "uefi" and d["firmware"]["efi_bits"] == 64 and d["firmware"]["secure_boot"] == "disabled"
    assert d["firmware"]["can_reboot_to_firmware"] and d["firmware"]["efivars_writable"] is True and d["firmware"]["windows_boot_entry"] is False
    assert {n["iface"]: (n["kind"], n["driver"], n["primary"]) for n in d["nics"]} == {"eno2": ("ethernet", "igc", True), "wlp0s20f3": ("wifi", "iwlwifi", False)}
    assert all(disk["installable"] and disk["rota"] is False for disk in d["disks"]) and len(d["disks"]) == 6
    assert {disk["bus"] for disk in d["disks"]} == {"nvme", "sata"}
    assert any(c["kind"] == "raid" and c["driver"] == "vmd" for c in d["storage_controllers"])
    assert codes("asus-desktop-rtx4090") == {"hw.fw_sb_disabled", "hw.st_vmd_active"}  # no st_rst_no_disks: the NVMe drives are visible
    assert d["bluetooth"]["present"] is True and d["audio"]["needs_sof"] is False
    assert d["clock"]["ntp_synced"] is True and d["clock"]["rtc_mode"] == "utc"
    assert d["firmware"]["tpm"] == "2.0"


def test_host_cpu_flags_match_cpuid2cpuflags_and_the_cpuinfo_fallback():
    profile = probe_fixture("asus-desktop-rtx4090")
    assert profile.cpu.cpu_flags_source == "cpuid2cpuflags"
    assert profile.cpu.cpu_flags_x86 == ["aes", "avx", "avx2", "avx_vnni", "bmi1", "bmi2", "f16c", "fma3", "mmx", "mmxext", "pclmul", "popcnt", "rdrand",
                                         "sha", "sse", "sse2", "sse3", "sse4_1", "sse4_2", "ssse3", "vpclmulqdq"]
    from gentoo_installer import hardware_data as data
    assert data.cpuinfo_to_cpu_flags(profile.cpu.flags) == sorted(profile.cpu.cpu_flags_x86)  # the real tool and the fallback agree


def test_displays_of_the_host_report_physical_density():
    displays = {d["connector"]: d for d in fixture_dict("asus-desktop-rtx4090")["displays"]}
    assert displays["card1-DP-2"]["name"] == "38GN950" and displays["card1-DP-2"]["dpi"] == 111
    assert displays["card1-HDMI-A-1"]["width"] == 3840 and displays["card1-HDMI-A-1"]["dpi"] == 61  # a 70-inch TV


def test_desktop_with_an_idle_igpu_is_not_hybrid():
    profile = probe_fixture("desktop-igpu-idle")
    assert [(g.vendor, g.integrated, g.boot_vga, g.connected_outputs) for g in profile.gpus] == [("intel", True, False, 0), ("nvidia", False, True, 2)]
    assert not hw.is_hybrid(profile)
    assert "hw.gpu_hybrid" not in codes("desktop-igpu-idle")


def test_qemu_q35_virtio_vm():
    d = fixture_dict("qemu-q35-virtio")
    assert d["machine"]["hypervisor"] == "qemu" and d["machine"]["form_factor"] == "vm" and d["hypervisor"]["detected"] and d["hypervisor"]["name"] == "qemu"
    assert d["cpu"]["hypervisor_flag"] is True
    assert d["gpus"][0]["vendor"] == "virtio" and d["gpus"][0]["virtual"] is True and d["gpus"][0]["driver"] == "virtio-pci"
    assert d["firmware"]["secure_boot"] == "setup"
    disks = {disk["name"]: disk for disk in d["disks"]}
    assert disks["vda"]["bus"] == "virtio" and disks["vda"]["installable"] and disks["vda"]["by_path"] == "pci-0000:00:03.0" and not disks["vda"]["serial"]
    assert disks["sdb"]["live_medium"] and not disks["sdb"]["installable"] and disks["sdb"]["blocker"] == "live-medium"
    assert [n["iface"] for n in d["nics"]] == ["ens3"]  # the virtio PCI adapter is not reported twice
    assert d["nics"][0]["bus"] == "virtio"
    assert {"hw.vm_detected", "hw.fw_sb_setup"} <= codes("qemu-q35-virtio")


def test_bios_sata_machine_with_an_old_radeon():
    d = fixture_dict("old-bios-sata-radeon")
    assert d["firmware"] == {**d["firmware"], "mode": "bios", "efi_bits": 0, "secure_boot": "n/a"}
    assert d["cpu"]["vendor"] == "amd" and d["cpu"]["x86_64_level"] == 1 and d["cpu"]["threads"] == 2
    gpu = d["gpus"][0]
    assert (gpu["vendor"], gpu["generation"], gpu["driver"], gpu["integrated"]) == ("amd", "terascale", "radeon", False)
    sda = next(disk for disk in d["disks"] if disk["name"] == "sda")
    assert (sda["bus"], sda["rota"], sda["phy_sec"], sda["log_sec"], sda["signatures"]) == ("sata", True, 4096, 512, ["ntfs"])
    assert d["machine"]["form_factor"] == "desktop" and d["machine"]["chassis_type"] == 6
    assert codes("old-bios-sata-radeon") == set()


def test_intel_laptop_with_hybrid_nvidia_graphics_and_sof_audio():
    d = fixture_dict("thinkpad-intel-nvidia-hybrid")
    assert d["machine"]["form_factor"] == "laptop" and d["power"]["has_battery"] and d["power"]["ac_online"] is False
    assert d["power"]["battery_percent"] == 82 and d["power"]["has_lid"] and d["power"]["has_backlight"]
    igpu, dgpu = d["gpus"]
    assert (igpu["vendor"], igpu["integrated"], igpu["boot_vga"], igpu["connected_outputs"]) == ("intel", True, True, 1)
    assert (dgpu["vendor"], dgpu["generation"], dgpu["driver"], dgpu["connected_outputs"]) == ("nvidia", "turing+", "nouveau", None)
    # the suspended discrete GPU's connector (card1) must not be read: reading it would wake the card
    assert [x["connector"] for x in d["displays"]] == ["card0-eDP-1"]
    assert d["displays"][0]["internal"] and d["displays"][0]["dpi"] == 284
    assert d["audio"]["needs_sof"] is True
    assert d["input"]["touchpad"] and d["input"]["fingerprint_usb"] == ["06cb:00bd"] and d["bluetooth"]["present"] and d["peripherals"]["webcam"]
    assert d["clock"]["rtc_mode"] == "local" and d["clock"]["rtc_delta_s"] == 25200
    assert {"hw.gpu_hybrid", "hw.bat_ac_offline", "hw.fw_win_entry", "hw.clock_rtc_local"} <= codes("thinkpad-intel-nvidia-hybrid")
    assert hw.is_hybrid(probe_fixture("thinkpad-intel-nvidia-hybrid"))


def test_amd_laptop_with_sof_audio_and_secure_boot_on():
    d = fixture_dict("amd-laptop-sof")
    assert d["cpu"]["vendor"] == "amd" and d["machine"]["form_factor"] == "laptop"
    gpu = d["gpus"][0]
    assert (gpu["vendor"], gpu["generation"], gpu["driver"], gpu["integrated"]) == ("amd", "gcn3+", "amdgpu", True)
    assert d["audio"]["needs_sof"] is True
    assert d["firmware"]["secure_boot"] == "enabled"
    warning = next(w for w in d["warnings"] if w["code"] == "hw.fw_sb_enabled")
    assert warning["severity"] == "warning" and warning["action"] == "reboot_firmware"
    assert {n["kind"] for n in d["nics"]} == {"ethernet", "wifi"} and d["clock"]["ntp_synced"] is False
    assert not hw.is_hybrid(probe_fixture("amd-laptop-sof"))


def test_vmware_guest():
    d = fixture_dict("vmware-guest")
    assert d["machine"]["hypervisor"] == "vmware" and d["gpus"][0]["vendor"] == "vmware" and d["gpus"][0]["driver"] == "vmwgfx"
    sda = next(disk for disk in d["disks"] if disk["name"] == "sda")
    assert sda["bus"] == "scsi" and sda["installable"]
    assert [n["driver"] for n in d["nics"]] == ["vmxnet3"]


def test_intel_rst_raid_mode_hides_the_nvme_drive():
    d = fixture_dict("intel-rst-hidden-nvme")
    assert [disk["name"] for disk in d["disks"] if disk["installable"]] == []  # only the installer stick, which is not a target
    assert any(c["kind"] == "raid" and c["vendor_id"] == "8086" and c["driver"] == "ahci" for c in d["storage_controllers"])
    found = {w["code"]: w for w in d["warnings"]}
    assert found["hw.st_rst_no_disks"]["severity"] == "error" and "AHCI" in found["hw.st_rst_no_disks"]["message"]
    assert found["hw.st_rst_no_disks"]["action"] == "reboot_firmware" and "hw.st_no_disk" in found
    assert "hw.st_vmd_active" not in found


def test_live_medium_is_never_offered_for_installation():
    for name in FIXTURE_NAMES:
        for disk in probe_fixture(name).disks:
            assert disk.installable == (not disk.live_medium and not disk.ro and disk.size > 0), (name, disk.name)


# -- robustness: the probe never raises -------------------------------------------------------

def test_probe_of_an_empty_tree_returns_a_profile(tmp_path):
    profile = hw.probe(tmp_path, now=1.0)
    assert profile.firmware.mode == "bios" and profile.gpus == [] and profile.disks == []
    assert profile.cpu.threads == 0
    json.dumps(profile.to_dict())


def test_probe_of_a_missing_tree_returns_a_profile(tmp_path):
    profile = hw.probe(tmp_path / "does-not-exist", now=1.0)
    assert isinstance(profile, hw.HardwareProfile)


def test_probe_survives_garbage_in_every_file(tmp_path):
    for source in (FIXTURES / "thinkpad-intel-nvidia-hybrid").rglob("*"):
        target = tmp_path / source.relative_to(FIXTURES / "thinkpad-intel-nvidia-hybrid")
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif source.name != "pci.ids":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\xff\xfe\x00 not what the kernel prints \x00\x00" + os.urandom(16))
    profile = hw.probe(tmp_path, now=1.0)
    json.dumps(profile.to_dict())
    assert profile.errors == [] or all(":" in error for error in profile.errors)


def test_probe_survives_a_runner_that_raises_and_one_that_returns_junk(tmp_path):
    class Exploding:
        def run(self, argv, **kwargs):
            raise OSError("no such tool")

    class Junk:
        def run(self, argv, **kwargs):
            return object()

    class Failing:
        def run(self, argv, **kwargs):
            from types import SimpleNamespace
            return SimpleNamespace(returncode=3, stdout="CPU_FLAGS_X86: avx")

    for runner in (Exploding(), Junk(), Failing()):
        profile = hw.probe(FIXTURES / "thinkpad-intel-nvidia-hybrid", runner, now=fixture_now("thinkpad-intel-nvidia-hybrid"))
        assert profile.cpu.cpu_flags_source == "cpuinfo"  # cpuid2cpuflags unavailable: derived from /proc/cpuinfo
        assert profile.disks and profile.disks[0].rota is not None  # disks still come from /sys/block without lsblk
        assert profile.errors == []


def test_disks_without_lsblk_still_report_flash_and_sector_size():
    profile = hw.probe(FIXTURES / "old-bios-sata-radeon", type("R", (), {"run": lambda self, argv, **k: (_ for _ in ()).throw(OSError())})(),
                       now=fixture_now("old-bios-sata-radeon"))
    sda = next(disk for disk in profile.disks if disk.name == "sda")
    assert sda.rota is True and sda.phy_sec == 4096 and sda.size == 500 * 10**9 and sda.serial == ""
    assert next(disk for disk in profile.disks if disk.name == "sdb").live_medium  # recognised from the mount table alone


def test_a_fake_runner_can_feed_lsblk():
    calls: list[tuple[str, ...]] = []

    class Fake:
        def run(self, argv, **kwargs):
            from types import SimpleNamespace
            calls.append(tuple(argv))
            if argv[0] == "lsblk":
                payload = {"blockdevices": [{"name": "sda", "path": "/dev/sda", "type": "disk", "size": 1000, "serial": "ABC", "wwn": "w", "maj:min": "8:0",
                                             "tran": "sata", "model": "Fake Disk", "children": []}]}
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload))
            raise OSError

    profile = hw.probe(FIXTURES / "old-bios-sata-radeon", Fake(), now=fixture_now("old-bios-sata-radeon"))
    sda = next(disk for disk in profile.disks if disk.name == "sda")
    assert (sda.serial, sda.wwn, sda.tran) == ("ABC", "w", "sata")
    assert all(call[0] in hw.PROBE_COMMANDS for call in calls)


def test_the_local_runner_refuses_anything_that_is_not_a_probe_command():
    runner = hw.LocalRunner()
    for argv in (("rm", "-rf", "/"), ("sgdisk", "--zap-all", "/dev/sda"), ("/bin/sh", "-c", "true"), ()):
        with pytest.raises(ValueError):
            runner.run(argv)


def test_fixture_roots_never_execute_programs(tmp_path, monkeypatch):
    import subprocess

    def boom(*args, **kwargs):
        raise AssertionError("a fixture probe must not run programs")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    profile = hw.probe(FIXTURES / "amd-laptop-sof", now=fixture_now("amd-laptop-sof"))
    assert profile.cpu.cpu_flags_source == "cpuid2cpuflags"  # served from .commands/


def test_gentoo_hw_root_redirects_the_default_probe(monkeypatch):
    monkeypatch.setenv(hw.ENV_ROOT, str(FIXTURES / "vmware-guest"))
    profile = hw.probe(now=fixture_now("vmware-guest"))
    assert profile.machine.hypervisor == "vmware"
    explicit = hw.probe(FIXTURES / "qemu-q35-virtio", now=fixture_now("qemu-q35-virtio"))
    assert explicit.machine.hypervisor == "qemu"  # an explicit root wins over the environment


def test_the_cli_prints_json_and_a_report(capsys):
    assert hw.main(["--root", str(FIXTURES / "thinkpad-intel-nvidia-hybrid"), "--json", "--redact"]) == 0
    raw = json.loads(capsys.readouterr().out)
    assert raw["machine"]["form_factor"] == "laptop"
    assert hw.main(["--root", str(FIXTURES / "amd-laptop-sof")]) == 0
    assert "Secure Boot enabled" in capsys.readouterr().out
    assert hw.main(["--root", str(FIXTURES / "qemu-q35-virtio"), "--summary"]) == 0
    assert "virtual machine (qemu)" in capsys.readouterr().out


def test_summary_line_for_the_readiness_page():
    summary = probe_fixture("asus-desktop-rtx4090").summary()
    assert summary == "Intel Core i9-14900K - 32 threads - 47 GiB - NVIDIA GeForce RTX 4090 - NVMe SSD + SATA SSD - UEFI 64-bit"
    assert probe_fixture("old-bios-sata-radeon").summary().endswith("SATA HDD - Legacy BIOS")


def test_lsblk_fixture_output_parses_with_the_shared_parser():
    for name in FIXTURE_NAMES:
        text = (fixture_path(name) / ".commands" / "lsblk.json").read_text()
        assert parse_lsblk(text)


# -- fixture hygiene -------------------------------------------------------------------------

MAC = re.compile(rb"(?<![0-9a-fA-F:])(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}(?![0-9a-fA-F:])")


def test_fixtures_contain_no_serials_macs_or_uuids():
    for name in FIXTURE_NAMES:
        root = fixture_path(name)
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix == "" and path.name == "edid":
                continue
            data = path.read_bytes()
            for match in MAC.findall(data):
                assert match.startswith(b"aa:bb:cc:dd:ee:") or path.name == "pci.ids", (name, path, match)
        raw = json.loads((root / ".commands" / "lsblk.json").read_text())

        def walk(node):
            assert not node.get("serial") or node["serial"].startswith("REDACTED") or name not in ("asus-desktop-rtx4090", "desktop-igpu-idle"), (name, node["name"])
            for key in ("uuid", "partuuid", "ptuuid"):
                assert node.get(key) in (None, "00000000-0000-0000-0000-000000000000"), (name, node["name"], key)
            for child in node.get("children") or []:
                walk(child)

        for device in raw["blockdevices"]:
            walk(device)
        assert not (root / "etc/hostname").exists()
        assert not any(p.name in {"serial", "product_uuid", "product_serial", "board_serial", "chassis_serial"} for p in root.rglob("*"))


def test_tpm_detection():
    expected = {"asus-desktop-rtx4090": "2.0", "desktop-igpu-idle": "2.0", "qemu-q35-virtio": "", "old-bios-sata-radeon": "", "thinkpad-intel-nvidia-hybrid": "2.0",
                "amd-laptop-sof": "2.0", "vmware-guest": "", "intel-rst-hidden-nvme": "1.2"}
    assert {name: fixture_dict(name)["firmware"]["tpm"] for name in expected} == expected
    # every machine added later has a TPM 2.0 (the Wave 5 fixtures are all modern PCs)
    assert {fixture_dict(name)["firmware"]["tpm"] for name in FIXTURE_NAMES if name not in expected} == {"2.0"}


def test_lsblk_output_with_a_warning_in_front_of_the_json_is_still_used():
    """The daemon's runner merges stderr into stdout, so lsblk warnings can precede the JSON."""
    from types import SimpleNamespace

    text = (FIXTURES / "old-bios-sata-radeon" / ".commands" / "lsblk.json").read_text()

    class Noisy:
        def run(self, argv, **kwargs):
            if argv[0] == "lsblk":
                return SimpleNamespace(returncode=0, stdout="lsblk: warning: something went sideways\n" + text)
            raise OSError

    profile = hw.probe(FIXTURES / "old-bios-sata-radeon", Noisy(), now=fixture_now("old-bios-sata-radeon"))
    assert profile.errors == [] and next(disk for disk in profile.disks if disk.name == "sda").signatures == ["ntfs"]


def test_network_profile_names_are_listed_but_never_read(tmp_path):
    connections = tmp_path / "etc/NetworkManager/system-connections"
    connections.mkdir(parents=True)
    (connections / "Home Wi-Fi.nmconnection").write_text("[wifi-security]\npsk=top-secret\n")
    (connections / "Wired connection 1.nmconnection").write_text("")
    (connections / "notes.txt").write_text("not a profile")
    profile = hw.probe(tmp_path, now=1.0)
    assert sorted(profile.network_profiles) == ["Home Wi-Fi", "Wired connection 1"]
    assert "top-secret" not in json.dumps(profile.to_dict())
    assert hw.probe(tmp_path / "nowhere", now=1.0).network_profiles == []


def test_clock_helpers():
    running = hw.ClockInfo(chrony_running=True, ntp_synced=False)
    assert hw.sync_time_argv(running) == ["chronyc", "makestep"]
    assert hw.sync_time_argv(hw.ClockInfo()) == ["chronyd", "-q", "pool pool.ntp.org iburst"]
    assert hw.parse_set_time("2026-10-24T12:00:00Z") == 1792843200
    assert hw.parse_set_time("2026-10-24 12:00:00") == 1792843200 and hw.parse_set_time("2026-10-24T12:00:00+00:00") == 1792843200
    for bad in ("2019-12-31T23:59:59Z", "2101-01-01T00:00:00Z", "yesterday", "2026-13-01T00:00:00Z", "2026-10-24T25:00:00Z", "2026-10-24T12:00:00+07:00", ""):
        assert hw.parse_set_time(bad) is None
    assert hw.rtc_write_allowed(hw.ClockInfo(rtc_mode="utc")) and not hw.rtc_write_allowed(hw.ClockInfo(rtc_mode="local"))
    assert not hw.rtc_write_allowed(hw.ClockInfo(rtc_mode="unknown"))


def test_chrony_state_in_the_profile():
    assert probe_fixture("asus-desktop-rtx4090").clock.chrony_running is True
    assert probe_fixture("qemu-q35-virtio").clock.chrony_running is None        # no chronyc output: unknown
    assert probe_fixture("amd-laptop-sof").clock.chrony_running is True and probe_fixture("amd-laptop-sof").clock.ntp_synced is False


def test_uefi_boot_entries_are_listed():
    labels = {name: [e.label for e in probe_fixture(name).firmware.boot_entries] for name in FIXTURE_NAMES}
    assert labels["asus-desktop-rtx4090"] == ["Gentoo", "UEFI OS", "UEFI: PXE IPv4 Intel(R) Ethernet Controller I226-V", "UEFI: PXE IPv6 Intel(R) Ethernet Controller I226-V"]
    assert labels["thinkpad-intel-nvidia-hybrid"] == ["Windows Boot Manager", "ubuntu"]
    assert labels["old-bios-sata-radeon"] == []          # BIOS: no NVRAM boot menu
