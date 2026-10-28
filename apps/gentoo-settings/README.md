# Gentoo Settings

[CachyOS-Settings](https://github.com/CachyOS/CachyOS-Settings) for Gentoo: the
same performance and desktop tuning, ported to Gentoo with OpenRC **or** systemd,
behind a Steam-styled GUI (the PortageHub look). Written in Rust with egui.

![Overview](docs/screenshots/overview.png)

CachyOS ships its tuning as a package of files under `/usr/lib` that you either
have or don't. Gentoo Settings turns each of those settings into a separate
**tweak** that you can switch on and off. For each one it shows whether this
machine supports it, what the running system does now, and what else in `/etc`
sets the same thing. You review every file and command before anything changes.

## Init system detection

The init system is detected at startup and decides how each tweak is
implemented:

| Signal | Result |
|---|---|
| `/run/systemd/system` exists | systemd (PID 1) |
| `/run/openrc` exists | OpenRC |
| `/proc/1/comm`, then `/sbin/init` → systemd, then which of `openrc-run` / `systemd` is installed | fallback (chroots, unbooted trees) |

| CachyOS does (systemd) | Gentoo Settings on OpenRC | on systemd |
|---|---|---|
| `zram-generator.conf` | own `/etc/init.d/gentoo-settings-zram` service, `rc-update add … boot` | `/etc/systemd/zram-generator.conf.d/` drop-in (needs `sys-apps/zram-generator`) |
| `pci-latency.service` | `/etc/local.d/gentoo-settings-pci-latency.start` | `gentoo-settings-pci-latency.service`, enabled |
| `system.conf.d/10-limits.conf` (`DefaultLimitNOFILE`) | `/etc/security/limits.d` hard `nofile` (PAM) | `system.conf.d` + `user.conf.d` |
| `game-performance` uses `systemd-inhibit` | uses `elogind-inhibit` | uses `systemd-inhibit` |
| `tmpfiles.d` (THP) | needs `systemd-tmpfiles-setup` (or `opentmpfiles`) in the boot runlevel; checked | native |
| journald size, service timeouts, oomd slices, `user@` delegation, timesyncd, coredump retention, rtkit log level, NetworkManager → resolved | shown as **systemd only**, with what OpenRC uses instead | applied |

Services are started and stopped with `rc-service`/`rc-update` on OpenRC and
`systemctl` on systemd. A test checks that no tweak uses the wrong init system's
tools.

| OpenRC | systemd (preview) |
|---|---|
| ![Services on OpenRC](docs/screenshots/services-openrc.png) | ![Services on systemd](docs/screenshots/services-systemd.png) |

## What's in it

43 tweaks in 8 categories, each with a beginner-friendly explanation, a live
check ("now 1 · wants 100"), and a link to the CachyOS file it comes from:

- **Memory & Swap** — zram swap (zstd/lz4/lzo-rle), swappiness, VFS cache pressure,
  dirty bytes and background bytes, writeback interval, `page-cluster`, THP
  `defrag` and the THP shrinker (`max_ptes_none`).
- **Storage & I/O** — I/O schedulers per drive type (BFQ for HDD, mq-deadline
  for SATA SSD, your choice for NVMe from the schedulers the kernel offers),
  SATA link power `max_performance`, hdparm no spin-down for internal HDDs.
- **Kernel & Security** — NMI watchdog off, hardware watchdog blacklist, quiet
  console, `kptr_restrict`, SysRq, unprivileged user namespaces, `fs.file-max`.
- **Graphics** — NVIDIA `NVreg_InitializeSystemMemoryAllocations=0` (opt-in),
  NVIDIA runtime PM (laptops only), amdgpu for GCN 1.0/2.x.
- **Audio** — realtime limits for `@audio`, HPET/RTC and `cpu_dma_latency` for
  the audio group, HD Audio power saving off on AC, PCI latency timers.
- **Gaming** — `ntsync` at boot, `vm.max_map_count` (from Arch's base, which
  CachyOS inherits), per-process file limits, the `game-performance`,
  `zink-run` and `dlss-swapper` launchers for Steam launch options.
- **Network** — `netdev_max_backlog`, Wi-Fi regulatory domain from the time zone.
- **System Services** — the systemd-only settings listed above.

**APPLY CACHYOS DEFAULTS** stages every recommended tweak that applies to this
machine and isn't in effect yet. Settings the system already has are left alone.

![Memory & Swap](docs/screenshots/memory.png)

### Status of a tweak

| Badge | Meaning |
|---|---|
| ACTIVE | applied by Gentoo Settings and in effect |
| REBOOT NEEDED / RE-LOGIN NEEDED | applied, waits for a reboot or a new session |
| OVERRIDDEN | applied, but something else wins — e.g. a sysctl file sorted after `70-gentoo-settings.conf` |
| MODIFIED | the block in the file was edited by hand, or is incomplete |
| ALREADY SET | not applied by us, but the system already behaves that way (limits count as met when higher) |
| N/A | doesn't apply here: wrong init system, missing hardware or kernel feature, or a missing package (the `emerge` command is shown) |

For sysctls it follows `sysctl --system`'s order: drop-ins sorted by file name,
the first directory winning for equal names, then `/etc/sysctl.conf`. It names
the file that overrides a setting, or warns when a root-only file
(`/etc/sysctl.d/99-*.conf` with mode 0600) applies after ours.

## Review, apply, undo

![Review](docs/screenshots/review.png)

Nothing changes until you press APPLY in the review dialog. The dialog lists
every file with a diff, and every command that runs afterwards (`sysctl
--system`, `udevadm control --reload`, `rc-update add …`, `systemctl
daemon-reload`, …). Each transaction:

1. backs up the files about to change to `/var/lib/gentoo-settings/backups/<time>/`;
2. stops what disabled tweaks started (zram service, units);
3. writes files atomically (temp file + rename) with the right mode;
4. applies the change now where it can (sysctl, sysfs writes, udev reload,
   `modprobe`, services). It saves the previous live value first, so turning a
   tweak off puts the old value back;
5. appends to `/var/lib/gentoo-settings/history.jsonl`. The **Activity** tab
   shows that history and can undo any transaction.

| Applying (scratch root) | Activity |
|---|---|
| ![Apply](docs/screenshots/apply-scratch.png) | ![Activity](docs/screenshots/activity.png) |

## Files it owns

Every tweak is a marker block inside a file only Gentoo Settings writes:

```
# >>> gentoo-settings vm.swappiness = 100
# How readily the kernel swaps out anonymous memory instead of dropping page cache.
vm.swappiness = 100
# <<< gentoo-settings vm.swappiness
```

When a file has no blocks left it is deleted. Lines you add outside the blocks
are kept. It never edits a file it doesn't own: `/etc/modprobe.d/nvidia.conf`,
`/etc/sysctl.conf`, `rc.conf`, `make.conf` and the kernel command line are
left alone. The full list is in [`src/managed.rs`](src/managed.rs): drop-ins in
`/etc/sysctl.d`, `/etc/udev/rules.d`, `/etc/modprobe.d`, `/etc/modules-load.d`,
`/etc/tmpfiles.d`, `/etc/security/limits.d`, `/etc/local.d`, `/etc/init.d`,
`/etc/systemd/…`, `/etc/NetworkManager/conf.d`, plus scripts in
`/usr/local/libexec/gentoo-settings/` and launchers in `/usr/local/bin/`.

## Security model

- The GUI runs as your user. Changes go through
  `/usr/libexec/gentoo-settings-helper`, started with `pkexec` once per
  transaction (`auth_admin_keep`, so a second apply within a few minutes doesn't
  ask again).
- The helper accepts **tweak ids, on/off and a value — nothing else**. It
  computes files and commands from its own built-in catalog. Values are checked
  against each tweak's range or choices. Only paths in the managed list are
  written; only `/proc/sys` and `/sys` values are touched. Programs come from
  `/usr/bin`, `/usr/sbin`, `/bin`, `/sbin` with a clean environment.
- The GUI and the helper must be the same version, since both plan from their
  own copy of the catalog.
- Unlike PortageHub, there is no long-lived root session over a socketpair
  with a peer check: the helper is started once per transaction. Because its
  input is limited to catalog ids and checked values, a process that reached it
  could only toggle catalog tweaks.

## Install

```bash
cargo build --release
```

```bash
sudo dist/install.sh
```

This installs `gentoo-settings`, the helper, the polkit policy and a desktop
entry. `sudo dist/uninstall.sh` removes the program. Applied tweaks stay —
press **REVERT ALL** first if you want them gone.

## Command line

```bash
gentoo-settings --status
```

```bash
gentoo-settings --plan zram=lz4 vm.swappiness=100 -kernel.sysrq
```

`--status` prints every tweak with its badge and live value. `--plan` prints the
diff and commands for enabling (`id[=value]`) and disabling (`-id`) tweaks
without writing anything. `--list` lists the ids.

## Development

- `cargo test` runs the helper against scratch trees (fake `/proc`, `/sys`,
  `/etc`) for OpenRC and systemd: apply/revert with value restore, foreign lines
  kept, hand edits detected, zram per init system, systemd-only tweaks refused
  on OpenRC, bad input refused.
- `GENTOO_SETTINGS_ROOT=/some/tree gentoo-settings` runs the GUI and helper on a
  scratch tree. No password is asked and no programs are run.
- `GENTOO_SETTINGS_INIT=systemd gentoo-settings` previews the other init
  system's implementation (applying is disabled).
- `GENTOO_SETTINGS_SCRIPT="cat=memory;expand=zram;wait=1;shot=/tmp/a.png;exit"`
  drives the UI for screenshots.

## Not ported

| CachyOS file | Why |
|---|---|
| `etc/debuginfod/cachyos.urls` | CachyOS's own debuginfod server; Gentoo has none |
| `usr/share/glib-2.0/schemas/…login-screen…` | GNOME/GDM branding |
| `usr/share/X11/xorg.conf.d/20-touchpad.conf` | X11 only; Plasma on Wayland configures touchpads itself |
| `xswap-generator`, `systemd-zram-setup@` xswap drop-in | needs CachyOS's xswap kernel feature |
| `kerver` | the **System** tab shows the same (kernel, x86-64 level, schedulers, sched_ext), with *Copy report* |
| `topmem`, `paste-cachyos`, `cachyos-bugreport.sh`, `sbctl-batch-sign`, `dlss-swapper-dll` | tools, not settings |

Changes from CachyOS's defaults:

- The NVIDIA memory-zeroing option is opt-in and marked ADVANCED, because it
  gives up a security guarantee.
- On OpenRC the open-file limit only raises the hard limit, so it never lowers a
  higher soft limit you already have.
- `fs.file-max`, `vm.max_map_count` and `netdev_max_backlog` count as met when
  the system already has a higher value.

## License

GPL-3.0-or-later. The tweaks are ported from CachyOS-Settings (GPL-3.0). The
scripts are rewritten, not copied. The UI theme and widgets come from PortageHub.
