# Testing the installer

This page is for people who change the installer or who want to prove that it works on a machine
that is not the author's. It covers the unit tests, the quality gate, and the QEMU harness that
installs Gentoo into disposable virtual machines (different disks, firmware, network cards,
laptop identities), and that can break an installed Gentoo on purpose to test the repair tool
(which repairs Gentoo only).

Nothing described here is a claim that a given scenario has passed: a scenario is listed as
validated only after a real QEMU run recorded under `build/vm/`, and `--all` runs only those. The
Wave 3 matrix (below) is the list of runs that still have to be made on real QEMU.

## Requirements

| For | You need |
|---|---|
| The Python tests | Python 3.13 or newer, `PySide6`, `pytest`, `pytest-qt`. From the repository root: `python3 -m venv --system-site-packages .venv && .venv/bin/pip install -e '.[dev]'`. |
| `scripts/test.sh`, `scripts/check.sh` | Run them from any directory; they change into the repository root. `cargo` (with `clippy`), `mypy` and `pkgcheck` are used when installed. Nothing needs the network (`--offline` for cargo). |
| The VM harness | `qemu-system-x86_64`, `qemu-img`, `ssh`, `ssh-keygen`, OVMF for UEFI scenarios (`/usr/share/edk2/OvmfX64`, `/usr/share/OVMF`, ...) and a **test ISO** (below). Without `/dev/kvm` QEMU falls back to software emulation: raise `--gui-timeout` and `--boot-timeout`. |
| Building the ISO | A rootful Docker daemon, an x86_64 host and 30 GiB free; see the README and `scripts/build-iso.sh --check`. |

The harness never writes to a host block device: every disk is a regular file below `build/vm/`
and the only host access of a VM is a loopback SSH forward. The ISO's boot menu is read with a
small ISO 9660 reader of the harness (`scripts/vm_iso.py`); `xorriso` is not needed.

## The three layers

1. **Unit and fixture tests** (`tests/`): pure logic, fixture trees, offscreen Qt. Fast.
2. **The fake lab** (`tests/vm/test_vm_runner.py`, `test_vm_scenarios.py`, `test_vm_rescue.py`,
   `test_vm_install_paths.py`, `test_vm_matrix.py`,
   `test_vm_gui_run.py`): the harness's whole control flow (processes, sockets, SSH, timeouts,
   signals, result files) runs for real against stand-ins for `qemu-system-x86_64`, `qemu-img`,
   `ssh` and `ssh-keygen` in `tests/vm/fakes/`. A JSON rule table plays the guest
   (`tests/vm/fake_env.py`, and `tests/vm/fake_rescue.py` for the live system's `gentoo-rescue`); the
   fake QEMU can print different console text per boot and restart by itself. These tests are marked `slow` (about a second per scenario).
   `tests/vm/test_vm_rescue_driver.py` runs the harness's rescue driver against the real
   `gentoo-rescue` command line on the rescue tests' laboratory (a fake kernel and fake tools), and
   `tests/vm/test_vm_probes.py` checks the probes against the receipt and the hardware plan that the
   product itself produces. `tests/vm/data/` holds what the first real VM runs printed (the hardware
   plan record, the output of `emerge -pvuDN @world`, the installer's final event) and first sectors
   that `parted` and `sgdisk` wrote with the commands of the storage stage, so that the parsers are
   tested against what the tools really say.
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
107 bytes). The HWMonux tests (`apps/hwmonux/tests`) are part of the default `pytest` run.

## The VM harness

### Preparing a test ISO

The harness logs in over SSH, so it needs an ISO built with a throw-away key (the README
describes the build). A production ISO has no SSH access, so the harness would only time out waiting for it.

```sh
install -d -m 0700 build/vm-control
testkey="build/vm-control/iso-test-$(date +%s)"
ssh-keygen -q -t ed25519 -N '' -f "$testkey"
GENTOO_TEST_SSH_PUBLIC_KEY="$(cat "$testkey.pub")" ISO_DIST_DIR=build/test-dist JOBS=2 ./scripts/build-iso.sh
```

`ISO_DIST_DIR` must be a directory outside `dist/`, not `dist/` and not a directory inside it: `build-iso.sh`
refuses both, because a release is taken from `dist/` as a whole and an ISO with a root login must never be in it.

If `GENTOO_TEST_SSH_PUBLIC_KEY` is exported in your shell, `build-iso.sh` forwards it silently:
unset it before a normal build. The test ISO must be built from the same tree as the harness.

### Running a scenario

```sh
python3 scripts/test-vm.py --list-scenarios
python3 scripts/test-vm.py --scenario openrc --iso build/test-dist/<iso> \
  --ssh-host 127.0.0.1 --ssh-port 40222 --ssh-private-key "$testkey"
```

Memory rule: run one VM at a time (or let `--matrix` run two), leave 3 GiB of host memory free
beyond `--memory`, and give desktop installs `--memory 12288 --build-jobs 1 --swap-mib 4096`.
`--companions both|portagehub|none` chooses which companion apps the manifest installs (and which
probes run). **`both`, the default, installs HWMonux, which pulls PySide: there is no binary package
for it, so it is compiled from source on every install, for an hour or more, and one compile job
needs several GiB.** The first real runs (4 GiB of memory, 512 MiB of swap, two jobs) had the compiler
killed by the kernel (and, once, a guest that thrashed until SSH gave up). Treat `--companions both`
like a desktop install, or use `--companions portagehub` (a Rust build that a 4 GiB VM does; the probe
`disabled-hwmonux-absent` then checks that HWMonux really is not there).
`--install-mode gui` drives the real graphical installer as the live user,
`--install-mode text` the console installer (below); `--check-resume` reruns the daemon with
`--resume`; `--cancel-then-resume` (GUI mode) cancels at stage3 and resumes. `--keep` keeps the disk
and the firmware variables after success (failed runs always keep them).

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
| `bios-gpt` | SeaBIOS with a GPT disk: BIOS-boot partition and `grub-install i386-pc`; the boot flag of the protective MBR is read |
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
| `bios-sata-minimal` | an old PC: SeaBIOS, SATA (`sda`), Intel NIC, standard VGA, MBR partition table |
| `uefi-nvme-luks-openrc` | an encrypted OpenRC laptop: NVMe, QXL display, passphrase on the serial console; swap must be a file inside the encrypted root |
| `uefi-luks-lvm` | guided LUKS with LVM inside on OpenRC: the container is `gentoo-crypt` (`rd.luks.name=<uuid>=gentoo-crypt`), the root logical volume `gentoo-root` (`rd.lvm.lv=gentoo/root`); dracut unlocks and activates both. Never booted before the Wave 3 matrix |
| `uefi-manual-two-disks` | a manual layout across two disks (ESP and root on the first, swap and `/home` on the second) next to a third disk that must stay byte for byte as it was |
| `text-install` | the console installer: the ISO's text entry, then `gentoo-installer-text` with a device confirmation |
| `rescue-gentoo` | a minimal Gentoo installed first, then broken and repaired three times (menu file, initramfs, fstab) |
| `screenshots` | the live ISO only: a picture of the live desktop and of every installer page, Guided and Advanced, at 1366x768 and 1920x1080 (see below) |
| `dmi-asus-rog`, `dmi-razer-blade`, `dmi-lenovo-legion` | the live ISO only, with the DMI strings of an ASUS ROG laptop, a Razer Blade or a Lenovo Legion: the installer's hardware report (`python3 -m gentoo_installer.hardware --json`, run in the live system) must read the vendor, product and (once the probe has it) `vendor_family` |
| `safe-graphics-grub`, `safe-graphics-systemd-boot`, `safe-graphics-efi-stub` | the NVIDIA driver is requested, so the installed boot menu must list Safe graphics (`nomodeset`, the graphics modules blacklisted); the entry is chosen once and booted (needs backlog items B04 and B05; compiles the NVIDIA driver) |
| `secure-boot-modules` | systemd-boot with a UKI signed by the test key, booted under OVMF's Secure Boot build; an unsigned and a signed dummy module are loaded on `gentoo-kernel-bin`; the verdict is the evidence B07 step 2 waits for |
| `desktop-kde-openrc`, `desktop-kde-systemd`, `desktop-gnome-openrc`, `desktop-gnome-systemd`, `desktop-xfce-openrc`, `desktop-xfce-systemd`, `desktop-cinnamon-openrc`, `desktop-cinnamon-systemd`, `desktop-mate-openrc`, `desktop-mate-systemd`, `desktop-lxqt-openrc`, `desktop-lxqt-systemd`, `desktop-gnome-light-openrc`, `desktop-gnome-light-systemd`, `desktop-sway-openrc`, `desktop-sway-systemd`, `desktop-i3-openrc`, `desktop-i3-systemd` | one scenario per Guided desktop and init system: the desktop's package is installed, the display manager runs with its greeter, the desktop's session file exists, and `pactl info` answers with PipeWire in a login session. Hours each: run with `--memory 12288 --build-jobs 1 --swap-mib 4096 --companions none` |
| `ssh-keys-only` | `accounts.ssh_password_login=false`: `sshd -T` of the installed system must show password and keyboard-interactive login off and root login off (interface I10) |
| `live-remote-access` | the live ISO only: sshd runs at boot, `gentoo-remote-access status/on/off` with a password login as `gentoo` from the host, fresh SSH host keys on a second boot (interface I9) |
| `rescue-chroot` | a minimal Gentoo installed first, then `gentoo-chroot` run non-interactively from the live ISO: it finds the system, mounts it, runs commands with working DNS and leaves nothing mounted (interface I12) |

The validated four set `hardware.guest_tools=none`, explicit build jobs and no microcode so that
nothing automatic changes their configuration; the new scenarios use the installer's `auto`
behaviour where the point is to test it.

### The installer paths

`--install-mode cli` (the default) runs `gentoo-installerd --manifest ... --yes`, `gui` drives the
wizard (see "GUI mode"), and `text` runs the console installer on the live system, as the person
with a dead desktop would:

1. `gentoo-installer-text --manifest FILE --dry-run` must print the plan and exit 0;
2. started without `--confirm-device`, and with a device that is not the manifest's, it must
   refuse with exit status 3 and run nothing destructive (the harness checks the disk is still
   blank afterwards);
3. `gentoo-installer-text --manifest FILE --yes --confirm-device <the guarded disk> --json-events`
   installs; its events are read like the daemon's. Exit statuses: 0 success, 1 the installation
   failed, 2 usage or an invalid manifest, 3 refused, 4 the daemon could not be reached, 130
   interrupted. The harness names the meaning in its error.

`--boot-entry ENTRY` starts the live system from an entry of the ISO's boot menu instead of the
default: `default`, `text` (text console only), `text-ram`, `text-installer`, `serial` (console on
ttyS0), `rescue`, `desktop`, `verbose`, `compat`, a title, or a regular expression matched against
the titles; `--list-boot-entries --iso FILE` prints them. The menu is drawn by `gfxterm`, which a
harness cannot drive reliably over a serial port, so the harness reads `/boot/grub/grub.cfg` out of
the ISO and starts QEMU with the entry's kernel, initramfs and arguments (`-kernel`, `-initrd`,
`-append`) while the ISO stays attached as the CD-ROM. **GRUB itself is not part of such a run**
(`scripts/verify-iso.py` checks the menu file; every other run boots through GRUB). Afterwards the
harness reads `/proc/cmdline` in the live system and requires every argument of the entry to be
there. An entry with `nox` starts no desktop, so it cannot be combined with `--install-mode gui` or
the graphical check of `--live-only`. `text-install` uses `--boot-entry text`.

`text` is the plain text console entry ("Text console only ..."). It must not be an entry that
starts `gentoo-installer-text` or `gentoo-rescue` by itself on tty1 (`gentoo.autostart=`): such an
installer would compete with the one the harness runs over SSH. `text-installer` and `rescue` are
the names for those autostart entries, matched by title (`ALIASES` in `scripts/vm_iso.py`); the
menu of `iso/cd_overlay/boot/grub/grub.cfg.in` has both ("Install in text mode" and "Repair a system (text console)").
The text console entry also asks for a keyboard layout; the initramfs waits ten seconds for an
answer and then goes on, which the boot timeout covers. The ISOs built before Wave 1 (`dist/test*`)
have only three "Boot LiveCD" entries, so every entry except `default` is missing there. They are test
images with a root login and are named like the release: move them out of `dist/` (for example to
`build/test-dist/`) so that nothing which publishes `dist/` takes them along.

### What is checked after an install

After the first boot of the installed disk the harness runs these probes as the test user.
Advisory probes record a finding in `result.json` (`advisory_findings`) and print it; only
`--strict-probes` turns a finding into a failure. Every job of the matrix runs with `--strict-probes`.
Information (`world-drift`) is kept in `result.json` and is neither.

| Probe | What must be true | If it is not |
|---|---|---|
| `init`, `hostname`, `user`, `network`, `dns`, `portage` | the basics boot and work; `network-static` for the static-address scenarios | failure |
| `hwmonux`, `portagehub` or `disabled-*-absent`, `desktop` | the companions and desktop the manifest asked for are installed and run; those it declined are absent. A companion that is missing carries the reason the installer gave for it in its final event | failure |
| `firmware`, `cmdline`, `root-source` | booted in the firmware mode it was installed for; a stable `root=` (UUID, PARTUUID, label or mapper name) and the encryption argument, nothing of the live system, the requested extra arguments; in a guided install the container name of `rd.luks.name` (`gentoo-root`, or `gentoo-crypt` when LVM sits inside) and `rd.lvm.lv=gentoo/root` for LVM; root on `/dev/mapper` for LUKS and LVM | failure |
| `swap` | a swap of the requested size is active (zram does not count). An encrypted install keeps swap in a **file** inside the encrypted root: a raw swap partition is a failure | failure |
| `receipt` | `/etc/gentoo-live-installer/install.json` exists and agrees with the manifest: firmware, bootloader, init, kernel, initramfs, UKI, Secure Boot, profile, ESP mount point, root file system and UUID, LUKS UUID, LVM, RAID, separate `/boot` | missing: finding; wrong: failure |
| `efi-fallback` | UEFI installs that must have it leave a non-empty EFI program at `EFI/BOOT/BOOTX64.EFI` on the ESP (read with `sudo`, the ESP is mounted with umask 0077) | failure; no sudo: finding |
| `boot-flag` | BIOS installs (guided): the first sector of the installed disk, read in the installed system with `sudo`, carries the flag strict BIOS firmware looks for: the protective MBR of a GPT disk has the boot flag (byte 446 is `0x80`), a DOS disk has exactly one active partition, the one that holds `/boot` (else `/`). SeaBIOS never asks for it, so a VM cannot show it by booting. UEFI installs and manual layouts (which keep a flag that is already there) are not looked at | failure; no sudo: finding |
| `hardware-plan-file` | the daemon left `/var/lib/gentoo-installer/hardware-plan.json` in the live system (copied out to `hardware-plan.json`; the file is a record `{version, fingerprint, created, plan}`, a bare plan is read too) and the plan in it fits a virtual machine: no Intel microcode, no guest tools when they are switched off, the hypervisor's tool when it is on automatic and the DMI strings name the hypervisor | missing: finding; wrong: failure |
| `hardware-plan` | the installed system agrees with the plan: no `sys-firmware/intel-microcode`, every guest tool of the plan installed unless the installer itself reported it as degraded, none when they are switched off | failure |
| `layout` | in a manual layout every row is mounted from the device the manifest named (the second disk's `/home`, its swap) | failure |
| `world-resolve` | `emerge -pvuDN @world` resolves: no blocker, no conflict, no change the configuration would need | failure; no sudo: finding |
| `world-drift` | nothing: the same command's upgrades, new packages and reinstalls are only counted and listed (`world_drift` in `result.json`). Right after an install they come from a tree that is newer than the stage3 (new versions, changed USE defaults), not from the installer | information: never a finding, never a failure, also under `--strict-probes` |
| `install-degraded` | the installer's final event reports no `degraded` parts (a desktop package or the NVIDIA driver that failed) and no `warnings` (a cleanup that failed after a good install). The finding carries the installer's own reasons, whole (also `install.completed` in `result.json`) | finding; a failure under `--strict-probes`: a part that did not install is a real problem |
| `dmi` | the VM shows the DMI identity the scenario asked for | failure |
| `sshd-service`, `sshd-config` | sshd runs and is enabled at boot (`rc-service sshd status` and `rc-update show default`, or `systemctl is-active` and `is-enabled`); `sudo sshd -T` says `permitrootlogin no`, no empty passwords, and a password login is possible exactly when `accounts.ssh_password_login` is true (password authentication, or keyboard-interactive with PAM, which is how Gentoo's own drop-in asks for passwords) | failure; no sudo: finding |
| `sudo`, `root-lock` | the new user can use sudo with the password; root's password is locked as asked | finding |

`--no-world-probe` skips the world probe (minutes on a desktop); `--world-timeout` bounds it.
`--fresh-nvram-check` (UEFI) boots the installed disk once more with an empty NVRAM after the
probes: it must start through `EFI/BOOT/BOOTX64.EFI` (the serial log of OVMF names the boot option),
answer SSH and, if encrypted, accept the passphrase again.

After the install the harness copies two files out of the live system, also when the install
failed: `daemon-stderr.log` (`/var/log/gentoo-installerd.log`) and `hardware-plan.json`;
`install-artifacts.json` says what was found. A completed event with `bootable: false` makes a
"successful" install a failure.

### The live medium and the rescue chroot (Wave 5, interface I13)

`scripts/vm_wave5.py` holds the commands and the parsers; the orchestration is in `ScenarioRunner` (`check_live_network`,
`check_live_remote_access`, `live_remote_and_host_keys`, `chroot_check`). The text of `gentoo-remote-access` and `gentoo-chroot`
is read tolerantly (an `ssh gentoo@ADDRESS` line, a `Password:` line, `SHA256:` fingerprints, a `Remote access (SSH): enabled`
line); a format the parsers cannot read is reported as "could not tell", never as a pass. If the iso owner words it differently,
change `STATE_LINE_RE` and friends in `vm_wave5.py` and the recorded text in `tests/vm/test_vm_wave5.py`.

* **`network_status` in every VM.** Every `boot_live` asks the live daemon for `network_status` (a short Python program that speaks the
  daemon's socket protocol; no client library is needed in the guest). The VM is wired, so the answer must be `online`, `default_route`
  and `dns` true, a connected ethernet device and no problem code. A daemon without `network.v1` (answer `proto.unsupported_action`) is
  a warning on stderr and an advisory record in `result.json` (`live.network_status`), not a failure. The live system answers SSH before the daemon has made its
  socket and before DHCP has finished, so a failed answer is asked again every 3 seconds for up to 90 seconds (`attempts` in the record) before it counts.
  An unreachable mirror is shown in the record but is not a failure (the installer cannot fix a host without internet).
* **`live-remote-access`** (use `--live-only`-style runs; the scenario implies it, with no display):
  `rc-service sshd status` must say started at boot; `gentoo-remote-access status` must show remote access off; `on` must print the
  password, an `ssh gentoo@ADDRESS` line and key fingerprints, and a password login as `gentoo` must work from the host; `off` must
  make the same password fail with "Permission denied" (any other failure, such as a refused connection, is "inconclusive" and fails);
  sshd keeps running. The host login uses plain `ssh` with `PubkeyAuthentication=no` and the password through `SSH_ASKPASS` with
  `SSH_ASKPASS_REQUIRE=force` (OpenSSH 8.4 or newer on the host, no `sshpass`): the harness key cannot answer for the password, and the
  password is on no command line and in no record (the generated password of a throw-away live medium does appear in the per-step logs
  `live-remote-NN.log`, like everything else the guest prints). The ISO is then shut down and booted a second time: the fingerprints of
  `/etc/ssh/ssh_host_*_key.pub` must differ per key type between the two boots (`live.host_keys` in `result.json`).
  Needs the **testkey ISO** (root key login for the harness) built from the tree that has `gentoo-remote-access`.
* **`rescue-chroot`**: after the install and the usual probes the installed VM is stopped, the live ISO is booted and `gentoo-chroot` (no session argument) is run
  twice with this on standard input, once as `gentoo-chroot --read-only` (no question to answer, so the check does not depend on how the tool reads its prompt)
  and once as plain `gentoo-chroot` (the first line is `y`, the answer to its one read-write question; a tool that does not ask just gets an unknown command):
  marker commands (`echo "CHROOT-FOUND-$((6*7))"`; the number is computed, so the echo of a typed command cannot pass for its output), `getent hosts distfiles.gentoo.org`,
  `emerge --info >/dev/null`, `grub-mkconfig -o /dev/null` (GRUB installs only), in the writable session `touch /root/.gentoo-vm-chroot-write`, and `exit`.
  Required: all markers, `/etc/gentoo-release` read inside, exit status 0, and after each session the cleanup check of the rescue runs
  (`vm_rescue.cleanup_command`): nothing mounted below `/mnt/rescue`, no session file left behind, no read-only flag, no `rescue-*` device-mapper volume.
  Encrypted installs are refused (the passphrase prompt is on the console). `result.json` has both sessions under `chroot.sessions`.

Commands for the lead (each needs the testkey ISO and its private key; see "Running a scenario"):

```sh
python3 scripts/test-vm.py --scenario live-remote-access --iso build/test-dist/<iso> --ssh-host 127.0.0.1 --ssh-port 40222 --ssh-private-key "$testkey" --memory 2048
python3 scripts/test-vm.py --scenario ssh-keys-only      --iso build/test-dist/<iso> --ssh-host 127.0.0.1 --ssh-port 40222 --ssh-private-key "$testkey" --no-world-probe
python3 scripts/test-vm.py --scenario rescue-chroot      --iso build/test-dist/<iso> --ssh-host 127.0.0.1 --ssh-port 40222 --ssh-private-key "$testkey" --no-world-probe
```

### Desktop sessions, the Safe graphics entry, Secure Boot (Wave 5, hardware backlog H04)

**Desktop login.** A VM cannot type into a greeter reliably, so the scenarios check what is broken when a desktop does not come up
(`scripts/vm_session.py`): `desktop-session` reads `/usr/share/xsessions` and `/usr/share/wayland-sessions` for the desktop's session
file, asks `rc-service display-manager status` or `systemctl is-active display-manager`, and looks for the greeter or X server in the
process list (Sway and i3 may have no display manager). `audio-pactl` runs `pactl info` in the test user's SSH login (it has
`/run/user/UID`): on systemd the user units start PipeWire by socket activation; on OpenRC the desktop's autostart does, which an SSH
login does not run, so the probe starts `gentoo-pipewire-launcher` itself (that proves the packages, not the autostart). The table of
desktops in `vm_session.DESKTOPS` is checked against the tree by `tests/hardware/test_tree_evidence.py`; the backend's desktop registry
(B14) will replace it.

**Safe graphics.** `safe-graphics-*` install with the NVIDIA driver requested. The harness lists the installed boot menu (GRUB:
`/boot/grub/grub.cfg`; systemd-boot: `loader/entries/*.conf`; EFI stub: `efibootmgr`), finds the entry titled "Safe graphics", checks its
own arguments (`nomodeset`, `module_blacklist=` with nvidia, nvidia_drm, nvidia_modeset, nvidia_uvm, nouveau), chooses it for one boot
(`grub-reboot`, `bootctl set-oneshot`, `efibootmgr --bootnext`), restarts the VM and requires the arguments in `/proc/cmdline` and none of
those modules in `/proc/modules`. GRUB needs `GRUB_DEFAULT=saved` for `grub-reboot` to work; if the installer's GRUB configuration does
not set it, the scenario fails at the one-shot step, which is a finding for B05. The VM has no NVIDIA card, so this proves the entry
and the boot, not NVIDIA (real hardware only).

**Secure Boot and out-of-tree modules** (`secure-boot-modules`; evidence for B07 step 2). Prepare once (the key is test-only and not a secret):

```sh
mkdir -p build/sb-test && cd build/sb-test
openssl req -new -x509 -newkey rsa:2048 -nodes -days 3650 -sha256 -subj "/CN=Gentoo installer VM test key (test only, not a secret)" -keyout db.key -out db.crt
# a copy of OVMF's variable store with db.crt as PK, KEK and db (python-virt-firmware; check `virt-fw-vars --help` for your version):
virt-fw-vars --input /usr/share/edk2-ovmf/OVMF_VARS.fd --output vars.fd --set-pk 00000000-0000-0000-0000-000000000000 db.crt \
  --add-kek 00000000-0000-0000-0000-000000000000 db.crt --add-db 00000000-0000-0000-0000-000000000000 db.crt --secure-boot
cd ../..
python3 scripts/test-vm.py --scenario secure-boot-modules --iso build/test-dist/<iso> --ssh-host 127.0.0.1 --ssh-port 40222 \
  --ssh-private-key "$testkey" --sb-key build/sb-test/db.key --sb-cert build/sb-test/db.crt --ovmf-vars-file build/sb-test/vars.fd \
  --companions none --no-world-probe --memory 4096
```

`vm_secureboot.key_generation_command` and `enrol_command` are those two calls. The harness uploads the pair to the live system
(`/run/gentoo-vm-secure-boot/`, the manifest names the paths, the daemon reads them there), installs systemd-boot with a UKI signed by it, and boots
under the Secure Boot build of OVMF (`OVMF_CODE.secboot.fd`, SMM on). In the installed system, as root, a Python program builds a
dummy module against `/lib/modules/$(uname -r)/build`, loads it unsigned, signs it with the same key (`scripts/sign-file`) and loads it
again, and reports `SecureBoot`, the lockdown mode, `module.sig_enforce`, the kernel keyrings, the kernel messages and the taint flags.
The verdict goes to `result.json` (`secure_boot_modules`) and to the console line `SECURE BOOT ...`:
`unsigned-modules-load` (B07 step 2 may allow NVIDIA with a warning, still refusing when lockdown is on), `unsigned-modules-refused`
(keep refusing), `only-signed-modules-load` (keep refusing in Guided mode, a signed path is possible), or `inconclusive` (Secure Boot was not
active, or the kernel has no build tree: the scenario fails, nothing is decided). The test certificate is the owner of this variable
store; no Microsoft key and no shim are involved, matching `manifest.py` (Secure Boot only with systemd-boot or the EFI stub and a UKI).
I could not run any of this where it was written: the first real run may need small corrections to the `virt-fw-vars` call or to the
dummy module build (the report says which step failed).

### Screenshots of the live desktop and of every installer page

```sh
python3 scripts/test-vm.py --scenario screenshots --iso build/test-dist/<iso> --ssh-host 127.0.0.1 --ssh-port 40222 \
  --ssh-private-key "$testkey" --memory 4096 --screenshots build/screenshots/$(date +%Y%m%d)
```

The scenario boots the live ISO once per display size (`--screenshot-sizes`, default `1366x768,1920x1080`) on a VM whose graphics card announces exactly that
size (`--resolution`), waits for the XFCE session (`--gui-timeout`) and the installer window that starts by itself (`--screenshot-settle`), and then:

1. takes a picture of the whole screen through QMP (what a person sees after the ISO has booted), converted from PPM to PNG without any tool on the host:
   `<size>/live-desktop.png`;
2. uploads `scripts/vm-gui-shots.py` and runs it as the live user in the live X session. It opens the real installer window, shows each page of
   `window.pages` (so a page added later is photographed too) with the mode switch in each position, waits for the page to settle and saves
   `<size>/page-NN-<page>-guided.png` and `...-advanced.png`; a page with a scroll bar also gets `...-full.png`, the whole content in one tall picture. It never
   presses Continue and never starts an installation; the pages are shown on a default manifest. The pictures come back as one tar archive that the host unpacks
   with checks (plain files named `*.png` or `*.json` only, size limits).

The run fails (and says why) when the guest's screen is not the size asked for (the picture is then useless as evidence for that size), when a mode or too
many pages are missing, or when the installer reports that a page needs horizontal scrolling or the window is larger than the screen (1366x768 is a real laptop).
`index.json` in the screenshot directory lists what was taken per size. The XFCE look of the live desktop (`wave5-xfce-look.md`) is judged on these pictures.
The guest program runs offscreen in the test suite (`tests/vm/test_vm_shots.py`), so its output format is tested; the real X session was not available where it was written.

### Real-hardware checklist for the user's PC (hardware backlog H05)

`scripts/hardware-checklist.py` is for the person who owns the machine, run by hand, in the installed system's KDE Wayland session, as the normal user:

```sh
python3 scripts/hardware-checklist.py              # a report on the screen
python3 scripts/hardware-checklist.py --markdown   # a table for the release notes
python3 scripts/hardware-checklist.py --json       # for a bug report
```

It is **read-only by construction** and the test suite proves it (`tests/vm/test_hardware_checklist.py`): it starts only `nvidia-smi`, `vulkaninfo`, `lsusb`,
`loginctl show-session`, `ss -ltn`, `pgrep -x`, `systemctl is-active`, `rc-service NAME status`, `efibootmgr` (no arguments), `journalctl -k` and `df`, each in
exactly one form that cannot change anything; it reads only below `/proc`, `/sys`, `/boot`, `/efi`, `/etc/gentoo-live-installer` and the udev rules directories;
it has no shell, no `sudo` and no write call. It does not start OpenRGB (that scans the SMBus). Checks and what each needs: the GPU is an RTX 4090 with a
595 driver and 24 GiB (`nvidia-smi`), `nvidia_drm` has `modeset=Y`, the kernel modules are the open ones, the session is Wayland, Vulkan lists the NVIDIA GPU, the
kernel log has no `NVRM`/`Xid` lines, `PreserveVideoMemoryAllocations` is on and `/var/tmp` has room for 24 GiB (suspend), the boot menu lists Safe graphics (GRUB
`grub.cfg`, systemd-boot entries or `efibootmgr`), the Razer Viper (1532:0091) is bound to `razermouse` with `openrazer-daemon` running, the Aura controller
(0b05:18f3) is present with OpenRGB's udev rules, the DualSense (054c:0ce6) has an input node, sshd runs and listens on port 22. It *records* Secure Boot, the
lockdown mode, the kernel taint and the install receipt. Statuses: PASS, FAIL (report it), RECORD, SKIP (not applicable or not readable by this user), MANUAL.
The two MANUAL steps are printed with their commands: suspend and resume with the video memory full, and the fixture re-capture
(`scripts/capture-hardware-fixture.py`), plus "log in over SSH from another PC" and "press a button on the controller". Put the report and the manual results
into the release notes; that is the acceptance of H05. The expectations (RTX 4090, branch 595, the three USB ids) are in `EXPECTED` at the top of the script.

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
| `--smbios` | `qemu`, `laptop-lenovo`, `laptop-dell`, `laptop-hp`, `desktop-asus`, `laptop-asus-rog`, `laptop-razer-blade`, `laptop-lenovo-legion`, `vmware`, `virtualbox`, `hyperv` (the Razer strings are modelled on public reports, not read from a real Blade) |
| `--extra-disk SIZE` | another empty raw disk (`256M`, `2G`); repeatable |
| `--secure-boot` | OVMF's Secure Boot build (setup mode unless `--ovmf-vars-file` enrols keys) |
| `--ovmf-vars` | `persist` (default), `fresh-on-target`, `readonly` |
| `--ovmf-vars-file` | seed the NVRAM from a file (a kept `OVMF_VARS.fd`, or one with enrolled keys) |
| `--resolution` | the display size the guest sees (`1366x768`; virtio, std or qxl adapter): the graphics card announces it in its EDID |
| `--screenshots`, `--screenshot-sizes`, `--screenshot-settle` | `screenshots` scenario only: where to keep the pictures (default `build/vm/<run>/screenshots`), the display sizes (default `1366x768,1920x1080`) and the seconds the live session gets to draw its desktop (default 15) |
| `--sb-key`, `--sb-cert` | `secure-boot-modules` only: the test-only key that signs the UKI and the dummy module, and its certificate (see below) |
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
and run, so no real disk can match by accident; for an image the image's own size); the model,
transport, sector size and removable flag the emulated bus must report; the PCI vendor of its
controller; the serial number when it has one; a per-run nonce the VM carries in its SMBIOS OEM
strings; and that no other disk is attached (besides the CD-ROM and the extra disks the scenario
asked for). Extra disks have a role: `data-N` (the install writes to it, as in
`uefi-manual-two-disks`) or `decoy-N` (it must survive). It runs before the install, before a
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
the method, the matched text and a hash of the serial log (`luks-unlock-receipt-fresh.json` for
the second boot of `--fresh-nvram-check`).

### GUI mode and detection

`--install-mode gui` drives the real wizard with `scripts/vm-gui-install.py`. With
`--gui-detect` (the default for the `defaults` scenario, `--no-gui-detect` turns it off) the
driver starts from the wizard's own defaults plus only what a person would type, selects the disk
like a user, and requires the final manifest to equal the expected one: firmware, partition table
(when the scenario fixes it) and disk identity must be what the GUI detected. A BIOS machine
whose wizard offers UEFI fails with `storage.firmware: the GUI chose 'uefi', expected 'bios'`.
In detect mode the wizard also seeds some fields from the hardware report: the swap size from the
memory, no Bluetooth without a radio, no microcode in a virtual machine, and the companions (a
machine without the memory for PySide may not get HWMonux). The plan records these fields and does
not assert them. The harness then takes the wizard's values over into the saved `manifest.json`, so
that the probes judge what was really asked for; what the harness itself expected stays in
`manifest-expected.json`, and `install.wizard_choices` in `result.json` lists every difference (a
machine with enough memory, like the 12 GiB of the `defaults` job, is expected to keep both companions,
and the list shows when it did not).
The driver stops itself with status 2 when a page does not advance (`--navigation-timeout`) or the
backend is silent (`--idle-timeout`), and retries a slow network (`--readiness-timeout`) but never
an identity problem.

### What a run leaves behind

`build/vm/<scenario>-<time>-<id>/` holds `result.json` (verdict, probes, `advisory_findings`,
`world_drift` with the counts and the packages, `luks_unlock`, `decoys`, `rescue`, `hardware`,
`disk`, `install`, `boot_entry`, `fresh_nvram`), `manifest.json` (`manifest-expected.json`
beside it when a detect-mode wizard chose other values), the logs (`install.log`, `qemu-*.log`, `serial-*.log` with one pair per
boot: `serial-live.log`, `serial-target.log`, ...), `ssh-*-wait.log`, `verify-*.log`,
`rescue-*.log` and the rescue audit logs, screenshots on failure (`failure-*.ppm`,
`broken-boot.ppm`), `*-disk-guard.json`, `daemon-stderr.log`,
`hardware-plan.json`, and the event streams as `events.jsonl`, `resume-events.jsonl` and
`cancel-events.jsonl` (the result files hold a summary only, so they stay small). Failed runs keep
the disk and `OVMF_VARS.fd`; so does `--keep`, and so does every rescue run. A matrix run adds
`build/vm/matrix-wave3-<time>-<id>/` with one log per job and `matrix-result.json`.

## Testing the rescue tools

`gentoo-rescue` and the rescue GUI are separate components; the harness provides the broken
system and judges the result. The system that is broken is a Gentoo that the harness installs
itself (or a kept disk of one, `--target-image`). The tool repairs Gentoo only, so there is no
scenario for another distribution: a disk that holds one is listed by a scan as "Not a Gentoo
system - this repair tool only supports Gentoo" and refused by `mount` (exit status 7), which
`tests/rescue/test_gentoo_only.py` and the driver test in `tests/vm/test_vm_rescue_driver.py` check
against the real command line.

### What the harness does with `gentoo-rescue`

With `--rescue-repair` (or a `rescue-*` scenario) the harness repairs without a person, one
command at a time, as root over SSH, and keeps every command's output in `rescue-<damage>-*.log`:

| Step | Command | Required |
|---|---|---|
| find | `gentoo-rescue --json scan` | exit 0; exactly one Gentoo on the guarded disk; the live medium is not offered |
| mount | `gentoo-rescue --json mount <id> --yes` | exit 0; a session that is mounted read-only |
| diagnose | `gentoo-rescue --json diagnose <session>` | the damage shows: `D05` for a deleted menu file, `D02`/`D03` for a missing initramfs, `D07` for a wrong fstab |
| plan | `gentoo-rescue --json plan <session> repair.recommended` | no failed precondition; a command that repairs the damage (`grub-mkconfig`, `grub-install`, `dracut`, a write of `/etc/fstab`) |
| run | `gentoo-rescue --json run <session> repair.recommended --confirm-plan <digest> [--typed <device name>] [--i-have-a-backup]` | exit 0, outcome `success`, every post-check ok; exactly the plan that was shown, with the confirmation its risk asks for |
| check | a read of the mounted result | at least one boot menu file, and none of `/boot/grub*/grub.cfg`, `/boot/loader/entries/*.conf`, the ESP's `grub.cfg`, `/etc/kernel/cmdline`, `/etc/default/grub` contains `root=live:`, `rd.live.`, `CDLABEL=` or `cdroot` |
| audit, unmount | `gentoo-rescue --json audit <session> --text`, `... unmount <session>` | no mounts left; afterwards nothing under `/mnt/rescue`, no session state, no read-only partition of the disk, no `rescue-*` mapping |

`--rescue-action` changes the action (default `repair.recommended`), `--rescue-transport` says how
the command reaches its engine: `auto` (the daemon when it answers, as for a person), `daemon`,
`local` (`--local`, in-process). The exit statuses of `gentoo-rescue` are 0 ok, 1 failed, 2
usage, 3 refused by the safety policy, 4 needs confirmation, 5 not found, 6 busy, 7 unsupported,
130 interrupted; the harness names them in its errors.

### Gentoo: `--rescue-break` and `rescue-gentoo`

```sh
# 1. Install, verify it boots, then damage it from the live ISO and prove it no longer boots.
test-vm.py --scenario bios --rescue-break grub-cfg ...        # or initramfs, fstab-uuid
# 2. Repair it with gentoo-rescue, boot it again, run every probe again (all in one run).
test-vm.py --scenario bios --rescue-break initramfs --rescue-repair ...
# 3. Or with a command of your own, as root in the live system ({device} is the guarded disk).
test-vm.py --scenario bios --rescue-break initramfs --rescue-command 'my-repair-script {device}' ...
# 4. Several damages one after the other (each repaired and verified before the next).
test-vm.py --scenario openrc --rescue-break grub-cfg,initramfs,fstab-uuid --rescue-repair ...
# 5. Repair a kept disk later (the image is never written; changes go to an overlay).
test-vm.py --scenario bios --target-image build/vm/<run>/disk.qcow2 \
  --rescue-repair --rescue-break grub-cfg --ovmf-vars-file build/vm/<run>/OVMF_VARS.fd ...
```

`--rescue-break` removes `grub.cfg` (also `boot/grub2/`), every `initramfs*`/`initrd*` image, or
replaces the root device in `/etc/fstab` (`UUID=`, `LABEL=` or `PARTUUID=`) with
`UUID=00000000-dead-beef-0000-000000000000`. It works on files of a mounted root only: nothing is
formatted, repartitioned or wiped; a separate `/boot` that the system's own fstab names is mounted
too (a partition of the disk, or a btrfs subvolume of the root file system with `-o subvol=NAME`:
a layout can keep the kernel, initramfs, `grub.cfg` and boot entries in a subvolume `boot`, not in
the `/boot` directory of the subvolume `root`), and a system inside a btrfs subvolume is found.
LUKS, LVM and RAID roots are
reported as unsupported. The damage must be *real*: the installed disk has `--broken-timeout`
seconds (default 120) to come up and a system that still boots is a failed test. The repair may run
for `--rescue-timeout` seconds (default 1800). For the fstab, whose damage does not always stop the
boot, the proof is `findmnt --verify` in the live system. A screenshot and the serial excerpt are
kept (`broken-boot.ppm`, `broken-boot-serial.txt`). `rescue-gentoo` is `--rescue-break
grub-cfg,initramfs,fstab-uuid --rescue-repair` on a UEFI OpenRC install; `result.json` has the
evidence of every round under `rescue.cycles`.

`--target-image` accepts any qcow2 or raw image of a Gentoo, provided
you supply the manifest its probes need (`--target-manifest`, default `manifest.json` beside the
image) and the image has an ext4, XFS or Btrfs root that the breaker can mount. It does not install
anything; the repair and everything after it run in the live system, and the base image's size,
modification time and the checksum of its first MiB are checked afterwards.

## The Wave 3 matrix

`test-vm.py --matrix wave3` runs the acceptance matrix: one child `test-vm.py` per entry, each
with its own SSH port (counted up from `--ssh-port`, or from 40222 when none is named, one per
running job), its own signal handling and its own QEMU. **At most two VMs run at the same time**,
in the order of the table below (the long installs first), and a job starts only when the memory of all running VMs plus its own stays within
`--matrix-memory-budget` (default 24576 MiB) and the host has that much `MemAvailable` plus 3 GiB.
A job that cannot ever fit is skipped with the reason. Before the first job starts, the matrix
checks what the last one needs: the SSH ports of all slots and the boot menu
entries of the ISO; a missing input is a usage error (exit status 2), not a failure hours later.
Every job gets `--strict-probes`; UEFI installs also get `--fresh-nvram-check`. Options of the
caller (ISO, SSH key, timeouts) are passed to every job.

| Entry | Scenario and options | Companions | RAM | CPUs | Minutes |
|---|---|---|---|---|---|
| `defaults` | `defaults`: KDE, UEFI, a virtio disk **without** a serial number (path identity), binhost; GUI install; `--disk-size 64 --build-jobs 1 --swap-mib 4096` | HWMonux and PortageHub | 12 GiB | 4 | 150 |
| `rescue-gentoo` | `rescue-gentoo` | PortageHub | 4 GiB | 2 | 70 |
| `uefi-nvme-luks-openrc` | `uefi-nvme-luks-openrc` | PortageHub | 4 GiB | 2 | 40 |
| `systemd` | `systemd`: re-acceptance, the guided erase sequence changed in Wave 1 | PortageHub | 4 GiB | 2 | 30 |
| `openrc` | `openrc`: re-acceptance | PortageHub | 4 GiB | 2 | 30 |
| `uefi-luks` | `uefi-luks`: re-acceptance | PortageHub | 4 GiB | 2 | 35 |
| `uefi-luks-lvm` | `uefi-luks-lvm`: LUKS with LVM inside, a combination that no VM has booted yet | PortageHub | 4 GiB | 2 | 40 |
| `uefi-manual-two-disks` | `uefi-manual-two-disks` | PortageHub | 4 GiB | 2 | 35 |
| `bios` | `bios`: re-acceptance; the boot flag of the disk is read | PortageHub | 4 GiB | 2 | 30 |
| `bios-sata-minimal` | `bios-sata-minimal`; the boot flag of the disk is read | PortageHub | 4 GiB | 2 | 30 |
| `text-install` | `text-install` | PortageHub | 4 GiB | 2 | 30 |

**Companions.** Only the `defaults` entry installs HWMonux. It is the entry that has the memory for
PySide (12 GiB, one build job, 4096 MiB of swap, which is also what the installer's guided defaults
give a person), and its point is "the untouched wizard installs everything it offers". Every other
install entry runs with `--companions portagehub`: HWMonux does not depend on the disk, the firmware
or the network card that the entry is there to vary, and PySide would put an hour of compiling, and
a memory limit that a 4 GiB VM does not meet, into every one of them. They still install PortageHub,
and `disabled-hwmonux-absent` proves that the switched-off companion is not installed. The
alternative, 6 GiB for every entry, would fit the budget (12 + 6 GiB of 24) but is still a guess for
PySide's large compile jobs and keeps the hour in each job. A `--companions` given to `--matrix`
reaches only the `defaults` job (the other entries name their own). The minutes are estimates of an install
without PySide, from the stage times of the first real runs: about 10 minutes for the base system, a
few more for PortageHub, and the boots and probes.

The matrix needs an ISO built from the merged tree: `text-install` asks the boot menu for the
text console entry, and the check before the first job stops the whole matrix with exit status 2
when the menu has none (`--matrix-only` leaves such an entry out).

The minutes are estimates for a machine with KVM. The first real `defaults` run stopped at the disk
page because the driver did not tick the approval that the wizard asks for a disk without a serial
number; the driver now ticks it, like a person. 12 GiB + 4 GiB + the margin is the most that ever runs.

```sh
test-vm.py --list-matrix                                    # the table above, from the data
test-vm.py --matrix wave3 --iso build/test-dist/<iso> --ssh-host 127.0.0.1 --ssh-port 40222 \
  --ssh-private-key "$testkey"                              # all of it
test-vm.py --matrix wave3 --matrix-only rescue-gentoo,bios ...             # a part of it
test-vm.py --matrix wave3 --matrix-parallel 1 ...                          # one at a time
```

`matrix-result.json` in the matrix directory lists, per job, the command line, the exit status
(0 pass, 1 fail, 2 usage or preflight error, 128+N killed), the run directory and the time; the
exit status of the matrix is 0 only when every job passed. Interrupting it asks every child to
clean up its own QEMU. `--matrix-parallel` cannot exceed 2.

## Known gaps

Read these before trusting, or blaming, a result.

* **Little outside the original four scenarios has run on real QEMU.** The first Wave 3 matrix runs
  (OpenRC with LUKS on NVMe, OpenRC, the install part of `rescue-gentoo`) installed a system that
  booted and passed the basic probes. They failed on the HWMonux build (PySide ran out of memory, see
  "Companions" above), on a check that read the daemon's plan file wrongly and on a drift of the
  Portage world that the installer did not cause (the harness reads the plan record now, and the drift
  is information). The new scenarios, the
  hardware options, `--live-only`, the rescue flow and the rest of the Wave 3 matrix are tested
  as argument lists and against the fake lab. The first real runs are also where QEMU and the
  guests confirm what the harness assumes and the fake lab cannot know: that `-smbios file=`
  replaces QEMU's own enclosure record (the `dmi` probe of `laptop-smbios` checks `chassis_type`),
  that USB with 4096-byte sectors works, that `-kernel`/`-initrd` boots an ISO entry under OVMF and
  SeaBIOS, and the exact
  output of `gentoo-rescue` and `gentoo-installer-text` on the live ISO of the same tree (the
  harness is tested against the rescue tool's laboratory, not against a live ISO). QEMU's IDE/SATA
  disks are 512-byte only, so `--disk-bus sata --sector-size 4096` is refused.
* **LUKS unlock over serial needs the installed system to honour `system.kernel_cmdline_extra`**
  (`configure.py` must put it on `/etc/kernel/cmdline` and `GRUB_CMDLINE_LINUX`). Until it does,
  `uefi-luks` and `openrc-luks` time out waiting for the prompt, with a message saying so.
* **`serial-less` fails until the daemon accepts `device_id_kind=path`.**
* **`--gui-detect` on a BIOS machine fails until the wizard seeds the firmware from detection**
  (the unit test in `tests/vm/test_vm_gui_driver.py` is an expected failure that starts passing then).
* **The hardware plan is read from `/var/lib/gentoo-installer/hardware-plan.json`** (the daemon's
  state directory), from the `plan` key of the record the daemon writes there. A missing file, or one
  without a plan, is a finding; with `--strict-probes` (the matrix) it fails the run.
* **`holders` (an active LVM or md stack on the target) and `uefi-luks-all` are not scenarios yet:**
  the first needs a consent box the manifest cannot carry, the second a settled layout. LUKS, LVM and
  RAID roots cannot be broken yet.
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
* **`--boot-entry` says no entry matches**: `--list-boot-entries --iso FILE` shows the titles the
  ISO's menu really has.
* **`check.sh --full` fails in pkgcheck**: the findings are real overlay problems (for example an
  ebuild USE flag that does not exist); fix the ebuild, do not weaken the check.
* **`uefi-luks` and friends hang at the passphrase prompt**: read `serial-target.log` and
  `luks-unlock-receipt.json`; if the kernel never printed to ttyS0 the installed system ignored
  `system.kernel_cmdline_extra`.
* **"The SSH connection to the live system was lost (exit 255) during the desktop stage"**: the guest
  stopped answering (SSH waits 30 minutes of silence before it gives up). A guest that runs out of
  memory thrashes instead of failing; the usual cause is a compile (PySide, for HWMonux). Read
  `serial-live.log` and `qemu-live.log`, and see the memory rule under "Running a scenario".
* **A matrix job was skipped for memory**: the note names the numbers; free memory or lower
  `--matrix-memory-budget` only if you know what else runs.

## For the maintainers: tools that depend on the harness

`build/*.py` (untracked) load `scripts/test-vm.py`. `tests/vm/test_vm_public_api.py` pins the
names, signatures and command-line fragments they use; change them only together. The acceptance
generators read `result.json` keys and artifacts (also pinned there). When LUKS acceptance is
automated, the generators should accept `luks-unlock-receipt.json` instead of the operator's
`luks-root-observation.json`.

The harness is `scripts/test-vm.py` plus small modules next to it: `scripts/vm_guard.py` (the disk
guard), `scripts/vm_hw.py` (the machine shape), `scripts/vm_guest.py` (programs run in the live system),
`scripts/vm_probes.py` (what the probes accept), `scripts/vm_rescue.py` (the rescue driver and the
checks around it), `scripts/vm_iso.py` (the ISO's boot menu) and `scripts/vm_matrix.py` (the matrix and its
scheduler).
