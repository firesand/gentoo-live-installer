# User guide: install Gentoo on your PC or laptop, and repair a Gentoo system that does not start

This guide is for people who have the installer disc and want to use it on their own PC or laptop. You do not need to know Gentoo. The build side (how the disc is made) is in [ISO.md](ISO.md).

**Scope: Gentoo only.** The installer installs Gentoo, and the repair tool repairs Gentoo systems. It does not install or repair other Linux systems. It does keep Windows on the disk when you ask for that (dual boot).

Contents: [what you need](#1-what-you-need) · [write the disc](#2-write-the-disc-to-a-usb-stick) · [start it: Secure Boot, UEFI and BIOS](#3-start-the-disc) · [if it does not start](#4-if-the-disc-does-not-start-or-the-screen-stays-black) · [network, keyboard, clock](#5-network-keyboard-and-clock) · [install Gentoo](#6-install-gentoo-with-the-desktop-wizard) · [install in text mode](#7-install-in-text-mode) · [laptops, graphics and special hardware](#8-laptops-graphics-and-special-hardware) · [repair a Gentoo system](#9-repair-a-gentoo-system-that-does-not-start) · [repair by hand](#10-repair-by-hand-cheat-sheet) · [logs for a bug report](#11-logs-and-diagnostics-for-a-bug-report) · [troubleshooting](#12-troubleshooting-table) · [limits](#13-what-the-disc-cannot-do) · [the AI rescue stick](#14-the-ai-rescue-stick-optional)

## 1. What you need

* A 64-bit PC or laptop (Intel or AMD). Not Apple Silicon Macs, Windows-on-ARM laptops or Raspberry Pi boards.
* A USB stick of at least 8 GB that you can erase. The image is between 2 and 3 GB. A blank DVD also works.
* Memory: 2 GiB or more is recommended for an installation. The installer warns below 2 GiB and refuses below 1 GiB. The boot entry "Desktop in RAM" needs 6 GiB, "Text console in RAM" needs 4 GiB. With less than 8 GiB the optional hardware monitor HWMonux is slow and heavy to install (step 5 of section 6).
* For an installation: a wired network if you can (Wi-Fi works too), power for a laptop, and time. Most packages come as binaries. Even so, a desktop installation takes from half an hour to several hours.
* Disk space for the new system: 8 GiB for a minimal system, 14 GiB for Xfce, 18 GiB for GNOME, 24 GiB for KDE Plasma. More if you compile packages yourself. The installer shows what it needs.
* For an installation that keeps Windows: a backup, and the BitLocker recovery key if Windows uses BitLocker.
* Nothing to change for **Secure Boot**: with it on, the first start needs one step on a blue screen (section 3).

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
* Linux, a friendlier way: `caligula` (in the GURU repository on Gentoo) writes the image as it is and checks the stick afterwards: `sudo caligula burn gentoo-installer-amd64-*.iso`.
* Windows: Rufus. When it asks, choose **DD Image mode**. Or use balenaEtcher, which always writes the image as it is.
* Do not use "ISO mode" or tools that unpack the files onto the stick. That stick does not boot. Ventoy has not been tested with this disc. Of these tools only `dd` is described by the project's own checks; caligula, Etcher and Rufus copy the same bytes in this mode but were not run for this guide.

A release can also come with signatures (`.sha512.asc` and `.manifest.json.asc`). Check them with `gpg --verify` and the public key of the maintainer.

## 3. Start the disc

### Secure Boot

The disc starts with **Secure Boot** on or off. With it off, nothing is special. With it on, the first start on a PC shows blue screens once (shim's MokManager). The disc's first program, shim, is Fedora's and signed by Microsoft. GRUB and Linux after it are signed with a key made for this one version of the disc, and the PC has to learn that key:

1. "Verification failed: (0x1A) Security Violation": press Enter (OK).
2. "Shim UEFI key management, Press any key to perform MOK management": press a key within 10 seconds. If you miss it, start the PC again.
3. "Perform MOK management": choose **Enroll key from disk**.
4. Choose **GENTOO_EFI**, then the file **ENROLL_THIS_KEY_IN_MOKMANAGER.cer**.
5. "[Enroll MOK]": **View key 0** shows the key. Its *Fingerprint* must be the one in the `.secureboot.txt` file published next to the ISO. Then choose **Continue**.
6. "Enroll the key(s)?": **Yes**. Then **Reboot**.

The private half of the key is destroyed when the disc is built, so it can sign nothing else. Every new version of the disc has a new key and needs these steps once more. An old key allows only its old version. Remove it when you no longer need that disc: run `sudo mokutil --delete` with its `.cer` file and confirm on the blue screen at the next start. Do it above all on a PC that is to run Linux games with Easy Anti-Cheat Linux v2 (announced October 2026): it accepts only keys Epic recognizes, and this disc's key is not one of them.

**If the PC still says "Secure Boot Violation" or skips the stick**, it trusts only Windows and not Microsoft's key for other systems. Look in the firmware setup for "Allow Microsoft 3rd Party UEFI CA" (some Lenovo, Dell, HP and Surface models) and turn it on. Or turn Secure Boot off:

1. Restart and press the setup key right after power on:

   | Maker | Setup | One-time boot menu |
   |---|---|---|
   | Dell | F2 | F12 |
   | HP | Esc, then F10 | Esc, then F9 |
   | Lenovo | F1 or F2 (ThinkPad: Enter, then F1) | F12 |
   | ASUS desktop board | Del or F2 | F8 |
   | ASUS laptop (ROG, TUF, Zenbook, Vivobook) | F2 (EZ Mode: F7 for Advanced) | Esc |
   | Acer | F2 | F12 (enable it in setup first) |
   | MSI | Del | F11 |
   | Microsoft Surface | hold Volume Up while pressing the power button | hold Volume Down |

2. Find **Secure Boot** (under Security, Boot or Authentication). Set it to **Disabled** (ASUS laptops: Security, Secure Boot, Secure Boot Control). Some firmware first needs an administrator password. Some need "OS type: Windows UEFI" changed to "Other OS".
3. Save and exit (usually F10).

On a PC with **Windows BitLocker or "Device encryption"**: look up the recovery key before you change firmware settings (<https://aka.ms/myrecoverykey>). Windows may ask for it at the next start. Enrolling the disc's key (steps 1 to 6) does not change the firmware's own Secure Boot settings. Intel Macs with a T2 chip: in the Startup Security Utility choose "No Security" and allow external media. The internal SSD of these Macs is not visible to this Linux. Memtest86+ is not signed: with Secure Boot on, its UEFI menu entry is hidden.

### Secure Boot and the system you install

The disc's key is only for the disc. What happens to the new system depends on its boot loader:

* **GRUB (the default)** is not signed. The PC will not start it while Secure Boot is on. Turn Secure Boot off before the first start of the new system, or choose another boot loader. When Secure Boot is on, the installer says so on the Readiness and Review pages, and again when the new system is ready to start.
* **systemd-boot or the EFI stub, with a Unified Kernel Image and your own signing key**, can work with Secure Boot. This is an advanced choice. The installer can make the signing key for you (the recommended choice) or use a key and certificate you made yourself. It signs the boot files with that key and keeps the key on the new system, readable by root only, so that kernel updates are signed again. You add the certificate to the firmware's key list (the installer says where it left the file). The proprietary NVIDIA driver cannot be combined with this, because its kernel modules would not be signed.

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
| **AI rescue (text console)** | Optional. Like the repair entry, but with an AI assistant that helps you diagnose (section 14, [RESCUE-AI.md](RESCUE-AI.md)). |
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
| The firmware shows an error about signatures, or boots something else | Secure Boot is on and this PC does not trust Microsoft's key for other systems (section 3: "Allow Microsoft 3rd Party UEFI CA", or turn Secure Boot off). A blue "Verification failed" screen is the normal first start: section 3. |
| The stick is not in the boot menu | Write it again with `dd`, or with Rufus in DD mode. Try another USB port (USB 2 is more reliable than USB 3 on some PCs). On a very old PC enable "Legacy boot / CSM". |
| The menu appears, then a black screen | Wait a minute (USB sticks are slow). Then restart and choose **Safe graphics**: the same desktop on a generic display driver that works on almost every PC (no 3D, so it is slower). If that fails too, choose **Install in text mode** or **Repair a system (text console)**. They need no graphics driver. |
| The PC freezes or loses the USB stick or NVMe disk during boot | More options, **Compatibility mode** (switches off PCIe, NVMe and USB power saving and uses the generic display driver), or **Old firmware**. |
| It is very slow | Use **Desktop in RAM**, or a USB 3 stick in a USB 3 port. |
| A message asks to "Load keymap" | That is a text console entry. Type one of the layout names shown on the screen (for example `de` or `fr`) or press Enter for the default. You have 10 seconds. |

**Why the desktop sometimes does not start.** A graphics chip that is newer than the live kernel (Linux 7.2) has no driver there, and the screen stays black. The **Safe graphics** entry starts the desktop through the generic display driver (`simpledrm`) that shows the picture the firmware already set up: no 3D and no choice of resolution, but it works on almost every PC. The text mode entries need no graphics at all. [ISO.md section 9](ISO.md#9-known-limits-and-follow-ups) has the details.

## 5. Network, keyboard and clock

* **Wired** networks connect by themselves.
* **Wi-Fi** on the desktop: click the network icon in the panel, choose the network, type the password. In a terminal or on a text console: run `nmtui`. Choose "Activate a connection", choose the network, type the password. Or: `nmcli device wifi connect "Network name" password "the password"`.
* **Wi-Fi inside the installer**: when the **Readiness** page finds no internet and the PC has a Wi-Fi chip, a box *Connect to Wi-Fi* appears under *Your connection*. Press **Switch Wi-Fi on** if the radio is off (if the PC has a hardware switch or an airplane key that is off, the page says so), pick your network in the list (press **Scan again** if it is missing), type the password, press **Connect**. Keep **Remember this network on the new system** ticked and the installed system joins this network by itself at its first start; this works when the installed system uses NetworkManager (the default). The page may ask for the password once more, for the installed system. WEP networks (a very old security) are refused: change the router to WPA2 or WPA3. If the page still says *Why not?*, press it: it lists the checks that failed (cable, address, DNS, reaching the Gentoo mirrors). The same box can set a fixed address for the live session (*More network settings*).
* **Log in to the live system from another computer (SSH)**: useful to read the Handbook on one screen and type on the other. It is **off at first**: nobody can log in. On the Readiness page press **Remote access (SSH)…**, or in a terminal run `gentoo-remote-access on`. The live user `gentoo` gets a password (made for you, or the one you type), shown once, and the window prints the line to type on the other computer, for example `ssh gentoo@192.168.1.20`, and the keys' fingerprints. Turn it off again when you are done (`gentoo-remote-access off`). The official Handbook way also works: `sudo passwd root` here, then `ssh root@ADDRESS`. Anyone on your network who knows the password can log in while it is on, so use it on a network you trust.
* **Login pages** (hotel, campus): open Firefox from the application menu (the Handbook button in the panel opens it as well). On a text console the text browser Links handles simple login pages, but not pages that need JavaScript.
* **A network behind a proxy**: enter it on the first page, Readiness, under *My network needs a proxy*. The Network tab of the Customization page only shows it.
* **Keyboard layout**: the desktop starts with a US layout. Change it with the layout button in the panel or in *Settings > Keyboard > Layout* **before** you type a disk password. On a text console you choose the layout in the boot menu step. Later: `sudo loadkeys de` (or `fr`, `uk`, ...).
* **Clock**: PCs that share Windows keep local time in the hardware clock, so the live clock can be hours off until the network is up. `chronyd` then corrects it. The live system does not change a hardware clock that keeps local time. When the clock is wrong, the Readiness page offers **Set clock from network**. It waits up to 15 seconds, and it says so when no time server answered (some networks block them). Then set the date and time by hand with the clock of the desktop, and press **Check again**. The page also has a **Network settings** button. In the text installer the "This computer" step offers `t` (fix the clock) when the clock is wrong; Wi-Fi is on the network step, where `n` opens `nmtui`.

## 6. Install Gentoo with the desktop wizard

The installer starts with the desktop. Or run `gentoo-installer`. It has eight steps and follows the order of the official **Gentoo Handbook for AMD64** (media, network, disks, stage3, base system, kernel, system configuration, tools, boot loader, finishing). It does what the Handbook describes, so you can read the Handbook next to it:

* The small **i** buttons next to many choices explain the choice in one sentence and, when you are online, link to the matching Handbook page. *Open the AMD64 Handbook* on the Welcome page opens the whole Handbook. The Handbook words are used: *Default* is what most people should pick, *Alternative* is another good choice, *Optional* can be skipped.
* The **Detail level** box at the top right has two settings. **Guided** shows few choices with safe defaults. **Advanced** shows every option ("More options" opens by itself). What you set is kept when you switch between them: nothing is lost.

**In guided mode the disk you choose is erased.** You confirm by typing its name on the Review page. The disc you booted from is never offered.

1. **Welcome**: choose *Install Gentoo*. The button *Repair a Gentoo system* opens the repair window instead (section 9).
2. **Readiness**: the installer checks the live system, the network, the tools and your hardware. Warnings can be ignored. Errors cannot. A hardware summary and advice appear here. You can save a diagnostics file (section 11).
3. **Storage**:
   * *Guided* makes a complete layout on one disk (boot partition, optional swap, root) and **erases that disk**.
   * *Manual* lets you use partitions you made yourself. Make them first with GParted (applications menu, or `sudo gparted` in a terminal; the live user has no password). Then choose which partition is the root, the boot partition and so on, and which ones to format. Use *Manual* to keep Windows or other systems.
   * Further down: encryption and advanced storage (LUKS, LVM, RAID).
   * Pick the right disk by size and model. If you have two SSDs, write them down before you start.
   * **A disk without a serial number must be approved.** Virtual disks and some USB adapters report no serial number, and two disks can report the same one. The installer then has no serial number to check that it is still the same disk later. The page shows *This disk has no serial number*. Check the model and the size. Then tick *I confirm that /dev/... is the disk to use*. From then on the installer knows the disk by its hardware path, its size and its model, and it refuses to go on if one of them changes. Without the tick you cannot continue. If the disk has no stable hardware path either, it cannot be used. (For a virtual machine, give the disk a serial number.)
   * **A disk that is in use asks for your consent.** The page shows *This disk is in use* when the disk, or a partition you format, holds active swap, an LVM volume group, a RAID array, or one half of a Btrfs or ZFS pool whose other half is on another disk. Erasing it destroys that data, and the data on the other disks of the same volume group, array or pool. Tick *Switch these volumes off and erase them together with the disk* to go on, or choose another disk. A disk with mounted partitions must be unmounted first.
   * In *Manual* mode, if you keep (do not format) the root partition, the installer asks **Install over the existing system?** before it starts. Say Yes only if the new system may go into that partition. If it already holds a Linux system, that system's `/etc`, its accounts and its boot files are replaced. A copy of `/etc` and of the SSH keys is saved in `/root` first. To repair a Gentoo system that is on it instead, say No and use *Repair a Gentoo system* (section 9).
4. **Base system**: stage3 (OpenRC or systemd), Portage profile, desktop, kernel, initramfs and boot loader. **Desktop**: Guided shows four cards: *KDE Plasma* (the default; polished and customisable, and a good partner for NVIDIA), *GNOME*, *Xfce* (light, for older or small PCs) and *Minimal system* (no desktop at all). *More desktops* adds *Cinnamon*, *MATE*, *LXQt*, *GNOME Light*, and for experienced people the tiling environments *Sway* (Wayland; upstream does not support NVIDIA graphics) and *i3* (X11); the last two have no settings program, you edit a text file. A desktop is a big download or a long build; the installer sizes the root partition for the desktop you choose and warns when the disk is too small. Cinnamon needs the GNOME Portage profile, and KDE Plasma needs a Standard or Desktop stage3; the page tells you when a choice does not fit. The preselected choices (a binary kernel, GRUB, a desktop profile) are the quickest and the most tested. Change them only when you know why.
5. **Customization** has four tabs. The installer fills in what it detected. Change it if you want something else.
   * **Hardware** comes first. It shows what the installer found on this computer (processor, memory, graphics, disks, firmware), advice for it, and the list *What the installer will set up*. Here you choose *This computer* (tuned for this hardware) or *A portable disk* (for a disk that will move to other computers: generic settings, all firmware, both microcode families and a fallback boot entry). You also choose the graphics driver, power management, compressed RAM swap, TRIM, hibernation and more. *System tuning* (settings from CachyOS, on by default) is explained in section 8. Most choices are on *Automatic*, and the page says what Automatic means on this computer. Set *Detail level* (top right) to *Advanced* to see all of them.
   * **Portage**: four questions in Guided mode: *Compiler optimisation* (works on any PC, or tuned for this CPU; programs tuned for this CPU may not start on an older PC), *Download ready-made packages* (on by default; every package is checked against Gentoo's signature, and if the signing keys cannot be set up everything is built from source instead), *Download sources* (automatic, a list of mirrors by country, or your own https:// address) and *Licences* (free software only, or free software plus redistributable firmware and drivers). 
     *More options* (Detail level: Advanced) adds, in groups: **Speed** (build jobs and load limit, compiler flags, processor features), **Packages** (the ready-made package variant, how the package tree is fetched, a searchable USE flag list with On/Off/Default, graphics drivers), **Licences** (groups and licences for one package) and **Advanced** (EMERGE_DEFAULT_OPTS, FEATURES, *Update the whole system* and removing packages that are no longer needed). The NVIDIA driver is never a ready-made package: it is always built on your computer.
   * **Network**: the network manager, DHCP or a static address, and Wi-Fi. It also shows the download proxy, which you set on the first page.
   * **Applications & tools**: *System tools (Handbook)* (the clock, the system logger, cron, locate and gentoolkit: with systemd the logger is hidden because systemd keeps its own journal), the companion applications that are on the live desktop too, all ticked by default: the HWMonux hardware monitor, the PortageHub package manager, **Gentoo Settings** (the CachyOS-style tuning as tweaks you switch on and off, for OpenRC or systemd) and **KernelSmith** (a guided builder for your own optimized kernel; it installs the kernel sources itself when you build), and services such as printing and Bluetooth. The three Rust applications are compiled during the installation, a few minutes each on a fast PC. **Gaming (optional)** is off by default. *Quick* installs Steam and Heroic from Flathub at the first start (needs the network then); *Native Gentoo* builds 32-bit graphics libraries from source (often hours), needs a multilib stage3, an outside repository (steam-overlay), some testing packages and a download from Valve, and you must tick that you accept this. **Fortnite is not playable on Linux as of 27 October 2026**, and games that need Easy Anti-Cheat Linux v2 will not run on Gentoo (section 8). **Check this PC** is read-only and shows Secure Boot, TPM, IOMMU, the MOK list and the other things secondary sources report Epic's new anti-cheat asks for; it never promises anything. **On a PC with less than 8 GiB of memory** a note appears under HWMonux: *HWMonux needs PySide6, which is compiled from source: about 40 minutes and up to 4 GiB of memory per compile job.* HWMonux stays ticked: the installer does not leave it out for you. Untick it if you do not need it, or if the PC is small and you do not want to wait.
6. **Personalize**: your user and passwords, remote login, language, time zone and keyboard. **Root login is turned off by default**: you administer the system with `sudo` from your own account, and the password dialogs of the desktop, Gentoo Settings, PortageHub and KernelSmith ask for your own password (with `su` as the administrator tool, or a user outside the `wheel` group, they ask for root's). The other root choices are the same password or a separate one; if you ever lock yourself out, the Repair tool fixes it. The time zone is read from the live session, or UTC with a note; pick *Region* then *City* with **Change**. **Hardware clock keeps local time** is ticked for you, once, when Windows is found or the clock already keeps local time (Windows keeps local time in the hardware clock and Linux normally keeps UTC, so without it the clock is wrong by some hours after you switch systems); untick it if you are sure. **Allow remote login over SSH** is on by default: root can never log in over SSH, your own account can (with the password, or with a public key you paste; *More options* has "keys only"). The installer writes your answers itself, so you do not need `systemd-firstboot`. More options also holds the administrator tool (sudo, doas, run0 or su), the groups of your user, extra locales, the console font and keymap, a domain name and extra `/etc/hosts` lines.
7. **Review**: nothing has been changed yet. Read the summary, especially the target disk. Type the disk's name to confirm.
8. **Install**: follow the log. Keep the live session running. Keep a laptop on power. Do not close or restart the installer. The disc prevents suspend (lid, idle) while the live system runs.

You can import and export settings. A settings file never contains passwords.

When it finishes, restart, remove the stick, and boot the new system. If the new system does not start, boot the disc again and use the repair tool (section 9).

If an installation fails, the installer shows the failing step and the buttons **Try again**, **Resume installation** and **Save diagnostics…**. It resumes on the same disk. If a late step fails after the boot loader was installed, the installer says *Your new system is installed and can start*: restart now and fix the rest from the installed system, or press **Retry the desktop step**. After an error or a cancel the installer first finishes and releases the disks; the buttons that start something appear when that is done. If you cancel after the boot loader was installed, the new system can start, and the same two choices are offered. If you cancel while the disk is being changed, only **Erase again and restart installation** is offered, because the disk is half done. If a repair session still holds the disks, the installer says it cannot start yet and nothing is changed (section 12). The log is in `/var/lib/gentoo-installer/install.log` (read it with `sudo less`). The daemon's own messages are in `/var/log/gentoo-installerd.log`.

## 7. Install in text mode

Use this when the desktop does not start, or on a PC with little memory. It installs the same system as the wizard.

1. Start the disc and choose **Install in text mode**. Choose your keyboard layout when the menu asks for it.
2. After the automatic login the text installer starts by itself. If it does not (for example after another boot entry), type: `sudo gentoo-installer-text`.
3. Answer the questions one at a time. They cover the main choices of the wizard: keyboard, network, disk, file system and encryption, desktop and init system, accounts, time zone and language. The text installer sets up the whole disk (guided mode), and it picks the hardware choices of the Hardware tab itself from what it finds. It does not ask about the NVIDIA driver (without the licence tick it uses nouveau), laptop and vendor tools, gaming, Portage options, the kernel or the boot loader, and SSH stays at its default (on); use the wizard or a settings file for those (sections 6 and 8). Read the summary at the end: it lists what was chosen for this computer, what happens to every disk, notes and advice. **The disk you choose is erased.** You confirm by typing its name.
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
* **Closing the lid** on the installed system: *When the lid is closed*, in *More options* on the **Hardware** tab, sets what the system does. A desktop with its own power settings, such as Plasma, can override that choice; if the lid does something else, change it in the desktop's settings.
* **Hybrid graphics** (Intel or AMD plus NVIDIA): the live desktop runs on the GPU that the firmware starts with, usually the integrated one. The installer can set up PRIME render offload for the installed system. Then the NVIDIA card is used only for programs you start with `prime-run`, and the integrated GPU does the rest and saves battery. `prime-run` comes with the NVIDIA driver, so without that driver (no licence tick) it does not exist.
* Fingerprint readers, IR cameras and some touch-screens do not work in the live session.
* Tablets without a keyboard have no on-screen keyboard on the disc.

### Laptop and device tools (Razer, ASUS ROG, and more)

On the **Hardware** tab the card **Devices on this computer** lists only tools for hardware the installer actually found. A plain PC or a virtual machine may show nothing. Each line says in one sentence what the tool does and what it costs (built on your PC, or from an unofficial repository). In *More options* laptops also get *Battery charge limit* (no limit, 80% or 60%; it uses TLP instead of power-profiles-daemon) and *When the lid is closed* (sleep, lock the screen or do nothing). A Razer Blade also gets *Known fixes for this computer's maker* there.

| Offer | When it appears | What you get | Cost |
|---|---|---|---|
| Razer devices | a Razer keyboard, mouse or laptop was found | the OpenRazer driver and daemon: colours, effects and DPI. On by default. A colour and DPI app comes with it (RazerGenie on KDE, Polychromatic elsewhere). | Built on your PC (a few minutes). It is a kernel module, so it cannot be combined with Secure Boot signing. |
| ASUS ROG and TUF tools | an ASUS laptop with the ASUS kernel driver or a ROG/TUF model | `asusctl` (keyboard lighting, fan and power profiles, battery limit) and, on a laptop with two graphics cards, `supergfxctl` (graphics switching). Off by default. They replace power-profiles-daemon and TLP: with them on, leave *Power management* on Automatic and *Battery charge limit* on No limit, or the installer refuses the combination and says why. | They are **not in Gentoo's own tree.** They come from the user-maintained repository **edorp** (<https://github.com/firesand/edorp-overlay>), and some of their dependencies from **GURU**. Both repositories are added to your new system and updated with it; Gentoo does not review them. One tested version is allowed. Built on your PC. |
| RGB lighting (OpenRGB) | an ASUS Aura, Corsair or SteelSeries lighting controller, or RGB memory/motherboard lighting | one app for all of it. Off by default. | Built on your PC. |
| Game controller access | a game controller was found | udev rules so games can use it. Off by default. | A testing version, one tested version allowed. |
| Logitech devices (Solaar) | a Logitech receiver or device | battery, pairing and settings. Off by default. | |
| Thunderbolt, pen and drawing tablets | a Thunderbolt port on a laptop; a pen or tablet | `bolt` (asks before a dock or drive may connect); Wacom pressure and buttons. On by default. | |
| Screen rotation and light sensors | a convertible laptop or a light sensor | auto-rotation and brightness. Off by default. | From GURU, a testing version, built on your PC. |
| ThinkPad fan control | a ThinkPad (More options) | `thinkfan`; you write its settings yourself. Off by default. | |

* The ASUS tools are an add-on from a personal repository. If edorp or GURU is down when you install, the installer says that the packages from it were skipped and goes on; install them later from the installed system.
* Razer peripherals (mice, keyboards, headsets) are supported through OpenRazer; the Razer laptop's own functions depend on what OpenRazer supports for your model.
* Fingerprint readers have their own tick box (*Fingerprint reader support*) under the power settings. Fingerprint readers and IR cameras do not work in the live session.
* **What is not tested.** These offers are tested as plans and files (the right packages, repositories, keywords and services for each machine), and the hardware report is checked in virtual machines that carry the DMI names of an ASUS ROG laptop and a Razer Blade (the live system only; no tool is installed there). No real Razer or ASUS laptop has run the result. How well a model works depends on the kernel driver for it.

### NVIDIA graphics

Where: *Customization*, **Hardware** tab, the card **Graphics**. Its NVIDIA parts (the licence box and the two NVIDIA drivers) appear only when the installer found an NVIDIA card (or you chose *A portable disk*).

* **You choose the driver.** *Automatic* (the default) means: **the NVIDIA proprietary driver as soon as you tick the licence box** *I accept the NVIDIA Software License*, and the open nouveau driver until you do. Nothing from NVIDIA is installed without your tick. The other choices are behind **Choose another driver**: *NVIDIA, open kernel modules* and *NVIDIA, closed kernel modules* (the driver, chosen by you; choosing one clears the licence box, so tick it again), *Open-source driver (nouveau, with NVK)* (needs no licence; games and 3D programs are slower) and *Basic display* (no 3D and no Wayland, the safest start; not possible with Sway). The card says in one sentence why it recommends what it recommends.
* **Which driver Automatic picks.** The installer reads the generation of your card:

  | Your card | Automatic gives |
  |---|---|
  | GeForce GTX 16 series, RTX 20, 30 and 40 series (Turing, Ampere, Ada) | driver **595** with the **open kernel modules** |
  | RTX 50 series (Blackwell) | driver 595 with the open kernel modules (the closed ones do not support this card) |
  | GeForce GTX 750 and 900 series, GTX 10 series, Titan V (Maxwell, Pascal, Volta) | driver **580**, the last long-term branch for these cards, with the closed kernel modules |
  | Older cards (Kepler and before) | nouveau (no current NVIDIA driver supports them) |
  | A virtual machine | nouveau, unless a real NVIDIA card is passed through |
  | A laptop with Intel or AMD plus NVIDIA | the same driver, with the NVIDIA card used for the programs you start with `prime-run` (render offload), so the battery lasts. Without the licence tick: nouveau, and no `prime-run` |

  Branches 470, 390 and 535 are never offered. *More options* lets you set the kernel modules (open or closed) and the branch by hand. A PC with a Blackwell card **and** an old Maxwell or Pascal card cannot use one NVIDIA driver for both: the installer says so and asks you to remove a card or use nouveau.
* **The driver is built on your computer.** NVIDIA's driver is never a ready-made package. It is compiled, which takes minutes, and its kernel modules are built for the kernel you install. It needs a Gentoo distribution kernel (`gentoo-kernel` or `gentoo-kernel-bin`) with Dracut, which is the preselected choice; with another kernel the installer refuses the driver with a plain message.
* **Extras (More options):** video decoding on the card (a testing version of `nvidia-vaapi-driver`), the monitor `nvtop`, `nvidia-persistenced`, and the CUDA toolkit (a very large download that needs a second licence, with its own tick box). Power saving of a hybrid Turing laptop's card is offered only with the closed modules of driver 580.
* **A boot entry "Safe graphics"** is added to the installed system's boot menu (on by default; *More options* can switch it off, and then there is no entry, even though the wizard says it is always added). If the NVIDIA driver ever gives a black screen, choose it: it starts the same system on the generic display driver, and you can fix the driver from there or with the repair tool (section 9, *A black screen with the NVIDIA driver*).
* **Secure Boot and the NVIDIA driver do not go together yet.** The driver's kernel modules are built on your PC and carry no signature the firmware trusts, so with Secure Boot signing the screen would stay black. The installer refuses the combination with a clear message: turn Secure Boot signing off, or use nouveau. Signed modules are planned, but they have not been proven in a test yet, so in *More options* the setting *Signed kernel modules* offers only Off; the other choices are marked "not available yet".
* The driver is installed on a best-effort basis: if it cannot be downloaded or built (for example the network is down), the installation still finishes and the PC runs on nouveau. The file `first-steps.txt` in your home directory then says how to try again (`emerge --ask x11-drivers/nvidia-drivers && dracut --force --regenerate-all`).
* Later you can change the driver with the normal Gentoo tools (`x11-drivers/nvidia-drivers`; the Gentoo wiki explains them).
* **Not tested on real hardware.** The choices, the branch rules and the files the installer writes are covered by tests and by the VM harness. Whether a given card shows a picture has been checked only through a checklist the maintainer runs by hand on one PC with an RTX 4090 ([TESTING.md](TESTING.md)); treat any other card as untried.


### Gaming (optional) and Fortnite

*Customization*, **Applications & tools**, *Gaming (optional)*. It needs a desktop and is **off** by default.

* **Quick (Flatpak):** installs Flatpak. Steam and Heroic (for Epic, GOG and Amazon games) are downloaded from Flathub **at the first start** of the new system, so the PC needs the network then.
* **Native Gentoo:** builds the 32-bit graphics libraries from source, which often takes hours. It needs a multilib stage3, an outside software repository (`steam-overlay`), a few testing (`~amd64`) packages and a download from Valve. You must tick that you accept all of that. It is refused with a no-multilib stage3.
* Both can add game-friendly settings and game controller access (on by default when gaming is on) and, if you want, Windows-game helpers.
* **Fortnite does not work on Linux today, and games that need Easy Anti-Cheat Linux v2 will not run on Gentoo.** As of 27 October 2026, Epic has announced a new anti-cheat for Linux (EAC Linux v2) and that Fortnite is coming, with no date. Secondary sources say it asks for: a signed kernel and signed kernel modules, built by an organization whose key Epic recognizes; Secure Boot on; a TPM 2.0; measured boot; the IOMMU on without pass-through; kernel lockdown in integrity mode; kexec off; and the BPF security module. The keys reported so far are those of Canonical (Ubuntu), Fedora, Universal Blue (Bazzite, Bluefin, Aurora), Debian, SUSE, Rocky Linux and the Microsoft UEFI CA, and when a PC starts through shim every key enrolled in its MOK list must be one of them. Gentoo is not on that list, and a kernel built on your PC cannot be. Epic's own page needs a login, so check <https://onlineservices.epicgames.com/news/introducing-easy-anti-cheat-linux-v2> yourself.
* If you keep another distribution for these games on the same PC, remove this disc's key from the MOK list once you no longer need the disc (section 3, *Secure Boot*).
* The button **Check this PC** is read-only. It shows Secure Boot, the TPM, the IOMMU (and whether it is in pass-through mode), the keys in the MOK list and the rest of that list for the computer you are on (the kernel rows depend on what you install, so they are decided later). It never promises anything. Remember that Secure Boot with the NVIDIA driver is refused (above), so those two goals conflict today.
* Nothing here has been tested on real hardware with a game.

### System tuning (settings from CachyOS)

*Customization*, **Hardware** tab, *System tuning*. It is **on** by default; untick it to keep Gentoo's own defaults. The values come from [CachyOS-Settings](https://github.com/CachyOS/CachyOS-Settings). The installer writes its own files with them and adapts them to what you chose: OpenRC or systemd, the kernel, the power management, laptop or desktop, zram and the desktop. Every file works the same with OpenRC and systemd; only the service that applies it differs. *What the installer will set up* lists each file.

| What | File | Applied by (OpenRC / systemd) | When |
|---|---|---|---|
| More of the file-name cache kept in memory; changed data written early and in small amounts, so a big copy does not stall the desktop; the NMI watchdog off | `/etc/sysctl.d/60-installer-tuning.conf` | the `sysctl` service / `systemd-sysctl` | always |
| Swap settings for zram: `vm.swappiness = 150`, `vm.page-cluster = 0`, and `zswap.enabled=0` on the kernel command line | the same file, the boot loader | as above | with compressed RAM swap (zram) |
| I/O schedulers: BFQ for hard disks, mq-deadline for SATA SSDs and SD cards, Kyber for NVMe | `/etc/udev/rules.d/59-installer-ioschedulers.rules` | udev / udev | with `gentoo-kernel` or `gentoo-kernel-bin` |
| Huge pages for the programs that ask for them | `/etc/tmpfiles.d/installer-thp.conf` | `systemd-tmpfiles-setup` from `systemd-utils` / `systemd-tmpfiles-setup` | with `gentoo-kernel` or `gentoo-kernel-bin` |
| The Wi-Fi country from your time zone, and the wireless regulatory database (`net-wireless/wireless-regdb`) | `/etc/modprobe.d/installer-wifi-country.conf` | the Wi-Fi driver when it loads | a Wi-Fi card (or *A portable disk*) and a time zone of one country |
| No audio crackle on the charger: audio power saving only on battery | with TLP: `/etc/tlp.d/80-installer-audio.conf`; otherwise `/etc/udev/rules.d/19-installer-audio-power.rules` and `/usr/local/libexec/installer-audio-power` | TLP or udev | always |
| Tap to click on the touchpad | `/etc/X11/xorg.conf.d/50-installer-touchpad.conf` (Xfce, MATE, LXQt, Cinnamon, i3) or `/etc/sway/config.d/30-installer-touchpad.conf` | the X server or Sway | laptops; KDE and GNOME have their own touchpad setting |
| The unused watchdog timers (`iTCO_wdt`, `sp5100_tco`, `wdat_wdt`) are not loaded | `/etc/modprobe.d/installer-watchdog.conf` | modprobe | not in a virtual machine |

* To undo one item later, delete its file and restart the PC: the running system keeps the value until then. Most files say so in a comment at the top; the audio and touchpad files do not.
* **Gentoo's own scheduler rules:** with the tuning on, `sys-block/io-scheduler-udev-rules` is not installed, because its rule would run after this one at every boot and put NVMe disks back on `none`. Deleting the file above then leaves the kernel's choice. With the tuning off a distribution kernel gets the package, and `/etc/default/io-scheduler` and `elevator=` work as Gentoo describes.
* **With Gentoo Settings** (installed by default) the memory, disk, huge-page, scheduler and audio items here can be changed from its window later, and its choice wins over the file above. It shows the installer's values as *ALREADY SET*. Switching such an item off there puts back the value it found, which is this one, so to go back to the kernel's value delete the file above. The audio item is the exception: switching it off can turn audio power saving back on until you restart or plug the charger in or out. It has no setting for tap to click, it cannot show the Wi-Fi country as set, and the watchdog list stays until you delete `/etc/modprobe.d/installer-watchdog.conf`.
* **Left out on purpose:** CachyOS's NVIDIA setting that skips clearing memory (a security risk); `kernel.unprivileged_userns_clone` (Gentoo's kernels do not have it); `kernel.sysrq=1` and the other security defaults; the shorter systemd start timeout (slow services would fail); the journald and systemd-oomd limits (systemd only); CachyOS's own DNS, time and debug servers; and the SATA and hard-disk power settings (they cost battery, and TLP manages them on laptops).

### Remote login (SSH) on the installed system

On the **Personalize** step, *Allow remote login over SSH* is **ticked by default**: the new system starts an SSH server at every boot, so you can log in from another computer, copy files with `scp` or `rsync`, and fix the machine without a keyboard on it.

* **Root can never log in over SSH.** Your own account can, with its password, or with a public key you paste in the box *SSH authorized keys*.
* *More options* has **Keys only: do not allow logging in with a password** (safer on a network you do not trust). Add at least one public key first, or you lock yourself out of SSH.
* Untick the box if you do not want an SSH server. It is a service that listens on port 22 of your network; anyone who knows your password can try it, so use a strong password.
* The last screen of the installer ("Your first steps in Gentoo") and the file `FIRST-STEPS.txt` in your home directory show the line to type, for example `ssh yourname@yourhostname`. Use the IP address if your network does not know the host name (`ip addr` shows it). A second file, `first-steps.txt`, holds the notes the installer made for your hardware.
* The SSH default and the keys-only setting are checked in tests and by the VM harness (it checks that sshd runs at boot and that root login and, for keys only, password login are off). No real PC has been tested.

### Windows on the same disk

In Windows, shrink the Windows partition with Disk Management first. Switch off *Fast Startup* (`powercfg /h off` in an administrator terminal). Shut down completely (not hibernate). Otherwise Windows' file systems are left "dirty" and Linux cannot touch them safely. Back up first. Use *Manual* storage in the installer, keep the existing EFI system partition, and do not format it. Windows keeps local time in the hardware clock and Linux normally keeps UTC. Two systems that disagree show a clock that is wrong by some hours after you switch. Tick **The clock keeps local time (for Windows dual boot)** on the Accounts page, so both agree. The text installer asks the same as *Keep the hardware clock in local time?*

### Disks and controllers

* **No disk is shown**: the firmware may be in Intel RST or "RAID" mode, which hides NVMe or SATA disks from some systems. The kernel on this disc supports Intel VMD. If no disk appears, switch the SATA mode to **AHCI** in the firmware. Windows may then fail to boot until you start it once in safe mode. Think about whether you need that.
* **4K sectors, NVMe-only machines, servers with RAID controllers**: the common controllers (AHCI, NVMe, Intel VMD, mpt3sas, megaraid, hpsa, smartpqi, aacraid) have drivers in the live kernel. Hardware RAID volumes appear as one disk.
* The live system does not start LVM volume groups or RAID arrays by itself. The installer and the repair tool start exactly the ones you select. An existing volume group, RAID array, or half of a Btrfs or ZFS pool on the target disk is shown as *This disk is in use* before anything is erased, and you must agree to switch it off and erase it (step 3 of section 6).

### Older or unusual PCs

Try *More options > Old firmware* (turns off ACPI and APIC) only if the normal entry hangs. For a machine that freezes, try *Compatibility mode*.

## 9. Repair a Gentoo system that does not start

This tool repairs **Gentoo only**. If the computer also has Debian, Ubuntu, Fedora, Arch or another Linux, the tool lists it as *Not a Gentoo system - this repair tool only supports Gentoo* and does not open it or change it.

Start the disc. The best entry is **Repair a system (text console)**: it works on every PC. On the desktop, open **Repair a Gentoo System** in the applications menu (or the button *Repair a Gentoo system* on the installer's Welcome page). In a terminal, run `sudo gentoo-rescue`.

Need the USB port for an external disk? Choose *More options > Text console in RAM* (needs 4 GiB of memory). The disc is copied to memory, you can remove the stick, and you start the tool yourself with `sudo gentoo-rescue`.

### What the tool does

1. It looks at all disks and finds the Gentoo systems. To look, it mounts read-only. It never writes at this stage. Another Linux that it finds is named in the list (so you know it was seen) and marked as not Gentoo.
2. It asks for the passphrase of encrypted (LUKS) disks. Set the keyboard layout first. The live system does not start volume groups or RAID arrays by itself. The tool does it for the ones you select.
3. It mounts the system **read-only** under `/mnt/rescue`, reads it (fstab, boot loader, kernels, logs) and tells you what looks wrong.
4. It offers repairs as a plan. You see the exact commands before anything is written. For risky repairs you type the name of the root device and confirm that you have a backup.
5. It unmounts everything cleanly when you finish, and tells you when you can remove the stick.

It never formats or repartitions a disk. The tool shows a guided menu on a text console. The first choice is *Fix everything recommended*: one plan that does what the diagnosis suggests, in the right order.

### What each repair does

| Repair | What it does |
|---|---|
| Rebuild the initramfs | `dracut --force` (or `genkernel`) for the chosen kernels |
| Regenerate the boot menu | `grub-mkconfig` |
| Reinstall the boot loader | `grub-install` for UEFI or BIOS, or `bootctl` for systemd-boot, then a new menu |
| Reinstall the kernel | `emerge --config`, or a new `emerge` of the kernel package, then initramfs and menu |
| Finish an interrupted package job | `emerge --resume` |

Repairs that do not depend on the boot loader:

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
* **Change options.** On the page with the plan, **Change options** opens the options of the repair. *Rebuild the initramfs* lists the installed kernels: tick the ones to rebuild, or none for all of them. *Reinstall the kernel* asks for the kernel package (empty: the newest installed one) and the method instead: `--param package=sys-kernel/gentoo-kernel-6.10.1 --param mode=remerge`. To rebuild only some initramfs images in a terminal: `--param kernels=VERSION,VERSION`.
* **Read the journal.** The logs page shows the last lines of the text log files the system kept. Many systems keep their messages in a binary journal instead. For those, **Read the journal of the last boot…** runs the system's own `journalctl` as root, so the window asks first: tick *I trust this system and want to read its journal*. You can limit it to errors, or to kernel messages. The system must be open with repairs allowed (not *Look only*). In a terminal: `gentoo-rescue logs SESSION --journal --allow-run-system-program`.

### Another Linux on the same computer

Arch Linux, Debian, Ubuntu, Fedora, openSUSE, Void, Alpine, NixOS and every other system that is not Gentoo are **listed but not opened**. The first page of the repair window shows them under *Other systems*, the text tool prints them without a number, and `gentoo-rescue mount` answers *Not a Gentoo system - this repair tool only supports Gentoo* (exit status 7). Nothing on them is changed, and the tool has no terminal for them. If you want to look at one anyway, mount it yourself, read-only first (section 10, *Mount it*), and use that distribution's own documentation to repair it.

### The repair terminal

Choose *Terminal* (`t`) in the text menu, or run:

```sh
sudo gentoo-chroot                           # finds the system, mounts it, opens a shell, unmounts it when you type exit
sudo gentoo-chroot --read-only               # the same, without the question about changing the system
gentoo-rescue status                         # lists the mounted systems (sessions)
gentoo-chroot SESSION                        # a shell inside the system, read-only; same as: gentoo-rescue chroot SESSION
gentoo-chroot SESSION --writable             # a shell that can change the system
```

* `gentoo-chroot` without a session asks one question: *Open it read-write? Changes are real.* The prompt of the shell starts with `(chroot) `, a short list of the usual fixes for your system (kernel, boot loader, password, fstab) is printed first, and `exit`, a lost terminal or a stop signal unmount everything for you. The repair window has the same as **Open a terminal in this system (chroot)**: one confirmation, and a tick box *Commands I type here change the installed system* for a terminal that can change files.
* With a SESSION the tool asks you to type the name of the root device to open the terminal.
* A **writable** terminal of a SESSION also asks you to type `I HAVE A BACKUP`. In it you can fix what the tool cannot (install packages, edit files).
* A read-only terminal does not change the system. Use it for looking around and for copying files out.
* Programs of the repaired system run as root with full rights while you are in the terminal. If you do not trust the system (malware), do not run its programs: use the read-only inspection and copy your data out.
* `exit` leaves the terminal. The session stays mounted until you unmount it: `gentoo-rescue unmount`, or *Unmount and quit* in the menu. Unmount before you restart.
* `gentoo-rescue audit SESSION` shows everything the tool did.

### A black screen with the NVIDIA driver

When the system uses the NVIDIA driver, the repair tool also checks that a kernel module exists for every kernel in /boot and that its version is the one of the installed driver, and it looks for `nvidia-drm.fbdev=1` on the kernel command line (Gentoo warns it can break the text console). A card **NVIDIA graphics** then offers three repairs, each shown as exact commands first:

* **Rebuild the NVIDIA kernel modules** runs `emerge --oneshot @module-rebuild` and then `dracut --force --regenerate-all`. It needs the network only when the driver file is not in `/var/cache/distfiles`.
* **Remove nvidia-drm.fbdev=1** edits the files the boot entries are made from (a backup copy is kept beside each) and rebuilds the boot menu.
* **Switch to the open-source driver (nouveau)** needs no network: it unmerges `x11-drivers/nvidia-drivers`, changes `VIDEO_CARDS` from `nvidia` to `nouveau`, switches off a block on nouveau in `/etc/modprobe.d`, removes the NVIDIA kernel arguments and rebuilds the initramfs and the boot menu. It is refused when the kernel has no nouveau driver. Mesa gets its nouveau support at the next system update.

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

Use this when you prefer the shell, or for a system the tool does not repair (any Linux that is not Gentoo). Replace the device names with yours (`lsblk -f` shows file systems, labels and UUIDs). Run the commands as the live user with `sudo`.

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
sudo mount /dev/nvme0n1p1 /mnt/system/efi        # the EFI system partition; older installs use /boot/efi
for d in dev proc sys run; do
  sudo mount --rbind /$d /mnt/system/$d && sudo mount --make-rslave /mnt/system/$d
done
sudo chroot /mnt/system /bin/bash                # use /bin/sh if there is no bash
```

(The repair tool keeps its own mounts below `/mnt/rescue`. Use another directory, such as `/mnt/system`, for mounts you make by hand.) Inside the chroot `efibootmgr` works when the live disc was started in UEFI mode (`ls /sys/firmware/efi` exists). When you are done: `exit`, then `sudo umount -R /mnt/system`. Do not run `rm -rf` inside the chroot while these mounts exist.

**Rebuild the boot loader and initramfs**

| System | Commands inside the chroot |
|---|---|
| Gentoo | `ls /boot /lib/modules` · initramfs: `dracut --force --kver <version>` (or `genkernel initramfs`) · `grub-install --target=x86_64-efi --efi-directory=/efi --bootloader-id=Gentoo` (BIOS: `grub-install /dev/sda`) · `grub-mkconfig -o /boot/grub/grub.cfg` |
| Any | A forgotten password: `passwd root` (or `passwd youruser`). A wrong disk in `/etc/fstab`: compare with `blkid`, edit the UUIDs, check with `findmnt --verify`. |

**UEFI boot entry missing** (the firmware forgot the entry, or the disk was moved): `efibootmgr -v` lists them. Some firmware only looks at the fallback path, so copying the loader there makes the disk bootable anywhere: `mkdir -p /efi/EFI/BOOT && cp /efi/EFI/Gentoo/grubx64.efi /efi/EFI/BOOT/BOOTX64.EFI`. A new entry: `efibootmgr --create --disk /dev/nvme0n1 --part 1 --loader '\EFI\Gentoo\grubx64.efi' --label "Gentoo"`.

**File system problems** (the file system must **not** be mounted; for encrypted disks unlock first): look before repairing: `sudo e2fsck -n /dev/sdXN` (ext4), `sudo btrfs check --readonly /dev/sdXN`, `sudo xfs_repair -n /dev/sdXN`. Repair only after a backup (`ddrescue` for a failing disk): `sudo e2fsck -f /dev/sdXN`, `sudo xfs_repair /dev/sdXN`. NTFS volumes that Windows left "dirty" (Fast Startup) are not repaired here: boot Windows and shut it down properly.

A system that is not Gentoo (Debian, Ubuntu, Fedora, Arch, openSUSE, ...) follows the same idea: mount the system, bind `dev`, `proc`, `sys` and `run`, `chroot`, rebuild the initramfs, reinstall the boot loader. Use that distribution's own documentation for the exact commands. This project has not tested them.

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

* Start on non-x86 machines.
* Start an installed system that uses GRUB, the default, while Secure Boot is on. The disc itself starts with Secure Boot on, the system it installs does not yet (section 3).
* Show an accelerated desktop on a GPU whose kernel driver the live kernel (Linux 7.2) lacks. The `.packages.txt` file next to the ISO names the exact version. The *Safe graphics* entry (generic `simpledrm`, no 3D) or the text mode entries still work (section 4).
* Run on hardware released after the image was made if the kernel has no driver for it. Wi-Fi, graphics and sound chips of the newest generation are the usual cases.
* Repair or install ZFS roots, or read BitLocker volumes (Windows).
* Repair a Linux that is not Gentoo (section 9). It lists such a system but does not open it.
* Read the screen aloud or show it in braille: the disc has no screen reader, no speech output and no braille software.
* Replace a backup. Repairs and installations can destroy data. Read what the tool shows before you confirm.

## 14. The AI rescue stick (optional)

The same disc has one more boot entry, **AI rescue (text console)**. It starts a text console where an AI coding assistant (Claude Code, Codex or OpenCode) can help you find out why a Gentoo system does not start. You still decide every change.

* You do not need it. The normal repair tool (section 9) needs no account, no network and no AI, and it is the better choice when your disk holds secrets you must not send to a cloud service: **whatever the assistant reads is sent to its maker's servers.**
* The assistants are not on the disc. You download them the first time (`gentoo-rescue-agents install`), at a fixed version with a checked checksum, and log in with your own account by scanning a QR code with your phone (`gentoo-rescue-login`).
* Your system is mounted read-only at `/mnt/gentoo` (`gentoo-rescue-mount`). The assistant runs as an ordinary user with no way to root. To run a command as root it asks, and **you type the password of that boot on the PC's own keyboard, on a separate console, for that one command.**
* `gentoo-rescue-share` shows the session in your phone's browser. It is encrypted, needs a password that is new for every share, and accepts one client at a time; the phone can never approve a command.
* Read the whole page before you use it: [RESCUE-AI.md](RESCUE-AI.md). It explains the risks, the approval, Claude Code's terms, and what has not been tested on real hardware (none of it has run on a real PC yet).
