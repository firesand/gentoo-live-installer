# Daemon protocol (version 1)

`gentoo-installerd` listens on `/run/gentoo-installer/server.sock` (mode 0660, group
`gentoo-installer`). Each connection carries **one** JSON request line and receives
JSON-lines events until the daemon closes the connection. `PROTOCOL_VERSION` stays 1:
everything below that is new is additive, and clients discover it with
`capabilities`.

## Request

```json
{"version": 1, "action": "<name>", "request_id": "<optional client id>", ...}
```

`request_id`, when given, is echoed in `data.request_id` of every event of that request.

## Event

```json
{"type": "status|stage|progress|log|error|completed|result",
 "job_id": "...", "stage": "...", "message": "...", "progress": 42.0,
 "data": {}, "error": "...", "timestamp": 1792800000.0, "version": 1, "seq": 17}
```

* `seq` — monotonically increasing per job (install job or rescue operation). Events
  that do not belong to a job have no `seq`.
* `result` — payload of a non-streaming action (capabilities, log lines, hardware and
  rescue queries).
* Every streaming request ends with exactly one `completed` event (`data.success`).
  Failures send one `error` event first. A client must not treat `error` alone as the
  end of a stream.

### Error codes

`data.code` is machine-readable and namespaced `<domain>.<code>`. Clients branch on it,
never on the human text in `error`/`message`.

| Domain | Codes | Owner |
|---|---|---|
| `proto` | `bad_request`, `unsupported_action`, `internal`, `timeout` | server |
| `install` | `busy`, `not_running`, `failed`, `cancelled`, `invalid_manifest`, `cleanup_failed` | install manager |
| `storage` | `unpinned`, `identity`, `not_approved`, `live_medium`, `mounted`, `capacity`, `holders`, `preserved`, `bios_boot`, `collision`, `existing_system`, `raid`, `layout`, `target`, `absent`, `ambiguous`, `resume`, `cryptsetup`, `cleanup`, `error` | storage preparation |
| `rescue` | `busy_install`, `live_medium`, `plan_stale`, ... | rescue manager |
| `hw` | `st_live_medium`, `fw_uefi_from_bios`, `clock_skew`, ... (findings, not stream errors) | hardware probe |

An install failure keeps the specific code of its cause: a `StorageError` is
`storage.<code>`; every other failure is `install.failed`.

`install.busy` has two causes. With `data.blocked: true` the start was refused by the repair
module's busy check (a repair session, an unlocked volume or a repair that was cut short holds
the disks): nothing runs, there is nothing to `attach` to, and the start can be tried again
once the repair tool has let go. Without that key an installation really runs, and `attach`
follows it. `storage.holders` also covers Btrfs, ZFS and bcachefs pools that have a member on
another disk (every member shows the same filesystem UUID); `storage.release_holders` is the
consent for those as well.

## Install events (feature `install.v1`)

Thirteen stages run in this order, with these shares of the overall progress bar (a
`minimal` desktop moves the share of `desktop`, except 1, to `base`; `status.stages` carries
the table of the current job, `capabilities.stages` the default one):

`preflight` 2, `storage` 8, `stage3` 12, `mounts` 1, `repository` 5, `portage` 2, `kernel` 15,
`target` 2, `base` 12, `bootloader` 5, `desktop` 31, `validation` 3, `cleanup` 2.

| `type` | Carries |
|---|---|
| `stage` | `stage`, `message`, `progress`; `data.status` is `running`, `completed` or `skipped` (a resumed run verified the stage), `data.stage_progress` |
| `progress` | `stage`, `message`, `progress`, `data.stage_progress`; plus `data.package` / `data.packages` (emerge counters), `data.downloaded` / `data.total` (stage3), `data.degraded` / `data.bootable` (desktop stage, optional parts that failed) |
| `log` | `message` (one output line; secrets are redacted) |
| `status` | `data.status` (`running` when `start` is accepted, `cancelling`); or `data.warning: true` with the text in `message`: something went wrong after the installation itself succeeded (a cleanup that failed) |
| `error` | see below |
| `completed` | see below |

* `progress` is **always the overall percentage** and never goes backwards; the percentage
  inside the stage is `data.stage_progress`. A failed or cancelled run does not move the bar
  during cleanup.
* The `preflight` progress event carries `data.inspection` (the `inspect` reply without
  `hardware`) and `data.hardware_setup` (the plan's summary lines). The `storage` progress event
  carries `data.data.layout` and `data.data.plan` (what happens to which disk).
* An `error` event always has `stage`, `error` (text) and
  `data = {code, retryable, resumable, bootable}`. `retryable`: starting again can work (a
  cancel is retryable). `resumable`: a run with `resume: true` continues from the checkpoints
  (storage was prepared). `bootable`: the bootloader stage completed, so the disk boots even
  though the run failed. Every `error` is followed by `completed` with `success: false`.
  When the job's own worker crashes (an exception that escapes the installer: `install.failed`
  with the exception text) both events read `resumable` from the checkpoints in the same way,
  and it is `false` when they cannot be read.
* The `completed` event has `message`, `progress: 100` on success and
  `data = {success, bootable, degraded, warnings}`, plus `resumable` after a failure.
  `degraded` lists optional parts that failed (a desktop package, the NVIDIA driver, a
  service). `warnings` lists texts the user should read: decisions of the hardware plan that
  differ from what was asked (the proprietary driver does not fit this PC) and a cleanup
  that failed after a finished installation. Such a run is `success: true`: the system is
  installed, and the warning says which commands release the disks before a restart.
* The install log (`install.log` in the state directory, copied into the new system under
  `/var/log/gentoo-installer/`) holds one JSON object per line: `ts`, `job_id`, `type`, and
  `stage`, `message`, `error`, `code`, ... when they apply. The hardware plan of the job is a
  file of its own, in two shapes that must not be mixed up: see "Files of a job" in
  [contracts.md](contracts.md).

## Actions

### Install (feature `install.v1`)

* `inspect` (optional `{"manifest": {...}}`: the sections `storage`, `system`, `portage`,
  `network`, `options`, `hardware` of the window's settings, without accounts or secrets;
  unreadable settings are ignored) → one `status` event, then `completed`. `data`:

  | Key | Meaning |
  |---|---|
  | `architecture`, `memory_bytes`, `uefi`, `network`, `tools`, `disks`, `live_medium` | what the preflight sees; `network` was probed against the settings' mirror, through their proxy |
  | `errors` | texts of the findings that block an installation |
  | `warnings` | `[{code, severity, message, action}]`: the probe's findings (`hw.*`, any severity) and the preflight's warnings (`preflight.*`) |
  | `findings` | `[{code, severity, message}]` behind `errors` and the preflight warnings |
  | `hardware` | `HardwareProfile.to_dict()` (docs/contracts.md); absent when the probe broke, then `hardware_error` says why |
  | `clock` | `{skew_seconds, tls_ok}`: the mirror's time minus this computer's clock, and whether the certificate check passed. Absent without a network |
  | `missing_for_selection` | programs the given settings need but the live system lacks (only when settings were sent) |
* `status` → `status` event with the keys below.
* `cancel` → `status` event `{"status": "cancelling"}`; error `install.not_running` when
  idle. Cancelling is retryable and cleanup still runs.
* `start {"manifest": {...}, "resume": false}` → `status` `{"status": "running"}`, then
  the job's events, then `completed`. While a job runs, a second `start` gets error
  `install.busy`. An invalid manifest is `install.invalid_manifest`. The socket never
  accepts the CLI-only `--allow-unverified-disk`.

### Attach, status and log (feature `attach.v1`)

* `capabilities` → `result` with `data = {protocol, version, features, actions, stages}`
  (`stages` is `[{name, weight}]`). Features: `install.v1`, `attach.v1`, `hardware.v1`,
  `rescue.v1`. A module that registers with an object that has `capabilities()` adds its
  entry under its name (`rescue`: `{api, root, families, repairs, actions}`).
* `status` data: `status` (`idle|running|completed|failed|cancelled`), `job_id`, `stage`,
  `progress`, `message`, `seq` (last event of the current or last job), `started_at`,
  `finished_at`, `cancelling`, `resumable`, `retryable`, `error` (the **specific** last
  error, never a generic replacement), `error_stage`, `error_code`, `errors` (the last ten),
  `stages`, `log_path`, `leftovers` (mounts under the target root and `gentoo-*`
  device-mapper devices an earlier run left; computed while idle) and `rescue` (rescue manager
  summary, when present: `api`, `sessions`, `pool`, `running`, and `orphans`, the number of
  leftovers of an earlier daemon that were **not** taken over at start-up: a system that is still
  mounted becomes a session again and is listed by `rescue.session`, not counted here).
* `attach {"after_seq": 0}` → replays the current/last job's buffered events with
  `seq > after_seq`, then streams live events until that job's `completed` (which is sent
  again when the client already had it). No leading `status` event: call `status` first.
  Error `install.not_running` when no job has ever run. The ring buffer keeps at least the
  last 5000 events and **always** keeps every `stage`, `error` and `completed` event. A
  client that falls far behind misses log lines, never those, and gets one `log` event
  (no `seq`) saying how many were skipped.
* `log {"tail": 0, "format": "text"}` → `result` events carrying the redacted install log in
  order (`data.lines`, at most 1000 per event), then `completed` with `data.lines` = the
  total. `tail > 0` limits the output to the last N lines; `format` is `text` (readable
  lines) or `jsonl` (the log records). Used by the window's "Save log…", because the log
  file itself is root-only.

### Hardware (feature `hardware.v1`)

Neither action runs while an installation runs or while a repair operation is running
(`install.busy`; a repair session that only holds a mounted system does not count, the repair
may need the right clock first). Each reply is one `result` event
`data = {ok: bool, message: str}`, then `completed` (`success` = `ok`).

* `sync_time` (no arguments): asks the network for the time with chrony (`chronyc makestep`,
  or a one-shot `chronyd -q`). With a running chronyd the daemon then polls
  `chronyc -c tracking` for up to about 15 seconds: `ok` is true once chrony follows a time
  server and the clock is within 5 seconds of it, and `ok: false` (with a message that says
  which of the two is wrong) when chrony follows no server or the clock stays more than 5
  seconds off. `chronyc makestep` alone answers "200 OK" even when chronyd has no server, so
  the call takes up to about 15 seconds longer in that case. After a one-shot `chronyd -q` there
  is nothing to ask, and its exit status is the answer.
* `set_time {"utc": "YYYY-MM-DD HH:MM:SS"}` (years 2020 to 2100, `T` and `Z` accepted):
  `date -u -s`, and `hwclock --systohc` only when the hardware clock is known to keep UTC.
  A bad time is `proto.bad_request`.

### Rescue (feature `rescue.v1`)

`gentoo_installerd.rescue.api.register(registry)` adds the fourteen `rescue.*` actions
(`scan`, `unlock`, `activate`, `mount`, `session`, `events`, `query`, `plan`, `run`, `cancel`,
`shell`, `unmount`, `audit`, `recover`) under the feature. The daemon loads it at start-up
unless `--no-rescue` is given (`--rescue-root`, `--rescue-state-dir`, `--rescue-audit-dir`
set its directories). It registers only on the live medium (`/run/initramfs/live`,
`/run/live/medium`, or `cdroot` / `rd.live.image` on the kernel command line) and returns
`None` elsewhere. It adds a shutdown hook and the `rescue` status provider, and sets
`InstallManager.busy_check`, so installing and repairing never run at the same time
(`install.busy` / `rescue.busy_install`). Rescue events carry their own `seq` per
operation; `rescue.events {job_id|session_id, after_seq}` re-attaches.

Payload changes since the first release (additive, except for the removals named in the last item; a client that ignores unknown keys keeps working):

* `rescue.plan` / `rescue.mount` mount plan: `unresolved[]` entries can have `status: "unstable"`
  and a `device` key: a boot or system mount that fstab names by a kernel name (`/dev/sdb1`,
  `/dev/md0`) or a by-path link, and that here is a partition on another disk than the system's
  own (or of another file system type than fstab expects). It is not opened automatically; the
  same device is in `suggestions[]` (`{mountpoint, device, reason}`), whose entries can now
  name a device on another disk. The `reason` then asks for care.
* `rescue.mount` error `mounted_elsewhere` has `details.leftover` (bool): the mount that stops
  "open this system" was left by an earlier repair session (release it with the repair tool)
  rather than made by the desktop (eject it in the file manager). The request key
  `unmount_existing` is gone; an old client's key is ignored without an error.
* `rescue.session` orphan rows of `state: "unknown"` (mounts below a session directory that has
  no saved state) have a `path`.
* The repair tool supports Gentoo only: the `rescue` entry of `capabilities` is `{api, root, families: ["gentoo"], repairs: ["gentoo"], actions}`.
  Every entry of `rescue.scan`'s `candidates` has `supported` (bool). A system that is not Gentoo has
  `supported: false`, its `os` (the name it gives itself) and `warnings: ["Not a Gentoo system - this
  repair tool only supports Gentoo"]`, and nothing else worth reading. `rescue.mount` refuses it, by
  its candidate id or by its device, with the error `unsupported_system` (the CLI exits with status
  7) before anything is mounted. The `immutable` key of a candidate and of a session, and the `kernels`
  list parameter of `boot.kernel` (it selected Debian kernels), are gone; the scan has a hint
  `no_gentoo` when it found systems and none of them is Gentoo.

## Server-side registry (for modules such as rescue and hardware)

```python
# gentoo_installerd/server.py
def register_action(name: str, handler: Handler, *, feature: str | None = None) -> None: ...
def add_shutdown_hook(hook: Callable[[], None]) -> None: ...

class ActionRegistry:                 # the process-wide default is server.REGISTRY
    install_manager: InstallManager   # set by serve()
    options: argparse.Namespace       # the parsed command line, for flags of a plugin
    def add_status_provider(self, key, provider) -> None: ...      # adds key to every status reply
    def add_capability_provider(self, key, provider) -> None: ...  # adds key to the capabilities reply

class Channel:            # passed to every handler
    def send(self, event: Event) -> None: ...          # raises ChannelClosed when the peer left
    def send_with_fds(self, event: Event, fds: Sequence[int]) -> None: ...  # SCM_RIGHTS
    @property
    def peer(self) -> PeerCredentials: ...             # pid, uid, gid from SO_PEERCRED
    @property
    def closed(self) -> bool: ...

Handler = Callable[[dict, Channel], None]
```

* A handler raises `server.ActionError(message, code, **data)` for an expected failure; the
  server turns it into one `error` event and one `completed` event. Any other exception
  becomes `proto.internal`.
* The handler runs in the connection thread. Long-running work (an install, a rescue plan)
  must run on a manager-owned worker thread with its own event buffer. A slow or vanished
  client must never stop it. The server applies a 30 s timeout only while reading the
  request line.
* Registering a different handler under a taken name raises `ValueError`; the same handler
  (or an equal bound method) may register twice.
* Optional modules (`gentoo_installerd.hwactions`, `gentoo_installerd.rescue.api`) are
  loaded by `load_plugins`: each exposes `register(registry)`. An absent module is skipped; a
  module whose own import fails is logged as an error.
* SIGTERM and SIGINT only set a flag and start a shutdown thread. That thread cancels the
  install (cleanup still runs), calls every shutdown hook, and then stops the server.

## Manifest settings added in Wave 5

All of these are optional in a settings file (a missing one has its default) and live in the `manifest` of `start` and `inspect`.
Where a setting says "effect later", the field is stored and validated but no install output changes yet; the defaults reproduce the behaviour before Wave 5.
The one deliberate change is `options.ssh`, now `true`.

### storage
| Field | Type | Default | Meaning |
|---|---|---|---|
| `separate_boot` | bool | `false` | A /boot partition (1 GiB, ext4) of its own in the guided layout (Advanced). LUKS, LVM and RAID always get one. |
| `swap_kind` | `auto`, `partition`, `file`, `zram`, `none` | `auto` | Kind of swap. `auto` is a partition, or a file inside the root when the disk is encrypted. `file` is a swap file inside the root (Btrfs uses `btrfs filesystem mkswapfile`; not on F2FS). `zram` and `none` create no swap on the disk (the compressed-memory swap itself is set up by the hardware step). `partition` with LUKS is refused (it would be unencrypted). |
| `swap_choice` | `recommended`, `none`, `custom`, `hibernate` | `custom` | `custom` uses `swap_mib`. `recommended` and `hibernate` are sized by the daemon from this computer's memory with the Gentoo Handbook table (`recommend_swap_mib`); the window writes the same number into `swap_mib`. `none` means no swap. `hibernate` with `zram` is refused. |
| `filesystem` | `ext4`, `xfs`, `btrfs`, `f2fs` | `ext4` | `f2fs` (flash media) is refused with GRUB unless `separate_boot`; manual rows may use `f2fs` too (a manual F2FS root with GRUB needs a /boot row). ext4 below 8 GiB is made with `-T small`; mkfs.xfs reads the newest `lts_*.conf` of `/usr/share/xfsprogs/mkfs` when it exists. |
| `fstab_ident` | `uuid`, `label`, `partuuid` | `uuid` | How fstab names filesystems. `label` makes the guided install label them (`gentoo-root`, `gentoo-boot`, `gentoo-home`, `gentoo-swap`, `GENTOO-EFI`); `partuuid` is refused on a DOS table. A volume without a label or partition id (LVM, LUKS) keeps its filesystem UUID. |
| `atime` | `noatime`, `lazytime`, `relatime` | `noatime` | Access-time mount option written to `/etc/fstab`. |
| `boot_noauto` | bool | `false` | Mount /boot only on demand (`noauto`); needs a /boot partition. |

Also in effect: the guided root and /home partitions get GPT type codes 8304 and 8302 (Discoverable Partitions) with systemd-boot, the EFI stub or a UKI, else 8300; separate /tmp and /var/tmp are mounted with mode 1777; the FAT mount options get `tz=UTC` unless `system.rtc_local`.

### network
| Field | Type | Default | Meaning |
|---|---|---|---|
| `keep_proxy` | bool | `true` | Keep the proxy in the new system's make.conf (written today). Effect later. |
| `live_static` | object `{address, gateway, dns[]}` | all empty | Fixed address for the live session; if any part is set, all must be valid. Effect later. |
| `wireless_tools` | bool | `false` | Install wireless tools. Effect later. |
| `ppp` | bool | `false` | Install PPP support. Effect later. |
| `copy_live_wifi` | string (uuid) | `""` | uuid of a live NetworkManager Wi-Fi connection copied with its secret (I11); needs NetworkManager. Effect later. |

### portage
| Field | Type | Default | Meaning |
|---|---|---|---|
| `optimize` | `generic`, `native` | `generic` | `native` = `-march=native`. Effect later. |
| `binhost_variant` | `auto`, `generic`, `v3` | `auto` | Binary package variant. Effect later. |
| `mirrors` | list of HTTPS URLs | `[]` | Mirrors in order; `mirror` and `mirror_fallbacks` stay valid. Effect later. |
| `rsync_mode` | `snapshot`, `sync` | `snapshot` | `snapshot` = emerge-webrsync (today). Effect later. |
| `license_groups` | list of strings | `[]` | ACCEPT_LICENSE groups; `accept_license` stays the source of truth. Effect later. |
| `depclean` | bool | `false` | Run emerge --depclean at the end. Effect later. |
| `profile_override` | string | `""` | A published 23.0 profile path replacing the derived one. Effect later. |

### system
| Field | Type | Default | Meaning |
|---|---|---|---|
| `world_update` | `auto`, `now`, `later` | `later` | Was a bool: `true` loads as `now`, `false` as `later`. Only `now` updates @world today. |
| `stage_auto` | bool | `true` | Pick the matching stage3 automatically. Effect later. |
| `kernel` | adds `custom` | `gentoo-kernel-bin` | `custom` is built by the user's own choice. Effect later. |
| `kernel_config_snippet` | string | `""` | `CONFIG_X=value` / `# CONFIG_X is not set` lines (at most 16 KiB). Effect later. |
| `initramfs` | `dracut`, `ugrd`, `none` | `dracut` | `genkernel` is refused with a plain message. Distribution kernels still require `dracut`. |
| `hostonly` | bool | `false` | dracut host-only mode (the image is generic today). Effect later. |
| `generic_uki` | bool | `false` | Also build a generic Unified Kernel Image. Effect later. |
| `bootloader` | adds `none` | `grub` | `none` needs no UEFI. Effect later. |
| `secure_boot_mode` | `off`, `own-key`, `create`, `shim` | `off` | An old file with `secure_boot: true` loads as `own-key`. Effect later. |
| `serial_console` | string | `""` | e.g. `ttyS0,115200`. Effect later. |
| `desktop` | adds `cinnamon`, `mate`, `lxqt`, `gnome-light`, `sway`, `i3` | `kde` | Checked against `FAMILY_DESKTOP_SUPPORT`; the registry comes later. |
| `domain` | string | `""` | Domain name. Effect later. |
| `hosts_extra` | list of `"address name [alias...]"` | `[]` | Extra /etc/hosts lines. Effect later. |
| `extra_locales` | list of locales | `[]` | Locales besides `locale`. Effect later. |

### accounts
| Field | Type | Default | Meaning |
|---|---|---|---|
| `root_mode` | `same`, `separate`, `locked` | `separate` | Kept in step with `disable_root_login` (`locked` = true). `same`/`separate` still need a root password today. |
| `privilege` | `sudo`, `doas`, `run0`, `su` | `sudo` | `run0` needs systemd. Only `sudo`/`doas` have an effect today. |
| `ssh_password_login` | bool | `true` | Password login over SSH (I10). With `ssh` on, `false` and no authorized key is refused. Effect later. |
| `ssh_root_login` | bool | `false` | Effect later. |

### options
| Field | Type | Default | Meaning |
|---|---|---|---|
| `ssh` | bool | **`true`** (was `false`) | sshd enabled on the installed system. |
| `logger` | `sysklogd`, `syslog-ng`, `metalog`, `none` | `syslog-ng` | Was a bool: `true` loads as `syslog-ng`, `false` as `none`. Only syslog-ng/none have an effect today. |
| `cron` | `cronie`, `dcron`, `fcron`, `bcron`, `none` | `cronie` | Was a bool: `true` loads as `cronie`, `false` as `none`. |
| `timesync` | `chrony`, `timesyncd`, `none` | `chrony` | The old bool `chrony` still loads (`true` = `chrony`, `false` = `none`); `timesyncd` needs systemd. |
| `gentoolkit`, `eclean_kernel`, `mlocate`, `journal_persistent` | bool | `false` | Effect later. |
| `browser` | `firefox`, `none` | `firefox` | Effect later. |
| `flathub` | bool | `false` | Effect later. |
| `input_method` | `auto`, `none`, `ibus`, `fcitx5` | `auto` | `auto` installs nothing yet. |
| `accessibility` | bool | `false` | Effect later. |
| `gaming` | `off`, `flatpak`, `native` | `off` | `native` is refused without multilib; both are refused on the minimal desktop. Effect later. |
| `gaming_tweaks` | bool | `true` | Read only when `gaming` is not `off`. |
| `gaming_windows_tools` | bool | `false` | Effect later. |
| `gaming_accept_external` | bool | `false` | Native mode: accept the Steam overlay, ~amd64 packages and the download from Valve. |

### hardware
| Field | Type | Default | Meaning |
|---|---|---|---|
| `gpu_driver` | `auto`, `nvidia`, `nouveau`, `basic` | `auto` | A saved `open` loads as `nouveau`. `auto` still plans like today. |
| `nvidia_modules` | `auto`, `open`, `closed` | `auto` | `closed` is invalid on Blackwell (hwplan checks later). |
| `nvk` | `auto`, `on`, `off` | `auto` | Saved `yes`/`no` load as `on`/`off`. The plan's `scalars["nvk"]` keeps `yes`/`no`. |
| `safe_graphics_entry` | bool | `true` | Safe graphics boot entry. Effect later. |
| `nvidia_rtd3` | `auto`, `on`, `off` | `auto` | Effect later. |
| `nvidia_extras` | list of `vaapi`, `cuda`, `nvtop`, `persistenced` | `[]` | `cuda` needs `nvidia_cuda_license_accepted`. |
| `nvidia_cuda_license_accepted` | bool | `false` | |
| `signed_modules` | `off`, `auto`, `kernel_key` | `off` | Effect after the Secure Boot VM test. |
| `devices` | object of bool | `{}` | Keys: `openrazer`, `openrgb`, `game_rules`, `solaar`, `razer_frontend`, `iio_sensors`, `bolt`, `wacom`, `fingerprint`, `asus_tools`, `thinkfan`; any other key is an error. |
| `battery_charge_limit` | `0`, `60`, `80` | `0` | Effect later. |
| `lid_action` | `suspend`, `lock`, `ignore` | `suspend` | Effect later. |
| `vendor_workarounds` | bool | `false` | Effect later. |
