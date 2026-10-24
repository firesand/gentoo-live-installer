# The ISO build

How the installer and rescue disc is made, what is checked, and how to change it safely. The user's side is in [USER-GUIDE.md](USER-GUIDE.md).

A Catalyst build takes hours, so everything that changes the image is collected in one set of files, rendered from one place and checked at the end by `scripts/verify-iso.py`. Nothing in this document claims a real-hardware boot: the checks are static, plus QEMU runs by the VM harness.

## 1. Pipeline

```
scripts/build-iso.sh  (host)                       iso/builder-entrypoint.sh  (container)
 validates inputs and free space                     reproducible source archives (.tar.zst)
 builds the pinned builder image  ─────────────►     overlay + Manifests, rendered specs
 docker run --name gentoo-iso-<hash>                 stage1 decision (iso/stage_reuse.py)
   log, signals, OOM hint                            stage3 seed + Gentoo commit: signatures checked
 checks the published files                          Catalyst stage1  ─►  receipt
                                                     Catalyst stage2  ─►  ISO
                                                     scripts/verify-iso.py  ─►  publish + manifest
```

1. **Builder image** (`iso/Containerfile`): a pinned `gentoo/stage3` image with Catalyst, git, rust-bin, the Gentoo OpenPGP keyrings and `xorriso`. The Gentoo tree in it is first bootstrapped from a GitHub tarball (only so that Portage can install the tools above), then replaced by the exact commit of `iso/pins.env`, fetched with git and accepted only when it carries a good signature of the key named by `GENTOO_TREEISH_SIGNER_FPR`. The keyrings come from the bootstrap tree, so this protects against a changed commit or a swapped signer, not against a compromised bootstrap; the pin is the reviewed decision.
2. **Source archives**: `gentoo-live-installer`, `hwmonux` and `portagehub` are packed reproducibly (sorted, fixed mtime) into `.tar.zst` distfiles. Their names and versions come from the ebuild file names in `packaging/gentoo-overlay`; nothing else repeats a version. `ebuild ... manifest` writes the Manifests; the same overlay is shipped inside the image for the target systems.
3. **Rendering** (`iso/render-config.py`): the specs, `portage/make.conf` (+ `make.conf.extra`), `README-live.txt` and everything below `iso/cd_overlay` come from one token map. Tokens: `@BUILD_DIR@ @TREEISH@ @VERSION_STAMP@ @STAGE3_BASENAME@ @ISO_NAME@ @VOLID@ @BOOTARGS@ @JOBS@ @LOAD_AVERAGE@`; an unexpanded token is an error. `ISO_VOLID` and `ISO_BOOTARGS` in `iso/pins.env` are the only source of the volume label and the kernel arguments, for `livecd/volid`, `livecd/bootargs` and every `linux` line of `grub.cfg`.
4. **Overlay ownership** (`iso/lib/overlay.sh`): inputs are copied without the builder's owner, then chowned to root with directories 0755, executables 0755 and other files 0644. Without this the image carried the build user's uid (1000, which is the live user) on `/etc` and the whole overlay.
5. **Stage1/stage2**: Catalyst runs through `iso/catalyst-no-loop.py`, which uses a verified directory snapshot instead of a loop-mounted squashfs, bounds ninja, and appends the settings from `iso/make_conf_keys.py` to Catalyst's two `make.conf` rewrites. The build chroot gets all of them; the rewrite that ends in the image gets only `LIVE_KEYS` (and `FEATURES` without `buildpkg`), and Catalyst's builder-only `DISTDIR`/`PKGDIR` lines are removed. A job count is therefore never frozen into somebody's machine; livecd-tools' `autoconfig` writes `--jobs=$(nproc)` at boot.
6. **Verification and publishing**: `scripts/verify-iso.py` must pass before the ISO is copied to `/dist`; then `.sha512`, `.blake2b`, `.packages.txt` and `.manifest.json` are written.

Catalyst 4.2 facts the design relies on (checked in `catalyst/targets/livecd_stage2.py`, `base/stagebase.py` and `/usr/share/catalyst/targets/`):

* `livecd/overlay`, `livecd/readme`, `livecd/rm`, `livecd/unmerge`, `livecd/rcadd`, `livecd/volid`, `livecd/bootargs` are valid `livecd-stage2` keys.
* Order: `build_kernel`, `bootloader` (writes `boot/grub/grub.cfg`, copies `/boot/memtest86plus/*` and the README to the ISO tree), `preclean`, `livecd_update` (writes `/etc/motd`, `make.conf.old`), `root_overlay`, `fsscript`, `rcupdate`, `unmerge`; then `remove` (`livecd/rm`), `empty`, `clean` (rewrites `make.conf`), `target_setup` (makes `image.squashfs`), **`setup_overlay`** (`rsync -a livecd/overlay/` onto the ISO tree, so our `grub.cfg` replaces Catalyst's), `create_iso` (`grub-mkrescue`). `root_overlay` therefore replaces `/etc/motd`; the live `make.conf` can only be fixed by the wrapper because `clean` runs after the fsscript.
* `rc-update.sh` exits 0 even when every `rc-update add` fails, so `verify-iso.py` checks the runlevel links and init scripts itself.
* The spec parser splits **every** line that contains a colon into key and value, strips `#` comments, and treats `%` as interpolation; list entries and values must not contain `:` or `%` (`tests/iso/test_specs.py`).

## 2. `scripts/build-iso.sh`

| Option / variable | Meaning |
|---|---|
| `--check` | Validate tools, pins, inputs, free space; start nothing, create nothing. |
| `--reuse auto` (default) | Reuse the previous stage1 when nothing that goes into it changed; reinstall only changed project packages when only those changed; else build everything. |
| `--reuse-stage1` | Same as `--reuse auto`. |
| `--reuse none` | Always build everything. Use it for releases when in doubt. |
| `--stage2-only`, `--reuse stage1` | Run only stage2; fails unless the receipt matches. |
| `--stage1-only` | Build stage1 and its receipt, no ISO. |
| `--test-key-file FILE` | Build a **TESTKEY** image with this OpenSSH public key as root login. Requires `ISO_DIST_DIR`, never `dist/`. `GENTOO_TEST_SSH_PUBLIC_KEY` does the same. |
| `--verify ISO [--flavor release\|testkey]` | Run `scripts/verify-iso.py` on an existing ISO. |
| `--sign-key KEYID` | After a successful release build, write detached, armoured OpenPGP signatures `<iso>.sha512.asc` and `<iso>.manifest.json.asc` with your key (16 or 40 hex digits) and check them. This runs on the host after the container has exited, so the key never enters Docker; the checksum file names the ISO hash, so it covers the ISO. Refused for a test-key build. |
| `--clean`, `--clean-packages` | Remove chroots, `*.partial` files and stage1 archives beyond `ISO_KEEP_STAGE1` (default 2); the second also empties `catalyst/packages`. |
| `JOBS`, `LOAD_AVERAGE` | Parallelism (default 2 jobs, load average 2x). |
| `BUILD_MEMORY` | Container memory and swap limit, default `8g` (`^[1-9][0-9]*[mMgG]$`); a warning if the host has less than that plus 2 GiB available. |
| `BUILD_MIN_FREE_GIB` (45), `ISO_DIST_MIN_FREE_GIB` (6), `DOCKER_MIN_FREE_GIB` (10) | Free space on the cache, output and Docker storage filesystems; filesystems are summed per device. |
| `ISO_CACHE_DIR`, `ISO_DIST_DIR`, `ISO_LOG_DIR` | Default `.cache/iso`, `dist`, `<dist>/logs`. |
| `ISO_SECCOMP` | `unconfined` (default) or `default`. Catalyst needs `mount(2)`/`chroot(2)`; try `default` and report if a build works. AppArmor is set to unconfined only where Docker reports it. |

Inside the container, `iso/builder-entrypoint.sh` is driven by the variables the script passes with `--env`: `BUILD_MODE` (`full`, `auto`, `reseed`, `stage1-only` or `stage2-only`, chosen from `--reuse` and the receipt), `ISO_FLAVOR` (`release` or `testkey`), `JOBS`, `LOAD_AVERAGE`, `ISO_KEEP_STAGE1` and the host user's `HOST_UID`/`HOST_GID` (the outputs are handed back to that user, and the verifier looks for image files that carry those ids). `PROC_MEMINFO` only exists for the tests of the memory check. Do not set these by hand; use the options above.

What the script guarantees: one build per cache (`flock`), a container named `gentoo-iso-<12 hex of the cache path>` that is stopped on Ctrl-C or SIGTERM (`docker stop --time 120`, exit 130 or 143), a timestamped log with the path printed first, the last 40 lines on failure, the last `Emerging (` line and a `BUILD_MEMORY`/`JOBS` hint when Docker says the container was OOM-killed, outputs handed back to the invoking user, and no host block devices. Inside, `iso/lib/run.sh` makes Catalyst a child that receives the signal.

Disk use measured on the r19 build: Catalyst store 40 GiB (builds 23 GiB with five stage1 archives of 2.9 GiB, chroots 15 GiB, binary packages 1.4 GiB, snapshots 0.5 GiB), distfiles 1.7 GiB, cargo 0.6 GiB, plus the 2.8 GiB ISO and Docker's image layers. `--clean` and `ISO_KEEP_STAGE1` keep that bounded. The binary package cache that matters is `catalyst/packages/23.0-default/` (Catalyst keys it per target); the former separate `binpkgs` directory was never used and is gone.

### Flavours

| | release | testkey |
|---|---|---|
| ISO file | `gentoo-installer-amd64-<stamp>.iso` | `gentoo-installer-amd64-<stamp>-TESTKEY.iso` |
| Volume label | `GENTOO_INSTALLER_AMD64` | `GENTOO_INSTALLER_TESTKEY` |
| Root | locked (`!` in `/etc/shadow`) | locked password, key-only SSH from `root/.ssh/authorized_keys`, `sshd` in the default runlevel |
| Banner | none | "TEST BUILD ..." on `/etc/issue` and `/etc/motd` |
| `verify-iso.py` | refuses any of the four test artifacts, requires a locked root | requires all of them |

The names are derived in `iso/lib/naming.sh` (used by the script and the container) and mirrored in `verify-iso.py`; a test keeps them equal. A boot-menu volume label only has to match between `livecd/volid` and the `root=live:CDLABEL=` argument, so nothing else in the repository depends on the label.

## 3. Reusing stage1 (`iso/stage_reuse.py`)

After every stage1 the builder writes `<storedir>/builds/23.0-default/livecd-stage1-amd64-<stamp>.tar.gz.receipt.json` (schema 1): archive name, size and SHA-512, Catalyst version, builder image id, `stage1_key`, the SHA-256 of each project source archive, and the name plus tree hash of each project ebuild directory.

* `stage1_key` hashes `iso/specs/livecd-stage1.spec`, everything in `iso/portage`, `iso/pins.env`, the wrapper, `make_conf_keys.py`, the `Containerfile`, the overlay's `acct-group`, `metadata` and `profiles`, and the constant `STAGE1_FORMAT` in `stage_reuse.py`. **Bump `STAGE1_FORMAT` when you change how `builder-entrypoint.sh` drives stage1.** Templates are hashed unrendered, so `JOBS` never invalidates a stage1. The stage2 spec, overlay, fsscript, menu and README do not belong to the key.
* **reuse**: key, Catalyst version and archive SHA-512 equal, and no project package changed: run stage2 only, from the archive (`options = ["pkgcache"]`, never `seedcache`, so a retained chroot cannot win over the verified archive).
* **reseed**: only project packages changed. The old archive is copied to `...-completed.tar.gz`, a stage1 spec with that `source_subpath` is run with `--reinstall-atoms=<each changed package>` added to the emerge options, the copy is removed, the receipt is rewritten, then stage2 runs. This needs no revision bump: a changed source archive, ebuild or `files/` directory is reinstalled by name. Reseed never removes packages, which is why any change of the spec or the Portage configuration forces a fresh stage1.
* **fresh**: anything else, including a missing, symlinked or tampered archive, an unknown receipt schema or a different Catalyst.

`--reinstall-atoms` is documented Portage behaviour but has not yet been run through a real reseed here: build once with `--reuse none` and once after a source edit, and compare `manifest.json` (`reused_stage1`, `stage1_mode`, `build_inputs_sha256`). Releases should be built with `--reuse none` unless the receipt shows `reuse`.

## 4. What `scripts/verify-iso.py` checks

Rules live in `iso/image-contract.json`; items with `when_source` apply only while that file exists in the repository, or while `pyproject.toml` declares the console script (`gentoo-rescue`, `gentoo-installer-text`, `gentoo-rescue-gui`). A required path can also name a `mode` (`/mnt/rescue` must be 0700). Exit status 0 passed, 1 a check failed, 2 the ISO could not be read.

| Area | Checks |
|---|---|
| ISO | Volume label equals the pinned one (`_TESTKEY` for a test image) and has at most 32 characters; exactly one BIOS and one UEFI El Torito image; hybrid MBR + GPT (USB sticks); required files: `boot/gentoo`, `boot/gentoo.igz`, `efi.img`, `efi/boot/bootx64.efi`, **`efi/boot/bootia32.efi`**, `README.txt`, `SECURE-BOOT-OFF.txt`, `memtest64.bios`, `memtest.efi64`; GRUB module directories for `i386-pc`, `x86_64-efi`, `i386-efi`. |
| `grub.cfg` | At least 12 entries; `default=0`, positive `timeout`; every `linux` line is `/boot/gentoo` with the pinned boot arguments and `root=live:CDLABEL=<label> rd.live.dir=/ rd.live.squashimg=image.squashfs cdroot`, an `initrd /boot/gentoo.igz` and `search ... -l <label>`; the default entry starts the desktop with no `dokeymap`; `dokeymap` only together with `nox`; entries with `nox`, `nomodeset`, `rd.live.ram=1`, serial console, `dospeakup`, `acpi=off`, `rd.debug`; the entries "Repair a system (text console)" and "Install in text mode" with `nox dokeymap gentoo.autostart=rescue` / `installer-text`, and `gentoo.autostart=` only with a known value and only together with `nox`; no "safe graphics" title; unique titles (no Memtest block twice); Memtest entries exist when the images are in the ISO. A rendered `livecd-stage2.spec` must agree on label and boot arguments. |
| initramfs | `dmsquash-live` scripts and `dmsquash-live-root`; modules `isofs squashfs overlay loop usb-storage uas vmd sdhci-pci mmc_block dm-crypt raid1`; at least 20 console keymaps when an entry asks for `dokeymap`. |
| squashfs | Required files and types, commands (about 80: partitioning, file systems, LUKS/LVM/RAID, EFI, SMART, `cpuid2cpuflags`, `setxkbmap`, `links`, `thin_check`, and every live-side program of the repair tool such as `lsinitrd`, `jfs_fsck`, `script`, `chroot`, `timeout`, `modprobe`, `zstd` ...); every `livecd/rcadd` service as a runlevel symlink **and** an init script (plus Catalyst's own `autoconfig`, `xdm`, `fixinittab`, `syslog-ng`); `acpid` in no runlevel; the live GRUB's platform directories (`usr/lib/grub/i386-pc` and `x86_64-efi`, which `grub-install` needs when the repair tool has to install the boot loader from the live side); the polkit agent and its Xfce autostart file; `gentoo-chroot` and the console scripts of the repair tool and the text installer; forbidden paths (`/var/cache/binhost`, `/opt/rust-bin-*`, `/usr/share/doc`, static Qt libraries, `make.conf.old/.extra`, the Gentoo tree); **no entry owned by uid/gid 1000 or by the build user's HOST_UID/HOST_GID** outside `home/gentoo` and the live user's mailbox; root directory owned by root; content rules (`lvmlocal.conf`, `mdadm.conf`, `ACPI="no"`, logind drop-in, Hidden screensaver, no `rtcsync`, a browser in `helpers.rc`, a live `make.conf` without `MAKEOPTS`, `NINJAOPTS`, `EMERGE_DEFAULT_OPTS`, `DISTDIR`, `buildpkg` or `ACCEPT_LICENSE="*"`, greeting texts); required and forbidden packages (`dev-lang/rust-bin`, `sys-boot/shim`, `net-misc/netifrc`); the flavour rules above. |

Run on the accepted r19 image the squashfs rules fail exactly where this change set changes the image (43 overlay entries owned by 1000:1000, `acpid` enabled, no `chronyd`, rust-bin present, a builder `make.conf`, the new packages missing), and pass where nothing changed; that is the red test for I10. `tests/iso/test_verify_iso.py` also builds a small ISO with xorriso and gensquashfs from the repository's own overlay and runs the real contract against it, in both flavours, and again with parts taken away.

### When a check stops the build

* **The image check fails** (`the ISO failed the image checks; nothing was published`): the finished ISO stays in `<cache>/catalyst/builds/23.0-default/`. Run `scripts/verify-iso.py <that file>` for the full list of failed rules, fix the cause (or the rule in `iso/image-contract.json` if the rule was wrong) and run `scripts/build-iso.sh --stage2-only`: stage1 is reused, so this takes minutes, not hours.
* **The Gentoo commit is refused** (`verify-snapshot:` in the log, either while the builder image is made or at the start of a build): the commit of `GENTOO_TREEISH` is not signed, is signed by another key than `GENTOO_TREEISH_SIGNER_FPR`, or its signer is missing from the keyring of `sec-keys/openpgp-keys-gentoo-developers` in the bootstrap tree (a very new key). Check the commit by hand (`git verify-commit`), then pin the commit that you verified together with its signer in `iso/pins.env`. The check cannot be switched off from the command line on purpose.
* **`stage2-only` is refused** (`no receipt`, `sha512`, `specs, Portage configuration or pins changed`): stage1 is out of date; build once without `--stage2-only`.

## 5. Boot menu

`iso/cd_overlay/boot/grub/grub.cfg.in` is rendered to the overlay and **replaces** Catalyst's three-entry menu.

1. Gentoo installer and rescue desktop (default, no keymap prompt)
2. Desktop in RAM, label says "needs 6 GiB" (the squashfs is 2.8 GB; with the overlay and the desktop 6 GiB is the sane figure)
3. **Repair a system (text console)**: `nox dokeymap gentoo.autostart=rescue`. The login on tty1 starts `sudo gentoo-rescue` once (see below).
4. **Install in text mode**: `nox dokeymap gentoo.autostart=installer-text`. The login on tty1 starts `sudo gentoo-installer-text` once.
5. Text console only: `nox dokeymap`, a plain shell (asks for a console keymap, which is needed for LUKS passphrases)
6. More options: verbose (`ignore_loglevel loglevel=7 rd.debug`), compatibility (`nomodeset pcie_aspm=off nvme_core.default_ps_max_latency_us=0 usbcore.autosuspend=-1`), serial console (`nox console=tty0 console=ttyS0,115200n8`), old firmware (`acpi=off noapic nomodeset`), IOMMU off, text console in RAM (4 GiB), accessibility (`dospeakup`)
7. Memtest86+ (BIOS and UEFI; Catalyst's own block, guarded by `[ -e ... ]` at boot time, present once)
8. UEFI firmware settings (`insmod efifwsetup; fwsetup`), continue with the next boot device (`exit`), restart, power off

Details that matter: `nox` is honoured by `gui-libs/display-manager-init` (its setup script touches `/run/.nogui`, `start_pre` refuses), and by livecd-tools' `autoconfig` (`X11=no`, `chvt 1`), so the project's `xdm` shim stays as it is. `secureconsole` stays in the boot arguments: livecd-tools' `fixinittab` then logs in only tty1 (as the live user) and puts real login prompts on tty2-6. With the serial entry the **last** `console=` becomes the login console: serial only, root auto-login (the VGA getty is removed), which is what a headless machine wants.

**The text entries and the autostart hook.** `nox` is the plain token; the display manager does not start, so the entries work on a GPU the live kernel cannot drive. `dokeymap` is on both because a LUKS passphrase or an account password is typed in the tool, and the initramfs prompt is the only place where the console layout is chosen before the login. `gentoo.autostart=rescue` and `gentoo.autostart=installer-text` mean nothing to the kernel (a parameter with a dot is ignored); `/etc/profile.d/20-gentoo-autostart.sh` reads them from `/proc/cmdline`. On the **first console login of an interactive text session** (tty1, no `DISPLAY`) it waits up to 20 s for the installer service socket, runs `sudo gentoo-rescue` or `sudo gentoo-installer-text` in the foreground, and prints how to start it again when it ends or fails. A `mkdir` of `/tmp/.gentoo-autostart` is the one-time switch, so the autologin that follows `exit` does not start it a second time. If `sudo` or the tool is missing it says so and the login continues. Another console, a script, scp or the desktop never start it. `etc/profile.d/10-gentoo-rescue-hint.sh` prints the three commands on every text console. The tokens and titles are checked by `scripts/verify-iso.py` and by `tests/iso/test_text_modes.py`, which drives the hook in a shell with stand-ins for `sudo`, `tty` and the tools.

There is deliberately **no "safe graphics" entry**. The live kernel (`gentoo-kernel-bin` 6.18.57) is built with `# CONFIG_DRM_SIMPLEDRM is not set`, `# CONFIG_DRM_EFIDRM is not set`, `# CONFIG_DRM_VESADRM is not set`, `# CONFIG_SYSFB_SIMPLEFB is not set`, `# CONFIG_FB_DEVICE is not set`; under `nomodeset` there is no DRM node and no `/dev/fb0`, so Xorg cannot start. The entries that use `nomodeset` say "text only". See section 9.

## 6. Live-session behaviour (iso/root_overlay)

| File | Effect |
|---|---|
| `etc/lvm/lvmlocal.conf`, `etc/mdadm.conf` | `event_activation = 0`, empty `auto_activation_volume_list`, `AUTO -all`: no volume group or RAID array of the PC is activated or assembled by udev (activation can replay journals and write). The installer and the repair tool activate exactly what the user selected. Consequence for installer guards: pre-existing VGs and arrays are no longer visible as holders, so `inspect`/storage preflight must check signatures explicitly (review H12). |
| `etc/xdg/xfce4/xfconf/.../thunar-volman.xml` | no automount, auto-open or autorun of drives and media. |
| `etc/elogind/logind.conf.d/50-gentoo-live.conf`, `.../xfce4-power-manager.xml`, `etc/xdg/autostart/xfce4-screensaver.desktop` (Hidden), `gentoo-live-noblank.desktop` | lid close and idle do nothing, presentation mode on, DPMS and the X screen saver off, critical battery does not hibernate. An inhibitor lock does not stop a lid-close suspend (`LidSwitchIgnoreInhibited=yes`), the drop-in does. Property names were taken from the strings of the xfce4-power-manager 4.20 binary. |
| `etc/conf.d/autoconfig` (`ACPI="no"`), `etc/acpi/actions/powerbtn.sh` | `acpid` is not started (autoconfig would start it whenever installed, and its handler powers off regardless of inhibitors); elogind and XFPM own the power key. The handler is a logging no-op in case somebody starts acpid. |
| `etc/chrony/chrony.conf`, `chronyd|default` | pool, `makestep 1.0 -1`; no `rtcsync`/`rtcfile`. OpenRC's `hwclock` does not write the RTC on a live shutdown (`CDBOOT`). |
| `etc/motd`, `etc/issue`, `etc/profile.d/10-gentoo-rescue-hint.sh` | where to find installer, text installer, repair, network, hardware report; the hint is printed on text consoles only. |
| `etc/profile.d/20-gentoo-autostart.sh` | starts the repair tool or the text installer once, when the boot menu entry asked for it (section 5). |
| `etc/xdg/autostart/polkit-gnome-authentication-agent-1.desktop` | the live Xfce session needs a password dialog for `pkexec` programs such as GParted. The package `gnome-extra/polkit-gnome` ships a file of the same name with `AutostartCondition=GNOME3 ...` and `NotShowIn=MATE;KDE;`, which Xfce may not honour; the overlay replaces it with `OnlyShowIn=XFCE;`. The live user has an empty password, so the dialog accepts an empty field (PAM `nullok`); `sudo gparted` needs no dialog. |
| `etc/xdg/xfce4/panel/default.xml`, `etc/xfce4/helpers.rc` | keyboard layout plugin (`xfce4-xkb-plugin`), `WebBrowser=links` (the launcher opened the "Preferred Applications" dialog before). |
| `fsscript-stage2.sh` | creates `/mnt/gentoo` (0755) and `/mnt/rescue` (0700); fails the build when a required program (the installer's and every live-side program of the repair tool), the polkit agent, the live GRUB's `i386-pc` and `x86_64-efi` files, the lvm/mdadm/autoconfig settings, chronyd or the Memtest images are missing; warns when the repair package or the text installer is installed but its console scripts are not. |

## 7. Package set and size

Added to `livecd/packages` (all verified in the pinned tree as stable amd64 with their licences and USE flags; `iso/portage/package.use` sets `sys-apps/memtest86+ boot bios64 uefi64`, `sys-fs/lvm2 thin`, `app-misc/hwmonux liquidctl`, `app-admin/gentoo-live-installer secureboot`): `sys-apps/memtest86+` 7.20, `sys-boot/mokutil`, `app-crypt/sbsigntools` (diagnostics only), `sys-block/thin-provisioning-tools`, `sys-process/psmisc`, `app-misc/jq`, `sys-apps/pv`, `sys-apps/hdparm`, `sys-apps/lshw`, `app-portage/cpuid2cpuflags`, `x11-apps/setxkbmap`, `x11-apps/xrandr`, `www-client/links`, `xfce-extra/xfce4-xkb-plugin`. `sys-boot/shim` is deliberately absent: without a signed GRUB chain it is dead weight. `lvm2[thin]` is rebuilt locally; `thin-provisioning-tools` (Rust) comes from the binhost or builds with the chroot's rust.

The installer ebuild's RDEPEND also names `sys-kernel/dracut` (for `lsinitrd`), `sys-boot/grub`, `sys-apps/kmod`, `sys-apps/coreutils` and `app-arch/zstd`. The image had all of them already (Catalyst installs dracut in stage2 for the kernel step), so its size does not change. What changes is stage1: it now installs dracut through the ebuild, so the first build after this change should use `--reuse none`.

Measured on the accepted r19 image (ISO 2,981,701,632 bytes; `image.squashfs` 2,842,341,376 bytes, xz; 788 packages; 7.4 GiB of files):

| Item | Uncompressed | Action |
|---|---|---|
| `/var/cache/binhost` (440 gpkgs, already compressed) | 471 MiB | removed (`livecd/rm`) |
| `dev-lang/rust-bin` (nothing installed depends on it at run time: `/var/db/pkg` checked) | 676 MiB | unmerged |
| `/usr/lib64/libQt6*.a` (private static libraries) | 273 MiB | removed |
| `/usr/share/doc`, `/usr/share/gtk-doc` | 76 + 30 MiB | removed; man and info pages stay |
| `/etc/portage/make.conf.old` and `.extra` | 4 KiB | removed (`livecd/rm`); they hold the builder's job counts and caches |
| `/usr/lib/firmware` | 1.9 GiB | kept: Wi-Fi, GPU and sound firmware is what makes other PCs work |
| `/usr/lib/llvm` | 573 MiB | kept: Mesa's drivers link libLLVM |
| `/usr/include`, other `.a` | 132 + 22 MiB | kept: users compile on the live system |
| `/var/cache/edb` | 25 MiB | kept: it holds Portage's COUNTER file next to the dependency cache; the saving is not worth any risk |

The removals are about 1.5 GiB uncompressed. The binhost packages do not compress further (about 0.47 GB of the ISO); rust-bin, static libraries and docs compress to roughly 0.35 GB, so the ISO should get **about 0.8 GB smaller**. The new packages add roughly 30-40 MiB uncompressed (the Rust `pdata_tools` binary, `links` and its libraries and the `lshw` ID files are the largest) and roughly 10-15 MB to the ISO. These are estimates until the next build; `manifest.json` records the final size and `verify-iso.py` warns when an ISO grows more than 20 percent over the previous manifest (`--previous-manifest`; the builder passes the manifest of an earlier build of the same ISO name, if `dist/` holds one).

## 8. Packaging

`packaging/gentoo-overlay`: `gentoo-live-installer-0.1.0-r5` (one revision only: `build-iso.sh` and the builder insist on exactly one ebuild per package; a changed ebuild needs no new revision, the reuse logic reinstalls it by name), `hwmonux-1.0.0-r1`, `portagehub-0.4.0`, `acct-group/gentoo-installer`, each with a `metadata.xml`. The distfiles are produced by the builder, so the ebuilds have `RESTRICT="mirror fetch"` and a `pkg_nofetch` message; Manifests are generated at build time (thin manifests), which is the one pkgcheck finding that stays (`ISO_RUN_PKGCHECK=1` runs pkgcheck on a copy and allows exactly that, `PythonCompatUpdate` for python3_15 which the stable pyside lacks, and the unstable `liquidctl` behind its flag).

* RDEPEND of the installer lists every program the daemon, the hardware probe and the repair tool run. `tests/iso/test_ebuilds.py` maps every entry of `REQUIRED_TOOLS`/`OPTIONAL_TOOLS` in `inspect.py`, of `TOOLS_WANTED` (`rescue/hostenv.py`), of `CHECK_TOOLS` (`rescue/fsprobe.py`) and every program that the host policy of the repair tool (`rescue/safety.py`) has a checker for to an atom (`TOOL_PACKAGE`), and fails when one is not mapped, when its package is not in RDEPEND, or when the stage2 fsscript or the image contract does not check for it. A new program therefore needs four edits in one change: the package in RDEPEND, `TOOL_PACKAGE`, `required_commands` in `iso/fsscript-stage2.sh` and `squashfs.commands` in `iso/image-contract.json`. The file system checks of the repair tool (F2FS, exFAT, JFS, NTFS), GRUB (the live `grub-install` is the fallback when the repaired system cannot run its own), Dracut (`lsinitrd`), kmod, coreutils and zstd are unconditional. Btrfs, XFS, EFI and Secure Boot tools (`sbsigntools`, `mokutil`) sit behind `btrfs`, `xfs`, `efi`, `secureboot` (the live image has all of them).
* The ebuild is made for the live disc. The overlay is also copied to installed systems (`/var/db/repos/gentoo-live-installer`, so that HWMonux and PortageHub can be installed there). Emerging the installer package on such a system would pull GRUB, dracut and the file system tools of the repair tool.
* The rescue launcher `data/gentoo-rescue.desktop` (`Exec=gentoo-installer --mode repair`) and the helper `gentoo-chroot` (`exec gentoo-rescue chroot "$@"`, installed from `files/` with `dobin`) are installed when the sources contain `src/gentoo_installerd/rescue`. The console scripts `gentoo-rescue` and `gentoo-installer-text` (and `gentoo-rescue-gui`, if it ever exists) come from `pyproject.toml` (distutils-r1 installs whatever it declares); the image contract requires each of them only when `pyproject.toml` declares it. The motd, the boot menu and the disc's README name both, so `pyproject.toml` must declare them when the tools ship: `tests/iso/test_text_modes.py` fails once `src/gentoo_installer/textui` exists without `gentoo-installer-text = "gentoo_installer.textui.cli:main"`, and `test_overlay_files.py` does the same for `gentoo-rescue`.
* HWMonux: `liquidctl` and `hidapi` are `~amd64` only, so the dependency sits behind a flag that is off by default: the ebuild solves on a stable system, and the live image enables it. `drivetemp` is loaded from `/etc/modules-load.d/hwmonux.conf`; Super I/O drivers (`nct6775`, `it87`) are never loaded automatically because probing can hang an embedded controller; `sensors-detect` stays a manual step.
* Services: OpenRC runs the daemon as root without D-Bus, logs to `/var/log/gentoo-installerd.log` and allows 60 s for the SIGTERM clean-up; systemd uses `KillMode=mixed`, `TimeoutStopSec=60`, `RuntimeDirectory=gentoo-installer`. A supervisor (`supervise-daemon`) was not enabled because it cannot be tried without an OpenRC boot; `Restart=on-failure` stays for systemd only.

## 9. Known limits and follow-ups

* **Safe graphics (review H1)**: needs a kernel with `CONFIG_DRM_EFIDRM`/`DRM_VESADRM` (or `SYSFB_SIMPLEFB` + `DRM_SIMPLEDRM`; the two families exclude each other), i.e. `sys-kernel/gentoo-kernel` built from source with a fragment in `/etc/kernel/config.d/` installed from a stage1 package, +30-60 minutes. Then add the entry back, make `verify-iso.py` check `boot/gentoo-config` for the symbols, and remove `safe graphics` from `forbidden_titles` in the contract.
* **Secure Boot**: the ISO is unsigned (no shim chain), documented in the README, in `SECURE-BOOT-OFF.txt` and in the disc's `README.txt`.
* **32-bit UEFI (review H6)**: `bootia32.efi` and `i386-efi` are on the ISO because the builder image sets `grub_platforms_efi-32` and `grub-mkrescue` runs in the builder; `iso/portage/make.conf` `GRUB_PLATFORMS` only concerns the GRUB inside the live root. The kernel has `CONFIG_EFI_MIXED=y`. A boot test with edk2-i386 in QEMU is still to be run by the harness.
* **Kernel age (review H11)**: Linux 6.18.57 and the firmware of the pinned tree. Hardware released later may lack drivers; a second image with a newer kernel needs `accept_keywords` for `gentoo-kernel` and its own pin.
* **Network shares**: `net-fs/cifs-utils` and `nfs-utils` are not on the disc. The repair design lists cifs-utils as an optional atom; its default `ads` flag pulls in Kerberos and keyutils, and a package added to a build that cannot be tested here is paid for with hours of rebuild if it fails. OpenSSH and rsync are on the disc, so data leaves a broken machine over `rsync -a` or `scp` to another computer, or to a USB disk. Adding cifs-utils later is one atom in `livecd-stage1.spec`; the changed spec makes the next build a fresh stage1.
* **Not in this change set**: the live hardware service (`gentoo.xkb=`/`gentoo.tz=` boot parameters, HiDPI helper, touchpad defaults), a browser for captive portals beyond Links, the daemon holding a logind inhibitor during an install (`elogind-inhibit`, the lid is already ignored by the drop-in), `accept-iso.py` (binds VM results to the ISO hash), the sha256 of the binhost `Packages` file in the manifest, and a VM run of `--reuse` reseeds. Each is listed in the integration notes of the change.
* **Manual checks on a build** (QEMU, then real hardware): boot each menu entry under SeaBIOS and OVMF (and edk2-i386); the text console entries show no `display-manager` process and a tty1 login; "Repair a system (text console)" starts `gentoo-rescue` and "Install in text mode" starts `gentoo-installer-text` after the keymap prompt and only once; `sudo gentoo-rescue` finds an installed Gentoo, Debian and Fedora disk; a polkit dialog appears for GParted on the desktop and takes an empty password; the serial entry logs in on `ttyS0`; Memtest86+ starts; "UEFI firmware settings" opens the setup; `rc-status` shows `chronyd` and no `acpid`; with a laptop, close the lid on battery and on power during an install and confirm it stays awake; plug in a disk with an LVM volume group and a RAID array and confirm they stay inactive until the repair tool activates them.

## 10. Tests

```sh
./scripts/test.sh tests/iso                      # or: python -m pytest tests/iso
ISO_TREE=<pinned gentoo tree> ISO_RUN_PKGCHECK=1 ./scripts/test.sh tests/iso
```

`tests/iso` covers: the text-mode menu entries, the console hook (`test_text_modes.py`) and `gentoo-chroot`; the specs and package union (atoms, USE flags and licences against the pinned tree with `ISO_TREE`); the renderer; the boot menu against the verifier; every overlay file (including property names against the XFPM and thunar-volman binaries); the Catalyst wrapper against the real Catalyst 4.2 classes, skipped for any other Catalyst release so that an upgrade is a conscious review; stage1 reuse decisions; `verify-iso.py` (fixtures from the accepted r19 image and an end-to-end ISO); `build-iso.sh` with a stub `docker` and `df` (limits, logging, SIGINT/SIGTERM, OOM hint, locking, disk checks, test-key rules, hardening options); the entrypoint's steps with a stub Catalyst; signal forwarding; the signed-tree verifier with throwaway keys; ebuilds, services and versions; the manifest writer.
