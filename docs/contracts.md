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
* A manual row cannot have the filesystem `none`: partitions that are not listed are never
  touched, so the row has to be removed. A filesystem row without a mount point is refused with
  its own message ("choose where it is mounted, or remove the row").
* A password and the LUKS passphrase must be storable: a terminal in another character set
  leaves a lone surrogate that no command input can carry, and the user is asked to type it
  again. Line breaks and NUL stay refused.

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
* `POOL_FILESYSTEMS` (`btrfs`, `zfs_member`, `bcachefs`), `FilesystemPool(fstype, uuid, inside,
  outside)` (with `spans_other_disks`, `describe()` and `reason()`) and `filesystem_pools(roots,
  nodes)`: the multi-device filesystems that have a member inside `nodes` and another outside,
  found by the UUID that every member shows. Erasing `nodes` would destroy data that the outside
  members hold too. A single-device filesystem is never reported. The daemon's release step
  (`storage.holders`) and a client's disk card use the same function.

`src/gentoo_installer/layout.py` is the single source for guided partition sizes:
`guided_plan`, `capacity_problems`, `minimum_root_mib` / `recommended_root_mib` and
`plan_storage(manifest, roots) -> StoragePlan` (every disk touched, every partition created,
formatted or kept, with warnings and capacity errors; `.lines()` and `.to_dict()`).
`luks_mapper_name(storage, name="root")` is the one place that names the dm-crypt mapping:
`gentoo-root`, or `gentoo-crypt` when LVM sits on top (the logical volume `gentoo/root` is
already `/dev/mapper/gentoo-root`, and the kernel allows each device-mapper name once). The
storage stage that opens the container and `rd.luks.name=<uuid>=<name>` of the initramfs
configuration both ask there; a resumed job reads the name from the receipt of the storage stage.

### fstab and layout entries

`LayoutEntry(device, mountpoint, filesystem, format, options, source)` is what storage
preparation hands to the fstab writer. `options` (a Btrfs `subvol=@,compress=zstd:3`) is
appended to the default mount options. `source` names a swap file by its path inside the new
system (`/swapfile`, `/swap/swapfile`); without it the entry is named by UUID. A guided Btrfs
install creates the subvolumes `@` (default) and `@home`; a guided LUKS install keeps its swap
in a file inside the encrypted root, never in a plain partition.

### Storage preparation and legacy BIOS

On a legacy BIOS target the storage stage also sets the boot flag that strict firmware looks
for: the boot flag of the protective MBR on a GPT disk, the active flag on a DOS table. A manual
layout gets it only when no partition of the disk is active (setting one clears the other), and a
failure there is a note (`Could not set the boot flag ...`); in guided mode it is an error. A UEFI
install never gets it. The stage's notes (`Storage.notes`, shown in the install log) say what
was set.

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
  advice, each item with a `code` (`rec.<name>`), a `severity` (`info`, `warning`), the
  `message`, and, when the advice maps to one manifest field, that `field` and the `suggestion`
  to store when the user accepts it.
* `rec.hwmonux_low_memory` (field `options.install_hwmonux`, suggestion `"false"`, severity
  `info`) is shown when the installed memory is below 8 GiB and HWMonux is on: it needs PySide6,
  which is compiled from source (about 40 minutes and up to 4 GiB per compile job). It is advice
  only: `defaults_for` and `apply_defaults` never change `options.install_hwmonux`, and its
  factory default stays on, so the user decides with the time and the memory in front of them.
* Formulas the planner shares with the window: `recommend_jobs`, `recommend_emerge_jobs`,
  `recommend_make_opts`, `make_opts_jobs(make_opts)` (the jobs a MAKEOPTS text allows, the last
  `-j` wins; `None` when it names none or leaves them unlimited) and `memory_heavy_jobs(total_kib,
  swap_mib)` (about 4 GiB per job, half of the swap counted, at least 1).
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
| `required_atoms`, `optional_atoms` | required atoms install with the desktop stage; optional atoms are best-effort, one emerge each (they can hold `media-fonts/noto` or `media-fonts/noto-cjk` for a language the default fonts lack) |
| `package_use`, `package_license` | `dict[atom, list[str]]`, written to `/etc/portage/*/installer-hardware` (sorts after `installer`, so these lines win) |
| `package_mask` | `list[str]` |
| `make_conf` | `dict[str, str]` such as `CPU_FLAGS_X86`, `VIDEO_CARDS`, `GRUB_PLATFORMS`; empty values are omitted, never written, so the profile defaults survive |
| `files` | `list[(path, content, mode)]`, written as root:root (also the two package.env files of the memory limit, see below) |
| `services`, `optional_services` | `list[(name, runlevel)]`; runlevel is ignored on systemd, names ending `.timer`/`.socket` are units. Optional ones are enabled only when their package is installed |
| `kernel_cmdline` | `list[str]`, appended after the installer's own arguments |
| `post_steps` | `list[argv]` run in the chroot after the desktop packages; a failure is a warning |
| `scalars` | resolved choices, JSON-safe: `microcode`, `power`, `trim`, `zram`, `prime`, `gpu_driver`, `nvidia_branch`, `removable_fallback`, `grub_target` (`i386-pc`, `x86_64-efi`, `i386-efi`), `btrfs_discard_async`, `luks_allow_discards`, `memory_heavy_jobs` (only when a limit was written), ... |
| `summary`, `notes`, `warnings` | short lines for the Review page (among them `Fonts: ...` for a language that needs a font package, and `Memory: ...` when the heavy packages get one compile job), why something non-obvious was decided, decisions that degrade or surprise (`Issue`) |
| `schema` | `PLAN_SCHEMA`; a saved plan with another schema is refused |

Helpers: `skel_files()`, `system_files()`, `services_to_enable(exists)`, `all_atoms()`,
`to_dict(redact_files=False)` (`redact_files` keeps path, mode, size and sha256 instead of
the content) and `from_dict()`. `Issue` is `{code, severity, message}`; the code is in the
`hw.` namespace. `nvidia_decision(manifest, profile)` says which driver branch fits.

`validate_plan` reports `hw.fw_uefi32` as an **error** when the boot setup cannot start on 32-bit UEFI
firmware (a Unified Kernel Image, the EFI stub, systemd-boot without multilib) and as a warning otherwise.

### Plan schema 2 (Wave 5)

`PLAN_SCHEMA` is 2; `READABLE_PLAN_SCHEMAS` is `(1, 2)`, so a plan saved by an earlier installer is still read on resume.
`HardwarePlan` gained six fields, all empty by default and left out of `to_dict()` while empty (so a plain plan looks like a
schema 1 plan apart from `schema`): `groups` (groups the first user joins, merged into `accounts.groups`), `package_keywords`
(atom to keywords, for `package.accept_keywords/installer-hardware`), `package_unmask` (atoms for `package.unmask/installer-hardware`),
`user_services` (`(name, kind)` enabled with `systemctl --global enable`), `repos` (companion overlay atoms) and `first_steps`
(lines for the finish page and `first-steps.txt`). The configuration code ignores empty fields; the planner fills them in later steps.
Manifest settings that were widened (`options.logger`/`cron` as choices, `system.world_update`, `hardware.nvk` as `on`/`off`) are read through
`Options.logger_enabled`/`cron_enabled` and plain comparisons; the plan's `scalars` keep their old spelling (`nvk` is `yes`/`no`, `gpu_driver` is `open`/`nvidia`).

### Memory-heavy packages

`hwplan.MEMORY_HEAVY_ATOMS` is a short list of packages the installer can pull in that need about
4 GiB per compile job, twice what MAKEOPTS assumes: `dev-python/pyside` (HWMonux), `dev-qt/qtwebengine`
(KDE), `llvm-core/llvm`, `llvm-core/clang` (both beside Mesa and PySide) and `net-libs/webkit-gtk`
(GNOME). With the binary host they are installed ready-made; compiled (a source install, or a USE flag
the binary host does not have, as for PySide), a few big compiles at once can exhaust a small PC: the
first VM acceptance run lost the compiler of PySide to the kernel on a 4 GiB machine. Every atom is in
the pinned tree and reached by a package set (`tests/hardware/test_plan_memory.py` checks both, with
`GENTOO_PINNED_TREE`). `dev-lang/rust` (rust.eclass prefers `rust-bin`) and `sys-devel/gcc` (in the
stage3, little memory per job) are left out on purpose.

When `recommend.memory_heavy_jobs(MemTotal, swap)` is lower than the machine-wide job count (the `-j`
of `portage.make_opts`, else `recommend_jobs` of the probed memory; the swap is the one the installation
creates), the plan adds `/etc/portage/env/installer-memory` (`MAKEOPTS="-jN"`),
`/etc/portage/package.env/installer-hardware` (one `atom installer-memory` line per package) and the
scalar `memory_heavy_jobs`. It adds nothing otherwise, and nothing when the memory is not known. Both
files are plan `files`, so they are written before the base and desktop stages and kept in the new
system (later updates of those packages keep the limit). The plan sizes the limit from what the probe
saw, while the setting in force is MAKEOPTS in the new system's `make.conf` (sized from the live memory
when the repository stage wrote it), and `package.env` replaces MAKEOPTS for the packages it names:
`Configurer.write_plan_files` therefore leaves both files out when the limit is not below that MAKEOPTS,
so the limit only ever lowers. Portage reads `package.env` per package, so everything else keeps MAKEOPTS. The calling environment of emerge still wins over `package.env` (checked
with Portage's own API: environment, then `package.env`, then `make.conf`), which is why the retry of a
build that was killed for lack of memory (`Configurer.emerge(..., one_job=True)`: `MAKEOPTS=-j1`,
`NINJAOPTS=-j1`, `--jobs=1`) really runs one job, and needs no change to these files.

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
  full plan including file contents; see "Files of a job") and a resume loads it instead of
  deciding again. A file of another job or schema is an error; a missing one means "decide
  again". Its summary and notes go to the install log, its surprising warnings to
  `completed.data.warnings`.
* **Stages**: `early_atoms` in the kernel stage before `installkernel`; `files` (the package.env
  files too) and `driver_atoms` before dracut builds the initramfs; `package_*` and `make_conf` when
  `make.conf` and `/etc/portage` are written; `required_atoms`, `optional_atoms`,
  `services` and `post_steps` in the desktop stage; `kernel_cmdline` into
  `/etc/kernel/cmdline` and `GRUB_CMDLINE_LINUX`; `scalars.grub_target` and
  `removable_fallback` at the bootloader; `luks_allow_discards` into the encryption
  arguments; `btrfs_discard_async` into the Btrfs mount options.
* **Left in the new system**: `/etc/gentoo-live-installer/install.json` (receipt) and, in
  `/var/log/gentoo-installer/` (mode 0700), `install.log`, `manifest.json` (redacted) and
  `hardware-plan.json` (paths and hashes of the plan's files, no contents).

### Files of a job

The hardware plan is written twice, in two shapes. Neither is the bare `HardwarePlan.to_dict()`;
a tool that wants the plan reads `["plan"]` (a test harness that looks for plan keys at the top
level of the state file finds none).

| File | Where | Shape |
|---|---|---|
| `hardware-plan.json`, the state file | `<state dir>/` on the live system, mode 0600 | `{"version": 1, "fingerprint": "<hash of the redacted manifest>", "created": "<UTC ISO-8601>", "plan": <HardwarePlan.to_dict()>}`, with every file's content. A resume uses it only when `version`, `fingerprint` and `plan.schema` match this job |
| `hardware-plan.json`, the support copy | `/var/log/gentoo-installer/` in the new system, mode 0600 in a 0700 directory | `{"version": 1, "created": "<UTC ISO-8601>", "plan": <HardwarePlan.to_dict(redact_files=True)>}`: no fingerprint, and each of the plan's `files` is `{path, mode, size, sha256}` instead of its content |

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

`root.lvm` is true when the installer created LVM (`storage.lvm`) or the root sits on a logical
volume that existed before (a manual layout). `root.raid` is the RAID level text (`"1"`, `"10"`, ...;
`"linear"` or `"md"` when lsblk names no level) of the array the installer created or the root sits
on, and `""` otherwise. A volume or array that only holds `/home` or swap leaves both unchanged.

Rescue reads it when present and falls back to detection. Detected facts win on a
mismatch.

## Target configuration (`src/gentoo_installerd/configure.py`)

`Configurer` applies the manifest and the hardware plan to the new system and decides nothing
about hardware. Its public API only grows:

* Volumes that existed before the installation (a manual layout on an activated volume group or
  array; `storage.lvm` and `raid_level` mean "create one"): `ExistingStack`,
  `detect_existing_stack(runner, layout, disks, resume_device, notify)` and
  `Configurer.detect_stack(layout, disks, resume_device)` find them; `prepare_stack_packages(stack)`
  writes the `lvm2` USE flags before `lvm2` is built; `finish_stack(manifest, stack)` writes
  `/etc/mdadm.conf` and enables the `lvm` and `mdraid` services on OpenRC. `configure_initramfs`,
  `kernel_stage_atoms`, `storage_tool_atoms`, `verify_boot_setup`, `build_receipt` and
  `write_receipt` take the optional keyword `stack=`; `configure_initramfs` also takes
  `resume_device=` (the swap partition that hibernation writes to).
  `root_stack_problems(manifest, runner)` is the preflight's check of a manual root on a device
  type the initramfs cannot assemble.
* Boot tools: `systemd_package(system)` names the stage3 package that carries `bootctl`, `ukify` and
  the systemd-boot images (`sys-apps/systemd-utils` on OpenRC, `sys-apps/systemd` on systemd), and
  `flag_rebuild(manifest, plan)` the one whose USE flags the installer changes and what must exist
  after the rebuild.
* A failed emerge: `explain_emerge_failure(output, read_log=None) -> EmergeDiagnosis`
  (`resolution`, `summary`, `build_failure`, `transient`, `out_of_memory`, `kind`, plus `failures`:
  `FailedBuild(package, phase, log)` for each package that failed itself, and `dropped`: the packages
  `--keep-going` left out because one they need failed). It understands the failure list that
  current Portage prints (`(cat/pkg-ver:slot::repo, ebuild scheduled for merge), Log file:`), the
  `ERROR: cat/pkg-ver::repo failed (compile phase)` messages, `>>> Failed to emerge` and the old
  numbered list. `read_log(path)` returns the end of a build log; with it the summary names the
  package and the first error line of its log, or says that its compiler was killed for lack of
  memory (`kind == "memory"`). `Configurer.explain(exception_or_output)` does that with
  `Configurer.build_log_tail(path)`, which reads the log below the target root and never outside it.
  Every failure the Configurer retries or degrades on goes through `explain`, and so does the
  hardware driver step of the kernel stage; a failed emerge of the other kernel and bootloader steps
  still ends the job with the plain command error.
* `Configurer.emerge(..., one_job=True)` is the retry of a build that was killed for lack of
  memory (one compile job, one package at a time); `install_essential` and `emerge_keep_going` use it
  once and then stop: when the packages already had one job (`compile_jobs(package)` reads MAKEOPTS and
  the plan's package.env limit from the new system) the message says "It ran out of memory even with
  one compile job: this PC needs more memory or swap for it."

## Rescue (`src/gentoo_installerd/rescue/`)

* `rescue.api.register(registry)` registers every `rescue.*` action and the
  `rescue.v1` feature with the server registry (protocol.md). The daemon calls it at
  start-up unless `--no-rescue` is given.
* `rescue.cli:main` is the `gentoo-rescue` console script. It talks to the daemon
  socket, or with `--local` runs as root without the daemon.
* The tool repairs Gentoo only. `rescue.osrelease.NOT_GENTOO` ("Not a Gentoo system - this repair
  tool only supports Gentoo") is the one sentence for every other system: a scan lists it
  (`supported: false`), `RescueManager.plan_mount` refuses to mount it (`unsupported_system`),
  and `Adapter.write_refusal` refuses every repair of it. Clients never branch on the wording.
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

## Module ownership

One owner per module, so that six engineers can work in parallel without editing the same file. `configure.py` is a
facade: it re-exports every name the old single module had, and `Configurer` is the sum of the mixins below
(`ConfigurerBase` plus one mixin per domain). Code calls `Configurer` as before; put new code in the module of its domain.
A call from one domain into another is declared once in `ConfigurerBase` (the `TYPE_CHECKING` block of
`configure_base.py`); keep that declaration in step with the owner's signature.

Owners: CORE (app shell, review, progress, manifest), STOR (storage), NET (network), PORT (Portage), BOOT (system, kernel,
bootloader, hardware), ACCT (accounts, tools, text installer).

| Module | Owner | Holds |
| --- | --- | --- |
| `gentoo_installerd/configure_base.py` | CORE | `ConfigurerBase` (target paths, files, chroot), `Resources`, install receipt, `efi_mountpoint`, `systemd_package` |
| `gentoo_installerd/configure_fstab.py` | STOR | `render_fstab`, the existing LVM/RAID/LUKS stack of a manual layout, filesystem and storage-tool atoms |
| `gentoo_installerd/configure_network.py` | NET | systemd-networkd/netifrc/dhcpcd files, NetworkManager keyfiles, live profile import |
| `gentoo_installerd/configure_portage.py` | PORT | make.conf, package.use/env/license, binhost, `PortageHub` and `HWMonux` companions on the target (`prepare_companions`, `configure_companion_overlay`) |
| `gentoo_installerd/configure_packages.py` | PORT | the emerge engine, package sets, failure diagnosis, cycle breaking |
| `gentoo_installerd/configure_desktop.py` | PORT | desktop atoms, `install_desktop`, desktop session files |
| `gentoo_installerd/configure_boot.py` | BOOT | kernel and bootloader atoms, initramfs, GRUB/systemd-boot/EFI stub, UKI, Secure Boot, `verify_boot_setup` |
| `gentoo_installerd/configure_boot_extra.py` | BOOT | safe-graphics entries, dist-kernel files (USE, config snippet, README), serial console, Secure Boot keys kept in `/etc/secureboot` |
| `gentoo_installerd/configure_hardware.py` | BOOT | applying the hardware plan (`run_plan_steps`, `write_plan_files`) |
| `gentoo_installerd/configure_system.py` | BOOT | hostname, locale, time zone, keyboard, hosts, services, logger, cron, sshd, `live_input_problems` |
| `gentoo_installerd/configure_accounts.py` | ACCT | users, groups, privileges |
| `gentoo_installerd/install.py` | CORE | stage list and weights, dispatch, preflight, validation and cleanup stages, checkpoints, events |
| `gentoo_installerd/stages/context.py` | CORE | `InstallError`, `StageContext` (what every stage may use) |
| `gentoo_installerd/stages/plan.py` | BOOT | the hardware plan of a job (plan, save, restore) |
| `gentoo_installerd/stages/storage.py` | STOR | storage, mounts and target (fstab) stages, root device and storage stack |
| `gentoo_installerd/stages/stage3.py`, `repository.py`, `portage.py`, `desktop.py` | PORT | the stage3, repository, portage and desktop stages |
| `gentoo_installerd/stages/kernel.py`, `bootloader.py` | BOOT | the kernel and bootloader stages and their checkpoint checks |
| `gentoo_installerd/stages/base.py` | CORE | the base stage (essential packages and services) |
| `gentoo_installer/pages/customize.py` | CORE | the tab host and the Hardware tab embedding |
| `gentoo_installer/pages/portage_tab.py` | PORT | Portage tab (mixin of `CustomizePage`; `load_portage`, `save_portage`) |
| `gentoo_installer/pages/network_tab.py` | NET | Network tab (`load_network`, `save_network`) |
| `gentoo_installer/pages/tools_tab.py` | ACCT | Applications && tools tab (`load_tools`, `save_tools`) |
| `gentoo_installer/pages/system.py` | CORE | init system, stage3 and profile, and the layout that embeds the two sections |
| `gentoo_installer/pages/desktop_section.py` | PORT | desktop cards, stage3 family rules (`load`, `collect`, `set_advanced`) |
| `gentoo_installer/pages/boot_section.py` | BOOT | kernel, initramfs, bootloader, Secure Boot and UKI controls (`load`, `collect`, `set_advanced`) |
| `gentoo_installer/pages/help.py` | CORE | `HelpNote`, `Badge`, `AdvancedSection`, `HANDBOOK_LINKS`; every page adopts them for its own text |

The window's Detail level (Guided or Advanced) calls `set_advanced(bool)` on every page (`Page.set_advanced` does nothing by
default). A page puts its expert-only controls in an `AdvancedSection`; the section opens in Advanced, collapses in Guided, and
never clears a value.
