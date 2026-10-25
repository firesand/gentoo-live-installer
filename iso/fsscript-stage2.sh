#!/bin/bash
set -eo pipefail

# Distribution profile scripts may read unset optional environment variables.
source /etc/profile
if [[ -f /tmp/envscript ]]; then
	source /tmp/envscript
fi
set -u

# /mnt/gentoo is the installer's target. /mnt/rescue holds the repair tool's sessions (one
# directory per session, made by the tool); only root may look into it.
install -d -m 0755 /mnt/gentoo
install -d -m 0700 /mnt/rescue
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
	/usr/bin/gentoo-remote-access \
	/usr/bin/portagehub \
	/usr/libexec/portagehub-helper \
	/usr/libexec/polkit-gnome-authentication-agent-1; do
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
	# the repair tool on the live side (hostenv.TOOLS_WANTED, fsprobe.CHECK_TOOLS and the
	# host policy of src/gentoo_installerd/rescue; tests/iso/test_ebuilds.py keeps them in step)
	blockdev mount umount dmesg script setsid chroot timeout sync modprobe
	dumpe2fs tune2fs xfs_db jfs_fsck lsinitrd pvs vgs vgchange zstd
)
for command_name in "${required_commands[@]}"; do
	command -v "${command_name}" >/dev/null 2>&1 || fail "Required live tool is missing: ${command_name}"
done

# The repair tool and the text installer are parts of the same Python package. When the
# package contains one, its console script must exist as well: the boot menu entries
# "Repair a system" and "Install in text mode" start them. This is a warning here because
# scripts/verify-iso.py (iso/image-contract.json) fails the build for a script that
# pyproject.toml declares, and tests/iso keeps the declaration in step.
if compgen -G '/usr/lib/python3*/site-packages/gentoo_installerd/rescue/__init__.py' >/dev/null; then
	for command_path in /usr/bin/gentoo-rescue /usr/bin/gentoo-chroot; do
		[[ -x "${command_path}" ]] || echo "warning: the rescue package is installed but ${command_path} is missing" >&2
	done
fi
if compgen -G '/usr/lib/python3*/site-packages/gentoo_installer/textui/__init__.py' >/dev/null; then
	[[ -x /usr/bin/gentoo-installer-text ]] \
		|| echo "warning: the text installer package is installed but /usr/bin/gentoo-installer-text is missing" >&2
fi

# The repair tool falls back to the live GRUB when the repaired system cannot run its own
# grub-install. iso/portage/make.conf builds it for GRUB_PLATFORMS="pc efi-64".
for grub_platform_dir in /usr/lib/grub/i386-pc /usr/lib/grub/x86_64-efi; do
	[[ -d "${grub_platform_dir}" ]] || fail "GRUB platform files are missing: ${grub_platform_dir}"
done

# Configuration that keeps the live system away from the PC's own disks and power state.
grep -q '^[[:space:]]*event_activation[[:space:]]*=[[:space:]]*0' /etc/lvm/lvmlocal.conf \
	|| fail "/etc/lvm/lvmlocal.conf must disable event activation"
grep -q '^AUTO -all$' /etc/mdadm.conf || fail "/etc/mdadm.conf must contain 'AUTO -all'"
grep -q '^ACPI="no"$' /etc/conf.d/autoconfig || fail "/etc/conf.d/autoconfig must keep acpid off"
[[ -x /etc/init.d/sshd ]] || fail "sshd is not installed"
[[ -f /etc/ssh/sshd_config.d/10-gentoo-live.conf ]] || fail "the sshd drop-in that refuses empty passwords is missing"
[[ -x /etc/init.d/chronyd ]] || fail "chronyd is not installed"
[[ -f /etc/elogind/logind.conf.d/50-gentoo-live.conf ]] || fail "the logind lid/idle drop-in is missing"
[[ -f /etc/xdg/xfce4/xfconf/xfce-perchannel-xml/thunar-volman.xml ]] || fail "the thunar-volman defaults are missing"

# The live desktop's look (wave 5). The themes, icons and fonts that xsettings.xml, xfwm4.xml and
# the greeter name must be installed, or the desktop would silently fall back to the defaults.
for look_file in \
	/usr/share/themes/Arc-Dark/gtk-3.0/gtk.css \
	/usr/share/themes/Arc-Dark/xfwm4/themerc \
	/usr/share/icons/Papirus-Dark/index.theme \
	/usr/bin/firefox-bin; do
	[[ -e "${look_file}" ]] || fail "The desktop look needs ${look_file}, which is missing"
done
fc-cache -f >/dev/null 2>&1 || true
for font_family in "Fira Sans" "JetBrains Mono"; do
	fc-match -f '%{family}\n' "${font_family}" | grep -q "${font_family}" || fail "The font ${font_family} is not installed"
done
# xfdesktop shows /usr/share/backgrounds/xfce/xfce-x.svg on every monitor that has no setting of its
# own, and per-monitor settings depend on connector names, so the default picture becomes Indigo
# silk. A new xfdesktop that names another file must stop the build, not leave the old wallpaper.
default_backdrop=/usr/share/backgrounds/xfce/xfce-x.svg
grep -aqF "${default_backdrop}" /usr/bin/xfdesktop \
	|| fail "xfdesktop no longer names ${default_backdrop}: update iso/fsscript-stage2.sh and docs/ISO.md"
[[ -f /usr/share/backgrounds/gentoo-live/indigo-silk.svg ]] || fail "the Indigo silk wallpaper is missing"
install -d -m 0755 /usr/share/backgrounds/xfce
ln -sfn ../gentoo-live/indigo-silk.svg "${default_backdrop}"

# The boot menu offers Memtest86+; Catalyst copies these two files to the ISO root.
for memtest in /boot/memtest86plus/memtest64.bios /boot/memtest86plus/memtest.efi64; do
	[[ -s "${memtest}" ]] || fail "Memtest86+ image is missing: ${memtest}"
done
