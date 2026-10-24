# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

PYTHON_COMPAT=( python3_13 python3_14 )
DISTUTILS_USE_PEP517=setuptools
inherit desktop distutils-r1 systemd

DESCRIPTION="Guided Gentoo live GUI installer, repair tool and privileged daemon"
HOMEPAGE="https://wiki.gentoo.org/wiki/No_homepage"
# The distfile is made by the ISO builder (scripts/build-iso.sh) from this repository.
SRC_URI="${P}.tar.zst"
S="${WORKDIR}/${P}"

LICENSE="GPL-2+"
SLOT="0"
KEYWORDS="amd64"
IUSE="+btrfs +efi secureboot +xfs"
RESTRICT="mirror fetch"

# Everything the daemon and the repair tool run: REQUIRED_TOOLS and OPTIONAL_TOOLS in
# src/gentoo_installerd/inspect.py, TOOLS_WANTED in rescue/hostenv.py, CHECK_TOOLS in
# rescue/fsprobe.py and the host policy in rescue/safety.py. tests/iso/test_ebuilds.py maps
# every program to its package and fails when one is missing here. The file system checks
# of the repair tool (F2FS, exFAT, JFS, NTFS) are not behind flags: a disk from another
# PC can have any of them. coreutils, kmod and zstd are in every Gentoo system anyway; they
# are listed because the repair tool runs timeout, chroot, modprobe and zstd by name.
# lsinitrd comes from dracut; grub-install of the live GRUB (pc and efi-64) is the fallback
# when the repaired system cannot run its own.
RDEPEND="
	acct-group/gentoo-installer
	app-arch/tar
	app-arch/zstd
	app-crypt/gnupg
	app-portage/cpuid2cpuflags
	dev-python/pyside[gui,widgets,${PYTHON_USEDEP}]
	sec-keys/openpgp-keys-gentoo-release
	sys-apps/coreutils
	sys-apps/gptfdisk
	sys-apps/kmod
	sys-apps/nvme-cli
	sys-apps/pciutils
	sys-apps/smartmontools
	sys-apps/util-linux
	sys-block/parted
	sys-boot/grub
	sys-fs/cryptsetup
	sys-fs/dosfstools
	sys-fs/e2fsprogs
	sys-fs/exfatprogs
	sys-fs/f2fs-tools
	sys-fs/jfsutils
	sys-fs/lvm2
	sys-fs/mdadm
	sys-fs/ntfs3g
	sys-kernel/dracut
	sys-process/lsof
	sys-process/psmisc
	virtual/udev
	x11-apps/setxkbmap
	x11-apps/xrandr
	btrfs? ( sys-fs/btrfs-progs )
	efi? ( sys-boot/efibootmgr )
	secureboot? (
		app-crypt/sbsigntools
		sys-boot/mokutil
	)
	xfs? ( sys-fs/xfsprogs )
"
BDEPEND="
	app-arch/zstd
	dev-python/setuptools[${PYTHON_USEDEP}]
"

pkg_nofetch() {
	einfo "${P}.tar.zst is made by the ISO builder from this repository's sources"
	einfo "(scripts/build-iso.sh) and must be in DISTDIR; it cannot be downloaded."
}

src_unpack() {
	tar --zstd -xf "${DISTDIR}/${P}.tar.zst" -C "${WORKDIR}" || die "failed to unpack ${P}.tar.zst"
}

src_install() {
	distutils-r1_src_install
	domenu "${FILESDIR}/gentoo-installer.desktop"
	# The repair tool belongs to the same Python package once the sources contain it. Its
	# launcher starts the installer GUI in repair mode; the console scripts (gentoo-rescue,
	# gentoo-installer-text) come from pyproject.toml and are installed by distutils-r1 when
	# it declares them. gentoo-chroot is the short form of "gentoo-rescue chroot".
	if [[ -d ${S}/src/gentoo_installerd/rescue ]]; then
		domenu "${S}/data/gentoo-rescue.desktop"
		dobin "${FILESDIR}/gentoo-chroot"
	fi
	newinitd "${FILESDIR}/gentoo-installerd.initd" gentoo-installerd
	newconfd "${FILESDIR}/gentoo-installerd.confd" gentoo-installerd
	systemd_dounit "${FILESDIR}/gentoo-installerd.service"
}
