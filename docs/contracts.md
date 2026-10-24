# Shared contracts

The installer is built from modules that are developed in parallel. This file fixes
the interfaces between them. The daemon wire protocol is in [protocol.md](protocol.md).

## Manifest (`src/gentoo_installer/manifest.py`)

* `SCHEMA_VERSION` stays 1. All new fields are optional, and a manifest without them
  loads with the defaults. `from_dict` still rejects unknown keys, so the GUI and the
  daemon ship together.
* `validate()` returns a flat list. `validate_sections()` returns
  `{section: [errors]}` for `VALIDATION_SECTIONS` (`general`, `storage`, `system`,
  `locale`, `portage`, `network`, `accounts`, `options`, `hardware`). GUI pages route
  errors by section, never by wording.
* Each section has its own `_validate_<section>()` method. Structural checks (enums,
  regexes, combinations of fields) live there. Checks that need probed hardware or the
  target tree live in `hwplan.validate_plan()` or the daemon preflight.
* `""` or `"auto"` means "decide from detection". The planner owns the decision. GUI
  defaults may pre-select a concrete value.
* `InstallManifest.warnings() -> list[str]` is advice that never blocks (unverified desktop
  on a hardened or no-multilib stage3, companions on a minimal system, manual kernel, Secure
  Boot without automatic signing, dual boot without GRUB, imported Wi-Fi passwords).
  `summary()` is the Review page's list of rows; its `Encryption` row is
  `Storage.encryption_summary()`.

### Fields added after the first release

| Section | Field | Meaning |
|---|---|---|
| `storage` | `device_id_kind` | `""` (legacy serial/WWN), `"serial"`, `"wwn"` or `"path"`. `"path"` records that the user approved a disk without a serial or WWN; `device_id` is then `<by-path link name>\|<size in bytes>\|<model>`. The daemon requires an exact match and exactly one matching disk, and never accepts the live medium |
| `storage` | `additional_device_ids` | Manual mode: serial/WWN (or `by-path\|size\|model`) of every other disk a partition row lives on; erase mode leaves it empty |
| `storage` | `release_holders` | Consent to deactivate LVM, dm-crypt, md and swap on the target disk, and to erase a member of a set that spans other disks |
| `storage` | `overwrite_existing_system` | Consent to install over a kept (`format: false`) root that already holds files; `/etc` is backed up first |
| `system` | `profile` | `""` derives the published 23.0 profile (`derive_profile`); otherwise one of `PUBLISHED_PROFILES` |
| `system` | `keyboard_layout`, `keyboard_variant`, `keyboard_model`, `keyboard_options` | XKB keyboard for display managers and sessions; `keymap` (console) must agree with them for the LUKS prompt (`keymaps.py`) |
| `system` | `rtc_local`, `console_font`, `kernel_cmdline_extra` | Hardware clock in local time; console font; plain tokens appended after the installer's own kernel arguments (never `root=`, `init=` or the encryption arguments) |
| `portage` | `make_opts` | `""` sizes `-j`/`-l` from the CPU count and memory of the machine that runs the install |
| `portage` | `mirror_fallbacks` | Extra HTTPS mirrors for the stage3 download |
| `network` | `import_live_profiles` | Copy this session's NetworkManager connections (Wi-Fi passwords included) into the new system |
| `options` | `kde_apps` | `"core"` or `"full"` |
| `hardware` | (section) | The hardware-dependent choices (GPU driver, NVIDIA licence, power, zram, TRIM, firmware, HiDPI, removable EFI fallback, ...); see the field comments |

### Rules `validate()` enforces

* A manual partition row **must state `format`** in JSON (`from_dict` raises `ManifestError`
  otherwise): `true` formats it, `false` keeps its data. A swap row has an empty mountpoint.
* KDE needs a Standard or Desktop stage3 (`desktop_problem()`; the message names the
  stage3). The profile must be published and fit the init system, multilib and hardened
  choice of the stage3 (`profile_problem()`); a no-multilib stage3 needs `multilib` off.
  The tables `FAMILY_DESKTOP_SUPPORT`, `PUBLISHED_PROFILES`, `STAGE3_VARIANTS` and
  `stage_family()` are public so the window uses the same ones.
* `raid_level` only with `mode == "manual"` and a `/dev/md*` target (`Storage.raid_problem()`).
* `network.proxy` is empty or `http(s)://[user:password@]host[:port]`; SOCKS is refused (the
  stage3 downloader and wget cannot use it). A proxy whose password is `<redacted>` is
  refused until the password is typed again.
* A guided LUKS install has no separate `/home` (it would stay unencrypted).

### Secrets

`to_dict(redact=True)` replaces `password`, `root_password`, `luks_passphrase` and
`wifi_password` by `"<redacted>"` (when set) and hides only the password in
`network.proxy` (`scheme://user:<redacted>@host`, also when the password contains `@` or
`/`). The job fingerprint is the hash of that text, so it covers which secrets are set but
not their values. `InstallManifest.load_redacted(path)` reads such a file without validating
it: each `"<redacted>"` value becomes empty and the proxy keeps its placeholder.

## Block devices (`src/gentoo_installer/blockdev.py`)

There is one lsblk parser for the GUI, the daemon, hardware detection and rescue.
Its public API may only grow:

* `lsblk_argv(all_devices=False)`, `parse_lsblk(text) -> list[BlockDevice]`
* `BlockDevice` fields mirror `LSBLK_COLUMNS`, plus `walk()`, `key` (MAJ:MIN),
  `mounted`, `identity`, `to_dict()`.
* `all_nodes(roots)`, `find(roots, path)` (`LookupError` when absent, `ValueError`
  when genuinely ambiguous; a node repeated under several parents is one device),
  `parent_disks(roots, node)`.
* `path_identity(disk, by_path_name)` and
  `identity_matches(disk, kind, value, by_path_name="")`.
* `live_medium_keys(roots, sources=..., cmdline=...)`: the union of the
  mount-source rule and the dracut `root=live:CDLABEL=` label rule, including the
  disk that holds the medium.
* `install_target_problem(node, live_keys=...)` / `install_target_disks(roots, live_keys=...)`: which disks
  may receive an installation (not zram, loop, optical, read-only, the live medium, ...).

`src/gentoo_installer/layout.py` is the single source for guided partition sizes:
`guided_plan`, `capacity_problems`, `minimum_root_mib` / `recommended_root_mib` and
`plan_storage(manifest, roots) -> StoragePlan` (every disk touched, every partition created,
formatted or kept, with warnings and capacity errors; `.lines()` and `.to_dict()`).

### fstab and layout entries

`LayoutEntry(device, mountpoint, filesystem, format, options, source)` is what storage
preparation hands to the fstab writer. `options` (a Btrfs `subvol=@,compress=zstd:3`) is
appended to the default mount options. `source` names a swap file by its path inside the new
system (`/swapfile`, `/swap/swapfile`); without it the entry is named by UUID. A guided Btrfs
install creates the subvolumes `@` (default) and `@home`; a guided LUKS install keeps its swap
in a file inside the encrypted root, never in a plain partition.

## Hardware probe (`src/gentoo_installer/hardware.py`; no Qt)

```python
def probe(root: Path = Path("/"), runner: Runner | None = None, *, now: float | None = None) -> HardwareProfile: ...
```

* `root` lets tests point the probe at a fixture tree (`/sys`, `/proc`, `/run`
  copies). `GENTOO_HW_ROOT` does the same from the environment. A fixture is replayed from
  its recorded command output (`runner=None`), and `fixture_now(root)` gives the moment it
  was captured. `python -m gentoo_installer.hardware [--json] [--redact] [--root DIR]`
  prints the report.
* The probe only reads and never raises. Its own commands are `lsblk`, `lscpu`,
  `cpuid2cpuflags`, `efibootmgr` and `chronyc`; the daemon runs them through a runner that
  allows nothing else and stops each after 5 s.
* `HardwareProfile.to_dict()` keys: `schema`, `cpu`, `memory`, `machine`, `firmware`
  (`mode`, `efi_bits`, `secure_boot`, `lockdown`, `tpm`, `boot_entries`, ...), `gpus`,
  `displays`, `storage_controllers`, `disks` (with `serial`, `wwn`, `ptuuid`; use
  `to_dict(redact=True)` for reports), `nics`, `power`, `input`, `audio`, `bluetooth`,
  `peripherals`, `clock` (`chrony_running`, `ntp_synced`, `offset_s`, `rtc_mode`, `year`),
  `hypervisor` (`{detected, name, vendor, evidence}`; `machine.hypervisor` is the plain
  name), `network_profiles` (names of the live session's connections, never their
  contents), `warnings`, `errors`.
* Each warning is `{code, severity: "info"|"warning"|"error", message, action}`, with
  a code in the `hw.` namespace. `action` names what the window can offer
  (`sync_time`, `set_time`, `reboot_firmware`, `safe_graphics`, ...).
* The daemon adds `profile.to_dict()` to the `inspect` reply as `data.hardware` (see
  protocol.md). The GUI never needs root to probe: anything that needs root comes
  from the daemon.

## Recommendations (`src/gentoo_installer/recommend.py`; no Qt)

* `recommend(profile, manifest) -> list[Recommendation]` returns human-readable
  advice, each item with a `code`.
* `apply_defaults(manifest, profile) -> None` fills `auto` fields with concrete GUI
  defaults (firmware, VIDEO_CARDS hint, power, guest tools, ...). The user can still
  change them. Both accept the profile as a dict.

## Hardware plan (`src/gentoo_installerd/hwplan.py`; pure, no subprocess)

```python
def plan(manifest: InstallManifest, hardware: dict | HardwareProfile | None) -> HardwarePlan: ...
def validate_plan(manifest: InstallManifest, hardware: dict | None) -> list[Issue]: ...
def blocking_issues(manifest: InstallManifest, hardware: dict | None) -> list[Issue]: ...
```

`hardware=None` (nothing was probed) gives the generic, portable plan: both microcode
families and the firmware package, no CPU tuning. `HardwarePlan` is the single source for
hardware-dependent target settings; `configure.py` names no firmware, microcode, licence or
power package itself.

| Field | Meaning |
|---|---|
| `early_atoms` | firmware/microcode needed before the kernel hooks (`options.microcode` and `options.firmware` are inputs) |
| `driver_atoms` | kernel modules such as `x11-drivers/nvidia-drivers`; own emerge before dracut, a failure only degrades |
| `required_atoms`, `optional_atoms` | required atoms install with the desktop stage; optional atoms are best-effort, one emerge each |
| `package_use`, `package_license` | `dict[atom, list[str]]`, written to `/etc/portage/*/installer-hardware` (sorts after `installer`, so these lines win) |
| `package_mask` | `list[str]` |
| `make_conf` | `dict[str, str]` such as `CPU_FLAGS_X86`, `VIDEO_CARDS`, `GRUB_PLATFORMS`; empty values are omitted, never written, so the profile defaults survive |
| `files` | `list[(path, content, mode)]`, written as root:root |
| `services`, `optional_services` | `list[(name, runlevel)]`; runlevel is ignored on systemd, names ending `.timer`/`.socket` are units. Optional ones are enabled only when their package is installed |
| `kernel_cmdline` | `list[str]`, appended after the installer's own arguments |
| `post_steps` | `list[argv]` run in the chroot after the desktop packages; a failure is a warning |
| `scalars` | resolved choices, JSON-safe: `microcode`, `power`, `trim`, `zram`, `prime`, `gpu_driver`, `nvidia_branch`, `removable_fallback`, `grub_target` (`i386-pc`, `x86_64-efi`, `i386-efi`), `btrfs_discard_async`, `luks_allow_discards`, ... |
| `summary`, `notes`, `warnings` | short lines for the Review page, why something non-obvious was decided, decisions that degrade or surprise (`Issue`) |
| `schema` | `PLAN_SCHEMA`; a saved plan with another schema is refused |

Helpers: `skel_files()`, `system_files()`, `services_to_enable(exists)`, `all_atoms()`,
`to_dict(redact_files=False)` (`redact_files` keeps path, mode, size and sha256 instead of
the content) and `from_dict()`. `Issue` is `{code, severity, message}`; the code is in the
`hw.` namespace. `nvidia_decision(manifest, profile)` says which driver branch fits.

### How an installation uses the plan

* **Preflight** (`Installer._preflight`): `Discovery.inspect()` probes the machine
  (`Inspection.hardware`); the install stops with every message of
  `inspection.blocking_errors()`, `hwplan.blocking_issues()`,
  `configure.live_input_problems()` (zone, keymap, locale, console font and Secure Boot key
  that the live system cannot honour) and a clock that is more than 600 s off the mirror's.
  A preview (`--dry-run`) logs these instead. The tools the selection needs are checked
  (`tools_for_selection`); the signing tools are not among them, signing runs inside the new
  system.
* **Decision**: the plan is made once, in preflight. It is saved to
  `<state dir>/hardware-plan.json` (mode 0600; `{version, fingerprint, created, plan}`, the
  full plan including file contents) and a resume loads it instead of deciding again. A file
  of another job or schema is an error; a missing one means "decide again". Its summary and
  notes go to the install log, its surprising warnings to `completed.data.warnings`.
* **Stages**: `early_atoms` in the kernel stage before `installkernel`; `files` and
  `driver_atoms` before dracut builds the initramfs; `package_*` and `make_conf` when
  `make.conf` and `/etc/portage` are written; `required_atoms`, `optional_atoms`,
  `services` and `post_steps` in the desktop stage; `kernel_cmdline` into
  `/etc/kernel/cmdline` and `GRUB_CMDLINE_LINUX`; `scalars.grub_target` and
  `removable_fallback` at the bootloader; `luks_allow_discards` into the encryption
  arguments; `btrfs_discard_async` into the Btrfs mount options.
* **Left in the new system**: `/etc/gentoo-live-installer/install.json` (receipt) and, in
  `/var/log/gentoo-installer/` (mode 0700), `install.log`, `manifest.json` (redacted) and
  `hardware-plan.json` (paths and hashes of the plan's files, no contents).

## Install receipt (written into the target)

`/etc/gentoo-live-installer/install.json`, mode 0644, owned by root, with no secrets:

```json
{"version": 1, "installer": "0.1.0", "created": "<UTC ISO-8601>",
 "firmware": "uefi|bios", "bootloader": "grub|systemd-boot|efi-stub",
 "bootloader_id": "Gentoo", "esp": "/efi", "boot_separate": false,
 "init": "openrc|systemd", "kernel": "gentoo-kernel-bin", "initramfs": "dracut",
 "uki": false, "secure_boot": false, "profile": "default/linux/amd64/23.0/desktop/plasma",
 "root": {"fstype": "ext4", "uuid": "...", "luks_uuid": "", "lvm": false, "raid": ""},
 "cmdline_file": "/etc/kernel/cmdline", "hardware": ["<HardwarePlan.summary lines>"]}
```

Rescue reads it when present and falls back to detection. Detected facts win on a
mismatch.

## Rescue (`src/gentoo_installerd/rescue/`)

* `rescue.api.register(registry)` registers every `rescue.*` action and the
  `rescue.v1` feature with the server registry (protocol.md). The daemon calls it at
  start-up unless `--no-rescue` is given.
* `rescue.cli:main` is the `gentoo-rescue` console script. It talks to the daemon
  socket, or with `--local` runs as root without the daemon.
* Paths taken from a target system are never resolved with `pathlib` or `os.path`
  string logic. They are resolved component by component with `dir_fd` and
  `O_NOFOLLOW`, or with `openat2(RESOLVE_IN_ROOT)`, and mount points are re-checked
  right before use.
* Rescue never formats or repartitions. Chroot steps run with gid 0, an empty
  supplementary group list and umask 022. A synthesized kernel command line is
  bind-mounted over the target's `/proc/cmdline` so the live command line can never
  leak into a repaired boot entry.

## Other interfaces (provided by other parts)

* Text installer: console script `gentoo-installer-text`
  (`gentoo_installer.textui.cli:main`). `--manifest FILE --yes --confirm-device /dev/vda
  [--socket PATH] [--json-events]`, `--dry-run` validates and prints the plan. Exit codes: 0
  success, 1 install failed, 2 usage or invalid manifest, 3 refused (confirmation missing
  or wrong), 4 daemon unreachable, 130 interrupted.
* Repair window: `gentoo_installer.rescue_app.RescueWindow(socket_path, parent=None)`, a
  top-level widget that deletes itself on close; `app.create_repair_window()` imports it
  lazily.
* Live-medium autostart: the kernel command-line token `gentoo.autostart=installer-text` or
  `gentoo.autostart=rescue` makes the tty1 login run `sudo gentoo-installer-text` or
  `sudo gentoo-rescue` once; both boot entries use the plain `nox` token.

## Conventions for parallel work

* Python in a git worktree: `PYTHONPATH=<worktree>/src:<worktree>` (the venv's
  editable install points at the main tree).
* Put new tests in new files named after the area (for example
  `tests/backend/test_storage_identity.py`). Edit an existing test only when it asserts
  behaviour you changed.
* Keep shared public APIs additive. Write behaviour changes to someone else's module as
  integration notes in your final report; do not make them yourself.
