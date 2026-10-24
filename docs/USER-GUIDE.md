# User guide: install Gentoo on your PC or laptop, and repair a system that does not start

This guide is for people who have the installer disc and want to use it on their own PC or laptop. You do not need to know Gentoo. The build side (how the disc is made) is in [ISO.md](ISO.md).

Contents: [what you need](#1-what-you-need) · [write the disc](#2-write-the-disc-to-a-usb-stick) · [start it: Secure Boot, UEFI and BIOS](#3-start-the-disc) · [if it does not start](#4-if-the-disc-does-not-start-or-the-screen-stays-black) · [network, keyboard, clock](#5-network-keyboard-and-clock) · [install Gentoo](#6-install-gentoo-with-the-desktop-wizard) · [install in text mode](#7-install-in-text-mode) · [laptops, graphics and special hardware](#8-laptops-graphics-and-special-hardware) · [repair a system](#9-repair-a-system-that-does-not-start) · [repair by hand](#10-repair-by-hand-cheat-sheet) · [logs for a bug report](#11-logs-and-diagnostics-for-a-bug-report) · [troubleshooting](#12-troubleshooting-table) · [limits](#13-what-the-disc-cannot-do)

## 1. What you need

* A 64-bit PC or laptop (Intel or AMD). Not Apple Silicon Macs, Windows-on-ARM laptops or Raspberry Pi boards.
* A USB stick of at least 8 GB that you can erase. The image is between 2 and 3 GB. A blank DVD also works.
* Memory: 2 GiB or more is recommended for an installation. The installer warns below 2 GiB and refuses below 1 GiB. The boot entry "Desktop in RAM" needs 6 GiB, "Text console in RAM" needs 4 GiB. With less than 8 GiB the optional hardware monitor HWMonux is slow and heavy to install (step 5 of section 6).
* For an installation: a wired network if you can (Wi-Fi works too), power for a laptop, and time. Most packages come as binaries. Even so, a desktop installation takes from half an hour to several hours.
* Disk space for the new system: 8 GiB for a minimal system, 14 GiB for Xfce, 18 GiB for GNOME, 24 GiB for KDE Plasma. More if you compile packages yourself. The installer shows what it needs.
* For an installation that keeps Windows: a backup, and the BitLocker recovery key if Windows uses BitLocker.
* **Secure Boot turned off** (section 3).

The disc does not change anything on your disks until you start an installation or a repair. The installer shows the disk it will erase before it does anything.

## 2. Write the disc to a USB stick

Download the `.iso` and its `.sha512`. Check the download:

```sh
sha512sum -c gentoo-installer-amd64-*.iso.sha512
```

Write the image to the **whole** stick, not to a partition. Everything on the stick is erased.

* Linux or macOS: find the stick with `lsblk` (Linux) or `diskutil list` (macOS). Make sure you have the right device. Then:

  ```sh
  sudo dd if=gentoo-installer-amd64-*.iso of=/dev/sdX bs=4M status=progress conv=fsync
  ```

  `/dev/sdX` is the stick, for example `/dev/sdb`. Never a partition such as `/dev/sdb1`. Never your system disk.
* Windows: Rufus. When it asks, choose **DD Image mode**. Or use balenaEtcher.
* Do not use "ISO mode" or tools that unpack the files onto the stick. That stick does not boot. Ventoy has not been tested with this disc.

A release can also come with signatures (`.sha512.asc` and `.manifest.json.asc`). Check them with `gpg --verify` and the public key of the maintainer.

## 3. Start the disc

### Turn Secure Boot off

The boot loader of this disc is not signed with a key that PC firmware trusts. With **Secure Boot on**, the firmware refuses it before anything of ours runs. You see "Secure Boot violation", "Invalid signature detected" or "Selected boot image did not authenticate". Or the firmware quietly skips the stick. Turn Secure Boot off:

1. Restart and press the setup key right after power on:

   | Maker | Setup | One-time boot menu |
   |---|---|---|
   | Dell | F2 | F12 |
   | HP | Esc, then F10 | Esc, then F9 |
   | Lenovo | F1 or F2 (ThinkPad: Enter, then F1) | F12 |
   | ASUS | F2 or Del | F8 or Esc |
   | Acer | F2 | F12 (enable it in setup first) |
   | MSI | Del | F11 |
   | Microsoft Surface | hold Volume Up while pressing the power button | hold Volume Down |

2. Find **Secure Boot** (under Security, Boot or Authentication). Set it to **Disabled**. Some firmware first needs an administrator password. Some need "OS type: Windows UEFI" changed to "Other OS".
3. Save and exit (usually F10).

On a PC with **Windows BitLocker or "Device encryption"**: look up the recovery key before you change firmware settings (<https://aka.ms/myrecoverykey>). Windows may ask for it at the next start. Intel Macs with a T2 chip: in the Startup Security Utility choose "No Security" and allow external media. The internal SSD of these Macs is not visible to this Linux.

### Secure Boot and the system you install

Only the live disc needs Secure Boot off. What happens to the new system depends on its boot loader:

* **GRUB (the default)** does not work with Secure Boot. The PC will not start it while Secure Boot is on. Leave Secure Boot off on that PC, or choose another boot loader.
* **systemd-boot or the EFI stub, with a Unified Kernel Image and your own signing key**, can work with Secure Boot. This is an advanced choice. You make the key and the certificate. The installer signs the boot files with them. You add the certificate to the firmware's key list (the installer says where it left the file). The proprietary NVIDIA driver cannot be combined with this, because its kernel modules would not be signed.

### UEFI or BIOS (CSM)?

PCs start in one of two modes. **UEFI** is the normal mode of PCs from about 2012 on. **BIOS** (also called Legacy or CSM) is the old mode. The disc starts in both. (It also starts on the few PCs with 32-bit UEFI firmware, mostly old tablets.)

* The installer installs the boot loader for the mode you started the disc in. The wrong mode gives a system that does not start.
* If Windows is installed on this PC, start the disc in the same mode as Windows. Windows on a GPT disk with an EFI partition means UEFI. Windows on an MBR disk means BIOS.
* If you are unsure, start in UEFI mode. The one-time boot menu often lists the stick twice. Choose the entry that starts with **UEFI**. Use the plain one only on an old PC with a classic BIOS.
* To check the mode in the live system, run `ls /sys/firmware/efi`. If the directory exists, the disc runs in UEFI mode. The installer shows the mode on its Readiness page.
* In BIOS mode the installer can use an MBR or a GPT partition table (GPT gets a small BIOS boot partition). A disk larger than 2 TiB needs GPT.

### Boot from the stick

Open the one-time boot menu (table above). Choose the stick. The disc's own menu appears. The first entry starts by itself after 10 seconds.

| Entry | When to use it |
|---|---|
| **Gentoo installer and rescue desktop** | The normal choice. You get a desktop with the installer wizard and the repair window. |
| Desktop in RAM | Copies the disc to memory first (needs 6 GiB). Then you can remove the stick, and everything runs faster. |
| **Repair a system (text console)** | No desktop. Starts the repair tool by itself. Use it to fix a system that does not start. It works with any graphics chip. It asks for a keyboard layout first. |
| **Install in text mode** | No desktop. Starts the text installer by itself. Use it when the desktop does not appear, or on a small or old machine. It asks for a keyboard layout first. |
| Text console only | No desktop and no tool: a shell for experts and headless machines. Asks for a keyboard layout first. |
| More options | Verbose boot, compatibility mode, serial console, old firmware, IOMMU off, text console in RAM (4 GiB). |
| Memtest86+ | Tests the memory. |
| UEFI firmware settings | Opens the firmware setup (UEFI only). |
| Continue with the next boot device | Leaves the disc and starts what is on the PC's disk (UEFI only). |

There is no entry for speech output or braille: the disc has no screen reader (section 13). To try a kernel option for one boot, press `e` on an entry. Change the line that starts with `linux`. Press Ctrl+X.

## 4. If the disc does not start or the screen stays black

| What you see | What to do |
|---|---|
| The firmware shows an error about signatures, or boots something else | Secure Boot is on (section 3). |
| The stick is not in the boot menu | Write it again with `dd`, or with Rufus in DD mode. Try another USB port (USB 2 is more reliable than USB 3 on some PCs). On a very old PC enable "Legacy boot / CSM". |
| The menu appears, then a black screen | Wait a minute (USB sticks are slow). Then restart and choose **Install in text mode** or **Repair a system (text console)**. They need no graphics driver. |
| The PC freezes or loses the USB stick or NVMe disk during boot | More options, **Compatibility mode** (switches off PCIe, NVMe and USB power saving; the screen is a text console), or **Old firmware**. |
| It is very slow | Use **Desktop in RAM**, or a USB 3 stick in a USB 3 port. |
| A message asks to "Load keymap" | That is a text console entry. Type one of the layout names shown on the screen (for example `de` or `fr`) or press Enter for the default. You have 10 seconds. |

**Why the desktop sometimes does not start.** The live kernel (Linux 6.18) has no generic framebuffer graphics driver (`simpledrm`). A graphics chip that is too new for this kernel therefore gets no desktop. `nomodeset` does not help: it gives a text console, not a desktop. The text mode entries are the way around this. They do not need graphics. [ISO.md section 9](ISO.md#9-known-limits-and-follow-ups) has the details and the planned fix.

## 5. Network, keyboard and clock

* **Wired** networks connect by themselves.
* **Wi-Fi** on the desktop: click the network icon in the panel, choose the network, type the password. In a terminal or on a text console: run `nmtui`. Choose "Activate a connection", choose the network, type the password. Or: `nmcli device wifi connect "Network name" password "the password"`.
* **Login pages** (hotel, campus): the browser button in the panel opens the text browser Links, which handles simple login pages. It cannot do pages that need JavaScript.
* **A network behind a proxy**: enter it on the Customization page (Network tab, *Download proxy*).
* **Keyboard layout**: the desktop starts with a US layout. Change it with the layout button in the panel or in *Settings > Keyboard > Layout* **before** you type a disk password. On a text console you choose the layout in the boot menu step. Later: `sudo loadkeys de` (or `fr`, `uk`, ...).
* **Clock**: PCs that share Windows keep local time in the hardware clock, so the live clock can be hours off until the network is up. `chronyd` then corrects it. The live system does not change a hardware clock that keeps local time. When the clock is wrong, the Readiness page offers **Set clock from network**. It waits up to 15 seconds, and it says so when no time server answered (some networks block them). Then set the date and time by hand with the clock of the desktop, and press **Check again**. The page also has a **Network settings** button. In the text installer press `t` on the "This computer" step.

## 6. Install Gentoo with the desktop wizard

The installer starts with the desktop. Or run `gentoo-installer`. It has eight steps. **In guided mode the disk you choose is erased.** You confirm by typing its name on the Review page. The disc you booted from is never offered.

1. **Welcome**: choose *Install Gentoo*. The button *Repair an existing system* opens the repair window instead (section 9).
2. **Readiness**: the installer checks the live system, the network, the tools and your hardware. Warnings can be ignored. Errors cannot. A hardware summary and advice appear here. You can save a diagnostics file (section 11).
3. **Storage**:
   * *Guided* makes a complete layout on one disk (boot partition, optional swap, root) and **erases that disk**.
   * *Manual* lets you use partitions you made yourself. Make them first with GParted (applications menu, or `sudo gparted` in a terminal; the live user has no password). Then choose which partition is the root, the boot partition and so on, and which ones to format. Use *Manual* to keep Windows or other systems.
   * Further down: encryption and advanced storage (LUKS, LVM, RAID).
   * Pick the right disk by size and model. If you have two SSDs, write them down before you start.
   * **A disk without a serial number must be approved.** Virtual disks and some USB adapters report no serial number, and two disks can report the same one. The installer cannot tell such a disk from another one. The page then shows *This disk has no serial number*. Check the model and the size. Then tick *I confirm that /dev/... is the disk to use*. From then on the installer knows the disk by its hardware path, its size and its model, and it refuses to go on if one of them changes. Without the tick you cannot continue. If the disk has no stable hardware path either, it cannot be used. (For a virtual machine, give the disk a serial number.)
   * **A disk that is in use asks for your consent.** The page shows *This disk is in use* when the disk, or a partition you format, holds active swap, an LVM volume group, a RAID array, or one half of a Btrfs or ZFS pool whose other half is on another disk. Erasing it destroys that data, and the data on the other disks of the same volume group, array or pool. Tick *Switch these volumes off and erase them together with the disk* to go on, or choose another disk. A disk with mounted partitions must be unmounted first.
   * In *Manual* mode, if you keep (do not format) the root partition, the installer asks **Install over the existing system?** before it starts. Say Yes only if the new system may go into that partition. If it already holds a Linux system, that system's `/etc`, its accounts and its boot files are replaced. A copy of `/etc` and of the SSH keys is saved in `/root` first. To repair that system instead, say No and use *Repair an existing system* (section 9).
4. **Base system**: stage3 (OpenRC or systemd), Portage profile, desktop (KDE Plasma, GNOME, Xfce or none), kernel, initramfs and boot loader. The preselected choices (a binary kernel, GRUB, a desktop profile) are the quickest and the most tested. Change them only when you know why.
5. **Customization** has four tabs. The installer fills in what it detected. Change it if you want something else.
   * **Hardware** comes first. It shows what the installer found on this computer (processor, memory, graphics, disks, firmware), advice for it, and the list *What the installer will set up*. Here you choose *This computer* (tuned for this hardware) or *A portable disk* (for a disk that will move to other computers: generic settings, all firmware, both microcode families and a fallback boot entry). You also choose the graphics driver, power management, compressed RAM swap, TRIM, hibernation and more. Most choices are on *Automatic*, and the page says what Automatic means on this computer. Set *Experience* (top right) to *Advanced* to see all of them.
   * **Portage**: the package mirror, binary packages, build jobs, compiler flags and USE flags.
   * **Network**: the network manager, DHCP or a static address, a download proxy and Wi-Fi.
   * **Applications & tools**: optional programs such as the HWMonux hardware monitor and the PortageHub package manager, and services such as printing, Bluetooth and SSH. **On a PC with less than 8 GiB of memory** a note appears under HWMonux: *HWMonux needs PySide6, which is compiled from source: about 40 minutes and up to 4 GiB of memory per compile job.* HWMonux stays ticked: the installer does not leave it out for you. Untick it if you do not need it, or if the PC is small and you do not want to wait.
6. **Accounts**: your user, passwords, language, time zone and keyboard. Tick **The clock keeps local time (for Windows dual boot)** if Windows stays on this PC. Windows keeps local time in the hardware clock and Linux normally keeps UTC, so without the tick the clock is wrong by some hours after you switch systems. The page suggests the tick when it finds Windows, or a hardware clock that already keeps local time. It never ticks the box for you.
7. **Review**: nothing has been changed yet. Read the summary, especially the target disk. Type the disk's name to confirm.
8. **Install**: follow the log. Keep the live session running. Keep a laptop on power. Do not close or restart the installer. The disc prevents suspend (lid, idle) while the live system runs.

You can import and export settings. A settings file never contains passwords.

When it finishes, restart, remove the stick, and boot the new system. If the new system does not start, boot the disc again and use the repair tool (section 9).

If an installation fails, the installer shows the failing step and the buttons **Try again**, **Resume installation** and **Save diagnostics…**. It resumes on the same disk. If a late step fails after the boot loader was installed, the installer says *Your new system is installed and can start*: restart now and fix the rest from the installed system, or press **Retry the desktop step**. If you cancel while the disk is being changed, only **Start over** is offered, because the disk is half done. If a repair session still holds the disks, the installer says it cannot start yet and nothing is changed (section 12). The log is in `/var/lib/gentoo-installer/install.log` (read it with `sudo less`). The daemon's own messages are in `/var/log/gentoo-installerd.log`.

## 7. Install in text mode

Use this when the desktop does not start, or on a PC with little memory. It installs the same system as the wizard.

1. Start the disc and choose **Install in text mode**. Choose your keyboard layout when the menu asks for it.
2. After the automatic login the text installer starts by itself. If it does not (for example after another boot entry), type: `sudo gentoo-installer-text`.
3. Answer the questions one at a time. They cover the main choices of the wizard: keyboard, network, disk, file system and encryption, desktop and init system, accounts, time zone and language. The text installer sets up the whole disk (guided mode), and it picks the hardware choices of the Hardware tab itself from what it finds. Read the summary at the end: it lists what was chosen for this computer, what happens to every disk, notes and advice. **The disk you choose is erased.** You confirm by typing its name.
   * The disk step shows what is on each disk. If something is in use on a disk (active swap, a volume group, a RAID array, half of a Btrfs or ZFS pool), it says so and asks *Switch them off and erase them together with the disk?* The default answer is No.
   * A disk without a serial number needs your approval: *Is this the disk to install on?*
   * If Windows seems to stay on the PC, it asks *Keep the hardware clock in local time?* (see section 8, *Windows on the same disk*).
   * On a PC with less than 8 GiB of memory it prints the HWMonux note of section 6, step 5. The text installer has no question for HWMonux. To leave it out, use the wizard, or a settings file with `"install_hwmonux": false`.
   * For your own partitions (manual mode) use the wizard, or a settings file.
4. When the installer ends you are at a normal shell. To run it again: `sudo gentoo-installer-text`. To leave: restart, remove the stick.

For scripts and test machines, the text installer can run without questions:

```sh
sudo gentoo-installer-text --manifest settings.json --dry-run                       # check the file and show the plan, change nothing
sudo gentoo-installer-text --manifest settings.json --yes --confirm-device /dev/vda  # install; the device name is the confirmation
```

`--confirm-device` also approves a disk that has no serial number. A disk that is in use needs `--release-holders`, which gives the consent that the questions ask for. Exit codes: 0 done, 1 the installation failed, 2 wrong usage or an invalid settings file, 3 refused (the confirmation is missing or wrong), 4 the installer service cannot be reached, 130 interrupted. `--json-events` prints the progress as JSON lines. A settings file saved by the wizard has no passwords. Add them in the file or answer when asked.

## 8. Laptops, graphics and special hardware

### Laptops

* Plug in the charger. Closing the lid does not suspend the live system, so an installation continues with the lid closed. Nothing can prevent a flat battery from stopping the install.
* **Wi-Fi and Bluetooth**: the disc includes the firmware for most chips. Broadcom chips that only work with the proprietary `wl` driver need Ethernet or phone tethering during the installation. See section 5 for how to connect.
* **Power saving** of the installed system: the installer can set up `power-profiles-daemon` or `tlp`, weekly TRIM for SSDs and compressed RAM swap. The defaults come from what it detects.
* **Hybrid graphics** (Intel or AMD plus NVIDIA): the live desktop runs on the GPU that the firmware starts with, usually the integrated one. The installer can set up PRIME render offload for the installed system. Then the NVIDIA card is used only for programs you start with `prime-run`, and the integrated GPU does the rest and saves battery.
* Fingerprint readers, IR cameras and some touch-screens do not work in the live session.
* Tablets without a keyboard have no on-screen keyboard on the disc.

### NVIDIA graphics

* **The default is the open driver (Nouveau)** with Mesa. It needs no licence and it is what the live session uses. This is the right choice for most people.
* **The proprietary driver is an option** in the installer's hardware choices (graphics driver: *NVIDIA proprietary*). It gives better performance in games and for GPU computing. It is not free software. You must accept its licence (called NVIDIA-2025 in Gentoo) with a check box. Without the check mark the installer does not install the driver.
* Which driver branch: **595** for GeForce GTX 16 series, RTX 20 series and newer. **580** (long-term) for Maxwell, Pascal and Volta cards (GeForce GTX 750 up to the 10 series, Titan V). Older cards (Kepler and before) work only with the open driver. The installer picks the branch for your card.
* The proprietary driver needs a Gentoo distribution kernel with Dracut (the preselected choice). It cannot be used with Secure Boot signing. If the driver cannot be installed, the installation goes on with the open driver and tells you.
* Later you can change the driver with the normal Gentoo tools (`x11-drivers/nvidia-drivers`; the Gentoo wiki explains them).

### Windows on the same disk

In Windows, shrink the Windows partition with Disk Management first. Switch off *Fast Startup* (`powercfg /h off` in an administrator terminal). Shut down completely (not hibernate). Otherwise Windows' file systems are left "dirty" and Linux cannot touch them safely. Back up first. Use *Manual* storage in the installer, keep the existing EFI system partition, and do not format it. Windows keeps local time in the hardware clock and Linux normally keeps UTC. Two systems that disagree show a clock that is wrong by some hours after you switch. Tick **The clock keeps local time (for Windows dual boot)** on the Accounts page, so both agree. The text installer asks the same as *Keep the hardware clock in local time?*

### Disks and controllers

* **No disk is shown**: the firmware may be in Intel RST or "RAID" mode, which hides NVMe or SATA disks from some systems. The kernel on this disc supports Intel VMD. If no disk appears, switch the SATA mode to **AHCI** in the firmware. Windows may then fail to boot until you start it once in safe mode. Think about whether you need that.
* **4K sectors, NVMe-only machines, servers with RAID controllers**: the common controllers (AHCI, NVMe, Intel VMD, mpt3sas, megaraid, hpsa, smartpqi, aacraid) have drivers in the live kernel. Hardware RAID volumes appear as one disk.
* The live system does not start LVM volume groups or RAID arrays by itself. The installer and the repair tool start exactly the ones you select. An existing volume group, RAID array, or half of a Btrfs or ZFS pool on the target disk is shown as *This disk is in use* before anything is erased, and you must agree to switch it off and erase it (step 3 of section 6).

### Older or unusual PCs

Try *More options > Old firmware* (turns off ACPI and APIC) only if the normal entry hangs. For a machine that freezes, try *Compatibility mode*.

## 9. Repair a system that does not start

Start the disc. The best entry is **Repair a system (text console)**: it works on every PC. On the desktop, open **Repair an Existing System** in the applications menu (or the button *Repair an existing system* on the installer's Welcome page). In a terminal, run `sudo gentoo-rescue`.

Need the USB port for an external disk? Choose *More options > Text console in RAM* (needs 4 GiB of memory). The disc is copied to memory, you can remove the stick, and you start the tool yourself with `sudo gentoo-rescue`.

### What the tool does

1. It looks at all disks and finds installed Linux systems. To look, it mounts read-only. It never writes at this stage.
2. It asks for the passphrase of encrypted (LUKS) disks. Set the keyboard layout first. The live system does not start volume groups or RAID arrays by itself. The tool does it for the ones you select.
3. It mounts the system **read-only** under `/mnt/rescue`, reads it (fstab, boot loader, kernels, logs) and tells you what looks wrong.
4. It offers repairs as a plan. You see the exact commands before anything is written. For risky repairs you type the name of the root device and confirm that you have a backup.
5. It unmounts everything cleanly when you finish, and tells you when you can remove the stick.

It never formats or repartitions a disk. The tool shows a guided menu on a text console. The first choice is *Fix everything recommended*: one plan that does what the diagnosis suggests, in the right order.

### What each repair does (Gentoo, Debian and Ubuntu, Fedora)

| Repair | Gentoo | Debian, Ubuntu, Mint | Fedora, RHEL, Alma, Rocky |
|---|---|---|---|
| Rebuild the initramfs | `dracut --force` (or `genkernel`) for the chosen kernels | `update-initramfs -u` (or `-c`) | `dracut --force` |
| Regenerate the boot menu | `grub-mkconfig` | `update-grub` | missing boot entries are added with `kernel-install`, then `grub2-mkconfig` |
| Reinstall the boot loader | `grub-install` for UEFI or BIOS, or `bootctl` for systemd-boot, then a new menu | `grub-install` with the Secure Boot chain when the system uses it, or reinstall the GRUB and shim packages; `bootctl` for systemd-boot | UEFI: reinstall the signed `shim` and GRUB packages with `dnf` (never `grub2-install` on UEFI), then a new menu and a firmware entry. BIOS: `grub2-install` |
| Reinstall the kernel | `emerge --config`, or a new `emerge` of the kernel package, then initramfs and menu | reinstall the kernel packages, then initramfs and menu | reinstall `kernel-core` and modules, then initramfs and menu |
| Finish an interrupted package job | `emerge --resume` | `dpkg --configure -a` and `apt -f install` | `rpm --rebuilddb` |

Repairs that work the same on every system:

* **Fix the disk list (fstab)**: finds the lines that point at disks that are not there, and proposes a fix for each one. If the tool is sure which disk is meant (the root you opened, the only swap area, the only EFI partition on the system's disk), it points the line at that disk by UUID. If not, it adds `nofail`, so the start does not wait for the disk. A line that already has `nofail` or `noauto`, or whose file system type is wrong (you correct that by hand), is left alone. Before anything is written you see the changes (a diff) and the lines the tool leaves alone, each with its reason. A backup copy of the old file is kept. The repair window shows this proposal, and you accept it or not as a whole. To choose line by line, use the terminal tool: `gentoo-rescue fstab show SESSION` lists the lines with their numbers, then `gentoo-rescue fstab fix SESSION --choose 3=replace:UUID=<uuid> --choose 5=comment`. The actions are `replace:UUID=<uuid>`, `nofail`, `comment` and `keep`.
* **Add, remove or reorder firmware boot entries** (UEFI): `efibootmgr`. Also: boot an entry once, and create the fallback file `EFI/BOOT/BOOTX64.EFI` so that firmware which forgot its entries can still find the system.
* **Check a file system**: a read-only check first. Repairs only when you ask, with the file system unmounted. Works for ext2/3/4, XFS, Btrfs, FAT, F2FS, exFAT, JFS and NTFS (NTFS only gets a check with `ntfsfix -n`).
* **Reset a password** or unlock an account in the repaired system.
* **Diagnosis** (changes nothing). It looks for, among other things:
  * a missing kernel, or a missing or empty initramfs;
  * a missing or empty boot menu;
  * an empty EFI partition, or a firmware with no working boot entry;
  * a BIOS or UEFI mismatch between the disc and the installed system;
  * a boot configuration that still has arguments of a live disc;
  * a full `/boot`;
  * a cloned disk with the same ID.

  Every finding says what it means and which repair helps.

A moved disk (a system now in another computer): choose *Rebuild the initramfs* with the option for many kinds of hardware. It adds the drivers for the new machine.

### What you may meet in the repair window

* **A disk that is already open.** The file manager of the desktop may have opened the disk of the system. The window then says where, and the tool does not touch it. Close it there (the eject button next to the disk, or `sudo umount DIRECTORY` in a terminal), then press **Check again**. The tool never unmounts something it did not open itself. The old option `gentoo-rescue mount --unmount-existing` was removed: it only hid this message, and the mount failed anyway.
* **Left open by an earlier repair.** If a repair was cut short (the window was closed, the service restarted, the power went), the system can stay open. The first page lists these under *Still open* (you can continue with them) and *Left open by an earlier repair* (no window can continue with them: disks still locked against writing, an unlocked disk, an active volume group). Press **Release them** to give them back. Only what the repair tool itself opened is released. If something else is mounted there, the page names it, and you unmount it yourself. In a terminal: `sudo gentoo-rescue cleanup`. The installer does not start while something is left open.
* **Disk names that can change.** A line of the system's disk list (fstab) can name a disk as `/dev/sda2`, or by the port it is plugged into (`by-path`), instead of by UUID. Such names change between starts and between computers: a USB stick can be `/dev/sda` in the live session. If the name now points at a partition on another disk than the system's own, the tool does not open it by itself. The mount plan lists the line under *Not found* and offers the partition under *Suggestions*. Press **Add** only if it is the right disk. In a terminal add `--mount MOUNTPOINT=DEVICE`. The diagnosis notes such names too: write UUIDs in their place (`blkid` shows them).
* **Change options.** On the page with the plan, **Change options** opens the options of the repair. *Rebuild the initramfs* lists the installed kernels: tick the ones to rebuild, or none for all of them. *Reinstall the kernel* lists them on Debian and Ubuntu (none ticked: the damaged kernel, or else the newest one). On Gentoo it asks for the kernel package and the method instead. In a terminal: `--param kernels=VERSION,VERSION`.
* **Read the journal.** The logs page shows the last lines of the text log files the system kept. Many systems keep their messages in a binary journal instead. For those, **Read the journal of the last boot…** runs the system's own `journalctl` as root, so the window asks first: tick *I trust this system and want to read its journal*. You can limit it to errors, or to kernel messages. The system must be open with repairs allowed (not *Look only*). In a terminal: `gentoo-rescue logs SESSION --journal --allow-run-system-program`.

### What is only detected: Arch, openSUSE and others

Arch Linux, Manjaro, openSUSE, Void, Alpine, NixOS, image-based systems (such as Fedora Silverblue) and any other system are **found and diagnosed, but not repaired by the tool**. The tool refuses to write and says why. You can still:

* look at the diagnosis, read the system's logs, and copy your files out;
* open a terminal in the system (next part) and use the commands in section 10;
* for NixOS and image-based systems use their own rollback tools, which the tool names.

### The repair terminal

Choose *Terminal* (`t`) in the text menu, or run:

```sh
gentoo-rescue status                         # lists the mounted systems (sessions)
gentoo-chroot SESSION                        # a shell inside the system, read-only; same as: gentoo-rescue chroot SESSION
gentoo-chroot SESSION --writable             # a shell that can change the system
```

* The tool asks you to type the name of the root device to open the terminal.
* A **writable** terminal also asks you to type `I HAVE A BACKUP`. In it you can fix what the tool cannot (install packages, edit files).
* A read-only terminal does not change the system. Use it for looking around and for copying files out.
* Programs of the repaired system run as root with full rights while you are in the terminal. If you do not trust the system (malware), do not run its programs: use the read-only inspection and copy your data out.
* `exit` leaves the terminal. The session stays mounted until you unmount it: `gentoo-rescue unmount`, or *Unmount and quit* in the menu. Unmount before you restart.
* `gentoo-rescue audit SESSION` shows everything the tool did.

### Copy your files out

Mount the system with the tool. Plug in an external disk. The desktop does not mount disks by itself, so mount it yourself. The mount directory of the repaired system is only open to root, so copy with `sudo`:

```sh
lsblk -f                                                       # find the external disk, for example /dev/sdc1
sudo mkdir -p /mnt/external && sudo mount /dev/sdc1 /mnt/external
sudo rsync -a --info=progress2 /mnt/rescue/SESSION/home/you/ /mnt/external/backup/
sudo umount /mnt/external
```

`gentoo-rescue status` shows the session name. For a dying disk use `ddrescue` to copy the whole disk first and work on the copy. Check health with `sudo smartctl -a /dev/sdX` or `sudo nvme smart-log /dev/nvme0`. The disc also has OpenSSH, so `scp` and `rsync` to another computer work.

## 10. Repair by hand: cheat sheet

Use this when you prefer the shell, or for a system the tool does not repair (Arch, openSUSE and others). Replace the device names with yours (`lsblk -f` shows file systems, labels and UUIDs). Run the commands as the live user with `sudo`.

**Unlock and assemble the storage**

```sh
lsblk -f                                        # find the root, boot and EFI partitions
sudo cryptsetup open /dev/nvme0n1p3 cryptroot   # LUKS: asks for the passphrase, creates /dev/mapper/cryptroot
sudo vgscan && sudo vgchange -ay                # LVM: volume groups are not started automatically on this disc
sudo mdadm --assemble --scan --config=none      # RAID: or name the members: mdadm --assemble /dev/md0 /dev/sda2 /dev/sdb2
```

**Mount it** (look first with `-o ro` if you are not sure what is wrong; for Btrfs add a subvolume, for example `-o subvol=@`)

```sh
sudo mkdir -p /mnt/system
sudo mount /dev/mapper/cryptroot /mnt/system
sudo mount /dev/nvme0n1p2 /mnt/system/boot       # only if /boot is separate
sudo mount /dev/nvme0n1p1 /mnt/system/boot/efi   # the EFI system partition; on some systems it is /efi
for d in dev proc sys run; do
  sudo mount --rbind /$d /mnt/system/$d && sudo mount --make-rslave /mnt/system/$d
done
sudo chroot /mnt/system /bin/bash                # use /bin/sh if there is no bash
```

(The repair tool keeps its own mounts below `/mnt/rescue`. Use another directory, such as `/mnt/system`, for mounts you make by hand.) Inside the chroot `efibootmgr` works when the live disc was started in UEFI mode (`ls /sys/firmware/efi` exists). When you are done: `exit`, then `sudo umount -R /mnt/system`. Do not run `rm -rf` inside the chroot while these mounts exist.

**Rebuild the boot loader and initramfs**

| System | Commands inside the chroot |
|---|---|
| Debian, Ubuntu, Mint | `update-initramfs -u -k all` · `update-grub` · BIOS: `grub-install /dev/sda` · UEFI: `grub-install --target=x86_64-efi --efi-directory=/boot/efi` · unfinished updates: `dpkg --configure -a && apt -f install` |
| Fedora, RHEL, Alma, Rocky | `dracut --force --regenerate-all` · `grub2-mkconfig -o /boot/grub2/grub.cfg` · UEFI: `dnf reinstall shim-x64 grub2-efi-x64` (do not run `grub2-install` on UEFI) · after editing files on an SELinux system: `touch /.autorelabel` |
| Arch, Manjaro | `mkinitcpio -P` · `grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=GRUB` · `grub-mkconfig -o /boot/grub/grub.cfg` |
| Gentoo | `ls /boot /lib/modules` · initramfs: `dracut --force --kver <version>` (or `genkernel initramfs`) · `grub-install --target=x86_64-efi --efi-directory=/efi --bootloader-id=Gentoo` (BIOS: `grub-install /dev/sda`) · `grub-mkconfig -o /boot/grub/grub.cfg` |
| Any | A forgotten password: `passwd root` (or `passwd youruser`). A wrong disk in `/etc/fstab`: compare with `blkid`, edit the UUIDs, check with `findmnt --verify`. |

**UEFI boot entry missing** (the firmware forgot the entry, or the disk was moved): `efibootmgr -v` lists them. Some firmware only looks at the fallback path, so copying the loader there makes the disk bootable anywhere: `mkdir -p /boot/efi/EFI/BOOT && cp /boot/efi/EFI/<name>/grubx64.efi /boot/efi/EFI/BOOT/BOOTX64.EFI` (on Fedora and Ubuntu the loader to copy is `shimx64.efi`). A new entry: `efibootmgr --create --disk /dev/nvme0n1 --part 1 --loader '\EFI\<name>\grubx64.efi' --label "Linux"`.

**File system problems** (the file system must **not** be mounted; for encrypted disks unlock first): look before repairing: `sudo e2fsck -n /dev/sdXN` (ext4), `sudo btrfs check --readonly /dev/sdXN`, `sudo xfs_repair -n /dev/sdXN`. Repair only after a backup (`ddrescue` for a failing disk): `sudo e2fsck -f /dev/sdXN`, `sudo xfs_repair /dev/sdXN`. NTFS volumes that Windows left "dirty" (Fast Startup) are not repaired here: boot Windows and shut it down properly.

For openSUSE, Void, Alpine, NixOS and other systems, the idea is the same: mount the system, bind `dev`, `proc`, `sys` and `run`, `chroot`, rebuild the initramfs, reinstall the boot loader. Use that distribution's own documentation for the exact commands. This project has not tested them.

**Other tools on the disc**: `gparted` (partitions, graphical), `testdisk` and `photorec` (lost partitions and deleted files), `ddrescue`, `smartctl`, `nvme`, `hdparm`, `lshw`, `lspci`, `lsusb`, `dmidecode`, `mokutil --sb-state` (Secure Boot state), `efibootmgr`, `os-prober`, `cryptsetup`, `lvm`, `mdadm`, `btrfs`, `xfs_repair`, `fsck.f2fs`, `fsck.exfat`, `ntfsfix`, `lsinitrd`.

## 11. Logs and diagnostics for a bug report

A good report lets someone else help you. Include:

* the model of the PC or laptop (`sudo dmidecode -t system`);
* which boot menu entry you used, and whether the disc ran in UEFI or BIOS mode (`ls /sys/firmware/efi`);
* whether Secure Boot is on (`mokutil --sb-state`);
* what you did and what you expected;
* the files below.

**The installer's diagnostics file.** The wizard has a **Save diagnostics…** button on the Readiness page and on the Install page when something failed. It writes one JSON file with the hardware view, the daemon state and read-only command output. Serial numbers and other identifiers are shortened or removed, and passwords are removed. Read it before you share it.

**Hardware report from a terminal** (also works on a text console):

```sh
python3 -m gentoo_installer.hardware --json --redact > hardware.json
python3 -m gentoo_installer.hardware --summary       # one line
sudo dmesg > dmesg.txt
sudo lspci -nnk > lspci.txt
lsblk -f > lsblk.txt
cat /proc/cmdline
sudo efibootmgr -v
```

**Installer logs**: `sudo cat /var/lib/gentoo-installer/install.log` (the steps and the emerge output; the wizard can also save it with **Save log…**), `sudo cat /var/log/gentoo-installerd.log` (the installer service), and `~/.cache/gentoo-installer/gui.log` (the wizard). There is no journal on this disc.

**Repair logs**: `gentoo-rescue audit SESSION` prints every command the repair tool ran and what it did (`-o file` saves it). When you unmount, the tool also saves a report inside the repaired system and tells you where.

The disc is read-only and its files are gone at power off. Save reports to a second USB stick, or send them with `scp`.

## 12. Troubleshooting table

| Problem | Fix |
|---|---|
| Firmware refuses the stick | Secure Boot off (section 3). |
| A password is not accepted | The keyboard layout of the live system is not the one you used when you set the password. Change it (section 5). Look at the characters in a visible field. |
| The installer cannot see Wi-Fi | The chip needs firmware that is not on the disc. Use Ethernet, or tether a phone by USB. |
| Time is wrong (certificate errors) | Connect to the network, the clock is corrected within a minute. Or use **Set clock from network** on the Readiness page. |
| The desktop does not appear | Restart and choose *Install in text mode* or *Repair a system (text console)* (section 4). Keep `lspci -nnk`, `dmesg` and `cat /proc/cmdline` for your report. |
| The text tool did not start by itself | Log in on the first console (Ctrl+Alt+F1) and type `sudo gentoo-rescue` or `sudo gentoo-installer-text`. |
| Installation stops at a package | The log shows it. Most failures are network problems. Try again, the installer resumes. With the defaults, binary packages are used. On a PC with less than 8 GiB, a big package that is built from source can run out of memory (the log says *Killed*). HWMonux needs PySide6 for this reason: leave HWMonux out (section 6, step 5) and try again. |
| The installer says it cannot start yet | A repair session, or a disk the repair tool unlocked, still holds the disks. Nothing was changed. Finish the repair (*Unmount and quit*, or `sudo gentoo-rescue unmount`), or release what a cut-short repair left (`sudo gentoo-rescue cleanup`). Then press **Try again**. |
| The repair tool says a disk is already mounted | The file manager, or an earlier repair, opened it. Unmount it (section 9, *A disk that is already open*), then press **Check again**. |
| Laptop battery runs low during an install | Plug in. Nothing can prevent a flat battery from stopping the install. The installer resumes on the same disk. |
| The installed system boots to a GRUB prompt | Boot the disc, choose *Repair a system (text console)* (section 9). |
| The installed system does not start with Secure Boot on | GRUB does not work with Secure Boot (section 3). Turn it off, or reinstall with systemd-boot and your own keys. |
| The repair tool says it cannot reach the installer service | Run `sudo gentoo-rescue`. As root it works without the service. |

## 13. What the disc cannot do

* Boot with Secure Boot on, or on non-x86 machines.
* Show a desktop on a GPU whose kernel driver the live kernel (6.18) lacks. A generic fallback (`simpledrm`) is not in the kernel. Use the text mode entries.
* Run on hardware released after the image was made if the kernel has no driver for it. Wi-Fi, graphics and sound chips of the newest generation are the usual cases.
* Repair or install ZFS roots, or read BitLocker volumes (Windows).
* Repair Arch, openSUSE and other systems automatically (section 9). It can look at them and open a shell.
* Read the screen aloud or show it in braille: the disc has no screen reader, no speech output and no braille software.
* Replace a backup. Repairs and installations can destroy data. Read what the tool shows before you confirm.
