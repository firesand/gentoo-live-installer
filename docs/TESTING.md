# Testing the installer

This page is for people who change the installer or who want to prove that it works on a machine
that is not the author's. It covers the unit tests, the quality gate, and the QEMU harness that
installs Gentoo into disposable virtual machines (different disks, firmware, network cards,
laptop identities) and that can break an installed system on purpose to test the rescue tools.

Nothing described here is a claim that a given scenario has passed: a scenario is listed as
validated only after a real QEMU run recorded under `build/vm/`, and `--all` runs only those.

## Requirements

| For | You need |
|---|---|
| The Python tests | Python 3.13 or newer, `PySide6`, `pytest`, `pytest-qt`. From the repository root: `python3 -m venv --system-site-packages .venv && .venv/bin/pip install -e '.[dev]'`. |
| `scripts/test.sh`, `scripts/check.sh` | Run them from any directory; they change into the repository root. `cargo` (with `clippy`), `mypy` and `pkgcheck` are used when installed. Nothing needs the network (`--offline` for cargo). |
| The VM harness | `qemu-system-x86_64`, `qemu-img`, `ssh`, `ssh-keygen`, OVMF for UEFI scenarios (`/usr/share/edk2/OvmfX64`, `/usr/share/OVMF`, ...) and a **test ISO** (below). Without `/dev/kvm` QEMU falls back to software emulation: raise `--gui-timeout` and `--boot-timeout`. |
| Building the ISO | A rootful Docker daemon, an x86_64 host and 30 GiB free; see the README and `scripts/build-iso.sh --check`. |

The harness never writes to a host block device: every disk is a regular file below `build/vm/`
and the only host access of a VM is a loopback SSH forward.

## The three layers

1. **Unit and fixture tests** (`tests/`): pure logic, fixture trees, offscreen Qt. Fast.
2. **The fake lab** (`tests/vm/test_vm_runner.py`, `test_vm_scenarios.py`, `test_vm_rescue.py`,
   `test_vm_gui_run.py`): the harness's whole control flow (processes, sockets, SSH, timeouts,
   signals, result files) runs for real against stand-ins for `qemu-system-x86_64`, `qemu-img`,
   `ssh` and `ssh-keygen` in `tests/vm/fakes/`. A JSON rule table plays the guest. These tests
   are marked `slow` (about a second per scenario).
3. **Real VMs** (`scripts/test-vm.py`): the real ISO, real QEMU, real installs. Hours for a
   desktop, minutes for a minimal system. Only this layer proves that something boots.

The fake lab proves the harness is correct, not the installer. It cannot know whether a QEMU
option is accepted by your QEMU: the argument lists are tested against QEMU's documented syntax
and each new scenario must pass one real run before it joins `--all`.

## Running the tests

```sh
scripts/test.sh                 # pytest + cargo test + HWMonux smoke run; extra arguments go to pytest
scripts/test.sh -k manifest -x  # a subset
pytest                          # a bare pytest works too (pyproject.toml sets the paths)
scripts/check.sh                # the quality gate, fast steps
scripts/check.sh --full         # everything, including the slow tests, cargo clippy and pkgcheck
```

`scripts/check.sh` is the single gate:

| Step | Fast | `--full` | Notes |
|---|---|---|---|
| Python and shell syntax | yes | yes | `compileall`, `sh -n`/`bash -n` |
| shellcheck, ruff | yes | yes | only when installed (ruff: syntax errors and undefined names only) |
| mypy | yes | yes | compared with `scripts/mypy-baseline.txt` (line numbers stripped): a *new* finding fails |
| pytest | not `slow` | all | |
| cargo test, clippy | | yes | clippy against `scripts/clippy-baseline.txt` (a count that may only go down) |
| HWMonux `--dump` | | yes | |
| pkgcheck | | yes | on a temporary copy of the overlay; `MissingManifest`/`MissingUri` are ignored (the ISO build creates the distfiles) |
| ISO verifier tests, `lint-plan.py` | | yes | each only once the script exists |

After an intended change that moves a baseline, run `scripts/check.sh --update-baseline` and
review the diff. Environment: `PYTHON`, `SKIP_RUST=1`, `SKIP_PKGCHECK=1`, `CARGO_FLAGS`.

The suite ends instead of hanging: `tests/conftest.py` gives each test 120 s (the
`pytest-timeout` plugin when installed, otherwise an equivalent built in), forces Qt offscreen
(set `GENTOO_TEST_QT_PLATFORM` to watch the GUI tests), gives every run a private, symlink-free
temporary directory, and exits instead of waiting for a test thread that never finishes.
`sock_dir` and `unix_listener` are fixtures for tests that need a Unix socket (the path limit is
107 bytes).

## The VM harness

### Preparing a test ISO

The harness logs in over SSH, so it needs an ISO built with a throw-away key (the README
describes the build). A production ISO has no SSH access and is refused.

```sh
install -d -m 0700 build/vm-control
testkey="build/vm-control/iso-test-$(date +%s)"
ssh-keygen -q -t ed25519 -N '' -f "$testkey"
GENTOO_TEST_SSH_PUBLIC_KEY="$(cat "$testkey.pub")" ISO_DIST_DIR=dist/test-only JOBS=2 ./scripts/build-iso.sh
```

If `GENTOO_TEST_SSH_PUBLIC_KEY` is exported in your shell, `build-iso.sh` forwards it silently:
unset it before a normal build.

### Running a scenario

```sh
python3 scripts/test-vm.py --list-scenarios
python3 scripts/test-vm.py --scenario openrc --iso dist/test-only/<iso> \
  --ssh-host 127.0.0.1 --ssh-port 40222 --ssh-private-key "$testkey"
```

Memory rule: run one VM at a time, leave 3 GiB of host memory free beyond `--memory`, and give
desktop installs `--memory 12288 --build-jobs 1 --swap-mib 4096`. `--companions both|portagehub|none`
chooses which companion apps the manifest installs (and which probes run). `--install-mode gui` drives the
real graphical installer as the live user; `--check-resume` reruns the daemon with `--resume`;
`--cancel-then-resume` (GUI mode) cancels at stage3 and resumes. `--keep` keeps the disk and the
firmware variables after success (failed runs always keep them).

Exit status: 0 all passed, 1 a scenario failed, 2 usage or preflight error, 128+N when the
harness itself was killed by signal N (130 INT, 143 TERM, 129 HUP); QEMU is stopped on all of
them and even when the harness is killed with SIGKILL (the kernel sends QEMU SIGKILL).
`--cleanup-stale` stops QEMU processes left behind by an earlier crash.

### Scenarios

`--all` runs the first four. Everything else runs on request.

| Scenario | What it proves |
|---|---|
| `openrc`, `systemd` | minimal UEFI installs with each init system |
| `uefi-luks` | an encrypted root, unlocked over the serial console without an operator |
| `bios` | SeaBIOS, MBR partition table, static network configuration |
| `defaults` | the untouched wizard defaults (KDE, OpenRC, firmware, microcode, both companions); hours |
| `bios-gpt` | SeaBIOS with a GPT disk: BIOS-boot partition and `grub-install i386-pc` |
| `openrc-luks` | OpenRC with dracut's non-systemd passphrase prompt |
| `uefi-fresh-nvram` | the installed disk boots with empty NVRAM, so it needs the removable fallback path |
| `decoy-disk` | a second disk with data survives the installation byte for byte |
| `nvme-uefi` | NVMe naming (`nvme0n1p1`), an Intel NIC and a standard VGA |
| `sata-bios` | an old PC: i440FX, SATA (`sda`), Realtek NIC, QXL display |
| `usb-portable` | a removable USB install (`hardware.target=portable`) that boots with empty NVRAM |
| `serial-less` | a disk without a serial number, approved by its by-path identity |
| `uefi-4kn` | NVMe with 4096-byte sectors |
| `laptop-smbios` | notebook DMI strings and chassis type, a named CPU model, guest tools `auto` |
| `vmware-guest` | hypervisor detection from VMware's DMI strings; run with `--memory 2048` |
| `manual-two-disks` | manual mode on a disk partitioned beforehand, next to a disk with data that must survive |
| `reinstall` | installing twice onto the same disk with a different layout |
| `small-disk` | a 6 GiB disk must be refused before anything is written |
| `btrfs-root`, `xfs-root`, `lvm-root`, `systemd-boot` | filesystem, LVM and bootloader variants |

The validated four set `hardware.guest_tools=none`, explicit build jobs and no microcode so that
nothing automatic changes their configuration; the new scenarios use the installer's `auto`
behaviour where the point is to test it.

### Making the machine different

Every option has a default that reproduces the original machine (q35, one virtio-blk disk with a
serial number, virtio NIC and display). An option left out keeps the scenario's own value.

| Option | Choices |
|---|---|
| `--firmware` | `uefi` (OVMF), `bios` (SeaBIOS); the manifest follows |
| `--disk-bus` | `virtio`, `sata`, `nvme`, `usb`, `scsi` |
| `--no-disk-serial` | virtio only; the install is approved as `device_id_kind=path` |
| `--sector-size` | `512`, `4096` (logical and physical) |
| `--removable-disk` | USB only |
| `--nic` | `virtio`, `e1000e`, `rtl8139` |
| `--vga` | `virtio`, `std`, `qxl`, `cirrus`, `vmware`, `none` (not with `--install-mode gui`) |
| `--cpu` | any QEMU model: `Haswell`, `EPYC`, `host` (KVM only) |
| `--machine` | `q35`, `pc` (i440FX) |
| `--smbios` | `qemu`, `laptop-lenovo`, `laptop-dell`, `laptop-hp`, `desktop-asus`, `vmware`, `virtualbox`, `hyperv` |
| `--extra-disk SIZE` | another empty raw disk (`256M`, `2G`); repeatable |
| `--secure-boot` | OVMF's Secure Boot build (setup mode unless `--ovmf-vars-file` enrols keys) |
| `--ovmf-vars` | `persist` (default), `fresh-on-target`, `readonly` |
| `--ovmf-vars-file` | seed the NVRAM from a file (a kept `OVMF_VARS.fd`, or one with enrolled keys) |
| `--rtc` | `utc`, `localtime` |

Examples:

```sh
# A laptop with an NVMe disk and an Intel NIC
test-vm.py --scenario openrc --disk-bus nvme --nic e1000e --smbios laptop-dell --cpu Haswell ...
# 4Kn NVMe, UEFI, systemd
test-vm.py --scenario uefi-4kn ...
# The fast scenarios on SATA
test-vm.py --all --disk-bus sata ...
```

### Does the live ISO work on this machine? (`--live-only`)

A full install takes minutes to hours; booting the live ISO on a machine shape takes a minute or
two and already answers most hardware questions. `--live-only` boots the ISO, runs the disk
guard, asks the installer's daemon what it sees of the machine (the same `inspect` request the
GUI sends) and checks that it is what the VM is: the firmware mode, the memory, a working network
on this NIC model, the VM's disk offered unmounted, the required tools, and, unless `--vga none`,
that the graphical session came up on this display adapter. Nothing is installed.

```sh
test-vm.py --scenario openrc --live-only --disk-bus nvme --nic rtl8139 --vga std ...
test-vm.py --scenario bios   --live-only --machine pc --memory 2048 ...      # a low-memory old PC
test-vm.py --scenario openrc --live-only --live-expect expected.json ...     # assertions on the report
```

`expected.json` maps dotted paths of the inspection to values (`{"uefi": true, "disks.0.size":
43007344640, "hardware.machine.form_factor": "re:^(notebook|laptop)$"}`; a string starting with
`re:` is a regular expression), which is how the hardware report that newer daemons add is checked
without the harness knowing its schema. The report is kept as `live-inspection.json`.

What the harness cannot show, by construction: real GPU, Wi-Fi, Bluetooth, audio and sensor
behaviour, suspend and resume, battery and lid handling, vendor firmware quirks, Secure Boot with
enforcement, 32-bit UEFI. The DMI presets let the *detection code* see a laptop; they do not make
the VM one. Run the installer on the real machines you care about as well.

### The disposable-disk guard

The harness erases disks, so before it touches one it proves in the guest (`scripts/vm_guard.py`)
that it is the one it created: the exact size (whole GiB plus 1-64 MiB, different for every disk
and run, so no real disk can match by accident); the model, transport, sector size and removable
flag the emulated bus must report; the PCI vendor of its controller; the serial number when it has
one; a per-run nonce the VM carries in its SMBIOS OEM strings; and that no other disk is attached
(besides the CD-ROM and the decoys the scenario asked for). It runs before the install, before a
resume check, before breaking a system, and again, as the unprivileged user, inside the GUI
driver. The device it finds (`/dev/vda`, `/dev/nvme0n1`, `/dev/sda`) is what the manifest names.

### LUKS

LUKS manifests get `console=tty0 console=ttyS0,115200n8` through `system.kernel_cmdline_extra`
(ttyS0 last, so `/dev/console` is the serial port). `--luks-unlock serial` (the default) waits
for the prompt of either unlock path (systemd's "Please enter passphrase", dracut's
"Password (...) [1/3]:") on the serial socket, answers it, and answers again if the prompt comes
back; every answer is tied to a position in the log so an answered prompt never matches twice.
If the manifest has no serial console, the harness types the passphrase through QMP instead
(`--luks-unlock qmp` forces this), but only after the screen has changed several times and then
stopped changing, because keystrokes before the kernel is up land in the GRUB menu; this method is a
heuristic. `--luks-unlock external` leaves it to an operator. `luks-unlock-receipt.json` records
the method, the matched text and a hash of the serial log.

### GUI mode and detection

`--install-mode gui` drives the real wizard with `scripts/vm-gui-install.py`. With
`--gui-detect` (the default for the `defaults` scenario, `--no-gui-detect` turns it off) the
driver starts from the wizard's own defaults plus only what a person would type, selects the disk
like a user, and requires the final manifest to equal the expected one: firmware, partition table
(when the scenario fixes it) and disk identity must be what the GUI detected. A BIOS machine
whose wizard offers UEFI fails with `storage.firmware: the GUI chose 'uefi', expected 'bios'`.
The driver stops itself with status 2 when a page does not advance (`--navigation-timeout`) or the
backend is silent (`--idle-timeout`), and retries a slow network (`--readiness-timeout`) but never
an identity problem.

### What a run leaves behind

`build/vm/<scenario>-<time>-<id>/` holds `result.json` (verdict, probes, `advisory_findings`,
`luks_unlock`, `decoys`, `rescue`, `hardware`, `disk`), `manifest.json`, the logs
(`install.log`, `qemu-*.log`, `serial-*.log`, `ssh-*-wait.log`, `verify-*.log`), screenshots on
failure, `*-disk-guard.json`, and the event streams as `events.jsonl`, `resume-events.jsonl` and
`cancel-events.jsonl` (the result files hold a summary only, so they stay small). Failed runs keep
the disk and `OVMF_VARS.fd`; so does `--keep`.

The probes check that the installed system boots in the firmware mode it was installed for, has a
stable `root=` (UUID, PARTUUID, label or a mapper name, never a kernel device name) and the LUKS
argument, no argument of the live system, the swap partition, the right root source, and the
requested DMI identity. Advisory findings (sudo with the user's password, root locked, the install
receipt `/etc/gentoo-live-installer/install.json`) are recorded and printed but only fail with
`--strict-probes`.

## Testing the rescue tools

`gentoo-rescue` and the rescue GUI are separate components; the harness provides the broken
system and judges the result.

```sh
# 1. Install, verify it boots, then damage it from the live ISO and prove it no longer boots.
test-vm.py --scenario bios --rescue-break grub-cfg ...        # or initramfs, fstab-uuid
# 2. Repair it, boot it again, run every probe again (all in one run).
test-vm.py --scenario bios --rescue-break initramfs \
  --rescue-command 'gentoo-rescue --local --json repair --target {device}' ...
# 3. Repair a kept disk later (the image is never written; changes go to an overlay).
test-vm.py --scenario bios --target-image build/vm/<run>/disk.qcow2 \
  --rescue-command '...' --ovmf-vars-file build/vm/<run>/OVMF_VARS.fd ...
```

`--rescue-break` removes `grub.cfg` (also `boot/grub2/`), every `initramfs*`/`initrd*` image, or
replaces the root UUID in `/etc/fstab` with `00000000-dead-beef-0000-000000000000`. It works on
files of a mounted root only: nothing is formatted, repartitioned or wiped, and LUKS, LVM and
RAID roots are reported as unsupported. The damage must be *real*: the installed disk has
`--broken-timeout` seconds (default 120) to come up and a system that still boots is a failed test.
The repair command may run for `--rescue-timeout` seconds (default 1800).
For the fstab, whose damage does not always stop the boot, the proof is `findmnt --verify` in
the live system. A screenshot and the serial excerpt are kept (`broken-boot.ppm`,
`broken-boot-serial.txt`). `{device}` in the command stands for the guarded target disk.

`--target-image` accepts any qcow2 or raw image, including another distribution's, provided
you supply the manifest its probes need (`--target-manifest`, default `manifest.json` beside the
image) and the image has an ext4, XFS or Btrfs root that the breaker can mount. The harness never
downloads anything. The repair command and everything after it run as root in the live system;
the base image's size and modification time are checked afterwards.

Rescue runs always keep the disk. For UEFI images pass the kept `OVMF_VARS.fd` with
`--ovmf-vars-file` to keep the boot entries, or omit it to boot with empty NVRAM.

## Known gaps

Read these before trusting, or blaming, a result.

* **Nothing outside the original four scenarios has run on real QEMU.** The new scenarios, the
  hardware options, `--live-only` and the rescue flow are tested as argument lists and against the
  fake lab. Each needs a first real run, which is also where QEMU confirms the less common device
  options (for example that `-smbios file=` replaces QEMU's own enclosure record: the `dmi` probe
  of `laptop-smbios` checks `chassis_type`, and USB with 4096-byte sectors). QEMU's IDE/SATA disks
  are 512-byte only, so `--disk-bus sata --sector-size 4096` is refused.
* **LUKS unlock over serial needs the installed system to honour `system.kernel_cmdline_extra`**
  (`configure.py` must put it on `/etc/kernel/cmdline` and `GRUB_CMDLINE_LINUX`). Until it does,
  `uefi-luks` and `openrc-luks` time out waiting for the prompt, with a message saying so, and
  `--all` still ends with a failure at `uefi-luks`, as before.
* **`serial-less` fails until the daemon accepts `device_id_kind=path`.** Today
  `StorageManager` refuses a disk without serial or WWN even though the GUI offers it.
* **`--gui-detect` on a BIOS machine fails until the wizard seeds the firmware from detection**
  (the unit test in `tests/vm/test_vm_gui_driver.py` is an expected failure that starts passing then).
* **`holders` (an active LVM or md stack on the target) and `uefi-luks-all` are not scenarios yet:**
  the first needs a consent box the manifest cannot carry, the second a settled layout.
* **`scripts/check.sh --full` is red on pkgcheck** because of real findings in the overlay
  (`app-misc/hwmonux`: unknown USE flag `nvidia`, `liquidctl[python_targets_python3_14]` not
  solvable). The mypy and clippy baselines describe the tree they were written from: run
  `scripts/check.sh --update-baseline` after merging other work and review the diff.

## Troubleshooting

* **A QEMU process is left over after a crash**: `python3 scripts/test-vm.py --cleanup-stale`.
* **"UNIX socket path ... is too long"**: handled; QEMU starts in the run directory and uses bare
  socket names when the absolute path would exceed 107 bytes.
* **The XFCE session takes too long** (no KVM): `--gui-timeout 600 --boot-timeout 1800`.
* **A scenario fails with `Unknown ... fields`**: the ISO is older than the harness; rebuild the
  test ISO from the same tree.
* **`check.sh --full` fails in pkgcheck**: the findings are real overlay problems (for example an
  ebuild USE flag that does not exist); fix the ebuild, do not weaken the check.
* **`uefi-luks` and friends hang at the passphrase prompt**: read `serial-target.log` and
  `luks-unlock-receipt.json`; if the kernel never printed to ttyS0 the installed system ignored
  `system.kernel_cmdline_extra`.

## For the maintainers: tools that depend on the harness

`build/*.py` (untracked) load `scripts/test-vm.py`. `tests/vm/test_vm_public_api.py` pins the
names, signatures and command-line fragments they use; change them only together. The acceptance
generators read `result.json` keys and artifacts (also pinned there). When LUKS acceptance is
automated, the generators should accept `luks-unlock-receipt.json` instead of the operator's
`luks-root-observation.json`.
