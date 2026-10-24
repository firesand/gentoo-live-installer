# Gentoo LiveCD Installer

A live USB/DVD image that does two jobs on almost any 64-bit PC or laptop:

* **Install Gentoo** with a guided Qt wizard (`gentoo-installer`) that talks to a small root daemon (`gentoo-installerd`).
* **Repair a system that no longer starts**, Gentoo or another Linux distribution (`gentoo-rescue`, "Repair an Existing System" in the menu). The disc finds installed systems, asks for disk passwords, mounts everything read-only first and offers repairs for the boot loader, fstab, initramfs and kernel, or a shell inside the system.

The repository holds the wizard and daemon (`src/`), the ISO build (`iso/`, `scripts/build-iso.sh`), the Portage overlay (`packaging/`), two companion apps (`apps/portagehub`, `apps/hwmonux`) and the disposable-VM test harness (`scripts/test-vm.py`). What has been verified and what has not is tracked in [REVIEW_FIX_STATE.md](REVIEW_FIX_STATE.md); nothing here claims a tested install on real hardware.

## Before you boot it: Secure Boot must be off

This disc cannot start while **Secure Boot** is on. Its boot loader is not signed with a key that PC firmware trusts, so the firmware refuses it ("Secure Boot violation", "Invalid signature detected", or it silently skips the stick). This is the most common reason a live USB "does not work".

1. Restart and open the firmware setup (F2, Del, F10, F12 or Esc right after power on).
2. Set **Secure Boot** to *Disabled* (often under Security or Boot; some firmware first wants an administrator password or "Other OS" mode).
3. Save, and boot the stick again.

On a PC that runs **Windows with BitLocker or "Device encryption"**, look up your recovery key first (<https://aka.ms/myrecoverykey>); changing firmware settings can make Windows ask for it. The same text is on the disc as `SECURE-BOOT-OFF.txt` and in `README.txt`, and [docs/USER-GUIDE.md](docs/USER-GUIDE.md) explains the vendor keys.

## Hardware: what to expect

| | |
|---|---|
| **Works** | 64-bit x86 PCs, laptops and virtual machines. BIOS/CSM, 64-bit UEFI and 32-bit UEFI firmware (the ISO carries `bootx64.efi` and `bootia32.efi`). SATA/AHCI, NVMe (also behind Intel VMD), USB, SD/eMMC and virtio disks. Wi-Fi, Bluetooth and sound that the Linux 6.18 kernel and `linux-firmware` support. Intel, AMD and (with the open nouveau driver) NVIDIA graphics. |
| **Needs a step from you** | Secure Boot off (above). If the disk does not show up, switch the firmware's SATA mode from Intel RST/"RAID" to AHCI (mind Windows) or leave it and use a kernel that handles VMD (this one does). A wrong clock is corrected automatically once the network is up; the live system never writes to the hardware clock. |
| **Not supported** | 32-bit CPUs, ARM machines (Apple Silicon Macs, Windows-on-ARM laptops, Raspberry Pi), Intel Macs with a T2 chip (the internal SSD is not visible), ZFS root. |
| **Known limits** | The live kernel has **no generic framebuffer driver** (no `simpledrm`/`efidrm`): a GPU that is too new for this kernel gets no desktop, only the text console entry. `nomodeset` therefore gives a text console, not a "safe graphics" desktop. The image is built from a fixed snapshot (Linux 6.18.57, Gentoo tree 2026-10); hardware released after it may lack drivers. Real-hardware boots are not part of the automated tests, only QEMU with BIOS and UEFI firmware. |

The boot menu offers a desktop, a desktop copied to RAM (needs 6 GiB), a text console for repairs and servers, and troubleshooting entries (verbose, compatibility, serial console, old firmware, IOMMU off, accessibility), Memtest86+, firmware setup and restart/power off. The live session never mounts or activates the PC's disks, volume groups or RAID arrays by itself, and ignores the lid switch and idle timers, so an installation or repair is not interrupted.

## Using the disc

* **Write it** to a USB stick (at least 8 GB) with `dd`, Rufus in *DD image* mode or balenaEtcher; check the `.sha512` first. Everything on the stick is erased. Tools that unpack files instead of copying the image do not give a bootable stick.
* **Install Gentoo**: boot the first entry, the installer starts with the desktop. Steps: Welcome, Readiness, Storage, Base system, Customization, Accounts, Review, Install. The disk you choose is erased; you type its name to confirm. See [docs/USER-GUIDE.md](docs/USER-GUIDE.md).
* **Repair a system**: open "Repair an Existing System" or run `gentoo-rescue` in a terminal. Mount the system, look at the diagnosis, then run the plan you choose. The guide has a manual chroot cheat sheet for Gentoo, Debian/Ubuntu, Fedora and Arch as well.

## Build the ISO

The build runs Catalyst in Docker (about 45 GiB of free disk, 8 GiB of RAM for the container, several hours the first time). See [docs/ISO.md](docs/ISO.md) for the pipeline, the flags and what is checked.

```sh
./scripts/build-iso.sh --check          # prerequisites and inputs, creates nothing
JOBS=4 ./scripts/build-iso.sh           # the release ISO into dist/
./scripts/build-iso.sh --stage2-only    # re-run only stage2 when stage1 is unchanged
./scripts/build-iso.sh --verify dist/gentoo-installer-amd64-*.iso
./scripts/build-iso.sh --sign-key 0xYOURKEYID   # also sign the checksums and the manifest
```

* The result is `dist/gentoo-installer-amd64-<stamp>.iso` with `.sha512`, `.blake2b`, `.manifest.json` and `.packages.txt`; every build writes a timestamped log to `dist/logs/`.
* A following build reuses the stage1 of the previous one when nothing that goes into it changed (`--reuse auto`, the default), reinstalls only the project's own packages when only those changed, and rebuilds everything otherwise. `--reuse none` always builds everything.
* The finished ISO is checked before it is published (`scripts/verify-iso.py`): boot images, boot menu, initramfs, services, ownership and no test access in a release.
* Settings: `JOBS`, `LOAD_AVERAGE`, `BUILD_MEMORY` (container memory, default 8g), `BUILD_MIN_FREE_GIB`, `ISO_CACHE_DIR`, `ISO_DIST_DIR`, `ISO_LOG_DIR`, `ISO_SECCOMP`. `--clean` removes retained chroots and old stage1 archives from the cache (`.cache/iso`).
* The container needs `SYS_ADMIN`, `NET_ADMIN`, `MKNOD` and `SYS_CHROOT` and, until a build is shown to work without, an open seccomp profile: the build runs the Gentoo ebuilds of about 500 packages as root inside the container. Build untrusted changes in a throwaway VM.

## Test ISO for the VM harness

The harness logs into the live system over SSH, so it needs an image with a temporary root key. Such an image is named and labelled `...-TESTKEY` (volume `GENTOO_INSTALLER_TESTKEY`), shows a "TEST BUILD" banner, and is refused in `dist/`:

```sh
install -d -m 0700 build/vm-control
testkey="build/vm-control/iso-test-$(date +%s)"
ssh-keygen -q -t ed25519 -N '' -f "$testkey"
ISO_DIST_DIR=dist/test-only JOBS=2 ./scripts/build-iso.sh --test-key-file "$testkey.pub"
```

Never hand a `-TESTKEY` image to anybody: whoever has the key can log in as root. The examples below assume `$testkey` is still set; remove the key pair when you are done.

## Tests

```sh
./scripts/test.sh
```

It runs the Python tests, the PortageHub Rust tests and the HWMonux dump smoke check (the Python environment is `.venv/bin/python` when present). Tests that check packages against a Gentoo tree need `ISO_TREE=<path to the pinned tree>`; `ISO_RUN_PKGCHECK=1` also runs pkgcheck on a copy of the overlay. The ISO tests (`tests/iso`) build no ISO: they check the specs, the boot menu, the overlay, the build script with a stub `docker`, and a synthetic image against the same rules the real ISO must pass.

## Disposable VM checks

The VM harness creates a new qcow2 disk under `build/vm`, refuses to overwrite an existing disk, and attaches it as the guest's only target disk with serial `GENTOO-VM-DISPOSABLE`. It does not pass through host block devices. The SSH forward is loopback-only. Use a free host port and the private key whose public half was included in the test ISO.

Run a GUI installation with both companion apps and a same-live-session resume check:

```sh
python3 scripts/test-vm.py \
  --scenario openrc --iso dist/test-only/gentoo-installer-amd64-20261018T124555Z-TESTKEY.iso \
  --ssh-host 127.0.0.1 --ssh-port 40222 \
  --ssh-private-key "$testkey" \
  --memory 12288 --cpus 4 --build-jobs 1 --swap-mib 4096 --companions both \
  --install-mode gui --check-resume
```

Companion dependencies can require compiling Qt/PySide. The example limits compilation to one job and allocates swap inside the disposable target disk. Leave at least 3 GiB of host memory available beyond the VM allocation and run this larger test alone. These settings describe the test configuration, not a verified minimum hardware requirement.

The resume check runs before the live VM shuts down. It reruns the daemon with `--resume` against the same manifest and disk, then checks storage identities and recorded destructive commands before the harness boots the installed disk without live media.

The opt-in GUI cancellation regression is:

```sh
python3 scripts/test-vm.py \
  --scenario systemd --iso dist/test-only/gentoo-installer-amd64-20261018T124555Z-TESTKEY.iso \
  --ssh-host 127.0.0.1 --ssh-port 40222 \
  --ssh-private-key "$testkey" \
  --memory 6144 --companions portagehub \
  --install-mode gui --cancel-then-resume
```

It requests cancellation when stage3 starts, checks the reported cancellation and cleanup, resumes on the same guarded disk, and then performs the disk-only boot checks. UEFI LUKS can use `--scenario uefi-luks --luks-unlock external`; the harness saves a QMP screendump and polls SSH while an operator inspects the prompt and enters the test passphrase. `--scenario bios` exercises the BIOS layout. These commands describe available checks; consult [REVIEW_FIX_STATE.md](REVIEW_FIX_STATE.md) for whether a VM run has actually succeeded.

## Source entry points

- GUI application and pages: `src/gentoo_installer/app.py`, `src/gentoo_installer/pages/`
- Manifest and GUI-to-daemon client: `src/gentoo_installer/manifest.py`, `src/gentoo_installer/backend.py`
- Daemon protocol and installation flow: `src/gentoo_installerd/server.py`, `src/gentoo_installerd/install.py`
- Storage and system configuration: `src/gentoo_installerd/storage.py`, `src/gentoo_installerd/configure.py`
- Shared interfaces: [docs/contracts.md](docs/contracts.md), [docs/protocol.md](docs/protocol.md)
- ISO build inputs: `scripts/build-iso.sh`, `iso/builder-entrypoint.sh`, `iso/specs/`, `iso/root_overlay/`, `iso/cd_overlay/`, `iso/image-contract.json`; packages in `packaging/gentoo-overlay`
- ISO acceptance: `scripts/verify-iso.py`; VM harness and GUI driver: `scripts/test-vm.py`, `scripts/vm-gui-install.py`
- Documentation: [docs/USER-GUIDE.md](docs/USER-GUIDE.md) (installing on other PCs, repairing a system), [docs/ISO.md](docs/ISO.md) (build pipeline)
