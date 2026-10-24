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
- **Intel and AMD GPUs** (integrated or discrete, any display adapter the kernel knows) — driver, PCI
  address, link; clocks, plus temperatures, fans, power, utilization and VRAM where the driver
  (`amdgpu`, `i915`, `xe`, `nouveau`) exposes them. A GPU that is runtime-suspended (hybrid
  laptops) is not woken up by polling: its sensors read "—" until something else wakes it
- **Drives** — temperatures, filesystem usage, activity, read/write rate
- any other chip the kernel exposes under `/sys/class/hwmon`

## Running

```bash
./hwmonux.py          # window
./hwmonux.py --dump   # one text snapshot on stdout
```

Needs Python 3 with PySide6 (`dev-python/pyside`), `dev-python/nvidia-ml-py` for NVIDIA GPU
sensors, and `app-misc/liquidctl` for USB AIO coolers.

The window opens at 800x900 but never larger than 90 % of the usable screen, so it fits 1366x768 and
1280x720 laptop panels. Every sensor is read on its own: one that fails shows "—" and the rest keep
updating (for example swap after `swapoff`).

To add it to the application launcher:

```bash
cp hwmonux.desktop ~/.local/share/applications/
```

## Getting more sensors

HWMonux shows whatever the kernel exposes; some sensors need a driver loaded or a permission:

| Missing | Enable with |
| --- | --- |
| Motherboard fans, voltages, temperatures | the board's Super I/O driver, usually `modprobe nct6775` (at boot: `echo nct6775 > /etc/modules-load.d/nct6775.conf`); some ASUS boards also have `asus_ec_sensors`, many Gigabyte/ITE boards `it87`. Run `sensors-detect` (`sys-apps/lm-sensors`) to find out which, and see the warning below |
| AIO liquid temperature, pump and fan speeds | `app-misc/liquidctl`; its udev rules grant access after a reboot or `udevadm trigger`. Only USB coolers have this; a cooler on a fan header (e.g. Arctic Liquid Freezer) shows up as a motherboard fan. Hubs that read back all zeros (Lian Li Uni SL-Infinity) are hidden |
| SATA drive temperatures | `modprobe drivetemp` (at boot: `echo drivetemp > /etc/modules-load.d/drivetemp.conf`) |
| NVIDIA GPU sensors | `dev-python/nvidia-ml-py` with the proprietary driver. On Gentoo: `echo 'app-misc/hwmonux nvidia' >> /etc/portage/package.use/hwmonux`, then re-emerge `app-misc/hwmonux`. Without it the GPU is still listed with its driver and PCI address |
| CPU package power | read access to `/sys/class/powercap/intel-rapl:*/energy_uj` (root-only by default) |

Devices are discovered at startup, so restart HWMonux after loading a module. When the board reports
DMI data but no sensor chip is bound, the Motherboard node says so under *Information*
("Fan/voltage sensors"). Virtual machines have no such chips and get no hint.

**`sensors-detect` can hang a machine.** It probes I/O ports and SMBus addresses, and on some boards
and laptops that confuses the embedded controller. Read its prompts, accept the safe defaults only,
and do not answer "yes" to the aggressive scans on a laptop or a machine you cannot afford to
power-cycle. Loading `nct6775` or `it87` blindly on hardware that does not have the chip is
discouraged for the same reason.

## Naming sensors

Super I/O chips don't know which header is which, so fans show up as `Fan #2 (nct6798)`. Give any
hwmon channel a name in `~/.config/HWMonux/labels.conf`, one `chip/channel = label` per line, where
`chip` is the name in `/sys/class/hwmon/hwmon*/name`:

```ini
nct6798/fan2 = Arctic LF III Pro 420 (CPU_FAN)
```

## Tests

```bash
cd apps/hwmonux && QT_QPA_PLATFORM=offscreen python -m pytest tests
```

The tests use fake `/sys` trees and a stub monitor, so they run on any machine and do not touch the
real hardware or your saved window settings.
