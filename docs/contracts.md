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
  regexes) live there. Checks that need probed hardware or the target tree live in
  `hwplan.validate_plan()` or the daemon preflight.
* `""` or `"auto"` means "decide from detection". The planner owns the decision. GUI
  defaults may pre-select a concrete value.
* `Storage.device_id_kind` is `""` (legacy serial/WWN), `"serial"`, `"wwn"` or `"path"`.
  `"path"` records that the user approved a disk without a serial or WWN. In that
  case `device_id` is `<by-path link name>|<size in bytes>|<model>`. The daemon
  requires an exact match and exactly one matching disk, and never accepts the live
  medium.
* `Hardware` holds the hardware-dependent choices (GPU driver, NVIDIA licence, power,
  zram, TRIM, firmware, HiDPI, removable EFI fallback, ...). See the field comments.

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

## Hardware probe (`src/gentoo_installer/hardware.py`; no Qt)

```python
def probe(root: Path = Path("/"), runner: Runner | None = None) -> HardwareProfile: ...
```

* `root` lets tests point the probe at a fixture tree (`/sys`, `/proc`, `/run`
  copies). `GENTOO_HW_ROOT` does the same from the CLI.
* `HardwareProfile.to_dict()` keys: `cpu`, `memory`, `machine`, `firmware`, `gpus`,
  `displays`, `storage_controllers`, `disks`, `nics`, `power`, `input`, `audio`,
  `bluetooth`, `clock`, `hypervisor`, `warnings`, `errors`.
* Each warning is `{code, severity: "info"|"warning"|"error", message, action}`, with
  a code in the `hw.` namespace.
* The daemon adds `profile.to_dict()` to the `inspect` reply as `data.hardware` (see
  protocol.md). The GUI never needs root to probe: anything that needs root comes
  from the daemon.

## Recommendations (`src/gentoo_installer/recommend.py`; no Qt)

* `recommend(profile, manifest) -> list[Recommendation]` returns human-readable
  advice, each item with a `code`.
* `apply_defaults(manifest, profile) -> None` fills `auto` fields with concrete GUI
  defaults (firmware, VIDEO_CARDS hint, power, guest tools, ...). The user can still
  change them.

## Hardware plan (`src/gentoo_installerd/hwplan.py`; pure, no subprocess)

```python
def plan(manifest: InstallManifest, hardware: dict) -> HardwarePlan: ...
def validate_plan(manifest: InstallManifest, hardware: dict) -> list[Issue]: ...
```

`HardwarePlan` is the single source for hardware-dependent target settings:

| Field | Meaning |
|---|---|
| `early_atoms` | firmware/microcode needed before the kernel hooks |
| `driver_atoms` | kernel modules such as `x11-drivers/nvidia-drivers` (before dracut) |
| `required_atoms`, `optional_atoms` | optional atoms are best-effort, one emerge each |
| `package_use`, `package_license` | `dict[atom, list[str]]`, written to `/etc/portage/*/installer-hardware` |
| `package_mask` | `list[str]` |
| `make_conf` | `dict[str, str]` such as `CPU_FLAGS_X86`, `VIDEO_CARDS`, `MAKEOPTS`, `L10N`; empty values are omitted, never written |
| `files` | `list[(path, content, mode)]`, written as root:root |
| `services` | `list[(name, runlevel)]`; runlevel is ignored on systemd |
| `kernel_cmdline` | `list[str]`, appended after the installer's own arguments |
| `post_steps` | `list[argv]` run in the chroot after packages |
| `summary` | short lines for the Review page and the install receipt |

`Issue` is `{code, severity, message}`; the code is in the `hw.` namespace.
`configure.py` consumes the plan. It never re-derives hardware decisions.

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

## Conventions for parallel work

* Python in a git worktree: `PYTHONPATH=<worktree>/src:<worktree>` (the venv's
  editable install points at the main tree).
* Put new tests in new files named after the area (for example
  `tests/backend/test_storage_identity.py`). Edit an existing test only when it asserts
  behaviour you changed.
* Keep shared public APIs additive. Write behaviour changes to someone else's module as
  integration notes in your final report; do not make them yourself.
