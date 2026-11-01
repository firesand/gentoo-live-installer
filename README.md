# Gentoo LiveCD Installer

A live USB/DVD image that does these jobs on almost any 64-bit PC or laptop:

* **Install Gentoo** with a guided desktop wizard (`gentoo-installer`), or **in text mode** when the desktop does not start (`gentoo-installer-text`).
* **Repair a Gentoo system that does not start** (`gentoo-rescue`). The disc finds the Gentoo systems on your disks, asks for disk passwords, mounts everything read-only first and offers repairs for the boot loader, fstab, initramfs and kernel. It repairs Gentoo only: any other Linux it finds is listed as "Not a Gentoo system - this repair tool only supports Gentoo" and is neither opened nor changed.
* **Look at the hardware** and set the installation up for it: graphics (the NVIDIA driver of your choice), firmware, power, disks, laptop and vendor tools (Razer, ASUS ROG), an optional desktop of your choice and optional gaming.
* Optionally, **repair with an AI assistant**: the boot entry *AI rescue (text console)* mounts your Gentoo system read-only and lets Claude Code, Codex or OpenCode (downloaded on first use, never bundled) help you diagnose it. Every command that needs root is approved by you, one by one, on the PC's own console. Read [docs/RESCUE-AI.md](docs/RESCUE-AI.md) first: it explains the risks and what is not tested.

The installer follows the official Gentoo Handbook for AMD64 and links to it. SSH on the installed system is on by default (root can never log in over SSH). Wi-Fi can be set up inside the installer and is carried over to the new system. [docs/USER-GUIDE.md](docs/USER-GUIDE.md) explains all of this for people who are new to Gentoo.

The repository holds the wizard and the root daemon (`src/`), the ISO build (`iso/`, `scripts/build-iso.sh`), the Portage overlay (`packaging/`), four companion apps (`apps/portagehub`, `apps/hwmonux`, `apps/gentoo-settings`, `apps/kernelsmith`) and the disposable-VM test harness (`scripts/test-vm.py`). Nothing here claims a tested install on real hardware: [docs/TESTING.md](docs/TESTING.md) lists what has run and what has not.

## Before you boot it: Secure Boot

The disc starts with **Secure Boot** on or off. With Secure Boot on, the first start on a PC shows blue screens once, because the PC has to learn the key of this disc:

1. "Verification failed": press Enter. "Press any key to perform MOK management": press a key within 10 seconds.
2. Choose **Enroll key from disk**, then **GENTOO_EFI**, then `ENROLL_THIS_KEY_IN_MOKMANAGER.cer`.
3. **Continue**, **Yes**, **Reboot**. The disc then starts on that PC like any other.

The first program on the disc is Fedora's shim, signed by Microsoft. The boot menu (GRUB) and Linux are signed with a key made for this one version of the disc. Its private half is destroyed when the build ends, so every version needs this step once. "View key 0" shows a fingerprint that must match the `.secureboot.txt` file published next to the ISO.

If the PC still refuses the stick ("Secure Boot Violation"), it trusts only Windows: turn on "Allow Microsoft 3rd Party UEFI CA" in the firmware setup, or set Secure Boot to *Disabled* there (F2, Del, F10, F12 or Esc right after power on). On a PC that runs **Windows with BitLocker or "Device encryption"**, look up your recovery key first (<https://aka.ms/myrecoverykey>): changing Secure Boot can make Windows ask for it. The same text is on the disc as `SECURE-BOOT.txt`. The installed Gentoo starts with GRUB, which is not signed: turn Secure Boot off before its first start, or choose systemd-boot with Secure Boot signing ([docs/USER-GUIDE.md](docs/USER-GUIDE.md)).

## The boot menu

| Entry | Use it to |
|---|---|
| Gentoo installer and rescue desktop | Install with the wizard, or open the repair window. The default. |
| **Safe graphics** | The same desktop on a generic display driver, for a black screen or no picture. |
| **Repair a system (text console)** | Fix a Gentoo system that does not start. No desktop, works with any graphics chip. |
| **Install in text mode** | Install without a desktop. |
| **AI rescue (text console)** | Optional: the repair console plus an AI assistant that helps you diagnose. Downloaded on first use, read-only until you approve each root command. [docs/RESCUE-AI.md](docs/RESCUE-AI.md) |
| Desktop in RAM | Run from memory (needs 6 GiB), then remove the stick. |
| Text console only | A shell, for experts and headless machines. |
| More options | Verbose boot, compatibility mode, serial console, old firmware, IOMMU off, text console in RAM. Also Memtest86+, firmware setup, restart and power off. |

The text entries ask for a keyboard layout first (you type passwords in both tools), then start their tool by themselves after the login on the first console. You can also run `sudo gentoo-rescue` or `sudo gentoo-installer-text` yourself. `gentoo-chroot SESSION` opens a shell in a system you mounted with the repair tool.

## Hardware: what to expect

| | |
|---|---|
| **Works** | 64-bit x86 PCs, laptops and virtual machines. BIOS/CSM, 64-bit UEFI and 32-bit UEFI firmware (the ISO carries `bootx64.efi` and `bootia32.efi`). SATA/AHCI, NVMe (also behind Intel VMD), USB, SD/eMMC and virtio disks. Wi-Fi, Bluetooth and sound that the Linux 7.2 kernel and `linux-firmware` support. Intel, AMD and (with the open nouveau driver) NVIDIA graphics. The proprietary NVIDIA driver is installed by *Automatic* once you tick its licence (open kernel modules on Turing and newer, branch 580 on Maxwell, Pascal and Volta); otherwise nouveau. |
| **Needs a step from you** | Secure Boot: one blue screen on the first start of the disc (above), and off before the first start of an installed system with GRUB, the default (docs/USER-GUIDE.md section 3). If the disk does not show up, switch the firmware's SATA mode from Intel RST/"RAID" to AHCI (mind Windows). A wrong clock is corrected once the network is up; the live system does not change a hardware clock that keeps local time (Windows). A disk that reports no serial number (a virtual disk, some USB adapters) must be approved in the installer's storage step: it is then recognised by its hardware path, size and model. |
| **Not supported** | 32-bit CPUs, ARM machines (Apple Silicon Macs, Windows-on-ARM laptops, Raspberry Pi), Intel Macs with a T2 chip (the internal SSD is not visible), ZFS root, speech output or braille (the disc has no screen reader). |
| **Known limits** | A GPU that is too new for the live kernel gets no accelerated desktop. The **Safe graphics** boot entry (`nomodeset`) then starts the desktop on the generic display driver (`simpledrm`, which the live kernel is built with: no 3D, but it works on almost every PC), and the text entries need no graphics at all ([docs/ISO.md](docs/ISO.md) section 9). The image is built from a fixed snapshot (Linux 7.2.13, Gentoo tree 2026-10): hardware released after it may lack drivers. Real-hardware boots are not part of the automated tests, only QEMU with BIOS and UEFI firmware. HWMonux, an optional program of the installed system, needs PySide6, which is compiled from source: about 40 minutes and up to 4 GiB of memory per compile job. On a PC with less than 8 GiB the installer says so and leaves the choice to you. |

The live session never mounts or activates the PC's disks, volume groups or RAID arrays by itself. It ignores the lid switch and idle timers, so an installation or repair is not interrupted.

## Using the disc

* **Write it** to a USB stick (at least 8 GB) with `dd`, `caligula`, Rufus in *DD image* mode or balenaEtcher. Check the `.sha512` first. Everything on the stick is erased. Tools that unpack files instead of copying the image do not give a bootable stick.
* **Another PC or laptop than yours?** The installer reads the hardware of the computer it runs on and sets the new system up for it (graphics, firmware, power, disks). If the disk will move to other computers, choose *A portable disk* on the Hardware tab of the Customization step.
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

* The result is `dist/gentoo-installer-amd64-<release>-<stamp>.iso` with `.sha512`, `.blake2b`, `.manifest.json`, `.packages.txt`, `.secureboot.cer` and `.secureboot.txt`. Every build writes a timestamped log to `dist/logs/`.
* A following build reuses the stage1 of the previous one when nothing that goes into it changed (`--reuse auto`, the default). Otherwise it builds stage1 again; the binary package cache makes that shorter. `--reuse none` always builds everything. Use it for a release when in doubt.
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
python3 scripts/test-vm.py --scenario openrc --iso build/test-dist/gentoo-installer-amd64-rc4-20261018T124555Z-TESTKEY.iso \
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
* AI rescue stick (`gentoo-rescue-ai`, `-do`, `-mount`, `-share`, `-agents`, `-login`): `src/gentoo_rescue_ai/`
* Shared interfaces: [docs/contracts.md](docs/contracts.md), [docs/protocol.md](docs/protocol.md)
* ISO build inputs: `scripts/build-iso.sh`, `iso/builder-entrypoint.sh`, `iso/specs/`, `iso/root_overlay/`, `iso/cd_overlay/`, `iso/image-contract.json`; packages in `packaging/gentoo-overlay`
* ISO acceptance: `scripts/verify-iso.py`; VM harness and GUI driver: `scripts/test-vm.py`, `scripts/vm-gui-install.py`
* Documentation: [docs/USER-GUIDE.md](docs/USER-GUIDE.md) (users), [docs/RESCUE-AI.md](docs/RESCUE-AI.md) (the AI rescue stick), [docs/ISO.md](docs/ISO.md) (build pipeline), [docs/TESTING.md](docs/TESTING.md) (tests and VM harness)
