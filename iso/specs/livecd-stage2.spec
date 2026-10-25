subarch: amd64
version_stamp: @VERSION_STAMP@
target: livecd-stage2
rel_type: 23.0-default
profile: default/linux/amd64/23.0/desktop
snapshot_treeish: @TREEISH@
# The extension is part of the path: Catalyst's file_check otherwise globs '<path>.*' and, besides the
# archive, finds the reuse receipt (<archive>.receipt.json) next to it - an 'Ambiguous Filename' error.
# It follows stage1's compression_mode (gzip).
source_subpath: 23.0-default/livecd-stage1-amd64-@VERSION_STAMP@.tar.gz
compression_mode: gzip
portage_confdir: @BUILD_DIR@/portage
repos: @BUILD_DIR@/overlay

# Kernel arguments shared by every boot-menu entry (iso/pins.env: ISO_BOOTARGS). The
# builder renders the same string into boot/grub/grub.cfg (iso/cd_overlay).
#   nodhcp         livecd-tools' autoconfig must not run its own DHCP client; NetworkManager
#                  owns the network interfaces.
#   secureconsole  livecd-tools' fixinittab then logs in only tty1 automatically (as the live
#                  user) and puts a normal login prompt on tty2-tty6. Without it all six
#                  consoles log in by themselves. Do not drop it.
# "dokeymap" is deliberately absent: the initramfs would stop on a ten second keymap prompt
# on every boot. Only the text-console menu entry adds it.
livecd/bootargs: @BOOTARGS@
livecd/depclean: no
livecd/fstype: squashfs
livecd/fsops: -j @JOBS@ -Q 20
livecd/iso: @ISO_NAME@
livecd/type: gentoo-release-livecd
livecd/volid: @VOLID@
livecd/cdtar: /usr/share/catalyst/livecd/cdtar/grub-theme-gentoo_frosted.tar.bz2
livecd/fsscript: @BUILD_DIR@/fsscript-stage2.sh
livecd/root_overlay: @BUILD_DIR@/root_overlay
# Catalyst writes boot/grub/grub.cfg first and rsyncs this directory over the ISO tree
# afterwards (setup_overlay runs after the bootloader step), so the project's menu and the
# SECURE-BOOT-OFF.txt notice replace the generic ones. It must not contain snapshots/.
livecd/overlay: @BUILD_DIR@/cd_overlay
# Becomes /README.txt on the ISO (Catalyst's own text is a generic boot option list).
livecd/readme: @BUILD_DIR@/README-live.txt
# chronyd steps a wrong hardware clock once the network is up. acpid is not started on
# purpose: elogind owns the power, sleep and lid keys (see conf.d/autoconfig and
# powerbtn.sh in the overlay). The same list is checked by scripts/verify-iso.py.
# sshd runs on every flavour (I9): a newcomer can set a password and log in from another
# computer. Root stays locked and the live user has no password, and the drop-in
# etc/ssh/sshd_config.d/10-gentoo-live.conf refuses empty passwords, so nobody can log in
# until `sudo gentoo-remote-access on` (or `sudo passwd root`, the Handbook's way) is used.
# The test-key flavour must not add sshd again (builder-entrypoint.sh configure_test_access).
livecd/rcadd:
	udev|sysinit
	dbus|default
	elogind|boot
	NetworkManager|default
	chronyd|default
	gentoo-installerd|default
	sshd|default
# dev-lang/rust-bin (about 680 MiB) is only needed to build PortageHub in stage1; nothing
# installed depends on it at run time (checked against /var/db/pkg of the r19 image).
livecd/unmerge:
	net-misc/netifrc
	dev-lang/rust-bin
# /etc/ssh/ssh_host_*: sshd runs at every boot and makes its own host keys (its init script runs
# ssh-keygen -A); no key may be shared by all discs, so a key left by a package step is removed.
# Download and documentation caches. Man and info pages stay for the rescue console.
# Static Qt libraries (about 270 MiB) are only needed to link against Qt privately.
# make.conf.old (written by livecd_update) and make.conf.extra (part of portage_confdir) hold
# the builder's job counts and caches; the wrapper reads the extra settings from the build
# directory, not from this copy, and appends the live-safe subset to the final make.conf.
livecd/rm:
	/var/cache/binhost
	/usr/share/doc
	/usr/share/gtk-doc
	/usr/lib64/libQt6*.a
	/etc/portage/make.conf.old
	/etc/portage/make.conf.extra
	/etc/ssh/ssh_host_*
livecd/empty:
	/var/db/repos
	/usr/src

# The kernel is built from source (sys-kernel/gentoo-kernel, Catalyst merges it with USE=-initramfs and
# runs dracut) and not taken as the binary package: the binary has no generic framebuffer DRM driver
# (simpledrm, efidrm, vesadrm), so the "Safe graphics" menu entry (nomodeset) could not show a desktop.
# The kernel-build eclass merges every /etc/kernel/config.d/*.config into its configuration. That
# fragment (50-gentoo-live-safe-graphics.config) is installed by app-admin/gentoo-live-installer[safe-graphics]
# in stage1, so it exists when this stage builds the kernel. Do NOT add a boot/kernel/gentoo/config key:
# for a distribution kernel Catalyst copies that file into /etc/kernel/config.d with os.makedirs (no
# exist_ok), which fails because the directory is already there. The "kerncache" option is not used either:
# Catalyst's kernel cache compares /var/tmp/gentoo.config, which only a genkernel build writes, so for a
# distribution kernel it would never hit. iso/portage/make.conf keeps the binary package of gentoo-kernel
# out of the build (--usepkg-exclude), because a binary built without the fragment would be taken.
# scripts/verify-iso.py reads boot/gentoo-config for the symbols. Costs about 30-60 minutes of build time.
boot/kernel: gentoo
boot/kernel/gentoo/distkernel: yes
boot/kernel/gentoo/sources: sys-kernel/gentoo-kernel
boot/kernel/gentoo/dracut_args: --xz --no-hostonly -a dmsquash-live -a dmsquash-live-ntfs -a mdraid -o btrfs -o crypt -o i18n -o usrmount -o lunmask -o qemu -o qemu-net -o nvdimm -o multipath -i /lib/keymaps /lib/keymaps -I busybox
