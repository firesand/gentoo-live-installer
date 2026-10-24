#!/bin/bash
set -eo pipefail

# Distribution profile scripts may read unset optional environment variables.
source /etc/profile
if [[ -f /tmp/envscript ]]; then
	source /tmp/envscript
fi
set -u

# /mnt/gentoo is the installer's target, /mnt/rescue holds the repair tool's sessions.
install -d -m 0755 /mnt/gentoo /mnt/rescue
install -d -m 0700 -o gentoo -g users /home/gentoo
chown -R gentoo:users /home/gentoo
passwd -d gentoo
passwd -l root
usermod -a -G gentoo-installer gentoo
printf 'DISPLAYMANAGER="lightdm"\n' > /etc/conf.d/display-manager
if [[ -f /etc/gentoo-installer-vm-test ]]; then
	# An impossible password permits key-only SSH without unlocking a password.
	usermod -p '*' root
fi

chmod 0755 /etc/init.d/gentoo-installerd /etc/acpi/actions/powerbtn.sh

fail() {
	echo "$*" >&2
	exit 1
}

# Everything the installer, the repair tool and the hardware support run on the live
# side. A missing program must stop the build here, not surface on somebody's PC.
for command_path in \
	/usr/bin/gentoo-installer \
	/usr/bin/gentoo-installerd \
	/usr/bin/hwmonux \
	/usr/bin/portagehub \
	/usr/libexec/portagehub-helper; do
	if [[ ! -x "${command_path}" ]]; then
		fail "Required live application is missing: ${command_path}"
	fi
done
required_commands=(
	# disks, partitions and boot loaders
	lsblk findmnt blkid sgdisk parted partprobe udevadm wipefs mkswap swapon
	mkfs.ext4 e2fsck mkfs.vfat fsck.vfat mkfs.xfs xfs_repair mkfs.btrfs btrfs
	fsck.f2fs fsck.exfat ntfsfix cryptsetup lvm mdadm thin_check dmsetup
	efibootmgr grub-install os-prober mokutil sbverify sbsign
	# diagnostics for hardware and disks
	smartctl nvme hdparm lshw lspci lsusb dmidecode ddrescue testdisk fuser
	cpuid2cpuflags setxkbmap xrandr xset
	# helpers
	chronyd chronyc jq pv links gpg gpgv tar rsync gparted lsof lvs lscpu
)
for command_name in "${required_commands[@]}"; do
	command -v "${command_name}" >/dev/null 2>&1 || fail "Required live tool is missing: ${command_name}"
done

# The repair tool is a separate deliverable of the same Python package. When the package
# contains it, its console scripts must exist as well.
if compgen -G '/usr/lib/python3*/site-packages/gentoo_installerd/rescue/__init__.py' >/dev/null; then
	for command_path in /usr/bin/gentoo-rescue /usr/bin/gentoo-rescue-gui; do
		[[ -x "${command_path}" ]] || echo "warning: the rescue package is installed but ${command_path} is missing" >&2
	done
fi

# Configuration that keeps the live system away from the PC's own disks and power state.
grep -q '^[[:space:]]*event_activation[[:space:]]*=[[:space:]]*0' /etc/lvm/lvmlocal.conf \
	|| fail "/etc/lvm/lvmlocal.conf must disable event activation"
grep -q '^AUTO -all$' /etc/mdadm.conf || fail "/etc/mdadm.conf must contain 'AUTO -all'"
grep -q '^ACPI="no"$' /etc/conf.d/autoconfig || fail "/etc/conf.d/autoconfig must keep acpid off"
[[ -x /etc/init.d/chronyd ]] || fail "chronyd is not installed"
[[ -f /etc/elogind/logind.conf.d/50-gentoo-live.conf ]] || fail "the logind lid/idle drop-in is missing"
[[ -f /etc/xdg/xfce4/xfconf/xfce-perchannel-xml/thunar-volman.xml ]] || fail "the thunar-volman defaults are missing"

# The boot menu offers Memtest86+; Catalyst copies these two files to the ISO root.
for memtest in /boot/memtest86plus/memtest64.bios /boot/memtest86plus/memtest.efi64; do
	[[ -s "${memtest}" ]] || fail "Memtest86+ image is missing: ${memtest}"
done
