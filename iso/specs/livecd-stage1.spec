subarch: amd64
version_stamp: @VERSION_STAMP@
target: livecd-stage1
rel_type: 23.0-default
profile: default/linux/amd64/23.0/desktop
snapshot_treeish: @TREEISH@
source_subpath: 23.0-default/@STAGE3_BASENAME@
compression_mode: gzip
portage_confdir: @BUILD_DIR@/portage
repos: @BUILD_DIR@/overlay

livecd/use:
	-aac
	compile-locales
	cryptsetup
	device-mapper
	fbcon
	livecd
	lvm
	mdadm
	networkmanager
	policykit
	pulseaudio
	qt6
	wayland

livecd/packages:
	app-admin/gentoo-live-installer
	app-admin/sudo
	app-admin/syslog-ng
	app-admin/testdisk
	app-arch/7zip
	app-arch/cpio
	app-arch/zstd
	app-cdr/cdrtools
	app-crypt/gnupg
	sec-keys/openpgp-keys-gentoo-release
	app-editors/mousepad
	app-editors/nano
	app-misc/hwmonux
	app-misc/livecd-tools
	app-misc/screen
	app-misc/tmux
	app-portage/eix
	app-portage/gentoolkit
	app-portage/portage-utils
	app-portage/portagehub
	app-shells/bash-completion
	dev-debug/strace
	dev-python/pyside
	dev-qt/qtsvg
	dev-qt/qttools
	dev-qt/qtwayland
	dev-vcs/git
	gnome-extra/nm-applet
	gnome-extra/polkit-gnome
	gui-libs/display-manager-init
	media-fonts/dejavu
	media-libs/mesa
	net-misc/chrony
	net-misc/networkmanager
	net-misc/openssh
	net-misc/rsync
	net-wireless/iw
	net-wireless/wpa_supplicant
	sys-apps/acl
	sys-apps/attr
	sys-apps/busybox
	sys-apps/dmidecode
	sys-apps/ethtool
	sys-apps/gptfdisk
	sys-apps/hwinfo
	sys-apps/inxi
	sys-apps/iproute2
	sys-apps/kexec-tools
	sys-apps/nvme-cli
	sys-apps/pciutils
	sys-apps/smartmontools
	sys-apps/usbutils
	sys-apps/util-linux
	sys-auth/elogind
	sys-auth/polkit
	sys-block/gparted
	sys-block/open-iscsi
	sys-block/parted
	sys-boot/efibootmgr
	sys-boot/grub
	sys-boot/os-prober
	sys-firmware/ipw2100-firmware
	sys-firmware/ipw2200-firmware
	sys-firmware/sof-firmware
	sys-fs/bcache-tools
	sys-fs/btrfs-progs
	sys-fs/cryptsetup
	sys-fs/ddrescue
	sys-fs/dmraid
	sys-fs/dosfstools
	sys-fs/e2fsprogs
	sys-fs/exfatprogs
	sys-fs/f2fs-tools
	=sys-fs/fuse-2.9.9-r2
	sys-fs/genfstab
	sys-fs/jfsutils
	sys-fs/lsscsi
	sys-fs/lvm2
	sys-fs/mdadm
	sys-fs/multipath-tools
	sys-fs/ntfs3g
	sys-fs/squashfs-tools
	sys-fs/xfsprogs
	sys-kernel/linux-firmware
	sys-power/acpid
	sys-process/htop
	sys-process/lsof
	x11-base/xorg-drivers
	x11-base/xorg-server
	x11-misc/lightdm
	x11-misc/lightdm-gtk-greeter
	x11-terms/xfce4-terminal
	xfce-base/xfce4-meta
	xfce-extra/xfce4-whiskermenu-plugin
