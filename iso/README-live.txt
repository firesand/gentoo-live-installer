GENTOO INSTALLER AND RESCUE DISC
================================

This disc starts a complete Gentoo Linux desktop from a USB stick or DVD.
From there you can install Gentoo on a PC or laptop with a guided program,
or repair a Gentoo system that does not start. Nothing on your disks
changes until you ask for it, and the installer shows what it will erase
before it does anything.

If the disc does not start at all, read SECURE-BOOT-OFF.txt first: Secure
Boot has to be turned off for this disc.

1. WRITING THE DISC
-------------------
Write the ISO file to the whole USB stick (not to a partition) with a tool
that copies it byte for byte. On Linux: dd if=gentoo-installer.iso
of=/dev/sdX bs=4M status=progress conv=fsync, where sdX is the stick. On
Windows: Rufus or balenaEtcher in "DD image" mode. Everything on the stick
is erased. Check the .sha512 file next to the download before you write it.
Tools that unpack the files instead of copying the image (the plain "ISO
mode" of some programs) do not give a bootable disc.

2. STARTING
-----------
Open the boot menu of your PC (often F12, F9, F11 or Esc right after power
on) and choose the USB stick, preferably the entry that has "UEFI" in its
name. The menu of this disc appears; the first entry starts after 10
seconds.

  Gentoo installer and rescue desktop
                                  the normal choice
  Desktop in RAM                  copies the disc to memory first (needs 6
                                  GiB of RAM); afterwards you can remove the
                                  stick
  Repair a system (text console)  no desktop. Starts the repair tool for
                                  a Gentoo system that does not start. It
                                  works with any graphics chip. It asks for
                                  a keyboard layout first.
  Install in text mode            no desktop. Starts the text installer,
                                  the same installation as the desktop
                                  wizard. It asks for a keyboard layout
                                  first.
  Text console only               no desktop and no tool: a shell for
                                  experts, headless machines and broken
                                  graphics. It asks for a keyboard layout
                                  first.
  More options (troubleshooting)
    Verbose boot                  shows kernel and initramfs messages
    Compatibility mode            switches off PCIe, NVMe and USB power
                                  saving for machines that freeze or lose
                                  drives. It also disables the graphics
                                  driver, so you get a text console only.
    Serial console                text console on COM1 at 115200 baud
    Old firmware                  no ACPI and no APIC (very old computers)
    IOMMU off                     for boards that hang with DMA remapping
    Text console in RAM           as above, copied to memory (needs 4 GiB)
  Memtest86+                      tests the memory (shown when the disc has
                                  it)
  UEFI firmware settings          opens the firmware setup (UEFI only)
  Continue with the next boot device
                                  leaves the disc (UEFI only)

To change kernel options for one boot, press "e" on an entry, edit the line
that starts with "linux" and press Ctrl+X. Useful options are listed in
section 8.

3. THE SCREEN STAYS BLACK OR THE DESKTOP DOES NOT APPEAR
--------------------------------------------------------
Wait a minute: starting from a USB stick is slow. If nothing appears,
restart and choose "Install in text mode" (to install Gentoo) or "Repair a
system (text console)" (to fix a Gentoo system that does not start). Both work
without a graphics driver, so they work on every PC. The tool starts by
itself after you have chosen the keyboard layout. Choose "Text console only"
if you want a plain shell. Newer graphics chips sometimes need a newer
kernel than this disc has; the release notes say which hardware generations
were tested.

4. NETWORK
----------
Wired networks connect by themselves. For Wi-Fi click the network icon in
the panel or run "nmtui" in a terminal. The clock is corrected automatically
once the network is up. On a network with a login page (hotel, campus) open
the web browser launcher in the panel: it starts the text browser Links in a
terminal, which handles simple login pages (pages that need JavaScript
cannot be completed with it).

5. KEYBOARD LAYOUT
------------------
The desktop starts with a US layout. Change it in Settings > Keyboard >
Layout or with the layout button in the panel. On the text console use, for
example, "sudo loadkeys de". Set the layout before you type a disk password.

6. INSTALLING GENTOO
--------------------
The installer starts with the desktop (or run "gentoo-installer"). It walks
you through the choices step by step: disk, user, language, desktop and
options. THE DISK YOU CHOOSE IS ERASED. The installer never offers the disc
you booted from, and it asks you to type the name of the target disk before
it changes anything. If you do not want to erase a whole disk, prepare free
space with GParted first (the partition editor in the applications menu) and
choose the manual disk layout.

No desktop? Choose "Install in text mode" in the boot menu, or type "sudo
gentoo-installer-text" on a text console. It asks the same questions one at
a time and installs the same system.

Before you start: connect the power cable of a laptop. This disc does not
suspend when the lid is closed or the PC is idle, but nothing can prevent a
flat battery. A Gentoo installation downloads or compiles many packages and
takes from half an hour to several hours.

7. REPAIRING A GENTOO SYSTEM THAT DOES NOT START
------------------------------------------------
Start this disc. Choose "Repair a system (text console)" in the boot menu,
or open "Repair a Gentoo System" in the applications menu of the desktop,
or run "sudo gentoo-rescue" in a terminal. It finds the Gentoo systems on
your disks, asks for the disk password if the disk is encrypted, mounts
everything under /mnt/rescue and offers repairs such as the boot loader,
fstab, the initramfs and the kernel, or a shell inside the system. Disks
are mounted read-only first. Nothing is formatted or partitioned.

Only Gentoo is repaired. Any other Linux on the computer (Debian, Ubuntu,
Fedora, Arch and so on) is listed as "Not a Gentoo system - this repair
tool only supports Gentoo". It is not opened and not changed.

"gentoo-chroot SESSION" opens a shell inside a mounted Gentoo system.
Tools for hard cases are on the disc: smartctl, nvme, ddrescue, testdisk,
fsck for ext4, xfs, btrfs, f2fs, exfat, vfat, jfs and ntfs, cryptsetup,
lvm and mdadm. This disc leaves volume groups and RAID arrays alone until
you select them.

8. BOOT OPTIONS (type them at the end of the "linux" line)
----------------------------------------------------------
  nox                             do not start the desktop (text console)
  dokeymap                        ask for a console keyboard layout while
                                  booting
  nomodeset                       do not load the graphics driver (text
                                  console only)
  nosound                         do not load sound
  nodhcp                          do not start the old DHCP helper (already
                                  set)
  gentoo.autostart=rescue         start the repair tool after the login on
                                  the first console (use it with nox)
  gentoo.autostart=installer-text start the text installer after the login
                                  on the first console (use it with nox)
  rd.live.ram=1                   copy the disc to RAM
  rd.break                        stop in the initramfs shell (advanced)
  console=ttyS0,115200n8          use COM1 as console
  acpi=off noapic                 for very old computers
  intel_iommu=off amd_iommu=off   disable DMA remapping

9. PROBLEMS AND HARDWARE REPORTS
--------------------------------
Hardware that was tested and hardware that is known not to work are listed
in the project documentation (README.md and docs/USER-GUIDE.md in the source
repository). When you report a problem please include the output of:
  sudo dmesg > dmesg.txt ; sudo lspci -nnk > lspci.txt
  sudo dmidecode -t system
  python3 -m gentoo_installer.hardware --json --redact > hardware.json
The installer wizard can save the same facts with "Save diagnostics". The
repair tool keeps a record of every step: "gentoo-rescue audit SESSION".
The installer writes its log to /var/lib/gentoo-installer/install.log
(readable with sudo) and the installer service logs to /var/log/gentoo-
installerd.log.

This disc is for 64-bit x86 computers only. It does not run on Apple Silicon
Macs, Windows-on-ARM laptops or Raspberry Pi boards.
It has no screen reader, speech output or braille support.
