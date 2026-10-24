subarch: amd64
version_stamp: @VERSION_STAMP@
target: livecd-stage2
rel_type: 23.0-default
profile: default/linux/amd64/23.0/desktop
snapshot_treeish: @TREEISH@
source_subpath: 23.0-default/livecd-stage1-amd64-@VERSION_STAMP@
compression_mode: gzip
portage_confdir: @BUILD_DIR@/portage
repos: @BUILD_DIR@/overlay

livecd/bootargs: dokeymap nodhcp secureconsole
livecd/depclean: no
livecd/fstype: squashfs
livecd/fsops: -j @JOBS@ -Q 20
livecd/iso: @ISO_NAME@
livecd/type: gentoo-release-livecd
livecd/volid: GENTOO_INSTALLER_AMD64
livecd/cdtar: /usr/share/catalyst/livecd/cdtar/grub-theme-gentoo_frosted.tar.bz2
livecd/fsscript: @BUILD_DIR@/fsscript-stage2.sh
livecd/root_overlay: @BUILD_DIR@/root_overlay
livecd/rcadd:
	udev|sysinit
	acpid|default
	dbus|default
	elogind|boot
	NetworkManager|default
	gentoo-installerd|default
livecd/unmerge: net-misc/netifrc
livecd/empty:
	/var/db/repos
	/usr/src

boot/kernel: gentoo
boot/kernel/gentoo/distkernel: yes
boot/kernel/gentoo/sources: sys-kernel/gentoo-kernel-bin
boot/kernel/gentoo/dracut_args: --xz --no-hostonly -a dmsquash-live -a dmsquash-live-ntfs -a mdraid -o btrfs -o crypt -o i18n -o usrmount -o lunmask -o qemu -o qemu-net -o nvdimm -o multipath -i /lib/keymaps /lib/keymaps -I busybox
