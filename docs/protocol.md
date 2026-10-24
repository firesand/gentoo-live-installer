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
* `result` — payload of a non-streaming action (capabilities, status, log, rescue
  queries).
* Every streaming request ends with exactly one `completed` event (`data.success`).
  Failures send one `error` event first. A client must not treat `error` alone as the
  end of a stream.

### Error codes

`data.code` is machine-readable and namespaced `<domain>.<code>`:

| Domain | Examples | Owner |
|---|---|---|
| `proto` | `proto.bad_request`, `proto.unsupported_action`, `proto.internal`, `proto.timeout` | server |
| `install` | `install.busy`, `install.not_running`, `install.failed`, `install.cancelled` | install manager |
| `rescue` | `rescue.busy_install`, `rescue.live_medium`, `rescue.plan_stale`, ... | rescue manager |
| `hw` | `hw.st_live_medium`, `hw.fw_uefi_from_bios`, `hw.clock_skew`, ... (warnings) | hardware probe |

Human text is in `error`/`message`; clients must branch on `data.code`, never on wording.

## Actions

### Existing

* `inspect` → `status` event with the inspection (`data`), then `completed`.
  `data.hardware` (the hardware profile) and `data.warnings`
  (`[{code, severity, message, action}]`) are added when the hardware feature exists.
* `status` → `status` event, `data` described below.
* `cancel` → `status` event `{"status": "cancelling"}`; error `install.not_running`
  when idle.
* `start {"manifest": {...}, "resume": false}` → `status` `{"status": "running"}`,
  then the job's events, then `completed`. While a job runs, a second `start` gets
  error `install.busy`.

### New (feature `attach.v1`)

* `capabilities` → `result` with `data = {"protocol": 1, "features": [...],
  "actions": [...]}`. Features: `install.v1`, `attach.v1`, plus `hardware.v1` and
  `rescue.v1` when those modules are present.
* `status` gains: `stage`, `progress`, `seq` (last event of the current or last job),
  `started_at`, `finished_at`, `resumable` (bool), `error` (the **specific** last
  error, never a generic replacement), and `rescue` (rescue manager summary, when
  present).
* `attach {"after_seq": 0}` → replays the current/last job's buffered events with
  `seq > after_seq`, then streams live events until that job's `completed`. Error
  `install.not_running` when no job has ever run. The ring buffer keeps at least the
  last 5000 events and **always** keeps every `stage`, `error` and `completed`
  event, so a reconnecting client can rebuild the progress page.
* `log {"tail": 0}` → `result` events carrying the redacted install log in order
  (`data.lines`, at most 1000 lines per event), then `completed`. `tail > 0` limits
  the output to the last N lines. Used by the GUI's "Save log…" because the log file
  itself is root-only.

## Server-side registry (for modules such as rescue and hardware)

```python
# gentoo_installerd/server.py
def register_action(name: str, handler: Handler, *, feature: str | None = None) -> None: ...
def add_shutdown_hook(hook: Callable[[], None]) -> None: ...

class Channel:            # passed to every handler
    def send(self, event: Event) -> None: ...          # raises ChannelClosed when the peer left
    def send_with_fds(self, event: Event, fds: Sequence[int]) -> None: ...  # SCM_RIGHTS
    @property
    def peer(self) -> PeerCredentials: ...             # pid, uid, gid from SO_PEERCRED
    @property
    def closed(self) -> bool: ...

Handler = Callable[[dict, Channel], None]
```

* The handler runs in the connection thread. Long-running work (an install, a rescue
  plan) must run on a manager-owned worker thread with its own event buffer. A slow or
  vanished client must never stop it.
* The server applies a 30 s timeout only while reading the request line. Exceptions a
  handler does not catch become one `error` event with code `proto.internal` and a
  `completed` event with `success: false`.
* Exclusivity: install and rescue never run at the same time.
  `InstallManager.busy_check` is an optional `Callable[[], str | None]`; when it returns
  a reason, `start` fails with `install.busy`. Rescue checks
  `InstallManager.is_running()` and refuses with `rescue.busy_install`.
* SIGTERM and SIGINT only set a flag and start a shutdown thread. That thread cancels the
  install (cleanup still runs), calls every shutdown hook, and then stops the server.
