# Gentoo LiveCD Installer

A live USB/DVD image that does three jobs on almost any 64-bit PC or laptop:

* **Install Gentoo** with a guided desktop wizard (`gentoo-installer`), or **in text mode** when the desktop does not start (`gentoo-installer-text`).
* **Repair a system that no longer starts** (`gentoo-rescue`). The disc finds installed systems, asks for disk passwords, mounts everything read-only first and offers repairs for the boot loader, fstab, initramfs and kernel. It repairs Gentoo, Debian and Ubuntu, and Fedora. It finds and diagnoses other systems (Arch, openSUSE, ...) and opens a shell in them.
* **Look at the hardware** and set the installation up for it: graphics, firmware, power, disks.

The repository holds the wizard and the root daemon (`src/`), the ISO build (`iso/`, `scripts/build-iso.sh`), the Portage overlay (`packaging/`), two companion apps (`apps/portagehub`, `apps/hwmonux`) and the disposable-VM test harness (`scripts/test-vm.py`). Nothing here claims a tested install on real hardware: [docs/TESTING.md](docs/TESTING.md) lists what has run and what has not.

## Before you boot it: Secure Boot must be off

This disc cannot start while **Secure Boot** is on. Its boot loader is not signed with a key that PC firmware trusts. The firmware refuses it ("Secure Boot violation", "Invalid signature detected") or silently skips the stick. This is the most common reason a live USB "does not work".

1. Restart and open the firmware setup (F2, Del, F10, F12 or Esc right after power on).
2. Set **Secure Boot** to *Disabled* (often under Security or Boot; some firmware first wants an administrator password or "Other OS" mode).
3. Save, and boot the stick again.

On a PC that runs **Windows with BitLocker or "Device encryption"**, look up your recovery key first (<https://aka.ms/myrecoverykey>): changing firmware settings can make Windows ask for it. The same text is on the disc as `SECURE-BOOT-OFF.txt`. [docs/USER-GUIDE.md](docs/USER-GUIDE.md) says what the installed system supports (GRUB does not work with Secure Boot).

## The boot menu

| Entry | Use it to |
|---|---|
| Gentoo installer and rescue desktop | Install with the wizard, or open the repair window. The default. |
| **Repair a system (text console)** | Fix a system that does not start. No desktop, works with any graphics chip. |
| **Install in text mode** | Install without a desktop. |
| Desktop in RAM | Run from memory (needs 6 GiB), then remove the stick. |
| Text console only | A shell, for experts and headless machines. |
| More options | Verbose boot, compatibility mode, serial console, old firmware, IOMMU off, text console in RAM, speech output. Also Memtest86+, firmware setup, restart and power off. |

The text entries ask for a keyboard layout first (you type passwords in both tools), then start their tool by themselves after the login on the first console. You can also run `sudo gentoo-rescue` or `sudo gentoo-installer-text` yourself. `gentoo-chroot SESSION` opens a shell in a system you mounted with the repair tool.

## Hardware: what to expect

| | |
|---|---|
| **Works** | 64-bit x86 PCs, laptops and virtual machines. BIOS/CSM, 64-bit UEFI and 32-bit UEFI firmware (the ISO carries `bootx64.efi` and `bootia32.efi`). SATA/AHCI, NVMe (also behind Intel VMD), USB, SD/eMMC and virtio disks. Wi-Fi, Bluetooth and sound that the Linux 6.18 kernel and `linux-firmware` support. Intel, AMD and (with the open nouveau driver) NVIDIA graphics. The proprietary NVIDIA driver is an option of the installed system. |
| **Needs a step from you** | Secure Boot off (above). If the disk does not show up, switch the firmware's SATA mode from Intel RST/"RAID" to AHCI (mind Windows). A wrong clock is corrected once the network is up; the live system never writes to the hardware clock. |
| **Not supported** | 32-bit CPUs, ARM machines (Apple Silicon Macs, Windows-on-ARM laptops, Raspberry Pi), Intel Macs with a T2 chip (the internal SSD is not visible), ZFS root. |
| **Known limits** | The live kernel has **no generic framebuffer driver** (no `simpledrm`/`efidrm`): a GPU that is too new for this kernel gets no desktop, only the text entries. `nomodeset` gives a text console, not a "safe graphics" desktop ([docs/ISO.md](docs/ISO.md) section 9). The image is built from a fixed snapshot (Linux 6.18.57, Gentoo tree 2026-10): hardware released after it may lack drivers. Real-hardware boots are not part of the automated tests, only QEMU with BIOS and UEFI firmware. |

The live session never mounts or activates the PC's disks, volume groups or RAID arrays by itself. It ignores the lid switch and idle timers, so an installation or repair is not interrupted.

## Using the disc

* **Write it** to a USB stick (at least 8 GB) with `dd`, Rufus in *DD image* mode or balenaEtcher. Check the `.sha512` first. Everything on the stick is erased. Tools that unpack files instead of copying the image do not give a bootable stick.
* **Install, repair, laptops, NVIDIA, logs for a bug report**: everything is in [docs/USER-GUIDE.md](docs/USER-GUIDE.md), in plain language.

## Build the ISO

The build runs Catalyst in Docker (about 45 GiB of free disk, 8 GiB of RAM for the container, several hours the first time). [docs/ISO.md](docs/ISO.md) has the pipeline, the flags and what is checked.

```sh
./scripts/build-iso.sh --check          # prerequisites and inputs, creates nothing
JOBS=4 ./scripts/build-iso.sh           # the release ISO into dist/
./scripts/build-iso.sh --stage2-only    # re-run only stage2 when stage1 is unchanged
./scripts/build-iso.sh --verify dist/gentoo-installer-amd64-*.iso
./scripts/build-iso.sh --sign-key 0xYOURKEYID   # also sign the checksums and the manifest
```

* The result is `dist/gentoo-installer-amd64-<stamp>.iso` with `.sha512`, `.blake2b`, `.manifest.json` and `.packages.txt`. Every build writes a timestamped log to `dist/logs/`.
* A following build reuses the stage1 of the previous one when nothing that goes into it changed (`--reuse auto`, the default). `--reuse none` always builds everything. Use it for a release when in doubt.
* The finished ISO is checked before it is published (`scripts/verify-iso.py`, rules in `iso/image-contract.json`): boot images, boot menu, initramfs, services, the repair and text-mode files, ownership, and no test access in a release.
* Settings: `JOBS`, `LOAD_AVERAGE`, `BUILD_MEMORY` (container memory, default 8g), `BUILD_MIN_FREE_GIB`, `ISO_CACHE_DIR`, `ISO_DIST_DIR`, `ISO_LOG_DIR`, `ISO_SECCOMP`. `--clean` removes retained chroots and old stage1 archives.
* The container needs `SYS_ADMIN`, `NET_ADMIN`, `MKNOD` and `SYS_CHROOT` and an open seccomp profile: the build runs the Gentoo ebuilds of about 500 packages as root inside the container. Build untrusted changes in a throwaway VM.

## Requirements and tests

```sh
python3 -m venv --system-site-packages .venv && .venv/bin/pip install -e '.[dev]'   # once, from this directory
scripts/check.sh            # the quality gate: syntax, mypy against a baseline, the fast tests
scripts/check.sh --full     # also the slow tests, cargo test and clippy, pkgcheck, the ISO verifier tests
scripts/test.sh             # the Python tests, the PortageHub Rust tests and the HWMonux smoke check
```

Tests that check packages against a Gentoo tree need `ISO_TREE=<path to the pinned tree>`. `ISO_RUN_PKGCHECK=1` also runs pkgcheck on a copy of the overlay. The ISO tests (`tests/iso`) build no ISO: they check the specs, the boot menu, the overlay, the build script with a stub `docker` and a synthetic image against the rules the real ISO must pass.

### Test ISO and VM checks

The VM harness logs into the live system over SSH, so it needs an image with a temporary root key. Such an image is named and labelled `...-TESTKEY` (volume `GENTOO_INSTALLER_TESTKEY`), shows a "TEST BUILD" banner, and is refused in `dist/` and in any directory below it (a release is taken from `dist/` as a whole):

```sh
install -d -m 0700 build/vm-control
testkey="build/vm-control/iso-test-$(date +%s)"
ssh-keygen -q -t ed25519 -N '' -f "$testkey"
ISO_DIST_DIR=build/test-dist JOBS=2 ./scripts/build-iso.sh --test-key-file "$testkey.pub"
python3 scripts/test-vm.py --scenario openrc --iso build/test-dist/gentoo-installer-amd64-20261018T124555Z-TESTKEY.iso \
  --ssh-host 127.0.0.1 --ssh-port 40222 --ssh-private-key "$testkey"
```

Never hand a `-TESTKEY` image to anybody: whoever has the key can log in as root. Remove the key pair when you are done. Build the test ISO from the same tree as the harness: an older ISO rejects a newer settings file. [docs/TESTING.md](docs/TESTING.md) describes the scenarios (disks, firmware, network cards, laptop identities, LUKS, rescue), the memory rules, the KVM and software-emulation timeouts and the known gaps. The harness creates a new qcow2 disk under `build/vm`, never touches a host block device, and forwards SSH on loopback only. If `GENTOO_TEST_SSH_PUBLIC_KEY` is exported in your shell, `build-iso.sh` uses it silently: unset it before a normal build.

## Source entry points

* GUI application and pages: `src/gentoo_installer/app.py`, `src/gentoo_installer/pages/`
* Manifest and GUI-to-daemon client: `src/gentoo_installer/manifest.py`, `src/gentoo_installer/backend.py`
* Daemon protocol and installation flow: `src/gentoo_installerd/server.py`, `src/gentoo_installerd/install.py`
* Storage and system configuration: `src/gentoo_installerd/storage.py`, `src/gentoo_installerd/configure.py`
* Hardware detection and planning: `src/gentoo_installer/hardware.py`, `src/gentoo_installerd/hwplan.py`
* Repair tool (`gentoo-rescue`): `src/gentoo_installerd/rescue/`
* Shared interfaces: [docs/contracts.md](docs/contracts.md), [docs/protocol.md](docs/protocol.md)
* ISO build inputs: `scripts/build-iso.sh`, `iso/builder-entrypoint.sh`, `iso/specs/`, `iso/root_overlay/`, `iso/cd_overlay/`, `iso/image-contract.json`; packages in `packaging/gentoo-overlay`
* ISO acceptance: `scripts/verify-iso.py`; VM harness and GUI driver: `scripts/test-vm.py`, `scripts/vm-gui-install.py`
* Documentation: [docs/USER-GUIDE.md](docs/USER-GUIDE.md) (users), [docs/ISO.md](docs/ISO.md) (build pipeline), [docs/TESTING.md](docs/TESTING.md) (tests and VM harness)
