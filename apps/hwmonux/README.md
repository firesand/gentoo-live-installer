# HWMonux

An HWMonitor-style hardware sensor viewer for Linux: one tree of devices, each with its
static information and live sensors in Value / Min / Max columns.

CPU Temperatures, Clocks, and Utilization each have independent **P-Cores** and **E-Cores**
branches when the kernel identifies those core types. Click the arrow beside a branch to
collapse or expand it; package temperature and overall processor utilization remain outside
the core branches. Collapsed sensors continue updating their values and min/max readings.

- **Motherboard** — vendor, model, BIOS; ACPI / Super I/O / EC sensors
- **CPU** — model, cores, caches, power limits; per-core temperature, clock and utilization; package power
- **Memory** — DIMM temperatures, usage, swap
- **AIO coolers / fan controllers** (via liquidctl) — firmware; liquid temperature, pump and fan speeds
- **NVIDIA GPU** — driver, VBIOS; temperature, fans, power, clocks, utilization, VRAM
- **Drives** — temperatures, filesystem usage, activity, read/write rate
- any other chip the kernel exposes under `/sys/class/hwmon`

## Running

```bash
./hwmonux.py          # window
./hwmonux.py --dump   # one text snapshot on stdout
```

Needs Python 3 with PySide6 (`dev-python/pyside`), `dev-python/nvidia-ml-py` for NVIDIA GPUs, and
`app-misc/liquidctl` for USB AIO coolers.

To add it to the application launcher:

```bash
cp hwmonux.desktop ~/.local/share/applications/
```

## Getting more sensors

HWMonux shows whatever the kernel exposes; some sensors need a driver loaded or a permission:

| Missing | Enable with |
| --- | --- |
| Motherboard fans, voltages, temperatures | the board's Super I/O driver, usually `modprobe nct6775` (at boot: `echo nct6775 > /etc/modules-load.d/nct6775.conf`); some ASUS boards also have `asus_ec_sensors` |
| AIO liquid temperature, pump and fan speeds | `app-misc/liquidctl`; its udev rules grant access after a reboot or `udevadm trigger`. Only USB coolers have this; a cooler on a fan header (e.g. Arctic Liquid Freezer) shows up as a motherboard fan. Hubs that read back all zeros (Lian Li Uni SL-Infinity) are hidden |
| SATA drive temperatures | `modprobe drivetemp` |
| CPU package power | read access to `/sys/class/powercap/intel-rapl:*/energy_uj` (root-only by default) |

Devices are discovered at startup, so restart HWMonux after loading a module.

## Naming sensors

Super I/O chips don't know which header is which, so fans show up as `Fan #2 (nct6798)`. Give any
hwmon channel a name in `~/.config/HWMonux/labels.conf`, one `chip/channel = label` per line, where
`chip` is the name in `/sys/class/hwmon/hwmon*/name`:

```ini
nct6798/fan2 = Arctic LF III Pro 420 (CPU_FAN)
```
