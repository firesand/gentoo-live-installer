# User guide: installing Gentoo on another PC, and repairing a system that does not start

This guide is for people who got the installer disc and want to use it on their own PC or laptop. It assumes no Gentoo knowledge. The build side (how the disc is made) is in [ISO.md](ISO.md).

Contents: [what you need](#1-what-you-need) · [write the disc](#2-write-the-disc-to-a-usb-stick) · [start it](#3-start-the-disc) · [if it does not start](#4-if-the-disc-does-not-start-or-the-screen-stays-black) · [network, keyboard, clock](#5-network-keyboard-and-clock) · [install Gentoo](#6-install-gentoo) · [laptops and other special cases](#7-laptops-dual-boot-and-special-hardware) · [repair a system](#8-repair-a-system-that-does-not-start) · [manual rescue cheat sheet](#9-manual-rescue-cheat-sheet-gentoo-debian-ubuntu-fedora-arch) · [troubleshooting](#10-troubleshooting-table) · [limits](#11-what-the-disc-cannot-do)

## 1. What you need

* A 64-bit PC or laptop (Intel or AMD). Not Apple Silicon Macs, Windows-on-ARM laptops or Raspberry Pi boards.
* A USB stick of at least 8 GB (the image is between 2 and 3 GB) that you can erase, or a DVD-R/DL drive.
* For an installation: a wired network if you can (Wi-Fi works too), power for a laptop, and time. With the recommended choices most packages are downloaded as binaries; a desktop installation still takes from half an hour to several hours depending on the PC and the network.
* For an installation that keeps Windows: a backup, and the BitLocker recovery key if Windows uses BitLocker.
* **Secure Boot turned off** (section 3).

The disc does not change anything on your disks until you start an installation or a repair, and the installer shows the disk it will erase before it does anything.

## 2. Write the disc to a USB stick

Download the `.iso` and its `.sha512`, then check it:

```sh
sha512sum -c gentoo-installer-amd64-*.iso.sha512
```

Write the image to the **whole** stick, not to a partition. Everything on the stick is erased.

* Linux or macOS: find the stick with `lsblk` (Linux) or `diskutil list` (macOS), make sure you have the right device, then

  ```sh
  sudo dd if=gentoo-installer-amd64-*.iso of=/dev/sdX bs=4M status=progress conv=fsync
  ```

  (`/dev/sdX` is the stick, for example `/dev/sdb`, never a partition such as `/dev/sdb1` and never your system disk.)
* Windows: Rufus, and when it asks, choose **DD Image mode**; or balenaEtcher.
* Programs that unpack the files onto the stick instead of copying the image (the "ISO mode" of some tools) give a stick that does not boot. Ventoy has not been tested with this disc.

## 3. Start the disc

### Turn Secure Boot off

The boot loader of this disc is not signed with a key that PC firmware trusts. With **Secure Boot on** the firmware refuses it before anything of ours runs ("Secure Boot violation", "Invalid signature detected", "Selected boot image did not authenticate"), or it quietly skips the stick. Turn it off:

1. Restart and press the firmware setup key right after power on:

   | Maker | Setup | One-time boot menu |
   |---|---|---|
   | Dell | F2 | F12 |
   | HP | Esc, then F10 | Esc, then F9 |
   | Lenovo | F1 or F2 (ThinkPad: Enter, then F1) | F12 |
   | ASUS | F2 or Del | F8 or Esc |
   | Acer | F2 | F12 (enable it in setup first) |
   | MSI | Del | F11 |
   | Microsoft Surface | hold Volume Up while pressing the power button | hold Volume Down |

2. Find **Secure Boot** (usually under Security, Boot or Authentication) and set it to **Disabled**. Some firmware first needs an administrator password, or the setting "OS type: Windows UEFI" changed to "Other OS".
3. Save and exit (usually F10).

On a PC with **Windows BitLocker or "Device encryption"**: look up the recovery key before you change firmware settings (<https://aka.ms/myrecoverykey>); Windows may ask for it at the next start. Secure Boot can be turned on again later; only this disc needs it off. Intel Macs with a T2 chip: in the Startup Security Utility choose "No Security" and allow external media (the internal SSD of these Macs is not visible to this Linux).

### Boot from the stick

Open the one-time boot menu (table above) and choose the stick. If the menu lists it twice, choose the entry that starts with **UEFI**; use the plain one only on an old PC with a classic BIOS.

The disc's own menu appears. The first entry starts by itself after 10 seconds:

| Entry | When to use it |
|---|---|
| **Gentoo installer and rescue desktop** | The normal choice. |
| Desktop in RAM | Copies the disc to memory first (needs 6 GiB of RAM). Then you can remove the stick, and everything runs faster. |
| Text console only | No desktop. For repairs, headless machines, or when the desktop does not appear. Asks for a keyboard layout first. |
| More options | Verbose boot, compatibility mode, serial console, old firmware, IOMMU off, text console in RAM (4 GiB), accessibility (speech). |
| Memtest86+ | Tests the memory. |
| UEFI firmware settings | Opens the firmware setup (UEFI only). |
| Continue with the next boot device | Leaves the disc and starts what is on the PC's disk (UEFI only). |

To try a kernel option for one boot, press `e` on an entry, change the line that starts with `linux`, and press Ctrl+X.

## 4. If the disc does not start or the screen stays black

| What you see | What to do |
|---|---|
| The firmware shows an error about signatures, or boots something else | Secure Boot is on (section 3). |
| The stick is not in the boot menu | Write it again with `dd` or Rufus in DD mode; try another USB port (a USB 2 port is more reliable than USB 3 on some PCs); on a very old PC enable "Legacy boot / CSM". |
| The menu appears, then a black screen | Wait a minute (USB sticks are slow). Then restart and choose **Text console only**. Log in as `gentoo` (no password). New graphics chips sometimes need a newer kernel than the disc has. |
| The PC freezes or loses the USB stick or NVMe disk during boot | More options, **Compatibility mode** (switches off PCIe, NVMe and USB power saving; the screen stays a text console), or **Old firmware**. |
| It is very slow | Use **Desktop in RAM**, or a USB 3 stick in a USB 3 port. |
| A message asks to "Load keymap" | That is the text console entry; press Enter for the default. |

## 5. Network, keyboard and clock

* **Wired** networks connect by themselves. For **Wi-Fi** click the network icon in the panel, or run `nmtui` in a terminal. Hotel and campus networks with a login page: the browser button in the panel opens the text browser Links, which handles simple login pages.
* **Keyboard layout**: the desktop starts with a US layout. Change it in *Settings > Keyboard > Layout* or with the layout button in the panel *before* you type a disk password. On the text console: `sudo loadkeys de` (or `fr`, `uk`, ...).
* **Clock**: PCs that share Windows keep local time in the hardware clock, so the live clock can be hours off until the network is up; `chronyd` then corrects it by itself. The live system never writes to the hardware clock.

## 6. Install Gentoo

The installer starts with the desktop (or run `gentoo-installer`). It has eight steps. **The disk you choose is erased** (in guided mode); you confirm by typing its name on the Review page. The disc you booted from is never offered.

1. **Welcome**.
2. **Readiness**: the installer checks the live system, the network and the tools. Warnings can be ignored, errors cannot.
3. **Storage**: *Guided* creates a complete layout on one disk (boot partition, optional swap, root) and erases that disk. *Manual* lets you assign partitions you made yourself (use GParted from the applications menu first). Further down: encryption and advanced storage (LUKS, LVM, RAID). Pick the right disk by size and model; on a PC with two SSDs write them down before you start.
4. **Base system**: stage3 (OpenRC or systemd), Portage profile, desktop environment, kernel, initramfs and boot loader. The preselected choices (a binary kernel, GRUB, a desktop profile) are the quickest and the most tested; change them only when you know why.
5. **Customization**: Portage settings, network configuration and optional applications.
6. **Accounts**: your user, passwords, language, time zone and keyboard.
7. **Review**: nothing has been changed yet. Read the summary, especially the target disk, and type the disk's name to confirm.
8. **Install**: follow the log. Keep the live session running, keep a laptop on power, and do not close or restart the installer. The disc prevents suspend (lid, idle) while the live system runs.

When it finishes, restart, remove the stick, and boot the new system. If the new system does not start, boot the disc again and use the repair tool (section 8).

If an installation fails, the installer shows the failing step. The log is in `/var/lib/gentoo-installer/install.log` (read it with `sudo less`), the daemon's own messages in `/var/log/gentoo-installerd.log`.

## 7. Laptops, dual boot and special hardware

* **Laptops**: plug in the charger. Closing the lid does not suspend the live system. Wi-Fi and Bluetooth need firmware that the disc includes for most chips; Broadcom chips that only work with the proprietary `wl` driver need Ethernet or phone tethering. Fingerprint readers, IR cameras and some touch-screens do not work in the live session. Laptops with two GPUs run the live desktop on the GPU that the firmware starts with.
* **Windows on the same disk**: in Windows, shrink the Windows partition with Disk Management first, switch off *Fast Startup* (`powercfg /h off` in an administrator terminal) and shut down completely (not hibernate); otherwise Windows' file systems are left "dirty" and Linux cannot touch them safely. Back up first. Use *Manual* storage in the installer, keep the existing EFI system partition and do not format it.
* **No disk is shown**: the firmware may be in Intel RST/"RAID" mode, which hides NVMe/SATA disks from some systems. The kernel on this disc supports Intel VMD, but if no disk appears, switch the SATA mode to **AHCI** in the firmware (Windows may then fail to boot until you start it once in safe mode, so ask yourself whether you need to).
* **Disks with 4K sectors, NVMe-only machines, servers with RAID controllers**: the common storage controllers (AHCI, NVMe, Intel VMD, mpt3sas, megaraid, hpsa, smartpqi, aacraid) have drivers in the live kernel; hardware RAID volumes appear as one disk.
* **Older or unusual PCs**: try *More options > Old firmware* (turns off ACPI and APIC) only if the normal entry hangs.

## 8. Repair a system that does not start

Start the disc (any entry; *Text console only* if the desktop does not work). Open **Repair an Existing System** in the applications menu, or run `gentoo-rescue` in a terminal. The tool:

1. looks at all disks and finds installed Linux systems (Gentoo, Debian, Ubuntu, Fedora, Arch and others), without mounting anything for writing;
2. asks for the passphrase of encrypted disks (set the keyboard layout first); the live system does not activate volume groups or RAID arrays by itself, the tool does it for the ones you select;
3. mounts the system **read-only** first, reads it (fstab, boot loader, kernels, logs) and tells you what looks wrong;
4. offers repairs as a plan that you review before anything is written: regenerate the boot configuration, reinstall the boot loader, repair a boot entry (UEFI NVRAM), rebuild the initramfs, fix `fstab`, reset a password, or open a shell inside the system;
5. unmounts everything cleanly when you are done.

It never formats or repartitions a disk. Programs of the system you repair run with full rights while you are in the chroot, so a damaged system can still do damage; if you do not trust the system (malware), use the read-only inspection and copy your data out instead.

To copy your files out of a system that cannot start: mount it with the tool (or section 9), plug in an external disk, and copy with `rsync -a --info=progress2 /mnt/rescue/root/home/you/ /media/external/`. For a dying disk use `ddrescue` to copy the whole disk first, then work on the copy; check health with `sudo smartctl -a /dev/sdX` or `sudo nvme smart-log /dev/nvme0`.

## 9. Manual rescue cheat sheet (Gentoo, Debian, Ubuntu, Fedora, Arch)

Use this when you prefer the shell, or for a system the tool does not handle. Replace the device names with yours (`lsblk -f` shows file systems, labels and UUIDs). Run the commands as the live user with `sudo`.

**Unlock and assemble the storage**

```sh
lsblk -f                                        # find the root, boot and EFI partitions
sudo cryptsetup open /dev/nvme0n1p3 cryptroot   # LUKS: asks for the passphrase, creates /dev/mapper/cryptroot
sudo vgscan && sudo vgchange -ay                # LVM: volume groups are not activated automatically on this disc
sudo mdadm --assemble --scan --config=none      # RAID: or name the members: mdadm --assemble /dev/md0 /dev/sda2 /dev/sdb2
```

**Mount it** (look first with `-o ro` if you are not sure what is wrong; add subvolume options for Btrfs, for example `-o subvol=@`)

```sh
sudo mkdir -p /mnt/rescue/root
sudo mount /dev/mapper/cryptroot /mnt/rescue/root
sudo mount /dev/nvme0n1p2 /mnt/rescue/root/boot            # only if /boot is separate
sudo mount /dev/nvme0n1p1 /mnt/rescue/root/boot/efi        # the EFI system partition; on some systems it is /efi
for d in dev proc sys run; do
  sudo mount --rbind /$d /mnt/rescue/root/$d && sudo mount --make-rslave /mnt/rescue/root/$d
done
sudo chroot /mnt/rescue/root /bin/bash                     # use /bin/sh if there is no bash
```

Inside the chroot `efibootmgr` works when the live disc was started in UEFI mode (`ls /sys/firmware/efi` exists). When you are done: `exit`, then `sudo umount -R /mnt/rescue/root`. Do not run `rm -rf` inside the chroot while these mounts exist.

**Rebuild the boot loader and initramfs**

| System | Commands inside the chroot |
|---|---|
| Debian, Ubuntu, Mint | `update-initramfs -u -k all` · `update-grub` · BIOS: `grub-install /dev/sda` · UEFI: `grub-install --target=x86_64-efi --efi-directory=/boot/efi` · unfinished updates: `dpkg --configure -a && apt -f install` |
| Fedora, RHEL, Alma, Rocky | `dracut --force --regenerate-all` · `grub2-mkconfig -o /boot/grub2/grub.cfg` · UEFI: `dnf reinstall shim-x64 grub2-efi-x64` (do not run `grub2-install` on UEFI) · after editing files on an SELinux system: `touch /.autorelabel` |
| Arch, Manjaro | `mkinitcpio -P` · `grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=GRUB` · `grub-mkconfig -o /boot/grub/grub.cfg` |
| Gentoo | `ls /boot /lib/modules` · initramfs: `dracut --force --kver <version>` (or `genkernel initramfs`) · `grub-install --target=x86_64-efi --efi-directory=/efi --bootloader-id=Gentoo` (BIOS: `grub-install /dev/sda`) · `grub-mkconfig -o /boot/grub/grub.cfg` |
| Any | A forgotten password: `passwd root` (or `passwd youruser`). A wrong disk in `/etc/fstab`: compare with `blkid`, edit the UUIDs, check with `findmnt --verify`. |

**UEFI boot entry missing** (the firmware forgot the entry, or the disk was moved): `efibootmgr -v` lists them. Some firmware only looks at the fallback path, so copying the loader there makes the disk bootable anywhere: `mkdir -p /boot/efi/EFI/BOOT && cp /boot/efi/EFI/<name>/grubx64.efi /boot/efi/EFI/BOOT/BOOTX64.EFI` (on Fedora and Ubuntu the loader to copy is `shimx64.efi`). A new entry: `efibootmgr --create --disk /dev/nvme0n1 --part 1 --loader '\EFI\<name>\grubx64.efi' --label "Linux"`.

**File system problems** (the file system must **not** be mounted; for encrypted disks unlock first): look before repairing, `sudo e2fsck -n /dev/sdXN` (ext4), `sudo btrfs check --readonly /dev/sdXN`, `sudo xfs_repair -n /dev/sdXN`; repair only after a backup (`ddrescue` for a failing disk): `sudo e2fsck -f /dev/sdXN`, `sudo xfs_repair /dev/sdXN`. NTFS volumes that Windows left "dirty" (Fast Startup) are not repaired here: boot Windows and shut it down properly.

**Other tools on the disc**: `gparted` (partitions, graphical), `testdisk`/`photorec` (lost partitions and deleted files), `ddrescue`, `smartctl`, `nvme`, `hdparm`, `lshw`, `lspci`, `lsusb`, `dmidecode`, `mokutil --sb-state` (Secure Boot state), `efibootmgr`, `os-prober`, `cryptsetup`, `lvm`, `mdadm`, `btrfs`, `xfs_repair`, `fsck.f2fs`, `fsck.exfat`, `ntfsfix`.

## 10. Troubleshooting table

| Problem | Fix |
|---|---|
| Firmware refuses the stick | Secure Boot off (section 3). |
| A password is not accepted | The keyboard layout of the live system is not the one you used when you set the password; change it (section 5) and watch the characters in a visible field. |
| The installer cannot see Wi-Fi | The chip needs firmware that is not on the disc; use Ethernet, or tether a phone by USB. |
| Time is wrong (certificate errors) | Connect to the network; the clock is corrected within a minute. |
| The desktop does not appear | Boot *Text console only*; keep `lspci -nnk`, `dmesg` and `/var/log/messages` for your report (there is no journal on this disc). |
| Installation stops at a package | The log shows it; most failures are network problems. Try again (the installer resumes); with the defaults, the binary packages are used. |
| Laptop battery runs low during an install | Plug in. Nothing can prevent a flat battery from stopping the install; the installer resumes on the same disk. |
| The installed system boots to a GRUB prompt | Boot the disc, use the repair tool or section 9 (boot loader). |

When you report a problem, include: the PC model, `sudo dmidecode -t system`, `sudo lspci -nnk`, `dmesg`, which menu entry you used, and the log files named in section 6.

## 11. What the disc cannot do

* Boot with Secure Boot on, or on non-x86 machines.
* Show a desktop on a GPU whose kernel driver the live kernel (6.18) lacks. A generic fallback (`simpledrm`) is not in the kernel; use the text console.
* Repair or install ZFS roots, or read BitLocker volumes (Windows).
* Replace a backup. Repairs and installations can destroy data; read what the tool shows before you confirm.
