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
* The `completed` event has `message`, `progress: 100` on success and
  `data = {success, bootable, degraded, warnings}`, plus `resumable` after a failure.
  `degraded` lists optional parts that failed (a desktop package, the NVIDIA driver, a
  service). `warnings` lists texts the user should read: decisions of the hardware plan that
  differ from what was asked (the proprietary driver does not fit this PC) and a cleanup
  that failed after a finished installation. Such a run is `success: true`: the system is
  installed, and the warning says which commands release the disks before a restart.
* The install log (`install.log` in the state directory, copied into the new system under
  `/var/log/gentoo-installer/`) holds one JSON object per line: `ts`, `job_id`, `type`, and
  `stage`, `message`, `error`, `code`, ... when they apply.

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
  summary, when present).
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

Neither action runs while an installation runs (`install.busy`). Each reply is one
`result` event `data = {ok: bool, message: str}`, then `completed` (`success` = `ok`).

* `sync_time` (no arguments): asks the network for the time with chrony (`chronyc makestep`,
  or a one-shot `chronyd -q`).
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
