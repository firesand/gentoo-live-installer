"""Hardware discovery and sampling for HWMonux: sysfs hwmon, procfs, NVML and liquidctl."""

import ctypes
import functools
import glob
import os
import re
import socket
import time

try:
    import pynvml
except ImportError:
    pynvml = None

try:
    import liquidctl
    from liquidctl.driver.commander_core import CommanderCore
except ImportError:
    liquidctl = None

SYS_CPU = "/sys/devices/system/cpu"
LABELS_FILE = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                           "HWMonux", "labels.conf")

# Display order of the sensor groups under a device.
GROUPS = ("Voltages", "Temperatures", "Fans", "Powers", "Currents", "Clocks", "Utilization", "Speed")

FORMATS = {
    "volt": "{:.3f} V",
    "fan": "{:.0f} RPM",
    "power": "{:.2f} W",
    "current": "{:.2f} A",
    "clock": "{:.1f} MHz",
    "percent": "{:.1f} %",
}

# hwmon channel prefix -> (group, kind, scale to display unit, noun for unlabelled channels)
HWMON_CHANNELS = {
    "in": ("Voltages", "volt", 1e-3, "Voltage"),
    "temp": ("Temperatures", "temp", 1e-3, "Temperature"),
    "fan": ("Fans", "fan", 1, "Fan"),
    "power": ("Powers", "power", 1e-6, "Power"),
    "curr": ("Currents", "current", 1e-3, "Current"),
    "freq": ("Clocks", "clock", 1e-6, "Clock"),
}
_CHANNEL_RE = re.compile(r"(in|temp|fan|power|curr|freq)(\d+)_(average|input)")

CPU_CHIPS = {"coretemp", "k10temp", "zenpower", "cpu_thermal"}
DIMM_CHIPS = {"spd5118", "jc42"}
GPU_CHIPS = {"amdgpu", "radeon", "nouveau", "i915", "xe"}
NETWORK_CHIP_PREFIXES = ("iwlwifi", "ath", "mt7", "rtw", "r8169")

RAPL_LABELS = {"core": "IA Cores", "uncore": "GT", "dram": "DRAM", "psys": "Platform"}

# liquidctl status unit -> (group, kind); entries in other units (counts, modes) aren't sensors
LIQUIDCTL_UNITS = {
    "°C": ("Temperatures", "temp"),
    "rpm": ("Fans", "fan"),
    "%": ("Fans", "percent"),
    "V": ("Voltages", "volt"),
    "A": ("Currents", "current"),
    "W": ("Powers", "power"),
}
LIQUIDCTL_LABELS = {"Water temperature": "Liquid", "Liquid temperature": "Liquid", "Pump speed": "Pump"}
LIQUID_WARN, LIQUID_CRIT = 50, 60  # AIO coolant is typically rated for 60 °C at most


def format_size(n):
    unit = "B"
    for larger in ("KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            break
        n, unit = n / 1024, larger
    digits = 0 if unit == "B" or n >= 100 else 1 if n >= 10 else 2
    return f"{n:.{digits}f} {unit}"


def format_value(kind, value, fahrenheit=False):
    if value is None:
        return "—"
    if kind == "temp":
        return f"{value * 9 / 5 + 32:.1f} °F" if fahrenheit else f"{value:.1f} °C"
    if kind == "size":
        return format_size(value)
    if kind == "rate":
        return format_size(value) + "/s"
    return FORMATS[kind].format(value)


def read_text(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def sysfs_reader(path, scale=1.0):
    def read():
        with open(path) as f:
            return float(f.read()) * scale
    return read


def parse_cpulist(text):
    """'0-3,8' -> {0, 1, 2, 3, 8}"""
    cpus = set()
    for part in filter(None, (text or "").split(",")):
        low, _, high = part.partition("-")
        cpus.update(range(int(low), int(high or low) + 1))
    return cpus


class Sensor:
    def __init__(self, label, kind, read, warn=None, crit=None):
        self.label, self.kind, self.read = label, kind, read
        self.warn, self.crit = warn, crit
        self.value = self.min = self.max = None

    def update(self):
        try:
            value = self.read()
        except (OSError, ValueError):
            value = None
        self.value = value
        if value is not None:
            self.min = value if self.min is None else min(self.min, value)
            self.max = value if self.max is None else max(self.max, value)

    def reset(self):
        self.min = self.max = self.value


class Device:
    def __init__(self, name, icon):
        self.name, self.icon = name, icon
        self.info = []    # (label, text) static properties
        self.groups = {}  # group title -> [Sensor]

    def add(self, group, label, kind, read, **limits):
        self.groups.setdefault(group, []).append(Sensor(label, kind, read, **limits))

    def sensors(self):
        return (sensor for group in self.groups.values() for sensor in group)


def _temp_limit(path):
    try:
        limit = float(read_text(path, "")) / 1000
    except ValueError:
        return None
    # Some drives and boards report 0 or absurd values for limits they don't implement.
    return limit if 0 < limit < 200 else None


@functools.cache
def user_labels():
    """'chip/channel' -> label from labels.conf, e.g. 'nct6798/fan2 = CPU_FAN'."""
    labels = {}
    for line in read_text(LABELS_FILE, "").splitlines():
        key, sep, label = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            labels[key.strip()] = label.strip()
    return labels


def add_hwmon(dev, path, rename=None):
    """Add every channel of the hwmon chip at `path` to `dev`."""
    chip = read_text(os.path.join(path, "name"), "")
    found = {}
    for name in sorted(os.listdir(path)):  # "_average" sorts first, so "_input" wins
        m = _CHANNEL_RE.fullmatch(name)
        if m:
            found[m[1], int(m[2])] = name
    for (prefix, n), name in sorted(found.items()):
        group, kind, scale, noun = HWMON_CHANNELS[prefix]
        base = os.path.join(path, f"{prefix}{n}")
        label = read_text(base + "_label") or f"{noun} #{n}"
        limits = {}
        if prefix == "temp":
            limits = {"warn": _temp_limit(base + "_max"), "crit": _temp_limit(base + "_crit")}
        label = user_labels().get(f"{chip}/{prefix}{n}") or (rename(label) if rename else label)
        dev.add(group, label, kind, sysfs_reader(os.path.join(path, name), scale), **limits)


class CpuUsage:
    """Busy percentage per CPU between consecutive samples of /proc/stat."""

    def __init__(self):
        self.percent = {}  # "cpu" (all CPUs) or "cpuN" -> %
        self._prev = {}

    def sample(self):
        with open("/proc/stat") as f:
            for line in f:
                if not line.startswith("cpu"):
                    break
                name, *fields = line.split()
                # user nice system idle iowait irq softirq steal
                ticks = [int(x) for x in fields[:8]]
                total, idle = sum(ticks), ticks[3] + ticks[4]
                prev_total, prev_idle = self._prev.get(name, (total, idle))
                self._prev[name] = total, idle
                if total > prev_total:
                    self.percent[name] = 100 * (1 - (idle - prev_idle) / (total - prev_total))


class EnergyMeter:
    """Average power of a RAPL energy counter between consecutive reads."""

    def __init__(self, zone):
        self._path = zone + "/energy_uj"
        self._range = int(read_text(zone + "/max_energy_range_uj", "0"))
        self._prev = None

    def read(self):
        with open(self._path) as f:
            now, energy = time.monotonic(), int(f.read())
        prev, self._prev = self._prev, (now, energy)
        if prev is None:
            return None
        delta = energy - prev[1]
        if delta < 0:  # counter wrapped
            delta += self._range
        return delta / 1e6 / (now - prev[0])


class MemInfo:
    def __init__(self):
        self.bytes = {}

    def sample(self):
        with open("/proc/meminfo") as f:
            for line in f:
                key, value = line.split(":")
                self.bytes[key] = int(value.split()[0]) * 1024

    def used_percent(self, total, free):
        return 100 * (1 - self.bytes[free] / self.bytes[total])


class DiskActivity:
    """Throughput and busy time of a block device between consecutive samples."""

    def __init__(self, block):
        self._path = block + "/stat"
        self._prev = None
        self.read_rate = self.write_rate = self.busy = None

    def sample(self):
        try:
            with open(self._path) as f:
                fields = f.read().split()
        except OSError:
            self._prev = self.read_rate = self.write_rate = self.busy = None
            return
        # sectors read, sectors written (always 512-byte units), ms spent doing I/O
        cur = time.monotonic(), int(fields[2]) * 512, int(fields[6]) * 512, int(fields[9])
        prev, self._prev = self._prev, cur
        if prev:
            dt = cur[0] - prev[0]
            self.read_rate = (cur[1] - prev[1]) / dt
            self.write_rate = (cur[2] - prev[2]) / dt
            self.busy = min(100.0, (cur[3] - prev[3]) / (dt * 10))


class LiquidctlStatus:
    """One status read per poll of a liquidctl device, shared by all its sensors."""

    def __init__(self, device):
        self._device = device
        self.values = {}

    def sample(self):
        try:
            self.values = {key: value for key, value, _ in self._device.get_status()}
        # Drivers raise more than OSError: assertion and protocol errors when the device is
        # unplugged or another program (e.g. the liquidctl CLI) talks to it at the same time.
        except Exception:
            self.values = {}


def _liquidctl_label(key):
    # Drivers name channels "Fan speed 1" (Corsair) or "Fan 1 speed" (Lian Li).
    return LIQUIDCTL_LABELS.get(key) or re.sub(r"^Fan (?:speed )?(\d+)(?: speed)?$", r"Fan #\1", key)


def _liquidctl_probe(lc):
    """Static info, unconnected fan channels and a first status read of a liquidctl device."""
    info, unconnected = [], set()
    if isinstance(lc, CommanderCore):
        # Its initialize() only reads firmware and port state. On other drivers initialize()
        # can reprogram pump modes or lighting, so it isn't called.
        for key, value, _ in lc.initialize():
            port = re.fullmatch(r"Fan port (\d+) connected", key)
            if key == "Firmware version":
                info.append(("Firmware", value))
            elif port and not value:
                unconnected.add(f"Fan speed {port[1]}")
    return info, unconnected, lc.get_status()


def _mounts():
    """Block device name -> its first mount point."""
    found = {}
    with open("/proc/mounts") as f:
        for line in f:
            source, target = line.split()[:2]
            if source.startswith("/dev/"):
                target = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), target)
                found.setdefault(os.path.basename(os.path.realpath(source)), target)
    return found


def _space_used(mount_point):
    def read():
        st = os.statvfs(mount_point)
        return 100 * (1 - st.f_bfree / st.f_blocks) if st.f_blocks else None
    return read


def _nv(fn, *args):
    try:
        return fn(*args)
    except pynvml.NVMLError:
        return None


def _nv_fan_rpm(handle, fan):
    # pynvml's own nvmlDeviceGetFanSpeedRPM() only ever asks for fan 0.
    try:
        info = pynvml.c_nvmlFanSpeedInfo_t()
        info.version, info.fan = pynvml.nvmlFanSpeedInfo_v1, fan
        fn = pynvml._nvmlGetFunctionPointer("nvmlDeviceGetFanSpeedRPM")
        pynvml._nvmlCheckReturn(fn(handle, ctypes.byref(info)))
    except (pynvml.NVMLError, AttributeError):
        return None
    return info.speed


def _nv_reader(fn, *args, scale=1.0, pick=None):
    """Sensor reader around an NVML call; `pick` extracts the number from its result."""
    def read():
        value = _nv(fn, *args)
        if value is None:
            return None
        return (pick(value) if pick else value) * scale
    return read


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


def _nvidia_gpu(index, handle):
    N = pynvml
    dev = Device(_text(_nv(N.nvmlDeviceGetName, handle)) or f"NVIDIA GPU #{index}", "gpu")
    # v2 reports driver-reserved memory separately instead of counting it as used.
    memory = (N.nvmlDeviceGetMemoryInfo, handle, *([N.nvmlMemory_v2] if hasattr(N, "nvmlMemory_v2") else []))

    def info(label, value):
        if value is not None:
            dev.info.append((label, _text(value)))

    def add(group, label, kind, read, **limits):
        if read() is not None:  # skip what this card or driver doesn't support
            dev.add(group, label, kind, read, **limits)

    info("Name", dev.name)
    info("Driver Version", _nv(N.nvmlSystemGetDriverVersion))
    info("VBIOS Version", _nv(N.nvmlDeviceGetVbiosVersion, handle))
    total = _nv(*memory)
    info("Memory", total and format_size(total.total))
    pci = _nv(N.nvmlDeviceGetPciInfo, handle)
    info("PCI Address", pci and pci.busId)
    gen, width = _nv(N.nvmlDeviceGetMaxPcieLinkGeneration, handle), _nv(N.nvmlDeviceGetMaxPcieLinkWidth, handle)
    info("Bus Interface", gen and width and f"PCIe Gen {gen} x{width}")
    limit = _nv(N.nvmlDeviceGetEnforcedPowerLimit, handle)
    info("Power Limit", limit and f"{limit / 1000:.0f} W")

    add("Temperatures", "GPU", "temp", _nv_reader(N.nvmlDeviceGetTemperature, handle, N.NVML_TEMPERATURE_GPU),
        warn=_nv(N.nvmlDeviceGetTemperatureThreshold, handle, N.NVML_TEMPERATURE_THRESHOLD_GPU_MAX),
        crit=_nv(N.nvmlDeviceGetTemperatureThreshold, handle, N.NVML_TEMPERATURE_THRESHOLD_SLOWDOWN))
    for fan in range(_nv(N.nvmlDeviceGetNumFans, handle) or 0):
        add("Fans", f"Fan #{fan}", "fan", lambda fan=fan: _nv_fan_rpm(handle, fan))
        add("Fans", f"Fan #{fan} Duty", "percent", _nv_reader(N.nvmlDeviceGetFanSpeed_v2, handle, fan))
    add("Powers", "GPU", "power", _nv_reader(N.nvmlDeviceGetPowerUsage, handle, scale=1e-3))
    for label, clock in (("Graphics", N.NVML_CLOCK_GRAPHICS), ("Memory", N.NVML_CLOCK_MEM),
                         ("Video", N.NVML_CLOCK_VIDEO)):
        add("Clocks", label, "clock", _nv_reader(N.nvmlDeviceGetClockInfo, handle, clock))
    utilization = N.nvmlDeviceGetUtilizationRates, handle
    add("Utilization", "GPU", "percent", _nv_reader(*utilization, pick=lambda u: u.gpu))
    add("Utilization", "Memory Controller", "percent", _nv_reader(*utilization, pick=lambda u: u.memory))
    add("Utilization", "Video Encoder", "percent",
        _nv_reader(N.nvmlDeviceGetEncoderUtilization, handle, pick=lambda u: u[0]))
    add("Utilization", "Video Decoder", "percent",
        _nv_reader(N.nvmlDeviceGetDecoderUtilization, handle, pick=lambda u: u[0]))
    add("Utilization", "Memory", "percent", _nv_reader(*memory, pick=lambda m: 100 * m.used / m.total))
    add("Utilization", "Memory Used", "size", _nv_reader(*memory, pick=lambda m: m.used))
    return dev


class Monitor:
    """Discovers the machine's hardware once, then refreshes every sensor on poll()."""

    def __init__(self):
        self.hostname = socket.gethostname()
        self._samplers = []  # run once per poll, before the sensors that share their data
        self._chips = [(read_text(path + "/name", ""), path)
                       for path in sorted(glob.glob("/sys/class/hwmon/hwmon*"),
                                          key=lambda p: int(p.rsplit("hwmon", 1)[1]))]
        drives = self._drives()  # first: claims the hwmon chips that belong to drives
        self.devices = [self._motherboard(), self._cpu(), *self._liquidctl_devices(), self._memory(),
                        *self._nvidia_gpus(), *drives, *self._other_chips()]
        for dev in self.devices:
            dev.groups = {g: dev.groups[g] for g in GROUPS if g in dev.groups}
        self.poll()  # primes the rate-based sensors

    def poll(self):
        for sample in self._samplers:
            sample()
        for dev in self.devices:
            for sensor in dev.sensors():
                sensor.update()

    def reset(self):
        for dev in self.devices:
            for sensor in dev.sensors():
                sensor.reset()

    def _take_chips(self, match):
        taken = [chip for chip in self._chips if match(*chip)]
        self._chips = [chip for chip in self._chips if chip not in taken]
        return taken

    def _motherboard(self):
        def dmi(field):
            return read_text("/sys/class/dmi/id/" + field, "")

        dev = Device(f"{dmi('board_vendor')} {dmi('board_name')}".strip() or "Motherboard", "board")
        for label, field in (("Manufacturer", "board_vendor"), ("Model", "board_name"),
                             ("Revision", "board_version"), ("BIOS Vendor", "bios_vendor"),
                             ("BIOS Version", "bios_version"), ("BIOS Date", "bios_date")):
            if dmi(field):
                dev.info.append((label, dmi(field)))
        # Super I/O, embedded controller and ACPI chips all hang off the platform bus.
        chips = self._take_chips(lambda name, path: name not in CPU_CHIPS and (
            name == "acpitz" or "/platform/" in os.path.realpath(path + "/device")))
        for name, path in chips:
            add_hwmon(dev, path, lambda label, name=name: f"{label} ({name})")
        return dev

    def _cpu(self):
        info = {}
        with open("/proc/cpuinfo") as f:
            for line in f:
                key, sep, value = line.partition(":")
                if not sep:  # blank line ends the first processor block
                    break
                info[key.strip()] = value.strip()
        model = info.get("model name", os.uname().machine)
        dev = Device(re.sub(r"\((R|TM)\)", "", model), "cpu")

        cores = {}  # (package, core id) -> [cpu numbers]
        for path in glob.glob(SYS_CPU + "/cpu[0-9]*"):
            core_id = read_text(path + "/topology/core_id")
            if core_id is not None:  # offline CPUs have no topology
                package = int(read_text(path + "/topology/physical_package_id", "0"))
                cores.setdefault((package, int(core_id)), []).append(int(os.path.basename(path)[3:]))
        p_cpus = parse_cpulist(read_text("/sys/devices/cpu_core/cpus"))
        e_cpus = parse_cpulist(read_text("/sys/devices/cpu_atom/cpus"))
        names = {}
        for i, key in enumerate(sorted(cores)):
            cpu = cores[key][0]
            names[key] = f"{'P-Core' if cpu in p_cpus else 'E-Core' if cpu in e_cpus else 'Core'} #{i}"
        packages = {package for package, _ in cores}
        threads = sum(len(cpus) for cpus in cores.values())

        dev.info.append(("Name", model))
        if "vendor_id" in info:
            dev.info.append(("Vendor", info["vendor_id"]))
        dev.info.append(("Architecture", os.uname().machine))
        if "cpu family" in info:
            dev.info.append(("Family / Model / Stepping",
                             f"{info['cpu family']} / {info.get('model', '?')} / {info.get('stepping', '?')}"))
        p_count = sum(cpus[0] in p_cpus for cpus in cores.values())
        e_count = sum(cpus[0] in e_cpus for cpus in cores.values())
        dev.info.append(("Cores", f"{len(cores)} ({p_count} P + {e_count} E)" if e_count else str(len(cores))))
        dev.info.append(("Threads", str(threads)))
        freqs = [int(v) for path in glob.glob(SYS_CPU + "/cpu[0-9]*/cpufreq")
                 for v in (read_text(path + "/cpuinfo_min_freq"), read_text(path + "/cpuinfo_max_freq")) if v]
        if freqs:
            dev.info.append(("Frequency Range", f"{min(freqs) / 1000:.0f} – {max(freqs) / 1000:.0f} MHz"))
        dev.info += self._cpu_caches()
        if "microcode" in info:
            dev.info.append(("Microcode", info["microcode"]))
        driver = read_text(SYS_CPU + "/cpu0/cpufreq/scaling_driver")
        if driver:
            dev.info.append(("Scaling Driver", driver))
        # Package power limits as the BIOS (or anything writing powercap) set them; readable without root.
        for zone in sorted(glob.glob("/sys/class/powercap/intel-rapl:*")):
            if not read_text(zone + "/name", "").startswith("package"):
                continue
            for path in sorted(glob.glob(zone + "/constraint_*_name")):
                limit = read_text(path.removesuffix("name") + "power_limit_uw", "")
                if limit.isdigit() and int(limit):
                    dev.info.append((f"Power Limit ({read_text(path).replace('_', ' ')})",
                                     f"{int(limit) / 1e6:.0f} W"))

        for name, path in self._take_chips(lambda name, path: name in CPU_CHIPS):
            # coretemp registers one platform device per package: coretemp.<package>
            suffix = os.path.realpath(path + "/device").rpartition(".")[2]
            package = int(suffix) if suffix.isdigit() else 0

            def rename(label, package=package):
                m = re.fullmatch(r"(Core|Package id) (\d+)", label)
                if not m:
                    return label
                if m[1] == "Core":
                    return names.get((package, int(m[2])), label)
                return "Package" if len(packages) == 1 else f"Package #{m[2]}"
            add_hwmon(dev, path, rename)

        for zone in sorted(glob.glob("/sys/class/powercap/intel-rapl:*")):
            if os.access(zone + "/energy_uj", os.R_OK):  # root-only on most kernels
                name = read_text(zone + "/name", "")
                label = "Package" if name.startswith("package") else RAPL_LABELS.get(name, name)
                dev.add("Powers", label, "power", EnergyMeter(zone).read)

        usage = CpuUsage()
        self._samplers.append(usage.sample)

        def core_usage(cpus):
            def read():
                loads = [usage.percent.get(f"cpu{n}") for n in cpus]
                return None if None in loads else sum(loads) / len(loads)
            return read

        def core_clock(cpus):
            readers = [sysfs_reader(f"{SYS_CPU}/cpu{n}/cpufreq/scaling_cur_freq", 1e-3) for n in cpus]
            return lambda: max(read() for read in readers)

        dev.add("Utilization", "Processor", "percent", lambda: usage.percent.get("cpu"))
        for key in sorted(cores):
            dev.add("Utilization", names[key], "percent", core_usage(cores[key]))
            if os.path.exists(f"{SYS_CPU}/cpu{cores[key][0]}/cpufreq/scaling_cur_freq"):
                dev.add("Clocks", names[key], "clock", core_clock(cores[key]))
        return dev

    @staticmethod
    def _cpu_caches():
        caches = {}  # (level, type) -> {shared cpu list: size in bytes}
        for path in glob.glob(SYS_CPU + "/cpu[0-9]*/cache/index[0-9]*"):
            level, kind, size = (read_text(f"{path}/{attr}") for attr in ("level", "type", "size"))
            if level and kind and size and size[:-1].isdigit():
                scale = {"K": 1024, "M": 1024 ** 2}.get(size[-1])
                if scale:
                    shared = read_text(path + "/shared_cpu_list")
                    caches.setdefault((int(level), kind), {})[shared] = int(size[:-1]) * scale
        rows = []
        for (level, kind), instances in sorted(caches.items()):
            label = f"L{level} {kind} Cache" if kind != "Unified" else f"L{level} Cache"
            plural = "s" if len(instances) > 1 else ""
            rows.append((label, f"{format_size(sum(instances.values()))} ({len(instances)} instance{plural})"))
        return rows

    def _memory(self):
        mem = MemInfo()
        mem.sample()
        self._samplers.append(mem.sample)
        dev = Device("Memory", "memory")
        dev.info.append(("Total", format_size(mem.bytes["MemTotal"])))
        for name, path in self._take_chips(lambda name, path: name in DIMM_CHIPS):
            # SPD hubs sit at I2C addresses 0x50-0x57, one per DIMM slot.
            address = os.path.basename(os.path.realpath(path + "/device")).rpartition("-")[2]
            slot = int(address, 16) & 7
            add_hwmon(dev, path, lambda label, slot=slot: f"DIMM #{slot}")
        dev.add("Utilization", "Memory", "percent", lambda: mem.used_percent("MemTotal", "MemAvailable"))
        dev.add("Utilization", "Used", "size", lambda: mem.bytes["MemTotal"] - mem.bytes["MemAvailable"])
        dev.add("Utilization", "Available", "size", lambda: mem.bytes["MemAvailable"])
        if mem.bytes.get("SwapTotal"):
            dev.info.append(("Swap", format_size(mem.bytes["SwapTotal"])))
            dev.add("Utilization", "Swap", "percent", lambda: mem.used_percent("SwapTotal", "SwapFree"))
        return dev

    def _drives(self):
        mounts = _mounts()
        drives = []
        for block in sorted(glob.glob("/sys/block/*")):
            name, device = os.path.basename(block), block + "/device"
            size = int(read_text(block + "/size", "0")) * 512
            if not os.path.exists(device) or not size:  # loop/dm/zram, empty card readers
                continue
            dev = Device(read_text(device + "/model") or name, "drive")
            if name.startswith("nvme"):
                kind = "NVMe SSD"
            else:
                kind = "HDD" if read_text(block + "/queue/rotational") == "1" else "SSD"
            dev.info += [("Device", "/dev/" + name), ("Type", kind), ("Capacity", format_size(size))]
            firmware = read_text(device + "/firmware_rev") or read_text(device + "/rev")
            if firmware:
                dev.info.append(("Firmware", firmware))

            # NVMe exposes hwmonN on the controller, drivetemp under the SCSI device's hwmon/.
            own = {os.path.realpath(p)
                   for p in glob.glob(device + "/hwmon[0-9]*") + glob.glob(device + "/hwmon/hwmon[0-9]*")}
            for _, path in self._take_chips(lambda _, path: os.path.realpath(path) in own):
                add_hwmon(dev, path)

            for part in sorted(os.listdir(block)):
                if part in mounts and part.startswith(name):
                    dev.add("Utilization", f"Space ({mounts[part]})", "percent", _space_used(mounts[part]))
            if name in mounts:  # filesystem directly on the whole disk
                dev.add("Utilization", f"Space ({mounts[name]})", "percent", _space_used(mounts[name]))
            activity = DiskActivity(block)
            self._samplers.append(activity.sample)
            dev.add("Utilization", "Activity", "percent", lambda a=activity: a.busy)
            dev.add("Speed", "Read Rate", "rate", lambda a=activity: a.read_rate)
            dev.add("Speed", "Write Rate", "rate", lambda a=activity: a.write_rate)
            drives.append(dev)
        return drives

    @staticmethod
    def _nvidia_gpus():
        if pynvml is None:
            return []
        try:
            pynvml.nvmlInit()
        except pynvml.NVMLError:  # no NVIDIA driver loaded
            return []
        gpus = []
        for index in range(_nv(pynvml.nvmlDeviceGetCount) or 0):
            handle = _nv(pynvml.nvmlDeviceGetHandleByIndex, index)
            if handle is not None:
                gpus.append(_nvidia_gpu(index, handle))
        return gpus

    def _liquidctl_devices(self):
        """AIO coolers and fan controllers on USB, read through liquidctl."""
        if liquidctl is None:
            return []
        try:
            found = list(liquidctl.find_liquidctl_devices())
        except Exception:
            return []
        devices = []
        for lc in found:
            try:
                lc.connect()  # fails without the udev rules liquidctl installs
            except Exception:
                continue
            for _ in range(3):  # reads fail while another program is talking to the device
                try:
                    info, unconnected, status = _liquidctl_probe(lc)
                    break
                except Exception:
                    time.sleep(0.25)
            else:
                lc.disconnect()
                continue
            readings = {key: value for key, value, _ in status}
            sensors = [(key, *LIQUIDCTL_UNITS[unit]) for key, _, unit in status
                       if unit in LIQUIDCTL_UNITS and key not in unconnected]
            # RGB controllers report no sensors, and some fan hubs only zeros: the Lian Li Uni
            # driver turns a failed speed read into 0 rpm. Neither has anything to show.
            if not any(readings[key] for key, _, _ in sensors):
                lc.disconnect()
                continue

            name = re.sub(r"\s*\((broken|experimental)\)", "", lc.description)
            dev = Device(name, "cooler")
            dev.info += [("Name", name), *info, ("Driver", f"liquidctl {liquidctl.__version__}")]
            reading = LiquidctlStatus(lc)
            self._samplers.append(reading.sample)
            for key, group, kind in sensors:
                label = _liquidctl_label(key)
                limits = {"warn": LIQUID_WARN, "crit": LIQUID_CRIT} if label == "Liquid" else {}
                dev.add(group, label, kind, lambda key=key, r=reading: r.values.get(key), **limits)
            devices.append(dev)
        return devices

    def _other_chips(self):
        devices = []
        for name, path in self._take_chips(lambda name, path: True):
            icon = "gpu" if name in GPU_CHIPS else "network" if name.startswith(NETWORK_CHIP_PREFIXES) else "chip"
            dev = Device(name, icon)
            add_hwmon(dev, path)
            if dev.groups:
                devices.append(dev)
        return devices


def report(monitor, fahrenheit=False):
    """Plain-text snapshot of every device, as saved by the GUI and printed by --dump."""
    lines = [monitor.hostname, f"{'':<34}{'Value':>14}{'Min':>14}{'Max':>14}"]
    for dev in monitor.devices:
        lines += ["", dev.name]
        lines += [f"  {label:<32}{text}" for label, text in dev.info]
        for group, sensors in dev.groups.items():
            lines.append(f"  {group}")
            for s in sensors:
                value, low, high = (format_value(s.kind, v, fahrenheit) for v in (s.value, s.min, s.max))
                lines.append(f"    {s.label:<30}{value:>14}{low:>14}{high:>14}")
    return "\n".join(lines) + "\n"
