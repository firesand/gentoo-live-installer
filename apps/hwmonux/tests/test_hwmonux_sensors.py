"""Sensor backend: failures stay local, and GPUs show up on any machine."""

import logging
import os

import pytest

import sensors
from sensors import Device, MemInfo, Monitor, Sensor

GIB = 1024 ** 3


def bare_monitor():
    """A Monitor that has discovered nothing (no real /sys access)."""
    m = Monitor.__new__(Monitor)
    m.hostname = "testhost"
    m._samplers = []
    m._reported = set()
    m._nvml_pci = set()
    m._chips = []
    m.devices = []
    return m


# --- P8: nothing may freeze the monitor --------------------------------------------------


def test_used_percent_without_a_total_is_none():
    mem = MemInfo()
    mem.bytes = {"MemTotal": 8 * GIB, "MemAvailable": 2 * GIB, "SwapTotal": 0, "SwapFree": 0}
    assert mem.used_percent("MemTotal", "MemAvailable") == pytest.approx(75.0)
    assert mem.used_percent("SwapTotal", "SwapFree") is None
    assert mem.used_percent("NoSuchTotal", "SwapFree") is None
    assert mem.used_percent("MemTotal", "NoSuchFree") is None


def test_swap_turned_off_at_runtime_does_not_stop_the_other_sensors(monkeypatch):
    meminfo = {"MemTotal": 8 * GIB, "MemAvailable": 4 * GIB, "SwapTotal": 2 * GIB, "SwapFree": GIB}
    monkeypatch.setattr(MemInfo, "sample", lambda self: setattr(self, "bytes", dict(meminfo)))
    m = bare_monitor()
    memory = m._memory()
    ticks = []
    after = Device("after memory", "chip")
    after.add("Utilization", "ticks", "percent", lambda: len(ticks.append(1) or ticks))
    m.devices = [memory, after]

    swap = next(s for s in memory.sensors() if s.label == "Swap")
    m.poll()
    assert swap.value == pytest.approx(50.0)

    meminfo.update(SwapTotal=0, SwapFree=0)  # swapoff, e.g. the installer cleaning up the target
    m.poll()
    m.poll()
    assert swap.value is None
    assert len(ticks) == 3, "devices after Memory must keep refreshing"
    memory_sensor = next(s for s in memory.sensors() if s.label == "Memory")
    assert memory_sensor.value == pytest.approx(50.0)


def test_a_failing_reader_is_isolated_and_logged_once(caplog):
    def broken():
        raise RuntimeError("driver bug")

    bad, good = Sensor("bad", "percent", broken), Sensor("good", "percent", lambda: 7)
    with caplog.at_level(logging.WARNING, logger="hwmonux"):
        for _ in range(3):
            bad.update()
            good.update()
    assert bad.value is None and good.value == 7
    assert [r.getMessage() for r in caplog.records if "bad" in r.getMessage()].__len__() == 1


@pytest.mark.parametrize("error", [OSError("gone"), ValueError("garbage")])
def test_expected_read_errors_are_silent(error, caplog):
    def broken():
        raise error

    s = Sensor("x", "percent", broken)
    with caplog.at_level(logging.WARNING, logger="hwmonux"):
        s.update()
    assert s.value is None and not caplog.records


def test_poll_survives_a_failing_sampler_and_a_failing_device():
    m = bare_monitor()
    calls = []

    def bad_sampler():
        raise RuntimeError("boom")

    m._samplers = [bad_sampler, lambda: calls.append("second sampler")]

    class BrokenDevice(Device):
        def sensors(self):
            raise RuntimeError("device boom")

    ok = Device("ok", "chip")
    ok.add("Utilization", "one", "percent", lambda: 1.0)
    m.devices = [BrokenDevice("broken", "chip"), ok]
    m.poll()
    m.poll()
    assert calls == ["second sampler"] * 2
    assert next(ok.sensors()).value == 1.0


def test_start_up_survives_a_failing_discovery_stage(monkeypatch):
    def device(name):
        d = Device(name, "chip")
        d.add("Utilization", "x", "percent", lambda: 1.0)
        return d

    monkeypatch.setattr(Monitor, "_hwmon_chips", staticmethod(lambda: []))
    monkeypatch.setattr(Monitor, "_drives", lambda self: [device("drive")])
    monkeypatch.setattr(Monitor, "_motherboard", lambda self: (_ for _ in ()).throw(OSError("odd sysfs")))
    monkeypatch.setattr(Monitor, "_cpu", lambda self: device("cpu"))
    monkeypatch.setattr(Monitor, "_liquidctl_devices", lambda self: (_ for _ in ()).throw(RuntimeError("usb")))
    monkeypatch.setattr(Monitor, "_memory", lambda self: device("memory"))
    monkeypatch.setattr(Monitor, "_nvidia_gpus", lambda self: [])
    monkeypatch.setattr(Monitor, "_display_adapters", lambda self: (_ for _ in ()).throw(ValueError("bad id")))
    monkeypatch.setattr(Monitor, "_other_chips", lambda self: [device("chip")])
    m = Monitor()
    assert [d.name for d in m.devices] == ["cpu", "memory", "drive", "chip"]


def test_hwmon_chip_list_tolerates_odd_names(monkeypatch, tmp_path):
    for name in ("hwmon10", "hwmon2", "hwmonX"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "name").write_text(name.upper() + "\n")
    real_glob = sensors.glob.glob
    monkeypatch.setattr(sensors.glob, "glob", lambda pattern: real_glob(str(tmp_path / "hwmon*")) if "hwmon" in pattern else real_glob(pattern))
    assert [name for name, _ in Monitor._hwmon_chips()] == ["HWMON2", "HWMON10", "HWMONX"]


# --- COMP-NEW-8: GPUs that NVML does not know -------------------------------------------

PCI_IDS = """\
# fake pci.ids
8086  Intel Corporation
\t46a6  Alder Lake-P GT2 [Iris Xe Graphics]
\t9a49  TigerLake-LP GT2 [Iris Xe Graphics]
10de  NVIDIA Corporation
\t2684  AD102 [GeForce RTX 4090]
1002  Advanced Micro Devices, Inc. [AMD/ATI]
\t73bf  Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]
C 03  Display controller
\t00  VGA compatible controller
"""


@pytest.fixture
def pci_ids(tmp_path, monkeypatch):
    path = tmp_path / "pci.ids"
    path.write_text(PCI_IDS)
    monkeypatch.setattr(sensors, "PCI_IDS", (str(path),))
    return path


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def fake_pci(root):
    """Intel iGPU (i915), AMD dGPU (amdgpu), an NVIDIA card NVML covers, and an audio device."""
    intel = root / "0000:00:02.0"
    write(intel / "class", "0x030000\n")
    write(intel / "vendor", "0x8086\n")
    write(intel / "device", "0x46a6\n")
    (intel / "drivers" / "i915").mkdir(parents=True)
    (intel / "driver").symlink_to(intel / "drivers" / "i915")
    write(intel / "drm" / "card0" / "gt_act_freq_mhz", "450\n")
    write(intel / "drm" / "card0" / "gt_cur_freq_mhz", "500\n")
    write(intel / "power" / "runtime_status", "active\n")

    amd = root / "0000:03:00.0"
    write(amd / "class", "0x030000\n")
    write(amd / "vendor", "0x1002\n")
    write(amd / "device", "0x73bf\n")
    write(amd / "max_link_speed", "16.0 GT/s PCIe\n")
    write(amd / "max_link_width", "16\n")
    (amd / "drivers" / "amdgpu").mkdir(parents=True)
    (amd / "driver").symlink_to(amd / "drivers" / "amdgpu")
    write(amd / "gpu_busy_percent", "37\n")
    write(amd / "mem_busy_percent", "5\n")
    write(amd / "mem_info_vram_total", str(16 * GIB) + "\n")
    write(amd / "mem_info_vram_used", str(4 * GIB) + "\n")
    write(amd / "pp_dpm_sclk", "0: 500Mhz\n1: 1500Mhz *\n2: 2500Mhz\n")
    write(amd / "pp_dpm_mclk", "0: 96Mhz\n1: 1000Mhz *\n")
    write(amd / "power" / "runtime_status", "active\n")
    hwmon = amd / "hwmon" / "hwmon7"
    write(hwmon / "name", "amdgpu\n")
    write(hwmon / "temp1_input", "56000\n")
    write(hwmon / "temp1_label", "edge\n")
    write(hwmon / "temp2_input", "61000\n")
    write(hwmon / "temp2_label", "junction\n")
    write(hwmon / "power1_average", "80000000\n")
    write(hwmon / "fan1_input", "900\n")

    nvidia = root / "0000:01:00.0"
    write(nvidia / "class", "0x030000\n")
    write(nvidia / "vendor", "0x10de\n")
    write(nvidia / "device", "0x2684\n")

    write(root / "0000:00:1f.3" / "class", "0x040300\n")
    return intel, amd, nvidia, hwmon


def adapter_monitor(hwmon):
    m = bare_monitor()
    m._nvml_pci = {"0000:01:00.0"}  # NVML reported the NVIDIA card
    m._chips = [("amdgpu", str(hwmon)), ("coretemp", "/sys/class/hwmon/hwmon9")]
    return m


def sensor(dev, group, label):
    return next(s for s in dev.groups[group] if s.label == label)


def test_intel_and_amd_gpus_are_listed_without_nvml(tmp_path, pci_ids):
    root = tmp_path / "pci"
    intel, amd, nvidia, hwmon = fake_pci(root)
    m = adapter_monitor(hwmon)
    devices = m._display_adapters(pci_dir=str(root))

    assert [d.name for d in devices] == ["Intel Iris Xe Graphics", "AMD Radeon RX 6800/6800 XT / 6900 XT"]
    assert all(d.icon == "gpu" for d in devices)
    assert [d.pci for d in devices] == ["0000:00:02.0", "0000:03:00.0"]
    # The unrelated hwmon chip stays available to the generic pass, the GPU's own chip is taken.
    assert m._chips == [("coretemp", "/sys/class/hwmon/hwmon9")]

    igpu, dgpu = devices
    assert ("Driver", "i915") in igpu.info and ("PCI Address", "0000:00:02.0") in igpu.info
    assert ("Driver", "amdgpu") in dgpu.info
    assert ("Bus Interface", "PCIe Gen 4 x16") in dgpu.info
    assert ("Memory", "16.0 GiB") in dgpu.info

    m.devices = devices
    m.poll()
    assert sensor(igpu, "Clocks", "Graphics").value == 450  # actual, not requested, frequency
    assert sensor(dgpu, "Temperatures", "Edge").value == pytest.approx(56.0)
    assert sensor(dgpu, "Temperatures", "Junction").value == pytest.approx(61.0)
    assert sensor(dgpu, "Powers", "GPU").value == pytest.approx(80.0)
    assert sensor(dgpu, "Fans", "Fan").value == 900
    assert sensor(dgpu, "Utilization", "GPU").value == 37
    assert sensor(dgpu, "Utilization", "Memory Controller").value == 5
    assert sensor(dgpu, "Utilization", "Memory").value == pytest.approx(25.0)
    assert sensor(dgpu, "Utilization", "Memory Used").value == 4 * GIB
    assert sensor(dgpu, "Clocks", "Graphics").value == 1500  # no hwmon freq1_input: parsed from pp_dpm_sclk
    assert sensor(dgpu, "Clocks", "Memory").value == 1000


def test_nvml_gpu_is_not_listed_twice(tmp_path, pci_ids):
    root = tmp_path / "pci"
    _, _, nvidia, hwmon = fake_pci(root)
    names = [d.name for d in adapter_monitor(hwmon)._display_adapters(pci_dir=str(root))]
    assert not any("NVIDIA" in n for n in names)
    # Without NVML (no nvidia-ml-py, or the open driver) the card is still listed, with a hint.
    m = adapter_monitor(hwmon)
    m._nvml_pci = set()
    (nvidia / "drivers" / "nvidia").mkdir(parents=True)
    (nvidia / "driver").symlink_to(nvidia / "drivers" / "nvidia")
    listed = {d.name: d for d in m._display_adapters(pci_dir=str(root))}
    assert "NVIDIA GeForce RTX 4090" in listed
    if sensors.pynvml is None:
        assert any(label == "Sensors" for label, _ in listed["NVIDIA GeForce RTX 4090"].info)


def test_a_sleeping_gpu_is_not_woken_by_polling(tmp_path, pci_ids):
    root = tmp_path / "pci"
    intel, amd, _, hwmon = fake_pci(root)
    m = adapter_monitor(hwmon)
    devices = m._display_adapters(pci_dir=str(root))
    m.devices = devices
    dgpu = devices[1]
    m.poll()
    assert sensor(dgpu, "Utilization", "GPU").value == 37
    # Every file stays perfectly readable: only the guard can be why nothing is read now.
    (amd / "power" / "runtime_status").write_text("suspended\n")
    m.poll()
    assert all(s.value is None for s in dgpu.sensors())
    assert sensor(dgpu, "Temperatures", "Edge").min == pytest.approx(56.0), "min/max keep the last real readings"
    (amd / "power" / "runtime_status").write_text("active\n")
    m.poll()
    assert sensor(dgpu, "Utilization", "GPU").value == 37


def test_an_adapter_that_cannot_be_read_costs_only_itself(tmp_path, pci_ids, monkeypatch):
    root = tmp_path / "pci"
    _, _, _, hwmon = fake_pci(root)
    m = adapter_monitor(hwmon)
    real = Monitor._display_adapter

    def flaky(self, path, address):
        if address == "0000:00:02.0":
            raise OSError("vanished")
        return real(self, path, address)

    monkeypatch.setattr(Monitor, "_display_adapter", flaky)
    assert [d.pci for d in m._display_adapters(pci_dir=str(root))] == ["0000:03:00.0"]


def test_pci_names_and_helpers(pci_ids):
    assert sensors.pci_names("0x8086", "0x46a6") == ("Intel Corporation", "Alder Lake-P GT2 [Iris Xe Graphics]")
    assert sensors.pci_names("0x8086", "0x0001") == ("Intel Corporation", None)
    assert sensors.pci_names("0x9999", "0x0001") == (None, None)
    assert sensors.gpu_name("0x10de", "0x2684") == "NVIDIA GeForce RTX 4090"
    assert sensors.gpu_name("0x9999", "0x0001") == "0x9999 GPU (0001)"
    assert sensors.gpu_name("0x1002", "0x73bf").startswith("AMD Radeon RX 6800")
    assert sensors.normalize_pci("00000000:01:00.0") == "0000:01:00.0"
    assert sensors.normalize_pci("0000:01:00.0") == "0000:01:00.0"
    assert sensors.normalize_pci(None) == ""


def test_gpu_name_without_pci_ids(monkeypatch):
    monkeypatch.setattr(sensors, "PCI_IDS", ("/nonexistent/pci.ids",))
    assert sensors.gpu_name("0x8086", "0x46a6") == "Intel GPU (46a6)"


def test_marked_mhz(tmp_path):
    f = tmp_path / "pp_dpm_sclk"
    f.write_text("0: 500Mhz\n1: 1500Mhz *\n")
    assert sensors._marked_mhz(str(f)) == 1500
    f.write_text("0: 500Mhz\n")
    assert sensors._marked_mhz(str(f)) is None
    assert sensors._marked_mhz(str(tmp_path / "missing")) is None


def test_nvml_buses_match_sysfs_after_normalising():
    # NVML reports 8-digit PCI domains, sysfs four.
    assert sensors.normalize_pci("00000000:C1:00.0") == "0000:c1:00.0"


# --- COMP-NEW-2: say why the motherboard has no fans -------------------------------------


def motherboard(monkeypatch, tmp_path, *, chips, vm=False, vendor="Acme"):
    real = sensors.read_text
    monkeypatch.setattr(sensors, "read_text", lambda path, default=None: {
        "/sys/class/dmi/id/board_vendor": vendor, "/sys/class/dmi/id/board_name": "Board",
    }.get(path, real(path, default) if not path.startswith("/sys/class/dmi") else default))
    monkeypatch.setattr(sensors, "in_virtual_machine", lambda: vm)
    m = bare_monitor()
    m._chips = chips
    return m._motherboard()


def test_missing_super_io_driver_is_explained(monkeypatch, tmp_path):
    dev = motherboard(monkeypatch, tmp_path, chips=[])
    hint = dict(dev.info).get("Fan/voltage sensors", "")
    assert "sensors-detect" in hint and "README" in hint


def test_no_hint_when_a_platform_chip_exists_or_in_a_vm_or_without_dmi(monkeypatch, tmp_path):
    chip = tmp_path / "hwmon0"
    write(chip / "name", "acpitz\n")
    write(chip / "temp1_input", "42000\n")
    assert "Fan/voltage sensors" not in dict(motherboard(monkeypatch, tmp_path, chips=[("acpitz", str(chip))]).info)
    assert "Fan/voltage sensors" not in dict(motherboard(monkeypatch, tmp_path, chips=[], vm=True).info)
    assert "Fan/voltage sensors" not in dict(motherboard(monkeypatch, tmp_path, chips=[], vendor="").info)
